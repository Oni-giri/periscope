#!/usr/bin/env python3
"""Per-account X actions: like keepers / follow handles on the owning topic account.

Ports the clicker logic from the cron scripts (``like_posts.py``,
``follow_accounts.py``) into the package and runs it with the account's own
Chrome profile. Nothing here runs unless ``periscope-digest --actions`` (or this
CLI without ``--dry-run``) is used. No posting, replies or reposts.

Default rule (``plan_actions``):
- a keeper surfaced by topic account X is liked by X (if X has likes on);
- follow candidates (``follow`` actions) whose topic matches topic account X, or
  that X surfaced, go to X's follow queue; X then drains up to its follow cap.
  Candidates that route to ``main`` are left alone (main keeps its manual queue).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from periscope.x_accounts import (
    MAIN_SLUG,
    XAccount,
    like_plan,
    profile_path,
    route_follow,
)
from periscope.x_scrape.account_scrape import (
    NOT_SIGNED_IN,
    PageOpener,
    check_signed_in,
    open_profile_page,
    profile_problem,
)


def like_one(page: Any, url: str) -> str:
    """Like one tweet URL. Returns liked | already | unsure."""

    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    time.sleep(1.2)
    if page.locator('[data-testid="unlike"]').count():
        return "already"
    page.locator('[data-testid="like"]').first.click(timeout=8000)
    time.sleep(0.6)
    if page.locator('[data-testid="unlike"]').count():
        return "liked"
    return "unsure"


def _follow_one(page: Any, handle: str) -> str:
    from periscope.x_scrape.follow_queued import follow_one

    return follow_one(page, handle)


def _with_account_page(
    account: XAccount,
    *,
    data_dir: Path,
    headless: bool,
    open_page: PageOpener,
    is_signed_in: Callable[[Any], bool],
    check_profile: Callable[[Path], str | None],
    work: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    profile = profile_path(account, data_dir)
    problem = check_profile(profile)
    if problem:
        print(f"{NOT_SIGNED_IN} account={account.slug}: {problem}; skipping actions", flush=True)
        return {"account": account.slug, "status": NOT_SIGNED_IN, "reason": problem}
    try:
        with open_page(profile, headless) as page:
            if not is_signed_in(page):
                print(f"{NOT_SIGNED_IN} account={account.slug}: login wall; skipping", flush=True)
                return {"account": account.slug, "status": NOT_SIGNED_IN, "reason": "login wall"}
            out = work(page)
    except Exception as exc:  # noqa: BLE001 - isolate per account
        print(f"[{account.slug}] actions failed: {exc}", flush=True)
        return {"account": account.slug, "status": "error", "reason": str(exc)[:300]}
    out.setdefault("account", account.slug)
    out.setdefault("status", "ok")
    return out


def like_keepers(
    account: XAccount,
    urls: Sequence[str],
    *,
    data_dir: Path = Path("data"),
    headless: bool = True,
    dry_run: bool = False,
    open_page: PageOpener = open_profile_page,
    is_signed_in: Callable[[Any], bool] = check_signed_in,
    check_profile: Callable[[Path], str | None] = profile_problem,
    like: Callable[[Any, str], str] = like_one,
    pause: float = 0.8,
) -> dict[str, Any]:
    urls = [str(u).strip() for u in urls if str(u).strip()]
    if not urls:
        return {"account": account.slug, "status": "ok", "liked": 0, "already": 0, "failed": 0}
    if not account.like_enabled:
        print(f"[{account.slug}] likes disabled; skip {len(urls)}", flush=True)
        return {"account": account.slug, "status": "disabled", "skipped": len(urls)}
    if dry_run:
        for url in urls:
            print(f"[{account.slug}] would like {url}", flush=True)
        return {"account": account.slug, "status": "dry-run", "would_like": urls}

    def work(page: Any) -> dict[str, Any]:
        counts = {"liked": 0, "already": 0, "failed": 0}
        for url in urls:
            try:
                outcome = like(page, url)
            except Exception as exc:  # noqa: BLE001
                outcome = "failed"
                print(f"[{account.slug}] like fail {url}: {exc}", flush=True)
            key = outcome if outcome in counts else "failed"
            counts[key] += 1
            print(f"[{account.slug}] like {outcome} {url}", flush=True)
            if pause:
                time.sleep(pause)
        return counts

    return _with_account_page(
        account,
        data_dir=data_dir,
        headless=headless,
        open_page=open_page,
        is_signed_in=is_signed_in,
        check_profile=check_profile,
        work=work,
    )


def follow_handles(
    account: XAccount,
    handles: Sequence[str],
    *,
    data_dir: Path = Path("data"),
    database: Any | None = None,
    headless: bool = True,
    dry_run: bool = False,
    open_page: PageOpener = open_profile_page,
    is_signed_in: Callable[[Any], bool] = check_signed_in,
    check_profile: Callable[[Path], str | None] = profile_problem,
    follow: Callable[[Any, str], str] = _follow_one,
) -> dict[str, Any]:
    """Follow up to ``account.follow_cap`` handles on this account's login.

    With ``database`` the follow_queue rows are marked followed/already/failed.
    """

    from periscope.x_scrape.follow_queued import queue_status_for_result

    cap = max(0, int(account.follow_cap))
    clean = []
    for handle in handles:
        h = str(handle or "").strip().removeprefix("@").lower()
        if h and h not in clean:
            clean.append(h)
    clean = clean[:cap]
    if not clean:
        return {"account": account.slug, "status": "ok", "outcomes": []}
    if dry_run:
        for h in clean:
            print(f"[{account.slug}] would follow @{h}", flush=True)
        return {"account": account.slug, "status": "dry-run", "would_follow": clean}

    def work(page: Any) -> dict[str, Any]:
        outcomes = []
        for h in clean:
            try:
                status, error = queue_status_for_result(follow(page, h))
            except Exception as exc:  # noqa: BLE001
                status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            if database is not None:
                try:
                    database.mark_follow(h, status, error=error)
                except Exception as exc:  # noqa: BLE001
                    print(f"[{account.slug}] mark_follow failed: {exc}", flush=True)
            print(f"[{account.slug}] follow @{h}: {status}", flush=True)
            outcomes.append({"handle": h, "status": status})
        return {"outcomes": outcomes}

    return _with_account_page(
        account,
        data_dir=data_dir,
        headless=headless,
        open_page=open_page,
        is_signed_in=is_signed_in,
        check_profile=check_profile,
        work=work,
    )


# ---------------------------------------------------------------------------
# Planning from a digest document


def _pick_follow_handles(pick: Mapping[str, Any], author: str) -> list[str]:
    from periscope.web.routes.follow_queue import parse_follow_handle

    handles = []
    for action in pick.get("actions") or []:
        if not isinstance(action, Mapping) or action.get("type") != "follow":
            continue
        handle = parse_follow_handle(
            url=action.get("url"), label=action.get("label"), fallback=author
        )
        if handle and handle not in handles:
            handles.append(handle)
    return handles


def keepers_from_digest(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Flatten digest picks into keeper dicts: url, sources, topic, follow handles."""

    tweets = {str(t.get("id")): t for t in document.get("tweets") or [] if isinstance(t, Mapping)}
    keepers = []
    for pick in document.get("picks") or []:
        if not isinstance(pick, Mapping):
            continue
        sid = str(pick.get("tweet_id") or "")
        tweet = tweets.get(sid) or {}
        author = str(tweet.get("author") or "").removeprefix("@")
        url = next((u for u in tweet.get("urls") or [] if "/status/" in str(u)), None)
        if not url and author and sid:
            url = f"https://x.com/{author}/status/{sid}"
        sources = list(pick.get("source_accounts") or tweet.get("source_accounts") or [])
        keepers.append(
            {
                "status_id": sid,
                "tweet_url": url,
                "author": author,
                "topic": pick.get("topic") or "",
                "source_accounts": sources or [MAIN_SLUG],
                "follow_handles": _pick_follow_handles(pick, author),
            }
        )
    return keepers


def plan_actions(
    accounts: Sequence[XAccount], keepers: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Return ``{"likes": {slug: [urls]}, "follows": {slug: [(handle, tweet_id)]}}``."""

    follows: dict[str, list[tuple[str, str]]] = {}
    for keeper in keepers:
        owner = route_follow(
            accounts, topic=keeper.get("topic"), source_accounts=keeper.get("source_accounts")
        )
        if owner == MAIN_SLUG:
            continue
        for handle in keeper.get("follow_handles") or []:
            bucket = follows.setdefault(owner, [])
            if all(h != handle for h, _ in bucket):
                bucket.append((handle, str(keeper.get("status_id") or "")))
    return {"likes": like_plan(accounts, keepers), "follows": follows}


def run_account_actions(
    database: Any,
    document: Mapping[str, Any],
    *,
    data_dir: Path,
    headless: bool = True,
    dry_run: bool = False,
    like: Callable[[Any, str], str] | None = None,
    follow: Callable[[Any, str], str] | None = None,
    pause: float = 0.8,
    **page_kwargs: Any,
) -> list[dict[str, Any]]:
    """Enqueue routed follows, then per topic account: like keepers + drain queue.

    ``page_kwargs`` (open_page / is_signed_in / check_profile) and ``like`` /
    ``follow`` exist for tests; production uses the Playwright defaults.
    """

    accounts = [XAccount.from_row(row) for row in database.list_x_accounts(enabled_only=True)]
    topic_accounts = {a.slug: a for a in accounts if not a.is_main}
    if not topic_accounts:
        print("actions: no enabled topic accounts; nothing to click", flush=True)
        return []
    plan = plan_actions(accounts, keepers_from_digest(document))
    if not dry_run:
        for slug, items in plan["follows"].items():
            for handle, tweet_id in items:
                database.enqueue_follow(
                    handle, tweet_id=tweet_id or None, source="digest", account=slug
                )
    results = []
    for slug, account in topic_accounts.items():
        urls = plan["likes"].get(slug, [])
        if urls:
            results.append(
                {
                    "kind": "like",
                    **like_keepers(
                        account,
                        urls,
                        data_dir=data_dir,
                        headless=headless,
                        dry_run=dry_run,
                        like=like or like_one,
                        pause=pause,
                        **page_kwargs,
                    ),
                }
            )
        if dry_run:
            handles = [h for h, _ in plan["follows"].get(slug, [])]
        else:
            handles = [str(r["handle"]) for r in database.list_pending_follows(slug)]
        if handles:
            results.append(
                {
                    "kind": "follow",
                    **follow_handles(
                        account,
                        handles,
                        data_dir=data_dir,
                        database=None if dry_run else database,
                        headless=headless,
                        dry_run=dry_run,
                        follow=follow or _follow_one,
                        **page_kwargs,
                    ),
                }
            )
    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--digest", type=Path, required=True)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--db", type=Path, default=None)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    from periscope.db import Database

    db_path = args.db or (args.data_dir / "periscope.db")
    database = Database(db_path)
    database.initialize()
    document = json.loads(args.digest.read_text(encoding="utf-8"))
    results = run_account_actions(
        database,
        document,
        data_dir=args.data_dir,
        headless=args.headless,
        dry_run=args.dry_run,
    )
    print(json.dumps(results, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
