"""Queue X follows from Today. Never talks to X; scrape_feeds drains the table."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from periscope.web.context import database_for

router = APIRouter()

_RESERVED_PATHS = {
    "i",
    "home",
    "search",
    "explore",
    "settings",
    "intent",
    "share",
    "hashtag",
    "compose",
    "messages",
    "notifications",
    "login",
    "signup",
    "privacy",
    "tos",
    "download",
    "about",
}
_PROFILE_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{1,30})")
_HANDLE_ONLY_RE = re.compile(r"^[A-Za-z0-9_]{1,30}$")


def _handle_from_profile_url(url: str) -> str | None:
    raw = url.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host not in _PROFILE_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return None
    candidate = parts[0]
    if candidate.lower() in _RESERVED_PATHS:
        return None
    if not _HANDLE_ONLY_RE.fullmatch(candidate):
        return None
    return candidate.lower()


def parse_follow_handle(
    url: str | None = None,
    label: str | None = None,
    fallback: str | None = None,
) -> str | None:
    """Resolve a follow target: x.com/Handle, then @ in label, then tweet author."""

    if url:
        handle = _handle_from_profile_url(str(url))
        if handle:
            return handle
    if label:
        match = _HANDLE_RE.search(str(label))
        if match:
            return match.group(1).lower()
    if fallback:
        canonical = str(fallback).strip().removeprefix("@").lower()
        if canonical:
            return canonical
    return None


def follow_handle_filter(action: Any, fallback: str = "") -> str:
    if not isinstance(action, Mapping):
        return parse_follow_handle(fallback=fallback) or ""
    return (
        parse_follow_handle(
            url=action.get("url"),  # type: ignore[arg-type]
            label=action.get("label"),  # type: ignore[arg-type]
            fallback=fallback,
        )
        or ""
    )


def resolve_follow_account(
    database: Any, *, account_hint: str | None = None, topic: str | None = None
) -> str:
    """Route a follow to the topic account whose topic matches (or that surfaced
    the post); otherwise the main account. Disabled/unknown accounts → main."""

    from periscope.x_accounts import MAIN_SLUG, XAccount, route_follow

    try:
        accounts = [XAccount.from_row(row) for row in database.list_x_accounts()]
    except Exception:  # noqa: BLE001 - pre-migration DB
        return MAIN_SLUG
    hint = str(account_hint or "").strip().lower()
    return route_follow(accounts, topic=topic, source_accounts=[hint] if hint else None)


@router.post("/follow-queue", name="queue_follow")
async def queue_follow(
    request: Request,
    handle: str = Form(""),
    tweet_id: str | None = Form(None),
    url: str | None = Form(None),
    label: str | None = Form(None),
    source: str | None = Form("today"),
    account: str | None = Form(None),
    topic: str | None = Form(None),
):
    resolved = parse_follow_handle(url=url, label=label, fallback=handle)
    if not resolved:
        raise HTTPException(status_code=400, detail="Follow handle is required")
    database = database_for(request)
    owner = resolve_follow_account(database, account_hint=account, topic=topic)
    try:
        database.enqueue_follow(
            resolved,
            tweet_id=tweet_id or None,
            source=source or "today",
            account=owner,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.headers.get("HX-Request") == "true":
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="partials/follow_queued.html",
            context={"handle": resolved, "request": request},
        )
    referer = request.headers.get("referer") or str(request.url_for("today"))
    return RedirectResponse(url=referer, status_code=303)
