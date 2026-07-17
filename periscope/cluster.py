"""Story-clustering contract and defensive result validation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from periscope.llm import JSONLLM, LLMResponseError


def _compact_tweet(tweet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(tweet["id"]),
        "author": str(tweet["author"]),
        "text": str(tweet["text"]),
        "is_thread": str(tweet.get("thread_root_id") or tweet["id"]) != str(tweet["id"]),
    }


async def cluster_tweets(
    tweets: Sequence[Mapping[str, Any]],
    *,
    llm: JSONLLM,
    model: str,
    system_prompt: str,
    aggressiveness: int = 45,
) -> list[dict[str, Any]]:
    if not tweets:
        return []
    valid_ids = {str(tweet["id"]) for tweet in tweets}
    try:
        response = await llm.complete_json(
            "clusters",
            system_prompt=system_prompt,
            payload={
                "tweets": [_compact_tweet(tweet) for tweet in tweets],
                "aggressiveness": min(max(int(aggressiveness), 0), 100),
            },
            model=model,
        )
    except LLMResponseError:
        return []

    if isinstance(response, Mapping):
        response = response.get("clusters", [])
    if not isinstance(response, list):
        return []

    clusters: list[dict[str, Any]] = []
    assigned: set[str] = set()
    for item in response:
        if not isinstance(item, Mapping):
            continue
        raw_ids = item.get("tweet_ids", [])
        if not isinstance(raw_ids, list):
            continue
        tweet_ids = [
            str(tweet_id)
            for tweet_id in raw_ids
            if str(tweet_id) in valid_ids and str(tweet_id) not in assigned
        ]
        headline = str(item.get("headline", "")).strip()
        synthesis = str(item.get("synthesis", "")).strip()
        tag = str(item.get("tag", "")).strip()
        if not tweet_ids or not headline or not synthesis or not tag:
            continue
        assigned.update(tweet_ids)
        clusters.append(
            {
                "headline": headline,
                "synthesis": synthesis,
                "tag": tag,
                "tweet_ids": tweet_ids,
            }
        )
    return clusters
