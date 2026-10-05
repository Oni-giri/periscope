#!/usr/bin/env python3
"""Scrape each enabled topic account's Following timeline to its own watermark.

Each topic account is a separate X login with its own Chromium profile. Output
goes to ``<out-dir>/accounts/<slug>/following.json`` with every post tagged
``source_account``. A signed-out or locked profile logs ``NOT_SIGNED_IN`` for
that account and is skipped; other accounts continue. The main account is not
touched here (``scrape_feeds`` handles it unchanged). No posting, no likes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from periscope.x_accounts import (
    XAccount,
    account_dump_dir,
    profile_path,
    profile_ready,
    watermark_path,
    write_watermark,
)
from periscope.x_scrape.watermark import load_watermark

NOT_SIGNED_IN = "NOT_SIGNED_IN"

# open_page(profile_dir, headless) -> context manager yielding a Playwright page
PageOpener = Callable[[Path, bool], AbstractContextManager[Any]]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def open_profile_page(profile: Path, headless: bool) -> Iterator[Any]:
    """Launch a persistent Chrome context on ``profile`` and yield its page."""

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            yield page
        finally:
            ctx.close()


def profile_problem(profile: Path) -> str | None:
    """Return a reason the profile cannot be used, or None if it looks usable."""

    from periscope.web.chrome_profile import profile_in_use

    if not profile_ready(profile):
        return "profile missing or never signed in"
    if profile_in_use(profile):
        return "profile locked (Chrome still has it open)"
    return None


def check_signed_in(page: Any) -> bool:
    from periscope.x_scrape.scrape_feeds import HOME, signed_in

    page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    return bool(signed_in(page))


def _default_collect(page: Any, *, min_unique: int, watermark_ids: set[str], max_scrolls: int):
    from periscope.x_scrape.scrape_feeds import click_tab, collect

    click_tab(page, "Following")
    return collect(
        page,
        min_unique=min_unique,
        watermark_ids=watermark_ids,
        max_scrolls=max(max_scrolls, 180),
        stall_limit=10,
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def scrape_account(
    account: XAccount,
    *,
    data_dir: Path,
    out_dir: Path,
    database: Any | None = None,
    min_following: int | None = None,
    max_scrolls: int = 160,
    headless: bool = True,
    open_page: PageOpener = open_profile_page,
    is_signed_in: Callable[[Any], bool] = check_signed_in,
    collect_following: Callable[..., list[dict]] = _default_collect,
    check_profile: Callable[[Path], str | None] = profile_problem,
    login_wait_ms: int = 120000,
) -> dict[str, Any]:
    """Scrape one topic account. Never raises; returns a status dict.

    Headless runs skip a missing/locked/signed-out profile immediately. Headed
    runs (no ``--headless``) open Chrome and wait ``login_wait_ms`` for a manual
    sign-in, like ``scrape_feeds`` does for the main account.
    """

    slug = account.slug
    dump_dir = account_dump_dir(out_dir, slug)
    following_path = dump_dir / "following.json"
    # Never let a previous run's dump leak into today's merge when we skip.
    if following_path.exists():
        following_path.unlink()
    profile = profile_path(account, data_dir)
    result: dict[str, Any] = {
        "account": slug,
        "label": account.label,
        "profile": str(profile),
        "status": "ok",
        "count": 0,
        "at": _now(),
    }

    def _finish(status: str, *, signed: bool | None, reason: str | None = None) -> dict:
        result["status"] = status
        if reason:
            result["reason"] = reason
        _write_json(dump_dir / "scrape_meta.json", result)
        if database is not None:
            try:
                label = status if not reason else f"{status}: {reason}"
                database.record_x_account_scrape(
                    slug,
                    status=label,
                    count=result.get("count") if status == "ok" else None,
                    signed_in=signed,
                    at=result["at"],
                )
            except Exception as exc:  # noqa: BLE001 - status is best-effort
                print(f"[{slug}] status write failed: {exc}", flush=True)
        return result

    problem = check_profile(profile)
    locked = bool(problem) and "locked" in str(problem)
    if problem and (headless or locked):
        print(f"{NOT_SIGNED_IN} account={slug}: {problem} ({profile}); skipping", flush=True)
        return _finish(NOT_SIGNED_IN, signed=None if locked else False, reason=problem)
    if problem:
        # Headed run on a fresh profile: open Chrome so the user can sign in.
        print(f"[{slug}] {problem}; opening a visible Chrome for sign-in", flush=True)
        profile.mkdir(parents=True, exist_ok=True)

    wm_path = watermark_path(account, data_dir)
    try:
        watermark_ids, _ = load_watermark(wm_path if wm_path.exists() else None)
    except (OSError, ValueError) as exc:
        print(f"[{slug}] watermark unreadable ({exc}); full catch-up", flush=True)
        watermark_ids = set()
    minimum = int(min_following if min_following is not None else account.min_following)

    try:
        with open_page(profile, headless) as page:
            signed = is_signed_in(page)
            if not signed and not headless and login_wait_ms > 0:
                print(
                    f"{NOT_SIGNED_IN} account={slug}: waiting {login_wait_ms // 1000}s "
                    "for a manual login in the visible window…",
                    flush=True,
                )
                page.wait_for_timeout(login_wait_ms)
                signed = is_signed_in(page)
            if not signed:
                print(
                    f"{NOT_SIGNED_IN} account={slug}: login wall; sign in once in "
                    f"{profile} then rerun. Skipping.",
                    flush=True,
                )
                return _finish(NOT_SIGNED_IN, signed=False, reason="login wall")
            print(f"[{slug}] Following (min {minimum}, watermark {len(watermark_ids)})", flush=True)
            posts = collect_following(
                page,
                min_unique=minimum,
                watermark_ids=watermark_ids,
                max_scrolls=max_scrolls,
            )
    except Exception as exc:  # noqa: BLE001 - one account must not break the rest
        message = f"{type(exc).__name__}: {exc}"[:300]
        lowered = message.lower()
        if "singleton" in lowered or "user data directory is already in use" in lowered:
            print(f"{NOT_SIGNED_IN} account={slug}: profile locked; skipping", flush=True)
            return _finish(NOT_SIGNED_IN, signed=None, reason="profile locked")
        print(f"[{slug}] scrape failed: {message}; skipping", flush=True)
        return _finish("error", signed=None, reason=message)

    tagged = []
    for post in posts:
        item = dict(post)
        item["source_account"] = slug
        item["feed"] = "following"
        tagged.append(item)
    hit = bool(watermark_ids) and any(p.get("status_id") in watermark_ids for p in tagged)
    _write_json(following_path, tagged)
    if tagged:
        write_watermark(wm_path, tagged, hit=hit)
    result["count"] = len(tagged)
    result["hit_watermark"] = hit
    result["ads"] = sum(1 for p in tagged if p.get("is_ad"))
    print(f"[{slug}] wrote {len(tagged)} posts hit_watermark={hit}", flush=True)
    return _finish("ok", signed=True)


def scrape_topic_accounts(
    accounts: Sequence[XAccount],
    *,
    data_dir: Path,
    out_dir: Path,
    database: Any | None = None,
    only: Sequence[str] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Scrape every enabled non-main account in turn; failures are isolated."""

    wanted = {s.strip().lower() for s in only or [] if s.strip()}
    results: list[dict[str, Any]] = []
    for account in accounts:
        if account.is_main or not account.enabled:
            continue
        if wanted and account.slug not in wanted:
            continue
        try:
            results.append(
                scrape_account(
                    account, data_dir=data_dir, out_dir=out_dir, database=database, **kwargs
                )
            )
        except Exception as exc:  # noqa: BLE001 - belt and braces
            print(f"[{account.slug}] unexpected failure: {exc}; skipping", flush=True)
            results.append({"account": account.slug, "status": "error", "reason": str(exc)})
    _write_json(
        Path(out_dir) / "accounts" / "accounts_meta.json", {"at": _now(), "results": results}
    )
    return results


def load_accounts(db_path: Path) -> tuple[Any | None, list[XAccount]]:
    """Open the Periscope DB (initializing migrations) and load X accounts."""

    from periscope.db import Database

    if not Path(db_path).is_file():
        return None, []
    database = Database(db_path)
    database.initialize()
    return database, [XAccount.from_row(row) for row in database.list_x_accounts()]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, default=Path("data"))
    p.add_argument("--db", type=Path, default=None, help="default: <data-dir>/periscope.db")
    p.add_argument("--out-dir", type=Path, default=Path("data/x-dumps"))
    p.add_argument(
        "--min-following",
        type=int,
        default=None,
        help="Override each account's min unique posts (account default 100)",
    )
    p.add_argument("--max-scrolls", type=int, default=160)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--account", action="append", default=[], help="Only these slugs")
    args = p.parse_args()

    db_path = args.db or (args.data_dir / "periscope.db")
    database, accounts = load_accounts(db_path)
    topic = [a for a in accounts if not a.is_main and a.enabled]
    if not topic:
        print("no enabled topic accounts; nothing to scrape", flush=True)
        return 0
    results = scrape_topic_accounts(
        accounts,
        data_dir=args.data_dir,
        out_dir=args.out_dir,
        database=database,
        only=args.account,
        min_following=args.min_following,
        max_scrolls=args.max_scrolls,
        headless=args.headless,
    )
    ok = sum(1 for r in results if r.get("status") == "ok")
    print(f"topic accounts scraped ok={ok}/{len(results)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
