#!/usr/bin/env python3
"""Convert GLM shortlist keepers into Periscope ingest JSON."""

from __future__ import annotations

import argparse
import sys
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


def convert(
    keepers: list[dict],
    digest_date: str,
    *,
    highlights: str | None = None,
) -> dict:
    """Build a Periscope ingest document from shortlist keepers.

    Optional ``highlights`` is edition-level magazine prose for the Today lede.
    Highlights and pick commentary may use ``**bold**`` sparingly; the web UI
    renders that subset safely.
    Prefer 2–4 short opinionated paragraphs separated by blank lines (`\n\n`),
    not one wall of text. Leave empty so a curator/agent can fill it later;
    ingest persists it when set.
    """
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
        tweet = {
            "id": sid,
            "author": handle,
            "created_at": keeper.get("created_at") or f"{digest_date}T18:00:00+00:00",
            "text": keeper.get("text") or "",
            "urls": [keeper["tweet_url"]] if keeper.get("tweet_url") else [],
            "media": media,
            "kind": "tweet",
        }
        avatar = keeper.get("author_avatar") or keeper.get("avatar")
        if avatar:
            tweet["avatar"] = str(avatar)
        tweets.append(tweet)
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
    doc: dict = {"date": digest_date, "tweets": tweets, "clusters": clusters, "picks": picks}
    if highlights and str(highlights).strip():
        doc["highlights"] = str(highlights).strip()
    return doc


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
        help=(
            "Directory Periscope serves at /media (use {data_dir}/media, "
            "not a magazine export images/ folder)"
        ),
    )
    args = parser.parse_args()
    source = json.loads(args.shortlist.read_text())
    keepers = source["keepers"] if isinstance(source, dict) else source
    digest_date = date.fromisoformat(args.date).isoformat()
    doc = convert(keepers, digest_date)
    if args.cache_media:
        media_dir = args.media_dir.resolve()
        if media_dir.name == "images" or "x-recap" in media_dir.as_posix():
            print(
                "warning: --media-dir looks like a magazine export folder "
                f"({media_dir}); Periscope serves {{data_dir}}/media at /media — "
                "avatars will 404 in the UI if files are not also there",
                file=sys.stderr,
            )
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
