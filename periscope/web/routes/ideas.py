"""Local idea inbox — park actions from Today; never syncs to X."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


def _ideas_page(
    request: Request,
    *,
    status: str = "parked",
    flash: str | None = None,
) -> HTMLResponse:
    database = database_for(request)
    if status not in {"parked", "done", "dropped", "stale", "all"}:
        status = "parked"
    if status == "stale":
        ideas = database.list_ideas(status="parked", stale_days=7)
    elif status == "all":
        ideas = database.list_ideas(status=None)
    else:
        ideas = database.list_ideas(status=status)
    context = {
        **base_context(request, page="ideas", title="Ideas"),
        "ideas": ideas,
        "filter_status": status,
        "flash": flash,
        "stale_count": len(database.list_ideas(status="parked", stale_days=7)),
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="ideas.html",
        context=context,
    )


@router.get("/ideas", response_class=HTMLResponse, name="ideas")
async def ideas(request: Request, status: str = "parked") -> HTMLResponse:
    return _ideas_page(request, status=status)


@router.post("/ideas", name="park_idea")
async def park_idea(
    request: Request,
    title: str = Form(...),
    tweet_id: str | None = Form(None),
    handle: str | None = Form(None),
    note: str | None = Form(None),
    action_type: str | None = Form(None),
    url: str | None = Form(None),
    source_digest_date: str | None = Form(None),
):
    database = database_for(request)
    try:
        idea = database.park_idea(
            title=title,
            tweet_id=tweet_id or None,
            handle=handle or None,
            note=note or None,
            action_type=action_type or None,
            url=url or None,
            source_digest_date=source_digest_date or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if request.headers.get("HX-Request") == "true":
        return request.app.state.templates.TemplateResponse(
            request=request,
            name="partials/idea_parked.html",
            context={"idea": idea, "request": request},
        )
    return RedirectResponse(url=str(request.url_for("ideas")), status_code=303)


@router.post("/ideas/{idea_id}/done", response_class=HTMLResponse, name="idea_done")
async def idea_done(request: Request, idea_id: int) -> HTMLResponse:
    if database_for(request).set_idea_status(idea_id, "done") is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    status = request.query_params.get("status") or "parked"
    return _ideas_page(request, status=status, flash="Marked done")


@router.post("/ideas/{idea_id}/drop", response_class=HTMLResponse, name="idea_drop")
async def idea_drop(request: Request, idea_id: int) -> HTMLResponse:
    if database_for(request).set_idea_status(idea_id, "dropped") is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    status = request.query_params.get("status") or "parked"
    return _ideas_page(request, status=status, flash="Dropped")


@router.post("/ideas/{idea_id}/note", response_class=HTMLResponse, name="idea_note")
async def idea_note(
    request: Request,
    idea_id: int,
    note: str = Form(""),
) -> HTMLResponse:
    if database_for(request).update_idea_note(idea_id, note) is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    status = request.query_params.get("status") or "parked"
    return _ideas_page(request, status=status, flash="Note saved")
