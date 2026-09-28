"""SQLite schema, migrations, and the persistence boundary for Periscope."""

from __future__ import annotations

import email.utils
import json
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from periscope.config import AppConfig, Secrets


class SearchQueryError(ValueError):
    """Raised when SQLite FTS5 rejects a user search expression."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _first(mapping: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _canonical_time(value: Any, *, fallback: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return raw
    else:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def normalize_tweet(raw: Mapping[str, Any], *, fetched_at: str) -> dict[str, Any]:
    """Project source JSON into searchable columns while preserving the source verbatim."""

    tweet_id = str(_first(raw, "id_str", "id", default="")).strip()
    if not tweet_id:
        raise ValueError("Tweet payload has no id or id_str")

    user = raw.get("user")
    user_data = user if isinstance(user, Mapping) else {}
    author_value = _first(raw, "author", "username") or _first(
        user_data, "username", "screen_name", default="unknown"
    )
    author = str(author_value).strip().removeprefix("@").lower() or "unknown"
    text = str(_first(raw, "text", "rawContent", "full_text", default="")).strip()

    quoted = _first(raw, "quotedTweet", "quoted_tweet", "quote")
    quoted_data = quoted if isinstance(quoted, Mapping) else {}
    quoted_id_value = _first(raw, "quoted_id", "quotedTweetId", "quoted_tweet_id")
    quoted_id = str(quoted_id_value or _first(quoted_data, "id_str", "id", default="")) or None

    retweeted = _first(raw, "retweetedTweet", "retweeted_tweet")
    reply_id = _first(
        raw,
        "inReplyToTweetIdStr",
        "inReplyToTweetId",
        "in_reply_to_status_id_str",
        "replying_to_status",
    )
    if retweeted:
        kind = "rt"
    elif reply_id:
        kind = "reply"
    else:
        kind = str(raw.get("kind", "tweet"))
    if kind not in {"tweet", "reply", "rt"}:
        kind = "tweet"

    thread_root = str(
        _first(
            raw,
            "thread_root_id",
            "conversationIdStr",
            "conversationId",
            "conversation_id_str",
            default=tweet_id,
        )
    )

    links = raw.get("urls", raw.get("links", []))
    if not isinstance(links, list):
        links = []
    urls: list[str] = []
    for link in links:
        if isinstance(link, str):
            urls.append(link)
        elif isinstance(link, Mapping):
            url = _first(link, "url", "expanded_url", "tcourl")
            if url:
                urls.append(str(url))

    return {
        "id": tweet_id,
        "author": author,
        "fetched_at": fetched_at,
        "created_at": _canonical_time(
            _first(raw, "created_at", "date"),
            fallback=fetched_at,
        ),
        "raw_json": _json(raw),
        "text": text,
        "thread_root_id": thread_root,
        "quoted_id": quoted_id,
        "urls_json": _json(list(dict.fromkeys(urls))),
        "kind": kind,
    }


MIGRATION_1 = """
CREATE TABLE IF NOT EXISTS accounts (
    handle TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    note TEXT
);

CREATE TABLE IF NOT EXISTS tweets (
    id TEXT PRIMARY KEY,
    author TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL CHECK (json_valid(raw_json)),
    text TEXT NOT NULL,
    thread_root_id TEXT,
    quoted_id TEXT,
    urls_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(urls_json)),
    kind TEXT NOT NULL CHECK (kind IN ('tweet', 'reply', 'rt'))
);
CREATE INDEX IF NOT EXISTS tweets_created_at_idx ON tweets(created_at);
CREATE INDEX IF NOT EXISTS tweets_author_created_idx ON tweets(author, created_at);
CREATE INDEX IF NOT EXISTS tweets_thread_root_idx ON tweets(thread_root_id);

CREATE VIRTUAL TABLE IF NOT EXISTS tweets_fts USING fts5(
    text,
    content='tweets',
    content_rowid='rowid',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS tweets_ai AFTER INSERT ON tweets BEGIN
    INSERT INTO tweets_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS tweets_ad AFTER DELETE ON tweets BEGIN
    INSERT INTO tweets_fts(tweets_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS tweets_au AFTER UPDATE OF text ON tweets BEGIN
    INSERT INTO tweets_fts(tweets_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO tweets_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TABLE IF NOT EXISTS fetch_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL,
    new_items INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS tweet_fetches (
    tweet_id TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
    fetch_log_id INTEGER NOT NULL REFERENCES fetch_log(id) ON DELETE CASCADE,
    PRIMARY KEY (tweet_id, fetch_log_id)
);

CREATE TABLE IF NOT EXISTS clusters (
    id TEXT PRIMARY KEY,
    digest_date TEXT NOT NULL,
    rank INTEGER NOT NULL,
    headline TEXT NOT NULL,
    synthesis TEXT NOT NULL,
    tag TEXT NOT NULL,
    tweet_ids_json TEXT NOT NULL CHECK (json_valid(tweet_ids_json))
);
CREATE INDEX IF NOT EXISTS clusters_digest_rank_idx ON clusters(digest_date, rank);

CREATE TABLE IF NOT EXISTS picks (
    id TEXT PRIMARY KEY,
    digest_date TEXT NOT NULL,
    tweet_id TEXT NOT NULL REFERENCES tweets(id),
    tag TEXT NOT NULL,
    reason TEXT NOT NULL,
    rank INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS picks_digest_rank_idx ON picks(digest_date, rank);

CREATE TABLE IF NOT EXISTS pick_decisions (
    digest_date TEXT NOT NULL,
    tweet_id TEXT NOT NULL REFERENCES tweets(id),
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    tag TEXT,
    reason TEXT NOT NULL,
    PRIMARY KEY (digest_date, tweet_id)
);

CREATE TABLE IF NOT EXISTS digests (
    date TEXT PRIMARY KEY,
    assembled_at TEXT NOT NULL,
    stats_json TEXT NOT NULL CHECK (json_valid(stats_json)),
    rendered_json TEXT NOT NULL CHECK (json_valid(rendered_json))
);

CREATE TABLE IF NOT EXISTS keeps (
    tweet_id TEXT PRIMARY KEY REFERENCES tweets(id) ON DELETE CASCADE,
    kept_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS follow_snapshots (
    handle TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    following_json TEXT NOT NULL CHECK (json_valid(following_json)),
    PRIMARY KEY (handle, taken_at)
);

CREATE TABLE IF NOT EXISTS candidates (
    handle TEXT PRIMARY KEY,
    surfaced_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    cofollow_count INTEGER NOT NULL DEFAULT 0,
    overlap_pct REAL NOT NULL DEFAULT 0,
    stats_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(stats_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'rejected'))
);

CREATE TABLE IF NOT EXISTS topics (
    name TEXT PRIMARY KEY,
    min_faves INTEGER NOT NULL,
    decay_weight REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_spend (
    date TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (date, model)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json))
);
CREATE INDEX IF NOT EXISTS events_kind_at_idx ON events(kind, at);

CREATE TABLE IF NOT EXISTS cluster_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id TEXT NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
    viewed_at TEXT NOT NULL
);
"""


MIGRATION_2 = """
CREATE TABLE IF NOT EXISTS weekly_reports (
    week TEXT PRIMARY KEY,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    assembled_at TEXT NOT NULL,
    stats_json TEXT NOT NULL CHECK (json_valid(stats_json)),
    rendered_json TEXT NOT NULL CHECK (json_valid(rendered_json))
);
CREATE INDEX IF NOT EXISTS weekly_reports_dates_idx
ON weekly_reports(start_date, end_date);
"""


MIGRATION_3 = """
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tweet_id TEXT,
    handle TEXT,
    title TEXT NOT NULL,
    note TEXT,
    action_type TEXT,
    url TEXT,
    source_digest_date TEXT,
    status TEXT NOT NULL DEFAULT 'parked'
        CHECK (status IN ('parked', 'done', 'dropped'))
);
CREATE INDEX IF NOT EXISTS ideas_status_created_idx ON ideas(status, created_at);
CREATE INDEX IF NOT EXISTS ideas_tweet_id_idx ON ideas(tweet_id);
"""


MIGRATION_4 = """
CREATE TABLE IF NOT EXISTS tweet_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT NOT NULL REFERENCES tweets(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL CHECK (action_type IN ('try','read','watch','steal','follow')),
  label TEXT NOT NULL,
  url TEXT,
  detail TEXT,
  source_digest_date TEXT,
  UNIQUE(tweet_id, action_type, label)
);
CREATE INDEX IF NOT EXISTS tweet_actions_type_idx ON tweet_actions(action_type);
CREATE INDEX IF NOT EXISTS tweet_actions_tweet_idx ON tweet_actions(tweet_id);
"""


TWEET_ACTION_TYPES: tuple[str, ...] = ("try", "read", "watch", "steal", "follow")


def _normalize_tweet_action(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    action_type = str(raw.get("type") or raw.get("action_type") or "").strip().lower()
    if action_type not in TWEET_ACTION_TYPES:
        return None
    label = str(raw.get("label") or "").strip()
    if not label:
        return None
    url = str(raw.get("url") or "").strip() or None
    detail = str(raw.get("detail") or "").strip() or None
    return {
        "type": action_type,
        "label": label[:240],
        "url": url[:2000] if url else None,
        "detail": detail[:500] if detail else None,
    }


MIGRATION_5 = """
CREATE TABLE IF NOT EXISTS follow_queue (
  handle TEXT PRIMARY KEY,
  tweet_id TEXT,
  added_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','followed','already','failed')),
  source TEXT,
  last_error TEXT,
  followed_at TEXT
);
CREATE INDEX IF NOT EXISTS follow_queue_status_idx ON follow_queue(status, added_at);
"""


FOLLOW_QUEUE_STATUSES: tuple[str, ...] = ("pending", "followed", "already", "failed")
FOLLOW_MARK_STATUSES: tuple[str, ...] = ("followed", "already", "failed")


MIGRATIONS: tuple[str, ...] = (MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4, MIGRATION_5)


class Database:
    """Small transaction-oriented facade around the single SQLite database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, migration in enumerate(MIGRATIONS, start=1):
                if version in applied:
                    continue
                connection.executescript(migration)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, isoformat()),
                )
                connection.commit()

    def seed(self, config: AppConfig, secrets: Secrets) -> None:
        now = isoformat()
        with self.connect() as connection:
            for handle in config.x.handles:
                connection.execute(
                    "INSERT OR IGNORE INTO accounts(handle, added_at, muted) VALUES (?, ?, 0)",
                    (handle, now),
                )
            for topic in config.topics:
                existing = connection.execute(
                    "SELECT name FROM topics WHERE lower(name) = lower(?) LIMIT 1",
                    (topic.name,),
                ).fetchone()
                if existing is not None:
                    continue
                connection.execute(
                    "INSERT INTO topics(name, min_faves, decay_weight) VALUES (?, ?, ?)",
                    (topic.name, topic.min_faves, topic.decay_weight),
                )
            self._dedupe_topics_casefold(connection)
            flags = {
                "x_is_configured": secrets.x_configured,
                "anthropic_is_configured": secrets.anthropic_configured,
                "telegram_is_configured": secrets.telegram_configured,
                "openrouter_is_configured": secrets.openrouter_configured,
            }
            for key, value in flags.items():
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, "true" if value else "false"),
                )
            connection.commit()

    def start_fetch(self, kind: str, *, now: datetime | None = None) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO fetch_log(started_at, kind) VALUES (?, ?)",
                (isoformat(now), kind),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_fetch(
        self,
        fetch_id: int,
        *,
        new_items: int,
        errors: int = 0,
        note: str | None = None,
        now: datetime | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE fetch_log SET finished_at = ?, new_items = ?, errors = ?, note = ? "
                "WHERE id = ?",
                (isoformat(now), new_items, errors, note, fetch_id),
            )
            connection.commit()

    def store_tweet(
        self,
        raw: Mapping[str, Any],
        *,
        fetch_id: int,
        fetched_at: datetime | None = None,
    ) -> bool:
        fetched = isoformat(fetched_at)
        row = normalize_tweet(raw, fetched_at=fetched)
        with self.connect() as connection:
            existed = connection.execute(
                "SELECT 1 FROM tweets WHERE id = ?", (row["id"],)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO tweets(
                    id, author, fetched_at, created_at, raw_json, text,
                    thread_root_id, quoted_id, urls_json, kind
                ) VALUES (
                    :id, :author, :fetched_at, :created_at, :raw_json, :text,
                    :thread_root_id, :quoted_id, :urls_json, :kind
                )
                ON CONFLICT(id) DO UPDATE SET
                    author = excluded.author,
                    fetched_at = excluded.fetched_at,
                    created_at = excluded.created_at,
                    raw_json = excluded.raw_json,
                    text = excluded.text,
                    thread_root_id = excluded.thread_root_id,
                    quoted_id = excluded.quoted_id,
                    urls_json = excluded.urls_json,
                    kind = excluded.kind
                """,
                row,
            )
            connection.execute(
                "INSERT OR IGNORE INTO tweet_fetches(tweet_id, fetch_log_id) VALUES (?, ?)",
                (row["id"], fetch_id),
            )
            connection.commit()
        return existed is None

    def get_tweet(self, tweet_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tweets WHERE id = ?", (str(tweet_id),)
            ).fetchone()
        return self._tweet_row(row) if row else None

    def get_tweets(self, tweet_ids: Sequence[str]) -> list[dict[str, Any]]:
        if not tweet_ids:
            return []
        placeholders = ",".join("?" for _ in tweet_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tweets WHERE id IN ({placeholders})",  # noqa: S608
                tuple(str(item) for item in tweet_ids),
            ).fetchall()
        by_id = {str(row["id"]): self._tweet_row(row) for row in rows}
        return [by_id[str(tweet_id)] for tweet_id in tweet_ids if str(tweet_id) in by_id]

    def tweets_for_digest(self, digest_date: date) -> list[dict[str, Any]]:
        target = digest_date.isoformat()
        with self.connect() as connection:
            previous = connection.execute(
                "SELECT assembled_at FROM digests WHERE date < ? ORDER BY date DESC LIMIT 1",
                (target,),
            ).fetchone()
            if previous:
                rows = connection.execute(
                    "SELECT * FROM tweets WHERE created_at > ? AND substr(created_at, 1, 10) <= ? "
                    "ORDER BY created_at, id",
                    (previous["assembled_at"], target),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tweets WHERE substr(created_at, 1, 10) <= ? "
                    "ORDER BY created_at, id",
                    (target,),
                ).fetchall()
        return [self._tweet_row(row) for row in rows]

    def search_archive(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT tweets.*
                FROM tweets_fts
                JOIN tweets ON tweets.rowid = tweets_fts.rowid
                WHERE tweets_fts MATCH ?
                ORDER BY bm25(tweets_fts), tweets.created_at DESC
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        return [self._tweet_row(row) for row in rows]

    @staticmethod
    def _tweet_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["raw"] = json.loads(result.pop("raw_json"))
        result["urls"] = json.loads(result.pop("urls_json"))
        raw = result["raw"]
        if isinstance(raw, dict):
            avatar = raw.get("avatar") or raw.get("author_avatar")
            if avatar:
                result["avatar"] = str(avatar)
            media = raw.get("media") or raw.get("image_urls") or []
            if isinstance(media, list) and media:
                result["media"] = [str(item) for item in media if item]
            quoted = raw.get("quoted_tweet") or raw.get("quotedTweet") or raw.get("quote")
            if isinstance(quoted, dict) and (quoted.get("text") or quoted.get("id")):
                result["quoted"] = {
                    "id": str(quoted.get("id") or quoted.get("id_str") or result.get("quoted_id") or ""),
                    "author": str(
                        quoted.get("author")
                        or (quoted.get("user") or {}).get("screen_name")
                        or (quoted.get("user") or {}).get("username")
                        or ""
                    ).removeprefix("@"),
                    "text": str(quoted.get("text") or quoted.get("full_text") or ""),
                }
            tweet_url = None
            for url in result.get("urls") or []:
                if "/status/" in str(url):
                    tweet_url = str(url)
                    break
            if not tweet_url and result.get("author") and result.get("id"):
                tweet_url = f"https://x.com/{result['author']}/status/{result['id']}"
            if tweet_url:
                result["tweet_url"] = tweet_url
        return result

    def replace_digest(
        self,
        *,
        digest_date: date,
        assembled_at: datetime,
        clusters: Sequence[Mapping[str, Any]],
        picks: Sequence[Mapping[str, Any]],
        decisions: Sequence[Mapping[str, Any]],
        stats: Mapping[str, Any],
        rendered: Mapping[str, Any],
    ) -> None:
        target = digest_date.isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT assembled_at FROM digests WHERE date = ?", (target,)
            ).fetchone()
            preserved_assembled_at = (
                str(existing["assembled_at"]) if existing else isoformat(assembled_at)
            )
            rendered_payload: dict[str, Any] = dict(rendered)
            if "assembled_at" in rendered_payload:
                rendered_payload["assembled_at"] = preserved_assembled_at
            connection.execute("DELETE FROM picks WHERE digest_date = ?", (target,))
            connection.execute("DELETE FROM clusters WHERE digest_date = ?", (target,))
            connection.execute("DELETE FROM pick_decisions WHERE digest_date = ?", (target,))
            for cluster in clusters:
                connection.execute(
                    """
                    INSERT INTO clusters(
                        id, digest_date, rank, headline, synthesis, tag, tweet_ids_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cluster["id"],
                        target,
                        cluster["rank"],
                        cluster["headline"],
                        cluster["synthesis"],
                        cluster["tag"],
                        _json(cluster["tweet_ids"]),
                    ),
                )
            for pick in picks:
                connection.execute(
                    "INSERT INTO picks(id, digest_date, tweet_id, tag, reason, rank) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        pick["id"],
                        target,
                        pick["tweet_id"],
                        pick["tag"],
                        pick["reason"],
                        pick["rank"],
                    ),
                )
            for decision in decisions:
                connection.execute(
                    "INSERT INTO pick_decisions("
                    "digest_date, tweet_id, selected, tag, reason"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        target,
                        decision["tweet_id"],
                        int(bool(decision["selected"])),
                        decision.get("tag"),
                        decision["reason"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO digests(date, assembled_at, stats_json, rendered_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    assembled_at = digests.assembled_at,
                    stats_json = excluded.stats_json,
                    rendered_json = excluded.rendered_json
                """,
                (
                    target,
                    preserved_assembled_at,
                    _json(stats),
                    _json(rendered_payload),
                ),
            )
            connection.commit()

    def get_digest(self, digest_date: date | str) -> dict[str, Any] | None:
        target = digest_date.isoformat() if isinstance(digest_date, date) else digest_date
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM digests WHERE date = ?", (target,)).fetchone()
        if row is None:
            return None
        return {
            "date": row["date"],
            "assembled_at": row["assembled_at"],
            "stats": json.loads(row["stats_json"]),
            "rendered": json.loads(row["rendered_json"]),
        }

    def record_llm_spend(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        usd: float,
        at: datetime | None = None,
    ) -> None:
        day = (at or utc_now()).astimezone(UTC).date().isoformat()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO llm_spend(date, model, input_tokens, output_tokens, usd)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date, model) DO UPDATE SET
                    input_tokens = input_tokens + excluded.input_tokens,
                    output_tokens = output_tokens + excluded.output_tokens,
                    usd = usd + excluded.usd
                """,
                (day, model, input_tokens, output_tokens, usd),
            )
            connection.commit()

    def record_event(self, kind: str, payload: Mapping[str, Any] | None = None) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(at, kind, payload_json) VALUES (?, ?, ?)",
                (isoformat(), kind, _json(payload or {})),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def record_job_failure(self, job: str, message: str) -> bool:
        """Record every failure but request at most one alert per UTC day and job."""

        today = utc_now().date().isoformat()
        with self.connect() as connection:
            already_alerted = connection.execute(
                "SELECT 1 FROM events WHERE kind = 'job_failure' "
                "AND substr(at, 1, 10) = ? "
                "AND json_extract(payload_json, '$.job') = ? "
                "AND json_extract(payload_json, '$.alerted') = 1 LIMIT 1",
                (today, job),
            ).fetchone()
            should_alert = already_alerted is None
            connection.execute(
                "INSERT INTO events(at, kind, payload_json) VALUES (?, 'job_failure', ?)",
                (
                    isoformat(),
                    _json({"job": job, "message": message, "alerted": should_alert}),
                ),
            )
            connection.commit()
        return should_alert

    def open_cookie_incident(self) -> bool:
        """Open one cookie incident, returning True only when a new alert is needed."""

        with self.connect() as connection:
            latest = connection.execute(
                "SELECT kind FROM events WHERE kind IN ('cookie_dead', 'cookie_recovered') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest and latest["kind"] == "cookie_dead":
                return False
            connection.execute(
                "INSERT INTO events(at, kind, payload_json) VALUES (?, 'cookie_dead', '{}')",
                (isoformat(),),
            )
            connection.commit()
            return True

    def recover_cookie_incident(self) -> bool:
        with self.connect() as connection:
            latest = connection.execute(
                "SELECT kind FROM events WHERE kind IN ('cookie_dead', 'cookie_recovered') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not latest or latest["kind"] != "cookie_dead":
                return False
            connection.execute(
                "INSERT INTO events(at, kind, payload_json) VALUES (?, 'cookie_recovered', '{}')",
                (isoformat(),),
            )
            connection.commit()
            return True

    def list_digest_dates(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT date FROM digests ORDER BY date DESC").fetchall()
        return [str(row["date"]) for row in rows]

    def latest_digest(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM digests ORDER BY date DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {
            "date": row["date"],
            "assembled_at": row["assembled_at"],
            "stats": json.loads(row["stats_json"]),
            "rendered": json.loads(row["rendered_json"]),
        }

    def get_cluster(self, cluster_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM clusters WHERE id = ?", (cluster_id,)
            ).fetchone()
        if row is None:
            return None
        tweet_ids = [str(item) for item in json.loads(row["tweet_ids_json"])]
        result = dict(row)
        result.pop("tweet_ids_json")
        result["tweet_ids"] = tweet_ids
        result["tweets"] = self.get_tweets(tweet_ids)
        return result

    def record_cluster_view(
        self,
        cluster_id: str,
        *,
        viewed_at: datetime | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO cluster_views(cluster_id, viewed_at) VALUES (?, ?)",
                (cluster_id, isoformat(viewed_at)),
            )
            connection.commit()

    def toggle_keep(self, tweet_id: str) -> bool:
        with self.connect() as connection:
            if (
                connection.execute("SELECT 1 FROM tweets WHERE id = ?", (str(tweet_id),)).fetchone()
                is None
            ):
                raise KeyError(tweet_id)
            kept = connection.execute(
                "SELECT 1 FROM keeps WHERE tweet_id = ?", (str(tweet_id),)
            ).fetchone()
            if kept:
                connection.execute("DELETE FROM keeps WHERE tweet_id = ?", (str(tweet_id),))
                result = False
            else:
                connection.execute(
                    "INSERT INTO keeps(tweet_id, kept_at) VALUES (?, ?)",
                    (str(tweet_id), isoformat()),
                )
                result = True
            connection.commit()
        return result

    def is_kept(self, tweet_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM keeps WHERE tweet_id = ?", (str(tweet_id),)
            ).fetchone()
        return row is not None

    def keep_tweet(self, tweet_id: str, *, now: datetime | None = None) -> bool:
        """Keep a tweet if it is not already kept. Never un-keeps."""
        with self.connect() as connection:
            if (
                connection.execute("SELECT 1 FROM tweets WHERE id = ?", (str(tweet_id),)).fetchone()
                is None
            ):
                raise KeyError(tweet_id)
            cursor = connection.execute(
                "INSERT OR IGNORE INTO keeps(tweet_id, kept_at) VALUES (?, ?)",
                (str(tweet_id), isoformat(now)),
            )
            connection.commit()
            return cursor.rowcount > 0

    def replace_tweet_actions(
        self,
        tweet_id: str,
        actions: Sequence[Mapping[str, Any]],
        digest_date: str | date | None = None,
    ) -> int:
        tweet_id = str(tweet_id)
        source_date = (
            digest_date.isoformat()
            if isinstance(digest_date, date)
            else (str(digest_date) if digest_date else None)
        )
        cleaned: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in actions:
            item = _normalize_tweet_action(raw)
            if item is None:
                continue
            key = (item["type"], item["label"].lower())
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(item)
        with self.connect() as connection:
            connection.execute("DELETE FROM tweet_actions WHERE tweet_id = ?", (tweet_id,))
            for item in cleaned:
                connection.execute(
                    """
                    INSERT INTO tweet_actions(
                        tweet_id, action_type, label, url, detail, source_digest_date
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tweet_id,
                        item["type"],
                        item["label"],
                        item["url"],
                        item["detail"],
                        source_date,
                    ),
                )
            connection.commit()
        return len(cleaned)

    def list_tweet_actions(self, tweet_id: str) -> list[dict[str, Any]]:
        return self._actions_for_tweets([str(tweet_id)]).get(str(tweet_id), [])

    def _actions_for_tweets(self, tweet_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
        ids = [str(item) for item in tweet_ids if str(item)]
        by_id: dict[str, list[dict[str, Any]]] = {tweet_id: [] for tweet_id in ids}
        if not ids:
            return by_id
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT tweet_id, action_type, label, url, detail FROM tweet_actions "
                f"WHERE tweet_id IN ({placeholders}) ORDER BY id",  # noqa: S608
                tuple(ids),
            ).fetchall()
        for row in rows:
            by_id.setdefault(str(row["tweet_id"]), []).append(
                {
                    "type": row["action_type"],
                    "label": row["label"],
                    "url": row["url"],
                    "detail": row["detail"],
                }
            )
        return by_id

    def park_idea(
        self,
        *,
        title: str,
        tweet_id: str | None = None,
        handle: str | None = None,
        note: str | None = None,
        action_type: str | None = None,
        url: str | None = None,
        source_digest_date: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        title = str(title or "").strip()
        if not title:
            raise ValueError("Idea title is required")
        stamped = isoformat(created_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ideas(
                    created_at, tweet_id, handle, title, note,
                    action_type, url, source_digest_date, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'parked')
                """,
                (
                    stamped,
                    str(tweet_id) if tweet_id else None,
                    (handle or "").removeprefix("@").lower() or None,
                    title[:240],
                    (note or "").strip()[:500] or None,
                    (action_type or "").strip().lower() or None,
                    (url or "").strip() or None,
                    source_digest_date,
                ),
            )
            idea_id = int(cursor.lastrowid)
            connection.commit()
            row = connection.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
        return dict(row)

    def list_ideas(
        self,
        *,
        status: str | None = "parked",
        stale_days: int | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if status:
            conditions.append("status = ?")
            parameters.append(status)
        if stale_days is not None:
            cutoff = (now or utc_now()).astimezone(UTC) - timedelta(days=stale_days)
            conditions.append("created_at <= ?")
            parameters.append(isoformat(cutoff))
            if status is None:
                conditions.append("status = 'parked'")
            elif status != "parked":
                # stale only meaningful for parked ideas
                conditions.append("status = 'parked'")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ideas {where} ORDER BY created_at DESC, id DESC",
                tuple(parameters),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_idea(self, idea_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ideas WHERE id = ?", (int(idea_id),)).fetchone()
        return dict(row) if row else None

    def set_idea_status(self, idea_id: int, status: str) -> dict[str, Any] | None:
        if status not in {"parked", "done", "dropped"}:
            raise ValueError(f"Invalid idea status: {status}")
        with self.connect() as connection:
            connection.execute(
                "UPDATE ideas SET status = ? WHERE id = ?",
                (status, int(idea_id)),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM ideas WHERE id = ?", (int(idea_id),)).fetchone()
        return dict(row) if row else None

    def update_idea_note(self, idea_id: int, note: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE ideas SET note = ? WHERE id = ?",
                ((note or "").strip()[:500] or None, int(idea_id)),
            )
            connection.commit()
            row = connection.execute("SELECT * FROM ideas WHERE id = ?", (int(idea_id),)).fetchone()
        return dict(row) if row else None

    def enqueue_follow(
        self,
        handle: str,
        tweet_id: str | None = None,
        source: str = "today",
    ) -> dict[str, Any]:
        canonical = str(handle or "").strip().removeprefix("@").lower()
        if not canonical:
            raise ValueError("Follow handle cannot be empty")
        tweet_id_value = str(tweet_id).strip() if tweet_id else None
        source_value = str(source or "today").strip() or "today"
        now = isoformat()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM follow_queue WHERE handle = ?",
                (canonical,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO follow_queue(
                        handle, tweet_id, added_at, status, source
                    ) VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (canonical, tweet_id_value, now, source_value),
                )
            elif existing["status"] == "failed":
                connection.execute(
                    """
                    UPDATE follow_queue
                    SET tweet_id = COALESCE(?, tweet_id),
                        added_at = ?,
                        status = 'pending',
                        source = COALESCE(?, source),
                        last_error = NULL,
                        followed_at = NULL
                    WHERE handle = ?
                    """,
                    (tweet_id_value, now, source_value, canonical),
                )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM follow_queue WHERE handle = ?",
                (canonical,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_pending_follows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM follow_queue WHERE status = 'pending' ORDER BY added_at, handle"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_follow_queue(
        self,
        *,
        statuses: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        wanted = tuple(statuses) if statuses else FOLLOW_QUEUE_STATUSES
        invalid = [item for item in wanted if item not in FOLLOW_QUEUE_STATUSES]
        if invalid:
            raise ValueError(f"Invalid follow statuses: {', '.join(invalid)}")
        placeholders = ",".join("?" for _ in wanted)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM follow_queue
                WHERE status IN ({placeholders})
                ORDER BY
                  CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'failed' THEN 1
                    ELSE 2
                  END,
                  added_at DESC,
                  handle
                """,
                wanted,
            ).fetchall()
        return [dict(row) for row in rows]

    def dismiss_follow(self, handle: str) -> bool:
        """Remove a follow proposition from the queue (no X call)."""

        canonical = str(handle or "").strip().removeprefix("@").lower()
        if not canonical:
            return False
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM follow_queue WHERE handle = ?",
                (canonical,),
            )
            connection.commit()
        return cursor.rowcount > 0

    def mark_follow(
        self,
        handle: str,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in FOLLOW_MARK_STATUSES:
            raise ValueError(f"Invalid follow status: {status}")
        canonical = str(handle or "").strip().removeprefix("@").lower()
        if not canonical:
            raise ValueError("Follow handle cannot be empty")
        now = isoformat()
        followed_at = now if status in {"followed", "already"} else None
        last_error = None
        if status == "failed" and error:
            last_error = str(error).strip()[:500] or None
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM follow_queue WHERE handle = ?",
                (canonical,),
            ).fetchone()
            if existing is None:
                return None
            connection.execute(
                """
                UPDATE follow_queue
                SET status = ?, last_error = ?, followed_at = ?
                WHERE handle = ?
                """,
                (status, last_error, followed_at, canonical),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM follow_queue WHERE handle = ?",
                (canonical,),
            ).fetchone()
        return dict(row) if row else None

    def queued_follow_handles(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT handle FROM follow_queue WHERE status IN ('pending', 'followed', 'already')"
            ).fetchall()
        return {str(row["handle"]) for row in rows}

    def feed_batches(
        self,
        *,
        account: str | None = None,
        kept_only: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            latest = connection.execute(
                "SELECT assembled_at FROM digests ORDER BY date DESC LIMIT 1"
            ).fetchone()
            boundary = str(latest["assembled_at"]) if latest else ""
            conditions = ["fetch_log.started_at > ?"]
            parameters: list[Any] = [boundary]
            if account:
                conditions.append("tweets.author = ?")
                parameters.append(account.removeprefix("@").lower())
            if kept_only:
                conditions.append("keeps.tweet_id IS NOT NULL")
            where = " AND ".join(conditions)
            limit_sql = ""
            if limit is not None and int(limit) > 0:
                limit_sql = f" LIMIT {int(limit)}"
            rows = connection.execute(
                f"""
                SELECT
                    tweets.*,
                    CASE WHEN keeps.tweet_id IS NULL THEN 0 ELSE 1 END AS kept,
                    fetch_log.id AS fetch_id,
                    fetch_log.started_at AS fetch_started_at,
                    fetch_log.finished_at AS fetch_finished_at
                FROM fetch_log
                JOIN tweet_fetches ON tweet_fetches.fetch_log_id = fetch_log.id
                JOIN tweets ON tweets.id = tweet_fetches.tweet_id
                LEFT JOIN keeps ON keeps.tweet_id = tweets.id
                WHERE {where}
                ORDER BY fetch_log.started_at DESC, tweets.created_at DESC, tweets.id
                {limit_sql}
                """,
                tuple(parameters),
            ).fetchall()

        batches: list[dict[str, Any]] = []
        by_fetch: dict[int, dict[str, Any]] = {}
        for row in rows:
            fetch_id = int(row["fetch_id"])
            batch = by_fetch.get(fetch_id)
            if batch is None:
                batch = {
                    "id": fetch_id,
                    "started_at": row["fetch_started_at"],
                    "finished_at": row["fetch_finished_at"],
                    "tweets": [],
                }
                by_fetch[fetch_id] = batch
                batches.append(batch)
            tweet = self._tweet_row(row)
            for key in ("fetch_id", "fetch_started_at", "fetch_finished_at"):
                tweet.pop(key, None)
            tweet["kept"] = bool(tweet["kept"])
            batch["tweets"].append(tweet)
        return batches

    def count_feed_posts(
        self,
        *,
        account: str | None = None,
        kept_only: bool = False,
    ) -> int:
        with self.connect() as connection:
            latest = connection.execute(
                "SELECT assembled_at FROM digests ORDER BY date DESC LIMIT 1"
            ).fetchone()
            boundary = str(latest["assembled_at"]) if latest else ""
            conditions = ["fetch_log.started_at > ?"]
            parameters: list[Any] = [boundary]
            if account:
                conditions.append("tweets.author = ?")
                parameters.append(account.removeprefix("@").lower())
            if kept_only:
                conditions.append("keeps.tweet_id IS NOT NULL")
            where = " AND ".join(conditions)
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM fetch_log
                JOIN tweet_fetches ON tweet_fetches.fetch_log_id = fetch_log.id
                JOIN tweets ON tweets.id = tweet_fetches.tweet_id
                LEFT JOIN keeps ON keeps.tweet_id = tweets.id
                WHERE {where}
                """,
                tuple(parameters),
            ).fetchone()
        return int(row["count"]) if row else 0

    def archive_page(
        self,
        *,
        query: str = "",
        from_date: str | None = None,
        to_date: str | None = None,
        account: str | None = None,
        topic: str | None = None,
        kept: bool | None = None,
        action: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        joins: list[str] = []
        conditions: list[str] = []
        parameters: list[Any] = []
        query = query.strip()
        if query:
            joins.append("JOIN tweets_fts ON tweets_fts.rowid = tweets.rowid")
            conditions.append("tweets_fts MATCH ?")
            parameters.append(query)
        if from_date:
            conditions.append("substr(tweets.created_at, 1, 10) >= ?")
            parameters.append(from_date)
        if to_date:
            conditions.append("substr(tweets.created_at, 1, 10) <= ?")
            parameters.append(to_date)
        if account:
            conditions.append("tweets.author = ?")
            parameters.append(account.removeprefix("@").lower())
        if topic:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM clusters, json_each(clusters.tweet_ids_json) AS cluster_tweet "
                "WHERE CAST(cluster_tweet.value AS TEXT) = tweets.id "
                "AND lower(clusters.tag) = lower(?)"
                ")"
            )
            parameters.append(topic)
        if kept is not None:
            operator = "" if kept else "NOT "
            conditions.append(
                f"{operator}EXISTS (SELECT 1 FROM keeps WHERE keeps.tweet_id = tweets.id)"
            )
        action_filter = (action or "").strip().lower()
        if action_filter == "any":
            conditions.append(
                "EXISTS (SELECT 1 FROM tweet_actions WHERE tweet_actions.tweet_id = tweets.id)"
            )
        elif action_filter in TWEET_ACTION_TYPES:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM tweet_actions "
                "WHERE tweet_actions.tweet_id = tweets.id AND tweet_actions.action_type = ?"
                ")"
            )
            parameters.append(action_filter)

        join_sql = " ".join(joins)
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order_sql = (
            "ORDER BY bm25(tweets_fts), tweets.created_at DESC"
            if query
            else "ORDER BY tweets.created_at DESC, tweets.id DESC"
        )
        try:
            with self.connect() as connection:
                total_row = connection.execute(
                    f"SELECT COUNT(DISTINCT tweets.id) AS total FROM tweets {join_sql} {where_sql}",
                    tuple(parameters),
                ).fetchone()
                rows = connection.execute(
                    f"SELECT tweets.* FROM tweets {join_sql} {where_sql} "
                    f"{order_sql} LIMIT ? OFFSET ?",
                    (*parameters, page_size, (page - 1) * page_size),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if query:
                raise SearchQueryError(str(exc)) from exc
            raise
        assert total_row is not None
        items = [self._tweet_row(row) for row in rows]
        actions_by_id = self._actions_for_tweets([str(item["id"]) for item in items])
        for item in items:
            item["actions"] = actions_by_id.get(str(item["id"]), [])
        return {
            "items": items,
            "total": int(total_row["total"]),
            "page": page,
            "page_size": page_size,
        }

    def archive_facets(self) -> dict[str, Any]:
        with self.connect() as connection:
            accounts = connection.execute(
                "SELECT author AS value FROM tweets "
                "UNION SELECT handle AS value FROM accounts ORDER BY value"
            ).fetchall()
            topics = connection.execute(
                "SELECT name AS value FROM topics "
                "UNION SELECT tag AS value FROM clusters ORDER BY value"
            ).fetchall()
            action_rows = connection.execute(
                "SELECT action_type AS value, COUNT(DISTINCT tweet_id) AS n "
                "FROM tweet_actions GROUP BY action_type"
            ).fetchall()
            any_row = connection.execute(
                "SELECT COUNT(DISTINCT tweet_id) AS n FROM tweet_actions"
            ).fetchone()
        action_counts = {str(row["value"]): int(row["n"]) for row in action_rows if row["value"]}
        return {
            "accounts": [str(row["value"]) for row in accounts if row["value"]],
            "topics": [str(row["value"]) for row in topics if row["value"]],
            "actions": action_counts,
            "action_any": int(any_row["n"]) if any_row is not None else 0,
        }

    def list_accounts(self, *, include_muted: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM accounts"
        if not include_muted:
            query += " WHERE muted = 0"
        query += " ORDER BY handle"
        with self.connect() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def store_follow_snapshot(
        self,
        handle: str,
        following: Sequence[str],
        *,
        taken_at: datetime | None = None,
    ) -> None:
        canonical = sorted(
            {str(item).strip().removeprefix("@").lower() for item in following if str(item).strip()}
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO follow_snapshots(handle, taken_at, following_json)
                VALUES (?, ?, ?)
                ON CONFLICT(handle, taken_at) DO UPDATE SET
                    following_json = excluded.following_json
                """,
                (handle.removeprefix("@").lower(), isoformat(taken_at), _json(canonical)),
            )
            connection.commit()

    def latest_follow_snapshot(
        self,
        handle: str,
        *,
        before: datetime | None = None,
    ) -> dict[str, Any] | None:
        parameters: list[Any] = [handle.removeprefix("@").lower()]
        condition = ""
        if before is not None:
            condition = " AND taken_at < ?"
            parameters.append(isoformat(before))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM follow_snapshots WHERE handle = ?"
                f"{condition} ORDER BY taken_at DESC LIMIT 1",
                tuple(parameters),
            ).fetchone()
        if row is None:
            return None
        return {
            "handle": str(row["handle"]),
            "taken_at": str(row["taken_at"]),
            "following": [str(item) for item in json.loads(row["following_json"])],
        }

    def tweets_between(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tweets WHERE substr(created_at, 1, 10) BETWEEN ? AND ? "
                "ORDER BY created_at, id",
                (start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [self._tweet_row(row) for row in rows]

    def upsert_candidate(
        self,
        *,
        handle: str,
        reason: str,
        cofollow_count: int,
        overlap_pct: float,
        stats: Mapping[str, Any],
        surfaced_at: datetime | None = None,
        suppression_days: int = 90,
    ) -> bool:
        canonical = handle.strip().removeprefix("@").lower()
        if not canonical:
            raise ValueError("Candidate handle cannot be empty")
        surfaced = surfaced_at or utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT status, surfaced_at FROM candidates WHERE handle = ?",
                (canonical,),
            ).fetchone()
            if existing and existing["status"] == "accepted":
                return False
            if existing and existing["status"] == "rejected":
                rejected_at = _canonical_time(existing["surfaced_at"], fallback="")
                try:
                    rejected = datetime.fromisoformat(rejected_at)
                except ValueError:
                    rejected = surfaced
                if (surfaced.astimezone(UTC) - rejected.astimezone(UTC)).days < suppression_days:
                    return False
            connection.execute(
                """
                INSERT INTO candidates(
                    handle, surfaced_at, reason, cofollow_count,
                    overlap_pct, stats_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(handle) DO UPDATE SET
                    surfaced_at = excluded.surfaced_at,
                    reason = excluded.reason,
                    cofollow_count = excluded.cofollow_count,
                    overlap_pct = excluded.overlap_pct,
                    stats_json = excluded.stats_json,
                    status = 'pending'
                """,
                (
                    canonical,
                    isoformat(surfaced),
                    reason,
                    max(0, int(cofollow_count)),
                    max(0.0, min(float(overlap_pct), 100.0)),
                    _json(stats),
                ),
            )
            connection.commit()
        return True

    def list_candidates(self, *, status: str = "pending") -> list[dict[str, Any]]:
        if status not in {"pending", "accepted", "rejected"}:
            raise ValueError(f"Unknown candidate status: {status}")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM candidates WHERE status = ? "
                "ORDER BY cofollow_count DESC, overlap_pct DESC, surfaced_at DESC, handle",
                (status,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["stats"] = json.loads(item.pop("stats_json"))
            result.append(item)
        return result

    def get_candidate(self, handle: str) -> dict[str, Any] | None:
        canonical = handle.removeprefix("@").lower()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE handle = ?", (canonical,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["stats"] = json.loads(result.pop("stats_json"))
        return result

    def review_candidate(self, handle: str, status: str) -> bool:
        if status not in {"accepted", "rejected"}:
            raise ValueError("Candidate review must be accepted or rejected")
        canonical = handle.removeprefix("@").lower()
        now = isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM candidates WHERE handle = ?", (canonical,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                connection.rollback()
                return False
            connection.execute(
                "UPDATE candidates SET status = ?, surfaced_at = ? WHERE handle = ?",
                (status, now, canonical),
            )
            if status == "accepted":
                connection.execute(
                    "INSERT OR IGNORE INTO accounts(handle, added_at, muted) VALUES (?, ?, 0)",
                    (canonical, now),
                )
            connection.commit()
        return True

    def replace_weekly_report(
        self,
        *,
        week: str,
        start_date: date,
        end_date: date,
        assembled_at: datetime,
        stats: Mapping[str, Any],
        rendered: Mapping[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO weekly_reports(
                    week, start_date, end_date, assembled_at, stats_json, rendered_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(week) DO UPDATE SET
                    start_date = excluded.start_date,
                    end_date = excluded.end_date,
                    assembled_at = excluded.assembled_at,
                    stats_json = excluded.stats_json,
                    rendered_json = excluded.rendered_json
                """,
                (
                    week,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    isoformat(assembled_at),
                    _json(stats),
                    _json(rendered),
                ),
            )
            connection.commit()

    def get_weekly_report(self, week: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM weekly_reports WHERE week = ?", (week,)
            ).fetchone()
        if row is None:
            return None
        return {
            "week": str(row["week"]),
            "start_date": str(row["start_date"]),
            "end_date": str(row["end_date"]),
            "assembled_at": str(row["assembled_at"]),
            "stats": json.loads(row["stats_json"]),
            "rendered": json.loads(row["rendered_json"]),
        }

    def latest_weekly_report(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT week FROM weekly_reports ORDER BY start_date DESC LIMIT 1"
            ).fetchone()
        return self.get_weekly_report(str(row["week"])) if row else None

    def list_weekly_weeks(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT week FROM weekly_reports ORDER BY start_date DESC"
            ).fetchall()
        return [str(row["week"]) for row in rows]

    def weekly_calibration(self, start_date: date, end_date: date) -> dict[str, Any]:
        parameters = (start_date.isoformat(), end_date.isoformat())
        with self.connect() as connection:
            kept_not_picked = connection.execute(
                """
                SELECT tweets.*
                FROM keeps
                JOIN tweets ON tweets.id = keeps.tweet_id
                WHERE substr(keeps.kept_at, 1, 10) BETWEEN ? AND ?
                AND NOT EXISTS (
                    SELECT 1 FROM picks
                    WHERE picks.tweet_id = tweets.id
                    AND picks.digest_date BETWEEN ? AND ?
                )
                ORDER BY keeps.kept_at DESC
                """,
                (*parameters, *parameters),
            ).fetchall()
            selected = connection.execute(
                "SELECT COUNT(*) AS count FROM picks WHERE digest_date BETWEEN ? AND ?",
                parameters,
            ).fetchone()
            kept = connection.execute(
                "SELECT COUNT(*) AS count FROM keeps WHERE substr(kept_at, 1, 10) BETWEEN ? AND ?",
                parameters,
            ).fetchone()
        return {
            "kept_count": int(kept["count"]) if kept else 0,
            "pick_count": int(selected["count"]) if selected else 0,
            "kept_not_picked": [self._tweet_row(row) for row in kept_not_picked],
        }

    def get_settings(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def update_settings(self, values: Mapping[str, str]) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(key), str(value)),
                )
            connection.commit()

    def add_account(self, handle: str, *, note: str | None = None) -> bool:
        canonical = handle.strip().removeprefix("@").lower()
        if not canonical:
            raise ValueError("Account handle cannot be empty")
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO accounts(handle, added_at, muted, note) VALUES (?, ?, 0, ?)",
                (canonical, isoformat(), note),
            )
            connection.commit()
        return cursor.rowcount > 0

    def set_account_muted(self, handle: str, muted: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE accounts SET muted = ? WHERE handle = ?",
                (int(muted), handle.removeprefix("@").lower()),
            )
            connection.commit()
        return cursor.rowcount > 0

    def remove_account(self, handle: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM accounts WHERE handle = ?",
                (handle.removeprefix("@").lower(),),
            )
            connection.commit()
        return cursor.rowcount > 0

    def list_topics(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM topics ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _dedupe_topics_casefold(connection: Any) -> None:
        """Collapse case-only duplicate topic names (keep preferred spelling)."""
        rows = connection.execute(
            "SELECT name, min_faves, decay_weight FROM topics ORDER BY name COLLATE NOCASE"
        ).fetchall()
        winners: dict[str, tuple[str, int, float]] = {}
        for row in rows:
            name = str(row["name"] if hasattr(row, "keys") else row[0])
            min_faves = int(row["min_faves"] if hasattr(row, "keys") else row[1])
            decay = float(row["decay_weight"] if hasattr(row, "keys") else row[2])
            key = name.casefold()
            if key not in winners:
                winners[key] = (name, min_faves, decay)
                continue
            prev_name, prev_faves, prev_decay = winners[key]
            # Prefer Title-ish spellings (more uppercase) when colliding; keep stronger thresholds.
            prefer_new = sum(1 for c in name if c.isupper()) > sum(
                1 for c in prev_name if c.isupper()
            )
            winners[key] = (
                name if prefer_new else prev_name,
                max(prev_faves, min_faves),
                max(prev_decay, decay),
            )
        if len(winners) == len(rows):
            return
        connection.execute("DELETE FROM topics")
        connection.executemany(
            "INSERT INTO topics(name, min_faves, decay_weight) VALUES (?, ?, ?)",
            [
                (name, min_faves, max(0.5, min(decay, 1.0)))
                for name, min_faves, decay in winners.values()
            ],
        )

    def replace_topics(self, topics: Sequence[Mapping[str, Any]]) -> None:
        cleaned: list[tuple[str, int, float]] = []
        seen: set[str] = set()
        for topic in topics:
            name = str(topic.get("name", "")).strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                # Merge thresholds onto the kept spelling.
                for i, (kept, faves, decay) in enumerate(cleaned):
                    if kept.casefold() == key:
                        cleaned[i] = (
                            kept,
                            max(faves, max(0, int(topic.get("min_faves", 0)))),
                            max(
                                decay,
                                max(0.5, min(float(topic.get("decay_weight", 1.0)), 1.0)),
                            ),
                        )
                        break
                continue
            seen.add(key)
            cleaned.append(
                (
                    name,
                    max(0, int(topic.get("min_faves", 0))),
                    max(0.5, min(float(topic.get("decay_weight", 1.0)), 1.0)),
                )
            )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM topics")
            connection.executemany(
                "INSERT INTO topics(name, min_faves, decay_weight) VALUES (?, ?, ?)",
                cleaned,
            )
            connection.commit()

    def recent_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def spend_summary(self) -> dict[str, Any]:
        today = utc_now().date()
        month = today.strftime("%Y-%m")
        with self.connect() as connection:
            today_row = connection.execute(
                "SELECT COALESCE(SUM(usd), 0) AS usd, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens "
                "FROM llm_spend WHERE date = ?",
                (today.isoformat(),),
            ).fetchone()
            month_row = connection.execute(
                "SELECT COALESCE(SUM(usd), 0) AS usd, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens "
                "FROM llm_spend WHERE date LIKE ?",
                (f"{month}-%",),
            ).fetchone()
            tracked = connection.execute(
                "SELECT COUNT(*) AS count FROM llm_spend"
            ).fetchone()
            today_tweets = connection.execute(
                "SELECT COUNT(*) AS count FROM tweets WHERE substr(created_at, 1, 10) = ?",
                (today.isoformat(),),
            ).fetchone()
            all_tweets = connection.execute("SELECT COUNT(*) AS count FROM tweets").fetchone()
        return {
            "today_usd": float(today_row["usd"]) if today_row else 0.0,
            "month_usd": float(month_row["usd"]) if month_row else 0.0,
            "today_input_tokens": int(today_row["input_tokens"]) if today_row else 0,
            "today_output_tokens": int(today_row["output_tokens"]) if today_row else 0,
            "month_input_tokens": int(month_row["input_tokens"]) if month_row else 0,
            "month_output_tokens": int(month_row["output_tokens"]) if month_row else 0,
            "has_tracked_spend": bool(tracked and int(tracked["count"]) > 0),
            "tweets_today": int(today_tweets["count"]) if today_tweets else 0,
            "tweets_all": int(all_tweets["count"]) if all_tweets else 0,
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def apply_topic_decay(self, *, at: date, enabled: bool = True) -> dict[str, float]:
        """Apply at most one 5% decay pass per ISO week, then return current weights."""

        iso_year, iso_week, _ = at.isocalendar()
        week_key = f"{iso_year}-W{iso_week:02d}"
        start = (at - timedelta(days=7)).isoformat()
        with self.connect() as connection:
            last = connection.execute(
                "SELECT value FROM settings WHERE key = 'topic_decay.last_week'"
            ).fetchone()
            if enabled and (last is None or str(last["value"]) != week_key):
                topics = connection.execute("SELECT name, decay_weight FROM topics").fetchall()
                for topic in topics:
                    clusters = connection.execute(
                        "SELECT id FROM clusters WHERE lower(tag) = lower(?) "
                        "AND digest_date BETWEEN ? AND ?",
                        (topic["name"], start, at.isoformat()),
                    ).fetchall()
                    if not clusters:
                        continue
                    placeholders = ",".join("?" for _ in clusters)
                    viewed = connection.execute(
                        f"SELECT 1 FROM cluster_views WHERE cluster_id IN ({placeholders}) LIMIT 1",
                        tuple(str(cluster["id"]) for cluster in clusters),
                    ).fetchone()
                    if viewed is None:
                        connection.execute(
                            "UPDATE topics SET decay_weight = MAX(0.5, decay_weight * 0.95) "
                            "WHERE name = ?",
                            (topic["name"],),
                        )
                connection.execute(
                    "INSERT INTO settings(key, value) VALUES ('topic_decay.last_week', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (week_key,),
                )
                connection.commit()
            rows = connection.execute("SELECT name, decay_weight FROM topics").fetchall()
        return {str(row["name"]): float(row["decay_weight"]) for row in rows}

    def health_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            last_fetch = connection.execute(
                "SELECT * FROM fetch_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cookie_event = connection.execute(
                "SELECT kind, at FROM events "
                "WHERE kind IN ('cookie_dead', 'cookie_recovered') "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            configured = connection.execute(
                "SELECT key, value FROM settings WHERE key LIKE '%_is_configured'"
            ).fetchall()
            spend = connection.execute(
                "SELECT COALESCE(SUM(usd), 0) AS usd FROM llm_spend WHERE date = substr(?, 1, 10)",
                (isoformat(),),
            ).fetchone()
            counts = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
                )
                for table in ("accounts", "tweets", "digests", "candidates")
            }
            journal = connection.execute("PRAGMA journal_mode").fetchone()
        return {
            "status": "ok",
            "database": {
                "journal_mode": str(journal[0]) if journal else "unknown",
                **counts,
            },
            "last_fetch": dict(last_fetch) if last_fetch else None,
            "cookie": {
                "ok": not cookie_event or cookie_event["kind"] != "cookie_dead",
                "last_change_at": cookie_event["at"] if cookie_event else None,
            },
            "configured": {str(row["key"]): row["value"] == "true" for row in configured},
            "llm_spend_today_usd": float(spend["usd"]) if spend else 0.0,
        }

    def count(self, table: str) -> int:
        allowed = {
            "accounts",
            "tweets",
            "fetch_log",
            "clusters",
            "picks",
            "pick_decisions",
            "digests",
            "events",
        }
        if table not in allowed:
            raise ValueError(f"Counting table {table!r} is not allowed")
        with self.connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        assert row is not None
        return int(row["count"])

    def rows(self, query: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
        """Read-only test/diagnostic helper for fixed queries owned by the caller."""

        if not query.lstrip().upper().startswith(("SELECT", "PRAGMA")):
            raise ValueError("Database.rows only accepts SELECT or PRAGMA statements")
        with self.connect() as connection:
            return connection.execute(query, tuple(parameters)).fetchall()
