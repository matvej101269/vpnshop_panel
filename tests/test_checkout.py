import asyncio
import os
import tempfile
import unittest
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
from app.db import Base, Checkout, PendingPayment, FulfillmentJob
from app.main import lava_webhook
from app.services import LavaClient


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + self.temp.name + "/test.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.patches = [patch("app.checkout.SessionLocal", self.sessions),
                        patch("app.main.SessionLocal", self.sessions),
                        patch("app.checkout.get_config", return_value="https://shop.example"),
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

    def choose(self, method="SBP"):
        return self.client.post(self.url, data={"csrf": self.row.csrf, "method": method}, follow_redirects=False)

    def test_page_and_single_invoice(self):
        response = self.client.get(self.url)
        self.assertIn("&lt;Тариф&gt;", response.text)
        self.assertIn("СБП", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(LavaClient, "create_invoice", new_callable=AsyncMock,
                          return_value=("invoice-one", "https://pay.example/one")) as create:
            self.assertEqual(self.choose().status_code, 303)
            self.assertEqual(self.choose("CARD").headers["location"], "https://pay.example/one")
            self.assertEqual(create.await_count, 1)
            self.assertEqual(create.call_args.kwargs["payment_method"], "SBP")
        with self.sessions() as db:
            self.assertIsNone(db.get(PendingPayment, self.row.invoice_id))
            self.assertEqual(db.get(PendingPayment, "invoice-one").telegram_id, 5000000000)

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
            payment = db.get(PendingPayment, "addon-invoice")
            self.assertEqual((payment.product_type, payment.package_id, payment.package_traffic_bytes),
                             ("addon", 4, 5368709120))

    def test_concurrent_choices(self):
        async def run():
            entered, release = asyncio.Event(), asyncio.Event()
            async def create(*args, **kwargs):
                entered.set()
                await release.wait()
                return "invoice-one", "https://pay.example/one"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://shop.example") as client:
                with patch.object(LavaClient, "create_invoice", side_effect=create) as mocked:
                    data = {"csrf": self.row.csrf, "method": "SBP"}
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
            for method, provider in [("SBP", "PAY2ME"), ("CARD", "SMART_GLOCAL")]:
                fake = AsyncMock()
                fake.post.return_value = httpx.Response(200, json={"id": "i", "paymentUrl": "https://pay.example/i"},
                                                        request=httpx.Request("POST", "https://gate.lava.top"))
                with patch("app.services.get_config_map", return_value=cfg), patch("app.services.httpx.AsyncClient") as client:
                    client.return_value.__aenter__.return_value = fake
                    await LavaClient().create_invoice(1, SimpleNamespace(amount=50, currency="RUB"), payment_method=method)
                    payload = fake.post.call_args.kwargs["json"]
                    self.assertEqual((payload["paymentProvider"], payload["paymentMethod"]), (provider, method))
                    self.assertEqual(payload["amount"], 50)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
