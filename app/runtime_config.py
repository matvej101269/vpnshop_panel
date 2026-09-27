"""Encrypted admin-managed configuration; secret values never leave this module."""
import base64
import binascii
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from app.config import settings as env_settings
from app.db import SessionLocal, AppSetting, AdminAccount


CONFIG_DEFAULTS = {
    "bot_token": env_settings.bot_token,
    "public_base_url": env_settings.public_base_url,
    "panel_scheme": "http",
    "panel_domain": "localhost",
    "panel_port": "8000",
    "panel_uri_path": "admin",
    "lava_api_key": env_settings.lava_api_key,
    "lava_api_url": "https://gate.lava.top",
    "lava_invoice_path": "/api/v3/invoice",
    "lava_payment_provider": "",
    "lava_offer_id": env_settings.lava_offer_id,
    "lava_webhook_key": env_settings.lava_webhook_key,
    "xui_base_url": env_settings.xui_base_url,
    "xui_api_base_path": "/panel/api",
    "xui_login_path": "/login",
    "xui_username": env_settings.xui_username,
    "xui_password": env_settings.xui_password,
    "xui_api_token": env_settings.xui_api_token,
    "xui_inbound_id": str(env_settings.xui_inbound_id),
    "xui_inbound_ids": str(env_settings.xui_inbound_id),
    "happ_subscription_base": env_settings.happ_subscription_base,
    "reminder_days": env_settings.reminder_days,
    "timezone": env_settings.timezone,
    "bot_welcome_text": "Выберите период VPN-подписки:",
    "offer_text": "Укажите здесь текст договора оферты.",
    "referral_enabled": "false",
}
SECRET_KEYS = {"bot_token", "lava_api_key", "lava_offer_id", "lava_webhook_key", "xui_username", "xui_password", "xui_api_token"}
DEFAULT_LABELS = {
    "bot_token": "Telegram Bot API token", "lava_api_key": "Lava.top API key",
    "lava_offer_id": "Lava.top offer ID", "lava_webhook_key": "Lava.top webhook key",
    "xui_username": "3x-ui username", "xui_password": "3x-ui password",
    "xui_api_token": "3x-ui API token",
}


def _key_path() -> Path:
    if env_settings.database_url.startswith("sqlite"):
        db_path = Path(env_settings.database_url.removeprefix("sqlite:///"))
        return db_path.parent / ".vpnshop-secret.key"
    return Path("data/.vpnshop-secret.key")


def _fernet() -> Fernet:
    path = _key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        key = Fernet.generate_key()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(key)
        except FileExistsError:
            pass
    try:
        return Fernet(path.read_bytes().strip())
    except (ValueError, OSError) as exc:
        raise RuntimeError("Cannot read the app secret-encryption key") from exc


def encrypt_handoff(value: str) -> str:
    """Encrypt a short-lived Happ deep link so it never appears in proxy URLs/logs."""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii").rstrip("=")


def decrypt_handoff(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    try:
        return _fernet().decrypt(padded.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Invalid or expired Happ handoff") from exc


def make_csrf_token(username: str) -> str:
    key = base64.urlsafe_b64decode(_key_path().read_bytes().strip())
    timestamp = str(int(time.time()))
    message = f"{username}:{timestamp}".encode()
    signature = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"{timestamp}.{signature}"


def verify_csrf_token(username: str, token: str) -> bool:
    try:
        timestamp, signature = token.split(".", 1)
        if abs(int(time.time()) - int(timestamp)) > 3600:
            return False
        key = base64.urlsafe_b64decode(_key_path().read_bytes().strip())
        expected = hmac.new(key, f"{username}:{timestamp}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, OSError):
        return False


def create_admin_session(username: str, lifetime_seconds: int = 12 * 60 * 60) -> str:
    expires = int(time.time()) + lifetime_seconds
    payload = f"{username}\n{expires}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    key = base64.urlsafe_b64decode(_key_path().read_bytes().strip())
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_admin_session(token: str) -> str | None:
    try:
        encoded, signature = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        key = base64.urlsafe_b64decode(_key_path().read_bytes().strip())
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        username, expires = payload.decode().rsplit("\n", 1)
        if int(expires) < int(time.time()):
            return None
        return username or None
    except (ValueError, UnicodeDecodeError, OSError, binascii.Error):
        return None


def _encode(key: str, value: str) -> str:
    return _fernet().encrypt(value.encode()).decode() if key in SECRET_KEYS else value


def _decode(key: str, value: str, is_secret: bool) -> str:
    if not is_secret:
        return value
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Secret config cannot be decrypted; restore the app key file") from exc


def get_config(key: str) -> str:
    with SessionLocal() as db:
        item = db.get(AppSetting, key)
        if item:
            return _decode(key, item.value, item.is_secret)
    return str(CONFIG_DEFAULTS.get(key, ""))


def get_config_map() -> dict[str, str]:
    result = dict(CONFIG_DEFAULTS)
    with SessionLocal() as db:
        for item in db.scalars(select(AppSetting)).all():
            result[item.key] = _decode(item.key, item.value, item.is_secret)
    return result


def save_config(values: dict[str, str], labels: dict[str, str] | None = None):
    labels = labels or {}
    with SessionLocal() as db:
        for key in set(values) | set(labels):
            value = values.get(key, "")
            item = db.get(AppSetting, key)
            if item is None:
                item = AppSetting(key=key, value="", is_secret=key in SECRET_KEYS, label=DEFAULT_LABELS.get(key, key))
                db.add(item)
            if key in values and (key not in SECRET_KEYS or value):
                item.value = _encode(key, value)
            item.is_secret = key in SECRET_KEYS
            if labels.get(key, "").strip():
                item.label = labels[key].strip()[:120]
        db.commit()


def config_status() -> dict[str, dict[str, str | bool]]:
    with SessionLocal() as db:
        stored = {item.key: item for item in db.scalars(select(AppSetting)).all()}
    result = {}
    for key in SECRET_KEYS:
        item = stored.get(key)
        result[key] = {"configured": bool(item and item.value), "label": item.label if item else DEFAULT_LABELS[key]}
    return result


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, salt, expected = encoded.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt), 310_000)
        return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode(), expected)
    except (ValueError, TypeError):
        return False


def verify_admin(username: str, password: str) -> bool:
    with SessionLocal() as db:
        account = db.scalar(select(AdminAccount).where(AdminAccount.username == username))
        if account:
            return _password_matches(password, account.password_hash)
    return hmac.compare_digest(username, env_settings.admin_user) and hmac.compare_digest(password, env_settings.admin_password)


def save_admin_account(username: str, password: str = ""):
    with SessionLocal() as db:
        accounts = db.scalars(select(AdminAccount)).all()
        if accounts:
            account = accounts[0]
            account.username = username
            if password:
                account.password_hash = _password_hash(password)
            for extra in accounts[1:]:
                db.delete(extra)
        else:
            if not password:
                raise ValueError("A password is required for the first administrator account")
            db.add(AdminAccount(username=username, password_hash=_password_hash(password)))
        db.commit()


def init_runtime_config():
    # Import environment values only at first boot; subsequent edits live in the admin DB.
    with SessionLocal() as db:
        initial_values = dict(CONFIG_DEFAULTS)
        # Preserve an already configured public origin when adding the panel address fields.
        public_setting = db.get(AppSetting, "public_base_url")
        public_origin = public_setting.value if public_setting and public_setting.value else env_settings.public_base_url
        parsed = urlsplit(public_origin)
        if parsed.hostname:
            initial_values.update(
                panel_scheme=parsed.scheme if parsed.scheme in {"http", "https"} else "https",
                panel_domain=parsed.hostname,
                panel_port=str(parsed.port or (443 if parsed.scheme == "https" else 80)),
            )
        for key, value in CONFIG_DEFAULTS.items():
            if db.get(AppSetting, key) is None:
                value = initial_values.get(key, value)
                db.add(AppSetting(key=key, value=_encode(key, str(value)), is_secret=key in SECRET_KEYS,
                                  label=DEFAULT_LABELS.get(key, key)))
        if not db.scalars(select(AdminAccount)).first():
            db.add(AdminAccount(username=env_settings.admin_user, password_hash=_password_hash(env_settings.admin_password)))
        db.commit()
