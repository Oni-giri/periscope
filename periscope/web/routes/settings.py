"""Operational settings routes with split secret and non-secret storage."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
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
    llm_model,
    save_llm_model,
    sync_config_models,
)
from periscope.scheduler import configure_scheduler, scheduled_jobs
from periscope.telegram.bot import build_notifier
from periscope.web import auth as web_auth
from periscope.web.chrome_profile import (
    extract_zip_to_temp,
    read_profile_meta,
    replace_chrome_profile,
    write_profile_meta,
)
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
_TABS = {"reading", "schedule", "system", "connections", "accounts"}


def _chrome_profile_path(config: Any) -> Any:
    """Resolve the Chromium user-data dir used for X scrape login (main account)."""
    from periscope.x_accounts import main_profile_path

    return main_profile_path(config.data_dir)


def _chrome_profile_ready(path: Any) -> bool:
    from pathlib import Path

    profile = Path(path)
    default = profile / "Default"
    if not default.exists():
        return False
    # Cookies or Local Storage are enough to treat as "created + used"
    return (default / "Cookies").exists() or (default / "Network" / "Cookies").exists() or (
        profile / "Local State"
    ).exists()


def _chrome_profile_meta_context(data_dir: Any) -> dict[str, Any]:
    meta = read_profile_meta(data_dir)
    if not meta:
        return {
            "chrome_profile_uploaded_at": None,
            "chrome_profile_upload_name": None,
        }
    return {
        "chrome_profile_uploaded_at": meta.get("uploaded_at"),
        "chrome_profile_upload_name": meta.get("source_filename"),
    }


def _x_account_status(account: Any, profile: Any) -> tuple[str, str]:
    """(short status, css state) for the Accounts list."""
    from periscope.web.chrome_profile import profile_in_use
    from periscope.x_accounts import profile_ready

    if not profile_ready(profile):
        return "Needs sign-in (no profile yet)", "warn"
    if profile_in_use(profile):
        return "Profile in use (Chrome open)", "warn"
    if account.signed_in is True:
        return "Signed in", "ok"
    if account.signed_in is False:
        return "NOT_SIGNED_IN at last scrape", "error"
    return "Profile present, not checked yet", "muted"


def _x_accounts_context(database: Any, config: Any) -> list[dict[str, Any]]:
    from periscope.x_accounts import XAccount, profile_path, watermark_path

    try:
        rows = database.list_x_accounts()
    except Exception:  # noqa: BLE001 - pre-migration database
        return []
    pending: dict[str, int] = {}
    try:
        for row in database.list_pending_follows():
            key = str(row.get("account") or "main")
            pending[key] = pending.get(key, 0) + 1
    except Exception:  # noqa: BLE001
        pending = {}
    out = []
    for row in rows:
        account = XAccount.from_row(row)
        if account.is_main and not account.last_scrape_at:
            account = _main_scrape_from_meta(account, config)
        profile = (
            _chrome_profile_path(config)
            if account.is_main and not account.profile_dir
            else profile_path(account, config.data_dir)
        )
        status, state = _x_account_status(account, profile)
        out.append(
            {
                "account": account,
                "row": row,
                "profile": str(profile),
                "watermark": str(watermark_path(account, config.data_dir)),
                "status": status,
                "state": state,
                "pending_follows": pending.get(account.slug, 0),
            }
        )
    return out


def _main_scrape_from_meta(account: Any, config: Any) -> Any:
    """Main's scrape is unchanged and does not write x_accounts; read its
    ``x-dumps/scrape_meta.json`` instead for last scrape / signed-in."""
    import dataclasses
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    meta_file = Path(config.data_dir) / "x-dumps" / "scrape_meta.json"
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        stamp = datetime.fromtimestamp(meta_file.stat().st_mtime, tz=UTC)
    except (OSError, ValueError):
        return account
    if not isinstance(meta, dict):
        return account
    signed = meta.get("signed_in")
    count = sum(
        int((meta.get(key) or {}).get("unique_count") or 0) for key in ("foryou", "following")
    )
    return dataclasses.replace(
        account,
        last_scrape_at=stamp.isoformat().replace("+00:00", "Z"),
        last_scrape_status="ok" if signed else "NOT_SIGNED_IN",
        last_scrape_count=count or None,
        signed_in=None if signed is None else bool(signed),
    )


def _account_form_values(form: Any, *, include_slug: bool) -> dict[str, Any]:
    values: dict[str, Any] = {
        "label": str(form.get("label", "")).strip(),
        "x_handle": str(form.get("x_handle", "")).strip(),
        "description": str(form.get("description", "")).strip(),
        "profile_dir": str(form.get("profile_dir", "")).strip(),
        "min_following": str(form.get("min_following", "100")).strip() or "100",
        "follow_cap": str(form.get("follow_cap", "15")).strip() or "15",
        "like_enabled": form.get("like_enabled") == "on",
        "enabled": form.get("enabled") == "on",
    }
    if include_slug:
        values["slug"] = str(form.get("slug", "")).strip().lower()
    return values


def _clean_account_values(values: dict[str, Any]) -> dict[str, Any]:
    clean = dict(values)
    clean.pop("slug", None)
    try:
        clean["min_following"] = int(values["min_following"])
        clean["follow_cap"] = int(values["follow_cap"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Min Following posts and follow cap must be whole numbers") from exc
    if clean["min_following"] < 1:
        raise ValueError("Min Following posts must be at least 1")
    return clean


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
    account_form: dict[str, Any] | None = None,
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
            ("accounts", "Accounts"),
        ),
        "x_accounts": _x_accounts_context(database, request.app.state.config),
        "account_form": account_form or {},
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
        "llm_model_id": llm_model(database),
        "chrome_profile": str(_chrome_profile_path(request.app.state.config)),
        "chrome_profile_ready": _chrome_profile_ready(
            _chrome_profile_path(request.app.state.config)
        ),
        **_chrome_profile_meta_context(request.app.state.config.data_dir),
        "events": database.recent_events(limit=30),
        "spend": database.spend_summary(),
        "jobs": scheduled_jobs(getattr(request.app.state, "scheduler", None)),
        "web_password_set": web_auth.password_is_set(request.app.state.config.data_dir),
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
    account_form: dict[str, Any] | None = None,
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
            account_form=account_form,
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
    model_raw = str(form.get("llm_model", "")).strip()
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
        if model_raw:
            database = database_for(request)
            saved_model = save_llm_model(database, model_raw)
            sync_config_models(request.app.state.config, saved_model)
            os.environ["OPENROUTER_MODEL"] = saved_model
            # Keep in-memory config aligned without requiring a process restart.
            request.app.state.config = effective_config(
                request.app.state.config, database
            )
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


@router.post("/settings/chrome-profile", response_class=HTMLResponse)
async def upload_chrome_profile(
    request: Request,
    profile_zip: UploadFile = File(...),
) -> HTMLResponse:
    """Replace the X scrape Chromium profile from an uploaded zip."""
    import shutil
    import tempfile
    from pathlib import Path

    filename = (profile_zip.filename or "").strip()
    content_type = (profile_zip.content_type or "").lower()
    looks_zip_name = filename.lower().endswith(".zip")
    looks_zip_type = content_type in {
        "application/zip",
        "application/x-zip-compressed",
        "multipart/x-zip",
    }

    config = request.app.state.config
    dest = Path(_chrome_profile_path(config))
    tmp_parent = Path(tempfile.mkdtemp(prefix="periscope-chrome-upload-"))
    extract_root = None
    try:
        raw = await profile_zip.read()
        if not raw:
            raise ValueError("Uploaded file is empty.")
        # Filename .zip or ZIP local-file / empty-archive magic.
        is_zip_magic = raw[:4] in (b"PK\x03\x04", b"PK\x05\x06")
        if not (looks_zip_name or is_zip_magic or looks_zip_type):
            raise ValueError("Upload must be a .zip Chrome profile archive.")

        extract_root = extract_zip_to_temp(raw, tmp_parent)
        replace_chrome_profile(dest, extract_root)
        write_profile_meta(
            config.data_dir,
            source_filename=filename or "profile.zip",
            bytes_count=len(raw),
        )
        database_for(request).record_event(
            "chrome_profile_replaced",
            {
                "source_filename": filename or "profile.zip",
                "bytes": len(raw),
                "dest": str(dest),
            },
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return _response(request, tab="connections", error=str(exc))
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)

    return _response(
        request,
        tab="connections",
        message="Chrome profile replaced. Login status should show Ready.",
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


@router.post("/settings/web-password", response_class=HTMLResponse)
async def change_web_password(request: Request) -> HTMLResponse:
    """Change the web UI password (Settings → System). Requires session cookie."""
    form = await request.form()
    old_password = str(form.get("old_password", ""))
    new_password = str(form.get("new_password", ""))
    confirm = str(form.get("confirm_password", ""))
    if len(new_password) < 8:
        return _response(
            request,
            tab="system",
            error="New password must be at least 8 characters.",
        )
    if new_password != confirm:
        return _response(
            request,
            tab="system",
            error="New passwords do not match.",
        )
    try:
        web_auth.change_password(
            request.app.state.config.data_dir,
            old_password=old_password,
            new_password=new_password,
        )
    except FileNotFoundError:
        return _response(
            request,
            tab="system",
            error="Web password is not configured yet.",
        )
    except PermissionError:
        return _response(
            request,
            tab="system",
            error="Current password is incorrect.",
        )
    return _response(request, tab="system", message="Web password updated.")


# ---------------------------------------------------------------------------
# Topic X accounts (Settings → Accounts)


@router.post("/settings/x-accounts/add", response_class=HTMLResponse)
async def add_x_account(request: Request) -> HTMLResponse:
    from periscope.x_accounts import MAIN_SLUG, slugify_account, validate_slug

    form = await request.form()
    values = _account_form_values(form, include_slug=True)
    database = database_for(request)
    try:
        if not values["label"]:
            raise ValueError("Topic label is required (e.g. AI, Crypto)")
        slug = validate_slug(values["slug"] or slugify_account(values["label"]))
        if slug == MAIN_SLUG:
            raise ValueError("'main' is reserved for your main account")
        database.add_x_account(slug, **_clean_account_values(values))
        database.record_event("x_account_added", {"slug": slug, "label": values["label"]})
    except ValueError as exc:
        return _response(request, tab="accounts", error=str(exc), account_form=values)
    return _response(
        request,
        tab="accounts",
        message=(
            f"Account '{values['label']}' added. Sign it in (upload a profile zip or "
            "headed Chrome) before the next scrape."
        ),
    )


@router.post("/settings/x-accounts/{slug}/edit", response_class=HTMLResponse)
async def edit_x_account(request: Request, slug: str) -> HTMLResponse:
    database = database_for(request)
    existing = database.get_x_account(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Account not found")
    form = await request.form()
    values = _account_form_values(form, include_slug=False)
    try:
        clean = _clean_account_values(values)
        if slug == "main":
            # Main stays mapped to the existing scrape profile and behaviour.
            clean = {
                key: clean[key] for key in ("label", "x_handle", "description") if key in clean
            }
        if not clean.get("label"):
            raise ValueError("Topic label is required")
        database.update_x_account(slug, **clean)
    except ValueError as exc:
        return _response(request, tab="accounts", error=str(exc))
    return _response(request, tab="accounts", message=f"Account '{slug}' saved.")


@router.post("/settings/x-accounts/{slug}/toggle", response_class=HTMLResponse)
async def toggle_x_account(request: Request, slug: str) -> HTMLResponse:
    database = database_for(request)
    existing = database.get_x_account(slug)
    if existing is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if slug == "main":
        return _response(
            request,
            tab="accounts",
            error="The main account is always scraped; it cannot be disabled here.",
        )
    enabled = not bool(existing["enabled"])
    database.update_x_account(slug, enabled=enabled)
    state = "enabled" if enabled else "disabled"
    return _response(request, tab="accounts", message=f"Account '{slug}' {state}.")


@router.post("/settings/x-accounts/{slug}/delete", response_class=HTMLResponse)
async def delete_x_account(request: Request, slug: str) -> HTMLResponse:
    database = database_for(request)
    try:
        removed = database.delete_x_account(slug)
    except ValueError as exc:
        return _response(request, tab="accounts", error=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail="Account not found")
    database.record_event("x_account_deleted", {"slug": slug})
    return _response(
        request,
        tab="accounts",
        message=(
            f"Account '{slug}' removed. Its Chrome profile folder was left on disk; "
            "pending follows for it were dropped."
        ),
    )


@router.post("/settings/x-accounts/{slug}/chrome-profile", response_class=HTMLResponse)
async def upload_x_account_profile(
    request: Request,
    slug: str,
    profile_zip: UploadFile = File(...),
) -> HTMLResponse:
    """Replace one topic account's Chromium profile from an uploaded zip."""
    import shutil
    import tempfile
    from pathlib import Path

    from periscope.x_accounts import XAccount, profile_path

    database = database_for(request)
    row = database.get_x_account(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    account = XAccount.from_row(row)
    config = request.app.state.config
    if account.is_main and not account.profile_dir:
        dest = Path(_chrome_profile_path(config))
    else:
        dest = profile_path(account, config.data_dir)
    filename = (profile_zip.filename or "").strip()
    tmp_parent = Path(tempfile.mkdtemp(prefix="periscope-chrome-upload-"))
    try:
        raw = await profile_zip.read()
        if not raw:
            raise ValueError("Uploaded file is empty.")
        is_zip_magic = raw[:4] in (b"PK\x03\x04", b"PK\x05\x06")
        if not (filename.lower().endswith(".zip") or is_zip_magic):
            raise ValueError("Upload must be a .zip Chrome profile archive.")
        extract_root = extract_zip_to_temp(raw, tmp_parent)
        replace_chrome_profile(dest, extract_root)
        database.record_x_account_profile_upload(slug, source_filename=filename or "profile.zip")
        if account.is_main:
            write_profile_meta(
                config.data_dir, source_filename=filename or "profile.zip", bytes_count=len(raw)
            )
        database.record_event(
            "chrome_profile_replaced",
            {"account": slug, "source_filename": filename or "profile.zip", "bytes": len(raw)},
        )
    except (ValueError, RuntimeError, OSError) as exc:
        return _response(request, tab="accounts", error=f"{account.label}: {exc}")
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)
    return _response(
        request,
        tab="accounts",
        message=f"Chrome profile for '{account.label}' replaced. Next scrape checks sign-in.",
    )
