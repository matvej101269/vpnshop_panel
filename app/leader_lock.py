"""PostgreSQL advisory locks for singleton roles in multi-container deployments."""
import asyncio
import hashlib
import logging

from app.db import engine

logger = logging.getLogger("vpnshop.leader_lock")


def try_lock(name: str):
    if engine.dialect.name != "postgresql":
        raise RuntimeError("Multi-instance bot and scheduler roles require PostgreSQL")
    key = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big", signed=True)
    connection = engine.raw_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (key,))
        acquired = bool(cursor.fetchone()[0])
        cursor.close()
        if acquired:
            return connection
    except Exception:
        connection.close()
        raise
    connection.close()
    return None


async def wait_for_lock(name: str):
    warned = False
    while True:
        try:
            connection = try_lock(name)
            if connection:
                logger.info("Acquired singleton role lock: %s", name)
                return connection
            if not warned:
                logger.warning("Another replica owns role %s; waiting for its lock", name)
                warned = True
        except Exception:
            logger.exception("Could not acquire role lock %s; retrying", name)
        await asyncio.sleep(10)


def release_lock(connection) -> None:
    if connection is not None:
        connection.close()  # PostgreSQL releases the session advisory lock.
