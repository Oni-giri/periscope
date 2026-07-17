from __future__ import annotations

import json
from datetime import UTC, datetime

from periscope.config import Secrets
from periscope.db import MIGRATIONS, Database


def test_schema_seed_raw_storage_and_fts(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    database.initialize()
    secrets = Secrets(x_auth_token="private-token", x_ct0="private-ct0")
    database.seed(app_config, secrets)

    fetch_id = database.start_fetch("test", now=datetime(2026, 7, 16, tzinfo=UTC))
    payload = {
        "id": "42",
        "author": "@Example",
        "created_at": "2026-07-16T08:00:00Z",
        "text": "A searchable SQLite artifact",
        "links": [{"url": "https://example.test/item"}],
    }
    assert database.store_tweet(payload, fetch_id=fetch_id)
    assert not database.store_tweet(payload, fetch_id=fetch_id)
    database.finish_fetch(fetch_id, new_items=1)

    stored = database.get_tweet("42")
    assert stored is not None
    assert stored["raw"] == payload
    assert stored["author"] == "example"
    assert stored["urls"] == ["https://example.test/item"]
    assert [item["id"] for item in database.search_archive("SQLite")] == ["42"]

    settings = {
        row["key"]: row["value"]
        for row in database.rows("SELECT key, value FROM settings ORDER BY key")
    }
    assert settings["x_is_configured"] == "true"
    assert "private-token" not in json.dumps(settings)
    assert len(database.rows("SELECT version FROM schema_migrations")) == len(MIGRATIONS)


def test_fts_tracks_tweet_updates(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    fetch_id = database.start_fetch("test")
    original = {"id": "7", "author": "a", "text": "first phrase"}
    updated = {"id": "7", "author": "a", "text": "second phrase"}

    database.store_tweet(original, fetch_id=fetch_id)
    database.store_tweet(updated, fetch_id=fetch_id)

    assert database.search_archive("first") == []
    assert database.search_archive("second")[0]["id"] == "7"


def test_cookie_incident_is_collapsed(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()

    assert database.open_cookie_incident()
    assert not database.open_cookie_incident()
    assert database.recover_cookie_incident()
    assert not database.recover_cookie_incident()
    assert database.open_cookie_incident()
