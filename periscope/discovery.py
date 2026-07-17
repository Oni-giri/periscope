"""Deterministic social-graph evidence extraction for weekly discovery."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def canonical_handle(value: Any) -> str:
    return str(value or "").strip().removeprefix("@").lower()


def following_delta(previous: Iterable[str], current: Iterable[str]) -> dict[str, set[str]]:
    """Return stable added and removed handle sets between two graph snapshots."""

    before = {canonical_handle(item) for item in previous if canonical_handle(item)}
    after = {canonical_handle(item) for item in current if canonical_handle(item)}
    return {"added": after - before, "removed": before - after}


def cofollow_sources(
    additions: Mapping[str, Iterable[str]],
    *,
    curated_handles: Iterable[str] = (),
) -> dict[str, set[str]]:
    """Invert per-account additions into candidate -> curated-source evidence."""

    curated = {canonical_handle(item) for item in curated_handles}
    result: dict[str, set[str]] = defaultdict(set)
    for source, handles in additions.items():
        source_handle = canonical_handle(source)
        for handle in handles:
            candidate = canonical_handle(handle)
            if candidate and candidate not in curated:
                result[candidate].add(source_handle)
    return dict(result)


def _nested_author(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    user = value.get("user")
    if isinstance(user, Mapping):
        nested = canonical_handle(user.get("username") or user.get("screen_name"))
        if nested:
            return nested
    return canonical_handle(value.get("author") or value.get("username"))


def interaction_handles(tweet: Mapping[str, Any]) -> set[str]:
    """Extract external accounts referenced through RT, quote, and reply relations."""

    raw_value = tweet.get("raw", tweet)
    raw = raw_value if isinstance(raw_value, Mapping) else {}
    result: set[str] = set()

    for key in ("retweetedTweet", "retweeted_tweet", "quotedTweet", "quoted_tweet"):
        handle = _nested_author(raw.get(key))
        if handle:
            result.add(handle)

    for key in (
        "inReplyToUserUsername",
        "in_reply_to_screen_name",
        "inReplyToUsername",
    ):
        handle = canonical_handle(raw.get(key))
        if handle:
            result.add(handle)

    return result


def interaction_counts(
    tweets: Sequence[Mapping[str, Any]],
    *,
    curated_handles: Iterable[str] = (),
) -> Counter[str]:
    curated = {canonical_handle(item) for item in curated_handles}
    counts: Counter[str] = Counter()
    for tweet in tweets:
        source = canonical_handle(tweet.get("author"))
        if source not in curated:
            continue
        for handle in interaction_handles(tweet):
            if handle not in curated:
                counts[handle] += 1
    return counts


def overlap_percentage(candidate_following: Iterable[str], graph_union: Iterable[str]) -> float:
    candidate = {canonical_handle(item) for item in candidate_following if canonical_handle(item)}
    graph = {canonical_handle(item) for item in graph_union if canonical_handle(item)}
    if not candidate:
        return 0.0
    return round(100.0 * len(candidate & graph) / len(candidate), 1)


def profile_stats(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the small display-safe subset of an X profile."""

    return {
        "name": str(
            profile.get("displayname")
            or profile.get("display_name")
            or profile.get("name")
            or profile.get("username")
            or ""
        ),
        "bio": str(profile.get("rawDescription") or profile.get("description") or ""),
        "followers": int(profile.get("followersCount") or profile.get("followers_count") or 0),
        "statuses": int(profile.get("statusesCount") or profile.get("statuses_count") or 0),
    }


def candidate_reason(
    *,
    cofollow_count: int,
    interaction_count: int,
    topic_hits: Sequence[str],
) -> str:
    if cofollow_count:
        noun = "account" if cofollow_count == 1 else "accounts"
        return f"Followed by {cofollow_count} curated {noun} this week"
    if interaction_count:
        return f"Referenced {interaction_count} times by your list this week"
    if topic_hits:
        return f"Surfaced in the {topic_hits[0]} topic search"
    return "Surfaced by weekly graph analysis"
