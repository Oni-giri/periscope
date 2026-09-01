"""Load an externally curated digest JSON. No X fetch, no Anthropic."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from periscope.config import AppConfig, Secrets, load_config, load_secrets
from periscope.db import Database, isoformat
from periscope.render import assemble_digest


@dataclass(frozen=True, slots=True)
class IngestResult:
    date: str
    tweet_count: int
    cluster_count: int
    pick_count: int
    digest_written: bool
    note: str | None = None


def _as_tweet_payload(item: dict[str, Any]) -> dict[str, Any]:
    if "user" in item or "rawContent" in item or "id_str" in item:
        return item
    payload = {
        "id": str(item.get("id") or item.get("tweet_id") or ""),
        "author": str(item.get("author", "unknown")).removeprefix("@"),
        "created_at": item.get("created_at") or isoformat(),
        "text": item.get("text") or item.get("rawContent") or "",
        "urls": list(item.get("urls") or []),
        "kind": item.get("kind", "tweet"),
    }
    media = item.get("media") or []
    if media:
        payload["media"] = media
        for url in media:
            if url not in payload["urls"]:
                payload["urls"].append(url)
    if item.get("quoted_id"):
        payload["quoted_id"] = item["quoted_id"]
    if item.get("thread_root_id"):
        payload["thread_root_id"] = item["thread_root_id"]
    return payload


def _collect_tweets(document: dict[str, Any]) -> list[dict[str, Any]]:
    collected: dict[str, dict[str, Any]] = {}
    for item in document.get("tweets") or []:
        payload = _as_tweet_payload(item)
        if payload["id"]:
            collected[payload["id"]] = payload
    for pick in document.get("picks") or []:
        nested = pick.get("tweet")
        if isinstance(nested, dict):
            payload = _as_tweet_payload(nested)
            if payload["id"]:
                collected.setdefault(payload["id"], payload)
        tweet_id = str(pick.get("tweet_id") or "")
        if tweet_id and tweet_id not in collected:
            collected[tweet_id] = _as_tweet_payload({"id": tweet_id, **pick})
    for cluster in document.get("clusters") or []:
        for nested in cluster.get("tweets") or []:
            payload = _as_tweet_payload(nested)
            if payload["id"]:
                collected.setdefault(payload["id"], payload)
    return list(collected.values())


def _cluster_drafts(document: dict[str, Any]) -> list[dict[str, Any]]:
    drafts = []
    for cluster in document.get("clusters") or []:
        tweet_ids = cluster.get("tweet_ids")
        if not tweet_ids:
            tweet_ids = [
                str(tweet["id"]) for tweet in cluster.get("tweets") or [] if tweet.get("id")
            ]
        drafts.append(
            {
                "headline": cluster["headline"],
                "synthesis": cluster.get("synthesis") or cluster.get("commentary") or "",
                "tag": cluster.get("tag", "INSIGHT"),
                "tweet_ids": [str(item) for item in tweet_ids],
            }
        )
    return drafts


def _pick_drafts(
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
    drafts = []
    commentary_by_id: dict[str, str] = {}
    extras_by_id: dict[str, dict[str, Any]] = {}
    for pick in document.get("picks") or []:
        tweet_id = str(pick.get("tweet_id") or (pick.get("tweet") or {}).get("id") or "")
        if not tweet_id:
            continue
        commentary = str(pick.get("commentary") or "").strip()
        if commentary:
            commentary_by_id[tweet_id] = commentary
        draft: dict[str, Any] = {
            "tweet_id": tweet_id,
            "tag": pick.get("tag", "SIGNAL"),
            "reason": pick.get("reason") or commentary[:80] or "curated",
        }
        extras: dict[str, Any] = {}
        if pick.get("actions"):
            extras["actions"] = pick["actions"]
            draft["actions"] = pick["actions"]
        for flag in ("nugget", "actionable"):
            if flag in pick:
                extras[flag] = bool(pick[flag])
                draft[flag] = bool(pick[flag])
        if pick.get("nugget_why"):
            extras["nugget_why"] = str(pick["nugget_why"])
            draft["nugget_why"] = str(pick["nugget_why"])
        if extras:
            extras_by_id[tweet_id] = extras
        drafts.append(draft)
    return drafts, commentary_by_id, extras_by_id


def _attach_media_and_commentary(
    digest: dict[str, Any],
    tweets: list[dict[str, Any]],
    commentary_by_id: dict[str, str],
    extras_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    media_by_id = {
        str(tweet["id"]): list(tweet.get("media") or []) for tweet in tweets if tweet.get("media")
    }
    extras_by_id = extras_by_id or {}
    for pick in digest["picks"]:
        tweet_id = str(pick["tweet_id"])
        if tweet_id in commentary_by_id:
            pick["commentary"] = commentary_by_id[tweet_id]
        for key, value in extras_by_id.get(tweet_id, {}).items():
            pick[key] = value
        suffixes = (".jpg", ".jpeg", ".png", ".webp", ".gif")
        media = media_by_id.get(tweet_id) or [
            url for url in pick["tweet"].get("urls", []) if str(url).lower().endswith(suffixes)
        ]
        if media:
            pick["tweet"]["media"] = media
    for cluster in digest["clusters"]:
        for tweet in cluster.get("tweets") or []:
            media = media_by_id.get(str(tweet["id"]))
            if media:
                tweet["media"] = media
    return digest


def run_ingest(
    config: AppConfig,
    secrets: Secrets,
    *,
    source: Path,
    database: Database | None = None,
    digest_date: date | None = None,
    now: datetime | None = None,
) -> IngestResult:
    document = json.loads(source.read_text(encoding="utf-8"))
    assembled_at = now or datetime.now(UTC)
    if assembled_at.tzinfo is None:
        assembled_at = assembled_at.replace(tzinfo=UTC)
    raw_date = document.get("date") or document.get("digest_date")
    if digest_date is not None:
        target_date = digest_date
    elif raw_date:
        target_date = date.fromisoformat(str(raw_date))
    else:
        target_date = assembled_at.date()

    database = database or Database(config.db_path)
    database.initialize()
    database.seed(config, secrets)
    fetch_id = database.start_fetch("ingest", now=assembled_at)

    tweets = _collect_tweets(document)
    new_items = 0
    for payload in tweets:
        if database.store_tweet(payload, fetch_id=fetch_id, fetched_at=assembled_at):
            new_items += 1
    database.finish_fetch(fetch_id, new_items=new_items, now=assembled_at)

    stored = database.get_tweets([str(item["id"]) for item in tweets])
    media_by_id = {str(item["id"]): item.get("media") or [] for item in tweets}
    for row in stored:
        media = media_by_id.get(str(row["id"]))
        if media:
            row["media"] = media

    cluster_drafts = _cluster_drafts(document)
    pick_drafts, commentary_by_id, extras_by_id = _pick_drafts(document)
    digest = assemble_digest(
        digest_date=target_date,
        assembled_at=assembled_at,
        tweets=stored,
        cluster_drafts=cluster_drafts,
        pick_drafts=pick_drafts,
        fetch_new_items=new_items,
    )
    digest = _attach_media_and_commentary(digest, tweets, commentary_by_id, extras_by_id)
    decisions = [
        {
            "tweet_id": pick["tweet_id"],
            "selected": True,
            "tag": pick["tag"],
            "reason": pick["reason"],
        }
        for pick in digest["picks"]
    ]
    database.replace_digest(
        digest_date=target_date,
        assembled_at=assembled_at,
        clusters=digest["clusters"],
        picks=digest["picks"],
        decisions=decisions,
        stats=digest["stats"],
        rendered=digest,
    )
    return IngestResult(
        date=target_date.isoformat(),
        tweet_count=len(stored),
        cluster_count=len(digest["clusters"]),
        pick_count=len(digest["picks"]),
        digest_written=True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument("--date", type=date.fromisoformat)
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, data_dir=args.data_dir)
    secrets = load_secrets(args.secrets, data_dir=config.data_dir)
    result = run_ingest(config, secrets, source=args.file, digest_date=args.date)
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
