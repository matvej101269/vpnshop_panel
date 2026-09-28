import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
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
from sqlalchemy.exc import IntegrityError
from app.db import (SessionLocal, Plan, AddonPackage, PendingPayment, BotMenuNode,
                    Subscription, SubscriptionHistory, TrialClaim, PromoCode, PromoPrompt,
                    PromoSelection, PromoRedemption, ReferralAttribution, ReferralReward)
from app.services import XUIClient, happ_link, quote_immediate_switch, provision_paid_invoice
from app.runtime_config import get_config, get_config_map, encrypt_handoff
from app.checkout import new_checkout
from app.db import PlanPeriod, UserCurrency
from app.plan_catalog import CURRENCIES, selected_currency, visible_plans, periods_for, period_label, period_plan

logger = logging.getLogger(__name__)
BOT_USERNAME = ""


def _trial_already_used(db, telegram_id: int) -> bool:
    return bool(db.get(Subscription, telegram_id) or db.get(TrialClaim, telegram_id) or
                db.scalar(select(SubscriptionHistory.id).where(
                    SubscriptionHistory.telegram_id == telegram_id).limit(1)) or
                db.scalar(select(PendingPayment.invoice_id).where(
                    PendingPayment.telegram_id == telegram_id).limit(1)))


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
    def add_button(node, button):
        if node.same_row and rows and len(rows[-1]) < 2:
            rows[-1].append(button)
        else:
            rows.append([button])
    for node in nodes:
        if node.action == "url":
            add_button(node, InlineKeyboardButton(text=node.label, url=node.url))
        elif node.action == "trial":
            if telegram_id is not None and not _trial_already_used(db, telegram_id):
                add_button(node, InlineKeyboardButton(text=node.label, callback_data=f"trial:{node.id}"))
        elif node.action == "routing" and node.routing_rules:
            # Route actions need a callback first: it associates the profile with
            # the user's active subscription before generating its Happ URL.
            add_button(node, InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}"))
        elif node.action == "open_happ" and telegram_id is not None:
            sub = db.get(Subscription, telegram_id)
            expiry = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
            link = happ_link(sub.sub_id) if sub and sub.enabled and expiry and expiry > datetime.now(timezone.utc) else ""
            deep_link = happ_bridge_url(f"happ://add/{link}") if link.startswith(("http://", "https://")) else ""
            if deep_link and len(deep_link) <= 4096:
                add_button(node, InlineKeyboardButton(text=node.label, url=deep_link))
            else:
                add_button(node, InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}"))
        elif node.action in {"promo", "referral"}:
            add_button(node, InlineKeyboardButton(text=node.label, callback_data=f"action:{node.id}"))
        else:
            add_button(node, InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}"))
    if parent_id is None and telegram_id is not None:
        configured_trial = db.scalar(select(BotMenuNode.id).where(
            BotMenuNode.action == "trial", BotMenuNode.enabled.is_(True)).limit(1))
        if not configured_trial and not _trial_already_used(db, telegram_id):
            rows.append([InlineKeyboardButton(text="Пробный период · 3 дня", callback_data="trial:0")])
    if include_back and parent_id is not None:
        parent = db.get(BotMenuNode, parent_id)
        back_parent = parent.parent_id if parent else None
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"menu:{back_parent or 0}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def currency_keyboard(action="buy", parent=0):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=c, callback_data=f"currency:{c}:{action}:{parent}") for c in CURRENCIES],
        [InlineKeyboardButton(text="‹ Главное меню", callback_data="menu:0")]])


def catalog_view(db, telegram_id, action="buy", parent=0):
    currency = selected_currency(db, telegram_id)
    if not currency:
        return "Выберите валюту оплаты:", currency_keyboard(action, parent)
    rows = []
    for plan in visible_plans(db, currency):
        periods = periods_for(db, plan.id)
        if periods:
            price = min(p.amount for p in periods)
            rows.append([InlineKeyboardButton(text=f"{plan.name} · от {price} {currency}", callback_data=f"periods:{plan.id}:{action}")])
    text = f"Выберите тариф · {currency}:" if rows else f"Пока нет доступных тарифов в {currency}. Выберите другую валюту."
    rows.append([InlineKeyboardButton(text=f"Сменить валюту · {currency}", callback_data=f"currencies:{action}:{parent}")])
    rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"menu:{parent}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("currencies:"))
async def choose_currency_menu(callback: CallbackQuery):
    _, action, parent = callback.data.split(":")
    if action not in {"buy", "switch"} or not parent.isdigit():
        await callback.answer("Меню недоступно")
        return
    await callback.message.answer("Выберите валюту оплаты:", reply_markup=currency_keyboard(action, int(parent)))
    await callback.answer()


@dp.callback_query(F.data.startswith("currency:"))
async def choose_currency(callback: CallbackQuery):
    _, currency, action, parent = callback.data.split(":")
    if currency not in CURRENCIES or action not in {"buy", "switch"} or not parent.isdigit():
        await callback.answer("Валюта недоступна", show_alert=True)
        return
    with SessionLocal() as db:
        row = db.get(UserCurrency, callback.from_user.id)
        if row:
            row.currency = currency
        else:
            db.add(UserCurrency(telegram_id=callback.from_user.id, currency=currency))
        db.commit()
        text, keyboard = catalog_view(db, callback.from_user.id, action, int(parent))
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("periods:"))
async def choose_period(callback: CallbackQuery):
    _, plan_id, action = callback.data.split(":")
    if action not in {"buy", "switch"} or not plan_id.isdigit():
        await callback.answer("Тариф недоступен")
        return
    with SessionLocal() as db:
        plan = db.get(Plan, int(plan_id))
        if not plan or not plan.enabled or not plan.show_in_bot:
            await callback.answer("Тариф недоступен", show_alert=True)
            return
        if selected_currency(db, callback.from_user.id) != plan.currency:
            await callback.message.answer("Выберите валюту оплаты:", reply_markup=currency_keyboard(action))
            await callback.answer()
            return
        rows = [[InlineKeyboardButton(text=f"{period_label(p)} · {p.days} дн. — {p.amount} {plan.currency}",
                                      callback_data=f"{action}:{plan.id}:{p.id}")]
                for p in periods_for(db, plan.id)]
        rows.append([InlineKeyboardButton(text="‹ Тарифы и валюта", callback_data=f"currencies:{action}:0")])
        text = f"{plan.name} · {plan.currency}\nВыберите период подписки:"
    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


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
                action = "switch" if node.action == "change_plan" else "buy"
                text, keyboard = catalog_view(db, telegram_id, action, node.parent_id or 0)
                if node.text:
                    text = node.text + "\n\n" + text
            elif node.action == "currency":
                text = node.text or "Выберите валюту оплаты:"
                keyboard = currency_keyboard("buy", node.parent_id or 0)
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
                packages = db.scalars(select(AddonPackage).where(AddonPackage.enabled.is_(True), AddonPackage.currency == selected_currency(db, telegram_id))).all() if sub and sub.enabled and sub.traffic_limit_bytes and expires and expires > datetime.now(timezone.utc) else []
                rows = [[InlineKeyboardButton(text=f"{pkg.name} · {pkg.traffic_gb:g} ГБ — {pkg.amount} {pkg.currency}",
                                              callback_data=f"addon:{pkg.id}")] for pkg in packages]
                rows.append([InlineKeyboardButton(text="Выбрать валюту", callback_data="currencies:buy:0")])
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
        if parent_id is None and not db.scalar(select(BotMenuNode.id).where(
                BotMenuNode.parent_id.is_(None), BotMenuNode.enabled.is_(True)).limit(1)):
            text, keyboard = catalog_view(db, telegram_id)
            if not _trial_already_used(db, telegram_id):
                keyboard.inline_keyboard.append([InlineKeyboardButton(text="Пробный период · 3 дня", callback_data="trial:0")])
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
    pieces = (message.text or "").split(maxsplit=1)
    payload = pieces[1].strip() if len(pieces) > 1 else ""
    user_id = message.from_user.id
    referral = re.fullmatch(r"ref_([0-9]{1,20})_([A-Za-z0-9_-]{8})", payload)
    if referral and get_config("referral_enabled").lower() in {"1", "true", "yes", "on"}:
        referrer_id = int(referral.group(1))
        expected = base64.urlsafe_b64encode(hmac.new(
            get_config("bot_token").encode(), str(referrer_id).encode(), hashlib.sha256).digest()[:6]
        ).decode().rstrip("=")
        with SessionLocal() as db:
            if (referrer_id != user_id and hmac.compare_digest(referral.group(2), expected)
                    and not _trial_already_used(db, user_id)
                    and not db.get(ReferralAttribution, user_id)):
                db.add(ReferralAttribution(referred_id=user_id, referrer_id=referrer_id))
                db.commit()
    await show_menu(message, None)


@dp.callback_query(F.data.startswith("action:"))
async def menu_action(callback: CallbackQuery):
    raw_id = callback.data.split(":", 1)[1]
    with SessionLocal() as db:
        node = db.get(BotMenuNode, int(raw_id)) if raw_id.isdigit() else None
        if not node or not node.enabled or node.action not in {"promo", "referral"}:
            await callback.answer("Действие недоступно.", show_alert=True)
            return
        if node.action == "promo":
            expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=10)
            prompt = db.get(PromoPrompt, callback.from_user.id)
            if prompt:
                prompt.expires_at = expiry
            else:
                db.add(PromoPrompt(telegram_id=callback.from_user.id, expires_at=expiry))
            db.commit()
            text = "Отправьте промокод одним сообщением. Запрос действует 10 минут."
        else:
            if not get_config("referral_enabled").lower() in {"1", "true", "yes", "on"}:
                await callback.answer("Реферальная программа сейчас выключена.", show_alert=True)
                return
            if not BOT_USERNAME:
                await callback.answer("Ссылка временно недоступна.", show_alert=True)
                return
            referral_signature = base64.urlsafe_b64encode(hmac.new(
                get_config("bot_token").encode(), str(callback.from_user.id).encode(), hashlib.sha256
            ).digest()[:6]).decode().rstrip("=")
            text = (f"Ваша реферальная ссылка:\nhttps://t.me/{BOT_USERNAME}?start="
                    f"ref_{callback.from_user.id}_{referral_signature}")
    await callback.message.answer(text)
    await callback.answer()


@dp.message(F.text, ~F.text.startswith("/"))
async def accept_promo_code(message: Message):
    user_id = message.from_user.id
    with SessionLocal() as db:
        prompt = db.get(PromoPrompt, user_id)
        if not prompt:
            return
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.delete(prompt)
        if prompt.expires_at <= now:
            db.commit()
            await message.answer("Время ввода истекло. Нажмите кнопку промокода ещё раз.")
            return
        code = (message.text or "").strip().upper()
        promo = db.scalar(select(PromoCode).where(PromoCode.code == code))
        expires = promo.expires_at.replace(tzinfo=None) if promo and promo.expires_at.tzinfo else (promo.expires_at if promo else None)
        if not promo or not expires or expires <= now:
            db.commit()
            await message.answer("Промокод не найден или срок его действия истёк.")
            return
        if db.get(PromoRedemption, (promo.id, user_id)):
            db.commit()
            await message.answer("Вы уже использовали этот промокод.")
            return
        selection = db.get(PromoSelection, user_id)
        if selection:
            selection.promo_id = promo.id
            selection.selected_at = now
        else:
            db.add(PromoSelection(telegram_id=user_id, promo_id=promo.id, selected_at=now))
        db.commit()
        eligible = [int(value) for value in promo.plan_ids.split(",") if value.isdigit()]
        plans = db.scalars(select(Plan).where(Plan.id.in_(eligible), Plan.enabled.is_(True),
                                             Plan.show_in_bot.is_(True))).all() if eligible else []
        names = ", ".join(plan.name for plan in plans) or "настроенные тарифы"
    await message.answer(f"Промокод принят: скидка {promo.discount_percent}%. Доступен для: {names}. Теперь выберите тариф.")


@dp.callback_query(F.data.startswith("menu:"))
async def open_menu(callback: CallbackQuery):
    raw_id = callback.data.split(":", 1)[1]
    parent_id = int(raw_id) if raw_id.isdigit() and int(raw_id) else None
    await show_menu(callback, parent_id)
    await callback.answer()


@dp.callback_query(F.data.startswith("trial:"))
async def start_trial(callback: CallbackQuery):
    raw_id = callback.data.split(":", 1)[1]
    if not raw_id.isdigit():
        await callback.answer("Пробный период недоступен.", show_alert=True)
        return
    telegram_id = callback.from_user.id
    menu_parent_id = None
    with SessionLocal() as db:
        node_id = int(raw_id)
        if node_id:
            node = db.get(BotMenuNode, node_id)
            if not node or not node.enabled or node.action != "trial":
                await callback.answer("Пробный период недоступен.", show_alert=True)
                return
            menu_parent_id = node.parent_id
        if _trial_already_used(db, telegram_id):
            await callback.answer("Пробный период доступен только новым пользователям.", show_alert=True)
            return
        db.add(TrialClaim(telegram_id=telegram_id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            await callback.answer("Пробный период уже был использован.", show_alert=True)
            return

    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=3)
    sub_id = uuid.uuid4().hex[:20]
    xui_created = False
    try:
        inbounds = XUIClient._inbound_ids(get_config_map())
        await XUIClient().add_or_update_client(
            telegram_id, sub_id, int(expires.timestamp() * 1000), 0, exists=False,
            limit_hwid=0, traffic_reset="never", inbound_ids=inbounds,
            group_name="Пробный период")
        xui_created = True
        with SessionLocal() as db:
            if db.get(Subscription, telegram_id):
                raise RuntimeError("Пользователь уже получил подписку")
            db.add(Subscription(telegram_id=telegram_id, sub_id=sub_id, expires_at=expires,
                                plan_id=None, plan_name="Пробный период", current_price=0,
                                currency="RUB", traffic_limit_bytes=0, limit_hwid=0,
                                traffic_reset="never", inbound_ids=",".join(map(str, inbounds)),
                                enabled=True))
            db.add(SubscriptionHistory(telegram_id=telegram_id, plan_name="Пробный период",
                                       plan_days=3, price=0, currency="RUB", traffic_limit_bytes=0,
                                       starts_at=now.replace(tzinfo=None), expires_at=expires.replace(tzinfo=None)))
            db.commit()
    except Exception:
        logger.exception("Could not provision a trial subscription for Telegram ID %s", telegram_id)
        rollback_ok = True
        if xui_created:
            try:
                await XUIClient().delete_client(sub_id, telegram_id)
            except Exception:
                rollback_ok = False
                logger.exception("Could not roll back the 3x-ui trial client for Telegram ID %s", telegram_id)
        if rollback_ok:
            with SessionLocal() as db:
                claim = db.get(TrialClaim, telegram_id)
                if claim:
                    db.delete(claim)
                    db.commit()
        await callback.answer("Не удалось выдать пробный период. Попробуйте позже.", show_alert=True)
        return

    link = happ_link(sub_id)
    bridge = happ_bridge_url(f"happ://add/{link}") if link else ""
    text = f"Пробный период на 3 дня активирован. Действует до {expires:%d.%m.%Y %H:%M UTC}."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="Открыть подписку в Happ", url=bridge)]]) if bridge else None
    try:
        with SessionLocal() as db:
            menu_markup = menu_keyboard(db, menu_parent_id, telegram_id=telegram_id)
        await callback.message.edit_reply_markup(reply_markup=menu_markup)
    except TelegramBadRequest:
        pass
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer("Пробный период активирован")


@dp.callback_query(F.data.startswith("buy:"))
async def buy(callback: CallbackQuery):
    await create_plan_order(callback, immediate_switch=False)


@dp.callback_query(F.data.startswith("switch:"))
async def switch_plan(callback: CallbackQuery):
    await create_plan_order(callback, immediate_switch=True)


async def create_plan_order(callback: CallbackQuery, immediate_switch: bool):
    try:
        parts = callback.data.split(":")
        plan_id = int(parts[1])
        switch_now = immediate_switch
        with SessionLocal() as db:
            plan = db.get(Plan, plan_id)
            if not plan or not plan.enabled or not plan.show_in_bot:
                await callback.answer("Тариф недоступен", show_alert=True)
                return
            if selected_currency(db, callback.from_user.id) != plan.currency:
                await callback.message.answer("Выберите валюту оплаты:", reply_markup=currency_keyboard("switch" if immediate_switch else "buy"))
                await callback.answer()
                return
            if len(parts) != 3:
                await callback.message.answer("Выберите период тарифа:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"{period_label(p)} — {p.amount} {plan.currency}", callback_data=f"{parts[0]}:{plan.id}:{p.id}")]
                    for p in periods_for(db, plan.id)]))
                await callback.answer()
                return
            period = db.get(PlanPeriod, int(parts[2]))
            plan = period_plan(plan, period)
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
            promo_id, promo_percent, promo_code = None, 0, ""
            selection = db.get(PromoSelection, callback.from_user.id)
            if selection:
                promo = db.get(PromoCode, selection.promo_id)
                promo_expiry = (promo.expires_at.replace(tzinfo=timezone.utc) if promo and promo.expires_at.tzinfo is None
                                else (promo.expires_at if promo else None))
                eligible_ids = [int(v) for v in promo.plan_ids.split(",") if v.isdigit()] if promo else []
                if not promo or not promo_expiry or promo_expiry <= datetime.now(timezone.utc):
                    await callback.answer("Промокод истёк. Введите действующий код заново.", show_alert=True)
                    return
                if plan.id not in eligible_ids:
                    await callback.answer("Промокод не действует на этот тариф.", show_alert=True)
                    return
                if db.get(PromoRedemption, (promo.id, callback.from_user.id)):
                    db.delete(selection)
                    db.commit()
                    await callback.answer("Вы уже использовали этот промокод.", show_alert=True)
                    return
                if db.scalar(select(PendingPayment.invoice_id).where(
                    PendingPayment.telegram_id == callback.from_user.id,
                    PendingPayment.promo_code_id == promo.id
                )):
                    await callback.answer("У вас уже есть счёт с этим промокодом.", show_alert=True)
                    return
                promo_id, promo_percent, promo_code = promo.id, promo.discount_percent, promo.code
                charge = (charge * (100 - promo_percent) + 99) // 100
            snapshot = {"plan_name_snapshot": plan.name, "plan_amount_snapshot": plan.amount,
                        "plan_currency_snapshot": plan.currency, "plan_days_snapshot": plan.days,
                        "plan_traffic_gb_snapshot": plan.traffic_limit_gb,
                        "plan_hwid_snapshot": plan.limit_hwid, "plan_reset_snapshot": plan.traffic_reset,
                        "promo_code_id": promo_id, "promo_percent_snapshot": promo_percent,
                        "promo_code_snapshot": promo_code}
        if charge == 0:
            invoice_id = "credit-" + uuid.uuid4().hex
            with SessionLocal() as db:
                db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id, plan_id=plan_id,
                                      product_type="plan", charged_amount=0, credit_amount=credit,
                                      immediate_switch=switch_now, switch_days=service_days if switch_now else 0,
                                      **snapshot))
                db.commit()
            result = await provision_paid_invoice(invoice_id)
            await callback.message.answer(
                f"Тариф активирован без оплаты. {('Скидка по промокоду ' + promo_code + ' составила 100%.') if promo_percent == 100 else f'Остаток зачтён; новый срок — {service_days} дн.'}")
            if isinstance(result, tuple) and result[1]:
                bridge = happ_bridge_url(f"happ://add/{result[1]}")
                if bridge:
                    await callback.message.answer("Откройте новую подписку в Happ:", reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="Открыть подписку в Happ", url=bridge)]]))
                else:
                    await callback.message.answer("Ссылка для Happ:\n" + result[1])
            await callback.answer()
            return
        with SessionLocal() as db:
            invoice_id, pay_url = new_checkout(db, f"{plan.name} · {period_label(period)}", charge, plan.currency)
            db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id,
                                  plan_id=plan_id, product_type="plan", charged_amount=charge,
                                  credit_amount=credit, immediate_switch=switch_now,
                                  switch_days=service_days if switch_now else 0, **snapshot))
            db.commit()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=pay_url)]])
        description = (f"Смена тарифа сейчас. К оплате {charge} {plan.currency}; учтено остатка: {credit} {plan.currency}." if switch_now
                       else "Счёт создан. После подтверждения оплаты бот пришлёт ссылку для Happ.")
        if promo_percent:
            description = f"Промокод {promo_code}: скидка {promo_percent}%. К оплате {charge} {plan.currency}.\n" + description
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
            if package and selected_currency(db, callback.from_user.id) != package.currency:
                await callback.answer("Выберите валюту пакета в меню бота", show_alert=True)
                return
            sub = db.get(Subscription, callback.from_user.id)
            expires = sub.expires_at.replace(tzinfo=timezone.utc) if sub and sub.expires_at.tzinfo is None else (sub.expires_at if sub else None)
            if not package or not package.enabled or not sub or not sub.enabled or not expires or expires <= datetime.now(timezone.utc) or not sub.traffic_limit_bytes:
                await callback.answer("Пакет сейчас недоступен.", show_alert=True)
                return
            invoice_id, pay_url = new_checkout(db, package.name, package.amount, package.currency)
            db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id,
                                  plan_id=0, package_id=package.id, product_type="addon",
                                  charged_amount=package.amount,
                                  package_traffic_bytes=int(package.traffic_gb * (1024 ** 3))))
            db.commit()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=pay_url)]])
        await callback.message.answer("Счёт на дополнительный трафик создан.", reply_markup=keyboard)
        await callback.answer()
    except Exception:
        await callback.answer("Не удалось создать счёт. Попробуйте позже.", show_alert=True)


async def start_bot(token: str):
    global BOT_USERNAME
    delay = 5
    while True:
        bot = Bot(token)
        try:
            logger.info("Connecting Telegram bot via long polling")
            me = await bot.get_me()
            BOT_USERNAME = me.username or ""
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
