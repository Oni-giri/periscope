"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import MutableHeaders

from periscope.config import AppConfig, Secrets, load_config, load_secrets
from periscope.db import Database
from periscope.mcp_server import build_mcp_server
from periscope.scheduler import JobRunner, configure_scheduler
from periscope.telegram.bot import TelegramPolling
from periscope.web.context import (
    absolute_date,
    absolute_datetime,
    absolute_time,
    date_heading,
    md_bold,
)
from periscope.web.routes import (
    archive,
    cluster_detail,
    discovery,
    feed,
    follow_queue,
    health,
    ideas,
    inbox,
    settings,
    today,
    weekly,
)
from periscope.xclient import XClient

WEB_ROOT = Path(__file__).parent


class ResponseHeadersMiddleware:
    """Add local-service safety headers without BaseHTTPMiddleware buffering."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                headers["X-Frame-Options"] = "DENY"
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_app(
    config: AppConfig | None = None,
    secrets: Secrets | None = None,
    *,
    database: Database | None = None,
    xclient: XClient | None = None,
) -> FastAPI:
    config = config or load_config()
    secrets = secrets or load_secrets(data_dir=config.data_dir)
    database = database or Database(config.db_path)
    mcp_server = build_mcp_server(
        config,
        secrets,
        database,
        xclient=xclient,
        streamable_http_path="/",
    )
    telegram_polling = TelegramPolling(config, secrets, database, xclient=xclient)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        database.seed(config, secrets)
        scheduler = AsyncIOScheduler()
        runner = JobRunner(app)
        app.state.scheduler = scheduler
        app.state.job_runner = runner
        configure_scheduler(scheduler, runner)
        scheduler.start()
        try:
            await telegram_polling.start()
        except Exception as exc:
            database.record_event("telegram_polling_failed", {"message": str(exc)})
        try:
            async with mcp_server.session_manager.run():
                yield
        finally:
            await telegram_polling.stop()
            scheduler.shutdown(wait=False)
            await runner.close()

    app = FastAPI(
        title="Periscope",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    templates = Jinja2Templates(directory=WEB_ROOT / "templates")
    templates.env.filters["absolute_date"] = absolute_date
    templates.env.filters["absolute_datetime"] = absolute_datetime
    templates.env.filters["absolute_time"] = absolute_time
    templates.env.filters["date_heading"] = date_heading
    templates.env.filters["md_bold"] = md_bold
    templates.env.filters["follow_handle"] = follow_queue.follow_handle_filter
    app.state.config = config
    app.state.secrets = secrets
    app.state.database = database
    app.state.templates = templates
    app.state.xclient = xclient
    app.state.mcp_server = mcp_server
    app.state.mcp_tools = mcp_server.periscope_tools
    app.state.telegram_polling = telegram_polling

    app.mount(
        "/static",
        StaticFiles(directory=WEB_ROOT / "static"),
        name="static",
    )
    media_dir = Path(config.data_dir) / "media"
    try:
        media_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        media_dir = None
    if media_dir is not None and media_dir.is_dir():
        app.mount(
            "/media",
            StaticFiles(directory=media_dir),
            name="media",
        )
    app.mount("/mcp", mcp_server.streamable_http_app(), name="mcp")
    app.add_middleware(ResponseHeadersMiddleware)
    app.include_router(today.router)
    app.include_router(feed.router)
    app.include_router(archive.router)
    app.include_router(cluster_detail.router)
    app.include_router(discovery.router)
    app.include_router(inbox.router)
    app.include_router(ideas.router)
    app.include_router(follow_queue.router)
    app.include_router(weekly.router)
    app.include_router(settings.router)
    app.include_router(health.router)

    @app.exception_handler(404)
    async def not_found(request: Request, exc) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            context={"request": request, "title": "Not found", "page": ""},
            status_code=404,
        )

    return app


app = create_app()
