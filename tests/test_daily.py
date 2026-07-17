from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.daily import run_daily


class RecordingNotifier:
    def __init__(self) -> None:
        self.digests = []
        self.alerts = []

    async def send_digest(self, digest, text) -> None:
        self.digests.append((digest, text))

    async def send_alert(self, text) -> None:
        self.alerts.append(text)


def test_mock_daily_pipeline_is_idempotent(app_config, timeline_fixture) -> None:
    database = Database(app_config.db_path)
    notifier = RecordingNotifier()
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    first = asyncio.run(
        run_daily(
            app_config,
            Secrets(),
            database=database,
            notifier=notifier,
            mock_x=timeline_fixture,
            mock_llm=True,
            digest_date=date(2026, 7, 16),
            now=now,
        )
    )
    second = asyncio.run(
        run_daily(
            app_config,
            Secrets(),
            database=database,
            notifier=notifier,
            mock_x=timeline_fixture,
            mock_llm=True,
            digest_date=date(2026, 7, 16),
            now=now,
        )
    )

    assert first.digest_written and first.errors == 0
    assert first.fetch is not None and first.fetch.new_items == 8
    assert first.tweet_count == 8
    assert first.cluster_count == 3
    assert first.pick_count == 3
    assert second.fetch is not None and second.fetch.new_items == 0
    assert database.count("tweets") == 8
    assert database.count("digests") == 1
    assert database.count("clusters") == 3
    assert database.count("picks") == 3
    assert database.count("pick_decisions") == 8

    digest = database.get_digest("2026-07-16")
    assert digest is not None
    assert digest["stats"]["items"] == 8
    assert len(notifier.digests) == 2
    assert not notifier.alerts
    assert "Periscope · 16 Jul 2026" in notifier.digests[0][1]
