#!/usr/bin/env python3
"""First-pass tweet curator via OpenRouter. Writes shortlist.json. No posting."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "meta/muse-spark-1.3-contributor"
ENV_PATH = Path(
    os.environ.get("PERISCOPE_SECRETS")
    or os.environ.get("OPENROUTER_ENV_FILE")
    or "data/secrets.env"
)

DEFAULT_INTERESTS = ("ai", "latvia", "crypto", "tools", "science", "business")

DEFAULT_SYSTEM = """You rank tweets for a daily magazine. The reader has ADHD and does not want doomscroll bait.

KEEP if it is notable for: AI/ML models and tools, Latvia (informational, not electoral combat), crypto/DeFi (real protocol/product news, not shills), new developer tools, statistics/science, or serious business/tech.

SKIP: ads, ragebait, engagement bait, reply-guy nothing, Latvian electoral tactics, price-go-up memes, generic motivational posts, duplicates of a more complete tweet in the batch.

Return JSON only:
{"items":[{"status_id":"...","keep":true,"score":0,"topic":"ai|latvia|crypto|tools|science|business|other","why":"one line","skip_reason":""}]}
score is 0-10. Keep only score >= 6 unless it is uniquely important.
Every input status_id must appear exactly once.

You MAY use **bold** sparingly in why/commentary text to emphasize important names, numbers, and key points for ADHD readability. Never invent other markup.
"""

# Back-compat alias used by older imports/tests.
SYSTEM = DEFAULT_SYSTEM


def _interest_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "other"


def load_interest_topics(db_path: Path | None = None) -> list[str]:
    """Read curator interests from the topics table; fall back to hardcoded defaults."""

    candidates: list[Path] = []
    if db_path is not None:
        candidates.append(Path(db_path))
    data_dir = os.environ.get("PERISCOPE_DATA_DIR")
    if data_dir:
        candidates.append(Path(data_dir) / "periscope.db")
    candidates.append(Path("data/periscope.db"))
    env_db = os.environ.get("PERISCOPE_DB")
    if env_db:
        candidates.insert(0, Path(env_db))

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            import sqlite3

            with sqlite3.connect(candidate) as connection:
                rows = connection.execute(
                    "SELECT name FROM topics ORDER BY name COLLATE NOCASE"
                ).fetchall()
            names = [_interest_slug(row[0]) for row in rows if row and row[0]]
            names = [name for name in names if name and name != "other"]
            # de-dupe preserving order
            seen: set[str] = set()
            clean: list[str] = []
            for name in names:
                if name in seen:
                    continue
                seen.add(name)
                clean.append(name)
            if clean:
                return clean
        except Exception:
            continue
    return list(DEFAULT_INTERESTS)


def build_system_prompt(topics: list[str] | None = None) -> str:
    interests = topics or list(DEFAULT_INTERESTS)
    joined = ", ".join(interests)
    enum = "|".join([*interests, "other"])
    return (
        "You rank tweets for a daily magazine. The reader has ADHD and does not want doomscroll bait.\n\n"
        f"KEEP if it is notable for these interests: {joined}.\n\n"
        "SKIP: ads, ragebait, engagement bait, reply-guy nothing, price-go-up memes, "
        "generic motivational posts, duplicates of a more complete tweet in the batch.\n\n"
        "Return JSON only:\n"
        '{"items":[{"status_id":"...","keep":true,"score":0,'
        f'"topic":"{enum}","why":"one line","skip_reason":""}}]}}\n'
        "score is 0-10. Keep only score >= 6 unless it is uniquely important.\n"
        "Every input status_id must appear exactly once.\n\n"
        "You MAY use **bold** sparingly in why/commentary text to emphasize important names, numbers, and key points for ADHD readability. Never invent other markup.\n"
    )


def build_schema(topics: list[str] | None = None) -> dict:
    interests = topics or list(DEFAULT_INTERESTS)
    enum = [*interests, "other"]
    # ensure unique
    seen: set[str] = set()
    clean_enum: list[str] = []
    for item in enum:
        if item in seen:
            continue
        seen.add(item)
        clean_enum.append(item)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "tweet_shortlist",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "status_id": {"type": "string"},
                                "keep": {"type": "boolean"},
                                "score": {"type": "integer"},
                                "topic": {
                                    "type": "string",
                                    "enum": clean_enum,
                                },
                                "why": {"type": "string"},
                                "skip_reason": {"type": "string"},
                            },
                            "required": [
                                "status_id",
                                "keep",
                                "score",
                                "topic",
                                "why",
                                "skip_reason",
                            ],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    }


SCHEMA = build_schema()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def load_json(path: Path) -> list:
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return data if isinstance(data, list) else []


def compact(post: dict, feed: str) -> dict:
    text = (post.get("text") or "").strip()
    if len(text) > 700:
        text = text[:700] + "…"
    return {
        "status_id": str(post.get("status_id") or ""),
        "feed": feed,
        "author": post.get("author_handle") or "",
        "text": text,
        "has_image": bool(post.get("image_urls")),
        "truncated": bool(post.get("is_truncated")),
    }


def gather(out_dir: Path) -> tuple[list[dict], dict[str, dict]]:
    files = [
        ("foryou", out_dir / "foryou.json"),
        ("following", out_dir / "following.json"),
    ]
    for p in sorted(out_dir.glob("timeline_*.json")):
        files.append((p.stem.replace("timeline_", ""), p))
    by_id: dict[str, dict] = {}
    compact_posts: list[dict] = []
    for feed, path in files:
        for post in load_json(path):
            sid = str(post.get("status_id") or "")
            if not sid or sid in by_id:
                continue
            post = dict(post)
            post["feed"] = post.get("feed") or feed
            by_id[sid] = post
            compact_posts.append(compact(post, post["feed"]))
    return compact_posts, by_id


def batches(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def call_openrouter(
    client: httpx.Client,
    key: str,
    model: str,
    batch: list[dict],
    *,
    system: str | None = None,
    schema: dict | None = None,
) -> list[dict]:
    payload = {
        "model": model,
        "temperature": 0.1,
        "reasoning": {"effort": "low"},
        "response_format": schema or SCHEMA,
        "messages": [
            {"role": "system", "content": system or SYSTEM},
            {
                "role": "user",
                "content": "Rank these tweets:\n" + json.dumps(batch, ensure_ascii=False),
            },
        ],
    }
    r = client.post(
        OPENROUTER,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/Oni-giri/periscope",
            "X-Title": "Periscope curator",
        },
        json=payload,
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    data = json.loads(content)
    return data.get("items") or []


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--in-dir", type=Path, default=Path("data/x-dumps"))
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--min-score", type=int, default=6)
    p.add_argument("--max-keep", type=int, default=40)
    args = p.parse_args()

    load_dotenv(ENV_PATH)
    load_dotenv(Path(".env"))
    interests = load_interest_topics()
    system_prompt = build_system_prompt(interests)
    response_schema = build_schema(interests)
    print(f"interests: {', '.join(interests)}", flush=True)
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        print(
            f"MISSING_KEY: put OPENROUTER_API_KEY in {ENV_PATH} or the environment.",
            flush=True,
        )
        return 2

    posts, by_id = gather(args.in_dir)
    ads = [x for x in posts if by_id[x["status_id"]].get("is_ad")]
    work = [x for x in posts if not by_id[x["status_id"]].get("is_ad")]
    print(f"loaded {len(posts)} unique ({len(ads)} ads skipped locally)", flush=True)

    judged: list[dict] = []
    with httpx.Client() as client:
        for i, batch in enumerate(batches(work, args.batch_size), start=1):
            print(f"batch {i}: {len(batch)} tweets via {args.model}", flush=True)
            for attempt in range(3):
                try:
                    judged.extend(
                        call_openrouter(
                            client,
                            key,
                            args.model,
                            batch,
                            system=system_prompt,
                            schema=response_schema,
                        )
                    )
                    break
                except Exception as e:
                    print(f"  retry {attempt + 1}: {e}", flush=True)
                    time.sleep(2 * (attempt + 1))
            else:
                print("  batch failed, skipping", flush=True)

    by_judge = {str(j.get("status_id")): j for j in judged if j.get("status_id")}
    keepers = []
    skipped = []
    for post in work:
        sid = post["status_id"]
        j = by_judge.get(sid) or {
            "status_id": sid,
            "keep": False,
            "score": 0,
            "topic": "other",
            "why": "",
            "skip_reason": "model miss",
        }
        full = dict(by_id[sid])
        full["curation"] = {
            "keep": bool(j.get("keep")) and int(j.get("score") or 0) >= args.min_score,
            "score": int(j.get("score") or 0),
            "topic": j.get("topic") or "other",
            "why": j.get("why") or "",
            "skip_reason": j.get("skip_reason") or "",
        }
        if full["curation"]["keep"]:
            keepers.append(full)
        else:
            skipped.append(full)

    keepers.sort(key=lambda x: (-x["curation"]["score"], x.get("created_at") or ""))
    if len(keepers) > args.max_keep:
        extra = keepers[args.max_keep :]
        for x in extra:
            x["curation"]["keep"] = False
            x["curation"]["skip_reason"] = "over max-keep"
        skipped.extend(extra)
        keepers = keepers[: args.max_keep]

    shortlist = {
        "model": args.model,
        "unique_in": len(posts),
        "ads_dropped": len(ads),
        "judged": len(by_judge),
        "kept": len(keepers),
        "keepers": keepers,
        "skipped_ids": [x["status_id"] for x in skipped],
    }
    out = args.in_dir / "shortlist.json"
    out.write_text(json.dumps(shortlist, ensure_ascii=False, indent=2) + "\n")
    print(
        f"wrote {out} kept {len(keepers)} / {len(posts)} (min_score {args.min_score})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
