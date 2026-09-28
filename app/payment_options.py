"""Lava v3 combinations documented in /docs/documentation.yaml (2026-09-28).

The schema spells the provider UNLIMINT; examples also use UNLIMIT.
Use the schema enum. Never retry an ambiguous invoice creation with another spelling.
"""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone, timedelta
import time
import xml.etree.ElementTree as ET

import httpx

METHODS = {
    "RUB": {"SBP": ("СБП", "PAY2ME"), "CARD": ("Карта РФ", "SMART_GLOCAL"),
            "CARD_PAY2ME": ("Карта РФ — альтернативный способ", "PAY2ME")},
    "USD": {"CARD": ("Зарубежная карта", "UNLIMINT"), "PAYPAL": ("PayPal", "PAYPAL"),
            "APPLE_PAY": ("Apple Pay", "UNLIMINT"), "PIX": ("PIX · Бразилия", "UNLIMINT")},
    "EUR": {"CARD": ("Зарубежная карта", "UNLIMINT"), "PAYPAL": ("PayPal", "PAYPAL"),
            "APPLE_PAY": ("Apple Pay", "UNLIMINT"), "PIX": ("PIX · Бразилия", "UNLIMINT"),
            "SEPATRANSFER": ("SEPA · банковский перевод", "UNLIMINT"),
            "IDEAL": ("iDEAL · Нидерланды", "UNLIMINT"), "BIZUM": ("Bizum · Испания", "UNLIMINT"),
            "MBWAY": ("MB WAY · Португалия", "UNLIMINT"), "BANCONTACT": ("Bancontact · Бельгия", "UNLIMINT")},
}
_cached = None


def amount_limit_error(amount, currency):
    # https://faq.lava.top/article/83555 — custom-price product limits.
    minimum, maximum = (Decimal(50), Decimal(1000000)) if currency == "RUB" else (Decimal(5), Decimal(10000))
    if Decimal(amount) < minimum:
        return f"Минимальная сумма оплаты через Lava.top — {minimum} {currency}. Выберите другую валюту или тариф с большей стоимостью."
    if Decimal(amount) > maximum:
        return f"Максимальная сумма оплаты через Lava.top — {maximum} {currency}. Выберите другой тариф."
    return ""


def parse_rates(content):
    root = ET.fromstring(content)
    day = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    today = datetime.now(timezone.utc).date()
    if day < today - timedelta(days=7) or day > today + timedelta(days=1):
        raise ValueError("Stale exchange rates")
    rates = {"RUB": Decimal(1)}
    for item in root.findall("Valute"):
        code = item.findtext("CharCode")
        if code in {"USD", "EUR"}:
            rate = Decimal(item.findtext("Value").replace(",", ".")) / Decimal(item.findtext("Nominal"))
            if not rate.is_finite() or rate <= 0:
                raise ValueError("Invalid exchange rate")
            rates[code] = rate
    if len(rates) != 3:
        raise ValueError("Missing exchange rates")
    return rates, day.isoformat()


async def exchange_rates():
    global _cached
    if _cached and time.monotonic() - _cached[0] < 3600:
        return _cached[1]
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.get("https://www.cbr.ru/scripts/XML_daily.asp")
        response.raise_for_status()
    result = parse_rates(response.content)
    _cached = (time.monotonic(), result)
    return result


def convert_amount(amount, source, target, rates):
    rate = rates[source] / rates[target]
    result = (Decimal(amount) * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if result <= 0:
        raise ValueError("Amount too small")
    return result, rate
