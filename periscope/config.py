"""Typed configuration loading without mixing secrets into application state."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a configuration file is present but invalid."""


def normalize_handle(value: str) -> str:
    """Return the canonical database form of an X handle."""

    handle = value.strip().removeprefix("@").lower()
    if not handle:
        raise ConfigError("X handles cannot be empty")
    return handle


@dataclass(frozen=True, slots=True)
class TopicConfig:
    name: str
    min_faves: int
    decay_weight: float = 1.0


@dataclass(frozen=True, slots=True)
class XConfig:
    list_id: int | None = None
    handles: tuple[str, ...] = ()
    fetch_limit: int = 200
    resolve_threads: bool = True
    thread_depth: int = 25


@dataclass(frozen=True, slots=True)
class ModelConfig:
    cheap: str = "claude-haiku-4-5"
    quality: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    cheap_input_usd_per_million: float = 0.0
    cheap_output_usd_per_million: float = 0.0
    quality_input_usd_per_million: float = 0.0
    quality_output_usd_per_million: float = 0.0

    def rates_for(self, model: str) -> tuple[float, float]:
        if model == self.quality:
            return (
                self.quality_input_usd_per_million,
                self.quality_output_usd_per_million,
            )
        return self.cheap_input_usd_per_million, self.cheap_output_usd_per_million


@dataclass(frozen=True, slots=True)
class PromptConfig:
    cluster: Path | None = None
    picks: Path | None = None
    weekly: Path | None = None


@dataclass(frozen=True, slots=True)
class PicksConfig:
    minimum: int = 0
    maximum: int = 5


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    timezone: str = "UTC"
    daily_times: tuple[str, ...] = ("07:30",)
    weekly_day: str = "sun"
    weekly_time: str = "09:00"
    feed_interval_minutes: int = 30


@dataclass(frozen=True, slots=True)
class DeliveryConfig:
    web_base_url: str = "http://localhost:3999"


@dataclass(frozen=True, slots=True)
class AppConfig:
    data_dir: Path
    db_path: Path
    twscrape_db_path: Path
    x: XConfig = field(default_factory=XConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    prompts: PromptConfig = field(default_factory=PromptConfig)
    picks: PicksConfig = field(default_factory=PicksConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    topics: tuple[TopicConfig, ...] = ()


@dataclass(frozen=True, slots=True)
class Secrets:
    x_auth_token: str | None = None
    x_ct0: str | None = None
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    openrouter_api_key: str | None = None
    openrouter_base_url: str | None = None

    @property
    def x_configured(self) -> bool:
        return bool(self.x_auth_token and self.x_ct0)

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"[{key}] must be a TOML table")
    return value


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if result < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return result


def _path(value: Any, base: Path) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    result = Path(str(value)).expanduser()
    return result if result.is_absolute() else base / result


def load_config(
    config_path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
) -> AppConfig:
    """Load TOML configuration, with explicit arguments taking precedence."""

    env_data_dir = os.environ.get("PERISCOPE_DATA_DIR")
    requested_data_dir = Path(data_dir or env_data_dir or "/data").expanduser()
    requested_config = Path(
        config_path or os.environ.get("PERISCOPE_CONFIG") or requested_data_dir / "config.toml"
    ).expanduser()

    if requested_config.exists():
        with requested_config.open("rb") as file:
            raw = tomllib.load(file)
    elif config_path is not None or os.environ.get("PERISCOPE_CONFIG"):
        raise ConfigError(f"Configuration file does not exist: {requested_config}")
    else:
        raw = {}

    data = _table(raw, "data")
    configured_dir = data.get("directory")
    if data_dir is not None or env_data_dir:
        resolved_data_dir = requested_data_dir
    elif configured_dir:
        candidate = Path(str(configured_dir)).expanduser()
        resolved_data_dir = (
            candidate if candidate.is_absolute() else requested_config.parent / candidate
        )
    else:
        resolved_data_dir = requested_data_dir

    db_path = _path(data.get("database", "periscope.db"), resolved_data_dir)
    twscrape_path = _path(data.get("twscrape_database", "twscrape.db"), resolved_data_dir)
    assert db_path is not None and twscrape_path is not None

    x_data = _table(raw, "x")
    list_id_value = x_data.get("list_id")
    list_id = None if list_id_value in (None, "") else _positive_int(list_id_value, "x.list_id")
    handles_value = x_data.get("handles", [])
    if not isinstance(handles_value, list):
        raise ConfigError("x.handles must be a TOML array")
    handles = tuple(dict.fromkeys(normalize_handle(str(item)) for item in handles_value))
    x = XConfig(
        list_id=list_id,
        handles=handles,
        fetch_limit=_positive_int(x_data.get("fetch_limit", 200), "x.fetch_limit"),
        resolve_threads=bool(x_data.get("resolve_threads", True)),
        thread_depth=_positive_int(x_data.get("thread_depth", 25), "x.thread_depth"),
    )

    model_data = _table(raw, "models")
    default_models = ModelConfig()
    models = ModelConfig(
        cheap=str(model_data.get("cheap", default_models.cheap)),
        quality=str(model_data.get("quality", default_models.quality)),
        max_tokens=_positive_int(model_data.get("max_tokens", 4096), "models.max_tokens"),
        cheap_input_usd_per_million=float(model_data.get("cheap_input_usd_per_million", 0.0)),
        cheap_output_usd_per_million=float(model_data.get("cheap_output_usd_per_million", 0.0)),
        quality_input_usd_per_million=float(model_data.get("quality_input_usd_per_million", 0.0)),
        quality_output_usd_per_million=float(model_data.get("quality_output_usd_per_million", 0.0)),
    )

    prompt_data = _table(raw, "prompts")
    prompts = PromptConfig(
        cluster=_path(prompt_data.get("cluster"), resolved_data_dir),
        picks=_path(prompt_data.get("picks"), resolved_data_dir),
        weekly=_path(prompt_data.get("weekly"), resolved_data_dir),
    )

    picks_data = _table(raw, "picks")
    picks = PicksConfig(
        minimum=_positive_int(picks_data.get("minimum", 0), "picks.minimum", allow_zero=True),
        maximum=_positive_int(picks_data.get("maximum", 5), "picks.maximum", allow_zero=True),
    )
    if picks.minimum > picks.maximum:
        raise ConfigError("picks.minimum cannot exceed picks.maximum")

    schedule_data = _table(raw, "schedule")
    daily_times_value = schedule_data.get("daily_times", ["07:30"])
    if not isinstance(daily_times_value, list):
        raise ConfigError("schedule.daily_times must be a TOML array")
    schedule = ScheduleConfig(
        timezone=str(schedule_data.get("timezone", "UTC")),
        daily_times=tuple(str(item) for item in daily_times_value),
        weekly_day=str(schedule_data.get("weekly_day", "sun")),
        weekly_time=str(schedule_data.get("weekly_time", "09:00")),
        feed_interval_minutes=_positive_int(
            schedule_data.get("feed_interval_minutes", 30),
            "schedule.feed_interval_minutes",
        ),
    )

    delivery_data = _table(raw, "delivery")
    delivery = DeliveryConfig(
        web_base_url=str(delivery_data.get("web_base_url", "http://localhost:3999")).rstrip("/")
    )

    topics_raw = raw.get("topics", [])
    if not isinstance(topics_raw, list):
        raise ConfigError("topics must use [[topics]] array-of-table syntax")
    topics: list[TopicConfig] = []
    for index, topic in enumerate(topics_raw):
        if not isinstance(topic, Mapping):
            raise ConfigError(f"topics[{index}] must be a table")
        name = str(topic.get("name", "")).strip()
        if not name:
            raise ConfigError(f"topics[{index}].name cannot be empty")
        topics.append(
            TopicConfig(
                name=name,
                min_faves=_positive_int(
                    topic.get("min_faves", 0),
                    f"topics[{index}].min_faves",
                    allow_zero=True,
                ),
                decay_weight=float(topic.get("decay_weight", 1.0)),
            )
        )

    return AppConfig(
        data_dir=resolved_data_dir,
        db_path=db_path,
        twscrape_db_path=twscrape_path,
        x=x,
        models=models,
        prompts=prompts,
        picks=picks,
        schedule=schedule,
        delivery=delivery,
        topics=tuple(topics),
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line_number, original in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ConfigError(f"Invalid secrets line {line_number} in {path}")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_secrets(
    path: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Secrets:
    """Load secrets from a chmod-600 env file, then overlay process variables."""

    directory = Path(data_dir or os.environ.get("PERISCOPE_DATA_DIR") or "/data").expanduser()
    secret_path = Path(path or os.environ.get("PERISCOPE_SECRETS") or directory / "secrets.env")
    values = _parse_env_file(secret_path)
    environment = os.environ if environ is None else environ
    for key in (
        "X_AUTH_TOKEN",
        "X_CT0",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
    ):
        if environment.get(key):
            values[key] = environment[key]

    def optional(key: str) -> str | None:
        value = values.get(key, "").strip()
        return value or None

    return Secrets(
        x_auth_token=optional("X_AUTH_TOKEN"),
        x_ct0=optional("X_CT0"),
        anthropic_api_key=optional("ANTHROPIC_API_KEY"),
        telegram_bot_token=optional("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=optional("TELEGRAM_CHAT_ID"),
        openrouter_api_key=optional("OPENROUTER_API_KEY"),
        openrouter_base_url=optional("OPENROUTER_BASE_URL"),
    )
