"""Chrome profile zip upload / replace helpers and settings route."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from periscope.config import Secrets
from periscope.db import Database
from periscope.web.app import create_app
from periscope.web.chrome_profile import (
    extract_zip_to_temp,
    profile_in_use,
    read_profile_meta,
    replace_chrome_profile,
    validate_chrome_profile_tree,
    write_profile_meta,
)
from tests.web_auth_helpers import authed_client


def _minimal_profile_tree(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "Local State").write_text("{}", encoding="utf-8")
    default = root / "Default"
    default.mkdir(parents=True, exist_ok=True)
    (default / "Cookies").write_bytes(b"")
    return root


def _zip_tree(tree: Path, zip_path: Path, *, arc_prefix: str = "") -> Path:
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in tree.rglob("*"):
            if path.is_file():
                arc = path.relative_to(tree).as_posix()
                if arc_prefix:
                    arc = f"{arc_prefix.rstrip('/')}/{arc}"
                zf.write(path, arcname=arc)
    return zip_path


def test_zip_slip_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "slip.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../evil.txt", "nope")
        zf.writestr("Local State", "{}")
    with pytest.raises(ValueError, match="unsafe|zip slip"):
        extract_zip_to_temp(zip_path, tmp_path / "out")


def test_absolute_path_member_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "abs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("/tmp/evil.txt", "nope")
    with pytest.raises(ValueError, match="unsafe|zip slip"):
        extract_zip_to_temp(zip_path, tmp_path / "out")


def test_valid_minimal_tree_round_trip_replace(tmp_path: Path) -> None:
    source = _minimal_profile_tree(tmp_path / "source")
    validate_chrome_profile_tree(source)

    dest = tmp_path / "chrome-profile"
    # Pre-existing dest with different content
    old = _minimal_profile_tree(dest)
    (old / "Default" / "Cookies").write_bytes(b"old-cookies")

    replace_chrome_profile(dest, source)
    assert (dest / "Local State").is_file()
    assert (dest / "Default" / "Cookies").read_bytes() == b""
    assert not list(tmp_path.glob("chrome-profile.old-*"))
    assert not list(tmp_path.glob("chrome-profile.tmp-*"))


def test_zip_with_top_level_folder(tmp_path: Path) -> None:
    nested = _minimal_profile_tree(tmp_path / "build" / "MyProfile")
    zip_path = _zip_tree(nested, tmp_path / "wrapped.zip", arc_prefix="MyProfile")
    extracted = extract_zip_to_temp(zip_path, tmp_path / "extracts")
    assert extracted.name == "MyProfile"
    validate_chrome_profile_tree(extracted)


def test_zip_with_profile_at_root(tmp_path: Path) -> None:
    tree = _minimal_profile_tree(tmp_path / "flat")
    zip_path = _zip_tree(tree, tmp_path / "flat.zip")
    extracted = extract_zip_to_temp(zip_path, tmp_path / "extracts")
    validate_chrome_profile_tree(extracted)


def test_in_use_singleton_lock_refuses(tmp_path: Path) -> None:
    dest = _minimal_profile_tree(tmp_path / "live")
    (dest / "SingletonLock").write_text("locked", encoding="utf-8")
    assert profile_in_use(dest) is True
    source = _minimal_profile_tree(tmp_path / "incoming")
    with pytest.raises(RuntimeError, match="still has this profile open"):
        replace_chrome_profile(dest, source)


def test_oversized_compressed_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "periscope.web.chrome_profile.MAX_COMPRESSED_BYTES",
        64,
    )
    tree = _minimal_profile_tree(tmp_path / "big")
    # Uncompressed tiny, but force compressed limit via monkeypatch after zip built
    zip_path = _zip_tree(tree, tmp_path / "big.zip")
    # Pad zip file on disk to exceed limit
    raw = zip_path.read_bytes() + b"\0" * 200
    with pytest.raises(ValueError, match="too large|Zip too large"):
        extract_zip_to_temp(raw, tmp_path / "out", max_compressed=64)


def test_oversized_uncompressed_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "huge.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Local State", "{}")
        zf.writestr("Default/Cookies", b"x" * 1000)
    with pytest.raises(ValueError, match="Uncompressed"):
        extract_zip_to_temp(zip_path, tmp_path / "out", max_uncompressed=500)


def test_meta_round_trip(tmp_path: Path) -> None:
    write_profile_meta(
        tmp_path,
        source_filename="clean.zip",
        bytes_count=1234,
        uploaded_at="2026-09-24T12:00:00Z",
    )
    meta = read_profile_meta(tmp_path)
    assert meta is not None
    assert meta["source_filename"] == "clean.zip"
    assert meta["bytes"] == 1234
    assert meta["uploaded_at"] == "2026-09-24T12:00:00Z"


def test_route_upload_replaces_profile(app_config, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_dir = app_config.data_dir / "chrome-profile"
    _minimal_profile_tree(profile_dir)
    (profile_dir / "Default" / "Cookies").write_bytes(b"stale")
    monkeypatch.setenv("PERISCOPE_X_CHROME_PROFILE", str(profile_dir))

    source = _minimal_profile_tree(app_config.data_dir / "upload-src")
    (source / "Default" / "Cookies").write_bytes(b"fresh")
    zip_path = _zip_tree(source, app_config.data_dir / "fresh.zip")

    database = Database(app_config.db_path)
    app = create_app(app_config, Secrets(), database=database)

    with authed_client(app) as client:
        page = client.get("/settings?tab=connections")
        assert page.status_code == 200
        assert "Upload Chrome profile" in page.text
        assert "Replace profile" in page.text
        assert "Never uploaded via UI" in page.text

        with zip_path.open("rb") as fh:
            response = client.post(
                "/settings/chrome-profile",
                files={"profile_zip": ("fresh.zip", fh, "application/zip")},
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "Chrome profile replaced" in response.text
        assert "Ready" in response.text
        assert "fresh.zip" in response.text

    assert (profile_dir / "Default" / "Cookies").read_bytes() == b"fresh"
    meta = read_profile_meta(app_config.data_dir)
    assert meta is not None
    assert meta["source_filename"] == "fresh.zip"


def test_route_in_use_returns_error(app_config, monkeypatch: pytest.MonkeyPatch) -> None:
    profile_dir = app_config.data_dir / "chrome-profile"
    _minimal_profile_tree(profile_dir)
    (profile_dir / "SingletonLock").touch()
    monkeypatch.setenv("PERISCOPE_X_CHROME_PROFILE", str(profile_dir))

    source = _minimal_profile_tree(app_config.data_dir / "upload-src")
    zip_path = _zip_tree(source, app_config.data_dir / "fresh.zip")

    database = Database(app_config.db_path)
    app = create_app(app_config, Secrets(), database=database)

    with authed_client(app) as client:
        with zip_path.open("rb") as fh:
            response = client.post(
                "/settings/chrome-profile",
                files={"profile_zip": ("fresh.zip", fh, "application/zip")},
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "still has this profile open" in response.text
