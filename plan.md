# Periscope — Implementation Plan

Self-hosted, read-only X/Twitter digest system. Scrapes a curated account list,
LLM-clusters tweets into a finite daily digest with verbatim "picks", surfaces
discovery candidates from the social graph, and delivers via web UI, Telegram,
and MCP. Runs on a homelab (Umbrel) box.

---

## 0. Design principles (hard requirements, not suggestions)

1. **Read-only.** No posting, replying, liking on X. The only write ops against
   X are: follow account (from discovery accept) — and it is gated behind
   explicit user action.
2. **Finite consumption.** No infinite scroll anywhere. Every page has a hard
   end. Feed and digest update only when cron fetch runs — no refresh buttons
   on consumption pages, no polling, no websockets pushing new content.
3. **No engagement-metric prominence.** Never render like/RT counts on digest
   or feed items. Counts may appear only in Discovery (candidate stats) and
   internal ranking.
4. **Absolute timestamps only** (e.g. `08:14`, `11 Jul`). Never relative
   ("2m ago").
5. **Local "keep" is a label, not a like.** It writes to SQLite only; never
   syncs to X.
6. **Raw-before-processed.** Every fetch persists raw JSON to SQLite before any
   transformation. Reprocessing must be possible without refetching.
7. **Secrets never in SQLite.** Credentials live in an env file
   (`/data/secrets.env`), chmod 600. DB stores only `is_configured` flags and
   non-secret settings.
8. **Fail loud to Telegram, quiet everywhere else.** Cookie death (401/403 from
   X) triggers one Telegram alert; jobs otherwise degrade gracefully and log.

## 1. Stack (locked — do not substitute)

- Python 3.12, `uv` for env/deps, single `pyproject.toml`
- Scraping: `twscrape` (async). Direct `httpx` GraphQL calls only where
  twscrape lacks an endpoint. **No Playwright in v1.**
- DB: SQLite via `sqlite-utils` + stdlib `sqlite3`. FTS5 for archive search.
  WAL mode. Single file at `/data/periscope.db`.
- LLM: Anthropic API. Cheap tier (clustering, picks, feed labeling):
  `claude-haiku-4-5`. Quality tier (weekly report): `claude-sonnet-4-6`.
  Model names read from config, never hardcoded in logic.
- Web: FastAPI + Jinja2, server-rendered. HTMX for small interactivity
  (keep toggle, discovery accept/reject, settings forms, tab switching).
  **No React, no build step, no node.**
- Telegram: `python-telegram-bot` v21+, webhook-less (polling).
- MCP: official `mcp` Python SDK, stdio + streamable-http transports.
- Scheduling: in-process APScheduler (works inside one Docker container;
  Umbrel apps shouldn't rely on host cron). Jobs must also be invocable as
  CLI: `python -m periscope.jobs.daily`.
- Container: single Dockerfile + `docker-compose.yml` + `umbrel-app.yml`.

## 2. Repo layout

```
periscope/
  pyproject.toml
  Dockerfile
  docker-compose.yml
  umbrel-app.yml
  config.example.toml
  secrets.example.env
  periscope/
    __init__.py
    config.py          # load config.toml + secrets env; typed dataclasses
    db.py              # schema, migrations, helpers, FTS triggers
    xclient.py         # twscrape wrapper: list timeline, threads, user tweets,
                       # following list, search, follow-account write op.
                       # Central 401/403 detection -> cookie_dead event.
    llm.py             # Anthropic client wrapper, JSON-mode helpers, token
                       # spend accounting (writes to llm_spend table)
    cluster.py         # story clustering + dedup + topic decay
    picks.py           # pick selection; loads prompts/picks_prompt.md
    rank.py            # engagement-decay ranking, bait penalty
    discovery.py       # follow-delta snapshots, RT/reply mining,
                       # co-follow weighting, overlap scoring
    render.py          # digest assembly -> digest JSON blob; text renderers
                       # shared by web/telegram/mcp
    jobs/
      daily.py         # fetch -> cluster -> picks -> rank -> digest -> deliver
      weekly.py        # follow deltas, topic search, meta-report
      fetchonly.py     # feed fetch (every 30 min per mock; configurable)
    web/
      app.py           # FastAPI factory, APScheduler startup
      routes/          # today.py feed.py archive.py discovery.py weekly.py
                       # settings.py health.py cluster_detail.py
      templates/       # server-rendered pages (see §6)
      static/periscope.css
    telegram/bot.py    # digest push, alerts, [keep]/[add] buttons
    mcp_server.py      # tools per §8
    prompts/
      picks_prompt.md
      cluster_prompt.md
      weekly_prompt.md
  tests/
```

## 3. SQLite schema (create in db.py, idempotent migrations)

```sql
accounts(handle PK, added_at, muted INT, note)
tweets(id PK, author, fetched_at, created_at, raw_json, text, thread_root_id,
       quoted_id, urls_json, kind)            -- kind: tweet|reply|rt
tweets_fts(text) -- FTS5 external-content table on tweets, triggers on insert
fetch_log(id PK, started_at, finished_at, kind, new_items, errors, note)
clusters(id PK, digest_date, rank, headline, synthesis, tag, tweet_ids_json)
picks(id PK, digest_date, tweet_id, tag, reason, rank)
digests(date PK, assembled_at, stats_json, rendered_json)
keeps(tweet_id PK, kept_at)
follow_snapshots(handle, taken_at, following_json)   -- weekly per account
candidates(handle PK, surfaced_at, reason, cofollow_count, overlap_pct,
           stats_json, status)               -- pending|accepted|rejected
           -- rejected suppressed 90 days (per mock)
topics(name PK, min_faves INT, decay_weight REAL DEFAULT 1.0)
settings(key PK, value)                       -- non-secret runtime settings
llm_spend(date, model, input_tokens, output_tokens, usd)
events(id PK, at, kind, payload_json)         -- cookie_dead, errors, etc.
```

## 4. Config

- `config.toml`: list of handles (seed), topics + thresholds, schedule times,
  picks min/max, clustering aggressiveness, topic-decay toggle, model names,
  feed fetch interval. On first run, seed into DB; thereafter DB settings win
  (Settings UI edits DB, not the file).
- `secrets.env`: `X_AUTH_TOKEN`, `X_CT0`, `ANTHROPIC_API_KEY`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Settings UI writes this file
  (chmod 600) and hot-reloads clients.

## 5. Core pipeline behavior

### 5.1 fetchonly job (every 30 min, configurable)
List timeline via twscrape → new tweets → resolve threads (recursive
self-reply fetch, depth-limited 25) and quoted tweets → store raw. Feed page
groups by fetch batch.

### 5.2 daily job (per schedule, 1–2×/day)
1. Gather tweets since last digest.
2. `cluster.py`: LLM pass (cheap tier) — group into stories. Input: compacted
   tweet list (id, author, text, is_thread). Output JSON:
   `[{headline, synthesis, tweet_ids, tag}]`. Apply topic decay: topics whose
   clusters were never expanded (no cluster_detail views logged) decay 5%/week,
   floor 0.5×, toggleable.
3. `picks.py`: LLM pass with `prompts/picks_prompt.md` (already written —
   preserve the ARTIFACT/INSIGHT/SIGNAL/ALPHA taxonomy, the anonymization
   test, "zero is acceptable", low-follower preference, JSON output with tag +
   ≤10-word reason). Picks bypass topic decay. Log every (tweet, decision,
   reason) for calibration.
4. `rank.py`: order clusters + picks. Engagement-decay: faves normalized by
   author follower count; penalize bait patterns (polls, "thoughts?",
   rage-quote markers). Interleave picks per mock.
5. Assemble digest row (`digests`), render, push Telegram summary with link.

### 5.3 weekly job
1. Snapshot `following` for every curated account; diff vs previous snapshot;
   candidates weighted by co-follow count within the week.
2. RT/reply mining: external accounts RT'd/replied-to ≥3× by list this week.
3. Overlap scoring: candidate's following ∩ union of list followings.
4. Topic searches: `min_faves:{threshold} {topic}` per topic, top N.
5. Meta-report (quality tier): themes, accounts trending up/down (mention
   deltas), suggested adds/drops, **picks-vs-keeps diff** ("you kept N tweets
   the digest didn't pick" — list them; this is the calibration surface).

## 6. Web UI

The interface uses a restrained Swiss style, CSS-variable light/dark themes,
and finite responsive layouts.

Implementation rules:
- Keep CSS-variable theming (`--bg/--fg/--accent`, `data-theme`,
  `data-accent`) in `static/periscope.css`.
- Split into Jinja: `base.html` (sidebar shell) + one template per page.
- Preserve mobile media queries as-is.
- Sidebar: nav (Today, Feed, Archive, Discovery+badge, Weekly, Settings),
  next-fetch time, **add a cookie-status dot** (green/red) visible on every
  page.
  Discovery badge hidden entirely when queue empty (never show "0").
- Cluster detail: add explicit "← Back to digest" link top and bottom
  for reliable navigation.
- Routes:
  - `GET /` and `/digest/{date}` — today + archive digests, prev/next + calendar
  - `GET /cluster/{id}` — detail (log the view for topic decay)
  - `GET /feed` — batches since last digest; HTMX `POST /keep/{tweet_id}` toggle
  - `GET /archive?q=&from=&to=&account=&topic=&kept=` — FTS5, paginated
  - `GET /discovery`, `POST /discovery/{handle}/accept|reject` — accept calls
    xclient follow + inserts into accounts
  - `GET /weekly/{week}`
  - `GET|POST /settings` — 4 tabs per mock (credentials incl. test buttons,
    curation incl. picks-prompt editor with version note + reset, schedule,
    system health incl. error log, spend, run-now/rebuild buttons)
  - `GET /health` — JSON for MCP/monitoring
- Auth: none. Bind 0.0.0.0 inside container; exposure is Tailscale/LAN via
  Umbrel. Document this assumption in README.

## 7. Telegram bot

- Push on digest assembly: headline list (clusters + picks), deep link to web
  UI. Inline buttons per pick/cluster row: `keep`, and `add @handle` on
  discovery mentions in weekly push.
- Alerts: cookie_dead (once per incident), job failure (collapsed, max 1/day).
- No browsing commands. It is a push channel + button pad only.

## 8. MCP server (`periscope.mcp_server`)

Tools (read ops unrestricted; write ops require `PERISCOPE_MCP_ALLOW_WRITES=1`):

```
get_digest(date?)                -> structured digest JSON
search_archive(query, since?, account?, limit=20)
get_cluster(cluster_id)          -> full tweets, threads unrolled
get_account_intel(handle)        -> stats, recent tweets, graph overlap
list_discovery_queue()
add_to_list(handle)     [write]
reject_candidate(handle)[write]
run_topic_search(query) [write, rate-limited 5/hour]
get_health()
```

Serve over streamable-http on an internal port; document Claude Desktop /
agent config snippet in README.

## 9. Deployment

- Dockerfile: python:3.12-slim, uv install, volume `/data` (db + secrets +
  prompts overrides). Entrypoint runs FastAPI (uvicorn) with APScheduler in
  lifespan; Telegram polling + MCP http as asyncio tasks in the same process.
- `umbrel-app.yml` + compose per Umbrel community app spec; web on port 3999.
- Healthcheck endpoint wired into compose.

## 10. Build phases & acceptance

**Phase 1 — core loop.** config, db, xclient, fetchonly + daily jobs, picks +
cluster prompts, Telegram push. Accept: given valid cookies, `python -m
periscope.jobs.daily` produces a digest row and a Telegram message.
Include a `--mock-x` mode: xclient loads fixture JSON instead of hitting X,
so the pipeline and all later phases are testable without credentials.

**Phase 2 — web.** Build the templates; Today, Feed (keep toggle works,
persists), Cluster detail, Archive FTS. Accept: consistent layouts in light +
dark at 1440px and 390px widths.

**Phase 3 — discovery + weekly.** Snapshots, deltas, mining, weekly report
page + push, Discovery queue accept/reject wired to follow op.

**Phase 4 — settings + MCP.** Settings tabs functional (credential test
buttons actually hit X/Anthropic/Telegram), prompt editor, health panel with
real stats; MCP server with all tools; Docker + Umbrel packaging.

## 11. Testing

- pytest; fixtures = anonymized raw tweet JSON (create `tests/fixtures/`).
- Unit: cluster/picks parse LLM JSON defensively (fence-stripping, schema
  check, fallback = empty picks not crash), rank math, follow-delta diff,
  FTS triggers.
- LLM calls mocked in tests; one optional integration test behind env flag.
- Every job idempotent: rerunning daily for the same date replaces, not
  duplicates.

## 12. Non-goals (do not build)

- Multi-user, auth systems, public exposure hardening
- Algorithmic home-timeline scraping (HomeTimeline endpoint)
- Trends/Explore scraping
- Posting/replying/liking on X
- Embedding-based taste model (v3; leave `keeps` table as its future input)
- Any notification besides digest push and failure alerts