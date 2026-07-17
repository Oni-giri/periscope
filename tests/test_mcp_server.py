from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.jobs.daily import run_daily
from periscope.mcp_server import MCPWriteDisabled, PeriscopeTools, build_mcp_server
from periscope.web.app import create_app


class NullNotifier:
    async def send_digest(self, digest, text) -> None:
        return None

    async def send_alert(self, text) -> None:
        return None

    async def send_weekly(self, report) -> None:
        return None


class FakeMCPXClient:
    def __init__(self) -> None:
        self.followed: list[str] = []
        self.search_count = 0

    async def follow_account(self, handle: str) -> None:
        self.followed.append(handle)

    async def search_posts(self, query: str, *, limit: int = 20) -> list[dict]:
        self.search_count += 1
        return [
            {
                "id": f"mcp-{self.search_count}",
                "author": "search_result",
                "created_at": "2026-07-16T12:00:00+00:00",
                "text": f"Result for {query}",
            }
        ][:limit]


def _seed(app_config, timeline_fixture) -> Database:
    database = Database(app_config.db_path)
    asyncio.run(
        run_daily(
            app_config,
            Secrets(),
            database=database,
            notifier=NullNotifier(),
            mock_x=timeline_fixture,
            mock_llm=True,
            digest_date=date(2026, 7, 16),
            now=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        )
    )
    return database


def test_mcp_tool_service_and_write_gate(app_config, timeline_fixture) -> None:
    database = _seed(app_config, timeline_fixture)
    client = FakeMCPXClient()
    reads = PeriscopeTools(
        app_config,
        Secrets(),
        database,
        xclient=client,
        allow_writes=False,
    )

    assert reads.get_digest()["date"] == "2026-07-16"
    assert reads.search_archive("SQLite")["total"] >= 1
    assert reads.get_account_intel("data_builder")["recent_tweet_count"] >= 1
    assert reads.get_health()["status"] == "ok"
    with pytest.raises(MCPWriteDisabled):
        asyncio.run(reads.add_to_list("new_account"))

    database.upsert_candidate(
        handle="new_account",
        reason="Graph overlap",
        cofollow_count=3,
        overlap_pct=20,
        stats={},
    )
    writes = PeriscopeTools(
        app_config,
        Secrets(),
        database,
        xclient=client,
        allow_writes=True,
    )
    result = asyncio.run(writes.add_to_list("@new_account"))
    assert result["status"] == "accepted"
    assert client.followed == ["new_account"]
    assert database.get_candidate("new_account")["status"] == "accepted"

    for index in range(5):
        search = asyncio.run(writes.run_topic_search(f"database query {index}"))
        assert search["items"]
    with pytest.raises(RuntimeError, match="five calls"):
        asyncio.run(writes.run_topic_search("one too many"))
    assert database.get_tweet("mcp-1") is not None


def test_official_mcp_registration_and_http_mount(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    server = build_mcp_server(
        app_config,
        Secrets(),
        database,
        allow_writes=False,
    )

    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "get_digest",
        "search_archive",
        "get_cluster",
        "get_account_intel",
        "list_discovery_queue",
        "add_to_list",
        "reject_candidate",
        "run_topic_search",
        "get_health",
    }
    _, structured_health = asyncio.run(server.call_tool("get_health", {}))
    assert structured_health["status"] == "ok"

    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        initialized = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Host": "localhost:3999",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "Periscope"
