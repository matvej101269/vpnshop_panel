import asyncio
import base64
import binascii
import json
import hashlib
import logging
import re
import secrets
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import FastAPI, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import httpx
from sqlalchemy import select, func, and_, or_
from urllib.parse import unquote, urlsplit
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings as env_settings
from app.db import (init_db, SessionLocal, Plan, AddonPackage, AddonBalance, TrialClaim, BotMenuNode, PendingPayment, FulfillmentJob, ProcessedPayment, Subscription,
                    SubscriptionHistory, AdminAccount, PromoCode, PromoSelection,
                    PromoRedemption, ReferralReward)
from app.services import (provision_paid_invoice, reconcile_addon_balances, XUIClient,
                          happ_link, upstream_happ_link)
from app.backups import create_backup, backup_directory
from app.bot import start_bot, notify_user, happ_bridge_url, routing_deep_link
from app.runtime_config import (init_runtime_config, get_config, save_config, config_status,
                                verify_admin, save_admin_account, CONFIG_DEFAULTS, SECRET_KEYS,
                                make_csrf_token, verify_csrf_token, get_config_map,
                                create_admin_session, verify_admin_session)
from app.runtime_config import decrypt_handoff
from app.checkout import router as checkout_router, matches_payment
from app.db import Checkout

templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=env_settings.timezone)
bot_task: asyncio.Task | None = None
SECTIONS = {"overview", "users", "subscriptions", "addons", "history", "settings", "botmenu", "referrals", "system"}
SECRET_LABELS = {
    "bot_token": "name_bot_token", "lava_api_key": "name_lava_api_key", "lava_offer_id": "name_lava_offer_id",
    "lava_webhook_key": "name_lava_webhook_key", "xui_password": "name_xui_password",
    "xui_api_token": "name_xui_api_token", "xui_username": "name_xui_username",
}


def admin(request: Request):
    username = verify_admin_session(request.cookies.get("vpnshop_admin", ""))
    if not username:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    request.state.admin_username = username


async def checked_form(request: Request):
    form = await request.form()
    username = getattr(request.state, "admin_username", "")
    if not verify_csrf_token(username, str(form.get("csrf_token", ""))):
        raise HTTPException(status_code=403, detail="Invalid form token")
    return form


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def remaining_days(expires: datetime) -> int:
    return max(0, (as_utc(expires).date() - datetime.now(timezone.utc).date()).days)


def sanitized_error(exc: Exception) -> str:
    detail = re.sub(r"https?://\S+", "[URL]", str(exc))
    detail = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]", detail, flags=re.I)
    detail = re.sub(r"\b\d{6,20}\b", "[ID]", detail)
    return f"{type(exc).__name__}: {detail[:300]}"


async def send_reminders():
    offsets = {int(v.strip()) for v in get_config("reminder_days").split(",") if v.strip().isdigit()}
    now = datetime.now(timezone.utc)
    try:
        local_zone = ZoneInfo(get_config("timezone"))
    except (ZoneInfoNotFoundError, ValueError):
        local_zone = timezone.utc
    local_today = now.astimezone(local_zone).date()
    with SessionLocal() as db:
        subscriptions = db.scalars(select(Subscription).where(Subscription.enabled.is_(True))).all()
        messages = []
        for sub in subscriptions:
            left = (as_utc(sub.expires_at).astimezone(local_zone).date() - local_today).days
            if left in offsets and str(left) not in sub.reminded.split(","):
                messages.append((sub.telegram_id, left, sub.expires_at))
    for tg_id, left, expiry in messages:
        try:
            await notify_user(tg_id, f"Срок VPN-подписки заканчивается через {left} дн. Продлите её в боте командой /start.")
            with SessionLocal() as db:
                sub = db.get(Subscription, tg_id)
                if sub and sub.expires_at == expiry:
                    sent = set(filter(None, sub.reminded.split(",")))
                    sent.add(str(left))
                    sub.reminded = ",".join(sorted(sent))
                    db.commit()
        except Exception:
            pass


async def purge_old_pending_payments():
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    with SessionLocal() as db:
        active_ids = select(FulfillmentJob.invoice_id).where(FulfillmentJob.status != "done")
        db.query(PendingPayment).filter(PendingPayment.created_at < cutoff, ~PendingPayment.invoice_id.in_(active_ids)).delete(synchronize_session=False)
        db.query(FulfillmentJob).filter(FulfillmentJob.status == "done", FulfillmentJob.created_at < cutoff).delete(synchronize_session=False)
        db.commit()


async def process_payment_jobs():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        stale_claim = now - timedelta(minutes=30)
        eligible = or_(and_(FulfillmentJob.status.in_(["queued", "retry"]),
                            FulfillmentJob.next_attempt_at <= now),
                       and_(FulfillmentJob.status == "processing",
                            or_(FulfillmentJob.claimed_at.is_(None),
                                FulfillmentJob.claimed_at < stale_claim)))
        jobs = db.scalars(select(FulfillmentJob).where(eligible)
                          .order_by(FulfillmentJob.created_at)
                          .limit(1).with_for_update(skip_locked=True)).all()
        ids = [job.invoice_id for job in jobs]
        for job in jobs:
            job.status = "processing"
            job.claimed_at = now
        db.commit()
    for invoice_id in ids:
        try:
            with SessionLocal() as db:
                job = db.get(FulfillmentJob, invoice_id)
                already_notifying = bool(job and job.notification_pending)
                telegram_id = job.telegram_id if job else None
                product_type = job.product_type if job else "plan"
            provision = "duplicate" if already_notifying else await provision_paid_invoice(invoice_id)
            if provision is None:
                raise RuntimeError("Связь оплаты с заказом пока не найдена")
            if isinstance(provision, tuple):
                telegram_id, link = provision
                with SessionLocal() as db:
                    job = db.get(FulfillmentJob, invoice_id)
                    job.telegram_id = telegram_id
                    job.product_type = product_type
                    job.notification_pending = True
                    db.commit()
            else:
                link = ""
                if not telegram_id:
                    raise RuntimeError("Не удалось определить получателя уведомления")
                if product_type != "addon":
                    with SessionLocal() as db:
                        sub = db.get(Subscription, telegram_id)
                        if sub:
                            from app.services import happ_link
                            link = happ_link(sub.sub_id)
            if product_type == "addon":
                message = ("Оплата подтверждена! Разовый пакет трафика добавлен. Он расходуется после квоты тарифа; "
                           "остаток пакета сохраняется при сбросе трафика.")
                keyboard = None
            else:
                bridge = happ_bridge_url(f"happ://add/{link}") if link else ""
                message = ("Оплата подтверждена! Нажмите кнопку, чтобы открыть Happ и импортировать подписку."
                           if bridge else f"Оплата подтверждена! Добавьте ссылку в Happ:\n{link}" if link
                           else "Оплата подтверждена, подписка активирована.")
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть подписку в Happ", url=bridge)]]) if bridge else None
            await notify_user(int(telegram_id), message, reply_markup=keyboard)
            with SessionLocal() as db:
                job = db.get(FulfillmentJob, invoice_id)
                if job:
                    job.status = "done"
                    job.notification_pending = False
                    job.last_error = ""
                    db.commit()
        except Exception as exc:
            with SessionLocal() as db:
                job = db.get(FulfillmentJob, invoice_id)
                if job:
                    job.attempts += 1
                    delay = min(3600, 30 * (2 ** min(job.attempts, 7)))
                    job.status = "retry"
                    job.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=delay)
                    # Keep only a short, non-sensitive error class/message; never persist payment payloads or URLs.
                    job.last_error = sanitized_error(exc)
                    db.commit()


async def reconcile_subscriptions():
    """Compare the minimal stored subscription state to 3x-ui without storing panel payloads."""
    checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        clients = await XUIClient().list_clients()
        by_email = {str(item.get("email", "")): item for item in clients}
        with SessionLocal() as db:
            subscriptions = db.scalars(select(Subscription)).all()
            global_inbounds = XUIClient._inbound_ids(get_config_map())
            for sub in subscriptions:
                remote = by_email.get(str(sub.telegram_id)) or by_email.get(f"sub-{sub.sub_id}@vpn.invalid")
                target_inbounds = ({int(value) for value in sub.inbound_ids.split(",") if value.strip().isdigit()}
                                   if sub.inbound_ids else set(global_inbounds))
                if not remote:
                    sub.sync_status = "client_missing"
                elif bool(remote.get("enable", True)) != bool(sub.enabled):
                    sub.sync_status = "enabled_differs"
                elif abs(int(remote.get("expiryTime", 0) or 0) - int(as_utc(sub.expires_at).timestamp() * 1000)) > 120000:
                    sub.sync_status = "expiry_differs"
                elif sub.traffic_limit_bytes != int(remote.get("totalGB", 0) or 0):
                    sub.sync_status = "traffic_differs"
                elif sub.limit_hwid != int(remote.get("limitHwid", 0) or 0):
                    sub.sync_status = "hwid_differs"
                elif sub.traffic_reset != str(remote.get("trafficReset") or "never"):
                    sub.sync_status = "traffic_reset_differs"
                elif target_inbounds and set(remote.get("inboundIds") or []) != target_inbounds:
                    sub.sync_status = "inbounds_differ"
                else:
                    sub.sync_status = "synced"
                sub.sync_checked_at = checked_at
            db.commit()
    except Exception as exc:
        with SessionLocal() as db:
            db.query(Subscription).filter(Subscription.enabled.is_(True)).update(
                {Subscription.sync_status: "check_error", Subscription.sync_checked_at: checked_at},
                synchronize_session=False)
            db.commit()
        logger.warning("3x-ui reconciliation failed: %s", type(exc).__name__)


async def restart_bot_runtime():
    global bot_task
    if bot_task and not bot_task.done():
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
    token = get_config("bot_token")
    bot_task = asyncio.create_task(start_bot(token)) if token else None


async def call_control_api(method: str, path: str, timeout: float = 30):
    if not env_settings.control_api_token:
        raise HTTPException(status_code=503, detail="Системное управление не настроено. Обновите .env на VPS и перезапустите Compose.")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(method, env_settings.control_api_url.rstrip("/") + path,
                                            headers={"Authorization": f"Bearer {env_settings.control_api_token}"})
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise HTTPException(status_code=502, detail=f"Сервис управления вернул ошибку: {detail}")
        return response
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Сервис управления недоступен. Проверьте Docker Compose.") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    if env_settings.app_role == "all":
        init_db()
        init_runtime_config()
    elif env_settings.app_role == "web":
        # Schema and first-boot secrets are initialized by the one-shot init service.
        with SessionLocal() as db:
            db.execute(select(func.count()).select_from(AdminAccount)).scalar_one()
    else:
        raise RuntimeError("The web application can only run with APP_ROLE=web or all")
    if env_settings.app_role == "all":
        scheduler.add_job(send_reminders, "interval", hours=6, id="reminders", replace_existing=True)
        scheduler.add_job(purge_old_pending_payments, "interval", hours=6, id="pending-retention", replace_existing=True)
        scheduler.add_job(process_payment_jobs, "interval", seconds=1, id="payment-fulfillment", replace_existing=True,
                          max_instances=1, coalesce=True)
        scheduler.add_job(reconcile_addon_balances, "interval", seconds=60, id="traffic-addons",
                          replace_existing=True, max_instances=1, coalesce=True)
        scheduler.add_job(create_backup, "interval", hours=24, id="daily-backup", replace_existing=True,
                          next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
        scheduler.add_job(reconcile_subscriptions, "interval", hours=24, id="3xui-reconciliation", replace_existing=True,
                          next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
        scheduler.start()
        await restart_bot_runtime()
    yield
    if env_settings.app_role == "all":
        scheduler.shutdown(wait=False)
        if bot_task:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)


app = FastAPI(title="VPN Shop", lifespan=lifespan)
app.include_router(checkout_router)


@app.middleware("http")
async def disable_admin_caching(request: Request, call_next):
    configured_prefix = "/" + (get_config("panel_uri_path").strip("/") or "admin")
    original_path = request.scope.get("path", "")
    if configured_prefix != "/admin" and (original_path == configured_prefix or original_path.startswith(configured_prefix + "/")):
        suffix = original_path[len(configured_prefix):]
        mapped_path = "/admin" + suffix
        request.scope["path"] = mapped_path
        request.scope["raw_path"] = mapped_path.encode("utf-8")
    response = await call_next(request)
    if original_path.startswith("/admin") or original_path == configured_prefix or original_path.startswith(configured_prefix + "/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    if configured_prefix != "/admin" and response.headers.get("content-type", "").startswith("text/html"):
        chunks = [chunk async for chunk in response.body_iterator]
        body = b"".join(chunks).replace(b"/admin", configured_prefix.encode("utf-8"))
        async def rewritten_body():
            yield body
        response.body_iterator = rewritten_body()
        response.headers["content-length"] = str(len(body))
    location = response.headers.get("location", "")
    if configured_prefix != "/admin" and (location == "/admin" or location.startswith("/admin/")):
        response.headers["location"] = configured_prefix + location[len("/admin"):]
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    if not verify_admin(username, password):
        return templates.TemplateResponse(request, "login.html", {
            "error": "Неверный логин или пароль"}, status_code=401)
    response = RedirectResponse("/" + (get_config("panel_uri_path").strip("/") or "admin"), status_code=303)
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    response.set_cookie("vpnshop_admin", create_admin_session(username), max_age=12 * 60 * 60,
                        httponly=True, secure=request.url.scheme == "https" or forwarded_proto == "https",
                        samesite="strict", path="/")
    return response


@app.post("/logout")
async def logout(request: Request, _: None = Depends(admin)):
    await checked_form(request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("vpnshop_admin", path="/")
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


def _normalize_routing_profile(rules: str) -> str:
    """Accept JSON or a Happ routing deep link and return compact JSON."""
    for prefix in ("happ://routing/add/", "happ://routing/onadd/"):
        if rules.startswith(prefix):
            encoded = unquote(rules[len(prefix):]).strip()
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            rules = decoded.decode("utf-8")
            break
    profile = json.loads(rules)
    if not isinstance(profile, dict):
        raise ValueError("Routing profile must be a JSON object")
    compact = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
    if len(compact.encode("utf-8")) > 8000:
        raise ValueError("Routing profile is too large")
    return compact


@app.get("/happ/open/{token}", response_class=HTMLResponse)
async def happ_open_bridge(token: str):
    """HTTPS handoff page that immediately tries the Happ app link, with a manual fallback."""
    if len(token) > 12000 or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise HTTPException(status_code=400, detail="Некорректная ссылка Happ")
    try:
        target = decrypt_handoff(token)
    except ValueError as exc:
        # Keep already-delivered links working; newly generated handoffs are encrypted.
        try:
            padded = token + "=" * (-len(token) % 4)
            target = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error):
            raise HTTPException(status_code=400, detail="Некорректная ссылка Happ") from exc
    try:
        handoff_payload = json.loads(target)
    except (TypeError, json.JSONDecodeError):
        handoff_payload = None
    if isinstance(handoff_payload, dict) and handoff_payload.get("action") == "apply_routing":
        try:
            sub_id = str(handoff_payload["sub_id"])
            routing_rules = _normalize_routing_profile(str(handoff_payload["rules"]))
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail="Некорректный профиль маршрутизации") from exc
        with SessionLocal() as db:
            sub = db.scalar(select(Subscription).where(Subscription.sub_id == sub_id))
            if not sub or not sub.enabled or as_utc(sub.expires_at) <= datetime.now(timezone.utc):
                raise HTTPException(status_code=400, detail="Нет активной подписки для применения правил")
            expires = int((datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp())
            # This temporary marker is consumed by the next successful Happ
            # subscription fetch, then the usual stable URL becomes route-free.
            sub.routing_rules = f"pending:{expires}\n{routing_rules}"
            db.commit()
        target = f"happ://add/{happ_link(sub_id)}"
    if target.startswith("happ://routing/add/"):
        target = target.replace("happ://routing/add/", "happ://routing/onadd/", 1)
    valid = False
    if target.startswith("happ://add/"):
        subscription_url = target.removeprefix("happ://add/")
        parsed_subscription = urlsplit(subscription_url)
        valid = parsed_subscription.scheme == "https" and bool(parsed_subscription.netloc)
    elif target.startswith(("happ://routing/add/", "happ://routing/onadd/")):
        route_data = unquote(target.removeprefix("happ://routing/onadd/").removeprefix("happ://routing/add/"))
        try:
            decoded = base64.b64decode(route_data, validate=True)
            json.loads(decoded)
            valid = bool(decoded) and len(decoded) <= 8000
        except (ValueError, json.JSONDecodeError, binascii.Error):
            valid = False
    if not valid:
        raise HTTPException(status_code=400, detail="Неподдерживаемая ссылка Happ")
    js_target = json.dumps(target, ensure_ascii=True).replace("<", "\\u003c")
    page = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Открыть Happ</title>
<body style="font:16px system-ui;max-width:520px;margin:12vh auto;padding:24px;text-align:center;background:#10151d;color:#eef2f7">
<h2>Открываем Happ</h2><p id="status" aria-live="polite" style="color:#aab5c3">Пробуем открыть приложение и импортировать подписку…</p>
<p><a id="manual" style="color:#8ab4ff" href="#">Если Happ не открылся, нажмите здесь</a></p>
<script>
const target={js_target};
document.getElementById('manual').href=target;
window.setTimeout(()=>{{window.location.href=target}},80);
window.setTimeout(()=>{{document.getElementById('status').textContent='Если приложение не открылось автоматически, используйте ссылку ниже.'}},1400);
</script></body></html>"""
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@app.get("/happ/sub/{sub_id}")
async def happ_subscription_proxy(sub_id: str, request: Request):
    """Proxy a subscription; attach a staged routing profile once after user request."""
    routing_rules = ""
    pending_routing = ""
    with SessionLocal() as db:
        sub = db.scalar(select(Subscription).where(Subscription.sub_id == sub_id))
        if not sub:
            raise HTTPException(status_code=404, detail="Подписка не найдена")
        stored_rules = sub.routing_rules or ""
        marker, separator, staged_rules = stored_rules.partition("\n")
        if separator and marker.startswith("pending:"):
            try:
                if int(marker.removeprefix("pending:")) >= int(datetime.now(timezone.utc).timestamp()):
                    routing_rules = _normalize_routing_profile(staged_rules)
                    pending_routing = stored_rules
                else:
                    sub.routing_rules = ""
                    db.commit()
            except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as exc:
                sub.routing_rules = ""
                db.commit()
                raise HTTPException(status_code=400, detail="Некорректный профиль маршрутизации") from exc
    upstream_url = upstream_happ_link(sub_id)
    upstream = urlsplit(upstream_url)
    if upstream.scheme != "https" or not upstream.netloc:
        raise HTTPException(status_code=503, detail="Не настроен HTTPS URL подписки 3x-ui")
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            upstream_response = await client.get(upstream_url, headers={
                "User-Agent": request.headers.get("user-agent", "Happ"),
                "Accept": request.headers.get("accept", "*/*"),
            })
    except httpx.HTTPError as exc:
        logger.warning("Happ subscription upstream request failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Не удалось получить подписку с 3x-ui") from exc
    if len(upstream_response.content) > 2 * 1024 * 1024:
        raise HTTPException(status_code=502, detail="Ответ подписки превышает допустимый размер")
    forwarded = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
    for name in ("content-type", "content-disposition", "subscription-userinfo", "profile-title",
                 "profile-update-interval", "announce", "support-url", "profile-web-page-url"):
        if name in upstream_response.headers:
            forwarded[name] = upstream_response.headers[name]
    if routing_rules and upstream_response.is_success:
        forwarded["routing"] = unquote(routing_deep_link(routing_rules))
        forwarded["X-Vpnshop-Routing-Attached"] = "1"
    else:
        forwarded["X-Vpnshop-Routing-Attached"] = "0"
    logger.info("Happ subscription response served; status=%s routing_profile=%s",
                upstream_response.status_code, "attached" if forwarded["X-Vpnshop-Routing-Attached"] == "1" else "none")
    if pending_routing and upstream_response.is_success:
        with SessionLocal() as db:
            sub = db.scalar(select(Subscription).where(Subscription.sub_id == sub_id))
            if sub and sub.routing_rules == pending_routing:
                sub.routing_rules = ""
                db.commit()
    return Response(content=upstream_response.content, status_code=upstream_response.status_code,
                    headers=forwarded)


@app.get("/offer", response_class=HTMLResponse)
async def offer_page(request: Request):
    return templates.TemplateResponse(request, "offer.html", {"offer_text": get_config("offer_text")})


@app.post("/webhooks/lava")
async def lava_webhook(request: Request):
    key = get_config("lava_webhook_key")
    if not key or not secrets.compare_digest(request.headers.get("X-Api-Key", ""), key):
        raise HTTPException(status_code=401, detail="Invalid webhook key")
    payload = await request.json()
    event_type = payload.get("eventType") or payload.get("event_type", "")
    if event_type == "payment.success":
        invoice_id = str(payload.get("contractId") or payload.get("invoiceId") or "")
        if not invoice_id:
            raise HTTPException(status_code=400, detail="Missing invoice reference")
        with SessionLocal() as db:
            payment = db.get(PendingPayment, invoice_id, with_for_update=True)
            if not payment:
                if db.get(FulfillmentJob, invoice_id) or db.get(ProcessedPayment, hashlib.sha256(invoice_id.encode()).hexdigest()):
                    return {"ok": True, "duplicate": True}
                raise HTTPException(status_code=503, detail="Payment mapping is not ready")
            existing = db.get(FulfillmentJob, invoice_id)
            if existing:
                return {"ok": True, "duplicate": True}
            checkout = db.scalar(select(Checkout).where(Checkout.invoice_id == invoice_id))
            if checkout:
                if checkout.state not in {"ready", "paid"} or not matches_payment(checkout, payload):
                    raise HTTPException(status_code=400, detail="Payment amount or currency does not match order")
                checkout.state = "paid"
            db.add(FulfillmentJob(invoice_id=invoice_id, status="queued", telegram_id=payment.telegram_id,
                                  product_type=payment.product_type))
            db.commit()
        return {"ok": True, "queued": True}
    return {"ok": True}


@app.get("/admin")
async def admin_root(_: None = Depends(admin)):
    return RedirectResponse("/admin/overview", status_code=303)


@app.get("/admin/{section}", response_class=HTMLResponse)
async def admin_page(section: str, request: Request, page: int = Query(1, ge=1),
                     sort: str = Query("telegram_id"), direction: str = Query("asc"),
                     _: None = Depends(admin)):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    ctx = {"request": request, "section": section,
           "csrf_token": make_csrf_token(getattr(request.state, "admin_username", "admin"))}
    with SessionLocal() as db:
        if section == "overview":
            now = datetime.now(timezone.utc)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
            current_month_end = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            all_plans = db.scalars(select(Plan)).all()
            active_filter = and_(Subscription.enabled.is_(True), Subscription.expires_at > now.replace(tzinfo=None))
            active_count = db.scalar(select(func.count()).select_from(Subscription).where(active_filter)) or 0
            total_count = db.scalar(select(func.count()).select_from(Subscription)) or 0
            first_starts = select(SubscriptionHistory.telegram_id,
                                  func.min(SubscriptionHistory.starts_at).label("first_start")
                                  ).group_by(SubscriptionHistory.telegram_id).subquery()
            current_month = db.scalar(select(func.count()).select_from(first_starts).where(
                first_starts.c.first_start >= month_start)) or 0
            churn = db.scalar(select(func.count()).select_from(Subscription).where(
                Subscription.expires_at >= month_start, Subscription.expires_at < current_month_end)) or 0
            popular = db.execute(select(SubscriptionHistory.plan_name, func.count().label("uses"))
                                 .group_by(SubscriptionHistory.plan_name)
                                 .order_by(func.count().desc(), SubscriptionHistory.plan_name).limit(7)).all()
            expiring_query = select(Subscription).where(
                active_filter, Subscription.expires_at <= (now + timedelta(days=7)).replace(tzinfo=None)
            ).order_by(Subscription.expires_at)
            expiring = db.scalars(expiring_query.limit(8)).all()
            months = []
            for back in range(5, -1, -1):
                index = now.year * 12 + now.month - 1 - back
                year, month_no = divmod(index, 12)
                month = datetime(year, month_no + 1, 1, tzinfo=timezone.utc)
                next_index = index + 1
                next_year, next_month_no = divmod(next_index, 12)
                next_month = datetime(next_year, next_month_no + 1, 1, tzinfo=timezone.utc)
                count_in = db.scalar(select(func.count()).select_from(first_starts).where(
                    first_starts.c.first_start >= month.replace(tzinfo=None),
                    first_starts.c.first_start < next_month.replace(tzinfo=None))) or 0
                count_out = db.scalar(select(func.count()).select_from(Subscription).where(
                    Subscription.expires_at >= month.replace(tzinfo=None),
                    Subscription.expires_at < next_month.replace(tzinfo=None))) or 0
                months.append({"label": month.strftime("%b"), "incoming": count_in, "outgoing": count_out})
            expiring_count = db.scalar(select(func.count()).select_from(Subscription).where(
                active_filter, Subscription.expires_at <= (now + timedelta(days=7)).replace(tzinfo=None))) or 0
            ctx.update(active_count=active_count, inactive_count=total_count-active_count,
                       expiring_count=expiring_count, incoming_count=current_month,
                       outgoing_count=churn, popular=[(row.plan_name, row.uses) for row in popular], months=months,
                       expiring=[{"sub": s, "days": remaining_days(s.expires_at)} for s in expiring],
                       enabled_plans=sum(1 for plan in all_plans if plan.enabled),
                       pending_count=db.scalar(select(func.count()).select_from(PendingPayment)))
        elif section == "users":
            sort_columns = {"telegram_id": Subscription.telegram_id, "plan_name": Subscription.plan_name,
                            "expires_at": Subscription.expires_at, "current_price": Subscription.current_price,
                            "enabled": Subscription.enabled}
            sort_column = sort_columns.get(sort, Subscription.telegram_id)
            ordering = sort_column.desc() if direction == "desc" else sort_column.asc()
            page_size = 100
            total = db.scalar(select(func.count()).select_from(Subscription)) or 0
            page_count = max(1, (total + page_size - 1) // page_size)
            page = min(page, page_count)
            subs = db.scalars(select(Subscription).order_by(ordering, Subscription.telegram_id)
                              .offset((page - 1) * page_size).limit(page_size)).all()
            try:
                usage_by_email = {str(client.get("email", "")): client.get("used_bytes")
                                  for client in await XUIClient().list_clients()}
            except Exception:
                logger.exception("Unable to load client traffic in one 3x-ui batch")
                usage_by_email = {}
            rows = []
            for index, sub in enumerate(subs, start=(page - 1) * page_size + 1):
                used = usage_by_email.get(str(sub.telegram_id), usage_by_email.get(f"sub-{sub.sub_id}@vpn.invalid"))
                remaining = None if sub.traffic_limit_bytes == 0 else max(0, sub.traffic_limit_bytes - used) if used is not None else None
                rows.append({"number": index, "sub": sub, "remaining_days": remaining_days(sub.expires_at),
                             "active": sub.enabled and as_utc(sub.expires_at) > datetime.now(timezone.utc),
                             "used": used, "remaining_bytes": remaining})
            ctx.update(rows=rows, page=page, page_count=page_count, total_users=total,
                       sort=sort, direction=direction)
            ctx["manual_plans"] = db.scalars(select(Plan).where(Plan.enabled.is_(True)).order_by(Plan.days)).all()
        elif section == "subscriptions":
            plans = db.scalars(select(Plan).order_by(Plan.days)).all()
            ctx["plans"] = plans
            by_plan = dict(db.execute(select(Subscription.plan_id, func.count())
                                      .where(Subscription.plan_id.is_not(None))
                                      .group_by(Subscription.plan_id)).all())
            legacy_counts = dict(db.execute(select(Subscription.plan_name, func.count())
                                            .where(Subscription.plan_id.is_(None))
                                            .group_by(Subscription.plan_name)).all())
            ctx["plan_users"] = {plan.id: by_plan.get(plan.id, 0) + legacy_counts.get(plan.name, 0)
                                 for plan in plans}
        elif section == "addons":
            ctx["packages"] = db.scalars(select(AddonPackage).order_by(AddonPackage.traffic_gb)).all()
        elif section == "history":
            page_size = 100
            total = db.scalar(select(func.count()).select_from(SubscriptionHistory)) or 0
            page_count = max(1, (total + page_size - 1) // page_size)
            page = min(page, page_count)
            ctx.update(history=db.scalars(select(SubscriptionHistory)
                                         .order_by(SubscriptionHistory.starts_at.desc(), SubscriptionHistory.id.desc())
                                         .offset((page - 1) * page_size).limit(page_size)).all(),
                       page=page, page_count=page_count, total_history=total)
        elif section == "settings":
            config = {key: get_config(key) for key in CONFIG_DEFAULTS if key not in SECRET_KEYS}
            config["selected_inbounds"] = [int(v) for v in config.get("xui_inbound_ids", "").split(",") if v.isdigit()]
            account = db.scalar(select(AdminAccount).limit(1))
            ctx.update(config=config, secret_status=config_status(),
                       admin_username=account.username if account else env_settings.admin_user)
        elif section == "botmenu":
            nodes = db.scalars(select(BotMenuNode).order_by(BotMenuNode.position, BotMenuNode.id)).all()
            by_parent = {}
            menu_labels = {node.id: node.label for node in nodes}
            for node in nodes:
                by_parent.setdefault(node.parent_id, []).append(node)
            menu_rows = []
            def walk(parent_id, depth=0):
                for node in by_parent.get(parent_id, []):
                    menu_rows.append({"node": node, "depth": depth})
                    walk(node.id, depth + 1)
            walk(None)
            ctx.update(menu_rows=menu_rows, menu_labels=menu_labels, bot_welcome_text=get_config("bot_welcome_text"),
                       offer_text=get_config("offer_text"), public_base_url=get_config("public_base_url"))
        elif section == "referrals":
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            plans = db.scalars(select(Plan).order_by(Plan.days, Plan.id)).all()
            promo_plans = db.scalars(select(Plan).where(
                Plan.enabled.is_(True), Plan.show_in_bot.is_(True)).order_by(Plan.days, Plan.id)).all()
            rewards = {item.plan_id: item.days for item in db.scalars(select(ReferralReward)).all()}
            promos = db.scalars(select(PromoCode).order_by(PromoCode.expires_at.desc())).all()
            promo_rows = []
            for promo in promos:
                expires_at = promo.expires_at.replace(tzinfo=None) if promo.expires_at.tzinfo else promo.expires_at
                seconds = max(0, int((expires_at - now).total_seconds()))
                remaining = (f"{seconds // 86400} дн. {(seconds % 86400) // 3600:02d} ч. "
                             f"{(seconds % 3600) // 60:02d} мин." if seconds else "Срок истёк")
                used = db.scalar(select(func.count()).select_from(PromoRedemption).where(
                    PromoRedemption.promo_id == promo.id)) or 0
                promo_rows.append({"promo": promo, "remaining": remaining, "expired": seconds == 0,
                                   "plans": [p.name for p in plans if p.id in [int(v) for v in promo.plan_ids.split(",") if v.isdigit()]],
                                   "used": used})
            ctx.update(referral_plans=plans, promo_plans=promo_plans,
                       referral_rewards=rewards, promo_rows=promo_rows,
                       referral_enabled=get_config("referral_enabled").lower() in {"1", "true", "yes", "on"})
        elif section == "system":
            backups = sorted(backup_directory().glob("vpnshop-*.vpbak"), key=lambda p: p.stat().st_mtime, reverse=True)
            sync_issues = db.scalars(select(Subscription).where(
                Subscription.sync_status.notin_(["synced", "unknown"])
            ).order_by(Subscription.sync_checked_at.desc()).limit(100)).all()
            payment_issues = db.scalars(select(FulfillmentJob).where(
                FulfillmentJob.status != "done"
            ).order_by(FulfillmentJob.created_at.desc()).limit(50)).all()
            ctx.update(control_enabled=bool(env_settings.control_api_token), operation=request.query_params.get("operation", ""),
                       backup_files=[{"name": p.name, "size": p.stat().st_size} for p in backups[:14]],
                       backup_key_configured=bool(env_settings.backup_encryption_key), sync_issues=sync_issues,
                       payment_issues=payment_issues)
        return templates.TemplateResponse(request, "admin_base.html", ctx | {"section_body": f"admin_{section}.html"})


def validate_menu_values(form, db, current_id: int | None = None):
    label = str(form.get("label", "")).strip()
    action = str(form.get("action", "menu")).strip()
    text_value = str(form.get("text", "")).strip()
    url = str(form.get("url", "")).strip()
    routing_rules = str(form.get("routing_rules", "")).strip()
    parent_raw = str(form.get("parent_id", "")).strip()
    if not label or len(label) > 64:
        raise HTTPException(status_code=400, detail="Название кнопки должно быть от 1 до 64 символов")
    if action not in {"menu", "plans", "change_plan", "subscription_info", "addons", "offer", "url", "open_happ", "routing", "trial", "promo", "referral"}:
        raise HTTPException(status_code=400, detail="Неизвестное действие кнопки")
    parent_id = int(parent_raw) if parent_raw.isdigit() and int(parent_raw) > 0 else None
    if parent_id is not None:
        parent = db.get(BotMenuNode, parent_id)
        if not parent or not parent.enabled or parent.action != "menu" or parent_id == current_id:
            raise HTTPException(status_code=400, detail="Выберите доступное родительское меню")
        ancestor_id = parent.parent_id
        while ancestor_id is not None:
            if ancestor_id == current_id:
                raise HTTPException(status_code=400, detail="Нельзя переместить меню внутрь самого себя")
            ancestor = db.get(BotMenuNode, ancestor_id)
            ancestor_id = ancestor.parent_id if ancestor else None
    if action == "url":
        from urllib.parse import urlsplit
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.netloc or len(url) > 500:
            raise HTTPException(status_code=400, detail="Для ссылки кнопки укажите корректный HTTPS URL")
    elif url:
        raise HTTPException(status_code=400, detail="URL используется только для действия «Открыть ссылку»")
    if action == "routing":
        if not routing_rules:
            raise HTTPException(status_code=400, detail="Укажите JSON правил или готовую ссылку happ://routing/add/…")
        try:
            prefix = "happ://routing/add/"
            if routing_rules.startswith(prefix):
                encoded = unquote(routing_rules[len(prefix):]).strip()
                decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                rules_object = json.loads(decoded.decode("utf-8"))
                normalized_rules = routing_rules
            else:
                rules_object = json.loads(routing_rules)
                normalized_rules = json.dumps(rules_object, ensure_ascii=False, separators=(",", ":"))
            if not isinstance(rules_object, dict):
                raise ValueError("Routing profile must be a JSON object")
            if len(normalized_rules.encode("utf-8")) > 16000:
                raise ValueError("Routing profile is too large")
            routing_rules = normalized_rules
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail="Укажите корректный JSON-объект или ссылку Happ с Base64 JSON") from exc
    else:
        routing_rules = ""
    position_raw = str(form.get("position", "0")).strip()
    try:
        position = max(0, min(9999, int(position_raw or 0)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Порядок должен быть целым числом")
    return {"parent_id": parent_id, "label": label, "action": action,
            "text": text_value[:4000], "url": url, "routing_rules": routing_rules,
            "position": position,
            "same_row": str(form.get("same_row", "")) == "on",
            "enabled": str(form.get("enabled", "")) == "on"}


@app.post("/admin/botmenu")
async def create_bot_menu_node(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    with SessionLocal() as db:
        node = BotMenuNode(**validate_menu_values(form, db))
        db.add(node)
        db.commit()
    return RedirectResponse("/admin/botmenu", status_code=303)


@app.post("/admin/botmenu/{node_id}/edit")
async def edit_bot_menu_node(node_id: int, request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    with SessionLocal() as db:
        node = db.get(BotMenuNode, node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Кнопка не найдена")
        values = validate_menu_values(form, db, node_id)
        has_children = db.scalars(select(BotMenuNode).where(BotMenuNode.parent_id == node_id)).first()
        if values["action"] != "menu" and has_children:
            raise HTTPException(status_code=409, detail="Сначала переместите или удалите вложенные кнопки")
        for key, value in values.items():
            setattr(node, key, value)
        db.commit()
    return RedirectResponse("/admin/botmenu", status_code=303)


@app.post("/admin/botmenu/{node_id}/delete")
async def delete_bot_menu_node(node_id: int, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    with SessionLocal() as db:
        node = db.get(BotMenuNode, node_id)
        if node:
            def remove_tree(parent_id):
                for child in db.scalars(select(BotMenuNode).where(BotMenuNode.parent_id == parent_id)).all():
                    remove_tree(child.id)
                    db.delete(child)
            remove_tree(node_id)
            db.delete(node)
            db.commit()
    return RedirectResponse("/admin/botmenu", status_code=303)


@app.post("/admin/referrals/promos")
async def create_promo_code(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    code = str(form.get("code", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
        raise HTTPException(status_code=400, detail="Код: от 3 до 40 латинских букв, цифр, дефиса или подчёркивания")
    try:
        discount = int(form.get("discount_percent", 0))
        days = int(form.get("days_valid", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Укажите корректную скидку и срок") from exc
    plan_ids = sorted({int(value) for value in form.getlist("plan_ids") if str(value).isdigit()})
    if not 1 <= discount <= 100 or not 1 <= days <= 3650 or not plan_ids:
        raise HTTPException(status_code=400, detail="Выберите тарифы и укажите скидку 1–100% и срок 1–3650 дней")
    with SessionLocal() as db:
        valid_ids = set(db.scalars(select(Plan.id).where(
            Plan.id.in_(plan_ids), Plan.enabled.is_(True), Plan.show_in_bot.is_(True))).all())
        if valid_ids != set(plan_ids):
            raise HTTPException(status_code=400, detail="В списке есть неизвестный тариф")
        if db.scalar(select(PromoCode.id).where(PromoCode.code == code)):
            raise HTTPException(status_code=409, detail="Такой промокод уже существует")
        db.add(PromoCode(code=code, discount_percent=discount,
                         plan_ids=",".join(map(str, plan_ids)),
                         expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days)))
        db.commit()
    return RedirectResponse("/admin/referrals", status_code=303)


@app.post("/admin/referrals/promos/{promo_id}/delete")
async def delete_promo_code(promo_id: int, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    with SessionLocal() as db:
        promo = db.get(PromoCode, promo_id)
        if promo:
            pending = db.scalar(select(PendingPayment.invoice_id).where(PendingPayment.promo_code_id == promo_id).limit(1))
            if pending:
                raise HTTPException(status_code=409, detail="Промокод привязан к неоплаченному счёту; повторите удаление после оплаты или истечения счёта")
            db.query(PromoSelection).filter(PromoSelection.promo_id == promo_id).delete(synchronize_session=False)
            db.query(PromoRedemption).filter(PromoRedemption.promo_id == promo_id).delete(synchronize_session=False)
            db.delete(promo)
            db.commit()
    return RedirectResponse("/admin/referrals", status_code=303)


@app.post("/admin/referrals/settings")
async def save_referral_settings(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    with SessionLocal() as db:
        plans = db.scalars(select(Plan)).all()
        for plan in plans:
            raw = str(form.get(f"days_{plan.id}", "0")).strip()
            try:
                days = int(raw or 0)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Некорректное число дней для тарифа {plan.name}") from exc
            if not 0 <= days <= 3650:
                raise HTTPException(status_code=400, detail="Число реферальных дней должно быть от 0 до 3650")
            reward = db.get(ReferralReward, plan.id)
            if days:
                if reward:
                    reward.days = days
                else:
                    db.add(ReferralReward(plan_id=plan.id, days=days))
            elif reward:
                db.delete(reward)
        db.commit()
    save_config({"referral_enabled": "true" if form.get("enabled") == "on" else "false"})
    return RedirectResponse("/admin/referrals", status_code=303)


@app.post("/admin/subscriptions")
async def create_plan(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    traffic_reset = str(form.get("traffic_reset", "never"))
    if traffic_reset not in {"never", "hourly", "daily", "weekly", "monthly"}:
        raise HTTPException(status_code=400, detail="Некорректный период сброса трафика")
    with SessionLocal() as db:
        db.add(Plan(name=str(form["name"]).strip(), days=int(form["days"]), amount=int(form["amount"]),
                    currency=str(form.get("currency", "RUB")).strip().upper(),
                    traffic_limit_gb=float(form.get("traffic_limit_gb", 0)),
                    limit_hwid=max(0, int(form.get("limit_hwid", 0))), traffic_reset=traffic_reset,
                    show_in_bot=form.get("show_in_bot") == "on"))
        db.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)


@app.post("/admin/addons")
async def create_addon_package(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    traffic_gb = float(form.get("traffic_gb", 0))
    amount = int(form.get("amount", 0))
    if traffic_gb <= 0 or amount <= 0:
        raise HTTPException(status_code=400, detail="Укажите положительные значения трафика и цены")
    with SessionLocal() as db:
        db.add(AddonPackage(name=str(form.get("name", "")).strip(), traffic_gb=traffic_gb, amount=amount,
                            currency=str(form.get("currency", "RUB")).strip().upper(), enabled=True))
        db.commit()
    return RedirectResponse("/admin/addons", status_code=303)


@app.post("/admin/addons/{package_id}/edit")
async def edit_addon_package(package_id: int, request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    with SessionLocal() as db:
        package = db.get(AddonPackage, package_id)
        if not package:
            raise HTTPException(status_code=404)
        package.name = str(form.get("name", "")).strip()
        package.traffic_gb = float(form.get("traffic_gb", 0))
        package.amount = int(form.get("amount", 0))
        package.currency = str(form.get("currency", "RUB")).strip().upper()
        package.enabled = form.get("enabled") == "on"
        if not package.name or package.traffic_gb <= 0 or package.amount <= 0:
            raise HTTPException(status_code=400, detail="Укажите название, объём трафика и цену")
        db.commit()
    return RedirectResponse("/admin/addons", status_code=303)


@app.post("/admin/addons/{package_id}/delete")
async def delete_addon_package(package_id: int, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    with SessionLocal() as db:
        package = db.get(AddonPackage, package_id)
        if package:
            pending = db.scalars(select(PendingPayment).where(PendingPayment.package_id == package_id)).first()
            if pending:
                raise HTTPException(status_code=409, detail="Нельзя удалить пакет с ожидающими платежами")
            db.delete(package)
            db.commit()
    return RedirectResponse("/admin/addons", status_code=303)


@app.get("/admin/api/xui/inbounds")
async def list_xui_inbounds(_: None = Depends(admin)):
    try:
        return {"items": await XUIClient().list_inbounds()}
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Не удалось получить список inbound из 3x-ui. Проверьте доступ, токен и путь API.") from exc


@app.get("/admin/api/secrets/{key}")
async def reveal_secret(key: str, _: None = Depends(admin)):
    if key not in SECRET_KEYS:
        raise HTTPException(status_code=404)
    return {"value": get_config(key)}


@app.get("/admin/api/system/logs")
async def system_logs(_: None = Depends(admin)):
    response = await call_control_api("GET", "/logs?lines=500")
    return {"logs": response.text}


@app.get("/admin/api/system/status")
async def system_status(_: None = Depends(admin)):
    response = await call_control_api("GET", "/status")
    return response.json()


@app.post("/admin/system/restart")
async def system_restart(request: Request, _: None = Depends(admin)):
    await checked_form(request)
    await call_control_api("POST", "/restart", timeout=90)
    return RedirectResponse("/admin/system?operation=restarting", status_code=303)


@app.post("/admin/system/update")
async def system_update(request: Request, _: None = Depends(admin)):
    await checked_form(request)
    try:
        await asyncio.to_thread(create_backup)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Обновление остановлено: резервная копия не создана: {exc}") from exc
    await call_control_api("POST", "/update", timeout=900)
    return RedirectResponse("/admin/system?operation=updating", status_code=303)


@app.post("/admin/system/backup")
async def system_backup(request: Request, _: None = Depends(admin)):
    await checked_form(request)
    try:
        path = await asyncio.to_thread(create_backup)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Резервная копия не создана: {exc}") from exc
    return RedirectResponse("/admin/system?backup=created", status_code=303)


@app.post("/admin/system/backup/{filename}/delete")
async def delete_backup(filename: str, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    if not re.fullmatch(r"vpnshop-[0-9]{8}-[0-9]{6}(?:-[0-9]{6})?\.vpbak", filename):
        raise HTTPException(status_code=404)
    root = backup_directory().resolve()
    path = (root / filename).resolve()
    if path.parent != root or not path.is_file():
        raise HTTPException(status_code=404)
    try:
        await asyncio.to_thread(path.unlink)
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Не удалось удалить резервную копию") from exc
    return RedirectResponse("/admin/system?backup=deleted", status_code=303)


@app.post("/admin/system/reconcile")
async def system_reconcile(request: Request, _: None = Depends(admin)):
    await checked_form(request)
    await reconcile_subscriptions()
    return RedirectResponse("/admin/system?reconciled=1", status_code=303)


@app.post("/admin/system/payments/{invoice_id}/retry")
async def retry_payment_job(invoice_id: str, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    with SessionLocal() as db:
        job = db.get(FulfillmentJob, invoice_id)
        if not job or job.status != "retry":
            raise HTTPException(status_code=404)
        job.status = "queued"
        job.next_attempt_at = datetime.now(timezone.utc).replace(tzinfo=None)
        job.last_error = ""
        db.commit()
    return RedirectResponse("/admin/system?payments=retry", status_code=303)


@app.get("/admin/system/backup/{filename}")
async def download_backup(filename: str, _: None = Depends(admin)):
    from fastapi.responses import FileResponse
    if not re.fullmatch(r"vpnshop-[0-9]{8}-[0-9]{6}(?:-[0-9]{6})?\.vpbak", filename):
        raise HTTPException(status_code=404)
    path = backup_directory() / filename
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@app.post("/admin/botmenu/content")
async def update_bot_content(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    save_config({"bot_welcome_text": str(form.get("bot_welcome_text", "")).strip(),
                 "offer_text": str(form.get("offer_text", "")).strip()})
    return RedirectResponse("/admin/botmenu", status_code=303)


@app.post("/admin/subscriptions/{plan_id}/edit")
async def edit_plan(plan_id: int, request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    traffic_reset = str(form.get("traffic_reset", "never"))
    if traffic_reset not in {"never", "hourly", "daily", "weekly", "monthly"}:
        raise HTTPException(status_code=400, detail="Некорректный период сброса трафика")
    renamed_subscribers = []
    with SessionLocal() as db:
        plan = db.get(Plan, plan_id)
        if not plan:
            raise HTTPException(status_code=404)
        new_name = str(form["name"]).strip()
        renamed = new_name != plan.name
        plan.name = new_name
        plan.days = int(form["days"])
        plan.amount = int(form["amount"])
        plan.currency = str(form.get("currency", "RUB")).strip().upper()
        plan.traffic_limit_gb = float(form.get("traffic_limit_gb", 0))
        plan.enabled = form.get("enabled") == "on"
        plan.show_in_bot = form.get("show_in_bot") == "on"
        plan.limit_hwid = max(0, int(form.get("limit_hwid", 0)))
        plan.traffic_reset = traffic_reset
        if renamed:
            renamed_subscribers = db.scalars(select(Subscription).where(
                Subscription.plan_id == plan_id)).all()
            for sub in renamed_subscribers:
                sub.plan_name = new_name
        db.commit()
    sync_failed = []
    for sub in renamed_subscribers:
        try:
            await XUIClient().add_or_update_client(
                sub.telegram_id, sub.sub_id, int(as_utc(sub.expires_at).timestamp() * 1000),
                sub.traffic_limit_bytes, exists=True, limit_hwid=sub.limit_hwid,
                traffic_reset=sub.traffic_reset,
                inbound_ids=[int(value) for value in sub.inbound_ids.split(",") if value.isdigit()] or None,
                enabled=sub.enabled, group_name=new_name)
        except Exception:
            logger.exception("Could not update 3x-ui group for Telegram ID %s after renaming plan %s",
                             sub.telegram_id, plan_id)
            sync_failed.append(sub.telegram_id)
    if sync_failed:
        with SessionLocal() as db:
            for telegram_id in sync_failed:
                sub = db.get(Subscription, telegram_id)
                if sub:
                    sub.sync_status = "plan_name_sync_failed"
            db.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)


@app.post("/admin/subscriptions/{plan_id}/delete")
async def delete_plan(plan_id: int, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    with SessionLocal() as db:
        plan = db.get(Plan, plan_id)
        if not plan:
            raise HTTPException(status_code=404, detail="Тариф не найден")
        users = db.scalars(select(Subscription).where(
            (Subscription.plan_id == plan.id) |
            ((Subscription.plan_id.is_(None)) & (Subscription.plan_name == plan.name))
        )).first()
        if users:
            raise HTTPException(status_code=409, detail="Нельзя удалить тариф, пока к нему привязаны пользователи")
        if db.scalar(select(PendingPayment.invoice_id).where(PendingPayment.plan_id == plan_id).limit(1)):
            raise HTTPException(status_code=409, detail="Нельзя удалить тариф, пока по нему ожидается оплата")
        reward = db.get(ReferralReward, plan_id)
        if reward:
            db.delete(reward)
        for promo in db.scalars(select(PromoCode)).all():
            selected = [int(value) for value in promo.plan_ids.split(",") if value.isdigit() and int(value) != plan_id]
            if len(selected) != len([value for value in promo.plan_ids.split(",") if value.isdigit()]):
                promo.plan_ids = ",".join(map(str, selected))
                if not selected:
                    promo.expires_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.delete(plan)
        db.commit()
    return RedirectResponse("/admin/subscriptions", status_code=303)


@app.post("/admin/users")
async def create_user(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    telegram_id = int(form["telegram_id"])
    now = datetime.now(timezone.utc)
    sub_id = uuid.uuid4().hex[:20]
    plan_id = int(form.get("plan_id", 0))
    with SessionLocal() as db:
        if db.get(Subscription, telegram_id):
            raise HTTPException(status_code=409, detail="Telegram ID already exists")
        plan = db.get(Plan, plan_id)
        if not plan or not plan.enabled:
            raise HTTPException(status_code=400, detail="Выберите действующий тариф")
        days, title, price, currency = plan.days, plan.name, plan.amount, plan.currency
        traffic_bytes = int(plan.traffic_limit_gb * (1024 ** 3))
        expires = now + timedelta(days=days)
        inbounds = XUIClient._inbound_ids(get_config_map())
        await XUIClient().add_or_update_client(telegram_id, sub_id, int(expires.timestamp() * 1000), traffic_bytes,
                                               exists=False, limit_hwid=plan.limit_hwid,
                                               traffic_reset=plan.traffic_reset, inbound_ids=inbounds,
                                               group_name=plan.name)
        db.add(Subscription(telegram_id=telegram_id, sub_id=sub_id, expires_at=expires, plan_id=plan.id, plan_name=title,
                            current_price=price, currency=currency, traffic_limit_bytes=traffic_bytes, enabled=True,
                            limit_hwid=plan.limit_hwid, traffic_reset=plan.traffic_reset,
                            inbound_ids=",".join(str(value) for value in inbounds)))
        db.add(SubscriptionHistory(telegram_id=telegram_id, plan_name=title, plan_days=days, price=price,
                                   currency=currency, traffic_limit_bytes=traffic_bytes,
                                   starts_at=now.replace(tzinfo=None), expires_at=expires.replace(tzinfo=None)))
        db.commit()
    try:
        await notify_user(telegram_id, f"VPN-доступ подключён вручную. Срок действия до {expires:%d.%m.%Y}.")
    except Exception:
        pass
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{telegram_id}/edit")
async def edit_user(telegram_id: int, request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    with SessionLocal() as db:
        sub = db.get(Subscription, telegram_id)
        if not sub:
            raise HTTPException(status_code=404)
        days = int(form["remaining_days"])
        expires = datetime.now(timezone.utc) + timedelta(days=days)
        sub.expires_at = expires
        sub.enabled = form.get("enabled") == "on"
        sub.traffic_limit_bytes = int(float(form.get("traffic_limit_gb", 0)) * (1024 ** 3))
        addon_balance = db.get(AddonBalance, telegram_id)
        if addon_balance:
            db.delete(addon_balance)
        sub.current_price = int(form.get("price", 0))
        await XUIClient().add_or_update_client(telegram_id, sub.sub_id, int(expires.timestamp() * 1000),
                                               sub.traffic_limit_bytes, exists=True, limit_hwid=sub.limit_hwid,
                                               traffic_reset=sub.traffic_reset,
                                               inbound_ids=[int(v) for v in sub.inbound_ids.split(",") if v.isdigit()] or None,
                                               enabled=sub.enabled, group_name=sub.plan_name)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


async def remove_subscription(db, sub: Subscription):
    await XUIClient().delete_client(sub.sub_id, sub.telegram_id)
    if not db.get(TrialClaim, sub.telegram_id):
        db.add(TrialClaim(telegram_id=sub.telegram_id))
    db.query(SubscriptionHistory).filter(
        SubscriptionHistory.telegram_id == sub.telegram_id).delete(synchronize_session=False)
    addon_balance = db.get(AddonBalance, sub.telegram_id)
    if addon_balance:
        db.delete(addon_balance)
    db.delete(sub)


@app.post("/admin/users/{telegram_id}/delete")
async def delete_user(telegram_id: int, request: Request, _: None = Depends(admin)):
    await checked_form(request)
    with SessionLocal() as db:
        sub = db.get(Subscription, telegram_id)
        if sub:
            await remove_subscription(db, sub)
            db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/delete-inactive")
async def delete_inactive_users(request: Request, _: None = Depends(admin)):
    await checked_form(request)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        subs = db.scalars(select(Subscription)).all()
        inactive = [s for s in subs if not s.enabled or as_utc(s.expires_at) <= now]
        for sub in inactive:
            await remove_subscription(db, sub)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/settings")
async def update_settings(request: Request, _: None = Depends(admin)):
    form = await checked_form(request)
    allowed_plain = set(CONFIG_DEFAULTS) - SECRET_KEYS
    values = {key: str(form.get(key, "")).strip() for key in allowed_plain if key in form}
    scheme = str(form.get("panel_scheme", "https")).strip().lower()
    domain = str(form.get("panel_domain", "")).strip()
    try:
        panel_port = int(str(form.get("panel_port", "")).strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Порт панели должен быть числом от 1 до 65535") from exc
    uri_path = str(form.get("panel_uri_path", "admin")).strip().strip("/")
    if scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Протокол панели должен быть HTTP или HTTPS")
    if not domain or any(char in domain for char in "/\\?#:@ "):
        raise HTTPException(status_code=400, detail="Укажите домен или IP без протокола и пути")
    if not 1 <= panel_port <= 65535:
        raise HTTPException(status_code=400, detail="Порт панели должен быть от 1 до 65535")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", uri_path):
        raise HTTPException(status_code=400, detail="URI-путь: только латинские буквы, цифры, дефис, подчёркивание и /")
    origin_port = "" if (scheme, panel_port) in {("http", 80), ("https", 443)} else f":{panel_port}"
    values.update(panel_scheme=scheme, panel_domain=domain, panel_port=str(panel_port), panel_uri_path=uri_path,
                  public_base_url=f"{scheme}://{domain}{origin_port}")
    old_global_inbounds = XUIClient._inbound_ids(get_config_map())
    inbound_ids = sorted({int(value) for value in str(form.get("xui_inbound_ids", "")).split(",")
                          if value.strip().isdigit() and int(value) > 0})
    if not inbound_ids:
        raise HTTPException(status_code=400, detail="Выберите хотя бы один inbound / сервер")
    values["xui_inbound_ids"] = ",".join(str(value) for value in inbound_ids)
    values["xui_inbound_id"] = str(inbound_ids[0])
    for key in SECRET_KEYS:
        submitted = str(form.get(key, ""))
        if submitted:
            values[key] = submitted
    labels = {key: str(form.get(field, "")).strip() for key, field in SECRET_LABELS.items()}
    save_config(values, labels)
    if set(old_global_inbounds) != set(inbound_ids):
        changed = 0
        try:
            with SessionLocal() as db:
                subs = db.scalars(select(Subscription)).all()
                for sub in subs:
                    current_ids = [int(value) for value in sub.inbound_ids.split(",") if value.strip().isdigit()]
                    if not current_ids:
                        current_ids = old_global_inbounds
                    await XUIClient().set_client_inbounds(sub.telegram_id, sub.sub_id, current_ids, inbound_ids)
                    sub.inbound_ids = ",".join(str(value) for value in inbound_ids)
                    db.commit()
                    changed += 1
        except Exception as exc:
            raise HTTPException(status_code=502, detail=(
                f"Настройки сохранены, но обновление inbound остановилось после {changed} клиентов. "
                f"Исправьте подключение к 3x-ui и сохраните эти же inbound ещё раз. Ошибка: {exc}"
            )) from exc
    new_username = str(form.get("admin_username", "")).strip()
    new_password = str(form.get("admin_password", ""))
    if new_username:
        save_admin_account(new_username, new_password)
    await restart_bot_runtime()
    return RedirectResponse("/admin/settings", status_code=303)
