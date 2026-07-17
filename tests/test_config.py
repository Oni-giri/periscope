from __future__ import annotations

from pathlib import Path

import pytest

from periscope.config import ConfigError, load_config, load_secrets


def test_load_config_normalizes_handles_and_resolves_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[data]
directory = "state"
database = "digest.db"

[x]
handles = ["@Example", "example", "Second"]
fetch_limit = 42

[[topics]]
name = "Databases"
min_faves = 30
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.data_dir == tmp_path / "state"
    assert config.db_path == tmp_path / "state" / "digest.db"
    assert config.x.handles == ("example", "second")
    assert config.x.fetch_limit == 42
    assert config.models.cheap == "claude-haiku-4-5"
    assert config.topics[0].name == "Databases"


def test_explicit_data_dir_wins_over_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[data]\ndirectory = "ignored"\n', encoding="utf-8")

    config = load_config(config_file, data_dir=tmp_path / "runtime")

    assert config.data_dir == tmp_path / "runtime"


def test_secret_environment_overrides_file(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text(
        "X_AUTH_TOKEN=file-token\nX_CT0=file-ct0\nANTHROPIC_API_KEY=file-key\n",
        encoding="utf-8",
    )

    secrets = load_secrets(
        secrets_file,
        environ={"ANTHROPIC_API_KEY": "process-key"},
    )

    assert secrets.x_configured
    assert secrets.anthropic_api_key == "process-key"
    assert not secrets.telegram_configured


def test_invalid_pick_bounds_are_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[picks]\nminimum = 6\nmaximum = 5\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="cannot exceed"):
        load_config(config_file, data_dir=tmp_path)
