"""Single-process job orchestration shared by APScheduler and web controls."""

from __future__ import annotations

import asyncio
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from periscope.jobs.daily import run_daily
from periscope.jobs.fetchonly import run_fetch
from periscope.jobs.weekly import run_weekly
from periscope.runtime import effective_config
from periscope.telegram.bot import build_notifier


class JobRunner:
    """Own in-process task lifetimes and prevent duplicate named runs."""

    def __init__(self, app: Any):
        self.app = app
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self._pipeline_lock = asyncio.Lock()

    async def run(self, name: str) -> Any:
        async with self._pipeline_lock:
            return await self._run(name)

    async def _run(self, name: str) -> Any:
        database = self.app.state.database
        config = effective_config(self.app.state.config, database)
        secrets = self.app.state.secrets
        notifier = build_notifier(secrets, config.delivery)
        client = self.app.state.xclient
        database.record_event("job_started", {"job": name})
        try:
            if name == "fetch":
                result = await run_fetch(
                    config,
                    secrets,
                    database=database,
                    xclient=client,
                    notifier=notifier,
                )
            elif name in {"daily", "rebuild"}:
                result = await run_daily(
                    config,
                    secrets,
                    database=database,
                    xclient=client,
                    notifier=notifier,
                    skip_fetch=name == "rebuild",
                )
            elif name == "weekly":
                result = await run_weekly(
                    config,
                    secrets,
                    database=database,
                    xclient=client,
                    notifier=notifier,
                )
            else:
                raise ValueError(f"Unknown job: {name}")
        except Exception as exc:
            database.record_job_failure(name, str(exc))
            raise
        database.record_event("job_completed", {"job": name})
        return result

    def launch(self, name: str) -> bool:
        active = self.tasks.get(name)
        if active is not None and not active.done():
            return False

        async def execute() -> None:
            try:
                await self.run(name)
            except Exception:
                # The durable failure event is the operator-facing signal.
                return

        task = asyncio.create_task(execute(), name=f"periscope-{name}")
        self.tasks[name] = task
        task.add_done_callback(lambda finished: self.tasks.pop(name, None))
        return True

    async def close(self) -> None:
        active = [task for task in self.tasks.values() if not task.done()]
        if not active:
            return
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


def configure_scheduler(
    scheduler: AsyncIOScheduler,
    runner: JobRunner,
) -> None:
    """Replace all schedules from the current effective runtime settings."""

    config = effective_config(runner.app.state.config, runner.app.state.database)
    scheduler.remove_all_jobs()
    scheduler.add_job(
        runner.run,
        trigger=IntervalTrigger(
            minutes=config.schedule.feed_interval_minutes,
            timezone=config.schedule.timezone,
        ),
        args=("fetch",),
        id="fetch",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    for index, value in enumerate(config.schedule.daily_times):
        hour_text, minute_text = value.split(":", 1)
        scheduler.add_job(
            runner.run,
            trigger=CronTrigger(
                hour=int(hour_text),
                minute=int(minute_text),
                timezone=config.schedule.timezone,
            ),
            args=("daily",),
            id=f"daily-{index}",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=1800,
        )
    weekly_hour, weekly_minute = config.schedule.weekly_time.split(":", 1)
    scheduler.add_job(
        runner.run,
        trigger=CronTrigger(
            day_of_week=config.schedule.weekly_day,
            hour=int(weekly_hour),
            minute=int(weekly_minute),
            timezone=config.schedule.timezone,
        ),
        args=("weekly",),
        id="weekly",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )


def scheduled_jobs(scheduler: AsyncIOScheduler | None) -> list[dict[str, Any]]:
    if scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in sorted(scheduler.get_jobs(), key=lambda item: item.id)
    ]
