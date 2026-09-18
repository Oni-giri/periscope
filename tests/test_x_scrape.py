from __future__ import annotations

import json
from pathlib import Path

from periscope.x_scrape.shortlist_to_digest import convert
from periscope.x_scrape.watermark import load_watermark


def test_load_watermark_reads_newest_ids(tmp_path: Path) -> None:
    path = tmp_path / "watermark.json"
    path.write_text(
        json.dumps(
            {
                "following_newest_ids": ["1", "2"],
                "following_newest_created_at": "2026-08-27T15:15:08.000Z",
            }
        )
    )
    ids, ts = load_watermark(path)
    assert ids == {"1", "2"}
    assert ts == "2026-08-27T15:15:08.000Z"


def test_load_watermark_missing() -> None:
    ids, ts = load_watermark(None)
    assert ids == set()
    assert ts is None


def test_shortlist_to_digest_maps_topics_and_skips_singletons() -> None:
    keepers = [
        {
            "status_id": "11",
            "author_handle": "@alice",
            "created_at": "2026-08-28T10:00:00Z",
            "text": "new model drop",
            "tweet_url": "https://x.com/alice/status/11",
            "image_urls": ["https://example.test/a.png"],
            "author_avatar": "https://example.test/alice.jpg",
            "curation": {"topic": "ai", "why": "frontier model news", "score": 9},
        },
        {
            "status_id": "12",
            "author_handle": "bob",
            "text": "eval harness",
            "tweet_url": "https://x.com/bob/status/12",
            "image_urls": [],
            "curation": {"topic": "ai", "why": "useful eval tooling", "score": 8},
        },
        {
            "status_id": "13",
            "author_handle": "@rigas",
            "text": "Riga tram data",
            "tweet_url": "https://x.com/rigas/status/13",
            "curation": {"topic": "latvia", "why": "local stats", "score": 7},
        },
    ]
    doc = convert(keepers, "2026-08-28")
    assert doc["date"] == "2026-08-28"
    assert [t["id"] for t in doc["tweets"]] == ["11", "12", "13"]
    assert doc["tweets"][0]["author"] == "alice"
    assert doc["tweets"][0]["media"] == ["https://example.test/a.png"]
    assert doc["tweets"][0]["avatar"] == "https://example.test/alice.jpg"
    assert "avatar" not in doc["tweets"][1]
    tags = {c["tag"] for c in doc["clusters"]}
    assert tags == {"AI"}
    assert doc["clusters"][0]["tweet_ids"] == ["11", "12"]
    assert len(doc["picks"]) == 3
    assert doc["picks"][0]["tag"] == "AI"
    assert doc["picks"][2]["tag"] == "LATVIA"


def test_empty_interests_fail_without_default_fallback(tmp_path, monkeypatch, capsys) -> None:
    import sqlite3
    import sys

    import pytest

    from periscope.x_scrape.curate_feeds import (
        NoInterestsError,
        build_schema,
        build_system_prompt,
        load_interest_topics,
        load_system_prompt_template,
        main,
    )

    db = tmp_path / "periscope.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE topics (name TEXT)")
        connection.commit()
    monkeypatch.setenv("PERISCOPE_DB", str(db))
    monkeypatch.setenv("PERISCOPE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(NoInterestsError, match="NO_INTERESTS"):
        load_interest_topics(db)
    with pytest.raises(NoInterestsError, match="NO_INTERESTS"):
        build_system_prompt([])
    with pytest.raises(NoInterestsError, match="NO_INTERESTS"):
        build_schema(None)

    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO topics(name) VALUES ('AI'), ('Latvia')")
        connection.commit()
    assert load_interest_topics(db) == ["ai", "latvia"]
    prompt = build_system_prompt(["AI", "Latvia"], template=None, data_dir=tmp_path)
    assert "ai, latvia" in prompt
    assert "ai|latvia|other" in prompt
    schema = build_schema(["AI", "Latvia"])
    assert schema["json_schema"]["schema"]["properties"]["items"]["items"]["properties"]["topic"][
        "enum"
    ] == ["ai", "latvia", "other"]

    custom = tmp_path / "prompts" / "curator_system.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("Rank for {interests}. enum={topic_enum}\n")
    loaded = load_system_prompt_template(tmp_path)
    assert "{interests}" in loaded
    filled = build_system_prompt(["tools"], template=loaded)
    assert "Rank for tools." in filled
    assert "tools|other" in filled

    empty = tmp_path / "empty.db"
    with sqlite3.connect(empty) as connection:
        connection.execute("CREATE TABLE topics (name TEXT)")
        connection.commit()
    monkeypatch.setenv("PERISCOPE_DB", str(empty))
    monkeypatch.setattr(sys, "argv", ["curate", "--in-dir", str(tmp_path)])
    assert main() == 2
    assert "NO_INTERESTS" in capsys.readouterr().out
