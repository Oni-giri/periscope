#!/usr/bin/env python3
"""First-pass tweet curator via OpenRouter. Writes shortlist.json. No posting."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

DEFAULT_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER = f"{DEFAULT_OPENROUTER_BASE}/chat/completions"
DEFAULT_MODEL = "meta/muse-spark-1.3-contributor"
ENV_PATH = Path(
    os.environ.get("PERISCOPE_SECRETS")
    or os.environ.get("OPENROUTER_ENV_FILE")
    or "data/secrets.env"
)



def resolve_model(explicit: str | None = None) -> str:
    """CLI/env/settings/default model for OpenRouter curator calls."""
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    env = (
        os.environ.get("OPENROUTER_MODEL", "").strip()
        or os.environ.get("PERISCOPE_LLM_MODEL", "").strip()
    )
    if env:
        return env
    for folder in _data_dirs():
        db_path = folder / "periscope.db"
        if not db_path.exists():
            continue
        try:
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT value FROM settings WHERE key = ? LIMIT 1",
                    ("llm.model",),
                ).fetchone()
            if row and str(row[0]).strip():
                return str(row[0]).strip()
        except Exception:  # noqa: BLE001 - fall through
            continue
    return DEFAULT_MODEL


def normalize_openrouter_base(value: str | None = None) -> str:
    """Return an OpenRouter-compatible API root (no /chat/completions suffix)."""

    raw = str(value or "").strip() or os.environ.get("OPENROUTER_BASE_URL", "").strip()
    raw = raw or DEFAULT_OPENROUTER_BASE
    raw = raw.rstrip("/")
    if raw.endswith("/chat/completions"):
        raw = raw[: -len("/chat/completions")].rstrip("/")
    if not raw.startswith(("http://", "https://")):
        raise ValueError("OpenRouter base URL must start with http:// or https://")
    return raw


def openrouter_chat_completions_url(base: str | None = None) -> str:
    return f"{normalize_openrouter_base(base)}/chat/completions"


# Sample only — Settings placeholder / Reset-example. Never used as a runtime fallback.
DEFAULT_INTERESTS = ("ai", "latvia", "crypto", "tools", "science", "business")
NO_INTERESTS_MESSAGE = "NO_INTERESTS: set interests in Settings → Reading"
CURATOR_PROMPT_FILENAME = "curator_system.md"

DEFAULT_SYSTEM_TEMPLATE = """You rank tweets for a daily magazine. The reader has ADHD and does not want doomscroll bait.

KEEP if it is notable for these interests: {interests}.

SKIP: ads, ragebait, engagement bait, reply-guy nothing, price-go-up memes, generic motivational posts, duplicates of a more complete tweet in the batch.

Return JSON only:
{"items":[{"status_id":"...","keep":true,"score":0,"topic":"{topic_enum}","why":"one line","skip_reason":""}]}
score is 0-10. Keep only score >= 6 unless it is uniquely important.
Every input status_id must appear exactly once.

You MAY use **bold** sparingly in why/commentary text to emphasize important names, numbers, and key points for ADHD readability. Never invent other markup.
"""

# Back-compat aliases (unfilled template — not a runnable curator prompt).
DEFAULT_SYSTEM = DEFAULT_SYSTEM_TEMPLATE
SYSTEM = DEFAULT_SYSTEM_TEMPLATE


class NoInterestsError(RuntimeError):
    """Raised when the curator has no valid interest topics."""


def interest_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug


def parse_interest_lines(raw: str) -> list[str]:
    """Split a Settings textarea (newlines or commas) into unique display names."""

    names: list[str] = []
    seen: set[str] = set()
    for part in str(raw or "").replace(",", "\n").splitlines():
        name = part.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def normalize_interest_topics(names: list[str] | tuple[str, ...] | None) -> list[str]:
    """Slugify interest names the way the curator loads them."""

    seen: set[str] = set()
    clean: list[str] = []
    for name in names or []:
        slug = interest_slug(str(name))
        if not slug or slug == "other" or slug in seen:
            continue
        seen.add(slug)
        clean.append(slug)
    return clean


def require_topics(topics: list[str] | tuple[str, ...] | None) -> list[str]:
    clean = normalize_interest_topics(list(topics) if topics is not None else [])
    if not clean:
        raise NoInterestsError(NO_INTERESTS_MESSAGE)
    return clean


def topic_enum(topics: list[str]) -> str:
    interests = require_topics(topics)
    enum = [*interests, "other"]
    seen: set[str] = set()
    ordered: list[str] = []
    for item in enum:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return "|".join(ordered)


def keep_line_for(topics: list[str]) -> str:
    interests = require_topics(topics)
    return f"KEEP if it is notable for these interests: {', '.join(interests)}."


def fill_prompt_template(template: str, topics: list[str]) -> str:
    interests = require_topics(topics)
    joined = ", ".join(interests)
    enum = topic_enum(interests)
    body = template if str(template or "").strip() else DEFAULT_SYSTEM_TEMPLATE
    rendered = body.replace("{interests}", joined).replace("{topic_enum}", enum)
    if "{interests}" not in body:
        rendered = rendered.rstrip() + f"\n\n{keep_line_for(interests)}\nTopic enum: {enum}\n"
    return rendered


def _data_dirs(explicit: Path | None = None) -> list[Path]:
    dirs: list[Path] = []
    if explicit is not None:
        dirs.append(Path(explicit))
    env_dir = os.environ.get("PERISCOPE_DATA_DIR")
    if env_dir:
        dirs.append(Path(env_dir))
    env_db = os.environ.get("PERISCOPE_DB")
    if env_db:
        dirs.append(Path(env_db).expanduser().resolve().parent)
    dirs.append(Path("data"))
    unique: list[Path] = []
    seen: set[str] = set()
    for item in dirs:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def curator_prompt_path(data_dir: Path | None = None) -> Path:
    dirs = _data_dirs(data_dir)
    return dirs[0] / "prompts" / CURATOR_PROMPT_FILENAME


def load_system_prompt_template(data_dir: Path | None = None) -> str:
    """Custom file if present and non-empty, else the built-in default template."""

    folders = [Path(data_dir)] if data_dir is not None else _data_dirs()
    for folder in folders:
        path = folder / "prompts" / CURATOR_PROMPT_FILENAME
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    return DEFAULT_SYSTEM_TEMPLATE


def build_system_prompt(
    topics: list[str] | None = None,
    *,
    template: str | None = None,
    data_dir: Path | None = None,
) -> str:
    interests = require_topics(topics)
    body = DEFAULT_SYSTEM_TEMPLATE if template is None else template
    if template is None:
        body = load_system_prompt_template(data_dir)
    return fill_prompt_template(body, interests)


def build_schema(topics: list[str] | None = None) -> dict:
    interests = require_topics(topics)
    enum = [*interests, "other"]
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


def load_interest_topics(db_path: Path | None = None) -> list[str]:
    """Read curator interests from the topics table. Empty list is an error."""

    candidates: list[Path] = []
    if db_path is not None:
        candidates.append(Path(db_path))
    env_db = os.environ.get("PERISCOPE_DB")
    if env_db:
        candidates.append(Path(env_db))
    for folder in _data_dirs():
        candidates.append(folder / "periscope.db")

    seen_paths: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        key = str(candidate)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if not candidate.exists():
            continue
        try:
            with sqlite3.connect(candidate) as connection:
                rows = connection.execute(
                    "SELECT name FROM topics ORDER BY name COLLATE NOCASE"
                ).fetchall()
            names = [str(row[0]) for row in rows if row and row[0]]
            return require_topics(names)
        except NoInterestsError:
            raise
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            last_error = exc
            continue
    if last_error is not None:
        raise NoInterestsError(NO_INTERESTS_MESSAGE) from last_error
    raise NoInterestsError(NO_INTERESTS_MESSAGE)


def describe_interest_input(
    raw: str,
    *,
    saved_names: list[str] | None = None,
    template: str | None = None,
) -> dict:
    """Validate a Settings textarea (or saved DB names) the way the curator would."""

    display_names = parse_interest_lines(raw)
    slugs = normalize_interest_topics(display_names)
    saved_slugs = normalize_interest_topics(saved_names or [])
    result: dict = {
        "ok": bool(slugs),
        "display_names": display_names,
        "topics": slugs,
        "saved_topics": saved_slugs,
        "db_differs": slugs != saved_slugs,
        "keep_line": "",
        "topic_enum": "",
        "prompt_snippet": "",
        "message": "",
    }
    if not slugs:
        result["message"] = NO_INTERESTS_MESSAGE
        return result
    rendered = fill_prompt_template(template or DEFAULT_SYSTEM_TEMPLATE, slugs)
    keep = keep_line_for(slugs)
    for line in rendered.splitlines():
        if line.startswith("KEEP"):
            keep = line.strip()
            break
    enum = topic_enum(slugs)
    result["keep_line"] = keep
    result["topic_enum"] = enum
    result["prompt_snippet"] = f'{keep}\ntopic: "{enum}"'
    result["message"] = f"Curator would load: {', '.join(slugs)}"
    return result


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



def _openrouter_usage_usd(body: dict, model: str) -> tuple[int, int, float]:
    """Extract token counts and USD cost from an OpenRouter chat response."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or 0
    )
    output_tokens = int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or 0
    )
    usd = 0.0
    for key in ("cost", "total_cost", "native_tokens_cost"):
        raw = usage.get(key)
        if raw is None:
            continue
        try:
            usd = float(raw)
            break
        except (TypeError, ValueError):
            continue
    # Some responses put cost on the root.
    if usd <= 0 and isinstance(body, dict):
        for key in ("cost", "total_cost"):
            raw = body.get(key)
            if raw is None:
                continue
            try:
                usd = float(raw)
                break
            except (TypeError, ValueError):
                continue
    return input_tokens, output_tokens, max(0.0, usd)


def record_openrouter_usage(body: dict, model: str) -> None:
    """Best-effort write of OpenRouter usage into periscope.db llm_spend."""
    input_tokens, output_tokens, usd = _openrouter_usage_usd(body, model)
    if input_tokens <= 0 and output_tokens <= 0 and usd <= 0:
        return
    for folder in _data_dirs():
        db_path = folder / "periscope.db"
        if not db_path.exists():
            continue
        try:
            from periscope.db import Database

            Database(db_path).record_llm_spend(
                model=str(model or "openrouter"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=usd,
            )
            return
        except Exception as exc:  # noqa: BLE001 - spend must never break curation
            print(f"llm_spend record soft-fail: {exc}", flush=True)
            return


def call_openrouter(
    client: httpx.Client,
    key: str,
    model: str,
    batch: list[dict],
    *,
    system: str,
    schema: dict,
) -> list[dict]:
    payload = {
        "model": model,
        "temperature": 0.1,
        "reasoning": {"effort": "low"},
        "response_format": schema,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": "Rank these tweets:\n" + json.dumps(batch, ensure_ascii=False),
            },
        ],
    }
    r = client.post(
        openrouter_chat_completions_url(),
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
    record_openrouter_usage(body, model)
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
    p.add_argument("--model", default=None, help="OpenRouter model id (default: settings/env/built-in)")
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--min-score", type=int, default=6)
    p.add_argument("--max-keep", type=int, default=40)
    args = p.parse_args()
    args.model = resolve_model(args.model)

    load_dotenv(ENV_PATH)
    load_dotenv(Path(".env"))
    try:
        interests = load_interest_topics()
        system_prompt = build_system_prompt(interests)
        response_schema = build_schema(interests)
    except NoInterestsError as exc:
        print(str(exc), flush=True)
        return 2
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
