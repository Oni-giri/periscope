from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.ingest import run_ingest
from periscope.web.app import create_app


def test_ingest_writes_commentary_and_media(app_config) -> None:
    source = Path(__file__).parent / "fixtures" / "agent-digest.json"
    database = Database(app_config.db_path)
    result = run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    )
    assert result.digest_written
    assert result.tweet_count == 3
    assert result.cluster_count == 1
    assert result.pick_count == 1

    digest = database.get_digest("2026-08-26")
    assert digest is not None
    pick = digest["rendered"]["picks"][0]
    assert pick["commentary"].startswith("This is the kind of AI post")
    assert pick["tweet"]["media"] == ["https://example.test/eval.png"]

    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "This is the kind of AI post" in page.text
        assert "https://example.test/eval.png" in page.text
        assert "Markets are paying for longs again" in page.text
