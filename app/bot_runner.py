"""Dedicated single-instance Telegram polling process."""
import asyncio
import logging

from app.bot import start_bot
from app.leader_lock import release_lock, wait_for_lock
from app.runtime_config import get_config

logger = logging.getLogger("vpnshop.bot_runner")


async def run():
    role_lock = await wait_for_lock("vpnshop:telegram-polling")
    current_token = ""
    polling_task = None
    try:
        while True:
            try:
                token = get_config("bot_token")
            except Exception:
                logger.exception("Cannot read Telegram configuration; retrying")
                await asyncio.sleep(5)
                continue
            if token != current_token:
                if polling_task:
                    polling_task.cancel()
                    await asyncio.gather(polling_task, return_exceptions=True)
                    polling_task = None
                current_token = token
                if token:
                    polling_task = asyncio.create_task(start_bot(token))
                    logger.info("Telegram polling task started")
            if polling_task and polling_task.done():
                await asyncio.gather(polling_task, return_exceptions=True)
                polling_task = None
                current_token = ""
            await asyncio.sleep(5)
    finally:
        if polling_task:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        release_lock(role_lock)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
