from pathlib import Path
from datetime import datetime, timezone
from sqlalchemy import String, Integer, DateTime, Boolean, Float, Text, create_engine, inspect, text
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
    action: Mapped[str] = mapped_column(String(16), default="menu")
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    routing_rules: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AddonPackage(Base):
    __tablename__ = "addon_packages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    traffic_gb: Mapped[float] = mapped_column(Float)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class PendingPayment(Base):
    """Temporary mapping needed to connect a payment webhook to a Telegram account."""
    __tablename__ = "pending_payments"
    invoice_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    plan_id: Mapped[int] = mapped_column(Integer, default=0)
    product_type: Mapped[str] = mapped_column(String(16), default="plan")
    package_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    package_traffic_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Subscription(Base):
    __tablename__ = "subscriptions"
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    sub_id: Mapped[str] = mapped_column(String(100), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    plan_name: Mapped[str] = mapped_column(String(100), default="")
    current_price: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    traffic_limit_bytes: Mapped[int] = mapped_column(Integer, default=0)
    limit_hwid: Mapped[int] = mapped_column(Integer, default=0)
    traffic_reset: Mapped[str] = mapped_column(String(16), default="never")
    inbound_ids: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reminded: Mapped[str] = mapped_column(String(100), default="")


class SubscriptionHistory(Base):
    __tablename__ = "subscription_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    plan_name: Mapped[str] = mapped_column(String(100))
    plan_days: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    traffic_limit_bytes: Mapped[int] = mapped_column(Integer, default=0)
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
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {})
SessionLocal = sessionmaker(engine, expire_on_commit=False)


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
    # Small in-place SQLite schema upgrade for databases created by earlier app revisions.
    if settings.database_url.startswith("sqlite"):
        upgrades = {
            "bot_menu_nodes": {"routing_rules": "TEXT NOT NULL DEFAULT ''"},
            "plans": {"traffic_limit_gb": "FLOAT NOT NULL DEFAULT 0",
                      "show_in_bot": "BOOLEAN NOT NULL DEFAULT 1",
                      "limit_hwid": "INTEGER NOT NULL DEFAULT 0",
                      "traffic_reset": "VARCHAR(16) NOT NULL DEFAULT 'never'"},
            "pending_payments": {
                "product_type": "VARCHAR(16) NOT NULL DEFAULT 'plan'",
                "package_id": "INTEGER",
                "package_traffic_bytes": "INTEGER NOT NULL DEFAULT 0",
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
