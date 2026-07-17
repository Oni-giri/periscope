from __future__ import annotations

import asyncio

from periscope.config import DeliveryConfig
from periscope.telegram.bot import TelegramNotifier


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


def test_telegram_delivery_has_local_action_buttons() -> None:
    notifier = TelegramNotifier(
        "token",
        "chat",
        DeliveryConfig(web_base_url="http://periscope.test"),
    )
    bot = FakeBot()
    notifier._bot = bot
    digest = {
        "date": "2026-07-16",
        "clusters": [{"id": "cluster_1", "headline": "Measured release"}],
        "picks": [
            {
                "id": "pick_1",
                "tweet_id": "42",
                "tweet": {"author": "builder"},
            }
        ],
        "items": [
            {"type": "cluster", "id": "cluster_1"},
            {"type": "pick", "id": "pick_1"},
        ],
    }

    asyncio.run(notifier.send_digest(digest, "Periscope digest"))
    keyboard = bot.messages[0]["reply_markup"].inline_keyboard
    assert keyboard[0][0].url == "http://periscope.test/cluster/cluster_1"
    assert keyboard[1][0].callback_data == "keep:42"

    asyncio.run(
        notifier.send_weekly(
            {
                "week": "2026-W29",
                "themes": [{"title": "Databases"}],
                "suggestions": {"add": ["candidate_db"]},
            }
        )
    )
    weekly_keyboard = bot.messages[1]["reply_markup"].inline_keyboard
    assert weekly_keyboard[0][0].callback_data == "add:candidate_db"
