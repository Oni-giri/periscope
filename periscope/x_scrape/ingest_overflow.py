"""Store scrape leftovers that did not make Today as a finite Feed batch."""

from __future__ import annotations

import argparse
import time
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from periscope.config import load_config
from periscope.db import Database, isoformat
from periscope.x_scrape.hydrate_shortlist import hydrate_one

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
    """Load foryou/following/timeline_*.json plus topic-account dumps, deduped by
    status_id (first body wins, every ``source_accounts`` tag kept)."""

    from periscope.x_accounts import MAIN_SLUG, merge_sources

    paths: list[tuple[str, str, Path]] = [
        (name.removesuffix(".json"), MAIN_SLUG, dumps_dir / name) for name in SCRAPE_NAMES
    ]
    if dumps_dir.is_dir():
        paths.extend(
            (p.stem.replace("timeline_", ""), MAIN_SLUG, p)
            for p in sorted(dumps_dir.glob("timeline_*.json"))
        )
        paths.extend(
            ("following", p.parent.name, p)
            for p in sorted((dumps_dir / "accounts").glob("*/following.json"))
            if p.parent.name != MAIN_SLUG
        )

    def _load(path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [post for post in data if isinstance(post, dict)] if isinstance(data, list) else []

    ordered, _ = merge_sources((feed, acct, _load(path)) for feed, acct, path in paths)
    return ordered


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
    reply_to = post.get("replying_to_status") or post.get("in_reply_to_status_id_str")
    kind = "reply" if reply_to else "tweet"
    payload: dict[str, Any] = {
        "id": str(post.get("status_id") or post.get("id") or ""),
        "author": str(post.get("author_handle") or post.get("author") or "unknown").removeprefix(
            "@"
        ),
        "created_at": post.get("created_at") or isoformat(),
        "text": post.get("text") or "",
        "urls": urls,
        "kind": kind,
    }
    if media:
        payload["media"] = media
        for url in media:
            if url not in payload["urls"]:
                payload["urls"].append(url)
    avatar = post.get("author_avatar") or post.get("avatar")
    if avatar:
        payload["avatar"] = str(avatar)
    quoted = post.get("quoted_tweet") or post.get("quote")
    if isinstance(quoted, dict):
        payload["quoted_tweet"] = quoted
        qid = str(quoted.get("id") or post.get("quoted_id") or "")
        if qid:
            payload["quoted_id"] = qid
    elif post.get("quoted_id"):
        payload["quoted_id"] = str(post["quoted_id"])
    sources = post.get("source_accounts")
    if isinstance(sources, list) and any(s != "main" for s in sources):
        payload["source_accounts"] = [str(s) for s in sources if s]
    if reply_to:
        payload["in_reply_to_status_id_str"] = str(reply_to)
        payload["replying_to_status"] = str(reply_to)
        payload["thread_root_id"] = str(post.get("thread_root_id") or reply_to)
    return payload


def apply_hydrate(post: dict[str, Any], got: dict[str, Any]) -> None:
    """Merge FxTwitter hydrate fields into a scrape dump post in place."""

    if got.get("text"):
        post["text"] = got["text"]
        post["is_truncated"] = False
    if got.get("image_urls"):
        post["image_urls"] = list(
            dict.fromkeys([*(post.get("image_urls") or []), *got["image_urls"]])
        )
    if got.get("author_avatar") and not post.get("author_avatar"):
        post["author_avatar"] = got["author_avatar"]
    if got.get("quoted_tweet"):
        post["quoted_tweet"] = got["quoted_tweet"]
    if got.get("quoted_id"):
        post["quoted_id"] = got["quoted_id"]
    if got.get("replying_to_status"):
        post["replying_to_status"] = got["replying_to_status"]
        post["in_reply_to_status_id_str"] = got["replying_to_status"]
        post["thread_root_id"] = got.get("thread_root_id") or got["replying_to_status"]


def hydrate_overflow_posts(
    posts: list[dict[str, Any]],
    *,
    all_posts: bool = False,
    sleep_s: float = 0.2,
) -> int:
    """Hydrate truncated leftovers (or all) via FxTwitter. Returns expanded count."""

    todo = [
        post
        for post in posts
        if post.get("status_id")
        and (all_posts or post.get("is_truncated"))
    ]
    if not todo:
        return 0
    expanded = 0
    with httpx.Client(headers={"User-Agent": "Periscope/0.1"}) as client:
        for post in todo:
            sid = str(post["status_id"])
            try:
                got = hydrate_one(client, sid)
            except Exception:
                time.sleep(sleep_s)
                continue
            if not got:
                time.sleep(sleep_s)
                continue
            apply_hydrate(post, got)
            expanded += 1
            time.sleep(sleep_s)
    return expanded


def ingest_overflow(
    database: Database,
    *,
    dumps_dir: Path,
    digest_path: Path | None = None,
    digest_document: dict[str, Any] | None = None,
    now: datetime | None = None,
    hydrate: bool = True,
    hydrate_all: bool = False,
) -> OverflowResult:
    """Persist leftover scrape posts as one fetch after the latest digest.

    When ``hydrate`` is true (default), truncated leftovers are expanded via
    FxTwitter so Feed gets full text, media, quotes, and reply links.
    Pass ``hydrate_all=True`` to hydrate every leftover (slower, richer quotes).
    """

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

    hydrated = 0
    if hydrate:
        hydrated = hydrate_overflow_posts(leftovers, all_posts=hydrate_all)

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
    note = f"hydrated={hydrated}" if hydrate else None
    return OverflowResult(
        fetch_id=fetch_id,
        leftover_count=len(leftovers),
        skipped_ads=skipped_ads,
        skipped_digest=skipped_digest,
        new_items=new_items,
        note=note,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--digest", type=Path)
    parser.add_argument(
        "--hydrate",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Expand truncated leftovers via FxTwitter (default: on)",
    )
    parser.add_argument(
        "--hydrate-all",
        action="store_true",
        help="Hydrate every leftover for quotes/replies (slower)",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, data_dir=args.data_dir)
    database = Database(config.db_path)
    database.initialize()
    result = ingest_overflow(
        database,
        dumps_dir=args.out_dir,
        digest_path=args.digest,
        hydrate=args.hydrate,
        hydrate_all=args.hydrate_all,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
