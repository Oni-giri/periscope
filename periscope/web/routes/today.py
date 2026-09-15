"""Digest index and dated digest routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


def _nugget_picks(rendered: dict) -> list[dict]:
    nuggets: list[dict] = []
    for pick in rendered.get("picks") or []:
        actions = pick.get("actions") or []
        steal_heavy = sum(1 for action in actions if action.get("type") == "steal") > 0
        if (
            pick.get("nugget")
            or pick.get("actionable")
            or str(pick.get("tag") or "").upper() == "INSIGHT"
            or steal_heavy
        ):
            nuggets.append(pick)
    return nuggets


def _first_sentence(text: str) -> str:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return ""
    for sep in (". ", "! ", "? "):
        if sep in cleaned:
            head, *_ = cleaned.split(sep, 1)
            ending = sep[0]
            return f"{head}{ending}"
    return cleaned


def fallback_edition_highlights(rendered: dict) -> str:
    """Magazine-style lede from topics + early pick commentaries when highlights absent."""

    picks = list(rendered.get("picks") or [])
    topics: list[str] = []
    seen: set[str] = set()
    for pick in picks:
        tag = str(pick.get("tag") or "").strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        topics.append(tag)

    if not topics:
        topic_bit = ""
    elif len(topics) == 1:
        topic_bit = f"Today's edition leans {topics[0]}."
    elif len(topics) == 2:
        topic_bit = f"Today's edition spans {topics[0]} and {topics[1]}."
    else:
        topic_bit = f"Today's edition spans {', '.join(topics[:-1])}, and {topics[-1]}."

    snippets: list[str] = []
    for pick in picks:
        raw = str(pick.get("commentary") or pick.get("reason") or pick.get("nugget_why") or "")
        sentence = _first_sentence(raw)
        if not sentence or sentence in snippets:
            continue
        snippets.append(sentence)
        if len(snippets) >= 3:
            break

    parts = [part for part in (topic_bit, " ".join(snippets)) if part]
    return " ".join(parts).strip()


def edition_highlights_for(rendered: dict | None) -> str:
    if not rendered:
        return ""
    stored = str(rendered.get("highlights") or "").strip()
    if stored:
        return stored
    return fallback_edition_highlights(rendered)


def _digest_response(request: Request, digest_date: str | None = None) -> HTMLResponse:
    database = database_for(request)
    digest = database.get_digest(digest_date) if digest_date else database.latest_digest()
    context = base_context(request, page="today", title="Today")
    rendered = digest["rendered"] if digest else None
    context["digest"] = rendered
    context["digest_record"] = digest
    context["nuggets"] = _nugget_picks(rendered) if rendered else []
    context["edition_highlights"] = edition_highlights_for(rendered)
    context["queued_handles"] = database.queued_follow_handles()
    context["follow_source"] = "today"
    dates = database.list_digest_dates()
    context["digest_dates"] = dates
    context["previous_date"] = None
    context["next_date"] = None
    if digest:
        current = str(digest["date"])
        if current in dates:
            index = dates.index(current)
            context["next_date"] = dates[index - 1] if index > 0 else None
            context["previous_date"] = dates[index + 1] if index + 1 < len(dates) else None
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="today.html",
        context=context,
    )


@router.get("/", response_class=HTMLResponse, name="today")
async def today(request: Request) -> HTMLResponse:
    return _digest_response(request)


@router.get("/digest/{digest_date}", response_class=HTMLResponse, name="dated_digest")
async def dated_digest(request: Request, digest_date: str) -> HTMLResponse:
    try:
        date.fromisoformat(digest_date)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Digest not found") from exc
    if database_for(request).get_digest(digest_date) is None:
        raise HTTPException(status_code=404, detail="Digest not found")
    return _digest_response(request, digest_date)
