"""Catalog a mounted drive: walk it into the database, then optionally hash candidates.

Walking is metadata-only. Each walk gets a generation number; rows not seen in a *complete*
walk are removed afterwards (guarded against truncated listings). Hashes survive a re-walk
only when a file's size and mtime are unchanged. Hashing is limited to files that could
possibly be duplicates (same size as another file; then same partial hash), which keeps it
to a small fraction of a drive.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import db as DB
from .config import BATCH_SIZE
from .hasher import FileChanged, Root, Stamp, full_hash, is_small, partial_hash
from .util import db_to_path, fmt_bytes, is_within, path_to_db
from .volumes import Volume, identify, safe_name
from .walker import DriveGone, Walker, WalkOptions

Log = Callable[[str], None]

# A walk that finds less than this fraction of what was cataloged (when that was a lot) is
# treated as a truncated listing, not as thousands of deletions.
SHRINK_FLOOR = 0.5
SHRINK_MIN_ROWS = 100


@dataclass
class CatalogResult:
    drive_name: str
    files: int = 0
    bytes: int = 0
    complete: bool = False
    removed: int = 0
    errors: int = 0
    symlinks: int = 0
    special: int = 0
    other_mounts: int = 0
    skipped_dirs: int = 0
    skipped_files: int = 0
    partial_hashed: int = 0
    full_hashed: int = 0
    changed_during_hash: int = 0
    warnings: list[str] = field(default_factory=list)


def _same_drive(row: sqlite3.Row, volume: Volume, mount: str) -> bool:
    """Is ``volume`` at ``mount`` the drive already cataloged as ``row``?"""
    old = row["volume_uuid"]
    if volume.uuid and old:
        return volume.uuid == old
    if volume.uuid or old:
        return False  # one side is identifiable and the other is not: do not guess
    return os.path.realpath(row["mount_path"]) == os.path.realpath(mount)


def resolve_name(
    conn: sqlite3.Connection, requested: str | None, volume: Volume, mount: str | None = None
) -> tuple[str, list[str]]:
    """Pick the catalog name for a volume, never merging two different physical drives."""
    warnings: list[str] = []
    name = safe_name(requested or volume.label)
    row = DB.get_drive(conn, name)
    if row is None or _same_drive(row, volume, mount or volume.mount_path):
        return name, warnings
    if requested:
        raise DB.CatalogError(
            f"the name '{name}' is already used by a drive that is not this one (different or "
            "unidentifiable volume, or mounted elsewhere); pick another --name, or "
            f"`driveaudit forget {name}` first if it is the same drive"
        )
    n = 2
    alt = f"{name}-{volume.uuid[:8].lower()}" if volume.uuid else f"{name}-{n}"
    while (r := DB.get_drive(conn, alt)) is not None and not _same_drive(
        r, volume, mount or volume.mount_path
    ):
        n += 1
        alt = f"{name}-{n}"
    warnings.append(f"another drive is already cataloged as '{name}'; using '{alt}' for this one")
    return alt, warnings


def _refuse_overlap(
    conn: sqlite3.Connection, name: str, mount: str, volume: Volume, allow: bool
) -> None:
    """The same physical files under two names would 'cover' themselves; refuse that."""
    if allow:
        return
    real = os.path.realpath(mount)
    for d in DB.get_drives(conn):
        if d["drive_name"] == name:
            continue
        other = os.path.realpath(d["mount_path"])
        nested = (
            real == other
            or real.startswith(other.rstrip("/") + "/")
            or other.startswith(real.rstrip("/") + "/")
        )
        same_volume = bool(volume.uuid) and volume.uuid == d["volume_uuid"]
        if nested or same_volume:
            raise DB.CatalogError(
                f"{mount} overlaps the already-cataloged drive '{d['drive_name']}' "
                f"({d['mount_path']}): the same files would be counted twice and appear to "
                "back each other up. Use that name, or pass --allow-overlap if you are sure"
            )


def catalog_drive(
    conn: sqlite3.Connection,
    mount_path: str,
    *,
    name: str | None = None,
    reference: bool | None = None,
    options: WalkOptions | None = None,
    hash_candidates: bool = False,
    allow_shrink: bool = False,
    allow_overlap: bool = False,
    volume: Volume | None = None,
    db_path: str | Path | None = None,
    log: Log = print,
) -> CatalogResult:
    mount = os.path.abspath(os.path.expanduser(mount_path))
    if not os.path.isdir(mount):
        raise DB.CatalogError(f"not a directory: {mount}")
    if db_path and is_within(db_path, mount):
        raise DB.CatalogError(
            f"the catalog database ({db_path}) is inside the tree being cataloged; "
            "put it somewhere else with --db"
        )
    vol = volume or identify(mount)
    conn.execute(
        "BEGIN IMMEDIATE"
    )  # one write lock across naming, the overlap check and the upsert
    try:
        drive_name, warnings = resolve_name(conn, name, vol, mount)
        _refuse_overlap(conn, drive_name, mount, vol, allow_overlap)
        DB.upsert_drive(
            conn, drive_name, mount, volume_uuid=vol.uuid, is_reference=reference, commit=False
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    for w in warnings:
        log(f"warning: {w}")
    res = CatalogResult(drive_name, warnings=list(warnings))
    log(f"cataloging {drive_name} ({mount})")
    _walk_into_db(conn, drive_name, mount, options or WalkOptions(), allow_shrink, res, log)
    if hash_candidates and res.complete:
        hr = hash_with_references(conn, drive_name, mount, log=log)
        res.partial_hashed, res.full_hashed, res.changed_during_hash = (
            hr.partial_hashed,
            hr.full_hashed,
            hr.changed_during_hash,
        )
    row = DB.require_drive(conn, drive_name)
    res.files, res.bytes = row["total_files"], row["total_bytes"]
    status = "complete" if res.complete else "INCOMPLETE"
    log(
        f"{drive_name}: {res.files:,} files, {fmt_bytes(res.bytes)} ({status}; "
        f"{res.errors} unreadable, {res.symlinks} symlinks skipped)"
    )
    return res


def _walk_into_db(
    conn: sqlite3.Connection,
    name: str,
    mount: str,
    options: WalkOptions,
    allow_shrink: bool,
    res: CatalogResult,
    log: Log,
) -> None:
    options = options or WalkOptions()
    walker = Walker(mount, options)
    conn.execute("BEGIN IMMEDIATE")  # one write transaction: bump the generation and reset state
    conn.execute(
        "UPDATE drives SET walk_gen = walk_gen + 1, walk_complete = 0, catalog_start = ?, "
        "cross_devices = ? WHERE drive_name = ?",
        (time.time(), int(options.cross_devices), name),
    )
    gen = conn.execute("SELECT walk_gen FROM drives WHERE drive_name = ?", (name,)).fetchone()[0]
    prev_rows = conn.execute("SELECT COUNT(*) FROM files WHERE drive_name = ?", (name,)).fetchone()[
        0
    ]
    conn.execute("DELETE FROM walk_errors WHERE drive_name = ?", (name,))
    conn.commit()

    upsert = """
        INSERT INTO files (drive_name, rel_path, file_size, mtime_ns, link_key, seen_gen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (drive_name, rel_path) DO UPDATE SET
            partial_hash = CASE WHEN files.file_size = excluded.file_size
                                 AND files.mtime_ns = excluded.mtime_ns THEN files.partial_hash END,
            full_hash    = CASE WHEN files.file_size = excluded.file_size
                                 AND files.mtime_ns = excluded.mtime_ns THEN files.full_hash END,
            file_size = excluded.file_size,
            mtime_ns  = excluded.mtime_ns,
            link_key  = excluded.link_key,
            seen_gen  = excluded.seen_gen
        WHERE files.seen_gen <= excluded.seen_gen
    """
    batch: list[tuple] = []
    try:
        for e in walker:
            batch.append((name, path_to_db(e.rel), e.size, e.mtime_ns, e.link_key, gen))
            if len(batch) >= BATCH_SIZE:
                if not _write_batch(conn, name, gen, upsert, batch):
                    return _superseded(res, walker, log)
                batch.clear()
        if batch and not _write_batch(conn, name, gen, upsert, batch):
            return _superseded(res, walker, log)
    except DriveGone as e:
        conn.rollback()
        res.warnings.append(
            f"drive disappeared during the walk: {e}; catalog kept as-is (incomplete)"
        )
        log(f"warning: {res.warnings[-1]}")
        _store_walk_meta(conn, name, walker, complete=False, gen=gen)
        res.complete = False
        _copy_stats(res, walker)
        return
    except KeyboardInterrupt:
        conn.rollback()
        _store_walk_meta(conn, name, walker, complete=False, gen=gen)
        raise

    st = walker.stats
    errored = [path_to_db(rel) for rel, _ in st.errors]
    conn.execute("BEGIN IMMEDIATE")  # the check and the sweep below happen under one write lock
    current = conn.execute("SELECT walk_gen FROM drives WHERE drive_name = ?", (name,)).fetchone()[
        0
    ]
    if current != gen:  # another catalog of this drive started meanwhile; it owns the sweep
        conn.rollback()
        return _superseded(res, walker, log)
    for rel, (_, msg) in zip(errored, st.errors, strict=True):
        conn.execute(
            "INSERT INTO walk_errors (drive_name, path, error) VALUES (?, ?, ?)", (name, rel, msg)
        )
    seen = conn.execute(
        "SELECT COUNT(*) FROM files WHERE drive_name = ? AND seen_gen = ?", (name, gen)
    ).fetchone()[0]
    truncated = (prev_rows >= SHRINK_MIN_ROWS and seen < prev_rows * SHRINK_FLOOR) or (
        prev_rows > 0 and seen == 0
    )
    if truncated and not allow_shrink:
        msg = (
            f"walk found only {seen:,} files but {prev_rows:,} were cataloged; looks like a "
            "truncated listing (unmounted share?). Nothing was removed; re-run with "
            "--allow-shrink if the drive really shrank"
        )
        res.warnings.append(msg)
        log(f"warning: {msg}")
        _store_walk_meta(conn, name, walker, complete=False, gen=gen)
        _copy_stats(res, walker)
        return

    # an incomplete walk (unreadable folders) confirms nothing as gone: it removes no rows
    res.removed = 0 if st.errors else _sweep_stale(conn, name, gen, errored)
    _store_walk_meta(conn, name, walker, complete=not st.errors, gen=gen)
    res.complete = not st.errors
    if st.errors:
        res.warnings.append(
            f"{len(st.errors)} directories/files could not be read; the drive is not fully "
            "covered by the catalog"
        )
    _copy_stats(res, walker)


def _copy_stats(res: CatalogResult, walker: Walker) -> None:
    s = walker.stats
    res.errors, res.symlinks, res.special, res.other_mounts = (
        len(s.errors),
        s.symlinks,
        s.special,
        s.other_mounts,
    )
    res.skipped_dirs, res.skipped_files = s.skipped_dirs, s.skipped_files


def _write_batch(conn: sqlite3.Connection, name: str, gen: int, sql: str, batch: list) -> bool:
    """Write one batch of walk rows, unless a newer walk of this drive has started."""
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("SELECT walk_gen FROM drives WHERE drive_name = ?", (name,)).fetchone()
    if cur is None or cur[0] != gen:
        conn.rollback()
        return False
    conn.executemany(sql, batch)
    conn.commit()
    return True


def _superseded(res: CatalogResult, walker: Walker, log: Log) -> None:
    msg = (
        "another catalog of this drive ran at the same time; this walk wrote nothing more "
        "and the result is marked incomplete. Re-run `catalog` once, alone"
    )
    res.warnings.append(msg)
    log(f"warning: {msg}")
    res.complete = False
    _copy_stats(res, walker)


def _store_walk_meta(
    conn: sqlite3.Connection, name: str, walker: Walker, *, complete: bool, gen: int
) -> None:
    """Record walk results in one write transaction, only if this walk is still the newest.

    The generation check and every write share the lock (``BEGIN IMMEDIATE``, or the one the
    caller already holds), and each update is also conditioned on the generation.
    """
    s = walker.stats
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("SELECT walk_gen FROM drives WHERE drive_name = ?", (name,)).fetchone()
    if cur is None or cur[0] != gen:
        conn.rollback()
        return
    DB.refresh_totals(conn, name, commit=False)
    conn.execute(
        """UPDATE drives SET walk_complete = ?, catalog_end = ?, skipped_symlinks = ?,
                  skipped_special = ?, skipped_mounts = ?, skipped_dirs = ?, skipped_files = ?,
                  error_count = ?, root_ino = COALESCE(?, root_ino)
           WHERE drive_name = ? AND walk_gen = ?""",
        (
            int(complete),
            time.time(),
            s.symlinks,
            s.special,
            s.other_mounts,
            s.skipped_dirs,
            s.skipped_files,
            len(s.errors),
            walker.root_ino,
            name,
            gen,
        ),
    )
    conn.commit()


def _sweep_stale(conn: sqlite3.Connection, name: str, gen: int, errored: list[str]) -> int:
    """Delete rows a complete walk did not see, except under directories it could not read."""
    stale = conn.execute(
        "SELECT id, rel_path FROM files WHERE drive_name = ? AND seen_gen != ?", (name, gen)
    ).fetchall()
    if "." in errored:  # the root itself was unreadable: nothing can be concluded
        return 0
    exact = set(errored)
    prefixes = tuple(e.rstrip("/") + "/" for e in errored)
    doomed = []
    for r in stale:
        rel = r["rel_path"]
        if rel in exact or rel.startswith(prefixes):
            continue
        doomed.append((r["id"],))
    conn.executemany("DELETE FROM files WHERE id = ?", doomed)
    return len(doomed)


# --- hashing --------------------------------------------------------------------------------


@dataclass
class HashResult:
    partial_hashed: int = 0
    full_hashed: int = 0
    changed_during_hash: int = 0
    unreadable: int = 0
    stopped_early: bool = False


def _partial_candidates(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.id, f.rel_path, f.file_size, f.mtime_ns FROM files f
        WHERE f.drive_name = ? AND f.partial_hash IS NULL
          AND EXISTS (SELECT 1 FROM files g WHERE g.file_size = f.file_size AND g.id != f.id)
        ORDER BY f.file_size DESC
        """,
        (name,),
    ).fetchall()


def _full_candidates(conn: sqlite3.Connection, name: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT f.id, f.rel_path, f.file_size, f.mtime_ns FROM files f
        WHERE f.drive_name = ? AND f.partial_hash IS NOT NULL AND f.full_hash IS NULL
          AND EXISTS (SELECT 1 FROM files g WHERE g.file_size = f.file_size
                      AND g.partial_hash = f.partial_hash AND g.id != f.id)
        ORDER BY f.file_size DESC
        """,
        (name,),
    ).fetchall()


def hash_drive(
    conn: sqlite3.Connection,
    name: str,
    mount_path: str,
    *,
    expected_uuid_check: bool = True,
    stages: tuple[str, ...] = ("partial", "full"),
    log: Log = print,
) -> HashResult:
    """Partial-hash then full-hash the candidate files of one mounted drive.

    When hashing several drives, run stage "partial" on all of them before stage "full" on
    any (a full-hash candidate is a file whose partial hash matches one on another drive).

    A hash is stored only if the file still has the size and mtime it was cataloged with, so
    pointing this at the wrong volume (or a drive that changed) stores nothing wrong.
    """
    drive = DB.require_drive(conn, name)
    root = os.path.abspath(mount_path)
    if not os.path.isdir(root):
        raise DB.CatalogError(f"not mounted: {root}")
    if expected_uuid_check and drive["volume_uuid"]:
        now = identify(root).uuid
        if now and now != drive["volume_uuid"]:
            raise DB.CatalogError(
                f"{root} is a different volume than the one cataloged as '{name}'; not hashing"
            )
    res = HashResult()
    root_dev = os.stat(root).st_dev
    guard = Root(root, drive["root_ino"], not drive["cross_devices"])

    def run(rows: list[sqlite3.Row], kind: str) -> None:
        pending_commit = 0
        consecutive_errors = 0
        for i, r in enumerate(rows, 1):
            if consecutive_errors >= 5:
                if not _root_alive(root, root_dev):
                    log(f"warning: {root} went away; stopping. Hashes so far are saved.")
                    res.stopped_early = True
                    break
                consecutive_errors = 0
            rel = db_to_path(r["rel_path"])
            path = os.path.join(root, rel)
            stamp = Stamp(r["file_size"], r["mtime_ns"])
            try:
                if kind == "partial":
                    h = partial_hash(path, stamp, guard)
                    if is_small(r["file_size"]):
                        cur = conn.execute(
                            "UPDATE files SET partial_hash = ?, full_hash = ? "
                            "WHERE id = ? AND file_size = ? AND mtime_ns = ?",
                            (h, h, r["id"], r["file_size"], r["mtime_ns"]),
                        )
                    else:
                        cur = conn.execute(
                            "UPDATE files SET partial_hash = ? "
                            "WHERE id = ? AND file_size = ? AND mtime_ns = ?",
                            (h, r["id"], r["file_size"], r["mtime_ns"]),
                        )
                    if cur.rowcount == 0:
                        raise FileChanged("catalog row changed while hashing")
                    res.partial_hashed += 1
                elif kind == "baseline":
                    h = full_hash(path, stamp, guard)
                    p = h if is_small(r["file_size"]) else partial_hash(path, stamp, guard)
                    cur = conn.execute(
                        "UPDATE files SET partial_hash = ?, full_hash = ? "
                        "WHERE id = ? AND file_size = ? AND mtime_ns = ?",
                        (p, h, r["id"], r["file_size"], r["mtime_ns"]),
                    )
                    if cur.rowcount == 0:
                        raise FileChanged("catalog row changed while hashing")
                    res.full_hashed += 1
                else:
                    h = full_hash(path, stamp, guard)
                    cur = conn.execute(
                        "UPDATE files SET full_hash = ? "
                        "WHERE id = ? AND file_size = ? AND mtime_ns = ? AND partial_hash IS NOT NULL",
                        (h, r["id"], r["file_size"], r["mtime_ns"]),
                    )
                    if cur.rowcount == 0:
                        raise FileChanged("catalog row changed while hashing")
                    res.full_hashed += 1
                consecutive_errors = 0
            except FileChanged:
                res.changed_during_hash += 1
            except OSError:
                res.unreadable += 1
                consecutive_errors += 1
            pending_commit += 1
            if pending_commit >= 100:
                conn.commit()
                pending_commit = 0
            if i % 2000 == 0:
                log(f"  {kind} hashing {name}: {i:,}/{len(rows):,}")
        conn.commit()

    if "partial" in stages:
        rows = _partial_candidates(conn, name)
        log(f"partial-hashing {len(rows):,} size-collision files on {name}")
        run(rows, "partial")
    if "full" in stages and not res.stopped_early:
        rows = _full_candidates(conn, name)
        log(f"full-hashing {len(rows):,} partial-collision files on {name}")
        run(rows, "full")
    if "baseline" in stages and not res.stopped_early:
        rows = conn.execute(
            "SELECT id, rel_path, file_size, mtime_ns FROM files "
            "WHERE drive_name = ? AND full_hash IS NULL",
            (name,),
        ).fetchall()
        log(f"baseline: full-hashing {len(rows):,} files on {name} that have no full hash")
        run(rows, "baseline")
    if res.changed_during_hash:
        log(
            f"note: {res.changed_during_hash} files changed since cataloged and were not hashed; re-catalog"
        )
    return res


def hash_with_references(
    conn: sqlite3.Connection, name: str, mount: str, *, log: Log = print
) -> HashResult:
    """Hash a just-cataloged drive plus any reference drive that is mounted right now.

    A new drive's files can only be *verified* against the reference if both sides have full
    hashes, and a full hash needs the file readable, so the mounted references are hashed too
    (partial pass on every drive first, then the full pass).
    """
    targets = [(name, mount, False)]
    for ref in DB.get_drives(conn):
        if ref["is_reference"] and ref["drive_name"] != name and os.path.isdir(ref["mount_path"]):
            targets.append((ref["drive_name"], ref["mount_path"], True))
    total = HashResult()
    for stage in ("partial", "full"):
        for tname, tmount, check_uuid in targets:
            try:
                r = hash_drive(
                    conn, tname, tmount, expected_uuid_check=check_uuid, stages=(stage,), log=log
                )
            except DB.CatalogError as e:
                log(f"warning: skipping {tname}: {e}")
                continue
            total.partial_hashed += r.partial_hashed
            total.full_hashed += r.full_hashed
            total.changed_during_hash += r.changed_during_hash
            total.unreadable += r.unreadable
            total.stopped_early |= r.stopped_early
    return total


def _root_alive(root: str, root_dev: int) -> bool:
    try:
        return os.stat(root).st_dev == root_dev
    except OSError:
        return False


def pending_counts(conn: sqlite3.Connection, name: str) -> tuple[int, int]:
    """(files still needing a partial hash, files still needing a full hash) for one drive.

    Only real candidates count: files that share a size (or a partial hash) with another file.
    """
    return len(_partial_candidates(conn, name)), len(_full_candidates(conn, name))
