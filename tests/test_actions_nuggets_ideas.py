from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.ingest import run_ingest
from periscope.web.app import create_app
from periscope.x_scrape.enrich_actions import (
    heuristic_actions,
    normalize_actions,
    parse_enrich_response,
)
from periscope.x_scrape.shortlist_to_digest import convert


def test_heuristic_actions_detects_try_read_watch_follow() -> None:
    linked = heuristic_actions(
        text="Ship it: https://github.com/acme/tool and https://docs.acme.test/guide",
        commentary="Handy release notes.",
        urls=["https://github.com/acme/tool", "https://docs.acme.test/guide"],
        handle="bob",
        tag="TOOLS",
    )
    linked_types = {action["type"] for action in linked["actions"]}
    assert "try" in linked_types
    assert "read" in linked_types

    watched = heuristic_actions(
        text="New exploit class against this oracle setup.",
        handle="bob",
        tag="CRYPTO",
    )
    assert any(action["type"] == "watch" for action in watched["actions"])

    followed = heuristic_actions(
        text="Worth following @security_alice on protocol risk.",
        handle="bob",
        tag="CRYPTO",
    )
    assert any(action["type"] == "follow" for action in followed["actions"])
    assert linked["actionable"] is True


def test_parse_enrich_response_and_normalize() -> None:
    parsed = parse_enrich_response(
        {
            "picks": [
                {
                    "tweet_id": "1",
                    "actions": [
                        {"type": "try", "label": "Try foo", "url": "https://example.test"},
                        {"type": "nope", "label": "bad"},
                        {"type": "try", "label": "Try foo"},
                    ],
                    "nugget": True,
                    "actionable": True,
                    "nugget_why": "pattern",
                }
            ]
        }
    )
    assert parsed["1"]["nugget"] is True
    assert len(parsed["1"]["actions"]) == 1
    assert normalize_actions([{"type": "steal", "label": "x"}])[0]["type"] == "steal"


def test_shortlist_convert_adds_action_fields() -> None:
    keepers = [
        {
            "status_id": "55",
            "author_handle": "@alice",
            "created_at": "2026-09-01T10:00:00Z",
            "text": "New model weights https://huggingface.co/acme/model",
            "tweet_url": "https://x.com/alice/status/55",
            "image_urls": [],
            "curation": {"topic": "ai", "why": "weights to try locally", "score": 8},
        }
    ]
    doc = convert(keepers, "2026-09-01")
    pick = doc["picks"][0]
    assert pick["actions"]
    assert any(action["type"] == "try" for action in pick["actions"])


def test_today_renders_actions_and_nuggets(app_config) -> None:
    source = Path(__file__).parent / "fixtures" / "actions-nuggets-digest.json"
    database = Database(app_config.db_path)
    run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
    )
    digest = database.get_digest("2026-09-01")
    assert digest is not None
    picks = digest["rendered"]["picks"]
    assert any(pick.get("actions") for pick in picks)
    assert any(pick.get("nugget") for pick in picks)

    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Nuggets" in page.text
        assert "action-strip" in page.text
        assert "Try example/rag-cli" in page.text
        assert "Park in inbox" in page.text
        assert "Prefer writable interfaces" in page.text


def test_ideas_park_list_done(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        nav = client.get("/ideas")
        assert nav.status_code == 200
        assert "Ideas" in nav.text

        parked = client.post(
            "/ideas",
            data={
                "title": "Try rag-cli",
                "action_type": "try",
                "url": "https://github.com/example/rag-cli",
                "handle": "builder",
                "tweet_id": "2001",
                "note": "tonight",
            },
            headers={"HX-Request": "true"},
        )
        assert parked.status_code == 200
        assert "Parked" in parked.text
        ideas = database.list_ideas(status="parked")
        assert len(ideas) == 1
        idea_id = ideas[0]["id"]

        listed = client.get("/ideas")
        assert "Try rag-cli" in listed.text

        done = client.post(f"/ideas/{idea_id}/done")
        assert done.status_code == 200
        assert database.get_idea(idea_id)["status"] == "done"

        client.post(
            "/ideas",
            data={"title": "Old idea", "action_type": "steal"},
        )
        stale_id = database.list_ideas(status="parked")[0]["id"]
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat(timespec="seconds")
        with database.connect() as connection:
            connection.execute(
                "UPDATE ideas SET created_at = ? WHERE id = ?",
                (old, stale_id),
            )
            connection.commit()
        stale_page = client.get("/ideas?status=stale")
        assert stale_page.status_code == 200
        assert "Old idea" in stale_page.text


def test_agent_digest_fixture_still_ingests_with_actions(app_config) -> None:
    source = Path(__file__).parent / "fixtures" / "agent-digest.json"
    doc = json.loads(source.read_text())
    assert doc["picks"][0]["actions"]
    database = Database(app_config.db_path)
    result = run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 8, 26, 18, 0, tzinfo=UTC),
    )
    assert result.pick_count == 1
    pick = database.get_digest("2026-08-26")["rendered"]["picks"][0]
    assert pick["nugget"] is True
    assert pick["actions"][0]["type"] == "read"
