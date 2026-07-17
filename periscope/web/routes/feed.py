"""Finite feed and local keep-label routes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


@router.get("/feed", response_class=HTMLResponse, name="feed")
async def feed(
    request: Request,
    account: str | None = None,
    kept: bool = Query(False),
) -> HTMLResponse:
    database = database_for(request)
    context = base_context(request, page="feed", title="Feed")
    context.update(
        {
            "batches": database.feed_batches(account=account, kept_only=kept),
            "facets": database.archive_facets(),
            "selected_account": account or "",
            "kept_only": kept,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="feed.html",
        context=context,
    )


@router.post("/keep/{tweet_id}", response_class=HTMLResponse, name="toggle_keep")
async def toggle_keep(request: Request, tweet_id: str):
    database = database_for(request)
    try:
        kept = database.toggle_keep(tweet_id)
    except KeyError:
        return HTMLResponse("Tweet not found", status_code=404)
    if request.headers.get("HX-Request") == "true":
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="partials/keep_button.html",
            context={"tweet_id": tweet_id, "kept": kept},
        )
    return RedirectResponse(request.url_for("feed"), status_code=303)
