#!/usr/bin/env python3
"""Run scrape → curate → hydrate → shortlist_to_digest → ingest as one CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path


def _run(label: str, argv: list[str]) -> None:
    print(f"==> {label}", flush=True)
    print(" ".join(argv), flush=True)
    result = subprocess.run(argv, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> int:
    today = datetime.now(UTC).date().isoformat()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("data/x-dumps"))
    parser.add_argument(
        "--profile",
        type=Path,
        default=Path(
            os.environ.get("PERISCOPE_X_CHROME_PROFILE") or "data/chrome-profile"
        ),
    )
    parser.add_argument(
        "--watermark",
        type=Path,
        default=Path("data/following_watermark.json"),
    )
    parser.add_argument("--date", default=today)
    parser.add_argument("--config", type=Path, default=Path("config.example.toml"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--media-dir", type=Path, default=None)
    parser.add_argument("--skip-scrape", action="store_true")
    parser.add_argument("--skip-curate", action="store_true")
    parser.add_argument("--skip-hydrate", action="store_true")
    parser.add_argument("--skip-digest", action="store_true")
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument(
        "--cache-media",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--min-foryou", type=int, default=200)
    parser.add_argument("--min-following", type=int, default=200)
    parser.add_argument("--max-scrolls", type=int, default=160)
    parser.add_argument("--skip-foryou", action="store_true")
    parser.add_argument("--skip-following", action="store_true")
    parser.add_argument("--skip-timelines", action="store_true")
    parser.add_argument("--timelines", default="Tech,Crypto,Business")
    parser.add_argument("--min-timeline", type=int, default=100)
    parser.add_argument("--model", default=None)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--min-score", type=int, default=6)
    parser.add_argument("--max-keep", type=int, default=40)
    args = parser.parse_args()

    digest_date = date.fromisoformat(args.date).isoformat()
    out_dir = args.out_dir
    shortlist = out_dir / "shortlist.json"
    digest_path = out_dir / f"digest-{digest_date}.json"
    media_dir = args.media_dir or (args.data_dir / "media")
    py = sys.executable

    if not args.skip_scrape:
        scrape_cmd = [
            py,
            "-m",
            "periscope.x_scrape.scrape_feeds",
            "--out-dir",
            str(out_dir),
            "--profile",
            str(args.profile),
            "--watermark",
            str(args.watermark),
            "--min-foryou",
            str(args.min_foryou),
            "--min-following",
            str(args.min_following),
            "--max-scrolls",
            str(args.max_scrolls),
            "--timelines",
            args.timelines,
            "--min-timeline",
            str(args.min_timeline),
        ]
        if args.headless:
            scrape_cmd.append("--headless")
        if args.skip_foryou:
            scrape_cmd.append("--skip-foryou")
        if args.skip_following:
            scrape_cmd.append("--skip-following")
        if args.skip_timelines:
            scrape_cmd.append("--skip-timelines")
        _run("scrape_feeds", scrape_cmd)

    if not args.skip_curate:
        curate_cmd = [
            py,
            "-m",
            "periscope.x_scrape.curate_feeds",
            "--in-dir",
            str(out_dir),
            "--batch-size",
            str(args.batch_size),
            "--min-score",
            str(args.min_score),
            "--max-keep",
            str(args.max_keep),
        ]
        if args.model:
            curate_cmd.extend(["--model", args.model])
        _run("curate_feeds", curate_cmd)

    if not args.skip_hydrate:
        _run(
            "hydrate_shortlist",
            [
                py,
                "-m",
                "periscope.x_scrape.hydrate_shortlist",
                "--shortlist",
                str(shortlist),
            ],
        )

    if not args.skip_digest:
        digest_cmd = [
            py,
            "-m",
            "periscope.x_scrape.shortlist_to_digest",
            "--shortlist",
            str(shortlist),
            "--out",
            str(digest_path),
            "--date",
            digest_date,
            "--media-dir",
            str(media_dir),
        ]
        digest_cmd.append("--cache-media" if args.cache_media else "--no-cache-media")
        _run("shortlist_to_digest", digest_cmd)

    if not args.skip_ingest:
        _run(
            "ingest",
            [
                py,
                "-m",
                "periscope.jobs.ingest",
                "--config",
                str(args.config),
                "--data-dir",
                str(args.data_dir),
                "--file",
                str(digest_path),
                "--date",
                digest_date,
            ],
        )

    print(f"pipeline done date={digest_date} digest={digest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
