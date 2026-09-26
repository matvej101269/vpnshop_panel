from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from sqlalchemy import select
from app.db import SessionLocal, Plan, PendingPayment
from app.services import LavaClient
from app.runtime_config import get_config

dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message):
    with SessionLocal() as db:
        plans = db.scalars(select(Plan).where(Plan.enabled.is_(True))).all()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"{p.name} — {p.amount} {p.currency}", callback_data=f"buy:{p.id}")] for p in plans])
        base = get_config("public_base_url").rstrip("/")
        if base:
            keyboard.inline_keyboard.append([InlineKeyboardButton(text="Договор оферты", url=f"{base}/offer")])
    await message.answer(get_config("bot_welcome_text"), reply_markup=keyboard)


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
    bot = Bot(token)
    await dp.start_polling(bot)


async def notify_user(telegram_id: int, text: str):
    token = get_config("bot_token")
    if not token:
        return
    bot = Bot(token)
    try:
        await bot.send_message(telegram_id, text)
    finally:
        await bot.session.close()
