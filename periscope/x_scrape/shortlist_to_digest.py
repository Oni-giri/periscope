#!/usr/bin/env python3
"""Convert GLM shortlist keepers into Periscope ingest JSON."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

from periscope.x_scrape.enrich_actions import enrich_pick_fields
from periscope.x_scrape.media_cache import cache_digest_media

TAG = {
    "ai": "AI",
    "latvia": "LATVIA",
    "crypto": "CRYPTO",
    "tools": "TOOLS",
    "science": "SCIENCE",
    "business": "BUSINESS",
    "other": "INSIGHT",
}


def convert(keepers: list[dict], digest_date: str) -> dict:
    tweets: list[dict] = []
    picks: list[dict] = []
    by_topic: dict[str, list[str]] = defaultdict(list)
    for keeper in keepers:
        sid = str(keeper["status_id"])
        curation = keeper.get("curation") or {}
        topic = curation.get("topic") or "other"
        tag = TAG.get(topic, "INSIGHT")
        handle = str(keeper.get("author_handle") or "unknown").removeprefix("@")
        media = list(keeper.get("image_urls") or [])
        tweets.append(
            {
                "id": sid,
                "author": handle,
                "created_at": keeper.get("created_at") or f"{digest_date}T18:00:00+00:00",
                "text": keeper.get("text") or "",
                "urls": [keeper["tweet_url"]] if keeper.get("tweet_url") else [],
                "media": media,
                "kind": "tweet",
            }
        )
        why = curation.get("why") or "curated"
        pick = {
            "tweet_id": sid,
            "tag": tag,
            "reason": why[:80],
            "commentary": why,
        }
        if curation.get("actions"):
            pick["actions"] = curation["actions"]
        for flag in ("nugget", "actionable"):
            if flag in curation:
                pick[flag] = bool(curation[flag])
        if curation.get("nugget_why"):
            pick["nugget_why"] = str(curation["nugget_why"])
        enrich_pick_fields(
            pick,
            tweet={
                "id": sid,
                "author": handle,
                "text": keeper.get("text") or "",
                "urls": [keeper["tweet_url"]] if keeper.get("tweet_url") else [],
            },
        )
        picks.append(pick)
        by_topic[tag].append(sid)

    clusters = []
    for tag, ids in by_topic.items():
        if len(ids) < 2:
            continue
        clusters.append(
            {
                "headline": f"{tag.title()} today",
                "synthesis": f"{len(ids)} notable {tag.lower()} posts from the GLM first pass.",
                "tag": tag,
                "tweet_ids": ids,
            }
        )
    return {"date": digest_date, "tweets": tweets, "clusters": clusters, "picks": picks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortlist", type=Path, default=Path("data/x-dumps/shortlist.json"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat())
    parser.add_argument(
        "--cache-media",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Download tweet images into data/media and rewrite URLs (default: on)",
    )
    parser.add_argument(
        "--media-dir",
        type=Path,
        default=Path("data/media"),
        help="Directory for cached media files",
    )
    args = parser.parse_args()
    source = json.loads(args.shortlist.read_text())
    keepers = source["keepers"] if isinstance(source, dict) else source
    digest_date = date.fromisoformat(args.date).isoformat()
    doc = convert(keepers, digest_date)
    if args.cache_media:
        cache_digest_media(doc, media_dir=args.media_dir)
    out = args.out or args.shortlist.with_name(f"digest-{digest_date}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print(
        f"wrote {out} tweets {len(doc['tweets'])} "
        f"clusters {len(doc['clusters'])} picks {len(doc['picks'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
