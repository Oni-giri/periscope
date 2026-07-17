"""Run fetch, curation, ranking, digest persistence, and Telegram delivery."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from periscope.cluster import cluster_tweets
from periscope.config import AppConfig, Secrets, load_config, load_secrets
from periscope.db import Database
from periscope.jobs.fetchonly import FetchResult, run_fetch
from periscope.llm import (
    JSONLLM,
    AnthropicJSONLLM,
    DeterministicJSONLLM,
    LLMError,
    load_prompt,
)
from periscope.picks import select_picks
from periscope.render import assemble_digest, render_telegram
from periscope.telegram.bot import Notifier, build_notifier
from periscope.xclient import XClient


@dataclass(frozen=True, slots=True)
class DailyResult:
    date: str
    fetch: FetchResult | None
    tweet_count: int
    cluster_count: int
    pick_count: int
    digest_written: bool
    errors: int = 0
    note: str | None = None


def _optional_prompt(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _build_llm(
    config: AppConfig,
    secrets: Secrets,
    database: Database,
    *,
    mock_llm: bool,
) -> tuple[JSONLLM, str, str]:
    if mock_llm:
        return DeterministicJSONLLM(), "", ""
    if not secrets.anthropic_api_key:
        raise LLMError("ANTHROPIC_API_KEY is required outside --mock-llm mode")
    cluster_prompt = load_prompt(config.prompts.cluster, "cluster")
    picks_prompt = load_prompt(config.prompts.picks, "picks")
    return (
        AnthropicJSONLLM(
            secrets.anthropic_api_key,
            database=database,
            models=config.models,
        ),
        cluster_prompt,
        picks_prompt,
    )


async def run_daily(
    config: AppConfig,
    secrets: Secrets,
    *,
    database: Database | None = None,
    xclient: XClient | None = None,
    llm: JSONLLM | None = None,
    notifier: Notifier | None = None,
    mock_x: str | Path | None = None,
    mock_llm: bool = False,
    digest_date: date | None = None,
    now: datetime | None = None,
    skip_fetch: bool = False,
) -> DailyResult:
    database = database or Database(config.db_path)
    database.initialize()
    database.seed(config, secrets)
    notifier = notifier or build_notifier(secrets, config.delivery)
    assembled_at = now or datetime.now(UTC)
    if assembled_at.tzinfo is None:
        assembled_at = assembled_at.replace(tzinfo=UTC)
    target_date = digest_date or assembled_at.astimezone(ZoneInfo(config.schedule.timezone)).date()

    fetch_result = None
    if not skip_fetch:
        fetch_result = await run_fetch(
            config,
            secrets,
            database=database,
            xclient=xclient,
            notifier=notifier,
            mock_x=mock_x,
            now=assembled_at,
        )

    tweets = database.tweets_for_digest(target_date)
    if not tweets:
        cluster_drafts: list[dict] = []
        pick_drafts: list[dict] = []
        decisions: list[dict] = []
    else:
        try:
            runtime_settings = database.get_settings()
            if llm is None:
                active_llm, cluster_prompt, picks_prompt = _build_llm(
                    config,
                    secrets,
                    database,
                    mock_llm=mock_llm,
                )
            else:
                active_llm = llm
                cluster_prompt = _optional_prompt(config.prompts.cluster)
                picks_prompt = _optional_prompt(config.prompts.picks)

            cluster_drafts = await cluster_tweets(
                tweets,
                llm=active_llm,
                model=config.models.cheap,
                system_prompt=cluster_prompt,
                aggressiveness=int(runtime_settings.get("clustering.aggressiveness", "45")),
            )
            pick_drafts, decisions = await select_picks(
                tweets,
                llm=active_llm,
                model=config.models.cheap,
                system_prompt=picks_prompt,
                minimum=config.picks.minimum,
                maximum=config.picks.maximum,
            )
        except Exception as exc:
            note = str(exc)
            if database.record_job_failure("daily", note):
                await notifier.send_alert(f"Daily digest failed: {note}")
            return DailyResult(
                target_date.isoformat(),
                fetch_result,
                len(tweets),
                0,
                0,
                False,
                errors=1,
                note=note,
            )

    runtime_settings = database.get_settings()
    topic_weights = database.apply_topic_decay(
        at=target_date,
        enabled=runtime_settings.get("topic_decay.enabled", "true") == "true",
    )
    digest = assemble_digest(
        digest_date=target_date,
        assembled_at=assembled_at,
        tweets=tweets,
        cluster_drafts=cluster_drafts,
        pick_drafts=pick_drafts,
        fetch_new_items=fetch_result.new_items if fetch_result else 0,
        topic_weights=topic_weights,
    )
    database.replace_digest(
        digest_date=target_date,
        assembled_at=assembled_at,
        clusters=digest["clusters"],
        picks=digest["picks"],
        decisions=decisions,
        stats=digest["stats"],
        rendered=digest,
    )

    try:
        await notifier.send_digest(digest, render_telegram(digest))
    except Exception as exc:
        note = f"Digest stored, but Telegram delivery failed: {exc}"
        database.record_job_failure("daily_delivery", note)
        return DailyResult(
            target_date.isoformat(),
            fetch_result,
            len(tweets),
            len(digest["clusters"]),
            len(digest["picks"]),
            True,
            errors=1,
            note=note,
        )

    return DailyResult(
        target_date.isoformat(),
        fetch_result,
        len(tweets),
        len(digest["clusters"]),
        len(digest["picks"]),
        True,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument("--mock-x", type=Path, metavar="FIXTURE")
    parser.add_argument(
        "--mock-llm",
        action="store_true",
        help="use structural deterministic curation without contacting Anthropic",
    )
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--date", type=date.fromisoformat)
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, data_dir=args.data_dir)
    secrets = load_secrets(args.secrets, data_dir=config.data_dir)
    result = asyncio.run(
        run_daily(
            config,
            secrets,
            mock_x=args.mock_x,
            mock_llm=args.mock_llm,
            digest_date=args.date,
            skip_fetch=args.skip_fetch,
        )
    )
    print(json.dumps(asdict(result), sort_keys=True))
    if result.errors:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
