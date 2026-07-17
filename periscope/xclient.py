"""X data-source adapters with an explicit fixture-backed development mode."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol

from periscope.config import Secrets, XConfig


class XClientError(RuntimeError):
    """Base exception for X data-source failures."""


class CookieDeadError(XClientError):
    """Raised for an authentication failure that needs one operator alert."""


class XClient(Protocol):
    async def timeline(self, *, limit: int) -> AsyncIterator[dict[str, Any]]: ...

    async def thread(
        self, tweet: Mapping[str, Any], *, depth: int
    ) -> AsyncIterator[dict[str, Any]]: ...

    async def following_handles(self, handle: str, *, limit: int = 500) -> list[str]: ...

    async def search_posts(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]: ...

    async def profile(self, handle: str) -> dict[str, Any]: ...

    async def follow_account(self, handle: str) -> None: ...


def payload_id(payload: Mapping[str, Any]) -> str:
    return str(payload.get("id_str", payload.get("id", "")))


def payload_author(payload: Mapping[str, Any]) -> str:
    direct = payload.get("author", payload.get("username"))
    user = payload.get("user")
    if direct:
        return str(direct).removeprefix("@").lower()
    if isinstance(user, Mapping):
        return str(user.get("username", user.get("screen_name", ""))).removeprefix("@").lower()
    return ""


def payload_thread_root(payload: Mapping[str, Any]) -> str:
    return str(
        payload.get(
            "thread_root_id",
            payload.get(
                "conversationIdStr",
                payload.get(
                    "conversationId", payload.get("conversation_id_str", payload_id(payload))
                ),
            ),
        )
    )


def embedded_tweets(payload: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield quoted/retweeted payloads included in a source snapshot."""

    for key in ("quotedTweet", "quoted_tweet", "retweetedTweet", "retweeted_tweet"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            copied = dict(nested)
            yield copied
            yield from embedded_tweets(copied)


class MockXClient:
    """Read deterministic tweet snapshots from a JSON fixture."""

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        self._document: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._document is None:
            if not self.fixture_path.exists():
                raise XClientError(f"Mock X fixture does not exist: {self.fixture_path}")
            raw = json.loads(self.fixture_path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                raw = {"tweets": raw}
            if not isinstance(raw, dict) or not isinstance(raw.get("tweets"), list):
                raise XClientError(
                    "Mock X fixture must be a JSON array or an object with a tweets array"
                )
            self._document = raw
        return self._document

    async def timeline(self, *, limit: int) -> AsyncIterator[dict[str, Any]]:
        for item in self._load()["tweets"][:limit]:
            if not isinstance(item, dict):
                raise XClientError("Each mock tweet must be a JSON object")
            yield dict(item)

    async def thread(
        self,
        tweet: Mapping[str, Any],
        *,
        depth: int,
    ) -> AsyncIterator[dict[str, Any]]:
        thread = tweet.get("thread", [])
        if not isinstance(thread, list):
            raise XClientError(f"Mock thread for tweet {payload_id(tweet)} must be an array")
        for item in thread[:depth]:
            if isinstance(item, dict):
                yield dict(item)

    async def following_handles(self, handle: str, *, limit: int = 500) -> list[str]:
        graph = self._load().get("following", {})
        if not isinstance(graph, Mapping):
            return []
        values = graph.get(handle.removeprefix("@").lower(), [])
        if not isinstance(values, list):
            return []
        return [
            str(value).removeprefix("@").lower() for value in values[:limit] if str(value).strip()
        ]

    async def search_posts(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        searches = self._load().get("searches", {})
        if not isinstance(searches, Mapping):
            return []
        values = searches.get(query, searches.get(query.split(" min_faves:", 1)[0], []))
        if not isinstance(values, list):
            return []
        return [dict(item) for item in values[:limit] if isinstance(item, Mapping)]

    async def profile(self, handle: str) -> dict[str, Any]:
        canonical = handle.removeprefix("@").lower()
        profiles = self._load().get("profiles", {})
        value = profiles.get(canonical, {}) if isinstance(profiles, Mapping) else {}
        result = dict(value) if isinstance(value, Mapping) else {}
        result.setdefault("username", canonical)
        result.setdefault("displayname", canonical)
        return result

    async def follow_account(self, handle: str) -> None:
        followed = self._load().setdefault("followed", [])
        if isinstance(followed, list):
            followed.append(handle.removeprefix("@").lower())


def _status_code(error: BaseException) -> int | None:
    for candidate in (error, getattr(error, "response", None)):
        if candidate is None:
            continue
        value = getattr(candidate, "status_code", None)
        if isinstance(value, int):
            return value
    return None


class TwscrapeXClient:
    """Live adapter around twscrape's public parsed timeline methods."""

    _account_name = "periscope-cookie"

    def __init__(
        self,
        config: XConfig,
        secrets: Secrets,
        *,
        pool_path: str | Path,
    ):
        if config.list_id is None:
            raise XClientError("x.list_id is required outside --mock-x mode")
        if not secrets.x_configured:
            raise XClientError("X_AUTH_TOKEN and X_CT0 are required outside --mock-x mode")
        self.config = config
        self.secrets = secrets
        self.pool_path = Path(pool_path)
        self._api: Any | None = None

    async def _get_api(self) -> Any:
        if self._api is not None:
            return self._api
        try:
            from twscrape import API
        except ImportError as exc:  # pragma: no cover - exercised only without runtime deps
            raise XClientError("twscrape is not installed; run `uv sync`") from exc

        self.pool_path.parent.mkdir(parents=True, exist_ok=True)
        api = API(str(self.pool_path), raise_when_no_account=True)
        account = await api.pool.get_account(self._account_name)
        cookies = {
            "auth_token": self.secrets.x_auth_token,
            "ct0": self.secrets.x_ct0,
        }
        if account is None:
            cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
            await api.pool.add_account_cookies(self._account_name, cookie_header)
        else:
            # The pool is dedicated to Periscope, so refreshing this one session is safe.
            account.cookies.update(cookies)
            account.active = True
            account.error_msg = None
            await api.pool.save(account)
        self._api = api
        return api

    async def _cookies_rejected(self, error: BaseException) -> bool:
        if _status_code(error) in {401, 403}:
            return True
        if error.__class__.__name__ != "NoAccountError" or self._api is None:
            return False
        account = await self._api.pool.get_account(self._account_name)
        # An active account can be temporarily unavailable because its endpoint
        # lock has not reset yet. That is a rate-limit condition, not cookie death.
        return account is None or not account.active

    async def timeline(self, *, limit: int) -> AsyncIterator[dict[str, Any]]:
        try:
            api = await self._get_api()
            assert self.config.list_id is not None
            async for tweet in api.list_timeline(self.config.list_id, limit=limit):
                yield tweet.dict()
        except Exception as exc:
            if await self._cookies_rejected(exc):
                raise CookieDeadError("X rejected the configured cookies") from exc
            raise XClientError(f"X list timeline failed: {exc}") from exc

    async def thread(
        self,
        tweet: Mapping[str, Any],
        *,
        depth: int,
    ) -> AsyncIterator[dict[str, Any]]:
        reply_count = int(tweet.get("replyCount", tweet.get("reply_count", 0)) or 0)
        is_reply = bool(
            tweet.get("inReplyToTweetIdStr")
            or tweet.get("inReplyToTweetId")
            or tweet.get("in_reply_to_status_id_str")
        )
        if reply_count <= 0 and not is_reply:
            return
        root_id = payload_thread_root(tweet)
        author = payload_author(tweet)
        if not root_id:
            return
        try:
            api = await self._get_api()
            async for item in api.tweet_thread(int(root_id), limit=depth):
                item_dict = item.dict()
                if payload_author(item_dict) == author:
                    yield item_dict
        except Exception as exc:
            if await self._cookies_rejected(exc):
                raise CookieDeadError("X rejected the configured cookies") from exc
            raise XClientError(f"X thread expansion failed for {root_id}: {exc}") from exc

    async def _user(self, handle: str) -> Any:
        canonical = handle.removeprefix("@").lower()
        try:
            api = await self._get_api()
            user = await api.user_by_login(canonical)
        except Exception as exc:
            if await self._cookies_rejected(exc):
                raise CookieDeadError("X rejected the configured cookies") from exc
            raise XClientError(f"X profile lookup failed for @{canonical}: {exc}") from exc
        if user is None:
            raise XClientError(f"X account @{canonical} was not found")
        return user

    async def following_handles(self, handle: str, *, limit: int = 500) -> list[str]:
        user = await self._user(handle)
        result: list[str] = []
        try:
            api = await self._get_api()
            async for followed in api.following(int(user.id), limit=limit):
                username = str(getattr(followed, "username", "")).removeprefix("@").lower()
                if username:
                    result.append(username)
        except Exception as exc:
            if await self._cookies_rejected(exc):
                raise CookieDeadError("X rejected the configured cookies") from exc
            raise XClientError(f"X following lookup failed for @{handle}: {exc}") from exc
        return list(dict.fromkeys(result))

    async def search_posts(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        try:
            api = await self._get_api()
            async for tweet in api.search(query, limit=limit):
                result.append(tweet.dict())
        except Exception as exc:
            if await self._cookies_rejected(exc):
                raise CookieDeadError("X rejected the configured cookies") from exc
            raise XClientError(f"X search failed for {query!r}: {exc}") from exc
        return result

    async def profile(self, handle: str) -> dict[str, Any]:
        user = await self._user(handle)
        return dict(user.dict())

    async def follow_account(self, handle: str) -> None:
        """Follow after an explicit review action using X's cookie-authenticated web API."""

        user = await self._user(handle)
        api = await self._get_api()
        account = await api.pool.get_account(self._account_name)
        if account is None:
            raise CookieDeadError("The configured X cookie account is unavailable")
        client = account.make_client()
        try:
            response = await client.post(
                "https://x.com/i/api/1.1/friendships/create.json",
                data={"user_id": str(user.id), "follow": "true"},
            )
            if response.status_code in {401, 403}:
                raise CookieDeadError("X rejected the configured cookies")
            response.raise_for_status()
        except CookieDeadError:
            raise
        except Exception as exc:
            raise XClientError(f"X follow failed for @{handle}: {exc}") from exc
        finally:
            await client.aclose()
