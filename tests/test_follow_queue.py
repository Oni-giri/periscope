from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.ingest import run_ingest
from periscope.web.app import create_app
from periscope.web.routes.follow_queue import parse_follow_handle
from periscope.x_scrape.follow_queued import (
    drain_follow_queue,
    drain_pending,
    queue_status_for_result,
)


def test_parse_follow_handle_url_label_fallback() -> None:
    assert parse_follow_handle(url="https://x.com/Security_Alice") == "security_alice"
    assert parse_follow_handle(url="https://twitter.com/Bob/status/99") == "bob"
    assert parse_follow_handle(label="Follow @carol_dev") == "carol_dev"
    assert parse_follow_handle(fallback="@Dave") == "dave"
    assert parse_follow_handle(url="https://github.com/acme", fallback="author") == "author"
    assert parse_follow_handle(url="https://x.com/home") is None


def test_enqueue_duplicate_and_mark_follow(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()

    first = database.enqueue_follow("@Foo", tweet_id="1", source="today")
    assert first["handle"] == "foo"
    assert first["status"] == "pending"
    assert first["tweet_id"] == "1"
    assert database.list_pending_follows()[0]["handle"] == "foo"

    again = database.enqueue_follow("foo", tweet_id="2")
    assert again["status"] == "pending"
    assert again["added_at"] == first["added_at"]
    assert again["tweet_id"] == "1"
    assert len(database.list_pending_follows()) == 1

    followed = database.mark_follow("foo", "followed")
    assert followed is not None
    assert followed["status"] == "followed"
    assert followed["followed_at"]
    assert database.list_pending_follows() == []
    left = database.enqueue_follow("FOO")
    assert left["status"] == "followed"

    already = database.mark_follow("foo", "already")
    assert already is not None
    assert already["status"] == "already"
    still = database.enqueue_follow("foo")
    assert still["status"] == "already"

    failed = database.mark_follow("foo", "failed", error="timeout")
    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["last_error"] == "timeout"
    assert failed["followed_at"] is None
    reset = database.enqueue_follow("@Foo", tweet_id="9", source="archive")
    assert reset["status"] == "pending"
    assert reset["last_error"] is None
    assert reset["tweet_id"] == "9"
    assert reset["source"] == "archive"
    assert "foo" in database.queued_follow_handles()

    missing = database.mark_follow("nobody", "followed")
    assert missing is None


def test_today_and_archive_html_contain_follow_button(app_config) -> None:
    source = Path(__file__).parent / "fixtures" / "actions-nuggets-digest.json"
    database = Database(app_config.db_path)
    run_ingest(
        app_config,
        Secrets(),
        source=source,
        database=database,
        now=datetime(2026, 9, 1, 18, 0, tzinfo=UTC),
    )
    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        today = client.get("/")
        assert today.status_code == 200
        assert "/follow-queue" in today.text
        assert ">Follow</button>" in today.text
        assert "Follow @security_alice" in today.text

        archive = client.get("/archive?action=follow")
        assert archive.status_code == 200
        assert ">Follow</button>" in archive.text
        assert "security_alice" in archive.text

        queued = client.post(
            "/follow-queue",
            data={"handle": "@Security_Alice", "tweet_id": "2001", "source": "today"},
            headers={"HX-Request": "true"},
        )
        assert queued.status_code == 200
        assert "Queued" in queued.text
        assert database.list_pending_follows()[0]["handle"] == "security_alice"

        again = client.get("/")
        assert "Queued" in again.text
        assert ">Follow</button>" not in again.text


def test_drain_pending_caps_and_maps_results(app_config, monkeypatch) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    for handle in ("aaa", "bbb", "ccc"):
        database.enqueue_follow(handle)

    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    calls: list[str] = []

    def fake_follow_pw(page, handle: str) -> str:
        calls.append(handle)
        if handle == "aaa":
            return "followed"
        if handle == "bbb":
            raise PlaywrightTimeout("timeout")
        return "already"

    monkeypatch.setattr("periscope.x_scrape.follow_queued.follow_one", fake_follow_pw)
    outcomes = drain_pending(object(), database, limit=2)
    assert [item["handle"] for item in outcomes] == ["aaa", "bbb"]
    assert outcomes[0]["status"] == "followed"
    assert outcomes[1]["status"] == "failed"
    remaining = database.list_pending_follows()
    assert [row["handle"] for row in remaining] == ["ccc"]
    failed_row = database.rows("SELECT last_error FROM follow_queue WHERE handle = ?", ("bbb",))[0]
    assert failed_row["last_error"] == "timeout"

    skipped = drain_follow_queue(object(), app_config.db_path.parent / "missing.db")
    assert skipped == []


def test_queue_status_for_result() -> None:
    assert queue_status_for_result("followed") == ("followed", None)
    assert queue_status_for_result("clicked") == ("followed", None)
    assert queue_status_for_result("already") == ("already", None)
    assert queue_status_for_result("pending") == ("already", None)
    assert queue_status_for_result("timeout") == ("failed", "timeout")
    assert queue_status_for_result("no-button") == ("failed", "no-button")
