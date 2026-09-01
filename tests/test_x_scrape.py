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
