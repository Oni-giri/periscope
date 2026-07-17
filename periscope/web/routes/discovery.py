"""Discovery queue routes with explicit follow approval."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.jobs.fetchonly import build_xclient
from periscope.web.context import base_context, database_for
from periscope.xclient import CookieDeadError

router = APIRouter()


def _queue_response(
    request: Request,
    *,
    partial: bool = False,
    action_error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    candidates = database_for(request).list_candidates()
    context = {
        **base_context(request, page="discovery", title="Discovery"),
        "candidates": candidates,
        "action_error": action_error,
    }
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/discovery_queue.html" if partial else "discovery.html",
        context=context,
        status_code=status_code,
    )


@router.get("/discovery", response_class=HTMLResponse, name="discovery")
async def discovery(request: Request) -> HTMLResponse:
    return _queue_response(request)


@router.post(
    "/discovery/{handle}/accept",
    response_class=HTMLResponse,
    name="accept_candidate",
)
async def accept_candidate(request: Request, handle: str) -> HTMLResponse:
    database = database_for(request)
    candidate = database.get_candidate(handle)
    if candidate is None or candidate["status"] != "pending":
        raise HTTPException(status_code=404, detail="Candidate not found")

    client = request.app.state.xclient
    if client is None:
        client = build_xclient(request.app.state.config, request.app.state.secrets)
    try:
        await client.follow_account(handle)
    except CookieDeadError as exc:
        database.open_cookie_incident()
        return _queue_response(
            request,
            partial=request.headers.get("HX-Request") == "true",
            action_error=str(exc),
            status_code=502,
        )
    except Exception as exc:
        database.record_event("candidate_follow_failed", {"handle": handle, "message": str(exc)})
        return _queue_response(
            request,
            partial=request.headers.get("HX-Request") == "true",
            action_error=f"Could not follow @{handle}: {exc}",
            status_code=502,
        )

    database.review_candidate(handle, "accepted")
    return _queue_response(
        request,
        partial=request.headers.get("HX-Request") == "true",
    )


@router.post(
    "/discovery/{handle}/reject",
    response_class=HTMLResponse,
    name="reject_candidate",
)
async def reject_candidate(request: Request, handle: str) -> HTMLResponse:
    if not database_for(request).review_candidate(handle, "rejected"):
        raise HTTPException(status_code=404, detail="Candidate not found")
    return _queue_response(
        request,
        partial=request.headers.get("HX-Request") == "true",
    )
