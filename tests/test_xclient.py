from __future__ import annotations

import asyncio
from types import SimpleNamespace

from periscope.config import Secrets, XConfig
from periscope.xclient import TwscrapeXClient


class NoAccountError(Exception):
    pass


class FakePool:
    def __init__(self, active: bool):
        self.active = active

    async def get_account(self, username):
        return SimpleNamespace(active=self.active)


def _client(tmp_path, *, active: bool) -> TwscrapeXClient:
    client = TwscrapeXClient(
        XConfig(list_id=1),
        Secrets(x_auth_token="token", x_ct0="ct0"),
        pool_path=tmp_path / "pool.db",
    )
    client._api = SimpleNamespace(pool=FakePool(active))
    return client


def test_active_but_locked_account_is_not_cookie_death(tmp_path) -> None:
    client = _client(tmp_path, active=True)

    assert not asyncio.run(client._cookies_rejected(NoAccountError()))


def test_inactive_account_is_cookie_death(tmp_path) -> None:
    client = _client(tmp_path, active=False)

    assert asyncio.run(client._cookies_rejected(NoAccountError()))


def test_http_auth_status_is_cookie_death(tmp_path) -> None:
    client = _client(tmp_path, active=True)
    error = RuntimeError("forbidden")
    error.status_code = 403

    assert asyncio.run(client._cookies_rejected(error))
