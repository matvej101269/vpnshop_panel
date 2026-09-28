"""Currency quotes and payment attempts with one fulfillment per order."""
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update

from app.db import Checkout, CheckoutQuote, PaymentAttempt, PendingPayment, SessionLocal
from app.runtime_config import get_config
from app.services import LavaClient
from app.payment_options import METHODS, amount_limit_error

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex, nofollow"}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def checkout_origin():
    origin = get_config("public_base_url").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Checkout requires a public HTTPS URL")
    return origin


def new_checkout(db, title: str, amount: int, currency: str) -> tuple[str, str]:
    origin = checkout_origin()
    if currency not in METHODS or amount <= 0:
        raise ValueError("Unsupported checkout currency or amount")
    token = secrets.token_urlsafe(32)
    invoice_id = "checkout-" + token
    db.add(Checkout(token=token, csrf=secrets.token_urlsafe(32), invoice_id=invoice_id,
                    title=title, amount=amount, currency=currency))
    return invoice_id, origin + "/pay/" + token


def matches_payment(payment, payload):
    try:
        amount = Decimal(str(payload.get("amount")))
        return (amount.is_finite() and amount == Decimal(payment.amount)
                and payload.get("currency") == payment.currency)
    except (InvalidOperation, ValueError, TypeError):
        return False


def get_checkout(db, token):
    row = db.get(Checkout, token) if len(token) <= 64 else None
    if row is None:
        raise HTTPException(404, "Заказ не найден")
    return row


def legacy_attempt(db, row):
    """Preserve invoices created by the previous checkout implementation."""
    if row.payment_url and not row.invoice_id.startswith("checkout-"):
        found = db.scalar(select(PaymentAttempt).where(PaymentAttempt.invoice_id == row.invoice_id))
        if not found:
            db.add(PaymentAttempt(token=secrets.token_urlsafe(32), checkout_token=row.token,
                                  invoice_id=row.invoice_id, amount=row.amount, currency=row.currency,
                                  method=row.method, payment_url=row.payment_url,
                                  state="paid" if row.state == "paid" else "ready"))
            db.flush()


async def render_page(token, request, *, error="", status=200):
    with SessionLocal() as db:
        row = get_checkout(db, token)
        state = row.state
        if state != "paid" and not db.get(PendingPayment, row.invoice_id):
            state = "expired"
        if state in {"open", "ready", "failed"} and row.created_at < now() - timedelta(hours=24):
            state = "expired"
        choose = state in {"open", "failed"} or (state == "ready" and request.query_params.get("change") == "1")
        currency = row.currency
        quote = None
        if choose:
            quote = db.scalar(select(CheckoutQuote).where(
                CheckoutQuote.checkout_token == token, CheckoutQuote.currency == currency,
                CheckoutQuote.expires_at > now()
            ).order_by(CheckoutQuote.expires_at.desc()).limit(1))
        base_amount = row.amount
    if choose and quote is None:
        try:
            amount, rate, day = Decimal(base_amount), Decimal(1), ""
            with SessionLocal() as db:
                quote = CheckoutQuote(token=secrets.token_urlsafe(32), checkout_token=token,
                                      amount=amount, currency=currency, rate=rate, rate_date=day,
                                      expires_at=now() + timedelta(minutes=15))
                db.add(quote)
                db.commit()
        except Exception as exc:
            logger.warning("Checkout quote unavailable (%s)", type(exc).__name__)
            error = "Не удалось подготовить заказ. Повторите позже."
    with SessionLocal() as db:
        row = get_checkout(db, token)
        if row.state == "paid":
            state, choose = "paid", False
        attempts = db.scalars(select(PaymentAttempt).where(
            PaymentAttempt.checkout_token == token).order_by(PaymentAttempt.created_at.desc())).all()
        active = next((a for a in attempts if a.payment_url == row.payment_url), None)
        extra_paid = any(a.state == "extra_paid" for a in attempts)
        return templates.TemplateResponse(request, "checkout.html", {
            "checkout": row, "state": state, "choose": choose, "error": error,
            "currency": currency, "methods": METHODS[currency],
            "quote": quote, "active": active, "extra_paid": extra_paid,
            "limit_error": amount_limit_error(quote.amount, quote.currency) if quote else "",
        }, status_code=status, headers=HEADERS)


@router.get("/pay/{token}")
async def checkout_page(token: str, request: Request):
    return await render_page(token, request)


@router.post("/pay/{token}")
async def choose_method(token: str, request: Request):
    form = await request.form()
    method = str(form.get("method", ""))
    with SessionLocal() as db:
        row = get_checkout(db, token)
        if not secrets.compare_digest(row.csrf, str(form.get("csrf", ""))):
            raise HTTPException(403, "Обновите страницу оплаты")
        if row.state in {"paid", "creating"}:
            return RedirectResponse("/pay/" + token, status_code=303, headers=HEADERS)
        if row.created_at < now() - timedelta(hours=24) or not db.get(PendingPayment, row.invoice_id):
            raise HTTPException(410, "Срок заказа истёк. Создайте новый заказ в боте.")
        quote = db.get(CheckoutQuote, str(form.get("quote", "")))
        if not quote or quote.checkout_token != token or quote.expires_at <= now():
            return await render_page(token, request, error="Цена устарела. Проверьте обновлённую сумму и выберите способ ещё раз.", status=409)
        if method not in METHODS.get(quote.currency, {}):
            raise HTTPException(400, "Способ недоступен для выбранной валюты")
        if quote.currency != row.currency or quote.amount != Decimal(row.amount):
            raise HTTPException(409, "Валюта и цена выбираются в боте. Откройте страницу заказа заново.")
        limit_error = amount_limit_error(quote.amount, quote.currency)
        if limit_error:
            return await render_page(token, request, error=limit_error, status=400)
        full_name = str(form.get("full_name", "")).strip() if method in {"BANCONTACT", "BIZUM"} else ""
        wallet_id = str(form.get("wallet_id", "")).strip() if method == "BIZUM" else ""
        if method in {"BANCONTACT", "BIZUM"} and not 1 <= len(full_name) <= 256:
            raise HTTPException(400, "Укажите имя плательщика")
        if method == "BIZUM" and not re.fullmatch(r"\+?[1-9][0-9]{7,14}", wallet_id):
            raise HTTPException(400, "Укажите телефон Bizum в международном формате")
        claimed = db.execute(update(Checkout).where(
            Checkout.token == token, Checkout.state.in_(["open", "ready", "failed"])
        ).values(state="creating")).rowcount
        if not claimed:
            db.rollback()
            return RedirectResponse("/pay/" + token, status_code=303, headers=HEADERS)
        legacy_attempt(db, row)
        previous = db.scalar(select(PaymentAttempt).where(
            PaymentAttempt.checkout_token == token, PaymentAttempt.currency == quote.currency,
            PaymentAttempt.amount == quote.amount, PaymentAttempt.method == method,
            PaymentAttempt.state == "ready").order_by(PaymentAttempt.created_at.desc()).limit(1))
        if previous:
            row.state, row.payment_url, row.method = "ready", previous.payment_url, method
            db.commit()
            return RedirectResponse(previous.payment_url, status_code=303, headers=HEADERS)
        uncertain = db.scalar(select(PaymentAttempt.token).where(
            PaymentAttempt.checkout_token == token, PaymentAttempt.currency == quote.currency,
            PaymentAttempt.method == method, PaymentAttempt.state.in_(["failed", "creating"])))
        if uncertain:
            db.rollback()
            return await render_page(token, request, error="Этот способ ожидает проверки после ошибки связи. Выберите другой или обратитесь в поддержку.", status=409)
        attempt = PaymentAttempt(token=secrets.token_urlsafe(32), checkout_token=token,
                                 amount=quote.amount, currency=quote.currency, method=method)
        db.add(attempt)
        db.commit()
        attempt_token = attempt.token
        product = SimpleNamespace(amount=quote.amount, currency=quote.currency)
    try:
        invoice_id, pay_url = await LavaClient().create_invoice(
            0, product, payment_method=method, return_url=checkout_origin() + "/pay/" + token,
            full_name=full_name, wallet_id=wallet_id)
        parsed = urlsplit(pay_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Invalid payment URL")
        with SessionLocal() as db:
            attempt = db.get(PaymentAttempt, attempt_token)
            attempt.invoice_id, attempt.payment_url, attempt.state = invoice_id, pay_url, "ready"
            changed = db.execute(update(Checkout).where(Checkout.token == token, Checkout.state == "creating")
                                 .values(state="ready", payment_url=pay_url, method=method)).rowcount
            db.commit()
        if not changed:
            return RedirectResponse("/pay/" + token, status_code=303, headers=HEADERS)
    except Exception as exc:
        retryable = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 422, 429}
        with SessionLocal() as db:
            attempt = db.get(PaymentAttempt, attempt_token)
            attempt.state = "rejected" if retryable else "failed"
            db.execute(update(Checkout).where(Checkout.token == token, Checkout.state == "creating")
                       .values(state="open" if retryable else "failed"))
            db.commit()
        logger.warning("Checkout invoice creation failed (%s)", type(exc).__name__)
        return await render_page(token, request, error="Способ сейчас недоступен или сумма не подходит. Попробуйте другой способ. При списании денег не оплачивайте повторно — обратитесь в поддержку.", status=502)
    return RedirectResponse(pay_url, status_code=303, headers=HEADERS)
