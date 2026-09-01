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
    assert pick["tweet"]["media"] == [
        "https://example.test/eval.png",
        "https://example.test/eval-chart.webp",
    ]

    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "This is the kind of AI post" in page.text
        assert "https://example.test/eval.png" in page.text
        assert "Markets are paying for longs again" in page.text


def test_today_renders_open_on_x_and_media_gallery(app_config) -> None:
    source = Path(__file__).parent / "fixtures" / "agent-digest.json"
    database = Database(app_config.db_path)
    run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    )
    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Open on X" in page.text
        assert "https://x.com/karpathy/status/1959000000000000001" in page.text
        assert page.text.count("https://example.test/eval.png") >= 1
        assert page.text.count("https://example.test/eval-chart.webp") >= 1
        assert 'class="topic-filters"' in page.text or "data-topic-filters" in page.text
        assert 'data-topic="AI"' in page.text
        # Distinct commentary vs reason: both may appear; duplicated equal text must not.
        assert page.text.count("Useful eval, not a model launch.") == 1
        assert "This is the kind of AI post" in page.text


def test_today_dedupes_prefix_commentary(app_config, tmp_path: Path) -> None:
    why = "Frontier model release with concrete evals and open weights for local runs."
    source = tmp_path / "dup.json"
    source.write_text(
        """
{
  "date": "2026-08-27",
  "tweets": [
    {
      "id": "99",
      "author": "alice",
      "created_at": "2026-08-27T10:00:00+00:00",
      "text": "hello",
      "urls": ["https://x.com/alice/status/99"],
      "media": [],
      "kind": "tweet"
    }
  ],
  "clusters": [],
  "picks": [
    {
      "tweet_id": "99",
      "tag": "AI",
      "reason": "Frontier model release with concrete evals and open weights for local ru",
      "commentary": "Frontier model release with concrete evals and open weights for local runs."
    }
  ]
}
""".strip()
    )
    database = Database(app_config.db_path)
    run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 8, 27, 18, 0, tzinfo=UTC),
    )
    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert why in page.text
        # Reason is a prefix of commentary — show commentary once, not both blocks.
        assert page.text.count("pick-reason") == 0
        assert page.text.count("pick-commentary") == 1
