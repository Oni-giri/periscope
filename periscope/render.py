"""Digest assembly and channel-neutral rendering."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from periscope.rank import interleave, rank_clusters, rank_picks


def _stable_id(prefix: str, digest_date: date, values: Sequence[str]) -> str:
    source = "|".join((prefix, digest_date.isoformat(), *values))
    return f"{prefix}_{hashlib.sha256(source.encode()).hexdigest()[:16]}"


def _tweet_view(tweet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(tweet["id"]),
        "author": str(tweet["author"]),
        "created_at": str(tweet["created_at"]),
        "text": str(tweet["text"]),
        "thread_root_id": tweet.get("thread_root_id"),
        "quoted_id": tweet.get("quoted_id"),
        "urls": list(tweet.get("urls", [])),
        "kind": str(tweet.get("kind", "tweet")),
    }


def assemble_digest(
    *,
    digest_date: date,
    assembled_at: datetime,
    tweets: Sequence[Mapping[str, Any]],
    cluster_drafts: Sequence[Mapping[str, Any]],
    pick_drafts: Sequence[Mapping[str, Any]],
    fetch_new_items: int = 0,
    topic_weights: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    ranked_clusters = rank_clusters(
        cluster_drafts,
        tweets,
        now=assembled_at,
        topic_weights=topic_weights,
    )
    ranked_picks = rank_picks(pick_drafts, tweets, now=assembled_at)
    by_id = {str(tweet["id"]): tweet for tweet in tweets}

    clusters: list[dict[str, Any]] = []
    for cluster in ranked_clusters:
        result = dict(cluster)
        result["id"] = _stable_id(
            "cluster",
            digest_date,
            [str(item) for item in result["tweet_ids"]],
        )
        result["tweets"] = [
            _tweet_view(by_id[str(tweet_id)])
            for tweet_id in result["tweet_ids"]
            if str(tweet_id) in by_id
        ]
        clusters.append(result)

    picks: list[dict[str, Any]] = []
    for pick in ranked_picks:
        result = dict(pick)
        result["id"] = _stable_id("pick", digest_date, [str(result["tweet_id"])])
        result["tweet"] = _tweet_view(by_id[str(result["tweet_id"])])
        picks.append(result)

    stats = {
        "stories": len(clusters),
        "picks": len(picks),
        "items": len(tweets),
        "accounts": len({str(tweet["author"]) for tweet in tweets}),
        "new_items": fetch_new_items,
    }
    return {
        "date": digest_date.isoformat(),
        "assembled_at": assembled_at.isoformat(timespec="seconds"),
        "stats": stats,
        "clusters": clusters,
        "picks": picks,
        "items": interleave(clusters, picks),
    }


def render_telegram(digest: Mapping[str, Any]) -> str:
    target = date.fromisoformat(str(digest["date"]))
    lines = [f"Periscope · {target.strftime('%d %b %Y')}"]
    clusters = {str(item["id"]): item for item in digest["clusters"]}
    picks = {str(item["id"]): item for item in digest["picks"]}
    story_number = 0
    for item in digest["items"]:
        item_id = str(item["id"])
        if item["type"] == "cluster":
            story_number += 1
            cluster = clusters[item_id]
            lines.append(f"{story_number:02d}  {cluster['headline']}")
        else:
            pick = picks[item_id]
            author = pick["tweet"]["author"]
            lines.append(f"★  @{author} — {pick['reason']}")
    if len(lines) == 1:
        lines.append("No new stories in this digest window.")
    return "\n".join(lines)
