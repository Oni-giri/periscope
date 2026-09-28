"""Password gate for the Periscope web UI."""

from __future__ import annotations

from fastapi.testclient import TestClient

from periscope.config import Secrets
from periscope.web import auth as web_auth
from periscope.web.app import create_app


def test_setup_creates_file_and_cookie(app_config) -> None:
    app = create_app(app_config, Secrets())
    auth_file = web_auth.auth_path(app_config.data_dir)
    assert not auth_file.exists()

    with TestClient(app, follow_redirects=False) as client:
        unauth = client.get("/")
        assert unauth.status_code == 302
        assert unauth.headers["location"] == "/auth/setup"

        setup_page = client.get("/auth/setup")
        assert setup_page.status_code == 200
        assert "Create a web password" in setup_page.text

        created = client.post(
            "/auth/setup",
            data={"password": "correct-horse", "confirm": "correct-horse"},
        )
        assert created.status_code == 303
        assert created.headers["location"] == "/"
        set_cookie = created.headers.get("set-cookie", "")
        assert web_auth.COOKIE_NAME in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Lax" in set_cookie
        # Tailscale often serves http — Secure must not be forced
        assert "Secure" not in set_cookie

    assert auth_file.is_file()
    record = web_auth.load_web_auth(app_config.data_dir)
    assert record is not None
    assert record.password_hash.startswith("scrypt$")
    assert len(record.session_secret) >= 64
    # Never store plaintext
    raw = auth_file.read_text(encoding="utf-8")
    assert "correct-horse" not in raw


def test_second_setup_blocked(app_config) -> None:
    web_auth.create_password(app_config.data_dir, "already-set-pw")
    app = create_app(app_config, Secrets())

    with TestClient(app, follow_redirects=False) as client:
        page = client.get("/auth/setup")
        assert page.status_code == 303
        assert page.headers["location"] == "/auth/login"

        again = client.post(
            "/auth/setup",
            data={"password": "new-password", "confirm": "new-password"},
        )
        assert again.status_code == 303
        assert again.headers["location"] == "/auth/login"


def test_login_required_without_cookie(app_config) -> None:
    web_auth.create_password(app_config.data_dir, "gate-password")
    app = create_app(app_config, Secrets())

    with TestClient(app, follow_redirects=False) as client:
        home = client.get("/")
        assert home.status_code == 302
        assert home.headers["location"].startswith("/auth/login")
        assert "next=%2F" in home.headers["location"]

        feed = client.get("/feed?kept=true")
        assert feed.status_code == 302
        assert "/auth/login" in feed.headers["location"]
        assert "next=" in feed.headers["location"]

        # POST APIs without cookie → 401 JSON
        keep = client.post("/keep/1001", headers={"HX-Request": "true"})
        assert keep.status_code == 401
        assert keep.json()["detail"] == "Authentication required"


def test_wrong_password_fails(app_config) -> None:
    web_auth.create_password(app_config.data_dir, "right-password")
    app = create_app(app_config, Secrets())

    with TestClient(app, follow_redirects=False) as client:
        bad = client.post(
            "/auth/login",
            data={"password": "wrong-password", "next": "/"},
        )
        assert bad.status_code == 200
        assert "Incorrect password" in bad.text
        assert web_auth.COOKIE_NAME not in bad.headers.get("set-cookie", "")


def test_valid_cookie_passes(app_config) -> None:
    record = web_auth.create_password(app_config.data_dir, "session-password")
    app = create_app(app_config, Secrets())
    token = web_auth.issue_session_token(record.session_secret)

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(web_auth.COOKIE_NAME, token)
        home = client.get("/")
        assert home.status_code == 200
        assert "No digest yet" in home.text or "Periscope" in home.text


def test_other_browser_blocked_without_cookie(app_config) -> None:
    """Simulate another browser: password exists, no session cookie."""
    web_auth.create_password(app_config.data_dir, "shared-password")
    app = create_app(app_config, Secrets())

    with TestClient(app, follow_redirects=False) as client:
        login = client.post(
            "/auth/login",
            data={"password": "shared-password", "next": "/"},
        )
        assert login.status_code == 303
        assert web_auth.COOKIE_NAME in login.headers.get("set-cookie", "")
        # Cookie from login sticks on this client (browser A)
        assert client.get("/").status_code == 200

        # Browser B: clear cookies on the same ASGI app (no second lifespan)
        client.cookies.clear()
        blocked = client.get("/")
        assert blocked.status_code == 302
        assert blocked.headers["location"].startswith("/auth/login")


def test_health_and_static_public(app_config) -> None:
    web_auth.create_password(app_config.data_dir, "public-paths")
    app = create_app(app_config, Secrets())

    with TestClient(app, follow_redirects=False) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert "database" in health.json()

        favicon = client.get("/static/favicon.svg")
        assert favicon.status_code == 200


def test_logout_clears_cookie(app_config) -> None:
    record = web_auth.create_password(app_config.data_dir, "logout-password")
    app = create_app(app_config, Secrets())
    token = web_auth.issue_session_token(record.session_secret)

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(web_auth.COOKIE_NAME, token)
        assert client.get("/").status_code == 200
        out = client.post("/auth/logout")
        assert out.status_code == 303
        assert out.headers["location"] == "/auth/login"
        set_cookie = out.headers.get("set-cookie", "")
        assert "Max-Age=0" in set_cookie


def test_change_password(app_config) -> None:
    record = web_auth.create_password(app_config.data_dir, "old-secret-pw")
    app = create_app(app_config, Secrets())
    token = web_auth.issue_session_token(record.session_secret)

    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(web_auth.COOKIE_NAME, token)
        bad = client.post(
            "/settings/web-password",
            data={
                "old_password": "wrong",
                "new_password": "new-secret-pw",
                "confirm_password": "new-secret-pw",
            },
        )
        assert bad.status_code == 200
        assert "Current password is incorrect" in bad.text

        ok = client.post(
            "/settings/web-password",
            data={
                "old_password": "old-secret-pw",
                "new_password": "new-secret-pw",
                "confirm_password": "new-secret-pw",
            },
        )
        assert ok.status_code == 200
        assert "Web password updated" in ok.text

        # Old password no longer works; new one does
        client.cookies.clear()
        fail = client.post(
            "/auth/login",
            data={"password": "old-secret-pw", "next": "/"},
        )
        assert "Incorrect password" in fail.text
        win = client.post(
            "/auth/login",
            data={"password": "new-secret-pw", "next": "/"},
        )
        assert win.status_code == 303


def test_hash_roundtrip() -> None:
    encoded = web_auth.hash_password("round-trip-secret")
    assert web_auth.verify_password("round-trip-secret", encoded)
    assert not web_auth.verify_password("other", encoded)


def test_expired_token_rejected(app_config) -> None:
    record = web_auth.create_password(app_config.data_dir, "expiry-test")
    # ttl already in the past
    token = web_auth.issue_session_token(record.session_secret, ttl_seconds=-10)
    assert not web_auth.verify_session_token(token, record.session_secret)

    app = create_app(app_config, Secrets())
    with TestClient(app, follow_redirects=False) as client:
        client.cookies.set(web_auth.COOKIE_NAME, token)
        home = client.get("/")
        assert home.status_code == 302
        assert "/auth/login" in home.headers["location"]
