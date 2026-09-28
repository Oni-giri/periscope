"""Shared helpers for authenticated TestClient usage."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from periscope.web import auth as web_auth

TEST_WEB_PASSWORD = "test-web-password"


def ensure_web_auth(data_dir: Path, password: str = TEST_WEB_PASSWORD):
    existing = web_auth.load_web_auth(data_dir)
    if existing is not None:
        return existing
    return web_auth.create_password(data_dir, password)


def authed_client(app, password: str = TEST_WEB_PASSWORD) -> TestClient:
    """TestClient carrying a valid periscope_session cookie."""
    record = ensure_web_auth(app.state.config.data_dir, password)
    client = TestClient(app)
    client.cookies.set(
        web_auth.COOKIE_NAME,
        web_auth.issue_session_token(record.session_secret),
    )
    return client
