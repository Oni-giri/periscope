"""Multi-account topic accounts: model/migration, dedupe, routing, skip-on-signed-out."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from periscope.config import Secrets
from periscope.db import MIGRATIONS, Database
from periscope.jobs.ingest import run_ingest
from periscope.web.app import create_app
from periscope.x_accounts import (
    MAIN_SLUG,
    XAccount,
    like_plan,
    merge_sources,
    owner_account,
    profile_path,
    route_follow,
    watermark_path,
)
from periscope.x_scrape import account_actions, account_scrape
from periscope.x_scrape.curate_feeds import gather, tag_keeper_account
from periscope.x_scrape.follow_queued import drain_follow_queue
from periscope.x_scrape.ingest_overflow import load_scrape_posts
from periscope.x_scrape.shortlist_to_digest import convert
from periscope.x_scrape.watermark import load_watermark
from tests.web_auth_helpers import authed_client


def _accounts() -> list[XAccount]:
    return [
        XAccount(slug="main", label="Main", like_enabled=False),
        XAccount(slug="ai", label="AI", description="Models and agents"),
        XAccount(slug="crypto", label="Crypto"),
        XAccount(slug="old", label="Science", enabled=False),
    ]


# ---------------------------------------------------------------------------
# Model / migration


def test_fresh_db_has_main_account_and_follow_account_column(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    rows = database.list_x_accounts()
    assert [r["slug"] for r in rows] == ["main"]
    main = XAccount.from_row(rows[0])
    assert main.is_main and main.enabled and not main.like_enabled
    assert main.follow_cap == 15 and main.min_following == 100
    row = database.enqueue_follow("@Alice")
    assert row["account"] == "main"


def test_migration_keeps_existing_follow_rows_as_main(tmp_path: Path) -> None:
    db_path = tmp_path / "periscope.db"
    # Build a database as it looked before migration 6.
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version, script in enumerate(MIGRATIONS[:5], start=1):
            connection.executescript(script)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-09-01T00:00:00Z')", (version,)
            )
        connection.execute(
            "INSERT INTO follow_queue(handle, added_at, status, source) "
            "VALUES ('legacy', '2026-09-01T00:00:00Z', 'pending', 'today')"
        )
        connection.execute(
            "INSERT INTO follow_queue(handle, added_at, status, source) "
            "VALUES ('done', '2026-09-01T00:00:00Z', 'followed', 'today')"
        )
        connection.commit()

    database = Database(db_path)
    database.initialize()
    database.initialize()  # idempotent
    rows = {r["handle"]: r for r in database.rows("SELECT * FROM follow_queue")}
    assert rows["legacy"]["account"] == "main"
    assert rows["done"]["account"] == "main"
    assert [r["handle"] for r in database.list_pending_follows("main")] == ["legacy"]
    assert database.list_pending_follows("ai") == []
    assert database.get_x_account("main") is not None


def test_x_account_crud_and_delete_drops_pending(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    added = database.add_x_account(
        "ai", label="AI", x_handle="@ai_reader", description="agents", follow_cap=5
    )
    assert added["x_handle"] == "ai_reader"
    assert added["enabled"] == 1 and added["like_enabled"] == 1
    with pytest.raises(ValueError, match="already exists"):
        database.add_x_account("ai", label="AI again")
    with pytest.raises(ValueError, match="between"):
        database.update_x_account("ai", follow_cap=-1)
    updated = database.update_x_account("ai", enabled=False, min_following=150)
    assert updated is not None and updated["enabled"] == 0 and updated["min_following"] == 150
    assert [r["slug"] for r in database.list_x_accounts(enabled_only=True)] == ["main"]

    database.enqueue_follow("pending_ai", account="ai")
    database.enqueue_follow("done_ai", account="ai")
    database.mark_follow("done_ai", "followed")
    with pytest.raises(ValueError, match="main"):
        database.delete_x_account("main")
    assert database.delete_x_account("ai") is True
    handles = {r["handle"] for r in database.rows("SELECT handle FROM follow_queue")}
    assert handles == {"done_ai"}


def test_profile_and_watermark_paths(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PERISCOPE_X_CHROME_PROFILE", str(tmp_path / "main-profile"))
    ai = XAccount(slug="ai", label="AI")
    assert profile_path(ai, tmp_path) == tmp_path / "chrome-profiles" / "ai"
    assert watermark_path(ai, tmp_path) == tmp_path / "watermarks" / "ai.json"
    custom = XAccount(slug="ai", label="AI", profile_dir="profiles/x-ai")
    assert profile_path(custom, tmp_path) == tmp_path / "profiles" / "x-ai"
    main = XAccount(slug="main", label="Main")
    assert profile_path(main, tmp_path) == tmp_path / "main-profile"
    assert watermark_path(main, tmp_path) == tmp_path / "following_watermark.json"


# ---------------------------------------------------------------------------
# Dedupe across sources


def _post(sid: str, text: str = "t", **extra) -> dict:
    return {
        "status_id": sid,
        "author_handle": "@a",
        "text": text,
        "tweet_url": f"https://x.com/a/status/{sid}",
        **extra,
    }


def test_merge_sources_keeps_every_source_tag() -> None:
    ordered, by_id = merge_sources(
        [
            ("foryou", "main", [_post("1"), _post("2", "short…", is_truncated=True)]),
            ("tech", "main", [_post("1")]),
            ("following", "ai", [_post("2", "full text"), _post("3")]),
            ("following", "crypto", [_post("1"), _post("3")]),
        ]
    )
    assert [p["status_id"] for p in ordered] == ["1", "2", "3"]
    assert by_id["1"]["source_accounts"] == ["main", "crypto"]
    assert by_id["1"]["feeds"] == ["foryou", "tech", "following"]
    assert by_id["1"]["feed"] == "foryou"
    assert by_id["2"]["source_accounts"] == ["main", "ai"]
    assert by_id["2"]["text"] == "full text" and by_id["2"]["is_truncated"] is False
    assert by_id["3"]["source_accounts"] == ["ai", "crypto"]


def test_curate_gather_and_overflow_include_account_dumps(tmp_path: Path) -> None:
    (tmp_path / "foryou.json").write_text(json.dumps([_post("1"), _post("2")]))
    (tmp_path / "following.json").write_text(json.dumps([_post("2")]))
    (tmp_path / "timeline_tech.json").write_text(json.dumps([_post("4")]))
    ai_dir = tmp_path / "accounts" / "ai"
    ai_dir.mkdir(parents=True)
    (ai_dir / "following.json").write_text(
        json.dumps([_post("2", source_account="ai"), _post("5", source_account="ai")])
    )
    stale = tmp_path / "accounts" / "gone"
    stale.mkdir(parents=True)
    (stale / "following.json").write_text(json.dumps([_post("9")]))

    compact, by_id = gather(
        tmp_path, accounts=["main", "ai"], account_labels={"main": "Main", "ai": "AI"}
    )
    assert sorted(by_id) == ["1", "2", "4", "5"]  # disabled/removed account dump ignored
    assert by_id["2"]["source_accounts"] == ["main", "ai"]
    item2 = next(c for c in compact if c["status_id"] == "2")
    assert item2["accounts"] == ["Main", "AI"]

    # Without topic dumps the curator payload is unchanged (no accounts key).
    single = tmp_path / "single"
    single.mkdir()
    (single / "foryou.json").write_text(json.dumps([_post("1")]))
    compact_single, _ = gather(single, accounts=[], account_labels=None)
    assert "accounts" not in compact_single[0]

    overflow = {p["status_id"]: p for p in load_scrape_posts(tmp_path)}
    assert overflow["2"]["source_accounts"] == ["main", "ai"]
    assert "5" in overflow


def test_keeper_account_tag_and_digest_chips(app_config) -> None:
    accounts = _accounts()
    keeper = {**_post("10"), "source_accounts": ["main", "ai"], "curation": {"topic": "ai"}}
    tag_keeper_account(keeper, accounts)
    assert keeper["account"] == "ai" and keeper["account_label"] == "AI"
    main_only = {
        **_post("11"),
        "source_accounts": ["main"],
        "curation": {"topic": "tools", "why": "x"},
    }
    tag_keeper_account(main_only, accounts)
    assert main_only["account"] == "main"

    keeper["curation"].update({"why": "agents", "score": 8})
    doc = convert([keeper, main_only], "2026-10-05")
    pick = doc["picks"][0]
    assert pick["account"] == "ai"
    assert pick["accounts"] == [{"slug": "main", "label": "Main"}, {"slug": "ai", "label": "AI"}]
    assert doc["tweets"][0]["source_accounts"] == ["main", "ai"]
    assert doc["picks"][1]["accounts"] == [{"slug": "main", "label": "Main"}]

    # Single-account digest: no chips.
    plain = convert([dict(main_only)], "2026-10-05")
    assert "accounts" not in plain["picks"][0]

    source = app_config.data_dir / "digest.json"
    source.write_text(json.dumps(doc))
    database = Database(app_config.db_path)
    run_ingest(
        app_config, Secrets(), source=source, database=database,
        now=datetime(2026, 10, 5, 6, 0, tzinfo=UTC),
    )
    rendered = database.get_digest("2026-10-05")["rendered"]
    ai_pick = next(p for p in rendered["picks"] if p["tweet_id"] == "10")
    assert ai_pick["account"] == "ai"
    app = create_app(app_config, Secrets(), database=database)
    with authed_client(app) as client:
        html = client.get("/").text
    assert 'class="account-chip' in html
    assert ">AI</span>" in html


# ---------------------------------------------------------------------------
# Routing


def test_routing_rules() -> None:
    accounts = _accounts()
    assert route_follow(accounts, topic="ai") == "ai"
    assert route_follow(accounts, topic="Crypto") == "crypto"
    assert route_follow(accounts, topic="science") == MAIN_SLUG  # disabled account
    assert route_follow(accounts, topic="tools", source_accounts=["main", "crypto"]) == "crypto"
    assert route_follow(accounts, topic="other", source_accounts=["main"]) == MAIN_SLUG
    # Topic beats source for follows.
    assert route_follow(accounts, topic="ai", source_accounts=["crypto"]) == "ai"
    # Owner prefers the sourcing account whose topic matches.
    assert owner_account(accounts, source_accounts=["crypto", "ai"], topic="ai") == "ai"
    assert owner_account(accounts, source_accounts=["crypto"], topic="ai") == "crypto"

    plan = like_plan(
        accounts,
        [
            {"tweet_url": "u1", "source_accounts": ["main", "ai"]},
            {"tweet_url": "u2", "source_accounts": ["main"]},
            {"tweet_url": "u3", "source_accounts": ["crypto", "ai", "old"]},
        ],
    )
    assert plan == {"ai": ["u1", "u3"], "crypto": ["u3"]}


def test_plan_actions_from_digest_routes_follows_by_topic() -> None:
    doc = {
        "tweets": [
            {"id": "1", "author": "alice", "urls": ["https://x.com/alice/status/1"]},
            {"id": "2", "author": "bob", "urls": []},
            {"id": "3", "author": "carol", "urls": []},
        ],
        "picks": [
            {"tweet_id": "1", "topic": "ai", "source_accounts": ["main"],
             "actions": [{"type": "follow", "label": "Follow @alice"}]},
            {"tweet_id": "2", "topic": "crypto", "source_accounts": ["crypto"],
             "actions": [{"type": "follow", "label": "x", "url": "https://x.com/BobChain"}]},
            {"tweet_id": "3", "topic": "tools", "source_accounts": ["main"],
             "actions": [{"type": "follow", "label": "Follow @carol"}]},
        ],
    }
    plan = account_actions.plan_actions(_accounts(), account_actions.keepers_from_digest(doc))
    assert plan["follows"] == {"ai": [("alice", "1")], "crypto": [("bobchain", "2")]}
    assert plan["likes"] == {"crypto": ["https://x.com/bob/status/2"]}


def test_queue_follow_route_uses_topic_account(app_config) -> None:
    database = Database(app_config.db_path)
    database.initialize()
    database.add_x_account("ai", label="AI")
    app = create_app(app_config, Secrets(), database=database)
    with authed_client(app) as client:
        r1 = client.post("/follow-queue", data={"handle": "agent_dev", "topic": "ai"},
                         headers={"HX-Request": "true"})
        r2 = client.post("/follow-queue", data={"handle": "toolsmith", "topic": "tools"},
                         headers={"HX-Request": "true"})
        r3 = client.post("/follow-queue", data={"handle": "src", "account": "ai"},
                         headers={"HX-Request": "true"})
    assert r1.status_code == r2.status_code == r3.status_code == 200
    by_handle = {r["handle"]: r["account"] for r in database.list_pending_follows()}
    assert by_handle == {"agent_dev": "ai", "toolsmith": "main", "src": "ai"}

    # The main scrape's drain only touches main rows.
    seen: list[str] = []
    import periscope.x_scrape.follow_queued as fq

    def fake_follow(page, handle):
        seen.append(handle)
        return "followed"

    original = fq.follow_one
    fq.follow_one = fake_follow
    try:
        drain_follow_queue(object(), app_config.db_path)
    finally:
        fq.follow_one = original
    assert seen == ["toolsmith"]
    assert {r["handle"] for r in database.list_pending_follows("ai")} == {"agent_dev", "src"}


# ---------------------------------------------------------------------------
# Skip on signed-out / locked


class _FakePage:
    def __init__(self, signed: bool):
        self.signed = signed


def _fake_opener(signed_by_profile: dict[str, bool], opened: list[str]):
    @contextmanager
    def opener(profile: Path, headless: bool):
        opened.append(profile.name)
        yield _FakePage(signed_by_profile.get(profile.name, False))

    return opener


def test_scrape_skips_signed_out_account_and_continues(tmp_path: Path) -> None:
    database = Database(tmp_path / "periscope.db")
    database.initialize()
    database.add_x_account("ai", label="AI")
    database.add_x_account("crypto", label="Crypto", min_following=3)
    database.add_x_account("locked", label="Locked")
    accounts = [XAccount.from_row(r) for r in database.list_x_accounts()]
    out_dir = tmp_path / "x-dumps"
    stale = out_dir / "accounts" / "ai" / "following.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps([_post("old")]))
    prior = tmp_path / "watermarks" / "crypto.json"
    prior.parent.mkdir(parents=True)
    prior.write_text(json.dumps({"following_newest_ids": ["c2"]}))

    opened: list[str] = []
    calls: list[dict] = []

    def collect(page, *, min_unique, watermark_ids, max_scrolls):
        calls.append({"min": min_unique, "wm": set(watermark_ids)})
        return [
            _post("c1", created_at="2026-10-05T05:00:00Z"),
            _post("c2", created_at="2026-10-04T05:00:00Z"),
        ]

    def check_profile(profile: Path):
        return "profile locked (Chrome still has it open)" if profile.name == "locked" else None

    results = account_scrape.scrape_topic_accounts(
        accounts,
        data_dir=tmp_path,
        out_dir=out_dir,
        database=database,
        open_page=_fake_opener({"crypto": True, "ai": False}, opened),
        is_signed_in=lambda page: page.signed,
        collect_following=collect,
        check_profile=check_profile,
    )
    by_slug = {r["account"]: r for r in results}
    assert by_slug["ai"]["status"] == account_scrape.NOT_SIGNED_IN
    assert by_slug["locked"]["status"] == account_scrape.NOT_SIGNED_IN
    assert by_slug["crypto"]["status"] == "ok" and by_slug["crypto"]["count"] == 2
    assert "locked" not in opened  # never launched Chrome on a locked profile
    assert not stale.exists()  # stale dump from a previous run removed on skip
    assert calls == [{"min": 3, "wm": {"c2"}}]
    dumped = json.loads((out_dir / "accounts" / "crypto" / "following.json").read_text())
    assert {p["source_account"] for p in dumped} == {"crypto"}
    ids, ts = load_watermark(tmp_path / "watermarks" / "crypto.json")
    assert ids == {"c1", "c2"} and ts == "2026-10-05T05:00:00Z"

    rows = {r["slug"]: r for r in database.list_x_accounts()}
    assert rows["ai"]["signed_in"] == 0
    assert rows["ai"]["last_scrape_status"].startswith("NOT_SIGNED_IN")
    assert rows["crypto"]["signed_in"] == 1 and rows["crypto"]["last_scrape_count"] == 2
    assert rows["main"]["last_scrape_at"] is None  # main untouched


def test_scrape_survives_launch_error(tmp_path: Path) -> None:
    accounts = [XAccount(slug="a", label="A"), XAccount(slug="b", label="B")]

    @contextmanager
    def opener(profile: Path, headless: bool):
        if profile.name == "a":
            raise RuntimeError("ProcessSingleton: user data directory is already in use")
        yield _FakePage(True)

    results = account_scrape.scrape_topic_accounts(
        accounts,
        data_dir=tmp_path,
        out_dir=tmp_path / "out",
        open_page=opener,
        is_signed_in=lambda page: True,
        collect_following=lambda page, **kw: [_post("1")],
        check_profile=lambda profile: None,
    )
    assert [r["status"] for r in results] == [account_scrape.NOT_SIGNED_IN, "ok"]


def test_actions_skip_signed_out_and_never_click(tmp_path: Path) -> None:
    clicks: list[str] = []
    account = XAccount(slug="ai", label="AI")
    result = account_actions.like_keepers(
        account,
        ["https://x.com/a/status/1"],
        data_dir=tmp_path,
        open_page=_fake_opener({}, []),
        is_signed_in=lambda page: False,
        check_profile=lambda profile: None,
        like=lambda page, url: clicks.append(url) or "liked",
    )
    assert result["status"] == account_actions.NOT_SIGNED_IN
    assert clicks == []

    ok = account_actions.like_keepers(
        account,
        ["https://x.com/a/status/1", "https://x.com/a/status/2"],
        data_dir=tmp_path,
        open_page=_fake_opener({"ai": True}, []),
        is_signed_in=lambda page: page.signed,
        check_profile=lambda profile: None,
        like=lambda page, url: clicks.append(url) or ("already" if url.endswith("2") else "liked"),
        pause=0,
    )
    assert ok == {"account": "ai", "status": "ok", "liked": 1, "already": 1, "failed": 0}


def test_run_account_actions_end_to_end_with_fakes(tmp_path: Path) -> None:
    database = Database(tmp_path / "periscope.db")
    database.initialize()
    database.add_x_account("ai", label="AI", follow_cap=1)
    database.add_x_account("crypto", label="Crypto")
    database.enqueue_follow("main_only")  # main queue must stay untouched
    doc = {
        "tweets": [{"id": "1", "author": "alice", "urls": ["https://x.com/alice/status/1"]}],
        "picks": [
            {"tweet_id": "1", "topic": "ai", "source_accounts": ["ai", "crypto"],
             "actions": [{"type": "follow", "label": "Follow @alice"},
                         {"type": "follow", "label": "Follow @bob"}]},
        ],
    }
    clicks: list[tuple[str, str]] = []
    results = account_actions.run_account_actions(
        database,
        doc,
        data_dir=tmp_path,
        open_page=_fake_opener({"ai": True, "crypto": False}, []),
        is_signed_in=lambda page: page.signed,
        check_profile=lambda profile: None,
        like=lambda page, url: clicks.append(("like", url)) or "liked",
        follow=lambda page, handle: clicks.append(("follow", handle)) or "followed",
        pause=0,
    )
    by_kind = {(r["kind"], r["account"]): r for r in results}
    assert by_kind[("like", "ai")]["liked"] == 1
    assert by_kind[("follow", "ai")]["outcomes"] == [{"handle": "alice", "status": "followed"}]
    # crypto surfaced it too but is signed out: skipped, nothing clicked as crypto.
    assert by_kind[("like", "crypto")]["status"] == account_actions.NOT_SIGNED_IN
    assert clicks == [("like", "https://x.com/alice/status/1"), ("follow", "alice")]
    assert [r["handle"] for r in database.list_pending_follows("ai")] == ["bob"]  # over cap
    assert [r["handle"] for r in database.list_pending_follows("main")] == ["main_only"]

    # Dry run plans without clicking or enqueuing.
    clicks.clear()
    database.add_x_account("tools", label="Tools")
    dry = account_actions.run_account_actions(
        database, doc, data_dir=tmp_path, dry_run=True,
        check_profile=lambda profile: None,
    )
    assert clicks == []
    assert any(r.get("status") == "dry-run" for r in dry)


# ---------------------------------------------------------------------------
# Settings UI


def _profile_zip(root: Path, name: str) -> Path:
    tree = root / f"{name}-tree"
    (tree / "Default").mkdir(parents=True)
    (tree / "Local State").write_text("{}")
    (tree / "Default" / "Cookies").write_bytes(b"fresh")
    zip_path = root / f"{name}.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in tree.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(tree).as_posix())
    return zip_path


def test_settings_accounts_tab_crud_and_upload(app_config, monkeypatch) -> None:
    monkeypatch.setenv("PERISCOPE_X_CHROME_PROFILE", str(app_config.data_dir / "chrome-profile"))
    database = Database(app_config.db_path)
    app = create_app(app_config, Secrets(), database=database)
    hx = {"HX-Request": "true"}
    with authed_client(app) as client:
        page = client.get("/settings?tab=accounts")
        assert page.status_code == 200
        assert "X accounts" in page.text and "Add topic account" in page.text
        assert "Main" in page.text

        added = client.post(
            "/settings/x-accounts/add",
            data={"label": "AI", "x_handle": "@ai_reader", "description": "agents",
                  "min_following": "120", "follow_cap": "10", "like_enabled": "on",
                  "enabled": "on"},
            headers=hx,
        )
        assert "Account &#39;AI&#39; added" in added.text or "Account 'AI' added" in added.text
        row = database.get_x_account("ai")
        assert row["x_handle"] == "ai_reader" and row["min_following"] == 120
        assert "Needs sign-in" in added.text

        dup = client.post("/settings/x-accounts/add", data={"label": "AI"}, headers=hx)
        assert "already exists" in dup.text
        reserved = client.post("/settings/x-accounts/add", data={"label": "Main"}, headers=hx)
        assert "reserved" in reserved.text

        edited = client.post(
            "/settings/x-accounts/ai/edit",
            data={"label": "AI", "description": "frontier models", "min_following": "100",
                  "follow_cap": "5", "enabled": "on"},
            headers=hx,
        )
        assert edited.status_code == 200
        row = database.get_x_account("ai")
        assert row["description"] == "frontier models" and row["like_enabled"] == 0

        main_edit = client.post(
            "/settings/x-accounts/main/edit",
            data={"label": "Main", "x_handle": "Yaki", "profile_dir": "/tmp/evil"},
            headers=hx,
        )
        assert main_edit.status_code == 200
        main = database.get_x_account("main")
        assert main["x_handle"] == "Yaki" and main["profile_dir"] == ""

        toggled = client.post("/settings/x-accounts/ai/toggle", headers=hx)
        assert "disabled" in toggled.text
        assert database.get_x_account("ai")["enabled"] == 0
        no_main = client.post("/settings/x-accounts/main/toggle", headers=hx)
        assert "cannot be disabled" in no_main.text

        zip_path = _profile_zip(app_config.data_dir, "ai")
        with zip_path.open("rb") as fh:
            up = client.post(
                "/settings/x-accounts/ai/chrome-profile",
                files={"profile_zip": ("ai.zip", fh, "application/zip")},
                headers=hx,
            )
        assert "replaced" in up.text
        dest = app_config.data_dir / "chrome-profiles" / "ai"
        assert (dest / "Default" / "Cookies").read_bytes() == b"fresh"
        assert database.get_x_account("ai")["profile_upload_name"] == "ai.zip"

        (dest / "SingletonLock").touch()
        with zip_path.open("rb") as fh:
            refused = client.post(
                "/settings/x-accounts/ai/chrome-profile",
                files={"profile_zip": ("ai.zip", fh, "application/zip")},
                headers=hx,
            )
        assert "still has this profile open" in refused.text

        deleted = client.post("/settings/x-accounts/ai/delete", headers=hx)
        assert "removed" in deleted.text
        assert database.get_x_account("ai") is None
        assert dest.exists()  # profile folder left on disk
        main_delete = client.post("/settings/x-accounts/main/delete", headers=hx)
        assert "cannot be deleted" in main_delete.text


def test_profile_in_use_ignores_leftover_lock_files(tmp_path: Path) -> None:
    import os
    import socket

    from periscope.web.chrome_profile import profile_in_use

    profile = tmp_path / "profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "LOCK").write_bytes(b"")  # Chrome leaves this after exit
    assert profile_in_use(profile) is False
    os.symlink(f"{socket.gethostname()}-999999999", profile / "SingletonLock")
    assert profile_in_use(profile) is False  # stale: pid gone
    (profile / "SingletonLock").unlink()
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", profile / "SingletonLock")
    assert profile_in_use(profile) is True
