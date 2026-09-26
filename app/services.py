import json
import uuid
import hashlib
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import httpx
from app.config import settings as env_settings
from app.db import SessionLocal, Plan, PendingPayment, Subscription, SubscriptionHistory, ProcessedPayment
from app.runtime_config import get_config_map


class LavaClient:
    async def create_invoice(self, telegram_id: int, plan: Plan) -> tuple[str, str]:
        cfg = get_config_map()
        if not cfg["lava_api_key"] or not cfg["lava_offer_id"]:
            raise RuntimeError("Не настроены LAVA_API_KEY и LAVA_OFFER_ID")
        # Lava's invoice API accepts an email field; use a random non-routable alias, not Telegram ID.
        payload = {"offerId": cfg["lava_offer_id"], "amount": plan.amount, "currency": plan.currency,
                   "email": f"{uuid.uuid4().hex}@users.invalid"}
        if cfg["lava_payment_provider"]:
            payload["paymentProvider"] = cfg["lava_payment_provider"]
        async with httpx.AsyncClient(timeout=20) as client:
            invoice_url = cfg["lava_api_url"].rstrip("/") + "/" + cfg["lava_invoice_path"].lstrip("/")
            response = await client.post(invoice_url, json=payload,
                                         headers={"X-Api-Key": cfg["lava_api_key"], "Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
        invoice_id = data.get("id") or data.get("invoiceId") or data.get("contractId")
        pay_url = data.get("url") or data.get("paymentUrl") or data.get("payment_url")
        if not invoice_id or not pay_url:
            raise RuntimeError("Lava.top returned an unexpected invoice response")
        return str(invoice_id), str(pay_url)


class XUIClient:
    async def add_or_update_client(self, telegram_id: int, sub_id: str, expiry_ms: int, traffic_limit_bytes: int, exists: bool):
        cfg = get_config_map()
        if not cfg["xui_base_url"] or (not cfg["xui_api_token"] and not all((cfg["xui_username"], cfg["xui_password"]))):
            raise RuntimeError("Не настроено подключение к 3x-ui")
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        # 3x-ui requires a client email value. Derive it from the random subscription token,
        # so the panel does not receive the Telegram ID or an actual email address.
        client_email = f"sub-{sub_id}@vpn.invalid"
        client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vpnshop:{sub_id}"))
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                headers = {"Authorization": f"Bearer {cfg['xui_api_token']}"}
                body = {"id": client_uuid, "email": client_email, "expiryTime": expiry_ms, "enable": True,
                        "limitIp": 0, "totalGB": math.ceil(traffic_limit_bytes / (1024 ** 3)), "subId": sub_id, "tgId": 0}
                if exists:
                    response = await client.post(f"{api}/clients/update/{quote(client_email)}", json=body, headers=headers)
                else:
                    response = await client.post(f"{api}/clients/add", json={
                    "client": body, "inboundIds": [int(cfg["xui_inbound_id"]) ]}, headers=headers)
            else:
                login = await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}", data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                login.raise_for_status()
                body = {"id": client_uuid, "email": client_email, "enable": True, "expiryTime": expiry_ms,
                        "limitIp": 0, "totalGB": math.ceil(traffic_limit_bytes / (1024 ** 3)), "subId": sub_id, "tgId": ""}
                if exists:
                    response = await client.post(f"{api}/inbounds/updateClient/{client_uuid}", json={
                        "id": int(cfg["xui_inbound_id"]), "settings": json.dumps({"clients": [body]})})
                else:
                    response = await client.post(f"{api}/inbounds/addClient", json={
                        "id": int(cfg["xui_inbound_id"]), "settings": json.dumps({"clients": [body]})})
            response.raise_for_status()
            result = response.json()
            if result.get("success") is False:
                raise RuntimeError(f"3x-ui error: {result.get('msg', 'unknown error')}")

    async def client_usage(self, sub_id: str) -> int:
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = f"sub-{sub_id}@vpn.invalid"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                response = await client.get(f"{api}/clients/traffic/{quote(email)}",
                                            headers={"Authorization": f"Bearer {cfg['xui_api_token']}"})
            else:
                await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}", data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                response = await client.get(f"{api}/inbounds/getClientTraffics/{quote(email)}")
            response.raise_for_status()
            obj = response.json().get("obj") or {}
            return int(obj.get("up", 0) or 0) + int(obj.get("down", 0) or 0)

    async def delete_client(self, sub_id: str):
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = f"sub-{sub_id}@vpn.invalid"
        client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vpnshop:{sub_id}"))
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                headers = {"Authorization": f"Bearer {cfg['xui_api_token']}"}
                response = await client.post(f"{api}/clients/del/{quote(email)}?keepTraffic=0", headers=headers)
            else:
                await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}", data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                response = await client.post(f"{api}/inbounds/{int(cfg['xui_inbound_id'])}/delClient/{client_uuid}")
            response.raise_for_status()

    async def reset_client_traffic(self, sub_id: str):
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = f"sub-{sub_id}@vpn.invalid"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                response = await client.post(f"{api}/clients/resetTraffic/{quote(email)}",
                                             headers={"Authorization": f"Bearer {cfg['xui_api_token']}"})
            else:
                await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}",
                                  data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                response = await client.post(f"{api}/inbounds/{int(cfg['xui_inbound_id'])}/resetClientTraffic/{quote(email)}")
            response.raise_for_status()

    async def sync_client(self, telegram_id: int, sub_id: str, expires_at: datetime, traffic_limit_bytes: int, exists: bool):
        await self.add_or_update_client(telegram_id, sub_id, int(expires_at.timestamp() * 1000), traffic_limit_bytes, exists)


def happ_link(sub_id: str) -> str:
    cfg = get_config_map()
    base, path = cfg["happ_subscription_base"].rstrip("/"), cfg["happ_subscription_path"].strip("/")
    return f"{base}/{path}/{quote(sub_id)}"


async def provision_paid_invoice(invoice_id: str) -> tuple[int, str] | str | None:
    with SessionLocal() as db:
        invoice_hash = hashlib.sha256(invoice_id.encode()).hexdigest()
        if db.get(ProcessedPayment, invoice_hash):
            return "duplicate"
        payment = db.get(PendingPayment, invoice_id)
        if not payment:
            return None
        telegram_id, plan_id = payment.telegram_id, payment.plan_id
        plan = db.get(Plan, plan_id)
        current = db.get(Subscription, telegram_id)
        now = datetime.now(timezone.utc)
        current_exp = current.expires_at.replace(tzinfo=timezone.utc) if current and current.expires_at.tzinfo is None else (current.expires_at if current else now)
        starts_at = max(now, current_exp)
        expires = starts_at + timedelta(days=plan.days)
        sub_id = current.sub_id if current else uuid.uuid4().hex[:20]
        plan_bytes = int(plan.traffic_limit_gb * (1024 ** 3))
        new_traffic_limit = plan_bytes
        await XUIClient().add_or_update_client(telegram_id, sub_id, int(expires.timestamp() * 1000),
                                               new_traffic_limit, exists=current is not None)
        if current:
            await XUIClient().reset_client_traffic(sub_id)
        if current:
            current.expires_at = expires
            current.plan_id = plan.id
            current.enabled = True
            current.reminded = ""
            current.plan_name = plan.name
            current.current_price = plan.amount
            current.currency = plan.currency
            current.traffic_limit_bytes = new_traffic_limit
        else:
            db.add(Subscription(telegram_id=telegram_id, sub_id=sub_id, expires_at=expires, enabled=True,
                                plan_id=plan.id,
                                plan_name=plan.name, current_price=plan.amount, currency=plan.currency,
                                traffic_limit_bytes=new_traffic_limit))
        db.add(SubscriptionHistory(telegram_id=telegram_id, plan_name=plan.name, plan_days=plan.days,
                                   price=plan.amount, currency=plan.currency, traffic_limit_bytes=plan_bytes,
                                   starts_at=starts_at.replace(tzinfo=None), expires_at=expires.replace(tzinfo=None)))
        db.add(ProcessedPayment(payment_hash=invoice_hash))
        db.delete(payment)
        db.commit()
        return telegram_id, happ_link(sub_id)
