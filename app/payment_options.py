"""Lava v3 combinations documented in /docs/documentation.yaml (2026-09-28).

The schema spells the provider UNLIMINT; examples also use UNLIMIT.
Use the schema enum. Never retry an ambiguous invoice creation with another spelling.
"""
from decimal import Decimal

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


def amount_limit_error(amount, currency):
    # https://faq.lava.top/article/83555 — custom-price product limits.
    minimum, maximum = (Decimal(50), Decimal(1000000)) if currency == "RUB" else (Decimal(5), Decimal(10000))
    if Decimal(amount) < minimum:
        return f"Минимальная сумма оплаты через Lava.top — {minimum} {currency}. Вернитесь в бот и выберите другой период, тариф или валюту."
    if Decimal(amount) > maximum:
        return f"Максимальная сумма оплаты через Lava.top — {maximum} {currency}. Выберите другой тариф."
    return ""
