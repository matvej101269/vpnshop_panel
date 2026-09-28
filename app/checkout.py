"""One checkout and at most one payable invoice per order."""
import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import update

from app.db import Checkout, PendingPayment, SessionLocal
from app.runtime_config import get_config
from app.services import LavaClient

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)
HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
           "X-Frame-Options": "DENY", "X-Robots-Tag": "noindex, nofollow"}


def new_checkout(db, title: str, amount: int, currency: str) -> tuple[str, str]:
    origin = get_config("public_base_url").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Checkout requires a public HTTPS URL")
    if currency != "RUB" or amount <= 0:
        raise ValueError("Checkout requires a positive RUB amount")
    token = secrets.token_urlsafe(32)
    invoice_id = "checkout-" + token
    db.add(Checkout(token=token, csrf=secrets.token_urlsafe(32), invoice_id=invoice_id,
                    title=title, amount=amount, currency=currency))
    return invoice_id, origin + "/pay/" + token


def matches_payment(checkout, payload):
    try:
        amount = Decimal(str(payload.get("amount")))
        return (amount.is_finite() and amount == Decimal(checkout.amount)
                and payload.get("currency") == checkout.currency)
    except (InvalidOperation, ValueError, TypeError):
        return False


def get_checkout(db, token):
    row = db.get(Checkout, token) if len(token) <= 64 else None
    if row is None:
        raise HTTPException(404, "Заказ не найден")
    return row


@router.get("/pay/{token}")
def checkout_page(token: str, request: Request):
    with SessionLocal() as db:
        row = get_checkout(db, token)
        state = row.state
        if state != "paid" and not db.get(PendingPayment, row.invoice_id):
            state = "expired"
        if state == "open" and row.created_at < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24):
            state = "expired"
        return templates.TemplateResponse(request, "checkout.html", {
            "checkout": row, "state": state,
        }, headers=HEADERS)


@router.post("/pay/{token}")
async def choose_method(token: str, request: Request):
    form = await request.form()
    method = str(form.get("method", ""))
    if method not in {"CARD", "SBP"}:
        raise HTTPException(400, "Неизвестный способ оплаты")
    with SessionLocal() as db:
        row = get_checkout(db, token)
        if not secrets.compare_digest(row.csrf, str(form.get("csrf", ""))):
            raise HTTPException(403, "Обновите страницу оплаты")
        if row.state == "ready":
            if not db.get(PendingPayment, row.invoice_id):
                raise HTTPException(410, "Срок заказа истёк")
            return RedirectResponse(row.payment_url, status_code=303, headers=HEADERS)
        if row.state != "open":
            return RedirectResponse("/pay/" + token, status_code=303, headers=HEADERS)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
        if row.created_at < cutoff or not db.get(PendingPayment, row.invoice_id):
            raise HTTPException(410, "Срок заказа истёк. Создайте новый заказ в боте.")
        # Compare-and-set works across web workers and also in SQLite mode.
        claimed = db.execute(update(Checkout).where(
            Checkout.token == token, Checkout.state == "open"
        ).values(state="creating", method=method)).rowcount
        db.commit()
        if not claimed:
            return RedirectResponse("/pay/" + token, status_code=303, headers=HEADERS)
        old_id = row.invoice_id
        product = SimpleNamespace(amount=row.amount, currency=row.currency)
    try:
        invoice_id, pay_url = await LavaClient().create_invoice(0, product, payment_method=method)
        parsed = urlsplit(pay_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Invalid payment URL")
        with SessionLocal() as db:
            payment = db.get(PendingPayment, old_id)
            if payment is None:
                raise ValueError("Order no longer exists")
            payment.invoice_id = invoice_id
            row = db.get(Checkout, token)
            row.invoice_id, row.payment_url, row.state = invoice_id, pay_url, "ready"
            db.commit()
    except Exception as exc:
        # Never blindly retry an ambiguous timeout: Lava may have created an invoice.
        retryable = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {400, 422, 429}
        with SessionLocal() as db:
            row = db.get(Checkout, token)
            row.state = "open" if retryable else "failed"
            db.commit()
        logger.warning("Checkout invoice creation failed (%s)", type(exc).__name__)
        if retryable:
            return templates.TemplateResponse(request, "checkout.html", {
                "checkout": row, "state": "open",
                "error": "Способ оплаты сейчас недоступен. Попробуйте другой способ или повторите позже.",
            }, status_code=502, headers=HEADERS)
        return RedirectResponse("/pay/" + token, status_code=303, headers=HEADERS)
    return RedirectResponse(pay_url, status_code=303, headers=HEADERS)
