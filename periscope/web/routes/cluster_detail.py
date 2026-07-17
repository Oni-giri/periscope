"""Expanded story detail route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.web.context import base_context, database_for

router = APIRouter()


@router.get("/cluster/{cluster_id}", response_class=HTMLResponse, name="cluster_detail")
async def cluster_detail(request: Request, cluster_id: str) -> HTMLResponse:
    database = database_for(request)
    cluster = database.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="Story not found")
    database.record_cluster_view(cluster_id)
    context = base_context(request, page="today", title=str(cluster["headline"]))
    context["cluster"] = cluster
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="cluster_detail.html",
        context=context,
    )
