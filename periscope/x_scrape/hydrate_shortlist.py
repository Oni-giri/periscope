#!/usr/bin/env python3
"""Fill truncated keeper text via FxTwitter (full tweet body). No posting."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

FX = "https://api.fxtwitter.com/status/{status_id}"


def hydrate_one(client: httpx.Client, status_id: str) -> dict | None:
    r = client.get(FX.format(status_id=status_id), timeout=30, follow_redirects=True)
    r.raise_for_status()
    tweet = (r.json() or {}).get("tweet") or {}
    text = tweet.get("text") or ""
    if not text:
        return None
    media = []
    photos = (tweet.get("media") or {}).get("photos") or []
    for photo in photos:
        url = photo.get("url") or photo.get("original_url")
        if url:
            media.append(url)
    author = tweet.get("author") or {}
    avatar = author.get("avatar_url") or author.get("avatar") or None
    return {"text": text, "image_urls": media, "author_avatar": avatar}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shortlist", type=Path, default=Path("data/x-dumps/shortlist.json"))
    args = parser.parse_args()
    data = json.loads(args.shortlist.read_text())
    keepers = data.get("keepers") or []
    todo = [k for k in keepers if k.get("is_truncated")]
    print(f"hydrate {len(todo)} / {len(keepers)} truncated keepers", flush=True)
    expanded = 0
    with httpx.Client(headers={"User-Agent": "Periscope/0.1"}) as client:
        for keeper in todo:
            sid = str(keeper["status_id"])
            print(f"  {sid} {keeper.get('author_handle')}", flush=True)
            try:
                got = hydrate_one(client, sid)
            except Exception as exc:
                print(f"    fail {exc}", flush=True)
                time.sleep(0.4)
                continue
            if not got:
                print("    empty", flush=True)
                continue
            old = keeper.get("text") or ""
            keeper["text"] = got["text"]
            if got["image_urls"]:
                keeper["image_urls"] = list(
                    dict.fromkeys([*(keeper.get("image_urls") or []), *got["image_urls"]])
                )
            if got.get("author_avatar") and not keeper.get("author_avatar"):
                keeper["author_avatar"] = got["author_avatar"]
            keeper["is_truncated"] = False
            expanded += 1
            print(f"    {len(old)} -> {len(keeper['text'])} chars", flush=True)
            time.sleep(0.25)
    args.shortlist.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    print(f"wrote {args.shortlist} expanded {expanded}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
