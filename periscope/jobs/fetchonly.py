"""Fetch an X list timeline and persist source payloads before downstream work."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from periscope.config import AppConfig, Secrets, load_config, load_secrets
from periscope.db import Database
from periscope.telegram.bot import Notifier, build_notifier
from periscope.xclient import (
    CookieDeadError,
    MockXClient,
    TwscrapeXClient,
    XClient,
    embedded_tweets,
    payload_id,
    payload_thread_root,
)


@dataclass(frozen=True, slots=True)
class FetchResult:
    fetch_id: int
    seen_items: int
    new_items: int
    errors: int
    note: str | None = None


def build_xclient(
    config: AppConfig,
    secrets: Secrets,
    *,
    mock_x: str | Path | None = None,
) -> XClient:
    if mock_x is not None:
        return MockXClient(mock_x)
    return TwscrapeXClient(config.x, secrets, pool_path=config.twscrape_db_path)


async def run_fetch(
    config: AppConfig,
    secrets: Secrets,
    *,
    database: Database | None = None,
    xclient: XClient | None = None,
    notifier: Notifier | None = None,
    mock_x: str | Path | None = None,
    now: datetime | None = None,
) -> FetchResult:
    database = database or Database(config.db_path)
    database.initialize()
    database.seed(config, secrets)
    client = xclient or build_xclient(config, secrets, mock_x=mock_x)
    notifier = notifier or build_notifier(secrets, config.delivery)
    fetch_id = database.start_fetch("fetchonly", now=now)
    fetched_at = now
    seen_ids: set[str] = set()
    expanded_roots: set[str] = set()
    new_items = 0

    def persist(payload: dict) -> None:
        nonlocal new_items
        item_id = payload_id(payload)
        if not item_id or item_id in seen_ids:
            return
        seen_ids.add(item_id)
        if database.store_tweet(payload, fetch_id=fetch_id, fetched_at=fetched_at):
            new_items += 1

    try:
        async for tweet in client.timeline(limit=config.x.fetch_limit):
            # Persist the fetched item before inspecting embedded or threaded context.
            persist(tweet)
            for related in embedded_tweets(tweet):
                persist(related)

            if config.x.resolve_threads:
                root = payload_thread_root(tweet)
                if root and root not in expanded_roots:
                    expanded_roots.add(root)
                    async for part in client.thread(tweet, depth=config.x.thread_depth):
                        persist(part)
                        for related in embedded_tweets(part):
                            persist(related)
    except CookieDeadError as exc:
        note = str(exc)
        database.finish_fetch(
            fetch_id,
            new_items=new_items,
            errors=1,
            note=note,
            now=now,
        )
        if database.open_cookie_incident():
            await notifier.send_alert(
                "X cookies were rejected. Update auth_token and ct0 in secrets.env."
            )
        return FetchResult(fetch_id, len(seen_ids), new_items, 1, note)
    except Exception as exc:
        note = str(exc)
        database.finish_fetch(
            fetch_id,
            new_items=new_items,
            errors=1,
            note=note,
            now=now,
        )
        if database.record_job_failure("fetchonly", note):
            await notifier.send_alert(f"Fetch failed: {note}")
        return FetchResult(fetch_id, len(seen_ids), new_items, 1, note)

    database.finish_fetch(fetch_id, new_items=new_items, now=now)
    database.recover_cookie_incident()
    return FetchResult(fetch_id, len(seen_ids), new_items, 0)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument(
        "--mock-x",
        type=Path,
        metavar="FIXTURE",
        help="load an anonymized JSON fixture instead of contacting X",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, data_dir=args.data_dir)
    secrets = load_secrets(args.secrets, data_dir=config.data_dir)
    result = asyncio.run(run_fetch(config, secrets, mock_x=args.mock_x))
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":  # pragma: no cover
    main()
