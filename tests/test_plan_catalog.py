import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from app import db as models, bot, main
from app.db import Base, Plan, PlanPeriod, UserCurrency, PendingPayment, Checkout, PromoCode, PromoSelection
from app.plan_catalog import parse_periods, save_periods, period_plan, periods_for, visible_plans
from datetime import datetime, timedelta


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.temp.name + "/catalog.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.sessions() as db:
            for currency, amount in [("RUB", 100), ("USD", 5), ("EUR", 6)]:
                plan = Plan(name="Базовая " + currency, currency=currency, days=30, amount=amount)
                db.add(plan)
                db.flush()
                save_periods(db, plan, [(1, 30, amount), (3, 90, amount * 2), (6, 180, amount * 4)])
            db.add(UserCurrency(telegram_id=5000000000, currency="RUB"))
            db.commit()
            self.rub = db.scalar(select(Plan).where(Plan.currency == "RUB")).id
            self.usd = db.scalar(select(Plan).where(Plan.currency == "USD")).id
            self.period = db.scalar(select(PlanPeriod).where(PlanPeriod.plan_id == self.rub, PlanPeriod.months == 3)).id

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def callback(self, data):
        return SimpleNamespace(data=data, from_user=SimpleNamespace(id=5000000000),
            message=SimpleNamespace(answer=AsyncMock()), answer=AsyncMock())

    def test_currency_catalog_and_persistence(self):
        with self.sessions() as db:
            plans = visible_plans(db, "RUB")
            self.assertEqual([p.id for p in plans], [self.rub])
            text, keyboard = bot.catalog_view(db, 5000000000)
            self.assertIn("RUB", text)
            self.assertFalse(any("USD" in b.text for row in keyboard.inline_keyboard for b in row))
        callback = self.callback("currency:USD:buy:0")
        with patch("app.bot.SessionLocal", self.sessions):
            asyncio.run(bot.choose_currency(callback))
        with self.sessions() as db:
            self.assertEqual(db.get(UserCurrency, 5000000000).currency, "USD")

    def test_period_snapshot_and_promo_survive_price_edits(self):
        with self.sessions() as db:
            promo = PromoCode(code="TEST", discount_percent=25, plan_ids=str(self.rub), expires_at=datetime.now() + timedelta(days=1))
            db.add(promo)
            db.flush()
            db.add(PromoSelection(telegram_id=5000000000, promo_id=promo.id))
            db.commit()
        callback = self.callback(f"buy:{self.rub}:{self.period}")
        with patch("app.bot.SessionLocal", self.sessions), patch("app.checkout.get_config", return_value="https://shop.example"):
            asyncio.run(bot.create_plan_order(callback, False))
        with self.sessions() as db:
            pending = db.scalar(select(PendingPayment))
            self.assertIsNotNone(pending)
            self.assertEqual((pending.plan_days_snapshot, pending.plan_amount_snapshot, pending.charged_amount), (90, 200, 150))
            self.assertEqual(pending.plan_currency_snapshot, "RUB")
            self.assertIn("3 месяца", db.scalar(select(Checkout)).title)
            db.get(PlanPeriod, self.period).amount = 999
            db.commit()
            self.assertEqual(pending.plan_amount_snapshot, 200)

    def test_forged_period_and_wrong_currency_do_not_create_order(self):
        with self.sessions() as db:
            other_period = periods_for(db, self.usd)[0].id
        with patch("app.bot.SessionLocal", self.sessions), patch("app.checkout.get_config", return_value="https://shop.example"):
            asyncio.run(bot.create_plan_order(self.callback(f"buy:{self.rub}:{other_period}"), False))
            asyncio.run(bot.create_plan_order(self.callback(f"buy:{self.usd}:{other_period}"), False))
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(PendingPayment)), 0)

    def test_admin_period_validation_and_save(self):
        for form in [{}, {"period_1_enabled": "on", "period_1_amount": "-5"}, {"period_3_enabled": "on", "period_3_amount": "bad"}]:
            with self.assertRaises(ValueError):
                parse_periods(form)
        values = parse_periods({"period_1_enabled": "on", "period_1_amount": "10", "period_6_enabled": "on", "period_6_amount": "45"})
        with self.sessions() as db:
            plan = db.get(Plan, self.rub)
            save_periods(db, plan, values)
            db.commit()
            self.assertEqual([(p.days, p.amount) for p in periods_for(db, plan.id)], [(30, 10), (180, 45)])
            self.assertEqual(plan.amount, 10)
            with self.assertRaises(ValueError):
                period_plan(plan, db.get(PlanPeriod, self.period))

    def test_upgrade_preserves_legacy_days_and_is_idempotent(self):
        with self.sessions() as db:
            legacy = Plan(name="Старый", days=45, amount=777, currency="RUB")
            db.add(legacy)
            db.commit()
            legacy_id = legacy.id
        with patch("app.db.engine", self.engine), patch("app.db.SessionLocal", self.sessions):
            models.init_db()
            models.init_db()
        with self.sessions() as db:
            periods = periods_for(db, legacy_id)
            self.assertEqual(len(periods), 1)
            self.assertEqual((periods[0].months, periods[0].days, periods[0].amount), (0, 45, 777))

    def test_admin_create_periods_and_currency_dropdown(self):
        app = FastAPI()
        app.add_api_route("/admin/subscriptions", main.create_plan, methods=["POST"])
        app.dependency_overrides[main.admin] = lambda: None
        async def form(request):
            return await request.form()
        values = {"name": "Премиум", "currency": "USD", "period_1_enabled": "on", "period_1_amount": "7",
                  "period_3_enabled": "on", "period_3_amount": "18", "enabled": "on", "show_in_bot": "on"}
        with patch("app.main.SessionLocal", self.sessions), patch("app.main.checked_form", side_effect=form), TestClient(app) as client:
            self.assertEqual(client.post("/admin/subscriptions", data=values, follow_redirects=False).status_code, 303)
            self.assertEqual(client.post("/admin/subscriptions", data=values | {"currency": "GBP"}).status_code, 400)
        with self.sessions() as db:
            plan = db.scalar(select(Plan).where(Plan.name == "Премиум"))
            self.assertEqual([(p.days, p.amount) for p in periods_for(db, plan.id)], [(30, 7), (90, 18)])
            env = Environment(loader=FileSystemLoader("app/templates"), autoescape=True)
            html = env.get_template("admin_subscriptions.html").render(plans=[plan], plan_users={plan.id: 0},
                plan_periods={plan.id: {p.months: p for p in periods_for(db, plan.id)}})
            self.assertIn('name="currency"', html)
            self.assertIn('name="period_6_amount"', html)
            self.assertIn('value="18"', html)

    def test_manual_connection_uses_selected_period(self):
        app = FastAPI()
        app.add_api_route("/admin/users", main.create_user, methods=["POST"])
        app.dependency_overrides[main.admin] = lambda: None
        async def form(request):
            return await request.form()
        with patch("app.main.SessionLocal", self.sessions), patch("app.main.checked_form", side_effect=form), \
             patch("app.main.get_config_map", return_value={}), \
             patch("app.main.XUIClient.add_or_update_client", new_callable=AsyncMock), \
             patch("app.main.notify_user", new_callable=AsyncMock), TestClient(app) as client:
            response = client.post("/admin/users", data={"telegram_id": "5000000001", "plan_id": f"{self.rub}:{self.period}"}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
        with self.sessions() as db:
            history = db.scalar(select(models.SubscriptionHistory))
            self.assertEqual((history.plan_days, history.price, history.currency), (90, 200, "RUB"))


if __name__ == "__main__":
    unittest.main()
