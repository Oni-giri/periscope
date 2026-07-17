"""Machine-readable health route."""

from __future__ import annotations

from fastapi import APIRouter, Request

from periscope.scheduler import scheduled_jobs
from periscope.web.context import database_for

router = APIRouter()


@router.get("/health", name="health")
async def health(request: Request):
    result = database_for(request).health_summary()
    result["scheduled_jobs"] = scheduled_jobs(getattr(request.app.state, "scheduler", None))
    return result
