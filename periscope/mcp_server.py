"""Official MCP server exposing Periscope's structured read and gated write tools."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from periscope.config import AppConfig, Secrets, load_config, load_secrets
from periscope.db import Database, SearchQueryError
from periscope.jobs.fetchonly import build_xclient
from periscope.xclient import XClient


class MCPWriteDisabled(PermissionError):
    """Raised when an MCP mutation is requested without the explicit write gate."""


class PeriscopeTools:
    def __init__(
        self,
        config: AppConfig,
        secrets: Secrets,
        database: Database,
        *,
        xclient: XClient | None = None,
        allow_writes: bool | None = None,
    ):
        self.config = config
        self.secrets = secrets
        self.database = database
        self.xclient = xclient
        self.allow_writes = (
            os.environ.get("PERISCOPE_MCP_ALLOW_WRITES") == "1"
            if allow_writes is None
            else allow_writes
        )

    def _require_writes(self) -> None:
        if not self.allow_writes:
            raise MCPWriteDisabled(
                "MCP writes are disabled. Set PERISCOPE_MCP_ALLOW_WRITES=1 "
                "for this trusted process."
            )

    def _client(self) -> XClient:
        if self.xclient is None:
            self.xclient = build_xclient(self.config, self.secrets)
        return self.xclient

    def get_digest(self, date: str | None = None) -> dict[str, Any] | None:
        """Return a dated digest, or the latest digest when date is omitted."""

        digest = self.database.get_digest(date) if date else self.database.latest_digest()
        return digest

    def search_archive(
        self,
        query: str,
        since: str | None = None,
        account: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search raw stored posts with optional date and account filters."""

        clean = query.strip()
        if not clean:
            raise ValueError("query cannot be empty")
        try:
            page = self.database.archive_page(
                query=clean,
                from_date=since,
                account=account,
                page_size=min(max(limit, 1), 100),
            )
        except SearchQueryError as exc:
            raise ValueError(f"Invalid FTS5 search query: {exc}") from exc
        return {"items": page["items"], "total": page["total"]}

    def get_cluster(self, cluster_id: str) -> dict[str, Any] | None:
        """Return a cluster with its source posts in thread order."""

        return self.database.get_cluster(cluster_id)

    def get_account_intel(self, handle: str) -> dict[str, Any]:
        """Return local account state, graph snapshot, candidate evidence, and recent posts."""

        canonical = handle.strip().removeprefix("@").lower()
        accounts = {
            str(item["handle"]): item for item in self.database.list_accounts(include_muted=True)
        }
        recent = self.database.archive_page(account=canonical, page_size=20)
        snapshot = self.database.latest_follow_snapshot(canonical)
        candidate = self.database.get_candidate(canonical)
        return {
            "handle": canonical,
            "curated": accounts.get(canonical),
            "candidate": candidate,
            "latest_follow_snapshot": snapshot,
            "recent_tweets": recent["items"],
            "recent_tweet_count": recent["total"],
        }

    def list_discovery_queue(self) -> list[dict[str, Any]]:
        """List pending candidates in review order."""

        return self.database.list_candidates()

    async def add_to_list(self, handle: str) -> dict[str, Any]:
        """Follow on X and add locally, only when MCP writes are explicitly enabled."""

        self._require_writes()
        canonical = handle.strip().removeprefix("@").lower()
        if not canonical:
            raise ValueError("handle cannot be empty")
        await self._client().follow_account(canonical)
        candidate = self.database.get_candidate(canonical)
        if candidate and candidate["status"] == "pending":
            self.database.review_candidate(canonical, "accepted")
        else:
            self.database.add_account(canonical, note="Added through MCP")
        self.database.record_event("mcp_account_added", {"handle": canonical})
        return {"handle": canonical, "status": "accepted"}

    def reject_candidate(self, handle: str) -> dict[str, Any]:
        """Reject and suppress a candidate, only when MCP writes are enabled."""

        self._require_writes()
        canonical = handle.strip().removeprefix("@").lower()
        if not self.database.review_candidate(canonical, "rejected"):
            raise ValueError(f"Pending candidate @{canonical} was not found")
        self.database.record_event("mcp_candidate_rejected", {"handle": canonical})
        return {"handle": canonical, "status": "rejected"}

    async def run_topic_search(self, query: str) -> dict[str, Any]:
        """Run one gated X search, limited to five calls per rolling hour."""

        self._require_writes()
        clean = query.strip()
        if not clean or len(clean) > 200:
            raise ValueError("query must contain 1 to 200 characters")
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        recent = 0
        for event in self.database.recent_events(limit=200):
            if event["kind"] != "mcp_topic_search":
                continue
            try:
                at = datetime.fromisoformat(str(event["at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            if at >= cutoff:
                recent += 1
        if recent >= 5:
            raise RuntimeError("MCP topic search limit reached: five calls per rolling hour")

        fetch_id = self.database.start_fetch("mcp_topic_search")
        posts = await self._client().search_posts(clean, limit=20)
        new_items = 0
        ids: list[str] = []
        try:
            for post in posts:
                if self.database.store_tweet(post, fetch_id=fetch_id):
                    new_items += 1
                tweet_id = str(post.get("id_str") or post.get("id") or "")
                if tweet_id:
                    ids.append(tweet_id)
            self.database.finish_fetch(fetch_id, new_items=new_items)
        except Exception as exc:
            self.database.finish_fetch(
                fetch_id,
                new_items=new_items,
                errors=1,
                note=str(exc),
            )
            raise
        self.database.record_event("mcp_topic_search", {"query": clean})
        return {
            "query": clean,
            "new_items": new_items,
            "items": self.database.get_tweets(ids),
        }

    def get_health(self) -> dict[str, Any]:
        """Return database, fetch, cookie, credential-flag, and spend health."""

        return self.database.health_summary()


def build_mcp_server(
    config: AppConfig,
    secrets: Secrets,
    database: Database,
    *,
    xclient: XClient | None = None,
    allow_writes: bool | None = None,
    host: str = "127.0.0.1",
    port: int = 4000,
    streamable_http_path: str = "/mcp",
) -> FastMCP:
    tools = PeriscopeTools(
        config,
        secrets,
        database,
        xclient=xclient,
        allow_writes=allow_writes,
    )
    web_url = urlparse(config.delivery.web_base_url)
    allowed_hosts = ["127.0.0.1:*", "localhost:*"]
    if web_url.hostname:
        allowed_hosts.append(f"{web_url.hostname}:*")
        allowed_hosts.append(web_url.hostname)
    server = FastMCP(
        "Periscope",
        instructions=(
            "Read a finite personal X digest and archive. Mutations require an "
            "explicit server-side write gate."
        ),
        host=host,
        port=port,
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[config.delivery.web_base_url],
        ),
    )
    server.tool()(tools.get_digest)
    server.tool()(tools.search_archive)
    server.tool()(tools.get_cluster)
    server.tool()(tools.get_account_intel)
    server.tool()(tools.list_discovery_queue)
    server.tool()(tools.add_to_list)
    server.tool()(tools.reject_candidate)
    server.tool()(tools.run_topic_search)
    server.tool()(tools.get_health)
    server.periscope_tools = tools
    return server


def _configured_server(*, http: bool) -> FastMCP:
    config = load_config()
    secrets = load_secrets(data_dir=config.data_dir)
    database = Database(config.db_path)
    database.initialize()
    database.seed(config, secrets)
    return build_mcp_server(
        config,
        secrets,
        database,
        host=os.environ.get("PERISCOPE_MCP_HOST", "0.0.0.0" if http else "127.0.0.1"),
        port=int(os.environ.get("PERISCOPE_MCP_PORT", "4000")),
    )


def main() -> None:
    _configured_server(http=False).run(transport="stdio")


def http_main() -> None:
    _configured_server(http=True).run(transport="streamable-http")


if __name__ == "__main__":
    main()
