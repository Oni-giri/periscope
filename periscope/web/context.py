"""Shared web context and absolute-time formatting."""

from __future__ import annotations

import re

from datetime import UTC, date, datetime
from typing import Any

from fastapi import Request
from markupsafe import Markup, escape

from periscope.db import Database

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")



def md_bold(value: object) -> Markup:
    """Escape text, then render a safe ``**bold**`` subset as <strong>."""

    raw = "" if value is None else str(value)
    escaped = str(escape(raw))

    def _wrap(match: re.Match[str]) -> str:
        inner = match.group(1)
        if not inner:
            return match.group(0)
        return f"<strong>{inner}</strong>"

    return Markup(_BOLD_RE.sub(_wrap, escaped))


def database_for(request: Request) -> Database:
    return request.app.state.database


def _datetime(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result


def absolute_time(value: str | datetime | None) -> str:
    parsed = _datetime(value)
    return parsed.astimezone(UTC).strftime("%H:%M") if parsed else "Unknown"


def absolute_date(value: str | date | datetime | None) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        parsed_date = value
    else:
        parsed = _datetime(value)  # type: ignore[arg-type]
        if parsed is None:
            try:
                parsed_date = date.fromisoformat(str(value))
            except (TypeError, ValueError):
                return "Unknown"
        else:
            parsed_date = parsed.date()
    return parsed_date.strftime("%d %b %Y")


def absolute_datetime(value: str | datetime | None) -> str:
    parsed = _datetime(value)
    return parsed.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC") if parsed else "Unknown"


def date_heading(value: str | date) -> str:
    parsed = value if isinstance(value, date) else date.fromisoformat(value)
    return parsed.strftime("%A, %-d %B %Y")


def base_context(request: Request, *, page: str, title: str) -> dict[str, Any]:
    database = database_for(request)
    health = database.health_summary()
    pending_follows = database.rows(
        "SELECT COUNT(*) AS count FROM follow_queue WHERE status = 'pending'"
    )
    pending_candidates = database.rows(
        "SELECT COUNT(*) AS count FROM candidates WHERE status = 'pending'"
    )
    follow_n = int(pending_follows[0]["count"]) if pending_follows else 0
    candidate_n = int(pending_candidates[0]["count"]) if pending_candidates else 0
    config = request.app.state.config
    return {
        "request": request,
        "page": page,
        "title": title,
        "health": health,
        "discovery_count": candidate_n,
        "inbox_count": follow_n + candidate_n,
        "schedule_times": config.schedule.daily_times,
    }
