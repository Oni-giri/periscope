from __future__ import annotations

from pathlib import Path

import pytest

from periscope.config import AppConfig, PicksConfig, XConfig


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "periscope.db",
        twscrape_db_path=tmp_path / "twscrape.db",
        x=XConfig(
            handles=("data_builder", "model_reader"),
            fetch_limit=100,
            resolve_threads=True,
            thread_depth=25,
        ),
        picks=PicksConfig(minimum=0, maximum=3),
    )


@pytest.fixture
def timeline_fixture() -> Path:
    return Path(__file__).parent / "fixtures" / "timeline.json"
