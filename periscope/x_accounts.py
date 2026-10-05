"""Topic X accounts: model, profile/watermark paths, source merge and routing.

Each topic account is its own X login (own Chromium profile) that only follows
and likes posts for one topic. The ``main`` account is the user's mixed
account and maps to the existing scrape profile so current behaviour is
unchanged. No browser imports here; Playwright code lives in
``periscope.x_scrape.account_scrape`` / ``account_actions``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAIN_SLUG = "main"
DEFAULT_MIN_FOLLOWING = 100
DEFAULT_FOLLOW_CAP = 15
WATERMARK_KEEP_IDS = 50
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def slugify_account(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug[:40].strip("-")


def validate_slug(value: str) -> str:
    slug = str(value or "").strip().lower()
    if not _SLUG_RE.fullmatch(slug):
        raise ValueError(
            "Account id must be 1-40 chars: lowercase letters, digits, dashes "
            "(start with a letter or digit)."
        )
    return slug


def topic_slug(value: str) -> str:
    """Match the curator's interest slug (``curate_feeds.interest_slug``)."""

    return re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")


@dataclass(frozen=True, slots=True)
class XAccount:
    slug: str
    label: str
    x_handle: str = ""
    profile_dir: str = ""
    enabled: bool = True
    description: str = ""
    watermark_path: str = ""
    min_following: int = DEFAULT_MIN_FOLLOWING
    follow_cap: int = DEFAULT_FOLLOW_CAP
    like_enabled: bool = True
    last_scrape_at: str | None = None
    last_scrape_status: str | None = None
    last_scrape_count: int | None = None
    signed_in: bool | None = None
    profile_uploaded_at: str | None = None
    profile_upload_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_main(self) -> bool:
        return self.slug == MAIN_SLUG

    @property
    def topic_keys(self) -> set[str]:
        """Curator topic slugs this account owns (label slug + account slug)."""

        if self.is_main:
            return set()
        keys = {topic_slug(self.label), topic_slug(self.slug)}
        return {key for key in keys if key and key != "other"}

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> XAccount:
        signed = row.get("signed_in")
        return cls(
            slug=str(row["slug"]),
            label=str(row.get("label") or row["slug"]),
            x_handle=str(row.get("x_handle") or ""),
            profile_dir=str(row.get("profile_dir") or ""),
            enabled=bool(row.get("enabled", 1)),
            description=str(row.get("description") or ""),
            watermark_path=str(row.get("watermark_path") or ""),
            min_following=int(row.get("min_following") or DEFAULT_MIN_FOLLOWING),
            follow_cap=int(
                row["follow_cap"] if row.get("follow_cap") is not None else DEFAULT_FOLLOW_CAP
            ),
            like_enabled=bool(row.get("like_enabled", 1)),
            last_scrape_at=row.get("last_scrape_at"),
            last_scrape_status=row.get("last_scrape_status"),
            last_scrape_count=(
                int(row["last_scrape_count"])
                if row.get("last_scrape_count") is not None
                else None
            ),
            signed_in=None if signed is None else bool(signed),
            profile_uploaded_at=row.get("profile_uploaded_at"),
            profile_upload_name=row.get("profile_upload_name"),
        )


# ---------------------------------------------------------------------------
# Paths


def main_profile_path(data_dir: Path | str | None) -> Path:
    """Existing scrape profile resolution (env, cron profile, data dir)."""

    env = os.environ.get("PERISCOPE_X_CHROME_PROFILE")
    if env:
        return Path(env).expanduser()
    candidates = [Path("/workspace/x-scrape/chrome-profile")]
    if data_dir is not None:
        candidates.append(Path(data_dir) / "chrome-profile")
    candidates.append(Path("data/chrome-profile"))
    for candidate in candidates:
        if (candidate / "Default").exists():
            return candidate
    return candidates[0]


def _resolve(data_dir: Path | str | None, raw: str) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute() or data_dir is None:
        return path
    # Relative paths are relative to the data dir ("chrome-profiles/ai").
    parts = path.parts
    if parts and parts[0] == "data" and Path(data_dir).name == "data":
        path = Path(*parts[1:]) if len(parts) > 1 else Path(".")
    return Path(data_dir) / path


def profile_path(account: XAccount, data_dir: Path | str | None) -> Path:
    if account.profile_dir:
        return _resolve(data_dir, account.profile_dir)
    if account.is_main:
        return main_profile_path(data_dir)
    base = Path(data_dir) if data_dir is not None else Path("data")
    return base / "chrome-profiles" / account.slug


def watermark_path(account: XAccount, data_dir: Path | str | None) -> Path:
    if account.watermark_path:
        return _resolve(data_dir, account.watermark_path)
    base = Path(data_dir) if data_dir is not None else Path("data")
    if account.is_main:
        return base / "following_watermark.json"
    return base / "watermarks" / f"{account.slug}.json"


def account_dump_dir(out_dir: Path, slug: str) -> Path:
    return Path(out_dir) / "accounts" / slug


def profile_ready(path: Path) -> bool:
    default = Path(path) / "Default"
    if not default.exists():
        return False
    return (
        (default / "Cookies").exists()
        or (default / "Network" / "Cookies").exists()
        or (Path(path) / "Local State").exists()
    )


def write_watermark(path: Path, posts: Sequence[Mapping[str, Any]], *, hit: bool) -> dict:
    """Persist newest Following ids in the format ``load_watermark`` reads."""

    ordered = sorted(posts, key=lambda p: str(p.get("created_at") or ""), reverse=True)
    payload = {
        "following_newest_created_at": ordered[0].get("created_at") if ordered else None,
        "following_newest_ids": [
            str(p.get("status_id")) for p in ordered[:WATERMARK_KEEP_IDS] if p.get("status_id")
        ],
        "following_count": len(ordered),
        "hit_watermark": bool(hit),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


# ---------------------------------------------------------------------------
# Multi-source merge (dedupe across all feeds/accounts, keep every source tag)


def merge_sources(
    sources: Iterable[tuple[str, str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Merge ``(feed, account_slug, posts)`` lists by ``status_id``.

    First occurrence wins for the post body (so the main account's existing
    feeds keep priority); every later sighting adds its feed and account to
    ``feeds`` / ``source_accounts`` instead of being dropped.
    Returns ``(ordered_ids_posts, by_id)``.
    """

    by_id: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for feed, account, posts in sources:
        for raw in posts or []:
            if not isinstance(raw, Mapping):
                continue
            sid = str(raw.get("status_id") or "")
            if not sid:
                continue
            acct = str(raw.get("source_account") or account or MAIN_SLUG)
            feed_name = str(raw.get("feed") or feed)
            existing = by_id.get(sid)
            if existing is None:
                post = dict(raw)
                post["feed"] = feed_name
                post["feeds"] = [feed_name]
                post["source_accounts"] = [acct]
                post["source_account"] = acct
                by_id[sid] = post
                ordered.append(post)
                continue
            if feed_name not in existing["feeds"]:
                existing["feeds"].append(feed_name)
            if acct not in existing["source_accounts"]:
                existing["source_accounts"].append(acct)
            # Prefer a non-truncated body if a later source has it.
            if existing.get("is_truncated") and not raw.get("is_truncated") and raw.get("text"):
                existing["text"] = raw.get("text")
                existing["is_truncated"] = False
    return ordered, by_id


# ---------------------------------------------------------------------------
# Routing


def _topic_accounts(accounts: Iterable[XAccount]) -> list[XAccount]:
    return [a for a in accounts if a.enabled and not a.is_main]


def account_for_topic(accounts: Iterable[XAccount], topic: str | None) -> XAccount | None:
    key = topic_slug(topic or "")
    if not key or key == "other":
        return None
    for account in _topic_accounts(accounts):
        if key in account.topic_keys:
            return account
    return None


def owner_account(
    accounts: Iterable[XAccount],
    *,
    source_accounts: Sequence[str] | None,
    topic: str | None,
) -> str:
    """Account that 'owns' a keeper: a topic account that surfaced it (topic
    match preferred), else the topic account whose topic matches, else main."""

    pool = list(accounts)
    topical = {a.slug: a for a in _topic_accounts(pool)}
    sourced = [s for s in (source_accounts or []) if s in topical]
    by_topic = account_for_topic(pool, topic)
    if sourced:
        if by_topic and by_topic.slug in sourced:
            return by_topic.slug
        return sourced[0]
    if by_topic:
        return by_topic.slug
    return MAIN_SLUG


def route_follow(
    accounts: Iterable[XAccount],
    *,
    topic: str | None,
    source_accounts: Sequence[str] | None = None,
) -> str:
    """Follow-queue owner: topic match first, then sourcing topic account, else main."""

    pool = list(accounts)
    by_topic = account_for_topic(pool, topic)
    if by_topic:
        return by_topic.slug
    topical = {a.slug for a in _topic_accounts(pool)}
    for slug in source_accounts or []:
        if slug in topical:
            return slug
    return MAIN_SLUG


def like_plan(
    accounts: Iterable[XAccount],
    keepers: Iterable[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """Default like rule: a keeper surfaced by topic account X is liked by X."""

    likers = {a.slug: a for a in _topic_accounts(accounts) if a.like_enabled}
    plan: dict[str, list[str]] = {}
    for keeper in keepers:
        url = keeper.get("tweet_url")
        if not url:
            continue
        for slug in keeper.get("source_accounts") or []:
            if slug in likers:
                bucket = plan.setdefault(slug, [])
                if url not in bucket:
                    bucket.append(str(url))
    return plan


def labels_by_slug(accounts: Iterable[XAccount]) -> dict[str, str]:
    return {a.slug: a.label for a in accounts}
