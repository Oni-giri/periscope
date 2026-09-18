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
        assert "Credentials saved locally" in credentials.text
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

        health = client.get("/health").json()
        assert len(health["scheduled_jobs"]) == 4
