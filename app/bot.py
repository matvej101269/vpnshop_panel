import asyncio
import base64
import ipaddress
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, Update
from aiogram.methods import GetUpdates
from aiogram.exceptions import (TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError,
                                TelegramConflictError, TelegramBadRequest)
from aiogram.utils.backoff import Backoff, BackoffConfig
from urllib.parse import quote, urlsplit
from sqlalchemy import select
from app.db import SessionLocal, Plan, AddonPackage, PendingPayment, BotMenuNode, Subscription
from app.services import LavaClient, XUIClient, happ_link, quote_immediate_switch, provision_paid_invoice
from app.runtime_config import get_config, encrypt_handoff

logger = logging.getLogger(__name__)


class SinglePollDispatcher(Dispatcher):
    @classmethod
    async def _listen_updates(cls, bot: Bot, polling_timeout: int = 30,
                              backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
                              allowed_updates: Optional[list[str]] = None) -> AsyncGenerator[Update, None]:
        backoff = Backoff(config=backoff_config)
        get_updates = GetUpdates(timeout=polling_timeout, allowed_updates=allowed_updates)
        kwargs = {}
        if bot.session.timeout:
            kwargs["request_timeout"] = int(bot.session.timeout + polling_timeout)
        failed = False
        while True:
            try:
                updates = await bot(get_updates, **kwargs)
            except TelegramConflictError:
                logger.error("Telegram polling stopped: another process is already using this bot token")
                return
            except TelegramUnauthorizedError:
                logger.error("Telegram rejected the bot token; update it in admin settings")
                return
            except (TelegramNetworkError, TelegramServerError) as exc:
                failed = True
                logger.warning("Telegram connection interrupted; retrying with backoff: %s: %s", type(exc).__name__, exc)
                await backoff.asleep()
                continue
            except Exception as exc:
                failed = True
                logger.exception("Unexpected Telegram polling error; retrying with backoff: %s", type(exc).__name__)
                await backoff.asleep()
                continue
            if failed:
                backoff.reset()
                failed = False
            for update in updates:
                yield update
                get_updates.offset = update.update_id + 1


dp = SinglePollDispatcher()


def is_public_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified)


def routing_deep_link(rules: str) -> str:
    prefix = "happ://routing/onadd/"
    if rules.startswith("happ://routing/add/"):
        return rules.replace("happ://routing/add/", prefix, 1)
    if rules.startswith(prefix):
        return rules
    compact = json.dumps(json.loads(rules), ensure_ascii=False, separators=(",", ":"))
    encoded = base64.b64encode(compact.encode("utf-8")).decode("ascii")
    return prefix + quote(encoded, safe="")


def happ_bridge_url(deep_link: str) -> str:
    """Wrap a Happ custom-scheme link in an HTTPS URL accepted by Telegram."""
    base = get_config("public_base_url").rstrip("/")
    if urlsplit(base).scheme != "https" or not is_public_http_url(base):
        return ""
    token = encrypt_handoff(deep_link)
    return f"{base}/happ/open/{token}"


def menu_keyboard(db, parent_id: int | None, include_back: bool = True, telegram_id: int | None = None):
    nodes = db.scalars(select(BotMenuNode).where(
        BotMenuNode.parent_id == parent_id, BotMenuNode.enabled.is_(True)
    ).order_by(BotMenuNode.position, BotMenuNode.id)).all()
    rows = []
    for node in nodes:
        if node.action == "url":
            rows.append([InlineKeyboardButton(text=node.label, url=node.url)])
        elif node.action == "routing" and node.routing_rules:
            # Route actions need a callback first: it associates the profile with
            # the user's active subscription before generating its Happ URL.
            rows.append([InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}")])
        elif node.action == "open_happ" and telegram_id is not None:
            sub = db.get(Subscription, telegram_id)
            expiry = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
            link = happ_link(sub.sub_id) if sub and sub.enabled and expiry and expiry > datetime.now(timezone.utc) else ""
            deep_link = happ_bridge_url(f"happ://add/{link}") if link.startswith(("http://", "https://")) else ""
            if deep_link and len(deep_link) <= 4096:
                rows.append([InlineKeyboardButton(text=node.label, url=deep_link)])
            else:
                rows.append([InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}")])
        else:
            rows.append([InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}")])
    if include_back and parent_id is not None:
        parent = db.get(BotMenuNode, parent_id)
        back_parent = parent.parent_id if parent else None
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"menu:{back_parent or 0}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def show_menu(target, parent_id: int | None):
    telegram_id = target.from_user.id
    with SessionLocal() as db:
        if parent_id is None:
            text = get_config("bot_welcome_text")
            keyboard = menu_keyboard(db, None, include_back=False, telegram_id=telegram_id)
        else:
            node = db.get(BotMenuNode, parent_id)
            if not node or not node.enabled:
                await target.message.answer("Это меню больше недоступно.")
                return
            text = node.text or node.label
            if node.action in {"plans", "change_plan"}:
                plans = db.scalars(select(Plan).where(Plan.enabled.is_(True), Plan.show_in_bot.is_(True))).all()
                action = "switch" if node.action == "change_plan" else "buy"
                rows = [[InlineKeyboardButton(text=f"{p.name} — {p.amount} {p.currency}", callback_data=f"{action}:{p.id}")]
                        for p in plans]
                rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"menu:{node.parent_id or 0}")])
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
                text = node.text or "Выберите период VPN-подписки:"
            elif node.action == "subscription_info":
                sub = db.get(Subscription, target.from_user.id)
                if not sub:
                    text = "У вас пока нет активной подписки."
                    keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
                else:
                    try:
                        used = await XUIClient().client_usage(sub.sub_id, sub.telegram_id)
                    except Exception:
                        used = None
                    until = sub.expires_at.strftime("%d.%m.%Y %H:%M UTC")
                    usage = (f"Использовано {used / (1024 ** 3):.2f} ГБ из {sub.traffic_limit_bytes / (1024 ** 3):.2f} ГБ"
                             if used is not None and sub.traffic_limit_bytes else
                             f"Использовано {used / (1024 ** 3):.2f} ГБ · без ограничений" if used is not None else
                             "Не удалось получить данные из 3x-ui")
                    text = f"Подписка: {sub.plan_name or 'VPN'}\nДействует до: {until}\nТрафик: {usage}"
                    keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
            elif node.action == "addons":
                sub = db.get(Subscription, target.from_user.id)
                expires = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
                packages = db.scalars(select(AddonPackage).where(AddonPackage.enabled.is_(True))).all() if sub and sub.enabled and sub.traffic_limit_bytes and expires and expires > datetime.now(timezone.utc) else []
                rows = [[InlineKeyboardButton(text=f"{pkg.name} · {pkg.traffic_gb:g} ГБ — {pkg.amount} {pkg.currency}",
                                              callback_data=f"addon:{pkg.id}")] for pkg in packages]
                back_keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
                rows.extend(back_keyboard.inline_keyboard if back_keyboard else [])
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
                text = node.text or ("Выберите разовый пакет трафика. Он расходуется после квоты тарифа, а не увеличивает её навсегда." if packages else
                                     "Пакеты доступны только для подписок с ограниченным трафиком.")
            elif node.action == "offer":
                text = node.text or "Ознакомьтесь с текстом оферты на странице по кнопке ниже."
                rows = []
                base = get_config("public_base_url").rstrip("/")
                if is_public_http_url(base):
                    rows.append([InlineKeyboardButton(text="Открыть оферту", url=f"{base}/offer")])
                back_keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
                rows.extend(back_keyboard.inline_keyboard if back_keyboard else [])
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
            elif node.action == "open_happ":
                sub = db.get(Subscription, telegram_id)
                expires = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
                link = happ_link(sub.sub_id) if sub and sub.enabled and expires and expires > datetime.now(timezone.utc) else ""
                if link.startswith(("http://", "https://")):
                    deep_link = happ_bridge_url(f"happ://add/{link}")
                    rows = [[InlineKeyboardButton(text="Открыть подписку в Happ", url=deep_link)]] if deep_link and len(deep_link) <= 4096 else []
                    back_keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
                    rows.extend(back_keyboard.inline_keyboard if back_keyboard else [])
                    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
                    text = node.text or ("Нажмите кнопку, чтобы открыть Happ и добавить подписку." if rows else
                                         "Для открытия Happ настройте публичный HTTPS-адрес панели.")
                else:
                    text = node.text or "Активная подписка не найдена или не настроен URL подписки Happ."
                    keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
            elif node.action == "routing":
                back_keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
                sub = db.get(Subscription, telegram_id)
                expires = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
                active = bool(sub and sub.enabled and expires and expires > datetime.now(timezone.utc))
                if active and node.routing_rules:
                    # Ask the HTTPS handoff page to stage a one-time routing
                    # profile, then reopen the exact same subscription URL.
                    handoff = json.dumps({"action": "apply_routing", "sub_id": sub.sub_id,
                                          "rules": node.routing_rules}, ensure_ascii=False)
                    route_link = happ_bridge_url(handoff)
                else:
                    route_link = ""
                rows = [[InlineKeyboardButton(text="Применить правила к подписке в Happ", url=route_link)]] if route_link and len(route_link) <= 4096 else []
                rows.extend(back_keyboard.inline_keyboard if back_keyboard else [])
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
                text = node.text or ("Профиль будет привязан к вашей подписке. Нажмите кнопку, чтобы обновить её в Happ." if route_link and len(route_link) <= 4096 else
                                     "Для применения маршрутов нужна активная подписка и настроенный публичный HTTPS-адрес панели.")
            else:
                keyboard = menu_keyboard(db, node.id, telegram_id=telegram_id)
        if parent_id is None and not keyboard:
            plans = db.scalars(select(Plan).where(Plan.enabled.is_(True), Plan.show_in_bot.is_(True))).all()
            rows = [[InlineKeyboardButton(text=f"{p.name} — {p.amount} {p.currency}", callback_data=f"buy:{p.id}")]
                    for p in plans]
            base = get_config("public_base_url").rstrip("/")
            if is_public_http_url(base):
                rows.append([InlineKeyboardButton(text="Договор оферты", url=f"{base}/offer")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    if isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard)
    else:
        try:
            await target.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                logger.warning("Cannot update Telegram menu message: %s", exc)


@dp.message(CommandStart())
async def start(message: Message):
    await show_menu(message, None)


@dp.callback_query(F.data.startswith("menu:"))
async def open_menu(callback: CallbackQuery):
    raw_id = callback.data.split(":", 1)[1]
    parent_id = int(raw_id) if raw_id.isdigit() and int(raw_id) else None
    await show_menu(callback, parent_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery):
    await create_plan_order(callback, immediate_switch=False)


@dp.callback_query(F.data.startswith("switch:"))
async def switch_plan(callback: CallbackQuery):
    await create_plan_order(callback, immediate_switch=True)


async def create_plan_order(callback: CallbackQuery, immediate_switch: bool):
    plan_id = int(callback.data.split(":", 1)[1])
    try:
        switch_now = immediate_switch
        with SessionLocal() as db:
            plan = db.get(Plan, plan_id)
            if not plan or not plan.enabled or not plan.show_in_bot:
                await callback.answer("Тариф недоступен", show_alert=True)
                return
            current = db.get(Subscription, callback.from_user.id)
            current_expiry = (current.expires_at.replace(tzinfo=timezone.utc) if current and current.expires_at.tzinfo is None
                              else (current.expires_at if current else None))
            if (current and current.enabled and current_expiry and current_expiry > datetime.now(timezone.utc)
                    and current.plan_id and current.plan_id != plan.id):
                switch_now = True
            if switch_now and db.scalar(select(PendingPayment).where(
                PendingPayment.telegram_id == callback.from_user.id,
                PendingPayment.immediate_switch.is_(True)
            )):
                await callback.answer("У вас уже есть неоплаченная смена тарифа. Завершите оплату или дождитесь отмены счёта.", show_alert=True)
                return
            charge, credit, service_days = quote_immediate_switch(db, callback.from_user.id, plan) if switch_now else (plan.amount, 0, plan.days)
            snapshot = {"plan_name_snapshot": plan.name, "plan_amount_snapshot": plan.amount,
                        "plan_currency_snapshot": plan.currency, "plan_days_snapshot": plan.days,
                        "plan_traffic_gb_snapshot": plan.traffic_limit_gb,
                        "plan_hwid_snapshot": plan.limit_hwid, "plan_reset_snapshot": plan.traffic_reset}
        if switch_now and charge == 0:
            invoice_id = "credit-" + uuid.uuid4().hex
            with SessionLocal() as db:
                db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id, plan_id=plan_id,
                                      product_type="plan", charged_amount=0, credit_amount=credit, immediate_switch=True,
                                      switch_days=service_days, **snapshot))
                db.commit()
            result = await provision_paid_invoice(invoice_id)
            await callback.message.answer(f"Тариф изменён сразу. Остаток зачтён; новый срок — {service_days} дн.")
            if isinstance(result, tuple) and result[1]:
                bridge = happ_bridge_url(f"happ://add/{result[1]}")
                if bridge:
                    await callback.message.answer("Откройте новую подписку в Happ:", reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="Открыть подписку в Happ", url=bridge)]]))
                else:
                    await callback.message.answer("Ссылка для Happ:\n" + result[1])
            await callback.answer()
            return
        invoice_id, pay_url = await LavaClient().create_invoice(callback.from_user.id, plan, charge)
        with SessionLocal() as db:
            db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id,
                                  plan_id=plan_id, product_type="plan", charged_amount=charge,
                                  credit_amount=credit, immediate_switch=switch_now,
                                  switch_days=service_days if switch_now else 0, **snapshot))
            db.commit()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=pay_url)]])
        description = (f"Смена тарифа сейчас. К оплате {charge} {plan.currency}; учтено остатка: {credit} {plan.currency}." if switch_now
                       else "Счёт создан. После подтверждения оплаты бот пришлёт ссылку для Happ.")
        await callback.message.answer(description, reply_markup=keyboard)
        await callback.answer()
    except Exception:
        await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)


@dp.callback_query(F.data.startswith("addon:"))
async def buy_addon(callback: CallbackQuery):
    try:
        package_id = int(callback.data.split(":", 1)[1])
        with SessionLocal() as db:
            package = db.get(AddonPackage, package_id)
            sub = db.get(Subscription, callback.from_user.id)
            expires = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
            if not package or not package.enabled or not sub or not sub.enabled or not expires or expires <= datetime.now(timezone.utc) or not sub.traffic_limit_bytes:
                await callback.answer("Пакет сейчас недоступен.", show_alert=True)
                return
            invoice_id, pay_url = await LavaClient().create_invoice(callback.from_user.id, package)
            db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id,
                                  plan_id=0, package_id=package.id, product_type="addon",
                                  package_traffic_bytes=int(package.traffic_gb * (1024 ** 3))))
            db.commit()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=pay_url)]])
        await callback.message.answer("Счёт на дополнительный трафик создан.", reply_markup=keyboard)
        await callback.answer()
    except Exception:
        await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)


async def start_bot(token: str):
    delay = 5
    while True:
        bot = Bot(token)
        try:
            logger.info("Connecting Telegram bot via long polling")
            await dp.start_polling(bot)
            logger.info("Telegram polling stopped")
            return
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.warning("Telegram connection failed; retrying in %s seconds: %s", delay, exc)
        except TelegramUnauthorizedError:
            logger.error("Telegram rejected the bot token; update it in admin settings")
            return
        except TelegramConflictError:
            logger.error("Telegram bot polling conflict: another process is using this token; this instance will stop")
            return
        finally:
            await bot.session.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def notify_user(telegram_id: int, text: str, reply_markup=None):
    token = get_config("bot_token")
    if not token:
        raise RuntimeError("Telegram bot token is not configured")
    bot = Bot(token)
    try:
        await bot.send_message(telegram_id, text, reply_markup=reply_markup)
    finally:
        await bot.session.close()
