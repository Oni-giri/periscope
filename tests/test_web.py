from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.daily import run_daily
from periscope.jobs.fetchonly import run_fetch
from periscope.web.app import create_app


class NullNotifier:
    async def send_digest(self, digest, text) -> None:
        return None

    async def send_alert(self, text) -> None:
        return None


def _seed_web_state(app_config, timeline_fixture) -> Database:
    database = Database(app_config.db_path)
    asyncio.run(
        run_daily(
            app_config,
            Secrets(),
            database=database,
            notifier=NullNotifier(),
            mock_x=timeline_fixture,
            mock_llm=True,
            digest_date=date(2026, 7, 16),
            now=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        )
    )
    asyncio.run(
        run_fetch(
            app_config,
            Secrets(),
            database=database,
            notifier=NullNotifier(),
            mock_x=timeline_fixture,
            now=datetime(2026, 7, 16, 13, 0, tzinfo=UTC),
        )
    )
    return database


def test_core_web_routes_and_htmx_keep(app_config, timeline_fixture) -> None:
    database = _seed_web_state(app_config, timeline_fixture)
    app = create_app(app_config, Secrets(), database=database)

    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "Thursday, 16 July 2026" in home.text
        assert "End of digest" in home.text
        assert home.headers["x-frame-options"] == "DENY"

        dated = client.get("/digest/2026-07-16")
        assert dated.status_code == 200

        digest = database.get_digest("2026-07-16")
        cluster_id = digest["rendered"]["clusters"][0]["id"]
        detail = client.get(f"/cluster/{cluster_id}")
        assert detail.status_code == 200
        assert "Back to digest" in detail.text
        assert len(database.rows("SELECT id FROM cluster_views")) == 1

        feed = client.get("/feed")
        assert feed.status_code == 200
        assert "Fetch 2" in feed.text
        assert "@data_builder" in feed.text

        kept = client.post("/keep/1001", headers={"HX-Request": "true"})
        assert kept.status_code == 200
        assert "★" in kept.text
        assert database.is_kept("1001")

        kept_feed = client.get("/feed?kept=true")
        assert "Released a SQLite diff tool" in kept_feed.text
        assert "Hot take" not in kept_feed.text

        archive = client.get("/archive?q=SQLite")
        assert archive.status_code == 200
        assert "A searchable" not in archive.text
        assert "Released a SQLite diff tool" in archive.text

        malformed = client.get('/archive?q="')
        assert malformed.status_code == 200
        assert "search expression is not valid" in malformed.text

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["database"]["digests"] == 1

        missing = client.get("/digest/2020-01-01")
        assert missing.status_code == 404
        assert "outside the digest" in missing.text


def test_empty_web_state(app_config) -> None:
    app = create_app(app_config, Secrets())

    with TestClient(app) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert "No digest yet" in home.text

        feed = client.get("/feed")
        assert "Leftovers appear after a scrape+digest run." in feed.text

        archive = client.get("/archive")
        assert "Try a broader search" in archive.text
