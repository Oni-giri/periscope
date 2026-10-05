#!/usr/bin/env python3
"""Run scrape → topic accounts → curate → hydrate → digest → enrich → ingest [→ actions].

Topic accounts (Settings → Accounts) are scraped after the main account; a
signed-out one logs NOT_SIGNED_IN and is skipped. ``--actions`` enables likes and
follows on the owning topic accounts; without it nothing is clicked.
"""

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
        default=Path(os.environ.get("PERISCOPE_X_CHROME_PROFILE") or "data/chrome-profile"),
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
    parser.add_argument("--skip-enrich", action="store_true")
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
    parser.add_argument(
        "--skip-accounts",
        action="store_true",
        help="Do not scrape topic accounts' Following timelines",
    )
    parser.add_argument(
        "--min-account-following",
        type=int,
        default=None,
        help="Min unique Following posts per topic account (default: per-account, 100)",
    )
    parser.add_argument(
        "--actions",
        action="store_true",
        help="After ingest, like keepers / follow routed handles on topic accounts",
    )
    parser.add_argument(
        "--actions-dry-run",
        action="store_true",
        help="With --actions: print the like/follow plan without clicking",
    )
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
            "--db",
            str(Path(args.data_dir) / "periscope.db"),
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

    if not args.skip_scrape and not args.skip_accounts:
        accounts_cmd = [
            py,
            "-m",
            "periscope.x_scrape.account_scrape",
            "--data-dir",
            str(args.data_dir),
            "--out-dir",
            str(out_dir),
            "--max-scrolls",
            str(args.max_scrolls),
        ]
        if args.min_account_following is not None:
            accounts_cmd.extend(["--min-following", str(args.min_account_following)])
        if args.headless:
            accounts_cmd.append("--headless")
        # Soft-fail: a broken topic account must not block the main magazine.
        print("==> account_scrape", flush=True)
        print(" ".join(accounts_cmd), flush=True)
        result = subprocess.run(accounts_cmd, check=False)
        if result.returncode != 0:
            print(f"account_scrape exited {result.returncode}; continuing", flush=True)

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

    if not args.skip_enrich and digest_path.is_file():
        enrich_cmd = [
            py,
            "-m",
            "periscope.x_scrape.enrich_actions",
            "--digest",
            str(digest_path),
        ]
        if args.model:
            enrich_cmd.extend(["--model", args.model])
        # Soft-fail: heuristics still write; LLM missing key is non-fatal.
        print("==> enrich_actions", flush=True)
        print(" ".join(enrich_cmd), flush=True)
        result = subprocess.run(enrich_cmd, check=False)
        if result.returncode not in (0,):
            print(
                f"enrich_actions exited {result.returncode}; continuing with digest as-is",
                flush=True,
            )

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
        # Soft-fail: leftovers must not block the magazine.
        print("==> ingest_overflow", flush=True)
        try:
            from periscope.config import load_config
            from periscope.db import Database
            from periscope.x_scrape.ingest_overflow import ingest_overflow

            cfg = load_config(args.config, data_dir=args.data_dir)
            database = Database(cfg.db_path)
            database.initialize()
            overflow = ingest_overflow(
                database,
                dumps_dir=out_dir,
                digest_path=digest_path if digest_path.is_file() else None,
            )
            print(
                "overflow "
                f"leftovers={overflow.leftover_count} ads={overflow.skipped_ads} "
                f"digest={overflow.skipped_digest} fetch_id={overflow.fetch_id}",
                flush=True,
            )
        except Exception as exc:
            print(f"ingest_overflow failed: {exc}; continuing", flush=True)

    if args.actions and digest_path.is_file():
        actions_cmd = [
            py,
            "-m",
            "periscope.x_scrape.account_actions",
            "--digest",
            str(digest_path),
            "--data-dir",
            str(args.data_dir),
        ]
        if args.headless:
            actions_cmd.append("--headless")
        if args.actions_dry_run:
            actions_cmd.append("--dry-run")
        print("==> account_actions", flush=True)
        print(" ".join(actions_cmd), flush=True)
        result = subprocess.run(actions_cmd, check=False)
        if result.returncode != 0:
            print(f"account_actions exited {result.returncode}; continuing", flush=True)
    elif not args.actions:
        print("actions disabled (pass --actions to like/follow on topic accounts)", flush=True)

    print(f"pipeline done date={digest_date} digest={digest_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
