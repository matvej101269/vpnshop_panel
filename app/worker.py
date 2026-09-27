"""Payment fulfillment worker; scale with docker compose --scale vpnshop-worker=N."""
import asyncio
import logging

from app.config import settings
from app.main import process_payment_jobs


async def run():
    delay = max(0.1, settings.worker_poll_seconds)
    async def consume():
        while True:
            try:
                await process_payment_jobs()
            except Exception:
                logging.getLogger("vpnshop.worker").exception("Payment worker loop failed")
            await asyncio.sleep(delay)
    await asyncio.gather(*(consume() for _ in range(max(1, settings.worker_concurrency))))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
