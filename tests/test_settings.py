from __future__ import annotations

import stat

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.db import Database
from periscope.runtime import effective_config
from periscope.web.app import create_app


class StubRunner:
    def __init__(self) -> None:
        self.launched: list[str] = []

    def launch(self, name: str) -> bool:
        self.launched.append(name)
        return True


def test_settings_separate_secrets_and_apply_runtime_updates(app_config) -> None:
    database = Database(app_config.db_path)
    app = create_app(app_config, Secrets(), database=database)

    with TestClient(app) as client:
        credentials = client.post(
            "/settings/credentials",
            data={
                "x_auth_token": "secret-auth",
                "x_ct0": "secret-ct0",
                "anthropic_api_key": "secret-anthropic",
            },
            headers={"HX-Request": "true"},
        )
        assert credentials.status_code == 200
        assert "Connections saved locally" in credentials.text
        assert "secret-auth" not in credentials.text

        secret_path = app_config.data_dir / "secrets.env"
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
        assert "secret-auth" in secret_path.read_text(encoding="utf-8")
        stored_settings = database.get_settings()
        assert stored_settings["x_is_configured"] == "true"
        assert "secret-auth" not in repr(stored_settings)

        schedule = client.post(
            "/settings/schedule",
            data={
                "timezone": "Europe/Paris",
                "daily_time": ["07:15", "18:45"],
                "weekly_day": "sat",
                "weekly_time": "10:30",
                "feed_interval_minutes": "45",
                "picks_minimum": "1",
                "picks_maximum": "6",
                "clustering_aggressiveness": "60",
                "topic_decay": "on",
            },
            headers={"HX-Request": "true"},
        )
        assert "Schedule updated immediately" in schedule.text
        active = effective_config(app_config, database)
        assert active.schedule.daily_times == ("07:15", "18:45")
        assert active.schedule.timezone == "Europe/Paris"
        assert active.picks.maximum == 6
        assert {job.id for job in app.state.scheduler.get_jobs()} == {
            "fetch",
            "daily-0",
            "daily-1",
            "weekly",
        }

        invalid = client.post(
            "/settings/schedule",
            data={
                "timezone": "UTC",
                "daily_time": ["25:99"],
                "weekly_day": "sun",
                "weekly_time": "09:00",
                "feed_interval_minutes": "30",
                "picks_minimum": "0",
                "picks_maximum": "5",
                "clustering_aggressiveness": "45",
            },
            headers={"HX-Request": "true"},
        )
        assert "24-hour HH:MM" in invalid.text

        added = client.post(
            "/settings/accounts/add",
            data={"handle": "@new_reader"},
            headers={"HX-Request": "true"},
        )
        assert "@new_reader added" in added.text
        client.post(
            "/settings/accounts/new_reader/mute",
            headers={"HX-Request": "true"},
        )
        account = next(
            item
            for item in database.list_accounts(include_muted=True)
            if item["handle"] == "new_reader"
        )
        assert account["muted"] == 1

        topics = client.post(
            "/settings/topics",
            data={
                "topic_name": ["Databases", "Homelab"],
                "topic_min_faves": ["30", "10"],
            },
            headers={"HX-Request": "true"},
        )
        assert "Topics saved" in topics.text
        assert [item["name"] for item in database.list_topics()] == ["Databases", "Homelab"]

        prompt = client.post(
            "/settings/prompt",
            data={"prompt": "Select durable artifacts and return structured JSON."},
            headers={"HX-Request": "true"},
        )
        assert "new version" in prompt.text
        assert (app_config.data_dir / "prompts" / "picks_prompt.md").exists()

        runner = StubRunner()
        app.state.job_runner = runner
        run = client.post(
            "/settings/run/weekly",
            headers={"HX-Request": "true"},
        )
        assert "Weekly queued" in run.text
        assert runner.launched == ["weekly"]


        reading = client.post(
            "/settings/reading",
            data={
                "feed_max_posts": "40",
                "archive_default_filter": "kept",
                "interests": "ai\nlatvia\ncrypto",
            },
            headers={"HX-Request": "true"},
        )
        assert "Reading settings saved" in reading.text
        from periscope.runtime import ui_settings

        assert ui_settings(database)["feed_max_posts"] == 40
        assert ui_settings(database)["archive_default_filter"] == "kept"
        assert [item["name"] for item in database.list_topics()] == ["ai", "crypto", "latvia"]

        archive = client.get("/archive")
        assert archive.status_code == 200
        # kept default -> keep select shows Kept
        assert 'name="kept"' in archive.text
        assert 'value="true" selected' in archive.text or "selected" in archive.text

        feed = client.get("/feed")
        assert feed.status_code == 200
        assert 'type="search"' in feed.text
        assert "<select name=\"account\">" not in feed.text

        inbox = client.get("/inbox")
        assert inbox.status_code == 200
        assert "No follow propositions" in inbox.text
        home = client.get("/")
        assert 'href="/inbox"' in home.text or "Inbox" in home.text
        assert ">Discovery<" not in home.text
        assert ">Ideas<" not in home.text
        assert ">Weekly<" not in home.text

        page = client.get("/settings?tab=system")
        assert page.status_code == 200
        assert "Event log" in page.text
        assert "Follow queue" in page.text

        connections = client.get("/settings?tab=connections")
        assert connections.status_code == 200
        assert "OpenRouter" in connections.text
        assert "Chrome profile" in connections.text
        assert "Legacy" not in connections.text
        assert "Anthropic" not in connections.text

        health = client.get("/health").json()
        assert len(health["scheduled_jobs"]) == 4


def test_reading_interests_validate_and_prompt_reset(app_config) -> None:
    from periscope.runtime import read_curator_prompt, read_enrich_prompt
    from periscope.x_scrape.curate_feeds import DEFAULT_SYSTEM_TEMPLATE, load_system_prompt_template

    database = Database(app_config.db_path)
    app = create_app(app_config, Secrets(), database=database)

    with TestClient(app) as client:
        page = client.get("/settings?tab=reading")
        assert page.status_code == 200
        assert "Validate interests" in page.text
        assert "Curator system prompt" in page.text
        assert 'name="curator_prompt"' in page.text
        assert "{interests}" in page.text
        assert "fallback: ai, latvia" not in page.text

        empty = client.post(
            "/settings/reading",
            data={
                "feed_max_posts": "50",
                "archive_default_filter": "has_action",
                "interests": "  \n  ",
                "curator_prompt": DEFAULT_SYSTEM_TEMPLATE,
                "enrich_prompt": "keep-me",
            },
            headers={"HX-Request": "true"},
        )
        assert empty.status_code == 200
        assert "NO_INTERESTS" in empty.text

        saved = client.post(
            "/settings/reading",
            data={
                "feed_max_posts": "50",
                "archive_default_filter": "has_action",
                "interests": "ai\nlatvia",
                "curator_prompt": DEFAULT_SYSTEM_TEMPLATE,
                "enrich_prompt": "Extract actions for {not-a-placeholder}.",
            },
            headers={"HX-Request": "true"},
        )
        assert "Reading settings saved" in saved.text
        assert [item["name"] for item in database.list_topics()] == ["ai", "latvia"]

        ok = client.post(
            "/settings/reading/validate",
            data={
                "feed_max_posts": "50",
                "archive_default_filter": "has_action",
                "interests": "crypto\ntools for AI",
                "curator_prompt": DEFAULT_SYSTEM_TEMPLATE,
                "enrich_prompt": "x",
            },
            headers={"HX-Request": "true"},
        )
        assert ok.status_code == 200
        assert "crypto, tools-for-ai" in ok.text
        assert "KEEP if it is notable for these interests: crypto, tools-for-ai" in ok.text
        assert "crypto|tools-for-ai|other" in ok.text
        assert "Saved DB differs" in ok.text
        assert 'name="interests"' in ok.text
        assert "crypto" in ok.text

        fail = client.post(
            "/settings/reading/validate",
            data={
                "feed_max_posts": "50",
                "archive_default_filter": "has_action",
                "interests": "",
                "curator_prompt": DEFAULT_SYSTEM_TEMPLATE,
            },
            headers={"HX-Request": "true"},
        )
        assert "NO_INTERESTS" in fail.text

        custom_body = "Only keep {interests}. Topics={topic_enum}."
        wrote = client.post(
            "/settings/reading",
            data={
                "feed_max_posts": "50",
                "archive_default_filter": "has_action",
                "interests": "ai\nlatvia",
                "curator_prompt": custom_body,
                "enrich_prompt": "Extract actions for builders.",
            },
            headers={"HX-Request": "true"},
        )
        assert "Reading settings saved" in wrote.text
        text, is_custom = read_curator_prompt(app_config)
        assert is_custom
        assert text == custom_body
        assert custom_body.strip() in load_system_prompt_template(app_config.data_dir)
        enrich_text, enrich_custom = read_enrich_prompt(app_config)
        assert enrich_custom
        assert enrich_text == "Extract actions for builders."

        reset = client.post(
            "/settings/reading/prompts/curator/reset",
            headers={"HX-Request": "true"},
        )
        assert "reset to the built-in default" in reset.text
        text, is_custom = read_curator_prompt(app_config)
        assert is_custom is False
        assert "{interests}" in text
        assert not (app_config.data_dir / "prompts" / "curator_system.md").exists()

        reset_enrich = client.post(
            "/settings/reading/prompts/enrich/reset",
            headers={"HX-Request": "true"},
        )
        assert "Enrich actions prompt reset" in reset_enrich.text
        _, enrich_custom = read_enrich_prompt(app_config)
        assert enrich_custom is False


def test_connections_saves_openrouter(app_config) -> None:
    database = Database(app_config.db_path)
    app = create_app(app_config, Secrets(), database=database)
    with TestClient(app) as client:
        page = client.get("/settings?tab=connections")
        assert "https://openrouter.ai/api/v1" in page.text
        assert "Chrome profile" in page.text
        saved = client.post(
            "/settings/credentials",
            data={
                "openrouter_base_url": "https://openrouter.ai/api/v1",
                "openrouter_api_key": "or-secret-key",
            },
            headers={"HX-Request": "true"},
        )
        assert "Connections saved locally" in saved.text
        body = (app_config.data_dir / "secrets.env").read_text(encoding="utf-8")
        assert "OPENROUTER_API_KEY=" in body
        assert "or-secret-key" in body
        assert "OPENROUTER_BASE_URL=" in body
        assert "or-secret-key" not in saved.text
