from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

from periscope.x_scrape.media_cache import cache_avatar_url, cache_digest_media, cache_media_urls
from periscope.x_scrape.shortlist_to_digest import convert


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/ok.png", "/avatar.png"}:
            body = b"\x89PNG\r\n\x1a\n" + b"0" * 32
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _serve() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def test_cache_media_rewrites_success_keeps_failure(tmp_path: Path) -> None:
    server, base = _serve()
    try:
        ok = f"{base}/ok.png"
        bad = f"{base}/missing.png"
        out = cache_media_urls("42", [ok, bad], media_dir=tmp_path / "media")
        assert out[0] == "/media/42_0.png"
        assert (tmp_path / "media" / "42_0.png").is_file()
        assert out[1] == bad
    finally:
        server.shutdown()


def test_convert_then_cache_digest_media(tmp_path: Path) -> None:
    server, base = _serve()
    try:
        keepers = [
            {
                "status_id": "11",
                "author_handle": "@alice",
                "text": "pic",
                "tweet_url": "https://x.com/alice/status/11",
                "image_urls": [f"{base}/ok.png"],
                "author_avatar": f"{base}/avatar.png",
                "curation": {"topic": "ai", "why": "image post", "score": 8},
            }
        ]
        doc = convert(keepers, "2026-08-28")
        assert doc["tweets"][0]["avatar"] == f"{base}/avatar.png"
        cache_digest_media(doc, media_dir=tmp_path / "media")
        assert doc["tweets"][0]["media"] == ["/media/11_0.png"]
        assert doc["tweets"][0]["avatar"] == "/media/avatar_alice.png"
        assert (tmp_path / "media" / "avatar_alice.png").is_file()
    finally:
        server.shutdown()


def test_cache_avatar_soft_fails(tmp_path: Path) -> None:
    server, base = _serve()
    try:
        remote = f"{base}/missing.png"
        out = cache_avatar_url(
            {"id": "9", "author": "bob"},
            remote,
            media_dir=tmp_path / "media",
        )
        assert out == remote
    finally:
        server.shutdown()
