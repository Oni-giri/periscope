from __future__ import annotations

import json
from pathlib import Path

from periscope.x_scrape.digest_pipeline import main


def test_pipeline_skip_to_digest_and_ingest(tmp_path: Path, monkeypatch, app_config) -> None:
    dumps = tmp_path / "x-dumps"
    dumps.mkdir()
    shortlist = {
        "keepers": [
            {
                "status_id": "77",
                "author_handle": "@alice",
                "created_at": "2026-08-28T10:00:00Z",
                "text": "tools note",
                "tweet_url": "https://x.com/alice/status/77",
                "image_urls": [],
                "is_truncated": False,
                "curation": {"topic": "tools", "why": "handy CLI", "score": 8},
            }
        ]
    }
    (dumps / "shortlist.json").write_text(json.dumps(shortlist))
    config = Path("config.example.toml")
    monkeypatch.setattr(
        "sys.argv",
        [
            "periscope-digest",
            "--skip-scrape",
            "--skip-curate",
            "--skip-hydrate",
            "--out-dir",
            str(dumps),
            "--date",
            "2026-08-28",
            "--config",
            str(config),
            "--data-dir",
            str(app_config.data_dir),
            "--media-dir",
            str(tmp_path / "media"),
            "--no-cache-media",
        ],
    )
    assert main() == 0
    digest_path = dumps / "digest-2026-08-28.json"
    assert digest_path.is_file()
    doc = json.loads(digest_path.read_text())
    assert doc["tweets"][0]["id"] == "77"
    assert (app_config.data_dir / "periscope.db").is_file()
