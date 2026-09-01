"""Follow pending X handles using the signed-in Playwright Chrome profile.

The web process never talks to X. Today queues a handle; scrape_feeds drains
the SQLite follow_queue after feed collection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from periscope.db import Database

FOLLOW_CAP = 15
_RESULT_TO_STATUS = {
    "followed": "followed",
    "clicked": "followed",
    "already": "already",
    "pending": "already",
}


def follow_one(page: Any, handle: str) -> str:
    handle = handle.lstrip("@")
    page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    following = page.get_by_role("button", name=re.compile(r"^Following$", re.I))
    if following.count() and following.first.is_visible():
        return "already"
    pending = page.get_by_role("button", name=re.compile(r"Pending|Requested", re.I))
    if pending.count() and pending.first.is_visible():
        return "pending"
    follow = page.get_by_role("button", name=re.compile(rf"^Follow( @{handle})?$", re.I))
    if follow.count() == 0:
        follow = page.locator('[data-testid$="-follow"]')
    if follow.count() == 0:
        return "no-button"
    follow.first.click()
    page.wait_for_timeout(1500)
    if page.get_by_role("button", name=re.compile(r"^Following$", re.I)).count():
        return "followed"
    return "clicked"


def queue_status_for_result(result: str) -> tuple[str, str | None]:
    status = _RESULT_TO_STATUS.get(result)
    if status:
        return status, None
    return "failed", result or "failed"


def drain_pending(
    page: Any,
    database: Database,
    *,
    limit: int = FOLLOW_CAP,
) -> list[dict[str, str]]:
    pending = database.list_pending_follows()[: max(0, int(limit))]
    if not pending:
        return []
    outcomes: list[dict[str, str]] = []
    for row in pending:
        handle = str(row["handle"])
        try:
            raw = follow_one(page, handle)
            status, error = queue_status_for_result(raw)
        except PlaywrightTimeout:
            status, error = "failed", "timeout"
        except Exception as exc:  # noqa: BLE001
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
        database.mark_follow(handle, status, error=error)
        print(f"queued-follow @{handle}: {status}", flush=True)
        outcomes.append({"handle": handle, "status": status})
    return outcomes


def drain_follow_queue(
    page: Any,
    db_path: str | Path | None,
    *,
    limit: int = FOLLOW_CAP,
) -> list[dict[str, str]]:
    """Open Periscope DB if present and follow pending handles. Never raises."""

    if db_path is None:
        print("queued-follow: no database path, skip", flush=True)
        return []
    path = Path(db_path)
    if not path.is_file():
        print(f"queued-follow: db missing ({path}), skip", flush=True)
        return []
    try:
        database = Database(path)
        database.initialize()
        pending = database.list_pending_follows()
        if not pending:
            return []
        return drain_pending(page, database, limit=limit)
    except Exception as exc:  # noqa: BLE001
        print(f"queued-follow: drain failed {exc}", flush=True)
        return []
