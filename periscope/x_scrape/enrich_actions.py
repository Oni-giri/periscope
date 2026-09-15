#!/usr/bin/env python3
"""Fill pick actions + nugget flags. Heuristic always; OpenRouter optional soft-fail."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "meta/muse-spark-1.3-contributor"
ENV_PATH = Path(
    os.environ.get("PERISCOPE_SECRETS")
    or os.environ.get("OPENROUTER_ENV_FILE")
    or "data/secrets.env"
)

ACTION_TYPES = ("try", "read", "watch", "steal", "follow")
MAX_ACTIONS = 3

_WATCH_WORDS = re.compile(
    r"\b(exploit|oracle|protocol|vulnerability|zero[- ]?day|rug|mev)\b",
    re.IGNORECASE,
)
_STEAL_WORDS = re.compile(
    r"\b(pattern|technique|playbook|template|reusable|contrarian|framework|heuristic)\b",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?:follow|worth following|@)[\s:@]*([A-Za-z0-9_]{2,30})", re.I)
_URL_RE = re.compile(r"https?://[^\s\]\)\"'<>]+", re.I)

SYSTEM = """You extract actionable follow-ups from curated X posts for a builder with ADHD.

For each pick return 0-3 actions with type in: try, read, watch, steal, follow.
- try: tool/model/repo/CLI to try
- read: paper/docs/thread
- watch: protocol/company/exploit class
- steal: reusable idea/pattern
- follow: account worth following

Also set nugget=true when the post is primarily an idea/technique/contrarian take/reusable pattern
(not breaking news). Set actionable=true when there is a concrete next step.
nugget_why is a short paragraph (up to ~800 chars) on why it matters for building.

Return JSON only:
{"picks":[{"tweet_id":"...","actions":[{"type":"try","label":"...","url":"","detail":""}],
"nugget":false,"actionable":false,"nugget_why":""}]}
Every input tweet_id must appear exactly once. Max 3 actions per pick. Empty url/detail ok.
"""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def normalize_action(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    action_type = str(raw.get("type") or "").strip().lower()
    if action_type not in ACTION_TYPES:
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    url = str(raw.get("url") or "").strip()
    detail = str(raw.get("detail") or "").strip()
    out = {"type": action_type, "label": label[:120]}
    if url:
        out["url"] = url[:500]
    if detail:
        out["detail"] = detail[:240]
    return out


def normalize_actions(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    actions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        action = normalize_action(item)
        if action is None:
            continue
        key = (action["type"], action["label"].lower())
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
        if len(actions) >= MAX_ACTIONS:
            break
    return actions


def _collect_urls(*parts: Any) -> list[str]:
    found: list[str] = []
    for part in parts:
        if isinstance(part, list):
            for item in part:
                if isinstance(item, str) and item.startswith("http"):
                    found.append(item)
        elif isinstance(part, str):
            found.extend(_URL_RE.findall(part))
    # de-dupe preserving order
    return list(dict.fromkeys(found))


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def heuristic_actions(
    *,
    text: str = "",
    commentary: str = "",
    urls: list[str] | None = None,
    handle: str = "",
    tag: str = "",
) -> dict[str, Any]:
    """Cheap non-LLM fallback for actions + nugget/actionable flags."""

    blob = "\n".join(part for part in (text, commentary) if part)
    link_pool = _collect_urls(urls or [], text, commentary)
    actions: list[dict[str, str]] = []

    for url in link_pool:
        host = _host(url)
        if "github.com" in host or "huggingface.co" in host or host.endswith("hf.co"):
            actions.append(
                {
                    "type": "try",
                    "label": f"Try {_short_path(url)}",
                    "url": url,
                }
            )
        elif (
            any(
                token in host
                for token in ("docs.", "readthedocs", "arxiv.org", "notion.", "gitbook")
            )
            or "/docs" in url.lower()
        ):
            actions.append({"type": "read", "label": f"Read {_short_path(url)}", "url": url})
        elif any(token in host for token in ("youtube.com", "youtu.be", "vimeo.com")):
            actions.append({"type": "watch", "label": "Watch linked video", "url": url})

    if _WATCH_WORDS.search(blob):
        match = _WATCH_WORDS.search(blob)
        word = match.group(1) if match else "signal"
        actions.append(
            {
                "type": "watch",
                "label": f"Watch {word.lower()} class",
                "detail": "Heuristic watch cue from post language",
            }
        )

    if _STEAL_WORDS.search(blob) or str(tag).upper() == "INSIGHT":
        snippet = (commentary or text or "Reusable pattern").strip().split("\n")[0][:80]
        actions.append({"type": "steal", "label": snippet or "Steal the pattern"})

    follow_match = _HANDLE_RE.search(commentary) or _HANDLE_RE.search(text)
    if follow_match:
        who = follow_match.group(1).removeprefix("@")
        if who.lower() != handle.lower().removeprefix("@"):
            actions.append(
                {
                    "type": "follow",
                    "label": f"Follow @{who}",
                    "url": f"https://x.com/{who}",
                }
            )

    by_type: dict[str, list[dict[str, str]]] = {name: [] for name in ACTION_TYPES}
    for raw in actions:
        action = normalize_action(raw)
        if action is None:
            continue
        bucket = by_type.setdefault(action["type"], [])
        key_label = action["label"].lower()
        if any(existing["label"].lower() == key_label for existing in bucket):
            continue
        bucket.append(action)
    # Prefer a spread of types; try/read/follow beat duplicate watches.
    preference = ("try", "read", "follow", "steal", "watch")
    diversified: list[dict[str, str]] = []
    for action_type in preference:
        bucket = by_type.get(action_type) or []
        if not bucket:
            continue
        diversified.append(bucket.pop(0))
        if len(diversified) >= MAX_ACTIONS:
            break
    if len(diversified) < MAX_ACTIONS:
        for action_type in preference:
            for action in by_type.get(action_type) or []:
                diversified.append(action)
                if len(diversified) >= MAX_ACTIONS:
                    break
            if len(diversified) >= MAX_ACTIONS:
                break
    actions = diversified
    steal_heavy = sum(1 for action in actions if action["type"] == "steal") >= 1
    nugget = steal_heavy or str(tag).upper() == "INSIGHT" or bool(_STEAL_WORDS.search(blob))
    actionable = bool(actions) and any(
        action["type"] in {"try", "read", "steal"} for action in actions
    )
    nugget_why = ""
    if nugget:
        nugget_why = (
            (commentary or text or "Reusable idea for building").strip().split("\n")[0][:800]
        )
    return {
        "actions": actions,
        "nugget": nugget,
        "actionable": actionable,
        "nugget_why": nugget_why,
    }


def _short_path(url: str) -> str:
    try:
        parsed = urlparse(url)
        path = parsed.path.strip("/") or parsed.hostname or url
        return path[:60]
    except ValueError:
        return url[:60]


def parse_enrich_response(payload: Any) -> dict[str, dict[str, Any]]:
    """Map tweet_id -> enriched fields from model JSON."""

    if isinstance(payload, str):
        content = payload.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        payload = json.loads(content)
    if not isinstance(payload, dict):
        return {}
    picks = payload.get("picks") or payload.get("items") or []
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(picks, list):
        return out
    for item in picks:
        if not isinstance(item, dict):
            continue
        tweet_id = str(item.get("tweet_id") or item.get("status_id") or "").strip()
        if not tweet_id:
            continue
        actions = normalize_actions(item.get("actions"))
        out[tweet_id] = {
            "actions": actions,
            "nugget": bool(item.get("nugget")),
            "actionable": bool(item.get("actionable")) or bool(actions),
            "nugget_why": str(item.get("nugget_why") or "").strip()[:800],
        }
    return out


def _tweet_for_pick(doc: dict[str, Any], tweet_id: str) -> dict[str, Any]:
    for tweet in doc.get("tweets") or []:
        if str(tweet.get("id")) == tweet_id:
            return tweet
    return {}


def enrich_pick_fields(
    pick: dict[str, Any],
    *,
    tweet: dict[str, Any] | None = None,
    llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge existing, LLM, and heuristic fields onto a pick (mutates + returns)."""

    tweet = tweet or pick.get("tweet") or {}
    text = str(tweet.get("text") or pick.get("text") or "")
    commentary = str(pick.get("commentary") or pick.get("reason") or "")
    urls = list(tweet.get("urls") or pick.get("urls") or [])
    handle = str(tweet.get("author") or pick.get("handle") or pick.get("author") or "")
    tag = str(pick.get("tag") or "")

    existing = normalize_actions(pick.get("actions"))
    heuristic = heuristic_actions(
        text=text,
        commentary=commentary,
        urls=urls,
        handle=handle,
        tag=tag,
    )
    llm = llm or {}
    llm_actions = normalize_actions(llm.get("actions"))

    actions = existing or llm_actions or heuristic["actions"]
    if existing and llm_actions:
        # Prefer existing labels; fill remaining slots from LLM.
        merged = list(existing)
        seen = {(a["type"], a["label"].lower()) for a in merged}
        for action in llm_actions:
            key = (action["type"], action["label"].lower())
            if key in seen:
                continue
            merged.append(action)
            seen.add(key)
            if len(merged) >= MAX_ACTIONS:
                break
        actions = merged

    nugget = bool(pick.get("nugget"))
    actionable = bool(pick.get("actionable"))
    nugget_why = str(pick.get("nugget_why") or "").strip()
    if llm:
        nugget = nugget or bool(llm.get("nugget"))
        actionable = actionable or bool(llm.get("actionable"))
        nugget_why = nugget_why or str(llm.get("nugget_why") or "").strip()
    if not nugget:
        nugget = bool(heuristic["nugget"])
    if not actionable:
        actionable = bool(heuristic["actionable"]) or bool(actions)
    if not nugget_why and nugget:
        nugget_why = heuristic["nugget_why"]

    pick["actions"] = actions[:MAX_ACTIONS]
    pick["nugget"] = nugget
    pick["actionable"] = actionable
    if nugget_why:
        pick["nugget_why"] = nugget_why[:800]
    elif "nugget_why" in pick and not pick["nugget_why"]:
        pick.pop("nugget_why", None)
    return pick


def enrich_digest_document(
    doc: dict[str, Any],
    *,
    llm_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    llm_by_id = llm_by_id or {}
    for pick in doc.get("picks") or []:
        if not isinstance(pick, dict):
            continue
        tweet_id = str(pick.get("tweet_id") or (pick.get("tweet") or {}).get("id") or "")
        enrich_pick_fields(
            pick,
            tweet=_tweet_for_pick(doc, tweet_id),
            llm=llm_by_id.get(tweet_id),
        )
    return doc


def _call_openrouter(key: str, model: str, compact_picks: list[dict[str, Any]]) -> dict[str, Any]:
    import httpx

    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": "Enrich these picks:\n"
                + json.dumps({"picks": compact_picks}, ensure_ascii=False),
            },
        ],
    }
    with httpx.Client(timeout=120) as client:
        response = client.post(
            OPENROUTER,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/Oni-giri/periscope",
                "X-Title": "Periscope actions enricher",
            },
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    content = body["choices"][0]["message"]["content"]
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return parse_enrich_response(content)


def compact_pick_for_llm(pick: dict[str, Any], tweet: dict[str, Any]) -> dict[str, Any]:
    return {
        "tweet_id": str(pick.get("tweet_id") or tweet.get("id") or ""),
        "handle": str(tweet.get("author") or ""),
        "tag": str(pick.get("tag") or ""),
        "text": str(tweet.get("text") or "")[:700],
        "commentary": str(pick.get("commentary") or pick.get("reason") or "")[:400],
        "urls": list(tweet.get("urls") or [])[:8],
    }


def enrich_file(
    path: Path,
    *,
    model: str = DEFAULT_MODEL,
    use_llm: bool = True,
) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    llm_by_id: dict[str, dict[str, Any]] = {}
    if use_llm:
        load_dotenv(ENV_PATH)
        load_dotenv(Path(".env"))
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if key:
            compact = [
                compact_pick_for_llm(pick, _tweet_for_pick(doc, str(pick.get("tweet_id") or "")))
                for pick in (doc.get("picks") or [])
                if isinstance(pick, dict)
            ]
            compact = [item for item in compact if item["tweet_id"]]
            if compact:
                try:
                    llm_by_id = _call_openrouter(key, model, compact)
                except Exception as exc:  # noqa: BLE001 - soft-fail enrich
                    print(f"enrich_actions LLM soft-fail: {exc}", flush=True)
        else:
            print(
                f"enrich_actions: no OPENROUTER_API_KEY in {ENV_PATH}; using heuristics only",
                flush=True,
            )
    enrich_digest_document(doc, llm_by_id=llm_by_id)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digest", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--llm",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attempt OpenRouter enrichment (soft-fail without key)",
    )
    args = parser.parse_args()
    if not args.digest.is_file():
        print(f"missing digest: {args.digest}", flush=True)
        return 1
    doc = enrich_file(args.digest, model=args.model, use_llm=args.llm)
    action_counts = sum(len(pick.get("actions") or []) for pick in doc.get("picks") or [])
    nuggets = sum(
        1 for pick in doc.get("picks") or [] if pick.get("nugget") or pick.get("actionable")
    )
    print(
        f"enriched {args.digest} picks={len(doc.get('picks') or [])} "
        f"actions={action_counts} nuggets={nuggets}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
