import asyncio
import os
import tempfile
import re
import unittest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from app import checkout
from app.db import Base, Checkout, CheckoutQuote, PaymentAttempt, PendingPayment, FulfillmentJob
from app.main import lava_webhook
from app.services import LavaClient
from app.payment_options import METHODS, convert_amount, parse_rates


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.temp.name + "/test.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch("app.checkout.SessionLocal", self.sessions),
                        patch("app.main.SessionLocal", self.sessions),
                        patch("app.checkout.get_config", return_value="https://shop.example"),
                        patch("app.checkout.exchange_rates", new_callable=AsyncMock,
                              return_value=({"RUB": Decimal(1), "USD": Decimal(100), "EUR": Decimal(125)}, "2026-09-28")),
                        patch("app.main.get_config", return_value="webhook-secret")]
        for item in self.patches:
            item.start()
        app = FastAPI()
        app.include_router(checkout.router)
        app.add_api_route("/webhooks/lava", lava_webhook, methods=["POST"])
        self.app = app
        self.client = TestClient(app)
        with self.sessions() as db:
            local_id, url = checkout.new_checkout(db, "<Тариф>", 50, "RUB")
            db.add(PendingPayment(invoice_id=local_id, telegram_id=5000000000,
                                  plan_id=1, charged_amount=50, plan_name_snapshot="Тариф"))
            db.commit()
            self.row = db.scalar(select(Checkout))
        self.url = "/pay/" + self.row.token

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def quote_token(self, currency="RUB"):
        response = self.client.get(self.url + "?change=1&currency=" + currency)
        match = re.search(r'name="quote" value="([^"]+)"', response.text)
        return match.group(1) if match else ""

    def choose(self, method="SBP", currency="RUB", **extra):
        return self.client.post(self.url + "?change=1&currency=" + currency,
                                data={"csrf": self.row.csrf, "method": method,
                                      "quote": self.quote_token(currency), **extra}, follow_redirects=False)

    def test_page_and_single_invoice(self):
        response = self.client.get(self.url)
        self.assertIn("&lt;Тариф&gt;", response.text)
        self.assertIn("СБП", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("invoice-one", "https://pay.example/one")) as create:
            self.assertEqual(self.choose().status_code, 303)
            self.assertEqual(self.choose("SBP").headers["location"], "https://pay.example/one")
            self.assertEqual(create.await_count, 1)
            self.assertEqual(create.call_args.kwargs["payment_method"], "SBP")
        with self.sessions() as db:
            self.assertEqual(db.get(PendingPayment, self.row.invoice_id).telegram_id, 5000000000)
            self.assertEqual(db.scalar(select(PaymentAttempt)).invoice_id, "invoice-one")

    def test_webhook_amount_currency_and_duplicate(self):
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("invoice-one", "https://pay.example/one")):
            self.choose()
        body = {"eventType": "payment.success", "contractId": "invoice-one", "amount": 49, "currency": "RUB"}
        headers = {"X-Api-Key": "webhook-secret"}
        self.assertEqual(self.client.post("/webhooks/lava", json=body).status_code, 401)
        for amount, currency in [(49, "RUB"), (50, "USD"), ("NaN", "RUB"), (None, "RUB")]:
            body.update(amount=amount, currency=currency)
            self.assertEqual(self.client.post("/webhooks/lava", json=body, headers=headers).status_code, 400)
        body.update(amount="50.00", currency="RUB")
        self.assertTrue(self.client.post("/webhooks/lava", json=body, headers=headers).json()["queued"])
        self.assertTrue(self.client.post("/webhooks/lava", json=body, headers=headers).json()["duplicate"])
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(FulfillmentJob)), 1)
            self.assertEqual(db.get(Checkout, self.row.token).state, "paid")

    def test_rejects_csrf_method_and_expiry(self):
        self.assertEqual(self.client.post(self.url, data={"method": "SBP"}).status_code, 403)
        self.assertEqual(self.choose("BAD").status_code, 400)
        with self.sessions() as db:
            db.get(Checkout, self.row.token).created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
            db.commit()
        self.assertEqual(self.choose().status_code, 410)

    def test_timeout_never_creates_second_invoice(self):
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          side_effect=httpx.ReadTimeout("timeout")) as create:
            self.choose()
            self.choose()
            self.assertEqual(create.await_count, 1)
        with self.sessions() as db:
            self.assertEqual(db.get(Checkout, self.row.token).state, "failed")

    def test_provider_rejection_allows_other_method(self):
        response = httpx.Response(422, request=httpx.Request("POST", "https://gate.lava.top"))
        error = httpx.HTTPStatusError("rejected", request=response.request, response=response)
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          side_effect=[error, ("invoice-one", "https://pay.example/one")]) as create:
            self.assertEqual(self.choose().status_code, 502)
            self.assertEqual(self.choose("CARD").status_code, 303)
            self.assertEqual(create.await_count, 2)

    def test_addon_keeps_traffic_snapshot(self):
        with self.sessions() as db:
            payment = db.get(PendingPayment, self.row.invoice_id)
            payment.product_type, payment.package_id = "addon", 4
            payment.package_traffic_bytes = 5368709120
            db.commit()
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("addon-invoice", "https://pay.example/addon")):
            self.choose("CARD")
        with self.sessions() as db:
            payment = db.get(PendingPayment, self.row.invoice_id)
            self.assertEqual((payment.product_type, payment.package_id, payment.package_traffic_bytes),
                             ("addon", 4, 5368709120))

    def test_concurrent_choices(self):
        quote_token = self.quote_token()
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            async def create(*args, **kwargs):
                entered.set()
                await release.wait()
                return "invoice-one", "https://pay.example/one"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://shop.example") as client:
                with patch.object(LavaClient, "create_invoice", side_effect=create) as mocked:
                    data = {"csrf": self.row.csrf, "method": "SBP", "quote": quote_token}
                    first = asyncio.create_task(client.post(self.url, data=data))
                    await asyncio.wait_for(entered.wait(), 3)
                    second = await client.post(self.url, data=data)
                    release.set()
                    await first
                    self.assertEqual(second.status_code, 303)
                    self.assertEqual(mocked.call_count, 1)
        asyncio.run(run())

    def test_lava_method_payload(self):
        cfg = {"lava_api_key": "test", "lava_offer_id": "offer", "lava_payment_provider": "PAYPAL",
               "lava_api_url": "https://gate.lava.top", "lava_invoice_path": "/api/v3/invoice"}
        async def run():
            for currency, method in [(c, m) for c in METHODS for m in METHODS[c]]:
                provider = METHODS[currency][method][1]
                fake = AsyncMock()
                fake.post.return_value = httpx.Response(200, json={"id": "i", "paymentUrl": "https://pay.example/i"},
                                                        request=httpx.Request("POST", "https://gate.lava.top"))
                with patch("app.services.get_config_map", return_value=cfg), patch("app.services.httpx.AsyncClient") as client:
                    client.return_value.__aenter__.return_value = fake
                    await LavaClient().create_invoice(1, SimpleNamespace(amount=Decimal("50.25"), currency=currency),
                                                     payment_method=method, return_url="https://shop.example/pay/test",
                                                     full_name="Test User", wallet_id="+34600111222")
                    payload = fake.post.call_args.kwargs["json"]
                    self.assertEqual(payload["paymentProvider"], provider)
                    self.assertEqual(payload.get("paymentMethod"), None if method == "PAYPAL" else "CARD" if method == "CARD_PAY2ME" else method)
                    self.assertEqual(payload["amount"], 50.25)
                    self.assertEqual(payload["cancel_return_url"], "https://shop.example/pay/test?change=1")
        asyncio.run(run())

    def webhook(self, invoice_id, amount, currency="RUB"):
        return self.client.post("/webhooks/lava", headers={"X-Api-Key": "webhook-secret"}, json={
            "eventType": "payment.success", "contractId": invoice_id, "amount": amount, "currency": currency})

    def test_currency_filter_conversion_and_invalid_combination(self):
        page = self.client.get(self.url + "?currency=USD")
        self.assertIn("0.50 USD", page.text)
        self.assertNotIn('value="SBP"', page.text)
        self.assertIn('value="APPLE_PAY"', page.text)
        self.assertEqual(self.choose("SBP", "USD").status_code, 400)
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("usd-invoice", "https://pay.example/usd")) as mocked:
            self.assertEqual(self.choose("PAYPAL", "USD").status_code, 303)
            product = mocked.call_args.args[1]
            self.assertEqual((product.amount, product.currency), (Decimal("0.50"), "USD"))
        self.assertEqual(self.webhook("usd-invoice", 50).status_code, 400)
        self.assertTrue(self.webhook("usd-invoice", "0.50", "USD").json()["queued"])

    def test_switch_and_old_invoice_payment_only_fulfills_once(self):
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock, side_effect=[
            ("old", "https://pay.example/old"), ("new", "https://pay.example/new")]) as mocked:
            self.choose()
            self.assertIn("Сменить способ оплаты", self.client.get(self.url).text)
            self.choose("CARD", "USD")
            self.assertEqual(mocked.await_count, 2)
        self.assertTrue(self.webhook("old", 50).json()["queued"])
        self.assertTrue(self.webhook("new", "0.50", "USD").json()["additional_payment"])
        self.assertTrue(self.webhook("new", "0.50", "USD").json()["duplicate"])
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(FulfillmentJob)), 1)
            self.assertEqual(db.scalar(select(PaymentAttempt).where(PaymentAttempt.invoice_id == "new")).state, "extra_paid")

    def test_payment_during_method_switch_does_not_reopen_checkout(self):
        quote_token = self.quote_token("USD")
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("old", "https://pay.example/old")):
            self.choose()
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            async def create(*args, **kwargs):
                entered.set()
                await release.wait()
                return "new", "https://pay.example/new"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://shop.example") as client:
                with patch.object(LavaClient, "create_invoice", side_effect=create):
                    task = asyncio.create_task(client.post(self.url, data={"csrf": self.row.csrf,
                        "method": "CARD", "quote": quote_token}))
                    await asyncio.wait_for(entered.wait(), 3)
                    result = await client.post("/webhooks/lava", headers={"X-Api-Key": "webhook-secret"}, json={
                        "eventType": "payment.success", "contractId": "old", "amount": 50, "currency": "RUB"})
                    self.assertEqual(result.status_code, 200)
                    release.set()
                    response = await task
                    self.assertEqual(response.headers["location"], self.url)
        asyncio.run(run())
        with self.sessions() as db:
            self.assertEqual(db.get(Checkout, self.row.token).state, "paid")

    def test_legacy_checkout_invoice_survives_switch(self):
        with self.sessions() as db:
            db.get(PendingPayment, self.row.invoice_id).invoice_id = "legacy"
            row = db.get(Checkout, self.row.token)
            row.invoice_id, row.payment_url, row.state, row.method = "legacy", "https://pay.example/legacy", "ready", "CARD"
            db.commit()
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("new", "https://pay.example/new")):
            self.choose("SBP")
        self.assertTrue(self.webhook("legacy", 50).json()["queued"])
        self.assertTrue(self.webhook("new", 50).json()["additional_payment"])

    def test_expired_quote_cannot_silently_change_amount(self):
        token = self.quote_token("USD")
        with self.sessions() as db:
            db.get(CheckoutQuote, token).expires_at = checkout.now() - timedelta(seconds=1)
            db.commit()
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock) as create:
            response = self.client.post(self.url, data={"csrf": self.row.csrf, "quote": token, "method": "CARD"})
            self.assertEqual(response.status_code, 409)
            create.assert_not_called()

    def test_quote_belongs_to_order_and_bizum_requires_details(self):
        token = self.quote_token("EUR")
        self.assertEqual(self.choose("BIZUM", "EUR").status_code, 400)
        with self.sessions() as db:
            db.get(CheckoutQuote, token).checkout_token = "other-order"
            db.commit()
        response = self.client.post(self.url, data={"csrf": self.row.csrf, "quote": token, "method": "CARD"})
        self.assertEqual(response.status_code, 409)

    def test_fx_outage_keeps_base_currency_available(self):
        with patch("app.checkout.exchange_rates", new_callable=AsyncMock, side_effect=ValueError("offline")):
            self.assertNotIn('class="payment-form"', self.client.get(self.url + "?currency=EUR").text)
            self.assertIn('class="payment-form"', self.client.get(self.url).text)

    def test_rate_nominal_and_rounding(self):
        day = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        rates, _ = parse_rates(f'<ValCurs Date="{day}"><Valute><CharCode>USD</CharCode><Value>1000,00</Value><Nominal>10</Nominal></Valute><Valute><CharCode>EUR</CharCode><Value>125,00</Value><Nominal>1</Nominal></Valute></ValCurs>'.encode())
        self.assertEqual(convert_amount(50, "RUB", "USD", rates)[0], Decimal("0.50"))
        self.assertEqual(convert_amount(1, "USD", "EUR", rates)[0], Decimal("0.80"))
        with self.assertRaises(ValueError):
            parse_rates(b'<ValCurs Date="01.01.2000"/>')


if __name__ == "__main__":
    unittest.main()
