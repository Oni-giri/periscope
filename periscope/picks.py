"""Standalone-pick selection contract and calibration logging."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from periscope.llm import JSONLLM, LLMResponseError

PICK_TAGS = {"ARTIFACT", "INSIGHT", "SIGNAL", "ALPHA"}


def _compact_tweet(tweet: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(tweet["id"]),
        "author": str(tweet["author"]),
        "text": str(tweet["text"]),
        "is_thread": str(tweet.get("thread_root_id") or tweet["id"]) != str(tweet["id"]),
    }


async def select_picks(
    tweets: Sequence[Mapping[str, Any]],
    *,
    llm: JSONLLM,
    model: str,
    system_prompt: str,
    minimum: int,
    maximum: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not tweets:
        return [], []
    valid_ids = {str(tweet["id"]) for tweet in tweets}
    try:
        response = await llm.complete_json(
            "picks",
            system_prompt=system_prompt,
            payload={
                "tweets": [_compact_tweet(tweet) for tweet in tweets],
                "minimum": minimum,
                "maximum": maximum,
            },
            model=model,
        )
    except LLMResponseError:
        response = []

    if isinstance(response, Mapping):
        response = response.get("picks", [])
    if not isinstance(response, list):
        response = []

    picks: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for item in response:
        if not isinstance(item, Mapping) or len(picks) >= maximum:
            continue
        tweet_id = str(item.get("tweet_id", ""))
        tag = str(item.get("tag", "")).upper().strip()
        reason_words = str(item.get("reason", "")).strip().split()
        if (
            tweet_id not in valid_ids
            or tweet_id in selected_ids
            or tag not in PICK_TAGS
            or not reason_words
        ):
            continue
        selected_ids.add(tweet_id)
        picks.append(
            {
                "tweet_id": tweet_id,
                "tag": tag,
                "reason": " ".join(reason_words[:10]),
            }
        )

    selected = {pick["tweet_id"]: pick for pick in picks}
    decisions = []
    for tweet in tweets:
        tweet_id = str(tweet["id"])
        pick = selected.get(tweet_id)
        decisions.append(
            {
                "tweet_id": tweet_id,
                "selected": pick is not None,
                "tag": pick["tag"] if pick else None,
                "reason": pick["reason"] if pick else "not_selected",
            }
        )
    return picks, decisions
