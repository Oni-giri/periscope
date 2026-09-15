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


def _beat_from_pick(pick: dict) -> str:
    """Prefer a short paragraph beat; fall back to first sentence."""

    for key in ("nugget_why", "commentary", "reason"):
        raw = " ".join(str(pick.get(key) or "").split()).strip()
        if not raw:
            continue
        # Keep up to two sentences when the field is already paragraph-ish.
        pieces: list[str] = []
        rest = raw
        for _ in range(2):
            sentence = _first_sentence(rest)
            if not sentence:
                break
            pieces.append(sentence)
            if len(sentence) >= len(rest):
                rest = ""
                break
            rest = rest[len(sentence) :].lstrip()
        beat = " ".join(pieces).strip()
        if beat:
            return beat
    return ""


def fallback_edition_highlights(rendered: dict) -> str:
    """Opinionated magazine lede when stored highlights are missing.

    Stitches 2–4 commentary/nugget beats into a longer paragraph instead of a
    bland topic list.
    """

    picks = list(rendered.get("picks") or [])
    beats: list[str] = []
    seen: set[str] = set()
    for pick in picks:
        beat = _beat_from_pick(pick)
        if not beat:
            continue
        key = beat.lower()
        if key in seen:
            continue
        seen.add(key)
        beats.append(beat)
        if len(beats) >= 4:
            break

    if not beats:
        return ""

    tags: list[str] = []
    tag_seen: set[str] = set()
    for pick in picks:
        tag = str(pick.get("tag") or "").strip()
        if not tag or tag in tag_seen:
            continue
        tag_seen.add(tag)
        tags.append(tag)
        if len(tags) >= 3:
            break

    if tags:
        lane = ", ".join(tags[:-1]) + (f", and {tags[-1]}" if len(tags) > 1 else tags[0])
        opener = (
            f"Don't skim the wire — today's cut is opinionated on {lane}. "
            "Here's the real story builders should care about:"
        )
    else:
        opener = (
            "Don't skim the wire — here's the real story in today's cut, "
            "not a neutral topic list:"
        )

    body = " ".join(beats)
    if not body.endswith((".", "!", "?")):
        body = f"{body}."
    closer = " Steal the patterns; ignore the noise."
    return f"{opener} {body}{closer}".strip()


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
