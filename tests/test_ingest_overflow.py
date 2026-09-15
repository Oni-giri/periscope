from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.ingest import run_ingest
from periscope.web.app import create_app
from periscope.x_scrape.ingest_overflow import ingest_overflow


def _dump(path: Path, posts: list[dict]) -> None:
    path.write_text(json.dumps(posts), encoding="utf-8")


def test_overflow_feed_shows_leftover_not_empty_state(app_config, tmp_path: Path) -> None:
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    _dump(
        dumps / "foryou.json",
        [
            {
                "status_id": "1959000000000000001",
                "author_handle": "@karpathy",
                "text": "A small eval on long-context retrieval",
                "tweet_url": "https://x.com/karpathy/status/1959000000000000001",
                "created_at": "2026-08-26T08:12:00+00:00",
                "image_urls": [],
                "is_ad": False,
            },
            {
                "status_id": "88",
                "author_handle": "@leftover_alice",
                "text": "did not make today",
                "tweet_url": "https://x.com/leftover_alice/status/88",
                "created_at": "2026-08-26T09:00:00+00:00",
                "image_urls": [],
                "is_ad": False,
            },
            {
                "status_id": "99",
                "author_handle": "@adbot",
                "text": "buy now",
                "tweet_url": "https://x.com/adbot/status/99",
                "created_at": "2026-08-26T09:01:00+00:00",
                "image_urls": [],
                "is_ad": True,
            },
        ],
    )
    _dump(
        dumps / "following.json",
        [
            {
                "status_id": "88",
                "author_handle": "@leftover_alice",
                "text": "did not make today",
                "tweet_url": "https://x.com/leftover_alice/status/88",
                "created_at": "2026-08-26T09:00:00+00:00",
                "image_urls": [],
                "is_ad": False,
            }
        ],
    )
    _dump(
        dumps / "timeline_tech.json",
        [
            {
                "status_id": "77",
                "author_handle": "@tech_bob",
                "text": "from the tech timeline",
                "tweet_url": "https://x.com/tech_bob/status/77",
                "created_at": "2026-08-26T09:02:00+00:00",
                "image_urls": ["https://example.test/tech.png"],
                "is_ad": False,
            }
        ],
    )

    source = Path(__file__).parent / "fixtures" / "agent-digest.json"
    database = Database(app_config.db_path)
    run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    )
    result = ingest_overflow(
        database,
        dumps_dir=dumps,
        digest_path=source,
        now=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    )
    assert result.leftover_count == 2
    assert result.skipped_ads == 1
    assert result.skipped_digest == 1
    assert result.fetch_id is not None

    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/feed")
        assert page.status_code == 200
        assert "@leftover_alice" in page.text
        assert "Nothing in this window" not in page.text
        assert "@adbot" not in page.text
        kept = client.post("/keep/88", headers={"HX-Request": "true"})
        assert kept.status_code == 200
        assert "★" in kept.text
        assert database.is_kept("88")

def test_reingest_preserves_assembled_at_keeps_feed(app_config, tmp_path: Path) -> None:
    """Same-day UX re-ingest must not move assembled_at past overflow scrapes."""
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    _dump(
        dumps / "foryou.json",
        [
            {
                "status_id": "88",
                "author_handle": "@leftover_alice",
                "text": "did not make today",
                "tweet_url": "https://x.com/leftover_alice/status/88",
                "created_at": "2026-08-26T09:00:00+00:00",
                "image_urls": [],
                "is_ad": False,
            }
        ],
    )
    _dump(dumps / "following.json", [])
    _dump(dumps / "timeline_tech.json", [])

    source = Path(__file__).parent / "fixtures" / "agent-digest.json"
    database = Database(app_config.db_path)
    morning = datetime(2026, 8, 26, 4, 31, 34, tzinfo=UTC)
    run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=morning,
    )
    first = database.get_digest("2026-08-26")
    assert first is not None
    assert first["assembled_at"] == "2026-08-26T04:31:34+00:00"
    assert first["rendered"]["assembled_at"] == "2026-08-26T04:31:34+00:00"

    overflow = ingest_overflow(
        database,
        dumps_dir=dumps,
        digest_path=source,
        now=datetime(2026, 8, 26, 4, 35, 56, tzinfo=UTC),
    )
    assert overflow.fetch_id is not None
    assert overflow.leftover_count >= 1
    batches_before = database.feed_batches()
    assert any(batch["id"] == overflow.fetch_id for batch in batches_before)

    reingest_doc = json.loads(source.read_text(encoding="utf-8"))
    reingest_doc["highlights"] = "Re-ingest highlight tweak for UX."
    revised = tmp_path / "digest-reingest.json"
    revised.write_text(json.dumps(reingest_doc), encoding="utf-8")
    run_ingest(
        app_config,
        Secrets(),
        source=revised,
        database=database,
        now=datetime(2026, 8, 26, 11, 59, 48, tzinfo=UTC),
    )

    after = database.get_digest("2026-08-26")
    assert after is not None
    assert after["assembled_at"] == "2026-08-26T04:31:34+00:00"
    assert after["rendered"]["assembled_at"] == "2026-08-26T04:31:34+00:00"
    assert after["rendered"]["highlights"] == "Re-ingest highlight tweak for UX."

    batches_after = database.feed_batches()
    assert any(batch["id"] == overflow.fetch_id for batch in batches_after)
    assert any(
        tweet["id"] == "88"
        for batch in batches_after
        for tweet in batch["tweets"]
    )

    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/feed")
        assert page.status_code == 200
        assert "@leftover_alice" in page.text
        assert "Nothing in this window" not in page.text
        assert "No leftover posts yet" not in page.text

