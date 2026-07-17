from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace

import pytest

from periscope.config import load_config, load_secrets
from periscope.db import Database
from periscope.jobs.fetchonly import run_fetch
from periscope.llm import AnthropicJSONLLM
from periscope.telegram.bot import NullNotifier

RUN_LIVE_TESTS = os.environ.get("PERISCOPE_RUN_LIVE_TESTS") == "1"


@pytest.mark.live
@pytest.mark.skipif(
    not RUN_LIVE_TESTS,
    reason="set PERISCOPE_RUN_LIVE_TESTS=1 to contact configured services",
)
def test_read_only_live_service_smoke(tmp_path) -> None:
    """Probe X, Anthropic, and optional Telegram without external writes."""

    async def run() -> None:
        source_config = load_config()
        secrets = load_secrets(data_dir=source_config.data_dir)
        if source_config.x.list_id is None:
            pytest.fail("The live config must define x.list_id")
        if not secrets.x_configured:
            pytest.fail("The live test requires X_AUTH_TOKEN and X_CT0")
        if not secrets.anthropic_configured:
            pytest.fail("The live test requires ANTHROPIC_API_KEY")

        config = replace(
            source_config,
            data_dir=tmp_path,
            db_path=tmp_path / "periscope.db",
            twscrape_db_path=tmp_path / "twscrape.db",
            x=replace(
                source_config.x,
                fetch_limit=min(source_config.x.fetch_limit, 3),
                resolve_threads=False,
            ),
        )
        database = Database(config.db_path)
        result = await run_fetch(
            config,
            secrets,
            database=database,
            notifier=NullNotifier(),
        )
        assert result.errors == 0, result.note
        assert result.seen_items > 0, "The configured X list returned no posts"
        assert database.rows("SELECT id FROM tweets LIMIT 1")

        assert secrets.anthropic_api_key is not None
        llm = AnthropicJSONLLM(
            secrets.anthropic_api_key,
            database=database,
            models=config.models,
        )
        try:
            response = await llm.complete_json(
                "live-smoke",
                system_prompt="Return only a JSON object matching the user's request.",
                payload={"request": 'Return {"ok": true}.'},
                model=config.models.cheap,
            )
            assert isinstance(response, Mapping)
            assert response.get("ok") is True
        finally:
            if llm._client is not None:
                await llm._client.close()

        if secrets.telegram_configured:
            from telegram import Bot

            assert secrets.telegram_bot_token is not None
            assert secrets.telegram_chat_id is not None
            async with Bot(secrets.telegram_bot_token) as bot:
                identity = await bot.get_me()
                chat = await bot.get_chat(secrets.telegram_chat_id)
            assert identity.id
            assert chat.id

    asyncio.run(run())
