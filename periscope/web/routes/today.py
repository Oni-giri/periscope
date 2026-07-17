"""Digest index and dated digest routes."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


def _digest_response(request: Request, digest_date: str | None = None) -> HTMLResponse:
    database = database_for(request)
    digest = database.get_digest(digest_date) if digest_date else database.latest_digest()
    context = base_context(request, page="today", title="Today")
    context["digest"] = digest["rendered"] if digest else None
    context["digest_record"] = digest
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
