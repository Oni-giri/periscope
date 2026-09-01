"""Download remote tweet image URLs into the local data/media cache."""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = "Periscope/0.1 (+local media cache)"
_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_HANDLE_SAFE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _extension_for(url: str, content_type: str | None) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in _ALLOWED_EXT:
        return ".jpg" if suffix == ".jpeg" else suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed == ".jpe":
            guessed = ".jpg"
        if guessed and guessed.lower() in _ALLOWED_EXT:
            return guessed.lower()
    return ".jpg"


def local_media_url(relative_name: str) -> str:
    """Return the FastAPI-mounted path for a cached media file."""

    return f"/media/{relative_name.lstrip('/')}"


def cache_media_urls(
    tweet_id: str,
    urls: list[str],
    *,
    media_dir: Path,
    timeout: float = 20.0,
) -> list[str]:
    """Download remote image URLs; keep originals on soft failure.

    Files land at ``{media_dir}/{tweet_id}_{n}.ext`` and successful downloads
    are rewritten to ``/media/{tweet_id}_{n}.ext``.
    """

    if not urls:
        return []
    media_dir.mkdir(parents=True, exist_ok=True)
    rewritten: list[str] = []
    for index, url in enumerate(urls):
        text = str(url or "").strip()
        if not text:
            continue
        if text.startswith("/media/"):
            rewritten.append(text)
            continue
        if not re.match(r"^https?://", text, re.IGNORECASE):
            rewritten.append(text)
            continue
        try:
            request = Request(text, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read()
                content_type = response.headers.get("Content-Type")
            if not body:
                rewritten.append(text)
                continue
            ext = _extension_for(text, content_type)
            name = f"{tweet_id}_{index}{ext}"
            target = media_dir / name
            target.write_bytes(body)
            rewritten.append(local_media_url(name))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            rewritten.append(text)
    return rewritten


def _avatar_basename(tweet: dict) -> str:
    handle = str(tweet.get("author") or "").removeprefix("@").strip()
    handle = _HANDLE_SAFE.sub("_", handle).strip("._") or ""
    if handle:
        return f"avatar_{handle}"
    tweet_id = str(tweet.get("id") or tweet.get("tweet_id") or "unknown")
    return f"{tweet_id}_avatar"


def cache_avatar_url(
    tweet: dict,
    url: str,
    *,
    media_dir: Path,
    timeout: float = 20.0,
) -> str:
    """Cache a single author avatar; soft-fail keeps the remote URL."""

    text = str(url or "").strip()
    if not text:
        return text
    if text.startswith("/media/"):
        return text
    if not re.match(r"^https?://", text, re.IGNORECASE):
        return text
    media_dir.mkdir(parents=True, exist_ok=True)
    base = _avatar_basename(tweet)
    # Probe extension after download; start with .jpg placeholder path.
    try:
        request = Request(text, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read()
            content_type = response.headers.get("Content-Type")
        if not body:
            return text
        ext = _extension_for(text, content_type)
        name = f"{base}{ext}"
        (media_dir / name).write_bytes(body)
        return local_media_url(name)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return text


def cache_digest_media(document: dict, *, media_dir: Path) -> dict:
    """Rewrite ``tweets[].media`` and ``tweets[].avatar`` in place."""

    for tweet in document.get("tweets") or []:
        tweet_id = str(tweet.get("id") or tweet.get("tweet_id") or "")
        media = list(tweet.get("media") or [])
        if tweet_id and media:
            tweet["media"] = cache_media_urls(tweet_id, media, media_dir=media_dir)
        avatar = tweet.get("avatar")
        if avatar:
            tweet["avatar"] = cache_avatar_url(tweet, str(avatar), media_dir=media_dir)
    return document
