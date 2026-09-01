"""Following-feed catch-up watermark helpers. No browser imports."""

from __future__ import annotations

import json
from pathlib import Path


def load_watermark(path: Path | None) -> tuple[set[str], str | None]:
    if path is None or not path.exists():
        return set(), None
    data = json.loads(path.read_text())
    ids = set(
        map(
            str,
            data.get("following_newest_ids")
            or data.get("status_ids")
            or data.get("all_following_ids")
            or [],
        )
    )
    ts = data.get("following_newest_created_at") or data.get("following_watermark_iso")
    return ids, ts
