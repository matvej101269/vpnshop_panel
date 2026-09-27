import json
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import httpx
from sqlalchemy import select
from app.config import settings as env_settings
from app.db import SessionLocal, Plan, AddonPackage, PendingPayment, Subscription, SubscriptionHistory, ProcessedPayment
from app.runtime_config import get_config_map


class LavaClient:
    async def create_invoice(self, telegram_id: int, plan: Plan, amount: int | None = None) -> tuple[str, str]:
        cfg = get_config_map()
        if not cfg["lava_api_key"] or not cfg["lava_offer_id"]:
            raise RuntimeError("Не настроены LAVA_API_KEY и LAVA_OFFER_ID")
        # Lava requires a syntactically valid email. Use a random placeholder, never Telegram ID
        # or a customer's personal email; example.com is reserved for documentation/examples.
        payload = {"offerId": cfg["lava_offer_id"], "amount": plan.amount if amount is None else amount, "currency": plan.currency,
                   "email": f"{uuid.uuid4().hex}@example.com"}
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
    @staticmethod
    def _inbound_ids(cfg: dict, inbound_ids: list[int] | None = None) -> list[int]:
        values = inbound_ids or [int(v) for v in cfg.get("xui_inbound_ids", "").split(",") if v.strip().isdigit()]
        if not values:
            values = [int(cfg.get("xui_inbound_id", "1"))]
        return sorted(set(int(v) for v in values if int(v) > 0))

    @staticmethod
    def _email(telegram_id: int) -> str:
        return str(telegram_id)

    async def add_or_update_client(self, telegram_id: int, sub_id: str, expiry_ms: int, traffic_limit_bytes: int,
                                   exists: bool, limit_hwid: int = 0, traffic_reset: str = "never",
                                   inbound_ids: list[int] | None = None, enabled: bool = True,
                                   group_name: str = ""):
        cfg = get_config_map()
        if not cfg["xui_base_url"] or (not cfg["xui_api_token"] and not all((cfg["xui_username"], cfg["xui_password"]))):
            raise RuntimeError("Не настроено подключение к 3x-ui")
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        client_email = self._email(telegram_id)
        legacy_email = f"sub-{sub_id}@vpn.invalid"
        client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vpnshop:{sub_id}"))
        targets = self._inbound_ids(cfg, inbound_ids)
        client_data = {"id": client_uuid, "email": client_email, "expiryTime": expiry_ms, "enable": enabled,
                       "limitIp": 0, "limitHwid": max(0, int(limit_hwid)),
                       # 3x-ui's totalGB field is stored in bytes (despite its name).
                       "totalGB": max(0, int(traffic_limit_bytes)), "trafficReset": traffic_reset,
                       "trafficResetDay": 1, "subId": sub_id, "tgId": 0, "group": group_name}
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                headers = {"Authorization": f"Bearer {cfg['xui_api_token']}"}
                if group_name:
                    await self.ensure_group(client, api, group_name, headers)
                if exists:
                    response = await client.post(f"{api}/clients/update/{quote(client_email)}", json=client_data, headers=headers)
                    if response.status_code == 404:
                        response = await client.post(f"{api}/clients/update/{quote(legacy_email)}", json=client_data, headers=headers)
                else:
                    response = await client.post(f"{api}/clients/add", json={
                        "client": client_data, "inboundIds": targets}, headers=headers)
            else:
                login = await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}", data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                login.raise_for_status()
                if group_name:
                    await self.ensure_group(client, api, group_name, {})
                body = client_data
                if exists:
                    for inbound_id in targets:
                        response = await client.post(f"{api}/inbounds/updateClient/{client_uuid}", json={
                            "id": inbound_id, "settings": json.dumps({"clients": [body]})})
                        response.raise_for_status()
                        result = response.json()
                        if result.get("success") is False:
                            raise RuntimeError(f"3x-ui error: {result.get('msg', 'unknown error')}")
                    return
                else:
                    responses = []
                    for inbound_id in targets:
                        current_response = await client.post(f"{api}/inbounds/addClient", json={
                            "id": inbound_id, "settings": json.dumps({"clients": [body]})})
                        current_response.raise_for_status()
                        current_result = current_response.json()
                        if current_result.get("success") is False:
                            raise RuntimeError(f"3x-ui error: {current_result.get('msg', 'unknown error')}")
                        responses.append(current_response)
                    return
            response.raise_for_status()
            result = response.json()
            if result.get("success") is False:
                raise RuntimeError(f"3x-ui error: {result.get('msg', 'unknown error')}")

    async def set_client_inbounds(self, telegram_id: int, sub_id: str, current_ids: list[int], target_ids: list[int]):
        """Reconcile an existing client's inbound attachments in 3x-ui 3.8.5."""
        cfg = get_config_map()
        if not cfg["xui_base_url"] or (not cfg["xui_api_token"] and not all((cfg["xui_username"], cfg["xui_password"]))):
            raise RuntimeError("Не настроено подключение к 3x-ui")
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = self._email(telegram_id)
        previous = set(self._inbound_ids(cfg, current_ids))
        target = set(self._inbound_ids(cfg, target_ids))
        attach, detach = sorted(target - previous), sorted(previous - target)
        if not attach and not detach:
            return
        headers = {"Authorization": f"Bearer {cfg['xui_api_token']}"} if cfg["xui_api_token"] else {}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            if not cfg["xui_api_token"]:
                login = await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}",
                                          data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                login.raise_for_status()
            for action, ids in (("attach", attach), ("detach", detach)):
                if not ids:
                    continue
                response = await client.post(f"{api}/clients/{quote(email)}/{action}",
                                             json={"inboundIds": ids}, headers=headers)
                if response.status_code == 404:
                    legacy = f"sub-{sub_id}@vpn.invalid"
                    response = await client.post(f"{api}/clients/{quote(legacy)}/{action}",
                                                 json={"inboundIds": ids}, headers=headers)
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError:
                    result = {}
                if result.get("success") is False:
                    raise RuntimeError(f"3x-ui {action} error: {result.get('msg') or 'unknown error'}")

    @staticmethod
    async def ensure_group(client: httpx.AsyncClient, api: str, group_name: str, headers: dict):
        response = await client.get(f"{api}/clients/groups", headers=headers)
        response.raise_for_status()
        data = response.json()
        if data.get("success") is False:
            raise RuntimeError(f"3x-ui groups error: {data.get('msg') or 'cannot load groups'}")
        groups = data.get("obj") or []
        if any((item.get("name") if isinstance(item, dict) else str(item)) == group_name for item in groups):
            return
        created = await client.post(f"{api}/clients/groups/create", json={"name": group_name}, headers=headers)
        created.raise_for_status()
        result = created.json()
        if result.get("success") is False:
            # Another payment may have created this group between the list and create requests.
            refreshed = await client.get(f"{api}/clients/groups", headers=headers)
            refreshed.raise_for_status()
            current = refreshed.json().get("obj") or []
            if not any((item.get("name") if isinstance(item, dict) else str(item)) == group_name for item in current):
                raise RuntimeError(f"3x-ui could not create client group: {result.get('msg') or 'unknown error'}")

    async def list_inbounds(self) -> list[dict]:
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            headers = {}
            if cfg["xui_api_token"]:
                headers["Authorization"] = f"Bearer {cfg['xui_api_token']}"
            else:
                login = await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}",
                                          data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                login.raise_for_status()
            response = await client.get(f"{api}/inbounds/list", headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("success") is False:
                raise RuntimeError(data.get("msg") or "3x-ui returned an error")
            items = data.get("obj") or []
            return [{"id": int(item["id"]), "name": str(item.get("remark") or item.get("tag") or f"Inbound {item['id']}"),
                     "protocol": str(item.get("protocol", "")), "port": int(item.get("port") or 0)} for item in items]

    async def list_clients(self) -> list[dict]:
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        headers = {"Authorization": f"Bearer {cfg['xui_api_token']}"} if cfg["xui_api_token"] else {}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            if not cfg["xui_api_token"]:
                response = await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}",
                                              data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                response.raise_for_status()
            response = await client.get(f"{api}/inbounds/list", headers=headers)
            response.raise_for_status()
            data = response.json()
            if data.get("success") is False:
                raise RuntimeError("3x-ui rejected the client list request")
            inbounds = data.get("obj") or []
            clients: dict[str, dict] = {}
            for inbound in inbounds:
                try:
                    settings = inbound.get("settings") or {}
                    settings = json.loads(settings) if isinstance(settings, str) else settings
                    inbound_id = int(inbound.get("id", 0))
                    for item in settings.get("clients", []):
                        email = str(item.get("email", ""))
                        if not email:
                            continue
                        entry = clients.setdefault(email, dict(item, inboundIds=[]))
                        entry["inboundIds"].append(inbound_id)
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
            return list(clients.values())

    async def client_usage(self, sub_id: str, telegram_id: int | None = None) -> int:
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = str(telegram_id) if telegram_id is not None else f"sub-{sub_id}@vpn.invalid"
        legacy_email = f"sub-{sub_id}@vpn.invalid"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                response = await client.get(f"{api}/clients/traffic/{quote(email)}",
                                            headers={"Authorization": f"Bearer {cfg['xui_api_token']}"})
            else:
                await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}", data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                response = await client.get(f"{api}/inbounds/getClientTraffics/{quote(email)}")
            if response.status_code == 404 and email != legacy_email:
                if cfg["xui_api_token"]:
                    response = await client.get(f"{api}/clients/traffic/{quote(legacy_email)}",
                                                headers={"Authorization": f"Bearer {cfg['xui_api_token']}"})
                else:
                    response = await client.get(f"{api}/inbounds/getClientTraffics/{quote(legacy_email)}")
            response.raise_for_status()
            obj = response.json().get("obj") or {}
            return int(obj.get("up", 0) or 0) + int(obj.get("down", 0) or 0)

    async def delete_client(self, sub_id: str, telegram_id: int | None = None):
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = str(telegram_id) if telegram_id is not None else f"sub-{sub_id}@vpn.invalid"
        client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"vpnshop:{sub_id}"))
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                headers = {"Authorization": f"Bearer {cfg['xui_api_token']}"}
                response = await client.post(f"{api}/clients/del/{quote(email)}?keepTraffic=0", headers=headers)
                if response.status_code == 404 and telegram_id is not None:
                    response = await client.post(f"{api}/clients/del/{quote(f'sub-{sub_id}@vpn.invalid')}?keepTraffic=0", headers=headers)
            else:
                await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}", data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                deleted = []
                for inbound_id in self._inbound_ids(cfg):
                    response = await client.post(f"{api}/inbounds/{inbound_id}/delClient/{client_uuid}")
                    if response.status_code != 404:
                        response.raise_for_status()
                        deleted.append(inbound_id)
                if not deleted:
                    raise RuntimeError("3x-ui did not find this client on selected inbounds")
                return
            response.raise_for_status()

    async def reset_client_traffic(self, sub_id: str, telegram_id: int | None = None):
        cfg = get_config_map()
        base = cfg["xui_base_url"].rstrip("/")
        api = base + "/" + cfg["xui_api_base_path"].strip("/")
        email = str(telegram_id) if telegram_id is not None else f"sub-{sub_id}@vpn.invalid"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if cfg["xui_api_token"]:
                response = await client.post(f"{api}/clients/resetTraffic/{quote(email)}",
                                             headers={"Authorization": f"Bearer {cfg['xui_api_token']}"})
            else:
                await client.post(f"{base}/{cfg['xui_login_path'].lstrip('/')}",
                                  data={"username": cfg["xui_username"], "password": cfg["xui_password"]})
                responses = []
                for inbound_id in self._inbound_ids(cfg):
                    current = await client.post(f"{api}/inbounds/{inbound_id}/resetClientTraffic/{quote(email)}")
                    if current.status_code != 404:
                        current.raise_for_status()
                        responses.append(current)
                if not responses:
                    raise RuntimeError("3x-ui did not find traffic for this client on selected inbounds")
                return
            response.raise_for_status()

    async def sync_client(self, telegram_id: int, sub_id: str, expires_at: datetime, traffic_limit_bytes: int, exists: bool):
        await self.add_or_update_client(telegram_id, sub_id, int(expires_at.timestamp() * 1000), traffic_limit_bytes, exists)


def happ_link(sub_id: str) -> str:
    cfg = get_config_map()
    # Happ subscription URLs end with the subscription ID directly; adding a
    # configurable `/sub` segment breaks the panel's subscription endpoint.
    base = cfg["happ_subscription_base"].rstrip("/")
    return f"{base}/{quote(sub_id)}"


def quote_immediate_switch(db, telegram_id: int, plan: Plan, now: datetime | None = None) -> tuple[int, int, int]:
    """Return charge, same-currency unused-value credit and rounded-up service days."""
    now = now or datetime.now(timezone.utc)
    sub = db.get(Subscription, telegram_id)
    credit = 0
    if sub and sub.enabled and sub.currency == plan.currency:
        rows = db.scalars(select(SubscriptionHistory).where(
            SubscriptionHistory.telegram_id == telegram_id,
            SubscriptionHistory.expires_at > now.replace(tzinfo=None)
        )).all()
        for row in rows:
            if row.currency != plan.currency:
                continue
            start = row.starts_at.replace(tzinfo=timezone.utc) if row.starts_at.tzinfo is None else row.starts_at
            end = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
            begin = max(now, start)
            finish = max(begin, end)
            whole = max(1.0, (end - start).total_seconds())
            remaining = max(0.0, (finish - begin).total_seconds())
            credit += int(row.price * min(1.0, remaining / whole))
    price = max(0, int(plan.amount))
    charge = max(0, price - credit)
    if price == 0:
        return 0, credit, max(1, int(plan.days))
    value = max(price, credit)
    seconds = max(1, int(plan.days * 86400 * value / max(1, price)))
    days = max(1, (seconds + 86399) // 86400)
    return charge, credit, days


async def provision_paid_invoice(invoice_id: str) -> tuple[int, str] | str | None:
    with SessionLocal() as db:
        invoice_hash = hashlib.sha256(invoice_id.encode()).hexdigest()
        if db.get(ProcessedPayment, invoice_hash):
            return "duplicate"
        payment = db.get(PendingPayment, invoice_id)
        if not payment:
            return None
        telegram_id, plan_id = payment.telegram_id, payment.plan_id
        current = db.get(Subscription, telegram_id)
        if payment.product_type == "addon":
            package = db.get(AddonPackage, payment.package_id) if payment.package_id else None
            if not package or not current:
                return None
            addon_bytes = payment.package_traffic_bytes or int(package.traffic_gb * (1024 ** 3))
            # Unlimited users stay unlimited; for limited users, extend the cap without resetting used traffic.
            new_limit = 0 if current.traffic_limit_bytes == 0 else current.traffic_limit_bytes + addon_bytes
            try:
                inbound_ids = [int(value) for value in current.inbound_ids.split(",") if value.isdigit()]
            except ValueError:
                inbound_ids = []
            await XUIClient().add_or_update_client(
                telegram_id, current.sub_id, int(current.expires_at.replace(tzinfo=timezone.utc).timestamp() * 1000),
                new_limit, exists=True, limit_hwid=current.limit_hwid, traffic_reset=current.traffic_reset,
                inbound_ids=inbound_ids or None, enabled=current.enabled, group_name=current.plan_name)
            current.traffic_limit_bytes = new_limit
            db.add(ProcessedPayment(payment_hash=invoice_hash))
            db.delete(payment)
            db.commit()
            return telegram_id, ""
        plan = db.get(Plan, plan_id)
        if not plan:
            return None
        has_snapshot = payment.plan_days_snapshot > 0
        plan_name = payment.plan_name_snapshot if has_snapshot else plan.name
        plan_amount = payment.plan_amount_snapshot if has_snapshot else plan.amount
        plan_currency = payment.plan_currency_snapshot if has_snapshot else plan.currency
        plan_days = payment.plan_days_snapshot if has_snapshot else plan.days
        plan_traffic_gb = payment.plan_traffic_gb_snapshot if has_snapshot else plan.traffic_limit_gb
        plan_hwid = payment.plan_hwid_snapshot if has_snapshot else plan.limit_hwid
        plan_reset = payment.plan_reset_snapshot if has_snapshot else plan.traffic_reset
        now = datetime.now(timezone.utc)
        immediate_switch = bool(payment.immediate_switch)
        current_exp = current.expires_at.replace(tzinfo=timezone.utc) if current and current.expires_at.tzinfo is None else (current.expires_at if current else now)
        starts_at = now if immediate_switch else max(now, current_exp)
        service_days = max(1, int(payment.switch_days or plan_days))
        expires = starts_at + timedelta(days=service_days)
        sub_id = current.sub_id if current else uuid.uuid4().hex[:20]
        plan_bytes = int(plan_traffic_gb * (1024 ** 3))
        new_traffic_limit = plan_bytes
        configured_inbounds = XUIClient._inbound_ids(get_config_map())
        await XUIClient().add_or_update_client(telegram_id, sub_id, int(expires.timestamp() * 1000),
                                               new_traffic_limit, exists=current is not None,
                                               limit_hwid=plan_hwid, traffic_reset=plan_reset,
                                               inbound_ids=configured_inbounds, group_name=plan_name)
        if current and immediate_switch:
            # The user is moved now, so unused old periods cannot be credited a second time.
            for row in db.scalars(select(SubscriptionHistory).where(
                SubscriptionHistory.telegram_id == telegram_id,
                SubscriptionHistory.expires_at > now.replace(tzinfo=None)
            )).all():
                row_start = row.starts_at.replace(tzinfo=timezone.utc) if row.starts_at.tzinfo is None else row.starts_at
                row.expires_at = (row.starts_at if row_start >= now else now.replace(tzinfo=None))
            # Keep traffic counters intact so retrying a webhook cannot erase usage twice.
        if current:
            current.expires_at = expires
            current.plan_id = plan.id
            current.enabled = True
            current.reminded = ""
            current.plan_name = plan_name
            current.current_price = plan_amount
            current.currency = plan_currency
            current.traffic_limit_bytes = new_traffic_limit
            current.limit_hwid = plan_hwid
            current.traffic_reset = plan_reset
            current.inbound_ids = ",".join(str(value) for value in configured_inbounds)
        else:
            db.add(Subscription(telegram_id=telegram_id, sub_id=sub_id, expires_at=expires, enabled=True,
                                plan_id=plan.id,
                                plan_name=plan_name, current_price=plan_amount, currency=plan_currency,
                                traffic_limit_bytes=new_traffic_limit, limit_hwid=plan_hwid,
                                traffic_reset=plan_reset,
                                inbound_ids=",".join(str(value) for value in configured_inbounds)))
        history_value = (payment.charged_amount + payment.credit_amount) if immediate_switch else (payment.charged_amount or plan_amount)
        db.add(SubscriptionHistory(telegram_id=telegram_id, plan_name=plan_name, plan_days=service_days,
                                   price=history_value, currency=plan_currency, traffic_limit_bytes=plan_bytes,
                                   starts_at=starts_at.replace(tzinfo=None), expires_at=expires.replace(tzinfo=None)))
        db.add(ProcessedPayment(payment_hash=invoice_hash))
        db.delete(payment)
        db.commit()
        return telegram_id, happ_link(sub_id)
