"""Push-only Telegram delivery used by jobs and health alerts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from periscope.config import AppConfig, DeliveryConfig, Secrets
from periscope.db import Database
from periscope.xclient import TwscrapeXClient, XClient


class Notifier(Protocol):
    async def send_digest(self, digest: Mapping[str, Any], text: str) -> None: ...

    async def send_alert(self, text: str) -> None: ...

    async def send_weekly(self, report: Mapping[str, Any]) -> None: ...


class NullNotifier:
    async def send_digest(self, digest: Mapping[str, Any], text: str) -> None:
        return None

    async def send_alert(self, text: str) -> None:
        return None

    async def send_weekly(self, report: Mapping[str, Any]) -> None:
        return None


class TelegramNotifier:
    """Minimal async Telegram sender; browsing commands arrive in a later phase."""

    def __init__(self, token: str, chat_id: str, delivery: DeliveryConfig):
        self.token = token
        self.chat_id = chat_id
        self.delivery = delivery
        self._bot: Any | None = None

    def _client(self) -> Any:
        if self._bot is None:
            try:
                from telegram import Bot
            except ImportError as exc:  # pragma: no cover - only without runtime deps
                raise RuntimeError("python-telegram-bot is not installed; run uv sync") from exc
            self._bot = Bot(token=self.token)
        return self._bot

    async def send_digest(self, digest: Mapping[str, Any], text: str) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        date = str(digest["date"])
        link = f"{self.delivery.web_base_url}/digest/{date}"
        rows = []
        clusters = {str(item["id"]): item for item in digest.get("clusters", [])}
        picks = {str(item["id"]): item for item in digest.get("picks", [])}
        for item in digest.get("items", [])[:10]:
            item_id = str(item["id"])
            if item.get("type") == "cluster" and item_id in clusters:
                cluster = clusters[item_id]
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"Open: {str(cluster['headline'])[:40]}",
                            url=f"{self.delivery.web_base_url}/cluster/{item_id}",
                        )
                    ]
                )
            elif item_id in picks:
                pick = picks[item_id]
                tweet_id = str(pick["tweet_id"])
                rows.append(
                    [
                        InlineKeyboardButton(
                            f"Keep @{pick['tweet']['author']}",
                            callback_data=f"keep:{tweet_id}",
                        )
                    ]
                )
        await self._client().send_message(
            chat_id=self.chat_id,
            text=f"{text}\n\n{link}",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )

    async def send_alert(self, text: str) -> None:
        await self._client().send_message(chat_id=self.chat_id, text=f"Periscope alert\n\n{text}")

    async def send_weekly(self, report: Mapping[str, Any]) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        week = str(report["week"])
        themes = report.get("themes", [])
        lines = [f"Periscope weekly {week}"]
        for index, theme in enumerate(themes[:5], start=1):
            if isinstance(theme, Mapping):
                lines.append(f"{index}. {theme.get('title', 'Theme')}")
        link = f"{self.delivery.web_base_url}/weekly/{week}"
        suggestions = report.get("suggestions", {})
        additions = suggestions.get("add", []) if isinstance(suggestions, Mapping) else []
        rows = [
            [
                InlineKeyboardButton(
                    f"Add @{str(handle).removeprefix('@')}",
                    callback_data=f"add:{str(handle).removeprefix('@')}",
                )
            ]
            for handle in additions[:5]
        ]
        await self._client().send_message(
            chat_id=self.chat_id,
            text="\n".join([*lines, "", link]),
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(rows) if rows else None,
        )


class TelegramPolling:
    """Callback-only Telegram runtime; it intentionally registers no browse commands."""

    def __init__(
        self,
        config: AppConfig,
        secrets: Secrets,
        database: Database,
        *,
        xclient: XClient | None = None,
    ):
        self.config = config
        self.secrets = secrets
        self.database = database
        self.xclient = xclient
        self.application: Any | None = None

    async def _callback(self, update: Any, context: Any) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        action, _, value = str(query.data).partition(":")
        try:
            if action == "keep":
                kept = self.database.toggle_keep(value)
                await query.answer("Kept locally" if kept else "Local keep removed")
                return
            if action == "add":
                client = self.xclient
                if client is None:
                    client = TwscrapeXClient(
                        self.config.x,
                        self.secrets,
                        pool_path=self.config.twscrape_db_path,
                    )
                    self.xclient = client
                await client.follow_account(value)
                candidate = self.database.get_candidate(value)
                if candidate and candidate["status"] == "pending":
                    self.database.review_candidate(value, "accepted")
                else:
                    self.database.add_account(value, note="Added through Telegram")
                self.database.record_event("telegram_account_added", {"handle": value})
                await query.answer(f"Added @{value}")
                return
            await query.answer("Unknown action", show_alert=True)
        except Exception as exc:
            self.database.record_event(
                "telegram_callback_failed",
                {"action": action, "message": str(exc)},
            )
            await query.answer("Action failed. Check Periscope health.", show_alert=True)

    async def start(self) -> None:
        if not self.secrets.telegram_configured or self.application is not None:
            return
        from telegram.ext import Application, CallbackQueryHandler

        assert self.secrets.telegram_bot_token is not None
        application = Application.builder().token(self.secrets.telegram_bot_token).build()
        application.add_handler(CallbackQueryHandler(self._callback))
        await application.initialize()
        await application.start()
        if application.updater is not None:
            await application.updater.start_polling(drop_pending_updates=True)
        self.application = application

    async def stop(self) -> None:
        application, self.application = self.application, None
        if application is None:
            return
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()

    async def reconfigure(self, secrets: Secrets) -> None:
        token_changed = secrets.telegram_bot_token != self.secrets.telegram_bot_token
        self.secrets = secrets
        self.xclient = None
        if token_changed and self.application is not None:
            await self.stop()
        if secrets.telegram_configured and self.application is None:
            await self.start()


def build_notifier(secrets: Secrets, delivery: DeliveryConfig) -> Notifier:
    if not secrets.telegram_configured:
        return NullNotifier()
    assert secrets.telegram_bot_token is not None and secrets.telegram_chat_id is not None
    return TelegramNotifier(secrets.telegram_bot_token, secrets.telegram_chat_id, delivery)
