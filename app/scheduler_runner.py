"""Single scheduler process for maintenance jobs shared by all web/worker replicas."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings
from app.backups import create_backup
from app.main import purge_old_pending_payments, reconcile_subscriptions, send_reminders
from app.services import reconcile_addon_balances
from app.leader_lock import release_lock, wait_for_lock


async def run():
    role_lock = await wait_for_lock("vpnshop:maintenance-scheduler")
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(send_reminders, "interval", hours=6, id="reminders", replace_existing=True)
    scheduler.add_job(purge_old_pending_payments, "interval", hours=6, id="pending-retention", replace_existing=True)
    scheduler.add_job(reconcile_addon_balances, "interval", seconds=60, id="traffic-addons",
                      replace_existing=True, max_instances=1, coalesce=True)
    scheduler.add_job(create_backup, "interval", hours=24, id="daily-backup", replace_existing=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
    scheduler.add_job(reconcile_subscriptions, "interval", hours=24, id="3xui-reconciliation", replace_existing=True,
                      next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2))
    scheduler.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        scheduler.shutdown(wait=False)
        release_lock(role_lock)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())
