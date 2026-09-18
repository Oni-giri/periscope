"""Operational settings routes with split secret and non-secret storage."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from periscope.config import normalize_handle
from periscope.jobs.fetchonly import build_xclient
from periscope.runtime import (
    RuntimeSettingsError,
    effective_config,
    read_curator_prompt,
    read_enrich_prompt,
    read_picks_prompt,
    reload_secrets,
    reset_curator_prompt,
    reset_enrich_prompt,
    reset_picks_prompt,
    save_curator_prompt,
    save_enrich_prompt,
    save_picks_prompt,
    save_schedule_settings,
    save_ui_settings,
    ui_settings,
    update_secrets_file,
)
from periscope.scheduler import configure_scheduler, scheduled_jobs
from periscope.telegram.bot import build_notifier
from periscope.web.context import base_context, database_for
from periscope.x_scrape.curate_feeds import (
    DEFAULT_OPENROUTER_BASE,
    NO_INTERESTS_MESSAGE,
    NoInterestsError,
    describe_interest_input,
    normalize_openrouter_base,
    parse_interest_lines,
    require_topics,
)

router = APIRouter()
_TABS = {"reading", "schedule", "system", "connections"}


def _chrome_profile_path(config: Any) -> Any:
    import os
    from pathlib import Path

    env = os.environ.get("PERISCOPE_X_CHROME_PROFILE")
    if env:
        return Path(env).expanduser()
    return Path(config.data_dir) / "chrome-profile"


def _parse_interest_names(raw: str) -> list[str]:
    return parse_interest_lines(raw)


def _reading_form_values(form: Any) -> dict[str, str]:
    return {
        "interests": str(form.get("interests", "")),
        "curator_prompt": str(form.get("curator_prompt", "")),
        "enrich_prompt": str(form.get("enrich_prompt", "")),
        "feed_max_posts": str(form.get("feed_max_posts", "50")),
        "archive_default_filter": str(form.get("archive_default_filter", "has_action")),
    }


def _context(
    request: Request,
    *,
    tab: str,
    message: str | None = None,
    error: str | None = None,
    interests_text: str | None = None,
    curator_prompt: str | None = None,
    enrich_prompt: str | None = None,
    validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if tab not in _TABS:
        tab = "reading"
    database = database_for(request)
    config = effective_config(request.app.state.config, database)
    prompt, prompt_default_exists = read_picks_prompt(config)
    curator_text, curator_is_custom = read_curator_prompt(config)
    enrich_text, enrich_is_custom = read_enrich_prompt(config)
    reading = ui_settings(database)
    topics = database.list_topics()
    pending_follows = len(database.list_pending_follows())
    if interests_text is None:
        interests_text = "\n".join(str(item["name"]) for item in topics)
    if curator_prompt is None:
        curator_prompt = curator_text
    if enrich_prompt is None:
        enrich_prompt = enrich_text
    return {
        **base_context(request, page="settings", title="Settings"),
        "tab": tab,
        "tabs": (
            ("reading", "Reading"),
            ("schedule", "Schedule"),
            ("system", "System"),
            ("connections", "Connections"),
        ),
        "message": message,
        "error": error,
        "validation": validation,
        "accounts": database.list_accounts(include_muted=True),
        "topics": topics,
        "interests_text": interests_text,
        "prompt": prompt,
        "prompt_default_exists": prompt_default_exists,
        "curator_prompt": curator_prompt,
        "curator_prompt_is_custom": curator_is_custom,
        "enrich_prompt": enrich_prompt,
        "enrich_prompt_is_custom": enrich_is_custom,
        "runtime_config": config,
        "settings": database.get_settings(),
        "ui": reading,
        "pending_follows": pending_follows,
        "openrouter_base_url": (
            getattr(request.app.state.secrets, "openrouter_base_url", None)
            or DEFAULT_OPENROUTER_BASE
        ),
        "openrouter_configured": bool(
            getattr(request.app.state.secrets, "openrouter_configured", False)
        ),
        "chrome_profile": str(_chrome_profile_path(request.app.state.config)),
        "chrome_profile_exists": _chrome_profile_path(request.app.state.config).exists(),
        "events": database.recent_events(limit=30),
        "spend": database.spend_summary(),
        "jobs": scheduled_jobs(getattr(request.app.state, "scheduler", None)),
    }


def _response(
    request: Request,
    *,
    tab: str,
    message: str | None = None,
    error: str | None = None,
    interests_text: str | None = None,
    curator_prompt: str | None = None,
    enrich_prompt: str | None = None,
    validation: dict[str, Any] | None = None,
) -> HTMLResponse:
    partial = request.headers.get("HX-Request") == "true"
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="partials/settings_panel.html" if partial else "settings.html",
        context=_context(
            request,
            tab=tab,
            message=message,
            error=error,
            interests_text=interests_text,
            curator_prompt=curator_prompt,
            enrich_prompt=enrich_prompt,
            validation=validation,
        ),
    )


@router.get("/settings", response_class=HTMLResponse, name="settings")
async def settings(request: Request, tab: str = "reading") -> HTMLResponse:
    return _response(request, tab=tab)


@router.post("/settings/reading", response_class=HTMLResponse)
async def save_reading(request: Request) -> HTMLResponse:
    form = await request.form()
    database = database_for(request)
    values = _reading_form_values(form)
    try:
        names = _parse_interest_names(values["interests"])
        require_topics(names)
        save_ui_settings(
            database,
            feed_max_posts=values["feed_max_posts"],
            archive_default_filter=values["archive_default_filter"],
        )
        existing = {
            str(item["name"]).lower(): item for item in database.list_topics()
        }
        topics = []
        for name in names:
            prior = existing.get(name.lower())
            topics.append(
                {
                    "name": name,
                    "min_faves": int(prior["min_faves"]) if prior else 0,
                    "decay_weight": float(prior["decay_weight"]) if prior else 1.0,
                }
            )
        database.replace_topics(topics)
        if "curator_prompt" in form:
            save_curator_prompt(
                request.app.state.config, database, values["curator_prompt"]
            )
        if "enrich_prompt" in form:
            save_enrich_prompt(
                request.app.state.config, database, values["enrich_prompt"]
            )
    except (ValueError, RuntimeSettingsError, NoInterestsError) as exc:
        message = NO_INTERESTS_MESSAGE if isinstance(exc, NoInterestsError) else str(exc)
        return _response(
            request,
            tab="reading",
            error=message,
            interests_text=values["interests"],
            curator_prompt=values["curator_prompt"],
            enrich_prompt=values["enrich_prompt"],
        )
    return _response(request, tab="reading", message="Reading settings saved.")


@router.post("/settings/reading/validate", response_class=HTMLResponse)
async def validate_reading(request: Request) -> HTMLResponse:
    form = await request.form()
    values = _reading_form_values(form)
    database = database_for(request)
    saved_names = [str(item["name"]) for item in database.list_topics()]
    template = values["curator_prompt"].strip() or None
    report = describe_interest_input(
        values["interests"],
        saved_names=saved_names,
        template=template,
    )
    extras = {
        "interests_text": values["interests"],
        "curator_prompt": values["curator_prompt"],
        "enrich_prompt": values["enrich_prompt"],
        "validation": report,
    }
    if report["ok"]:
        message = report["message"]
        if report["db_differs"]:
            saved = ", ".join(report["saved_topics"]) or "(empty)"
            message += f" Saved DB differs ({saved})."
        return _response(request, tab="reading", message=message, **extras)
    return _response(request, tab="reading", error=report["message"], **extras)


@router.post("/settings/reading/prompts/{name}/reset", response_class=HTMLResponse)
async def reset_reading_prompt(request: Request, name: str) -> HTMLResponse:
    database = database_for(request)
    config = request.app.state.config
    try:
        if name == "curator":
            reset_curator_prompt(config, database)
            message = "Curator system prompt reset to the built-in default."
        elif name == "enrich":
            reset_enrich_prompt(config, database)
            message = "Enrich actions prompt reset to the built-in default."
        else:
            raise HTTPException(status_code=404, detail="Unknown prompt")
    except (OSError, RuntimeSettingsError) as exc:
        return _response(request, tab="reading", error=str(exc))
    return _response(request, tab="reading", message=message)


@router.post("/settings/credentials", response_class=HTMLResponse)
async def save_credentials(request: Request) -> HTMLResponse:
    import os

    form = await request.form()
    base_raw = str(form.get("openrouter_base_url", "")).strip()
    try:
        if base_raw:
            base_raw = normalize_openrouter_base(base_raw)
    except ValueError as exc:
        return _response(request, tab="connections", error=str(exc))
    updates = {
        "OPENROUTER_API_KEY": str(form.get("openrouter_api_key", "")),
        "OPENROUTER_BASE_URL": base_raw,
        "X_AUTH_TOKEN": str(form.get("x_auth_token", "")),
        "X_CT0": str(form.get("x_ct0", "")),
    }
    try:
        update_secrets_file(request.app.state.config, updates)
        request.app.state.secrets = reload_secrets(request.app.state.config)
        request.app.state.xclient = None
        request.app.state.mcp_tools.secrets = request.app.state.secrets
        request.app.state.mcp_tools.xclient = None
        if updates["OPENROUTER_API_KEY"].strip():
            os.environ["OPENROUTER_API_KEY"] = updates["OPENROUTER_API_KEY"].strip()
        if base_raw:
            os.environ["OPENROUTER_BASE_URL"] = base_raw
        database_for(request).seed(request.app.state.config, request.app.state.secrets)
        try:
            await request.app.state.telegram_polling.reconfigure(request.app.state.secrets)
        except Exception as exc:
            database_for(request).record_event("telegram_polling_failed", {"message": str(exc)})
    except (OSError, RuntimeSettingsError) as exc:
        return _response(request, tab="connections", error=str(exc))
    return _response(
        request,
        tab="connections",
        message="Connections saved locally. Blank fields kept their previous values.",
    )


@router.post("/settings/test/{service}", response_class=HTMLResponse)
async def test_service(request: Request, service: str) -> HTMLResponse:
    database = database_for(request)
    config = effective_config(request.app.state.config, database)
    secrets = request.app.state.secrets
    try:
        if service == "x":
            accounts = database.list_accounts()
            if not accounts:
                raise RuntimeError("Add a curated account before testing X")
            client = request.app.state.xclient or build_xclient(config, secrets)
            await client.profile(str(accounts[0]["handle"]))
        elif service == "anthropic":
            if not secrets.anthropic_api_key:
                raise RuntimeError("Anthropic is not configured")
            from anthropic import AsyncAnthropic

            await AsyncAnthropic(api_key=secrets.anthropic_api_key).messages.create(
                model=config.models.cheap,
                max_tokens=1,
                messages=[{"role": "user", "content": "Reply with OK."}],
            )
        elif service == "telegram":
            if not secrets.telegram_configured:
                raise RuntimeError("Telegram is not configured")
            await build_notifier(secrets, config.delivery).send_alert("Settings test message")
        else:
            raise HTTPException(status_code=404, detail="Unknown service")
    except HTTPException:
        raise
    except Exception as exc:
        database.record_event(
            "credential_test_failed",
            {"service": service, "message": str(exc)},
        )
        return _response(
            request,
            tab="connections",
            error=f"{service.title()} test failed: {exc}",
        )
    database.record_event("credential_test_succeeded", {"service": service})
    return _response(
        request,
        tab="connections",
        message=f"{service.title()} connection verified.",
    )


@router.post("/settings/accounts/add", response_class=HTMLResponse)
async def add_account(request: Request) -> HTMLResponse:
    form = await request.form()
    try:
        handle = normalize_handle(str(form.get("handle", "")))
        added = database_for(request).add_account(handle)
    except ValueError as exc:
        return _response(request, tab="connections", error=str(exc))
    return _response(
        request,
        tab="connections",
        message=f"@{handle} added." if added else f"@{handle} is already curated.",
    )


@router.post("/settings/accounts/{handle}/mute", response_class=HTMLResponse)
async def mute_account(request: Request, handle: str) -> HTMLResponse:
    database = database_for(request)
    account = next(
        (item for item in database.list_accounts(include_muted=True) if item["handle"] == handle),
        None,
    )
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    database.set_account_muted(handle, not bool(account["muted"]))
    return _response(request, tab="connections", message=f"@{handle} updated.")


@router.post("/settings/accounts/{handle}/remove", response_class=HTMLResponse)
async def remove_account(request: Request, handle: str) -> HTMLResponse:
    if not database_for(request).remove_account(handle):
        raise HTTPException(status_code=404, detail="Account not found")
    return _response(request, tab="connections", message=f"@{handle} removed locally.")


@router.post("/settings/topics", response_class=HTMLResponse)
async def save_topics(request: Request) -> HTMLResponse:
    form = await request.form()
    names = [str(item) for item in form.getlist("topic_name")]
    thresholds = [str(item) for item in form.getlist("topic_min_faves")]
    topics = []
    try:
        for name, threshold in zip(names, thresholds, strict=False):
            if name.strip():
                topics.append(
                    {
                        "name": name.strip(),
                        "min_faves": int(threshold),
                        "decay_weight": 1.0,
                    }
                )
        database_for(request).replace_topics(topics)
    except ValueError as exc:
        return _response(request, tab="reading", error=f"Invalid topic value: {exc}")
    return _response(request, tab="reading", message="Topics saved.")


@router.post("/settings/prompt", response_class=HTMLResponse)
async def save_prompt(request: Request) -> HTMLResponse:
    form = await request.form()
    try:
        save_picks_prompt(
            request.app.state.config,
            database_for(request),
            str(form.get("prompt", "")),
        )
    except (OSError, RuntimeSettingsError) as exc:
        return _response(request, tab="connections", error=str(exc))
    return _response(request, tab="connections", message="Picks prompt saved as a new version.")


@router.post("/settings/prompt/reset", response_class=HTMLResponse)
async def reset_prompt(request: Request) -> HTMLResponse:
    try:
        reset_picks_prompt(request.app.state.config, database_for(request))
    except (OSError, RuntimeSettingsError) as exc:
        return _response(request, tab="connections", error=str(exc))
    return _response(
        request,
        tab="connections",
        message="Picks prompt reset to the installed default.",
    )


@router.post("/settings/schedule", response_class=HTMLResponse)
async def save_schedule(request: Request) -> HTMLResponse:
    form = await request.form()
    daily_times = [str(item) for item in form.getlist("daily_time")]
    try:
        save_schedule_settings(
            database_for(request),
            timezone=str(form.get("timezone", "UTC")),
            daily_times=daily_times,
            weekly_day=str(form.get("weekly_day", "sun")),
            weekly_time=str(form.get("weekly_time", "09:00")),
            feed_interval_minutes=int(str(form.get("feed_interval_minutes", "30"))),
            picks_minimum=int(str(form.get("picks_minimum", "0"))),
            picks_maximum=int(str(form.get("picks_maximum", "5"))),
            clustering_aggressiveness=int(str(form.get("clustering_aggressiveness", "45"))),
            topic_decay=form.get("topic_decay") == "on",
        )
        scheduler = getattr(request.app.state, "scheduler", None)
        if scheduler is not None:
            configure_scheduler(scheduler, request.app.state.job_runner)
    except (ValueError, RuntimeSettingsError) as exc:
        return _response(request, tab="schedule", error=str(exc))
    return _response(request, tab="schedule", message="Schedule updated immediately.")


@router.post("/settings/run/{job}", response_class=HTMLResponse)
async def run_job(request: Request, job: str) -> HTMLResponse:
    if job not in {"fetch", "rebuild", "weekly"}:
        raise HTTPException(status_code=404, detail="Unknown job")
    launched = request.app.state.job_runner.launch(job)
    database_for(request).record_event("job_requested", {"job": job, "launched": launched})
    label = "Digest rebuild" if job == "rebuild" else job.title()
    message = f"{label} queued." if launched else f"{label} is already running."
    return _response(request, tab="connections", message=message)
