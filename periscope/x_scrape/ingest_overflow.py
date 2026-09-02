"""Store scrape leftovers that did not make Today as a finite Feed batch."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from periscope.config import load_config
from periscope.db import Database, isoformat

SCRAPE_NAMES = ("foryou.json", "following.json")


@dataclass(frozen=True, slots=True)
class OverflowResult:
    fetch_id: int | None
    leftover_count: int
    skipped_ads: int
    skipped_digest: int
    new_items: int = 0
    note: str | None = None


def _parse_dt(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def overflow_fetch_now(
    assembled_at: datetime | str | None,
    now: datetime | None = None,
) -> datetime:
    """Return a fetch timestamp strictly after digest assembled_at (ISO seconds)."""

    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    stamp = stamp.astimezone(UTC)
    boundary = _parse_dt(assembled_at)
    if boundary is not None and isoformat(stamp) <= isoformat(boundary):
        stamp = boundary + timedelta(seconds=1)
    return stamp


def load_scrape_posts(dumps_dir: Path) -> list[dict[str, Any]]:
    """Load foryou/following/timeline_*.json, deduped by status_id."""

    paths = [dumps_dir / name for name in SCRAPE_NAMES]
    if dumps_dir.is_dir():
        paths.extend(sorted(dumps_dir.glob("timeline_*.json")))
    by_id: dict[str, dict[str, Any]] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, list):
            continue
        for post in data:
            if not isinstance(post, dict):
                continue
            sid = str(post.get("status_id") or "")
            if not sid or sid in by_id:
                continue
            by_id[sid] = post
    return list(by_id.values())


def digest_keeper_ids(document: dict[str, Any] | None) -> set[str]:
    ids: set[str] = set()
    if not document:
        return ids
    for pick in document.get("picks") or []:
        if not isinstance(pick, dict):
            continue
        tweet_id = str(pick.get("tweet_id") or (pick.get("tweet") or {}).get("id") or "")
        if tweet_id:
            ids.add(tweet_id)
    for cluster in document.get("clusters") or []:
        if not isinstance(cluster, dict):
            continue
        for item in cluster.get("tweet_ids") or []:
            if item:
                ids.add(str(item))
        for nested in cluster.get("tweets") or []:
            if not isinstance(nested, dict):
                continue
            nid = str(nested.get("id") or "")
            if nid:
                ids.add(nid)
    return ids


def load_digest_document(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def scrape_post_to_payload(post: dict[str, Any]) -> dict[str, Any]:
    """Map a scrape dump post to the ingest tweet payload shape."""

    urls: list[str] = []
    tweet_url = post.get("tweet_url")
    if tweet_url:
        urls.append(str(tweet_url))
    media = [str(url) for url in (post.get("image_urls") or post.get("media") or []) if url]
    payload: dict[str, Any] = {
        "id": str(post.get("status_id") or post.get("id") or ""),
        "author": str(post.get("author_handle") or post.get("author") or "unknown").removeprefix(
            "@"
        ),
        "created_at": post.get("created_at") or isoformat(),
        "text": post.get("text") or "",
        "urls": urls,
        "kind": "tweet",
    }
    if media:
        payload["media"] = media
        for url in media:
            if url not in payload["urls"]:
                payload["urls"].append(url)
    avatar = post.get("author_avatar") or post.get("avatar")
    if avatar:
        payload["avatar"] = str(avatar)
    return payload


def ingest_overflow(
    database: Database,
    *,
    dumps_dir: Path,
    digest_path: Path | None = None,
    digest_document: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> OverflowResult:
    """Persist leftover scrape posts as one fetch after the latest digest."""

    document = digest_document if digest_document is not None else load_digest_document(digest_path)
    exclude = digest_keeper_ids(document)
    latest = database.latest_digest()
    if latest is not None:
        exclude |= digest_keeper_ids(latest.get("rendered") if isinstance(latest, dict) else None)
        assembled_at = latest.get("assembled_at")
    else:
        assembled_at = None

    skipped_ads = 0
    skipped_digest = 0
    leftovers: list[dict[str, Any]] = []
    for post in load_scrape_posts(dumps_dir):
        sid = str(post.get("status_id") or "")
        if post.get("is_ad"):
            skipped_ads += 1
            continue
        if sid in exclude:
            skipped_digest += 1
            continue
        leftovers.append(post)

    if not leftovers:
        return OverflowResult(
            fetch_id=None,
            leftover_count=0,
            skipped_ads=skipped_ads,
            skipped_digest=skipped_digest,
            note="no leftovers",
        )

    stamp = overflow_fetch_now(assembled_at, now)
    fetch_id = database.start_fetch("scrape", now=stamp)
    new_items = 0
    for post in leftovers:
        payload = scrape_post_to_payload(post)
        if not payload["id"]:
            continue
        if database.store_tweet(payload, fetch_id=fetch_id, fetched_at=stamp):
            new_items += 1
    database.finish_fetch(fetch_id, new_items=new_items, now=stamp)
    return OverflowResult(
        fetch_id=fetch_id,
        leftover_count=len(leftovers),
        skipped_ads=skipped_ads,
        skipped_digest=skipped_digest,
        new_items=new_items,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--digest", type=Path)
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, data_dir=args.data_dir)
    database = Database(config.db_path)
    database.initialize()
    result = ingest_overflow(database, dumps_dir=args.out_dir, digest_path=args.digest)
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
