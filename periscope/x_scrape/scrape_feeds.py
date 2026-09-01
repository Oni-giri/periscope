#!/usr/bin/env python3
"""Dump signed-in X For You / Following / custom timeline feeds to JSON. No posting, no likes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .watermark import load_watermark

HOME = "https://x.com/home"
EXTRACT_JS = r"""
() => {
  const articles = [...document.querySelectorAll('article[data-testid="tweet"], article')];
  const out = [];
  const seen = new Set();
  for (const a of articles) {
    const links = [...a.querySelectorAll('a[href*="/status/"]')]
      .map(x => x.getAttribute('href') || '')
      .filter(Boolean);
    const statusLink = links.find(h => /\/status\/\d+/.test(h) && !h.includes('/analytics') && !h.includes('/photo'));
    if (!statusLink) continue;
    const m = statusLink.match(/\/([^\/]+)\/status\/(\d+)/);
    if (!m) continue;
    const handle = m[1];
    const status_id = m[2];
    if (seen.has(status_id)) continue;
    seen.add(status_id);
    const textEl = a.querySelector('[data-testid="tweetText"]');
    const text = textEl ? textEl.innerText : '';
    const timeEl = a.querySelector('time');
    const created_at = timeEl ? (timeEl.getAttribute('datetime') || null) : null;
    const nameEl = a.querySelector('[data-testid="User-Name"] span');
    const author_name = nameEl ? nameEl.innerText.trim() : handle;
    const imgs = [...a.querySelectorAll('img')]
      .map(i => i.src)
      .filter(s => s && s.includes('pbs.twimg.com/media/'));
    // Prefer the author's profile image (not tweet media).
    const avatarImg = a.querySelector('img[src*="profile_images"]');
    const author_avatar = avatarImg && avatarImg.src ? avatarImg.src : null;
    const blob = (a.innerText || '').toLowerCase();
    const is_ad = /\b(ad|promoted|promoted by)\b/.test(blob) || !!a.querySelector('[data-testid="placementTracking"]');
    const is_truncated = /\bshow more\b/i.test(a.innerText || '') || text.endsWith('…') || text.endsWith('...');
    out.push({
      status_id,
      author_name,
      author_handle: '@' + handle,
      author_avatar,
      text,
      tweet_url: 'https://x.com/' + handle + '/status/' + status_id,
      created_at,
      image_urls: [...new Set(imgs)],
      is_ad,
      is_truncated,
    });
  }
  return out;
}
"""


def signed_in(page) -> bool:
    url = page.url or ""
    if "login" in url or "/i/flow/login" in url:
        return False
    try:
        page.get_by_role("tab", name="For you").wait_for(timeout=8000)
        return True
    except PlaywrightTimeout:
        return False


def click_tab(page, name: str) -> None:
    tab = page.get_by_role("tab", name=name)
    tab.click()
    page.wait_for_timeout(1500)


def collect(
    page,
    *,
    min_unique: int | None,
    watermark_ids: set[str],
    max_scrolls: int,
    stall_limit: int,
) -> list[dict]:
    by_id: dict[str, dict] = {}
    stalled = 0
    hit_water = 0
    for i in range(max_scrolls):
        batch = page.evaluate(EXTRACT_JS)
        new = 0
        for item in batch:
            sid = item["status_id"]
            if sid in by_id:
                continue
            by_id[sid] = item
            new += 1
            if watermark_ids and sid in watermark_ids:
                hit_water += 1
        if new == 0:
            stalled += 1
        else:
            stalled = 0
        n = len(by_id)
        print(f"  scroll {i + 1}: +{new} (unique {n})", flush=True)
        reached_min = min_unique is None or n >= min_unique
        # For You: stop at min. Following: keep past min until watermark (full catch-up),
        # but do not stop on watermark before min unique posts.
        if not watermark_ids and min_unique is not None and n >= min_unique:
            break
        if watermark_ids and hit_water >= 4 and reached_min:
            print("  watermark hit", flush=True)
            break
        if stalled >= stall_limit:
            print("  feed stalled", flush=True)
            break
        page.evaluate("window.scrollBy(0, Math.floor(window.innerHeight * 0.9))")
        page.wait_for_timeout(1200)
        # nudge the timeline loader
        page.keyboard.press("PageDown")
        page.wait_for_timeout(400)
    return list(by_id.values())


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=Path("data/x-dumps"))
    p.add_argument("--profile", type=Path, default=Path("data/chrome-profile"))
    p.add_argument("--watermark", type=Path, default=Path("data/following_watermark.json"))
    p.add_argument("--min-foryou", type=int, default=200)
    p.add_argument("--min-following", type=int, default=200)
    p.add_argument("--max-scrolls", type=int, default=160)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--skip-foryou", action="store_true")
    p.add_argument("--skip-following", action="store_true")
    p.add_argument("--skip-timelines", action="store_true")
    p.add_argument(
        "--timelines",
        default="Tech,Crypto,Business",
        help="Comma-separated home tab names for Grok custom timelines",
    )
    p.add_argument("--min-timeline", type=int, default=100)
    p.add_argument(
        "--db",
        type=Path,
        default=Path("data/periscope.db"),
        help="Periscope SQLite path for draining the follow queue",
    )
    p.add_argument("--follow-limit", type=int, default=15)
    p.add_argument("--skip-follows", action="store_true")
    args = p.parse_args()

    args.profile.mkdir(parents=True, exist_ok=True)
    watermark_ids, _ = load_watermark(args.watermark if args.watermark.exists() else None)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(args.profile),
            channel="chrome",
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        if not signed_in(page):
            print("NOT_SIGNED_IN: log in once in this Chrome profile, then rerun.", flush=True)
            write_json(
                args.out_dir / "scrape_meta.json",
                {"signed_in": False, "error": "login wall"},
            )
            # leave the window up a bit if headed
            if not args.headless:
                print("Waiting 120s for a manual login…", flush=True)
                page.wait_for_timeout(120000)
                if not signed_in(page):
                    ctx.close()
                    return 2
            else:
                ctx.close()
                return 2

        meta = {"signed_in": True, "foryou": None, "following": None, "timelines": {}}

        if not args.skip_foryou:
            print("For You", flush=True)
            click_tab(page, "For you")
            posts = collect(
                page,
                min_unique=args.min_foryou,
                watermark_ids=set(),
                max_scrolls=args.max_scrolls,
                stall_limit=8,
            )
            write_json(args.out_dir / "foryou.json", posts)
            meta["foryou"] = {
                "unique_count": len(posts),
                "ads": sum(1 for x in posts if x.get("is_ad")),
                "truncated": sum(1 for x in posts if x.get("is_truncated")),
            }
            print("wrote foryou", meta["foryou"], flush=True)

        if not args.skip_following:
            print("Following", flush=True)
            click_tab(page, "Following")
            posts = collect(
                page,
                min_unique=args.min_following,
                watermark_ids=watermark_ids,
                max_scrolls=max(args.max_scrolls, 180),
                stall_limit=10,
            )
            write_json(args.out_dir / "following.json", posts)
            meta["following"] = {
                "unique_count": len(posts),
                "ads": sum(1 for x in posts if x.get("is_ad")),
                "truncated": sum(1 for x in posts if x.get("is_truncated")),
                "watermark_ids": len(watermark_ids),
                "hit_watermark": bool(watermark_ids)
                and sum(1 for x in posts if x["status_id"] in watermark_ids) >= 1,
            }
            print("wrote following", meta["following"], flush=True)

        if not args.skip_timelines:
            names = [n.strip() for n in args.timelines.split(",") if n.strip()]
            for name in names:
                slug = name.lower().replace(" ", "_")
                print("Timeline", name, flush=True)
                try:
                    click_tab(page, name)
                except PlaywrightTimeout:
                    print(f"  TAB_MISSING: {name}", flush=True)
                    meta["timelines"][slug] = {"error": "tab missing", "tab": name}
                    continue
                posts = collect(
                    page,
                    min_unique=args.min_timeline,
                    watermark_ids=set(),
                    max_scrolls=args.max_scrolls,
                    stall_limit=8,
                )
                for item in posts:
                    item["feed"] = slug
                write_json(args.out_dir / f"timeline_{slug}.json", posts)
                meta["timelines"][slug] = {
                    "tab": name,
                    "unique_count": len(posts),
                    "ads": sum(1 for x in posts if x.get("is_ad")),
                    "truncated": sum(1 for x in posts if x.get("is_truncated")),
                }
                print("wrote timeline", slug, meta["timelines"][slug], flush=True)

        write_json(args.out_dir / "scrape_meta.json", meta)

        if not args.skip_follows:
            from .follow_queued import drain_follow_queue

            drain_follow_queue(page, args.db, limit=args.follow_limit)

        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
