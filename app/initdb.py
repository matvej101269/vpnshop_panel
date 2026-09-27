"""One-shot schema and first-boot initialization for Compose deployments."""
from app.config import settings
from app.db import init_db
from app.runtime_config import init_runtime_config


if __name__ == "__main__":
    if not settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise SystemExit("Compose services require PostgreSQL; migrate the existing SQLite database before starting split services")
    init_db()
    init_runtime_config()
    print("Database schema and initial settings are ready")
