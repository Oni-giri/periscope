from __future__ import annotations

import asyncio

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.fetchonly import run_fetch
from periscope.xclient import CookieDeadError


class DeadClient:
    async def timeline(self, *, limit):
        raise CookieDeadError("expired")
        yield

    async def thread(self, tweet, *, depth):
        yield


class RecordingNotifier:
    def __init__(self) -> None:
        self.alerts = []

    async def send_digest(self, digest, text) -> None:
        return None

    async def send_alert(self, text) -> None:
        self.alerts.append(text)


def test_cookie_death_alerts_only_once(app_config) -> None:
    database = Database(app_config.db_path)
    notifier = RecordingNotifier()

    first = asyncio.run(
        run_fetch(
            app_config,
            Secrets(),
            database=database,
            xclient=DeadClient(),
            notifier=notifier,
        )
    )
    second = asyncio.run(
        run_fetch(
            app_config,
            Secrets(),
            database=database,
            xclient=DeadClient(),
            notifier=notifier,
        )
    )

    assert first.errors == second.errors == 1
    assert len(notifier.alerts) == 1
    assert database.count("fetch_log") == 2
