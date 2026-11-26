"""Settings: built-in defaults, overridden by a TOML file, environment, then CLI flags.

Nothing here is machine-specific. The database path resolves in this order:
``--db`` flag, ``$DRIVEAUDIT_DB``, ``db`` in the config file, then the per-user data dir.
The config file is the first of: ``--config``, ``$DRIVEAUDIT_CONFIG``,
``$XDG_CONFIG_HOME/driveaudit/config.toml`` (default ``~/.config/driveaudit/config.toml``).
A ``driveaudit.toml`` in the current directory is NOT picked up automatically.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Directories that are never walked (OS and NAS housekeeping, not user data).
DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".Spotlight-V100",
        ".Trashes",
        ".fseventsd",
        ".TemporaryItems",
        ".DocumentRevisions-V100",
        ".com.apple.timemachine.donotpresent",
        ".MobileBackups",
        ".vol",
        "System Volume Information",
        "$RECYCLE.BIN",
        "@Recycle",
        "@Recently-Snapshot",
        "@Transcode",
        ".@__thumb",
        "@sharebin",
    }
)
# Time Machine trees are skipped unless asked for (huge, and all hard links).
TIME_MACHINE_DIRS: frozenset[str] = frozenset({".timemachine", "Backups.backupdb"})

DEFAULT_SKIP_FILES: frozenset[str] = frozenset(
    {".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "Icon\r"}
)
DEFAULT_SKIP_EXTENSIONS: frozenset[str] = frozenset({".tmp", ".part"})

DEFAULT_WATCH_ROOTS: tuple[str, ...] = ("/Volumes",)

PARTIAL_CHUNK = 8192  # bytes read from each end for a partial hash
SMALL_FILE = PARTIAL_CHUNK * 2  # at or below this a partial hash covers the whole file
FULL_CHUNK = 1 << 20
BATCH_SIZE = 1000


def default_db_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "driveaudit" / "catalog.db"


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "driveaudit" / "config.toml"


@dataclass
class Settings:
    db: Path = field(default_factory=default_db_path)
    reference: list[str] = field(default_factory=list)
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    skip_files: frozenset[str] = DEFAULT_SKIP_FILES
    skip_extensions: frozenset[str] = DEFAULT_SKIP_EXTENSIONS
    include_time_machine: bool = False
    cross_devices: bool = False
    watch_roots: tuple[str, ...] = DEFAULT_WATCH_ROOTS
    watch_interval: float = 5.0
    watch_settle: float = 3.0
    watch_hash: bool = False
    watch_ignore: tuple[str, ...] = ()
    source: Path | None = None  # config file actually loaded, if any


def find_config(explicit: str | None = None) -> Path | None:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"config file not found: {p}")
        return p
    env = os.environ.get("DRIVEAUDIT_CONFIG")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"$DRIVEAUDIT_CONFIG points at a missing file: {p}")
        return p
    # Deliberately no lookup in the current directory: a config found there could have come
    # from the very drive being cataloged and silently exclude its files.
    cand = default_config_path()
    return cand if cand.is_file() else None


def _strs(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"config key '{key}' must be a list of strings")
    return value


def _bool(table: dict, key: str, default: bool) -> bool:
    v = table.get(key, default)
    if not isinstance(v, bool):
        raise ValueError(f"config key '{key}' must be true or false")
    return v


def load_settings(config: str | None = None, db: str | None = None) -> Settings:
    s = Settings()
    path = find_config(config)
    if path is not None:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        s.source = path
        if "db" in data:
            s.db = Path(str(data["db"])).expanduser()
        if "reference" in data:
            s.reference = _strs(data["reference"], "reference")
        scan = data.get("scan", {})
        if "extra_skip_dirs" in scan:
            s.skip_dirs |= frozenset(_strs(scan["extra_skip_dirs"], "scan.extra_skip_dirs"))
        if "extra_skip_files" in scan:
            s.skip_files |= frozenset(_strs(scan["extra_skip_files"], "scan.extra_skip_files"))
        if "extra_skip_extensions" in scan:
            s.skip_extensions |= frozenset(
                e.lower()
                for e in _strs(scan["extra_skip_extensions"], "scan.extra_skip_extensions")
            )
        s.include_time_machine = _bool(scan, "include_time_machine", False)
        s.cross_devices = _bool(scan, "cross_devices", False)
        watch = data.get("watch", {})
        if "roots" in watch:
            s.watch_roots = tuple(_strs(watch["roots"], "watch.roots"))
        s.watch_interval = float(watch.get("interval", s.watch_interval))
        s.watch_settle = float(watch.get("settle", s.watch_settle))
        s.watch_hash = _bool(watch, "hash", False)
        if "ignore" in watch:
            s.watch_ignore = tuple(_strs(watch["ignore"], "watch.ignore"))
    env_db = os.environ.get("DRIVEAUDIT_DB")
    if env_db:
        s.db = Path(env_db).expanduser()
    if db:
        s.db = Path(db).expanduser()
    return s
