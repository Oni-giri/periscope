from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from periscope.cluster import cluster_tweets
from periscope.llm import StaticJSONLLM, parse_json_response
from periscope.picks import select_picks
from periscope.rank import engagement_score


def _tweets():
    return [
        {
            "id": "1",
            "author": "one",
            "text": "Released a useful tool",
            "created_at": "2026-07-16T08:00:00+00:00",
            "thread_root_id": "1",
            "raw": {"likeCount": 30, "user": {"followersCount": 1000}},
        },
        {
            "id": "2",
            "author": "two",
            "text": "Hot take. Thoughts?",
            "created_at": "2026-07-16T08:00:00+00:00",
            "thread_root_id": "2",
            "raw": {"likeCount": 30, "user": {"followersCount": 1000}},
        },
    ]


def test_parse_json_response_strips_fences_and_trailing_text() -> None:
    parsed = parse_json_response('preface\n```json\n[{"id": 1}]\n```\nafter')
    assert parsed == [{"id": 1}]


def test_cluster_parser_drops_unknown_and_duplicate_ids() -> None:
    llm = StaticJSONLLM(
        {
            "clusters": [
                {
                    "headline": "First",
                    "synthesis": "Summary",
                    "tag": "test",
                    "tweet_ids": ["1", "missing"],
                },
                {
                    "headline": "Duplicate",
                    "synthesis": "Summary",
                    "tag": "test",
                    "tweet_ids": ["1", "2"],
                },
            ]
        }
    )

    clusters = asyncio.run(cluster_tweets(_tweets(), llm=llm, model="test", system_prompt=""))

    assert clusters[0]["tweet_ids"] == ["1"]
    assert clusters[1]["tweet_ids"] == ["2"]


def test_malformed_pick_output_falls_back_to_no_picks() -> None:
    llm = StaticJSONLLM({"picks": "```json\nnot-json\n```"})

    picks, decisions = asyncio.run(
        select_picks(
            _tweets(),
            llm=llm,
            model="test",
            system_prompt="",
            minimum=0,
            maximum=5,
        )
    )

    assert picks == []
    assert all(not decision["selected"] for decision in decisions)


def test_pick_validation_and_reason_limit() -> None:
    llm = StaticJSONLLM(
        {
            "picks": [
                {
                    "tweet_id": "1",
                    "tag": "artifact",
                    "reason": "one two three four five six seven eight nine ten eleven",
                },
                {"tweet_id": "2", "tag": "INVALID", "reason": "No"},
            ]
        }
    )

    picks, decisions = asyncio.run(
        select_picks(
            _tweets(),
            llm=llm,
            model="test",
            system_prompt="",
            minimum=0,
            maximum=5,
        )
    )

    assert len(picks) == 1
    assert picks[0]["tag"] == "ARTIFACT"
    assert len(picks[0]["reason"].split()) == 10
    assert len(decisions) == 2


def test_bait_penalty_reduces_otherwise_equal_score() -> None:
    useful, bait = _tweets()
    now = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)

    assert engagement_score(useful, now=now) > engagement_score(bait, now=now)
