"""Finite weekly report routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


def _response(request: Request, report: dict | None) -> HTMLResponse:
    database = database_for(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="weekly.html",
        context={
            **base_context(request, page="weekly", title="Weekly"),
            "report": report,
            "weeks": database.list_weekly_weeks(),
            "stale_ideas": database.list_ideas(status="parked", stale_days=7),
        },
    )


@router.get("/weekly", response_class=HTMLResponse, name="weekly")
async def weekly(request: Request) -> HTMLResponse:
    return _response(request, database_for(request).latest_weekly_report())


@router.get("/weekly/{week}", response_class=HTMLResponse, name="weekly_report")
async def weekly_report(request: Request, week: str) -> HTMLResponse:
    report = database_for(request).get_weekly_report(week)
    if report is None:
        raise HTTPException(status_code=404, detail="Weekly report not found")
    return _response(request, report)
