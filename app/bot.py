import asyncio
import ipaddress
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramUnauthorizedError
from urllib.parse import urlsplit
from sqlalchemy import select
from app.db import SessionLocal, Plan, PendingPayment, BotMenuNode
from app.services import LavaClient
from app.runtime_config import get_config

dp = Dispatcher()
logger = logging.getLogger(__name__)


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


def menu_keyboard(db, parent_id: int | None, include_back: bool = True):
    nodes = db.scalars(select(BotMenuNode).where(
        BotMenuNode.parent_id == parent_id, BotMenuNode.enabled.is_(True)
    ).order_by(BotMenuNode.position, BotMenuNode.id)).all()
    rows = []
    for node in nodes:
        if node.action == "url":
            rows.append([InlineKeyboardButton(text=node.label, url=node.url)])
        else:
            rows.append([InlineKeyboardButton(text=node.label, callback_data=f"menu:{node.id}")])
    if include_back and parent_id is not None:
        parent = db.get(BotMenuNode, parent_id)
        back_parent = parent.parent_id if parent else None
        rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"menu:{back_parent or 0}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def show_menu(target, parent_id: int | None):
    with SessionLocal() as db:
        if parent_id is None:
            text = get_config("bot_welcome_text")
            keyboard = menu_keyboard(db, None, include_back=False)
        else:
            node = db.get(BotMenuNode, parent_id)
            if not node or not node.enabled:
                await target.message.answer("Это меню больше недоступно.")
                return
            text = node.text or node.label
            if node.action == "plans":
                plans = db.scalars(select(Plan).where(Plan.enabled.is_(True))).all()
                rows = [[InlineKeyboardButton(text=f"{p.name} — {p.amount} {p.currency}", callback_data=f"buy:{p.id}")]
                        for p in plans]
                rows.append([InlineKeyboardButton(text="‹ Назад", callback_data=f"menu:{node.parent_id or 0}")])
                keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
                text = node.text or "Выберите период VPN-подписки:"
            else:
                keyboard = menu_keyboard(db, node.id)
        if parent_id is None and not keyboard:
            plans = db.scalars(select(Plan).where(Plan.enabled.is_(True))).all()
            rows = [[InlineKeyboardButton(text=f"{p.name} — {p.amount} {p.currency}", callback_data=f"buy:{p.id}")]
                    for p in plans]
            base = get_config("public_base_url").rstrip("/")
            if is_public_http_url(base):
                rows.append([InlineKeyboardButton(text="Договор оферты", url=f"{base}/offer")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    if isinstance(target, Message):
        await target.answer(text, reply_markup=keyboard)
    else:
        await target.message.answer(text, reply_markup=keyboard)


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
    plan_id = int(callback.data.split(":", 1)[1])
    try:
        with SessionLocal() as db:
            plan = db.get(Plan, plan_id)
            if not plan or not plan.enabled:
                await callback.answer("Тариф недоступен", show_alert=True)
                return
        invoice_id, pay_url = await LavaClient().create_invoice(callback.from_user.id, plan)
        with SessionLocal() as db:
            db.add(PendingPayment(invoice_id=invoice_id, telegram_id=callback.from_user.id, plan_id=plan_id))
            db.commit()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=pay_url)]])
        await callback.message.answer("Счёт создан. После подтверждения оплаты бот пришлёт ссылку для Happ.", reply_markup=keyboard)
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
            return
        except (TelegramNetworkError, TelegramServerError) as exc:
            logger.warning("Telegram connection failed; retrying in %s seconds: %s", delay, exc)
        except TelegramUnauthorizedError:
            logger.error("Telegram rejected the bot token; update it in admin settings")
            return
        finally:
            await bot.session.close()
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


async def notify_user(telegram_id: int, text: str):
    token = get_config("bot_token")
    if not token:
        return
    bot = Bot(token)
    try:
        await bot.send_message(telegram_id, text)
    finally:
        await bot.session.close()
