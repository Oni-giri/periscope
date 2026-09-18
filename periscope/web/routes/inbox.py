"""Inbox for follow propositions and optional discovery candidates."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


def _inbox_context(request: Request, *, action_error: str | None = None) -> dict:
    database = database_for(request)
    follows = database.list_follow_queue(
        statuses=("pending", "followed", "already", "failed")
    )
    pending = [item for item in follows if item["status"] == "pending"]
    done = [item for item in follows if item["status"] != "pending"]
    candidates = database.list_candidates(status="pending")
    return {
        **base_context(request, page="inbox", title="Inbox"),
        "pending_follows": pending,
        "done_follows": done,
        "candidates": candidates,
        "action_error": action_error,
    }


@router.get("/inbox", response_class=HTMLResponse, name="inbox")
async def inbox(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="inbox.html",
        context=_inbox_context(request),
    )


@router.post(
    "/inbox/follow/{handle}/dismiss",
    response_class=HTMLResponse,
    name="dismiss_follow",
)
async def dismiss_follow(request: Request, handle: str) -> HTMLResponse:
    if not database_for(request).dismiss_follow(handle):
        raise HTTPException(status_code=404, detail="Follow proposition not found")
    partial = request.headers.get("HX-Request") == "true"
    context = _inbox_context(request)
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/inbox_panel.html" if partial else "inbox.html",
        context=context,
    )
