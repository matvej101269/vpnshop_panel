"""Shared currency and period rules for bot orders and admin forms."""
from types import SimpleNamespace
from sqlalchemy import select
from app.db import Plan, PlanPeriod, UserCurrency

CURRENCIES = ("RUB", "USD", "EUR")
MONTHS = (1, 3, 6)


def selected_currency(db, telegram_id):
    row = db.get(UserCurrency, telegram_id)
    return row.currency if row and row.currency in CURRENCIES else None


def visible_plans(db, currency):
    if currency not in CURRENCIES:
        return []
    return db.scalars(select(Plan).where(Plan.currency == currency,
        Plan.enabled.is_(True), Plan.show_in_bot.is_(True)).order_by(Plan.id)).all()


def periods_for(db, plan_id):
    return db.scalars(select(PlanPeriod).where(PlanPeriod.plan_id == plan_id,
        PlanPeriod.enabled.is_(True)).order_by(PlanPeriod.days, PlanPeriod.id)).all()


def period_label(period):
    return {1: "1 месяц", 3: "3 месяца", 6: "6 месяцев"}.get(period.months, f"{period.days} дн.")


def period_plan(plan, period):
    if not period or period.plan_id != plan.id or not period.enabled:
        raise ValueError("Период недоступен для тарифа")
    fields = {column.name: getattr(plan, column.name) for column in Plan.__table__.columns}
    fields.update(days=period.days, amount=period.amount)
    return SimpleNamespace(**fields)


def parse_periods(form):
    result = []
    for months in MONTHS:
        if form.get(f"period_{months}_enabled") == "on":
            try:
                amount = int(form.get(f"period_{months}_amount", ""))
            except (ValueError, TypeError):
                raise ValueError("Укажите целую положительную цену каждого включённого периода")
            if not 1 <= amount <= 1000000:
                raise ValueError("Цена периода должна быть от 1 до 1000000")
            result.append((months, months * 30, amount))
    if form.get("period_0_enabled") == "on":
        try:
            days, amount = int(form.get("period_0_days", "")), int(form.get("period_0_amount", ""))
        except (ValueError, TypeError):
            raise ValueError("Некорректный сохранённый период")
        if days < 1 or not 1 <= amount <= 1000000:
            raise ValueError("Некорректный сохранённый период")
        result.append((0, days, amount))
    if not result:
        raise ValueError("Включите хотя бы один период")
    return result


def save_periods(db, plan, values):
    existing = {p.months: p for p in db.scalars(select(PlanPeriod).where(PlanPeriod.plan_id == plan.id))}
    for p in existing.values():
        p.enabled = False
    for months, days, amount in values:
        period = existing.get(months)
        if not period:
            period = PlanPeriod(plan_id=plan.id, months=months)
            db.add(period)
        period.days, period.amount, period.enabled = days, amount, True
    # Legacy integrations/manual displays use the shortest enabled period.
    first = min(values, key=lambda value: value[1])
    plan.days, plan.amount = first[1], first[2]
