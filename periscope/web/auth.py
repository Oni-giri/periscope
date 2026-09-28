"""Web UI password hashing and signed session cookies (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

COOKIE_NAME = "periscope_session"
AUTH_FILENAME = "web_auth.json"
SESSION_TTL_SECONDS = 180 * 24 * 60 * 60  # 180 days

# scrypt parameters (N, r, p) — modest for interactive login on a single-user box
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16


@dataclass(frozen=True, slots=True)
class WebAuth:
    password_hash: str
    session_secret: str
    created_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "password_hash": self.password_hash,
            "session_secret": self.session_secret,
            "created_at": self.created_at,
        }


def auth_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / AUTH_FILENAME


def load_web_auth(data_dir: Path | str) -> WebAuth | None:
    path = auth_path(data_dir)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    password_hash = str(raw.get("password_hash", "")).strip()
    session_secret = str(raw.get("session_secret", "")).strip()
    created_at = str(raw.get("created_at", "")).strip()
    if not password_hash or not session_secret:
        return None
    return WebAuth(
        password_hash=password_hash,
        session_secret=session_secret,
        created_at=created_at or "",
    )


def password_is_set(data_dir: Path | str) -> bool:
    return load_web_auth(data_dir) is not None


def hash_password(password: str) -> str:
    """Return scrypt$N$r$p$salt_b64$hash_b64."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii").rstrip("=")
    hash_b64 = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${hash_b64}"


def _b64decode_padded(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = encoded.split("$", 5)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64decode_padded(salt_b64)
        expected = _b64decode_padded(hash_b64)
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError, OSError):
        return False
    return hmac.compare_digest(actual, expected)


def save_web_auth(data_dir: Path | str, auth: WebAuth) -> Path:
    path = auth_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(auth.to_dict(), indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def create_password(data_dir: Path | str, password: str) -> WebAuth:
    """Create web_auth.json. Raises FileExistsError if already configured."""
    if load_web_auth(data_dir) is not None:
        raise FileExistsError("Web password already configured")
    from datetime import UTC, datetime

    auth = WebAuth(
        password_hash=hash_password(password),
        session_secret=secrets.token_hex(32),
        created_at=datetime.now(UTC).isoformat(),
    )
    save_web_auth(data_dir, auth)
    return auth


def change_password(
    data_dir: Path | str,
    *,
    old_password: str,
    new_password: str,
) -> WebAuth:
    """Verify old password and rewrite hash; keep session_secret so cookies stay valid."""
    auth = load_web_auth(data_dir)
    if auth is None:
        raise FileNotFoundError("Web password is not configured")
    if not verify_password(old_password, auth.password_hash):
        raise PermissionError("Current password is incorrect")
    from datetime import UTC, datetime

    updated = WebAuth(
        password_hash=hash_password(new_password),
        session_secret=auth.session_secret,
        created_at=auth.created_at or datetime.now(UTC).isoformat(),
    )
    save_web_auth(data_dir, updated)
    return updated


def issue_session_token(session_secret: str, *, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """HMAC-signed token: sid.expiry.signature (all hex/decimal ascii)."""
    sid = secrets.token_hex(16)
    expiry = int(time.time()) + int(ttl_seconds)
    body = f"{sid}.{expiry}"
    sig = hmac.new(
        session_secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{body}.{sig}"


def verify_session_token(token: str, session_secret: str) -> bool:
    if not token or not session_secret:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    sid, expiry_s, sig = parts
    if not sid or not expiry_s or not sig:
        return False
    try:
        expiry = int(expiry_s)
    except ValueError:
        return False
    if expiry < int(time.time()):
        return False
    body = f"{sid}.{expiry_s}"
    expected = hmac.new(
        session_secret.encode("utf-8"),
        body.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


def cookie_header_value(
    token: str,
    *,
    secure: bool,
    max_age: int = SESSION_TTL_SECONDS,
    clear: bool = False,
) -> str:
    """Build a Set-Cookie header value for periscope_session."""
    if clear:
        parts = [
            f"{COOKIE_NAME}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
    else:
        parts = [
            f"{COOKIE_NAME}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={int(max_age)}",
        ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def safe_next_path(raw: str | None, *, default: str = "/") -> str:
    """Allow only same-origin relative paths (no scheme, no //)."""
    if not raw:
        return default
    value = raw.strip()
    if not value.startswith("/") or value.startswith("//"):
        return default
    if "://" in value or "\\" in value:
        return default
    return value


def login_redirect_location(path: str, query: bytes | str = b"") -> str:
    """Build /auth/login?next=... preserving path+query safely."""
    if isinstance(query, bytes):
        query_s = query.decode("latin-1")
    else:
        query_s = query
    target = path if not query_s else f"{path}?{query_s}"
    next_path = safe_next_path(target, default="/")
    return "/auth/login?" + urlencode({"next": next_path})


def request_is_https(scope: dict[str, Any]) -> bool:
    if scope.get("scheme") == "https":
        return True
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }
    forwarded = headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


def parse_cookie_header(header_value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for part in header_value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        result[name.strip()] = value.strip()
    return result


def get_session_cookie_from_scope(scope: dict[str, Any]) -> str | None:
    headers = dict(scope.get("headers") or [])
    raw = headers.get(b"cookie")
    if not raw:
        return None
    cookies = parse_cookie_header(raw.decode("latin-1"))
    return cookies.get(COOKIE_NAME)


def path_is_public(path: str) -> bool:
    """Paths that bypass auth middleware.

    /mcp is intentionally unprotected for local tooling (MCP clients on the
    same Tailscale host). /media and HTML routes remain gated.
    """
    if path == "/health":
        return True
    if path.startswith("/auth"):
        return True
    if path.startswith("/static"):
        return True
    if path == "/mcp" or path.startswith("/mcp/"):
        return True
    return False


def wants_html_redirect(scope: dict[str, Any]) -> bool:
    method = scope.get("method", "GET").upper()
    if method in {"GET", "HEAD"}:
        return True
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in scope.get("headers", [])
    }
    accept = headers.get("accept", "")
    return "text/html" in accept and "application/json" not in accept.split(",")[0]




class AuthMiddleware:
    """Pure ASGI gate: require web password + valid session cookie.

    Public: /auth/*, /static/*, /health, and /mcp (local tooling — see comment
    in path_is_public). Everything else, including /media and HTML pages,
    requires a configured password and a valid periscope_session cookie.
    """

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or "/"
        if path_is_public(path):
            await self.app(scope, receive, send)
            return

        app = scope.get("app")
        data_dir = None
        if app is not None:
            config = getattr(getattr(app, "state", None), "config", None)
            if config is not None:
                data_dir = getattr(config, "data_dir", None)

        if data_dir is None:
            await self.app(scope, receive, send)
            return

        record = load_web_auth(data_dir)
        method = (scope.get("method") or "GET").upper()
        query = scope.get("query_string") or b""

        if record is None:
            if wants_html_redirect(scope):
                await _send_redirect(send, "/auth/setup")
            else:
                await _send_json(send, 401, {"detail": "Web password not configured"})
            return

        token = get_session_cookie_from_scope(scope)
        if token and verify_session_token(token, record.session_secret):
            await self.app(scope, receive, send)
            return

        if wants_html_redirect(scope) or method in {"GET", "HEAD"}:
            await _send_redirect(send, login_redirect_location(path, query))
        else:
            await _send_json(send, 401, {"detail": "Authentication required"})


async def _send_redirect(send: Any, location: str, status: int = 302) -> None:
    body = b""
    headers = [
        (b"location", location.encode("latin-1")),
        (b"content-length", b"0"),
        (b"cache-control", b"no-store"),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_json(send: Any, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})
