from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import String, Integer, BigInteger, DateTime, Boolean, Float, Text, Index, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from app.config import settings


class Base(DeclarativeBase):
    pass


class Plan(Base):
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    days: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    traffic_limit_gb: Mapped[float] = mapped_column(Float, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    show_in_bot: Mapped[bool] = mapped_column(Boolean, default=True)
    limit_hwid: Mapped[int] = mapped_column(Integer, default=0)
    traffic_reset: Mapped[str] = mapped_column(String(16), default="never")


class BotMenuNode(Base):
    """Admin-authored Telegram menu tree; contains no subscriber data."""
    __tablename__ = "bot_menu_nodes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), default="menu")
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    routing_rules: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    same_row: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    discount_percent: Mapped[int] = mapped_column(Integer)
    plan_ids: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PromoSelection(Base):
    __tablename__ = "promo_selections"
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    promo_id: Mapped[int] = mapped_column(Integer, index=True)
    selected_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PromoPrompt(Base):
    __tablename__ = "promo_prompts"
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"
    promo_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    invoice_hash: Mapped[str] = mapped_column(String(64), unique=True)
    redeemed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ReferralAttribution(Base):
    __tablename__ = "referral_attributions"
    referred_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class ReferralReward(Base):
    __tablename__ = "referral_rewards"
    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    days: Mapped[int] = mapped_column(Integer, default=0)


class AddonPackage(Base):
    __tablename__ = "addon_packages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    traffic_gb: Mapped[float] = mapped_column(Float)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AddonBalance(Base):
    """Remaining one-time add-on traffic, tracked independently from plan quota."""
    __tablename__ = "addon_balances"
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    base_limit_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    remaining_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    consumed_cycle_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    last_usage_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    reset_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TrialClaim(Base):
    """Permanent trial-block marker; stores only the Telegram ID and claim time."""
    __tablename__ = "trial_claims"
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PendingPayment(Base):
    """Temporary mapping needed to connect a payment webhook to a Telegram account."""
    __tablename__ = "pending_payments"
    __table_args__ = (Index("uq_pending_immediate_switch_user", "telegram_id", unique=True,
                            postgresql_where=text("immediate_switch = true"),
                            sqlite_where=text("immediate_switch = 1")),)
    invoice_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), index=True)
    plan_id: Mapped[int] = mapped_column(Integer, default=0)
    product_type: Mapped[str] = mapped_column(String(16), default="plan")
    package_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_traffic_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    charged_amount: Mapped[int] = mapped_column(Integer, default=0)
    credit_amount: Mapped[int] = mapped_column(Integer, default=0)
    immediate_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    switch_days: Mapped[int] = mapped_column(Integer, default=0)
    plan_name_snapshot: Mapped[str] = mapped_column(String(100), default="")
    plan_amount_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    plan_currency_snapshot: Mapped[str] = mapped_column(String(8), default="")
    plan_days_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    plan_traffic_gb_snapshot: Mapped[float] = mapped_column(Float, default=0)
    plan_hwid_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    plan_reset_snapshot: Mapped[str] = mapped_column(String(16), default="")
    promo_code_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    promo_percent_snapshot: Mapped[int] = mapped_column(Integer, default=0)
    promo_code_snapshot: Mapped[str] = mapped_column(String(40), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("ix_subscriptions_enabled_expires", "enabled", "expires_at"),)
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sub_id: Mapped[str] = mapped_column(String(100), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    plan_name: Mapped[str] = mapped_column(String(100), default="")
    current_price: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    limit_hwid: Mapped[int] = mapped_column(Integer, default=0)
    traffic_reset: Mapped[str] = mapped_column(String(16), default="never")
    inbound_ids: Mapped[str] = mapped_column(Text, default="")
    routing_rules: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reminded: Mapped[str] = mapped_column(String(100), default="")
    sync_status: Mapped[str] = mapped_column(String(24), default="unknown")
    sync_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class FulfillmentJob(Base):
    __tablename__ = "fulfillment_jobs"
    __table_args__ = (Index("ix_fulfillment_claim", "status", "next_attempt_at", "created_at"),
                      Index("ix_fulfillment_stale_claim", "status", "claimed_at"))
    invoice_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(String(500), default="")
    notification_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    product_type: Mapped[str] = mapped_column(String(16), default="plan")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class SubscriptionHistory(Base):
    __tablename__ = "subscription_history"
    __table_args__ = (Index("ix_subscription_history_user_start", "telegram_id", "starts_at"),
                      Index("ix_subscription_history_start", "starts_at"),
                      Index("ix_subscription_history_plan", "plan_name"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), index=True)
    plan_name: Mapped[str] = mapped_column(String(100))
    plan_days: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class ProcessedPayment(Base):
    __tablename__ = "processed_payments"
    # One-way invoice digest for webhook idempotency; the original payment ID is discarded.
    payment_hash: Mapped[str] = mapped_column(String(64), primary_key=True)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class AdminAccount(Base):
    __tablename__ = "admin_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))


if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.removeprefix("sqlite:///")
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
engine_options = {"pool_pre_ping": True, "pool_recycle": 1800}
if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
else:
    engine_options.update(pool_size=max(1, settings.db_pool_size),
                          max_overflow=max(0, settings.db_max_overflow), pool_timeout=30)
engine = create_engine(settings.database_url, **engine_options)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def upgrade_postgres_schema(target_engine):
    if target_engine.dialect.name != "postgresql":
        return
    with target_engine.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote
        for table in Base.metadata.sorted_tables:
            existing = {column["name"]: column["type"]
                        for column in inspect(conn).get_columns(table.name)}
            for column in table.columns:
                if isinstance(column.type, BigInteger) and not isinstance(existing[column.name], BigInteger):
                    conn.execute(text(f"ALTER TABLE {quote(table.name)} ALTER COLUMN {quote(column.name)} TYPE BIGINT"))
    columns = inspect(target_engine).get_columns("bot_menu_nodes")
    action_type = next(column["type"] for column in columns if column["name"] == "action")
    if getattr(action_type, "length", None) is not None and action_type.length < 64:
        with target_engine.begin() as conn:
            conn.execute(text("ALTER TABLE bot_menu_nodes ALTER COLUMN action TYPE VARCHAR(64)"))


def init_db():
    legacy_subscriptions = []
    migrated_legacy_schema = False
    table_names = set(inspect(engine).get_table_names())
    if "users" in table_names and "subscriptions" in table_names and settings.database_url.startswith("sqlite"):
        columns = {column["name"] for column in inspect(engine).get_columns("subscriptions")}
        if "user_id" in columns:
            migrated_legacy_schema = True
            with engine.begin() as conn:
                legacy_subscriptions = conn.execute(text(
                    "SELECT u.telegram_id, s.sub_id, s.expires_at, s.enabled, s.reminded "
                    "FROM subscriptions s JOIN users u ON u.id = s.user_id"
                )).mappings().all()
                conn.execute(text("ALTER TABLE subscriptions RENAME TO legacy_subscriptions"))
                conn.execute(text("ALTER TABLE users RENAME TO legacy_users"))
                if "orders" in table_names:
                    conn.execute(text("DROP TABLE orders"))
                if "webhook_events" in table_names:
                    conn.execute(text("DROP TABLE webhook_events"))
    Base.metadata.create_all(engine)
    upgrade_postgres_schema(engine)
    # Small in-place SQLite schema upgrade for databases created by earlier app revisions.
    if settings.database_url.startswith("sqlite"):
        upgrades = {
            "bot_menu_nodes": {"routing_rules": "TEXT NOT NULL DEFAULT ''",
                               "same_row": "BOOLEAN NOT NULL DEFAULT 0"},
            "plans": {"traffic_limit_gb": "FLOAT NOT NULL DEFAULT 0",
                      "show_in_bot": "BOOLEAN NOT NULL DEFAULT 1",
                      "limit_hwid": "INTEGER NOT NULL DEFAULT 0",
                      "traffic_reset": "VARCHAR(16) NOT NULL DEFAULT 'never'"},
            "pending_payments": {
                "product_type": "VARCHAR(16) NOT NULL DEFAULT 'plan'",
                "package_id": "INTEGER",
                "package_traffic_bytes": "INTEGER NOT NULL DEFAULT 0",
                "charged_amount": "INTEGER NOT NULL DEFAULT 0",
                "credit_amount": "INTEGER NOT NULL DEFAULT 0",
                "immediate_switch": "BOOLEAN NOT NULL DEFAULT 0",
                "switch_days": "INTEGER NOT NULL DEFAULT 0",
                "plan_name_snapshot": "VARCHAR(100) NOT NULL DEFAULT ''",
                "plan_amount_snapshot": "INTEGER NOT NULL DEFAULT 0",
                "plan_currency_snapshot": "VARCHAR(8) NOT NULL DEFAULT ''",
                "plan_days_snapshot": "INTEGER NOT NULL DEFAULT 0",
                "plan_traffic_gb_snapshot": "FLOAT NOT NULL DEFAULT 0",
                "plan_hwid_snapshot": "INTEGER NOT NULL DEFAULT 0",
                "plan_reset_snapshot": "VARCHAR(16) NOT NULL DEFAULT ''",
                "promo_code_id": "INTEGER",
                "promo_percent_snapshot": "INTEGER NOT NULL DEFAULT 0",
                "promo_code_snapshot": "VARCHAR(40) NOT NULL DEFAULT ''",
            },
            "subscriptions": {
                "plan_id": "INTEGER",
                "plan_name": "VARCHAR(100) NOT NULL DEFAULT ''",
                "current_price": "INTEGER NOT NULL DEFAULT 0",
                "currency": "VARCHAR(8) NOT NULL DEFAULT 'RUB'",
                "traffic_limit_bytes": "INTEGER NOT NULL DEFAULT 0",
                "limit_hwid": "INTEGER NOT NULL DEFAULT 0",
                "traffic_reset": "VARCHAR(16) NOT NULL DEFAULT 'never'",
                "inbound_ids": "TEXT NOT NULL DEFAULT ''",
                "routing_rules": "TEXT NOT NULL DEFAULT ''",
                "sync_status": "VARCHAR(24) NOT NULL DEFAULT 'unknown'",
                "sync_checked_at": "DATETIME",
            },
            "subscription_history": {
                "price": "INTEGER NOT NULL DEFAULT 0",
                "currency": "VARCHAR(8) NOT NULL DEFAULT 'RUB'",
                "traffic_limit_bytes": "INTEGER NOT NULL DEFAULT 0",
            },
        }
        with engine.begin() as conn:
            for table, columns in upgrades.items():
                if table not in set(inspect(engine).get_table_names()):
                    continue
                existing = {column["name"] for column in inspect(engine).get_columns(table)}
                for name, definition in columns.items():
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            if "pending_payments" in set(inspect(engine).get_table_names()):
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pending_immediate_switch_user "
                    "ON pending_payments (telegram_id) WHERE immediate_switch = 1"
                ))
            if "bot_buttons" in set(inspect(engine).get_table_names()):
                conn.execute(text("DROP TABLE bot_buttons"))
    with SessionLocal() as db:
        for row in legacy_subscriptions:
            expires = row["expires_at"]
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires)
            db.add(Subscription(telegram_id=row["telegram_id"], sub_id=row["sub_id"], expires_at=expires,
                                enabled=bool(row["enabled"]), reminded=row["reminded"] or ""))
        if migrated_legacy_schema:
            db.commit()
            with engine.begin() as conn:
                conn.execute(text("DROP TABLE legacy_subscriptions"))
                conn.execute(text("DROP TABLE legacy_users"))
        if not db.query(Plan).first():
            db.add_all([Plan(name="1 месяц", days=30, amount=300), Plan(name="3 месяца", days=90, amount=800)])
            db.commit()
        # Attach older subscriptions to the matching current plan when possible.
        for plan in db.query(Plan).all():
            db.query(Subscription).filter(
                Subscription.plan_id.is_(None), Subscription.plan_name == plan.name
            ).update({Subscription.plan_id: plan.id}, synchronize_session=False)
        db.commit()
    for table in Base.metadata.tables.values():
        for index in table.indexes:
            index.create(engine, checkfirst=True)
