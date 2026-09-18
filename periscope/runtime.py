"""Validated runtime overrides and secret-safe local file updates."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from periscope.config import (
    AppConfig,
    TopicConfig,
    load_secrets,
)
from periscope.db import Database

_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_SECRET_KEYS = (
    "X_AUTH_TOKEN",
    "X_CT0",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
)


class RuntimeSettingsError(ValueError):
    """Raised when a settings mutation would create an invalid runtime config."""


def _valid_time(value: str, field: str) -> str:
    result = value.strip()
    if not _TIME.fullmatch(result):
        raise RuntimeSettingsError(f"{field} must use 24-hour HH:MM format")
    return result


def effective_config(config: AppConfig, database: Database) -> AppConfig:
    """Overlay validated SQLite settings onto immutable file configuration."""

    values = database.get_settings()
    daily_raw = values.get("schedule.daily_times")
    daily_times = (
        tuple(item for item in daily_raw.split(",") if item)
        if daily_raw
        else config.schedule.daily_times
    )
    schedule = replace(
        config.schedule,
        timezone=values.get("schedule.timezone", config.schedule.timezone),
        daily_times=daily_times,
        weekly_day=values.get("schedule.weekly_day", config.schedule.weekly_day),
        weekly_time=values.get("schedule.weekly_time", config.schedule.weekly_time),
        feed_interval_minutes=int(
            values.get(
                "schedule.feed_interval_minutes",
                config.schedule.feed_interval_minutes,
            )
        ),
    )
    picks = replace(
        config.picks,
        minimum=int(values.get("picks.minimum", config.picks.minimum)),
        maximum=int(values.get("picks.maximum", config.picks.maximum)),
    )
    db_topics = database.list_topics()
    topics = tuple(
        TopicConfig(
            name=str(item["name"]),
            min_faves=int(item["min_faves"]),
            decay_weight=float(item["decay_weight"]),
        )
        for item in db_topics
    )
    return replace(config, schedule=schedule, picks=picks, topics=topics)


def save_schedule_settings(
    database: Database,
    *,
    timezone: str,
    daily_times: list[str],
    weekly_day: str,
    weekly_time: str,
    feed_interval_minutes: int,
    picks_minimum: int,
    picks_maximum: int,
    clustering_aggressiveness: int,
    topic_decay: bool,
) -> None:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeSettingsError(f"Unknown timezone: {timezone}") from exc
    clean_daily = [_valid_time(item, "Daily time") for item in daily_times if item.strip()]
    if not clean_daily or len(clean_daily) > 2:
        raise RuntimeSettingsError("Choose one or two daily digest times")
    day = weekly_day.strip().lower()[:3]
    if day not in _DAYS:
        raise RuntimeSettingsError("Weekly day must be Monday through Sunday")
    weekly = _valid_time(weekly_time, "Weekly time")
    if not 5 <= feed_interval_minutes <= 1440:
        raise RuntimeSettingsError("Feed interval must be between 5 and 1440 minutes")
    if not 0 <= picks_minimum <= picks_maximum <= 20:
        raise RuntimeSettingsError("Pick limits must satisfy 0 <= minimum <= maximum <= 20")
    if not 0 <= clustering_aggressiveness <= 100:
        raise RuntimeSettingsError("Clustering aggressiveness must be from 0 to 100")
    database.update_settings(
        {
            "schedule.timezone": timezone,
            "schedule.daily_times": ",".join(clean_daily),
            "schedule.weekly_day": day,
            "schedule.weekly_time": weekly,
            "schedule.feed_interval_minutes": str(feed_interval_minutes),
            "picks.minimum": str(picks_minimum),
            "picks.maximum": str(picks_maximum),
            "clustering.aggressiveness": str(clustering_aggressiveness),
            "topic_decay.enabled": "true" if topic_decay else "false",
        }
    )


ARCHIVE_DEFAULT_FILTERS = ("has_action", "kept", "all")
FEED_MAX_POSTS_DEFAULT = 50
FEED_MAX_POSTS_MIN = 20
FEED_MAX_POSTS_MAX = 200


def clamp_feed_max_posts(value: int | str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeSettingsError("Feed max posts must be an integer") from exc
    return max(FEED_MAX_POSTS_MIN, min(FEED_MAX_POSTS_MAX, parsed))


def save_ui_settings(
    database: Database,
    *,
    feed_max_posts: int | str,
    archive_default_filter: str,
) -> None:
    """Persist reading-surface knobs (feed cap + archive landing filter)."""

    feed_max = clamp_feed_max_posts(feed_max_posts)
    archive_filter = str(archive_default_filter or "").strip().lower()
    if archive_filter not in ARCHIVE_DEFAULT_FILTERS:
        raise RuntimeSettingsError(
            "Archive default filter must be has_action, kept, or all"
        )
    database.update_settings(
        {
            "ui.feed_max_posts": str(feed_max),
            "ui.archive_default_filter": archive_filter,
        }
    )


def ui_settings(database: Database) -> dict[str, str | int]:
    values = database.get_settings()
    try:
        feed_max = clamp_feed_max_posts(
            values.get("ui.feed_max_posts", FEED_MAX_POSTS_DEFAULT)
        )
    except RuntimeSettingsError:
        feed_max = FEED_MAX_POSTS_DEFAULT
    archive_filter = values.get("ui.archive_default_filter", "has_action")
    if archive_filter not in ARCHIVE_DEFAULT_FILTERS:
        archive_filter = "has_action"
    return {
        "feed_max_posts": feed_max,
        "archive_default_filter": archive_filter,
    }


def secrets_path(config: AppConfig) -> Path:
    configured = os.environ.get("PERISCOPE_SECRETS")
    return Path(configured).expanduser() if configured else config.data_dir / "secrets.env"


def _read_secret_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for original in path.read_text(encoding="utf-8").splitlines():
        line = original.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def update_secrets_file(config: AppConfig, updates: dict[str, str]) -> Path:
    """Atomically update only supplied, non-empty values and enforce mode 600."""

    unknown = set(updates) - set(_SECRET_KEYS)
    if unknown:
        raise RuntimeSettingsError(f"Unsupported secret keys: {', '.join(sorted(unknown))}")
    path = secrets_path(config)
    values = _read_secret_values(path)
    for key, value in updates.items():
        if value.strip():
            values[key] = value.strip()

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    body = "# Managed by Periscope. Process environment variables take precedence.\n"
    body += "".join(
        f"{key}={shlex.quote(values[key])}\n" for key in _SECRET_KEYS if values.get(key)
    )
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return path


def reload_secrets(config: AppConfig):
    return load_secrets(secrets_path(config), data_dir=config.data_dir)


def picks_prompt_path(config: AppConfig) -> Path:
    return config.prompts.picks or config.data_dir / "prompts" / "picks_prompt.md"


def read_picks_prompt(config: AppConfig) -> tuple[str, bool]:
    path = picks_prompt_path(config)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    default_exists = path.with_suffix(path.suffix + ".default").exists()
    return text, default_exists


def save_picks_prompt(config: AppConfig, database: Database, content: str) -> Path:
    clean = content.strip()
    if not clean:
        raise RuntimeSettingsError("The picks prompt cannot be empty")
    path = picks_prompt_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(clean + "\n", encoding="utf-8")
    temporary.replace(path)
    database.record_event("picks_prompt_updated", {"path": str(path)})
    return path


def reset_picks_prompt(config: AppConfig, database: Database) -> Path:
    path = picks_prompt_path(config)
    default = path.with_suffix(path.suffix + ".default")
    if not default.exists():
        raise RuntimeSettingsError("No default picks prompt has been installed yet")
    return save_picks_prompt(config, database, default.read_text(encoding="utf-8"))


def _prompts_dir(config: AppConfig) -> Path:
    return config.data_dir / "prompts"


def _write_prompt_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def curator_prompt_path(config: AppConfig) -> Path:
    from periscope.x_scrape.curate_feeds import CURATOR_PROMPT_FILENAME

    return _prompts_dir(config) / CURATOR_PROMPT_FILENAME


def read_curator_prompt(config: AppConfig) -> tuple[str, bool]:
    from periscope.x_scrape.curate_feeds import DEFAULT_SYSTEM_TEMPLATE

    path = curator_prompt_path(config)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, True
    return DEFAULT_SYSTEM_TEMPLATE.strip(), False


def save_curator_prompt(config: AppConfig, database: Database, content: str) -> Path:
    from periscope.x_scrape.curate_feeds import DEFAULT_SYSTEM_TEMPLATE

    clean = content.strip()
    if not clean:
        raise RuntimeSettingsError("The curator system prompt cannot be empty")
    path = curator_prompt_path(config)
    if clean == DEFAULT_SYSTEM_TEMPLATE.strip():
        if path.exists():
            path.unlink()
        database.record_event("curator_prompt_reset", {"path": str(path), "via": "save-default"})
        return path
    _write_prompt_file(path, clean)
    database.record_event("curator_prompt_updated", {"path": str(path)})
    return path


def reset_curator_prompt(config: AppConfig, database: Database) -> Path:
    path = curator_prompt_path(config)
    if path.exists():
        path.unlink()
    database.record_event("curator_prompt_reset", {"path": str(path)})
    return path


def enrich_prompt_path(config: AppConfig) -> Path:
    from periscope.x_scrape.enrich_actions import ENRICH_PROMPT_FILENAME

    return _prompts_dir(config) / ENRICH_PROMPT_FILENAME


def read_enrich_prompt(config: AppConfig) -> tuple[str, bool]:
    from periscope.x_scrape.enrich_actions import DEFAULT_ENRICH_SYSTEM

    path = enrich_prompt_path(config)
    if path.is_file():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text, True
    return DEFAULT_ENRICH_SYSTEM.strip(), False


def save_enrich_prompt(config: AppConfig, database: Database, content: str) -> Path:
    from periscope.x_scrape.enrich_actions import DEFAULT_ENRICH_SYSTEM

    clean = content.strip()
    if not clean:
        raise RuntimeSettingsError("The enrich actions prompt cannot be empty")
    path = enrich_prompt_path(config)
    if clean == DEFAULT_ENRICH_SYSTEM.strip():
        if path.exists():
            path.unlink()
        database.record_event("enrich_prompt_reset", {"path": str(path), "via": "save-default"})
        return path
    _write_prompt_file(path, clean)
    database.record_event("enrich_prompt_updated", {"path": str(path)})
    return path


def reset_enrich_prompt(config: AppConfig, database: Database) -> Path:
    path = enrich_prompt_path(config)
    if path.exists():
        path.unlink()
    database.record_event("enrich_prompt_reset", {"path": str(path)})
    return path
