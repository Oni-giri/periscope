"""Build weekly graph discovery evidence and an editorial meta-report."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from periscope.config import AppConfig, Secrets, load_config, load_secrets
from periscope.db import Database
from periscope.discovery import (
    candidate_reason,
    cofollow_sources,
    following_delta,
    interaction_counts,
    overlap_percentage,
    profile_stats,
)
from periscope.jobs.fetchonly import build_xclient
from periscope.llm import JSONLLM, AnthropicJSONLLM, LLMError, load_prompt
from periscope.telegram.bot import Notifier, build_notifier
from periscope.xclient import XClient, payload_author


@dataclass(frozen=True, slots=True)
class WeeklyResult:
    week: str
    candidate_count: int
    snapshot_count: int
    tweet_count: int
    report_written: bool
    errors: int = 0
    note: str | None = None


def week_bounds(value: date) -> tuple[str, date, date]:
    start = value - timedelta(days=value.weekday())
    iso_year, iso_week, _ = start.isocalendar()
    return f"{iso_year}-W{iso_week:02d}", start, start + timedelta(days=6)


def parse_week(value: str) -> date:
    try:
        year_text, week_text = value.upper().split("-W", 1)
        return date.fromisocalendar(int(year_text), int(week_text), 1)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("week must use YYYY-Www format") from exc


def _topic_rows(database: Database, start: date, end: date) -> list[dict[str, Any]]:
    rows = database.rows(
        "SELECT tag, COUNT(*) AS count FROM clusters "
        "WHERE digest_date BETWEEN ? AND ? GROUP BY tag "
        "ORDER BY count DESC, tag LIMIT 5",
        (start.isoformat(), end.isoformat()),
    )
    return [
        {
            "title": str(row["tag"]),
            "summary": f"{int(row['count'])} clustered stories carried this theme.",
            "count": int(row["count"]),
        }
        for row in rows
    ]


def _account_trends(
    database: Database,
    handles: Sequence[str],
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    previous_start = start - timedelta(days=7)
    previous_end = start - timedelta(days=1)

    def counts(period_start: date, period_end: date) -> Counter[str]:
        rows = database.rows(
            "SELECT author, COUNT(*) AS count FROM tweets "
            "WHERE substr(created_at, 1, 10) BETWEEN ? AND ? "
            "GROUP BY author",
            (period_start.isoformat(), period_end.isoformat()),
        )
        return Counter({str(row["author"]): int(row["count"]) for row in rows})

    current = counts(start, end)
    previous = counts(previous_start, previous_end)
    changes = [(handle, current[handle] - previous[handle]) for handle in handles]
    up = [
        {"handle": handle, "delta": delta, "reason": "more posts in the digest window"}
        for handle, delta in sorted(changes, key=lambda item: (-item[1], item[0]))
        if delta > 0
    ][:5]
    down = [
        {"handle": handle, "delta": delta, "reason": "fewer posts in the digest window"}
        for handle, delta in sorted(changes, key=lambda item: (item[1], item[0]))
        if delta < 0
    ][:5]
    return up, down


def _candidate_handles(posts: Sequence[Mapping[str, Any]]) -> set[str]:
    return {payload_author(post) for post in posts if payload_author(post)}


def _validate_narrative(value: Any, fallback: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LLMError("Weekly model output must be a JSON object")
    result = dict(fallback)
    themes = value.get("themes")
    if isinstance(themes, list):
        clean = []
        for theme in themes[:5]:
            if not isinstance(theme, Mapping):
                continue
            title = str(theme.get("title", "")).strip()
            summary = str(theme.get("summary", "")).strip()
            if title and summary:
                clean.append({"title": title, "summary": summary})
        if clean:
            result["themes"] = clean
    for key in ("trending_up", "trending_down"):
        rows = value.get(key)
        if isinstance(rows, list):
            clean_rows = []
            for row in rows[:5]:
                if not isinstance(row, Mapping):
                    continue
                handle = str(row.get("handle", "")).strip().removeprefix("@").lower()
                reason = str(row.get("reason", "")).strip()
                try:
                    delta = int(row.get("delta", 0))
                except (TypeError, ValueError):
                    delta = 0
                if handle:
                    clean_rows.append({"handle": handle, "delta": delta, "reason": reason})
            result[key] = clean_rows
    suggestions = value.get("suggestions")
    if isinstance(suggestions, Mapping):
        result["suggestions"] = {
            "add": [str(item).removeprefix("@").lower() for item in suggestions.get("add", [])][:5],
            "drop": [str(item).removeprefix("@").lower() for item in suggestions.get("drop", [])][
                :5
            ],
        }
    return result


async def run_weekly(
    config: AppConfig,
    secrets: Secrets,
    *,
    database: Database | None = None,
    xclient: XClient | None = None,
    llm: JSONLLM | None = None,
    notifier: Notifier | None = None,
    mock_x: str | Path | None = None,
    mock_llm: bool = False,
    target_week: date | None = None,
    now: datetime | None = None,
) -> WeeklyResult:
    database = database or Database(config.db_path)
    database.initialize()
    database.seed(config, secrets)
    client = xclient or build_xclient(config, secrets, mock_x=mock_x)
    notifier = notifier or build_notifier(secrets, config.delivery)
    assembled_at = now or datetime.now(UTC)
    if assembled_at.tzinfo is None:
        assembled_at = assembled_at.replace(tzinfo=UTC)
    week, start, end = week_bounds(target_week or assembled_at.date())
    accounts = [str(row["handle"]) for row in database.list_accounts()]
    fetch_id = database.start_fetch("weekly", now=assembled_at)
    error_notes: list[str] = []

    additions: dict[str, set[str]] = {}
    snapshots: dict[str, list[str]] = {}
    for handle in accounts:
        try:
            current = await client.following_handles(handle)
            previous = database.latest_follow_snapshot(handle, before=assembled_at)
            additions[handle] = following_delta(
                previous["following"] if previous else [],
                current,
            )["added"]
            snapshots[handle] = current
            database.store_follow_snapshot(handle, current, taken_at=assembled_at)
        except Exception as exc:
            error_notes.append(f"@{handle} graph: {exc}")

    tweets = database.tweets_between(start, end)
    interactions = interaction_counts(tweets, curated_handles=accounts)
    topic_hits: dict[str, list[str]] = defaultdict(list)
    search_items = 0
    for topic in config.topics:
        query = f"min_faves:{topic.min_faves} {topic.name}"
        try:
            posts = await client.search_posts(query, limit=20)
            for post in posts:
                if database.store_tweet(post, fetch_id=fetch_id, fetched_at=assembled_at):
                    search_items += 1
            for handle in _candidate_handles(posts):
                if handle not in accounts:
                    topic_hits[handle].append(topic.name)
        except Exception as exc:
            error_notes.append(f"{topic.name} search: {exc}")

    source_map = cofollow_sources(additions, curated_handles=accounts)
    eligible = {
        handle
        for handle in set(source_map) | set(interactions) | set(topic_hits)
        if len(source_map.get(handle, set())) >= 2
        or interactions[handle] >= 3
        or bool(topic_hits.get(handle))
    }
    graph_union = {item for values in snapshots.values() for item in values}
    surfaced = 0
    for handle in sorted(eligible):
        try:
            profile = await client.profile(handle)
        except Exception as exc:
            error_notes.append(f"@{handle} profile: {exc}")
            profile = {"username": handle}
        try:
            candidate_graph = await client.following_handles(handle)
        except Exception:
            candidate_graph = []
        sources = source_map.get(handle, set())
        stats = {
            **profile_stats(profile),
            "interaction_count": interactions[handle],
            "topic_hits": topic_hits.get(handle, []),
            "cofollow_sources": sorted(sources),
        }
        if database.upsert_candidate(
            handle=handle,
            reason=candidate_reason(
                cofollow_count=len(sources),
                interaction_count=interactions[handle],
                topic_hits=topic_hits.get(handle, []),
            ),
            cofollow_count=len(sources),
            overlap_pct=overlap_percentage(candidate_graph, graph_union),
            stats=stats,
            surfaced_at=assembled_at,
        ):
            surfaced += 1

    database.finish_fetch(
        fetch_id,
        new_items=search_items,
        errors=len(error_notes),
        note="; ".join(error_notes) if error_notes else None,
        now=assembled_at,
    )

    trend_up, trend_down = _account_trends(database, accounts, start, end)
    pending = database.list_candidates()
    calibration = database.weekly_calibration(start, end)
    story_rows = database.rows(
        "SELECT COUNT(*) AS count FROM clusters WHERE digest_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    )
    story_count = int(story_rows[0]["count"]) if story_rows else 0
    report: dict[str, Any] = {
        "week": week,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "themes": _topic_rows(database, start, end),
        "trending_up": trend_up,
        "trending_down": trend_down,
        "suggestions": {
            "add": [str(item["handle"]) for item in pending[:5]],
            "drop": [str(item["handle"]) for item in trend_down if item["delta"] < 0][:5],
        },
        "calibration": {
            "kept_count": calibration["kept_count"],
            "pick_count": calibration["pick_count"],
            "kept_not_picked": [
                {
                    "id": tweet["id"],
                    "author": tweet["author"],
                    "text": tweet["text"],
                    "created_at": tweet["created_at"],
                }
                for tweet in calibration["kept_not_picked"][:10]
            ],
        },
    }
    stats = {
        "items": len(tweets),
        "stories": story_count,
        "accounts": len({str(tweet["author"]) for tweet in tweets}),
        "reading_hours_saved": round(len(tweets) * 0.75 / 60, 1),
    }

    if not mock_llm:
        try:
            active_llm = llm
            if active_llm is None:
                if not secrets.anthropic_api_key:
                    raise LLMError("ANTHROPIC_API_KEY is required outside --mock-llm mode")
                prompt = load_prompt(config.prompts.weekly, "weekly")
                active_llm = AnthropicJSONLLM(
                    secrets.anthropic_api_key,
                    database=database,
                    models=config.models,
                )
            else:
                prompt = (
                    config.prompts.weekly.read_text(encoding="utf-8")
                    if config.prompts.weekly and config.prompts.weekly.exists()
                    else ""
                )
            narrative = await active_llm.complete_json(
                "weekly",
                system_prompt=prompt,
                payload={"report_facts": report, "stats": stats},
                model=config.models.quality,
            )
            report = _validate_narrative(narrative, report)
        except Exception as exc:
            note = str(exc)
            if database.record_job_failure("weekly", note):
                await notifier.send_alert(f"Weekly report failed: {note}")
            return WeeklyResult(
                week,
                surfaced,
                len(snapshots),
                len(tweets),
                False,
                errors=1,
                note=note,
            )

    database.replace_weekly_report(
        week=week,
        start_date=start,
        end_date=end,
        assembled_at=assembled_at,
        stats=stats,
        rendered=report,
    )
    try:
        await notifier.send_weekly(report)
    except Exception as exc:
        error_notes.append(f"weekly delivery: {exc}")
        database.record_job_failure("weekly_delivery", str(exc))

    return WeeklyResult(
        week,
        surfaced,
        len(snapshots),
        len(tweets),
        True,
        errors=len(error_notes),
        note="; ".join(error_notes) if error_notes else None,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--secrets", type=Path)
    parser.add_argument("--mock-x", type=Path, metavar="FIXTURE")
    parser.add_argument("--mock-llm", action="store_true")
    parser.add_argument("--week", type=parse_week)
    return parser


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, data_dir=args.data_dir)
    secrets = load_secrets(args.secrets, data_dir=config.data_dir)
    result = asyncio.run(
        run_weekly(
            config,
            secrets,
            mock_x=args.mock_x,
            mock_llm=args.mock_llm,
            target_week=args.week,
        )
    )
    print(json.dumps(asdict(result), sort_keys=True))
    if result.errors and not result.report_written:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
