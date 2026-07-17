"""Engagement-normalized, time-decayed ordering without UI metric prominence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

_BAIT = re.compile(
    r"(?:\bthoughts\??\b|\bwhat do you think\b|\bwrong answers only\b|"
    r"\blike and retweet\b|\bhot take\b|\bpoll\b)",
    re.IGNORECASE,
)


def _metric(tweet: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    raw = tweet.get("raw")
    source = raw if isinstance(raw, Mapping) else tweet
    for key in keys:
        value = source.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return default


def _followers(tweet: Mapping[str, Any]) -> float:
    raw = tweet.get("raw")
    source = raw if isinstance(raw, Mapping) else tweet
    user = source.get("user")
    if isinstance(user, Mapping):
        for key in ("followersCount", "followers_count"):
            if user.get(key) is not None:
                try:
                    return float(user[key])
                except (TypeError, ValueError):
                    pass
    return _metric(tweet, "author_followers", "followers_count", default=1000.0)


def _created_at(tweet: Mapping[str, Any]) -> datetime:
    value = str(tweet.get("created_at", ""))
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def engagement_score(
    tweet: Mapping[str, Any],
    *,
    now: datetime,
    half_life_hours: float = 36.0,
) -> float:
    """Normalize engagement by audience, decay by age, and discount bait patterns."""

    followers = max(_followers(tweet), 100.0)
    likes = _metric(tweet, "likeCount", "favorite_count", "faves")
    reposts = _metric(tweet, "retweetCount", "retweet_count", "reposts")
    normalized = (likes + reposts * 1.5) / followers * 1000
    age_hours = max((now.astimezone(UTC) - _created_at(tweet)).total_seconds() / 3600, 0.0)
    decay = 0.5 ** (age_hours / half_life_hours)
    penalty = 0.55 if _BAIT.search(str(tweet.get("text", ""))) else 1.0
    return math.log1p(max(normalized, 0.0)) * decay * penalty


def rank_clusters(
    clusters: Sequence[Mapping[str, Any]],
    tweets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    topic_weights: Mapping[str, float] | None = None,
) -> list[dict[str, Any]]:
    by_id = {str(tweet["id"]): tweet for tweet in tweets}
    weights = {key.lower(): value for key, value in (topic_weights or {}).items()}

    def score(cluster: Mapping[str, Any]) -> float:
        ids = [str(item) for item in cluster["tweet_ids"] if str(item) in by_id]
        item_scores = [engagement_score(by_id[item], now=now) for item in ids]
        base = sum(item_scores) / len(item_scores) if item_scores else 0.0
        size_bonus = math.log1p(len(ids)) * 0.08
        topic_weight = weights.get(str(cluster.get("tag", "")).lower(), 1.0)
        return (base + size_bonus) * topic_weight

    ranked = sorted(
        (dict(cluster) for cluster in clusters),
        key=lambda cluster: (-score(cluster), str(cluster["headline"]).lower()),
    )
    for index, cluster in enumerate(ranked, start=1):
        cluster["rank"] = index
    return ranked


def rank_picks(
    picks: Sequence[Mapping[str, Any]],
    tweets: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    by_id = {str(tweet["id"]): tweet for tweet in tweets}
    ranked = sorted(
        (dict(pick) for pick in picks if str(pick["tweet_id"]) in by_id),
        key=lambda pick: (
            -engagement_score(by_id[str(pick["tweet_id"])], now=now),
            str(pick["tweet_id"]),
        ),
    )
    for index, pick in enumerate(ranked, start=1):
        pick["rank"] = index
    return ranked


def interleave(
    clusters: Sequence[Mapping[str, Any]],
    picks: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Spread picks through stories while preserving each list's ranking."""

    items: list[dict[str, str]] = []
    pick_index = 0
    if not clusters:
        return [{"type": "pick", "id": str(pick["id"])} for pick in picks]

    for cluster_index, cluster in enumerate(clusters, start=1):
        items.append({"type": "cluster", "id": str(cluster["id"])})
        target_picks = round(cluster_index * len(picks) / len(clusters))
        while pick_index < target_picks:
            items.append({"type": "pick", "id": str(picks[pick_index]["id"])})
            pick_index += 1
    while pick_index < len(picks):
        items.append({"type": "pick", "id": str(picks[pick_index]["id"])})
        pick_index += 1
    return items
