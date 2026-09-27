"""Copy an existing SQLite installation into a clean PostgreSQL database.

The source is never modified. Set TARGET_DATABASE_URL and pass --clear-target
only after taking a backup and verifying the target connection.
"""
import argparse
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db import Base, engine as configured_engine, init_db


def migrate(source_path: Path, target_url: str, clear_target: bool) -> None:
    if not clear_target:
        raise SystemExit("Refusing to write: pass --clear-target for a verified empty target database")
    if not source_path.is_file():
        raise SystemExit(f"SQLite source does not exist: {source_path}")
    if configured_engine.dialect.name != "sqlite" or Path(configured_engine.url.database).resolve() != source_path.resolve():
        raise SystemExit("Run with DATABASE_URL pointing to the same SQLite source so its schema can be upgraded safely")
    init_db()
    source = create_engine("sqlite:///" + source_path.resolve().as_posix())
    target = create_engine(target_url, pool_pre_ping=True)
    if target.dialect.name != "postgresql":
        raise SystemExit("TARGET_DATABASE_URL must point to PostgreSQL")
    Base.metadata.create_all(target)
    source_tables = set(inspect(source).get_table_names())
    tables = [table for table in Base.metadata.sorted_tables if table.name in source_tables]

    with target.begin() as connection:
        if tables:
            quoted = ", ".join(connection.dialect.identifier_preparer.quote(table.name)
                                for table in tables)
            connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
        with source.connect() as source_connection:
            for table in tables:
                result = source_connection.execution_options(stream_results=True).execute(table.select())
                while rows := result.fetchmany(1000):
                    connection.execute(table.insert(), [dict(row._mapping) for row in rows])
        for table in tables:
            for column in table.columns:
                if not column.primary_key or not column.autoincrement:
                    continue
                sequence = connection.execute(text(
                    "SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                    {"table_name": table.name, "column_name": column.name}).scalar_one_or_none()
                if sequence:
                    connection.execute(text(
                        "SELECT setval(CAST(:sequence AS regclass), COALESCE((SELECT MAX(" +
                        connection.dialect.identifier_preparer.quote(column.name) + ") FROM " +
                        connection.dialect.identifier_preparer.quote(table.name) +
                        "), 1), EXISTS(SELECT 1 FROM " +
                        connection.dialect.identifier_preparer.quote(table.name) + "))"),
                        {"sequence": sequence})
    source.dispose()
    target.dispose()
    print(f"Copied {len(tables)} application tables from {source_path.name} to PostgreSQL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Path to the SQLite database")
    parser.add_argument("--clear-target", action="store_true", help="Replace all data in target PostgreSQL tables")
    args = parser.parse_args()
    import os
    target = os.environ.get("TARGET_DATABASE_URL", "")
    if not target:
        raise SystemExit("Set TARGET_DATABASE_URL in the process environment")
    migrate(args.source, target, args.clear_target)
