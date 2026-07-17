from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from periscope.config import Secrets, TopicConfig
from periscope.db import Database
from periscope.discovery import (
    cofollow_sources,
    following_delta,
    interaction_counts,
    overlap_percentage,
)
from periscope.jobs.weekly import run_weekly
from periscope.web.app import create_app


class FakeDiscoveryClient:
    def __init__(self) -> None:
        self.followed: list[str] = []

    async def following_handles(self, handle: str, *, limit: int = 500) -> list[str]:
        graph = {
            "data_builder": ["candidate_db"],
            "model_reader": ["candidate_db"],
            "candidate_db": ["data_builder", "sqlite_org"],
        }
        return graph.get(handle, [])[:limit]

    async def search_posts(self, query: str, *, limit: int = 20) -> list[dict]:
        return [
            {
                "id": "weekly-1",
                "author": "candidate_db",
                "created_at": "2026-07-15T12:00:00+00:00",
                "text": "A database benchmark with the raw scripts attached.",
                "user": {"username": "candidate_db", "followersCount": 4200},
            }
        ][:limit]

    async def profile(self, handle: str) -> dict:
        return {
            "username": handle,
            "displayname": "Candidate DB",
            "description": "Measured database systems work.",
            "followersCount": 4200,
            "statusesCount": 800,
        }

    async def follow_account(self, handle: str) -> None:
        self.followed.append(handle)


class NullNotifier:
    async def send_digest(self, digest, text) -> None:
        return None

    async def send_alert(self, text) -> None:
        return None

    async def send_weekly(self, report) -> None:
        return None


def test_discovery_evidence_math() -> None:
    assert following_delta(["one", "gone"], ["one", "new"]) == {
        "added": {"new"},
        "removed": {"gone"},
    }
    assert cofollow_sources(
        {"a": {"candidate"}, "b": {"candidate", "a"}},
        curated_handles={"a", "b"},
    ) == {"candidate": {"a", "b"}}
    assert overlap_percentage(["one", "two", "three", "four"], ["two", "four"]) == 50.0

    tweets = [
        {
            "author": "a",
            "raw": {
                "retweetedTweet": {"author": "outside"},
                "quotedTweet": {"user": {"username": "quoted"}},
            },
        },
        {
            "author": "not_curated",
            "raw": {"inReplyToUserUsername": "ignored"},
        },
    ]
    assert interaction_counts(tweets, curated_handles={"a"}) == {
        "outside": 1,
        "quoted": 1,
    }


def test_weekly_job_and_discovery_review(app_config) -> None:
    config = replace(app_config, topics=(TopicConfig("Databases", 30),))
    database = Database(config.db_path)
    client = FakeDiscoveryClient()
    now = datetime(2026, 7, 16, 16, 0, tzinfo=UTC)

    first = asyncio.run(
        run_weekly(
            config,
            Secrets(),
            database=database,
            xclient=client,
            notifier=NullNotifier(),
            mock_llm=True,
            now=now,
        )
    )
    second = asyncio.run(
        run_weekly(
            config,
            Secrets(),
            database=database,
            xclient=client,
            notifier=NullNotifier(),
            mock_llm=True,
            now=now,
        )
    )

    assert first.report_written
    assert second.report_written
    assert database.get_tweet("weekly-1")["text"].startswith("A database benchmark")
    assert len(database.rows("SELECT week FROM weekly_reports")) == 1
    assert len(database.rows("SELECT handle FROM follow_snapshots")) == 2
    assert database.get_candidate("candidate_db")["status"] == "pending"

    database.upsert_candidate(
        handle="candidate_reject",
        reason="Topic search",
        cofollow_count=0,
        overlap_pct=10,
        stats={"name": "Reject Me"},
        surfaced_at=now,
    )
    app = create_app(config, Secrets(), database=database, xclient=client)
    with TestClient(app) as web:
        queue = web.get("/discovery")
        assert queue.status_code == 200
        assert "Candidate DB" in queue.text
        assert "2 candidates" in queue.text

        accepted = web.post(
            "/discovery/candidate_db/accept",
            headers={"HX-Request": "true"},
        )
        assert accepted.status_code == 200
        assert "Candidate DB" not in accepted.text
        assert client.followed == ["candidate_db"]
        assert database.get_candidate("candidate_db")["status"] == "accepted"
        assert any(row["handle"] == "candidate_db" for row in database.list_accounts())

        rejected = web.post(
            "/discovery/candidate_reject/reject",
            headers={"HX-Request": "true"},
        )
        assert rejected.status_code == 200
        assert database.get_candidate("candidate_reject")["status"] == "rejected"

        weekly = web.get("/weekly")
        assert weekly.status_code == 200
        assert first.week in weekly.text
        assert "Picks and keeps calibration" in weekly.text

    assert not database.upsert_candidate(
        handle="candidate_reject",
        reason="Too soon",
        cofollow_count=4,
        overlap_pct=50,
        stats={},
        surfaced_at=now + timedelta(days=89),
    )
    assert database.upsert_candidate(
        handle="candidate_reject",
        reason="Suppression expired",
        cofollow_count=4,
        overlap_pct=50,
        stats={},
        surfaced_at=now + timedelta(days=91),
    )
