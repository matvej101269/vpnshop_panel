import asyncio
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections import Counter
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings as env_settings
from app.db import (init_db, SessionLocal, Plan, AddonPackage, BotMenuNode, PendingPayment, Subscription,
                    SubscriptionHistory, AdminAccount)
from app.services import provision_paid_invoice, XUIClient
from app.bot import start_bot, notify_user
from app.runtime_config import (init_runtime_config, get_config, save_config, config_status,
                                verify_admin, save_admin_account, CONFIG_DEFAULTS, SECRET_KEYS,
                                make_csrf_token, verify_csrf_token, get_config_map)

templates = Jinja2Templates(directory="app/templates")
security = HTTPBasic()
scheduler = AsyncIOScheduler(timezone=env_settings.timezone)
bot_task: asyncio.Task | None = None
SECTIONS = {"overview", "users", "subscriptions", "addons", "history", "settings", "botmenu"}
SECRET_LABELS = {
    "bot_token": "name_bot_token", "lava_api_key": "name_lava_api_key", "lava_offer_id": "name_lava_offer_id",
    "lava_webhook_key": "name_lava_webhook_key", "xui_password": "name_xui_password",
    "xui_api_token": "name_xui_api_token", "xui_username": "name_xui_username",
}


def admin(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    if not verify_admin(credentials.username, credentials.password):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
    request.state.admin_username = credentials.username


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
                messages.append((sub.telegram_id, left))
                sub.reminded = (sub.reminded + "," if sub.reminded else "") + str(left)
        db.commit()
    for tg_id, left in messages:
        try:
            await notify_user(tg_id, f"Срок VPN-подписки заканчивается через {left} дн. Продлите её в боте командой /start.")
        except Exception:
            continue


async def purge_old_pending_payments():
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    with SessionLocal() as db:
        db.query(PendingPayment).filter(PendingPayment.created_at < cutoff).delete(synchronize_session=False)
        db.commit()


async def restart_bot_runtime():
    global bot_task
    if bot_task and not bot_task.done():
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
    token = get_config("bot_token")
    bot_task = asyncio.create_task(start_bot(token)) if token else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_runtime_config()
    scheduler.add_job(send_reminders, "interval", hours=6, id="reminders", replace_existing=True)
    scheduler.add_job(purge_old_pending_payments, "interval", hours=6, id="pending-retention", replace_existing=True)
    scheduler.start()
    await restart_bot_runtime()
    yield
    scheduler.shutdown(wait=False)
    if bot_task:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


app = FastAPI(title="VPN Shop", lifespan=lifespan)


@app.middleware("http")
async def disable_admin_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}


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
        provision = await provision_paid_invoice(invoice_id)
        if provision == "duplicate":
            return {"ok": True, "duplicate": True}
        if not provision:
            raise HTTPException(status_code=503, detail="Payment mapping is not ready")
        telegram_id, link = provision
        if link:
            message = f"Оплата подтверждена! Ваша подписка для Happ:\n\n{link}\n\nДобавьте ссылку в Happ через «Добавить по ссылке»."
        else:
            message = "Оплата подтверждена! Лимит дополнительного трафика добавлен к вашей подписке."
        await notify_user(telegram_id, message)
    return {"ok": True}


@app.get("/admin")
async def admin_root(_: None = Depends(admin)):
    return RedirectResponse("/admin/overview", status_code=303)


@app.get("/admin/{section}", response_class=HTMLResponse)
async def admin_page(section: str, request: Request, _: None = Depends(admin)):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    ctx = {"request": request, "section": section,
           "csrf_token": make_csrf_token(getattr(request.state, "admin_username", "admin"))}
    with SessionLocal() as db:
        if section == "overview":
            now = datetime.now(timezone.utc)
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
            current_month_end = (now.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            subs = db.scalars(select(Subscription)).all()
            all_plans = db.scalars(select(Plan)).all()
            active = [s for s in subs if s.enabled and as_utc(s.expires_at) > now]
            inactive = [s for s in subs if not s.enabled or as_utc(s.expires_at) <= now]
            expiring = [s for s in active if as_utc(s.expires_at) <= now + timedelta(days=7)]
            history = db.scalars(select(SubscriptionHistory)).all()
            first_start_by_user = {}
            for item in history:
                first_start_by_user[item.telegram_id] = min(first_start_by_user.get(item.telegram_id, item.starts_at), item.starts_at)
            new_users_by_month = {tg_id: starts for tg_id, starts in first_start_by_user.items()}
            current_month = [starts for starts in new_users_by_month.values() if starts >= month_start]
            churn = sum(1 for s in subs if month_start <= as_utc(s.expires_at).replace(tzinfo=None) < current_month_end)
            plans = Counter(h.plan_name for h in history)
            popular = sorted(plans.items(), key=lambda item: (-item[1], item[0]))[:7]
            months = []
            for back in range(5, -1, -1):
                index = now.year * 12 + now.month - 1 - back
                year, month_no = divmod(index, 12)
                month = datetime(year, month_no + 1, 1, tzinfo=timezone.utc)
                next_index = index + 1
                next_year, next_month_no = divmod(next_index, 12)
                next_month = datetime(next_year, next_month_no + 1, 1, tzinfo=timezone.utc)
                count_in = sum(1 for started in first_start_by_user.values() if month.replace(tzinfo=None) <= started < next_month.replace(tzinfo=None))
                count_out = sum(1 for s in subs if month.replace(tzinfo=None) <= as_utc(s.expires_at).replace(tzinfo=None) < next_month.replace(tzinfo=None))
                months.append({"label": month.strftime("%b"), "incoming": count_in, "outgoing": count_out})
            ctx.update(active_count=len(active), inactive_count=len(inactive), expiring_count=len(expiring),
                       incoming_count=len(current_month), outgoing_count=churn, popular=popular, months=months,
                       expiring=[{"sub": s, "days": remaining_days(s.expires_at)} for s in sorted(expiring, key=lambda s: s.expires_at)[:8]],
                       enabled_plans=sum(1 for plan in all_plans if plan.enabled),
                       pending_count=db.scalar(select(func.count()).select_from(PendingPayment)))
        elif section == "users":
            subs = db.scalars(select(Subscription).order_by(Subscription.telegram_id)).all()
            rows = []
            for index, sub in enumerate(subs, start=1):
                try:
                    used = await XUIClient().client_usage(sub.sub_id, sub.telegram_id)
                except Exception:
                    used = None
                remaining = None if sub.traffic_limit_bytes == 0 else max(0, sub.traffic_limit_bytes - used) if used is not None else None
                rows.append({"number": index, "sub": sub, "remaining_days": remaining_days(sub.expires_at),
                             "active": sub.enabled and as_utc(sub.expires_at) > datetime.now(timezone.utc),
                             "used": used, "remaining_bytes": remaining})
            ctx["rows"] = rows
            ctx["manual_plans"] = db.scalars(select(Plan).where(Plan.enabled.is_(True)).order_by(Plan.days)).all()
        elif section == "subscriptions":
            plans = db.scalars(select(Plan).order_by(Plan.days)).all()
            subscriptions = db.scalars(select(Subscription)).all()
            ctx["plans"] = plans
            ctx["plan_users"] = {
                plan.id: sum(1 for sub in subscriptions
                             if sub.plan_id == plan.id or (sub.plan_id is None and sub.plan_name == plan.name))
                for plan in plans
            }
        elif section == "addons":
            ctx["packages"] = db.scalars(select(AddonPackage).order_by(AddonPackage.traffic_gb)).all()
        elif section == "history":
            ctx["history"] = db.scalars(select(SubscriptionHistory).order_by(SubscriptionHistory.starts_at.desc())).all()
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
        return templates.TemplateResponse(request, "admin_base.html", ctx | {"section_body": f"admin_{section}.html"})


def validate_menu_values(form, db, current_id: int | None = None):
    label = str(form.get("label", "")).strip()
    action = str(form.get("action", "menu")).strip()
    text_value = str(form.get("text", "")).strip()
    url = str(form.get("url", "")).strip()
    parent_raw = str(form.get("parent_id", "")).strip()
    if not label or len(label) > 64:
        raise HTTPException(status_code=400, detail="Название кнопки должно быть от 1 до 64 символов")
    if action not in {"menu", "plans", "change_plan", "subscription_info", "addons", "offer", "url"}:
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
    position_raw = str(form.get("position", "0")).strip()
    try:
        position = max(0, min(9999, int(position_raw or 0)))
    except ValueError:
        raise HTTPException(status_code=400, detail="Порядок должен быть целым числом")
    return {"parent_id": parent_id, "label": label, "action": action,
            "text": text_value[:4000], "url": url, "position": position,
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
    with SessionLocal() as db:
        plan = db.get(Plan, plan_id)
        if not plan:
            raise HTTPException(status_code=404)
        plan.name = str(form["name"]).strip()
        plan.days = int(form["days"])
        plan.amount = int(form["amount"])
        plan.currency = str(form.get("currency", "RUB")).strip().upper()
        plan.traffic_limit_gb = float(form.get("traffic_limit_gb", 0))
        plan.enabled = form.get("enabled") == "on"
        plan.show_in_bot = form.get("show_in_bot") == "on"
        plan.limit_hwid = max(0, int(form.get("limit_hwid", 0)))
        plan.traffic_reset = traffic_reset
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
                                               traffic_reset=plan.traffic_reset, inbound_ids=inbounds)
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
        sub.current_price = int(form.get("price", 0))
        await XUIClient().add_or_update_client(telegram_id, sub.sub_id, int(expires.timestamp() * 1000),
                                               sub.traffic_limit_bytes, exists=True, limit_hwid=sub.limit_hwid,
                                               traffic_reset=sub.traffic_reset,
                                               inbound_ids=[int(v) for v in sub.inbound_ids.split(",") if v.isdigit()] or None,
                                               enabled=sub.enabled)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


async def remove_subscription(db, sub: Subscription):
    await XUIClient().delete_client(sub.sub_id, sub.telegram_id)
    db.query(SubscriptionHistory).filter(SubscriptionHistory.telegram_id == sub.telegram_id).delete(synchronize_session=False)
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
    new_username = str(form.get("admin_username", "")).strip()
    new_password = str(form.get("admin_password", ""))
    if new_username:
        save_admin_account(new_username, new_password)
    await restart_bot_runtime()
    return RedirectResponse("/admin/settings", status_code=303)
