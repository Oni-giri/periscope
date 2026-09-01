"""FTS5-backed finite archive search."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from periscope.db import TWEET_ACTION_TYPES, SearchQueryError
from periscope.web.context import base_context, database_for

ACTION_FILTERS = ("any",) + TWEET_ACTION_TYPES

router = APIRouter()


@router.get("/archive", response_class=HTMLResponse, name="archive")
async def archive(
    request: Request,
    q: str = "",
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    account: str | None = None,
    topic: str | None = None,
    kept: str | None = None,
    action: str | None = None,
    page: int = Query(1, ge=1),
) -> HTMLResponse:
    database = database_for(request)
    kept_value = None
    if kept in {"1", "true", "yes"}:
        kept_value = True
    elif kept in {"0", "false", "no"}:
        kept_value = False
    action_value = (action or "").strip().lower() or None
    if action_value and action_value not in ACTION_FILTERS:
        action_value = None
    error = None
    try:
        results = database.archive_page(
            query=q,
            from_date=from_date,
            to_date=to_date,
            account=account,
            topic=topic,
            kept=kept_value,
            action=action_value,
            page=page,
        )
    except SearchQueryError:
        error = "That search expression is not valid. Try fewer operators or plain words."
        results = {"items": [], "total": 0, "page": page, "page_size": 20}

    context = base_context(request, page="archive", title="Archive")
    context.update(
        {
            "results": results,
            "facets": database.archive_facets(),
            "q": q,
            "from_date": from_date or "",
            "to_date": to_date or "",
            "selected_account": account or "",
            "selected_topic": topic or "",
            "selected_kept": kept or "",
            "selected_action": action_value or "",
            "action_filters": ACTION_FILTERS,
            "search_error": error,
        }
    )
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="archive.html",
        context=context,
    )
