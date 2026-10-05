"""Chrome/Chromium user-data profile upload helpers (zip validate + atomic replace)."""

from __future__ import annotations

import json
import shutil
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

META_FILENAME = "chrome_profile_upload.json"
MAX_COMPRESSED_BYTES = 200 * 1024 * 1024  # 200 MB
MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024  # 500 MB

_LOCK_MARKERS = (  # kept for backwards compatibility; see profile_in_use
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "lockfile",
)


def profile_lock_path(profile_dir: Path) -> Path:
    """Path Chromium uses as its primary SingletonLock marker."""
    return Path(profile_dir) / "SingletonLock"


def _file_lock_held(path: Path) -> bool:
    """True if another process holds a POSIX lock on ``path``.

    Chromium leaves ``Default/LOCK`` (and similar) on disk after a clean exit;
    only an active lock means the profile is open. Uses ``F_GETLK`` on a
    read-only handle so checking never takes or changes a lock.
    """
    try:
        import fcntl
        import struct
    except ImportError:  # pragma: no cover - non-POSIX: stay conservative
        return True
    layout = "hhqqi4x"  # struct flock on 64-bit Linux
    try:
        query = struct.pack(layout, fcntl.F_WRLCK, 0, 0, 0, 0)
        with open(path, "rb") as handle:
            reply = fcntl.fcntl(handle.fileno(), fcntl.F_GETLK, query)
        lock_type = struct.unpack(layout, reply)[0]
    except (OSError, struct.error):
        return True
    return lock_type != fcntl.F_UNLCK


def _singleton_lock_live(path: Path) -> bool:
    """SingletonLock is a symlink ``<host>-<pid>``; stale if that pid is gone."""
    import os
    import socket

    if not path.is_symlink():
        return True  # unknown format: be conservative
    try:
        target = os.readlink(path)
    except OSError:
        return True
    host, _, pid_text = target.rpartition("-")
    if not pid_text.isdigit() or host != socket.gethostname():
        return True
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def profile_in_use(profile_dir: Path) -> bool:
    """True if Chromium appears to still hold this user-data directory.

    Best-effort: a live SingletonLock (or SingletonCookie/Socket), or an
    actively flock-held ``lockfile`` / ``Default/LOCK``. When unsure, refuse
    rather than risk corrupting a live profile.
    """
    root = Path(profile_dir)
    if not root.exists():
        return False
    singleton = root / "SingletonLock"
    if singleton.is_symlink() or singleton.exists():
        if _singleton_lock_live(singleton):
            return True
    for name in ("SingletonCookie", "SingletonSocket"):
        marker = root / name
        if (marker.is_symlink() or marker.exists()) and not (
            singleton.is_symlink() and not _singleton_lock_live(singleton)
        ):
            return True
    for lock in (root / "lockfile", root / "Default" / "LOCK"):
        if lock.exists() and _file_lock_held(lock):
            return True
    return False


def validate_chrome_profile_tree(root: Path) -> None:
    """Require ``root`` looks like a Chromium user-data directory."""
    profile = Path(root)
    local_state = profile / "Local State"
    if not local_state.is_file():
        raise ValueError(
            "Not a Chrome profile: missing 'Local State' file at the profile root."
        )
    default = profile / "Default"
    if not default.is_dir():
        raise ValueError("Not a Chrome profile: missing 'Default/' directory.")
    has_cookies = (
        (default / "Cookies").exists()
        or (default / "Network" / "Cookies").exists()
        or (default / "Local Storage").exists()
    )
    if not has_cookies:
        raise ValueError(
            "Not a Chrome profile: Default/ needs Cookies, Network/Cookies, "
            "or Local Storage."
        )


def _is_unsafe_zip_member(name: str) -> bool:
    if not name or name.startswith("/"):
        return True
    # Normalize separators; reject absolute Windows paths and parent traversal.
    parts = Path(name.replace("\\", "/")).parts
    if any(part == ".." for part in parts):
        return True
    if parts and parts[0].endswith(":"):  # e.g. C:
        return True
    return False


def _looks_like_profile(path: Path) -> bool:
    try:
        validate_chrome_profile_tree(path)
        return True
    except ValueError:
        return False


def _resolve_profile_root(extract_root: Path) -> Path:
    """If zip has a single top-level folder that is the profile, use it."""
    if _looks_like_profile(extract_root):
        return extract_root
    children = [p for p in extract_root.iterdir() if p.name not in {".", ".."}]
    dirs = [p for p in children if p.is_dir()]
    files = [p for p in children if p.is_file()]
    if len(dirs) == 1 and not files and _looks_like_profile(dirs[0]):
        return dirs[0]
    raise ValueError(
        "Zip does not contain a Chrome profile. Expected 'Local State' and "
        "Default/ (with Cookies or Local Storage) at the zip root or inside "
        "a single top-level folder."
    )


def extract_zip_to_temp(
    upload: bytes | BinaryIO | Path,
    tmp_parent: Path,
    *,
    max_compressed: int = MAX_COMPRESSED_BYTES,
    max_uncompressed: int = MAX_UNCOMPRESSED_BYTES,
) -> Path:
    """Unzip safely into a new directory under ``tmp_parent``; return profile root.

    Rejects zip-slip paths, oversized archives, and trees that are not Chrome
    user-data dirs. Caller owns cleanup of the returned tree's parent extract dir.
    """
    tmp_parent = Path(tmp_parent)
    tmp_parent.mkdir(parents=True, exist_ok=True)
    extract_dir = tmp_parent / f"chrome-profile-extract-{uuid.uuid4().hex}"
    extract_dir.mkdir(parents=True, exist_ok=False)

    if isinstance(upload, Path):
        zip_path = upload
        compressed_size = zip_path.stat().st_size
        if compressed_size > max_compressed:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise ValueError(
                f"Zip too large ({compressed_size} bytes); max compressed "
                f"is {max_compressed} bytes."
            )
        zip_source: Path | BinaryIO = zip_path
        close_after = False
    elif isinstance(upload, (bytes, bytearray)):
        compressed_size = len(upload)
        if compressed_size > max_compressed:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise ValueError(
                f"Zip too large ({compressed_size} bytes); max compressed "
                f"is {max_compressed} bytes."
            )
        zip_bytes_path = extract_dir / "_upload.zip"
        zip_bytes_path.write_bytes(bytes(upload))
        zip_source = zip_bytes_path
        close_after = False
    else:
        # File-like: materialize to bound size checks.
        data = upload.read()
        if callable(getattr(upload, "seek", None)):
            try:
                upload.seek(0)
            except (OSError, TypeError):
                pass
        compressed_size = len(data)
        if compressed_size > max_compressed:
            shutil.rmtree(extract_dir, ignore_errors=True)
            raise ValueError(
                f"Zip too large ({compressed_size} bytes); max compressed "
                f"is {max_compressed} bytes."
            )
        zip_bytes_path = extract_dir / "_upload.zip"
        zip_bytes_path.write_bytes(data)
        zip_source = zip_bytes_path
        close_after = False

    try:
        with zipfile.ZipFile(zip_source, "r") as zf:
            # Pre-scan members for slip + uncompressed size.
            total_uncompressed = 0
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if _is_unsafe_zip_member(info.filename):
                    raise ValueError(
                        f"Zip entry rejected (unsafe path): {info.filename!r}"
                    )
                total_uncompressed += int(info.file_size)
                if total_uncompressed > max_uncompressed:
                    raise ValueError(
                        f"Uncompressed zip contents exceed "
                        f"{max_uncompressed} bytes."
                    )
            for info in zf.infolist():
                if _is_unsafe_zip_member(info.filename):
                    raise ValueError(
                        f"Zip entry rejected (unsafe path): {info.filename!r}"
                    )
                target = (extract_dir / info.filename).resolve()
                if not str(target).startswith(str(extract_dir.resolve())):
                    raise ValueError(
                        f"Zip entry rejected (zip slip): {info.filename!r}"
                    )
            zf.extractall(extract_dir)
        # Drop materialized zip if present so it doesn't confuse profile root.
        leftover = extract_dir / "_upload.zip"
        if leftover.exists():
            leftover.unlink()
        profile_root = _resolve_profile_root(extract_dir)
        validate_chrome_profile_tree(profile_root)
        return profile_root
    except Exception:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise
    finally:
        if close_after and hasattr(zip_source, "close"):
            zip_source.close()  # type: ignore[union-attr]


def replace_chrome_profile(dest: Path, source_tree: Path) -> None:
    """Atomically-ish replace ``dest`` with a validated ``source_tree``.

    Stages into ``dest.name.tmp-<uuid>``, renames existing dest to
    ``dest.name.old-<uuid>``, then renames tmp → dest and removes the old tree.
    Restores the old tree if the final rename fails.
    """
    dest = Path(dest)
    source_tree = Path(source_tree)
    validate_chrome_profile_tree(source_tree)

    if profile_in_use(dest):
        raise RuntimeError(
            "Chromium still has this profile open — close the scrape browser and retry"
        )

    parent = dest.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    incoming = parent / f"{dest.name}.tmp-{token}"
    old = parent / f"{dest.name}.old-{token}"

    if incoming.exists():
        shutil.rmtree(incoming)
    if old.exists():
        shutil.rmtree(old)

    shutil.copytree(source_tree, incoming, symlinks=False)
    validate_chrome_profile_tree(incoming)

    moved_old = False
    try:
        if dest.exists():
            dest.rename(old)
            moved_old = True
        incoming.rename(dest)
    except Exception:
        # Best-effort restore.
        if dest.exists() and not moved_old:
            shutil.rmtree(dest, ignore_errors=True)
        if moved_old and old.exists() and not dest.exists():
            try:
                old.rename(dest)
            except OSError:
                pass
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
        raise

    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def meta_path(data_dir: Path) -> Path:
    return Path(data_dir) / META_FILENAME


def write_profile_meta(
    data_dir: Path,
    *,
    source_filename: str,
    bytes_count: int,
    uploaded_at: str | None = None,
) -> dict:
    """Persist upload metadata outside the profile folder (after successful replace)."""
    payload = {
        "uploaded_at": uploaded_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "source_filename": source_filename,
        "bytes": int(bytes_count),
    }
    path = meta_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def read_profile_meta(data_dir: Path) -> dict | None:
    path = meta_path(data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data
