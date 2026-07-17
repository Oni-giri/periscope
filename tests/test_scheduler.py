from __future__ import annotations

import asyncio
from types import SimpleNamespace

from periscope.config import Secrets
from periscope.db import Database
from periscope.scheduler import JobRunner


def test_daily_fetches_but_manual_rebuild_reuses_raw_rows(
    app_config,
    monkeypatch,
) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    app = SimpleNamespace(
        state=SimpleNamespace(
            database=database,
            config=app_config,
            secrets=Secrets(),
            xclient=None,
        )
    )
    calls: list[bool] = []

    async def fake_daily(config, secrets, **kwargs):
        calls.append(bool(kwargs["skip_fetch"]))
        return {"ok": True}

    monkeypatch.setattr("periscope.scheduler.run_daily", fake_daily)
    runner = JobRunner(app)

    asyncio.run(runner.run("daily"))
    asyncio.run(runner.run("rebuild"))

    assert calls == [False, True]
