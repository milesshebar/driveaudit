"""SQLite catalog: schema, connection, and the few queries everything else shares."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS drives (
    drive_name        TEXT PRIMARY KEY,
    mount_path        TEXT NOT NULL,
    volume_uuid       TEXT,
    is_reference      INTEGER NOT NULL DEFAULT 0,
    walk_gen          INTEGER NOT NULL DEFAULT 0,
    walk_complete     INTEGER NOT NULL DEFAULT 0,
    catalog_start     REAL,
    catalog_end       REAL,
    total_files       INTEGER NOT NULL DEFAULT 0,
    total_bytes       INTEGER NOT NULL DEFAULT 0,
    skipped_symlinks  INTEGER NOT NULL DEFAULT 0,
    skipped_special   INTEGER NOT NULL DEFAULT 0,
    skipped_mounts    INTEGER NOT NULL DEFAULT 0,
    skipped_dirs      INTEGER NOT NULL DEFAULT 0,
    skipped_files     INTEGER NOT NULL DEFAULT 0,
    error_count       INTEGER NOT NULL DEFAULT 0,
    notes             TEXT,
    root_ino          INTEGER,        -- inode of the cataloged root; hashing/verify must find the same one
    cross_devices     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_name    TEXT    NOT NULL REFERENCES drives(drive_name),
    rel_path      TEXT    NOT NULL,
    file_size     INTEGER NOT NULL,
    mtime_ns      INTEGER NOT NULL,
    link_key      TEXT,            -- "dev:ino" when st_nlink > 1 (hard links), else NULL
    partial_hash  TEXT,            -- SHA-256 of first 8 KiB + last 8 KiB; NULL = not computed
    full_hash     TEXT,            -- SHA-256 of the whole file; NULL = not computed
    seen_gen      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (drive_name, rel_path)
);

CREATE TABLE IF NOT EXISTS walk_errors (
    drive_name TEXT NOT NULL,
    path       TEXT NOT NULL,
    error      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_size    ON files(file_size);
CREATE INDEX IF NOT EXISTS idx_files_partial ON files(file_size, partial_hash) WHERE partial_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_files_full    ON files(file_size, full_hash)    WHERE full_hash    IS NOT NULL;
"""


class CatalogError(Exception):
    """A problem the user should read about, not a traceback."""


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    p = Path(db_path)
    if not p.parent.exists():
        p.parent.mkdir(parents=True, mode=0o700)  # the catalog lists every path on every drive
    old_umask = os.umask(0o077)  # new DB / WAL / SHM files are created owner-only
    try:
        conn = sqlite3.connect(str(p), timeout=60)
        conn.execute("PRAGMA schema_version").fetchone()  # force the file into existence
    finally:
        os.umask(old_umask)
    conn.row_factory = sqlite3.Row
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise CatalogError(
                f"{p} was written by a newer driveaudit (schema {version} > {SCHEMA_VERSION})"
            )
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")  # lock first, inspect under the lock, migrate in it
        have = {r[1] for r in conn.execute("PRAGMA table_info(drives)")}
        if "root_ino" not in have:  # catalogs made before these columns existed
            conn.execute("ALTER TABLE drives ADD COLUMN root_ino INTEGER")
        if "cross_devices" not in have:
            conn.execute("ALTER TABLE drives ADD COLUMN cross_devices INTEGER NOT NULL DEFAULT 0")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        yield conn
    except sqlite3.DatabaseError as e:
        raise CatalogError(f"catalog database problem ({p}): {e}") from e
    finally:
        conn.close()


# --- drives ---------------------------------------------------------------------------------


def get_drive(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM drives WHERE drive_name = ?", (name,)).fetchone()


def get_drives(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM drives ORDER BY is_reference DESC, drive_name").fetchall()


def require_drive(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = get_drive(conn, name)
    if row is None:
        known = ", ".join(r["drive_name"] for r in get_drives(conn)) or "none"
        raise CatalogError(f"drive '{name}' is not in the catalog (known: {known})")
    return row


def upsert_drive(
    conn: sqlite3.Connection,
    name: str,
    mount_path: str,
    *,
    volume_uuid: str | None = None,
    is_reference: bool | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO drives (drive_name, mount_path, volume_uuid, is_reference, catalog_start)
        VALUES (?, ?, ?, COALESCE(?, 0), ?)
        ON CONFLICT(drive_name) DO UPDATE SET
            mount_path   = excluded.mount_path,
            volume_uuid  = COALESCE(excluded.volume_uuid, drives.volume_uuid),
            is_reference = COALESCE(?, drives.is_reference),
            catalog_start = excluded.catalog_start
        """,
        (
            name,
            mount_path,
            volume_uuid,
            None if is_reference is None else int(is_reference),
            time.time(),
            None if is_reference is None else int(is_reference),
        ),
    )
    if commit:
        conn.commit()


def set_reference(conn: sqlite3.Connection, name: str, value: bool) -> None:
    require_drive(conn, name)
    conn.execute("UPDATE drives SET is_reference = ? WHERE drive_name = ?", (int(value), name))
    conn.commit()


def reference_names(conn: sqlite3.Connection, extra: Iterable[str] = ()) -> list[str]:
    names = [
        r["drive_name"]
        for r in conn.execute("SELECT drive_name FROM drives WHERE is_reference = 1")
    ]
    for n in extra:
        if n not in names:
            names.append(n)
    return sorted(names)


def forget_drive(conn: sqlite3.Connection, name: str) -> None:
    """Remove a drive from the catalog (the catalog only; the drive itself is never touched)."""
    conn.execute("DELETE FROM files WHERE drive_name = ?", (name,))
    conn.execute("DELETE FROM walk_errors WHERE drive_name = ?", (name,))
    conn.execute("DELETE FROM drives WHERE drive_name = ?", (name,))
    conn.commit()
    conn.execute("VACUUM")  # actually drop the forgotten paths from the file


def refresh_totals(conn: sqlite3.Connection, name: str, *, commit: bool = True) -> None:
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(file_size), 0) AS s FROM files WHERE drive_name = ?",
        (name,),
    ).fetchone()
    conn.execute(
        "UPDATE drives SET total_files = ?, total_bytes = ? WHERE drive_name = ?",
        (row["n"], row["s"], name),
    )
    if commit:
        conn.commit()


def overlapping_drives(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Pairs of cataloged drives whose mount points are the same place or nested in one another,
    or that carry the same volume UUID.

    Such pairs describe the same physical files twice, so one would falsely "cover" the other.
    """
    rows = [
        (r["drive_name"], os.path.realpath(r["mount_path"]), r["volume_uuid"])
        for r in get_drives(conn)
    ]
    out = []
    for i, (a, pa, ua) in enumerate(rows):
        for b, pb, ub in rows[i + 1 :]:
            if (
                (ua and ua == ub)  # one physical volume registered twice under different paths
                or pa == pb
                or pa.startswith(pb.rstrip("/") + "/")
                or pb.startswith(pa.rstrip("/") + "/")
            ):
                out.append((a, b))
    return out
