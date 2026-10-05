# Periscope

Periscope is a self-hosted, read-only X/Twitter digest. It turns a curated list
timeline into a finite daily set of clustered stories and standalone picks,
stores the source material in SQLite, and pushes a short summary to Telegram.

The product and architecture contract lives in [plan.md](plan.md). The
production interface is a FastAPI, Jinja, HTMX application.

## Implemented system

Periscope currently includes:

- typed TOML configuration plus chmod-600 env-file secrets;
- idempotent SQLite migrations, WAL, FTS5, and raw source persistence;
- live `twscrape` and fixture-backed X adapters with thread and quote expansion;
- defensive Anthropic clustering, picks, weekly narration, and spend accounting;
- engagement-normalized ranking, bait penalties, and view-driven topic decay;
- finite Today, Feed, Archive, Discovery, Weekly, and Settings pages;
- local keeps, graph discovery, review actions, and weekly calibration;
- in-process APScheduler jobs with matching CLI and run-now entrypoints;
- Telegram delivery, inline actions, and collapsed failure alerts;
- official MCP stdio and Streamable HTTP tools with an explicit write gate;
- non-root Docker, Compose healthcheck, and Umbrel packaging;
- deterministic `--mock-x --mock-llm` execution for credential-free testing.

The production curation prompts are deliberately not included yet. A live daily
or weekly run reports a clear configuration error until the corresponding
prompt path points to a non-empty file. The deterministic mock curator exercises
structure and persistence only; it does not encode editorial policy.

## How data moves

```text
X list or JSON fixture
        |
        v
fetch log -> raw tweet rows -> FTS5 archive
                               |
                               v
                     clustering + picks
                               |
                               v
                    ranked digest row
                        |            |
                        v            v
                   Telegram        web/MCP
```

The important boundary is the raw tweet row. Fetching completes that write
before curation begins, so a stored window can be rebuilt with new prompts
without contacting X again.

## Local mock run

Python 3.12 and `uv` are required.

```bash
uv sync --extra dev
uv run python -m periscope.jobs.daily \
  --config config.example.toml \
  --data-dir ./data \
  --mock-x tests/fixtures/timeline.json \
  --mock-llm \
  --date 2026-07-16
```

This creates `data/periscope.db`, ingests the anonymized fixture, assembles a
digest, and prints a machine-readable job result. No X, Anthropic, or Telegram
credentials are required.

Reprocess the stored rows without fetching again:

```bash
uv run python -m periscope.jobs.daily \
  --config config.example.toml \
  --data-dir ./data \
  --skip-fetch \
  --mock-llm \
  --date 2026-07-16
```


Load an already-curated digest JSON (no X fetch, no Anthropic). Tweets, picks,
optional cluster stories, commentary, and image URLs are stored as-is:

```bash
uv run python -m periscope.jobs.ingest \
  --config config.example.toml \
  --data-dir ./data \
  --file tests/fixtures/agent-digest.json
```

## X scrape and GLM curator

Periscope itself does not fetch the algo feed. The happy path is one pipeline CLI
that scrapes signed-in home timelines, ranks them with a cheap OpenRouter model,
hydrates truncated keepers, caches images under `data/media/`, converts to ingest
JSON, and loads the digest:

```bash
uv sync --extra scrape
uv run playwright install chromium

# Chrome profile via env (or pass --profile)
export PERISCOPE_X_CHROME_PROFILE=./data/chrome-profile

uv run periscope-digest \
  --out-dir ./data/x-dumps \
  --watermark ./data/following_watermark.json \
  --config config.example.toml \
  --data-dir ./data \
  --date 2026-08-28
```

Skip steps when replaying an existing dump:

```bash
uv run periscope-digest --skip-scrape --date 2026-08-28
```

Individual stages remain available:

```bash
uv run python -m periscope.x_scrape.scrape_feeds \
  --out-dir ./data/x-dumps \
  --profile ./data/chrome-profile \
  --watermark ./data/following_watermark.json \
  --min-foryou 200 --min-following 200 \
  --timelines Tech,Crypto,Business --min-timeline 100

uv run python -m periscope.x_scrape.curate_feeds --in-dir ./data/x-dumps

uv run python -m periscope.x_scrape.hydrate_shortlist --shortlist ./data/x-dumps/shortlist.json

uv run python -m periscope.x_scrape.shortlist_to_digest \
  --shortlist ./data/x-dumps/shortlist.json \
  --date 2026-08-28

uv run python -m periscope.jobs.ingest \
  --config config.example.toml \
  --data-dir ./data \
  --file ./data/x-dumps/digest-2026-08-28.json
```

The Chrome profile must already be signed in as the X account. Put
`OPENROUTER_API_KEY` in `/data/secrets.env` or the environment. Dumps, cached
media, the Chrome profile, and secrets stay in `data/` and are gitignored. The
curator does not tweet, like, follow, or reply. Today serves cached images from
`/media/...` when present and keeps original remote URLs if a download fails.


### Topic accounts (one X account per topic)

Each topic can be its own X account: account `ai` only follows AI posters and
likes AI posts, `crypto` does the same for crypto, and so on. Your main account
stays the mixed one (For You, Following, pinned Grok timelines) and is the
built-in `main` row, mapped to the existing scrape profile, so nothing changes
until you add a topic account. The recap merges every source into one magazine;
duplicates are merged and keep all source tags, and Today shows a small account
chip on each pick once a topic account contributed.

Adding one:

1. Create the X account and follow a few good accounts for the topic by hand.
2. Settings → **Accounts** → *Add topic account*: label (e.g. `AI`), optional
   id (defaults to the label slug), X handle, and a short topic description.
   The curator sees the description; the label joins the topic list.
3. Sign it in, using either option:
   - **Upload**: on any machine, create a fresh Chrome profile, sign in to that
     X account only, quit Chrome, zip the profile folder, then use *Edit → Replace
     profile* on the account card. Uploading is refused while Chrome has that
     profile open.
   - **Headed Chrome on this host**: run
     `uv run python -m periscope.x_scrape.account_scrape --account ai` (no
     `--headless`) once with a visible desktop. It opens
     `data/chrome-profiles/ai` (or the folder you set). If it prints
     `NOT_SIGNED_IN`, open that profile in Chrome
     (`google-chrome --user-data-dir=data/chrome-profiles/ai https://x.com/login`),
     sign in, close it, and rerun.
4. The next `periscope-digest` scrapes that account's Following timeline to its
   own watermark (`data/watermarks/<id>.json`, at least 100 posts by default;
   set this per account or with `--min-account-following`). Every post is tagged
   `source_account`. If an account is signed out or locked, the run logs
   `NOT_SIGNED_IN account=<id>` and skips that account; the other accounts and
   the main magazine still run. The card shows signed-in status and the last
   scrape time.

Likes and follows only happen with `periscope-digest --actions`
(`--actions-dry-run` prints the plan without clicking). The default rule:
a keeper that topic account X surfaced is liked by X, if likes are on for X. A
`follow` candidate whose topic matches X, or that X surfaced, goes to X's
follow queue, and X follows up to its cap per run. Follows that route to
`main` stay in the manual Today queue that the main scrape drains, as before.
To run the actions by hand:
`uv run python -m periscope.x_scrape.account_actions --digest data/x-dumps/digest-YYYY-MM-DD.json --dry-run`.


## Actions, nuggets, and idea inbox

Today is not only news. Each pick can carry 1–3 typed actions (`try`, `read`,
`watch`, `steal`, `follow`) with open / copy / **Park in inbox** controls.
**Follow** on Today queues; next scrape follows. A **Nuggets** lane surfaces
picks marked `nugget` or `actionable` (ideas and techniques, not just breaking
topic posts). Archive filters by action type; ingest auto-keeps action picks
locally.

The local **Ideas** inbox (SQLite `ideas` table) stores parked actions. Park,
mark done, drop, or edit a note — nothing syncs to X. Weekly also lists parked
ideas older than 7 days that were never touched.

### How enrich_actions fits `periscope-digest`

After hydrate and `shortlist_to_digest`, the pipeline runs
`periscope.x_scrape.enrich_actions` on the digest JSON (skip with
`--skip-enrich`). Without `OPENROUTER_API_KEY` it soft-fails to heuristics:
GitHub/Hugging Face → `try`, docs → `read`, exploit/oracle language → `watch`,
follow mentions → `follow`. With a key it uses OpenRouter `glm-5.3-flash` the
same way as `curate_feeds`, filling `actions` (max 3) plus `nugget` /
`actionable` / `nugget_why`. Ingest preserves those fields on the rendered
digest.

```bash
uv run python -m periscope.x_scrape.enrich_actions \
  --digest ./data/x-dumps/digest-2026-09-01.json
```



Run ingestion only:

```bash
uv run python -m periscope.jobs.fetchonly \
  --config config.example.toml \
  --data-dir ./data \
  --mock-x tests/fixtures/timeline.json
```

## Live configuration

1. Copy `config.example.toml` to `/data/config.toml`.
2. Set `x.list_id` to the numeric ID of the curated X list.
3. Add the seed handles and topics.
4. Copy `secrets.example.env` to `/data/secrets.env`, fill it, and run
   `chmod 600 /data/secrets.env`.
5. Add the cluster, picks, and weekly prompt files at the configured paths.
6. Update model pricing in TOML if USD spend reporting is required; prices are
   configuration because they change independently from application releases.

Secrets accepted by the env file are:

- `X_AUTH_TOKEN` and `X_CT0`;
- `ANTHROPIC_API_KEY`;
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Process environment variables override values from the file. Secret values are
never copied into the Periscope database. The database stores only
`*_is_configured` flags.

A live daily run is:

```bash
uv run python -m periscope.jobs.daily
```

An X authentication failure records one open `cookie_dead` incident and sends
one Telegram alert. Successful fetching closes the incident, allowing a future
failure to alert again.

## Web and scheduling

Start the complete local service:

```bash
PERISCOPE_DATA_DIR=./data uv run uvicorn periscope.web.app:app \
  --host 0.0.0.0 --port 3999
```

The FastAPI lifespan starts the feed interval, daily digest times, weekly report,
mounted MCP session manager, and optional Telegram callback polling. Settings
schedule edits replace APScheduler jobs immediately. Consumption pages do not
poll, auto-refresh, or provide infinite scroll.

The Settings credentials form never renders stored secret values. Blank fields
preserve the existing value; supplied values are atomically written to
`/data/secrets.env` with mode 600.

## MCP

The Streamable HTTP endpoint is available at:

```text
http://periscope.local:3999/mcp
```

The stdio server is:

```bash
uv run periscope-mcp
```

Example Claude Desktop style configuration:

```json
{
  "mcpServers": {
    "periscope": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/periscope", "periscope-mcp"],
      "env": {"PERISCOPE_DATA_DIR": "/data"}
    }
  }
}
```

Read tools are always available. `add_to_list`, `reject_candidate`, and
`run_topic_search` require `PERISCOPE_MCP_ALLOW_WRITES=1`. Topic searches
are additionally limited to five calls per rolling hour.

## Docker and Umbrel

Build the multi-architecture image before Umbrel distribution:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  --tag your-registry/periscope:0.1.0 --push .
```

Before submitting to an app store, replace the local image in
`docker-compose.yml` with that published image and its multi-architecture
SHA-256 digest, then fill the repository and support URLs in `umbrel-app.yml`.
Umbrel mounts `${APP_DATA_DIR}/data` at `/data`; the database, configuration,
secrets, prompt overrides, and twscrape pool all persist there.

The application has no built-in user authentication. Umbrel's app proxy may
protect the UI, but direct Docker deployments must remain on a trusted LAN or
Tailscale network. Do not expose port 3999 to the public internet.

## Tests

```bash
uv run pytest
```

The suite covers configuration precedence, migration idempotency, raw payload
preservation, FTS trigger synchronization, secret isolation, cookie-alert
collapse, defensive LLM parsing, ranking, weekly idempotency, Settings
operations, MCP write gating, and the mounted HTTP transport.

An explicitly opt-in smoke test can verify configured live services without
writing to X or sending a Telegram message:

```bash
PERISCOPE_CONFIG=/data/config.toml \
PERISCOPE_SECRETS=/data/secrets.env \
PERISCOPE_RUN_LIVE_TESTS=1 \
uv run pytest -m live tests/test_live_integration.py
```

It reads at most three posts from the configured X list into a temporary
database, makes one small structured Anthropic request, and, when Telegram is
configured, performs read-only bot and chat lookups. It never follows an
account, sends a Telegram message, or touches the production database.

## Deployment assumption

The trusted-private-network assumption is a product boundary, not a substitute
for public-exposure hardening.
