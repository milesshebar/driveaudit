"""Re-check a mounted drive against its catalog entry (read-only; the catalog is not changed)."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from . import db as DB
from .hasher import FileChanged, Root, Stamp, full_hash, partial_hash
from .util import db_to_path, display_path, path_to_db
from .walker import Walker, WalkOptions


@dataclass
class VerifyResult:
    drive: str
    new_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    hash_mismatches: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    content_unverified: list[str] = field(default_factory=list)  # deep: no stored hash at all
    partial_only: list[str] = field(default_factory=list)  # deep: only head+tail was compared
    checked: int = 0

    @property
    def fully_verified(self) -> bool:
        """True if no cataloged file went through a deep check without a full-hash baseline."""
        return not (self.content_unverified or self.partial_only)

    @property
    def ok(self) -> bool:
        return not (
            self.new_files
            or self.missing_files
            or self.modified_files
            or self.hash_mismatches
            or self.unreadable
        )


_NOT_CATALOGED = (
    "{mount} is not the directory that was cataloged (a different folder, or a link to one); "
    "refusing to verify it against this catalog entry"
)


def _check_root_identity(mount: str, row: sqlite3.Row) -> None:
    """The root must be the directory recorded at catalog time. Catalogs from before the inode
    was recorded cannot be checked, so a symlinked root is refused for them."""
    if row["root_ino"] is None:
        if os.path.islink(mount):
            raise DB.CatalogError(
                f"{mount} is a symlink and this catalog entry has no recorded root identity; "
                "re-catalog the drive first"
            )
        return
    if os.stat(mount).st_ino != row["root_ino"]:
        raise DB.CatalogError(_NOT_CATALOGED.format(mount=mount))


def verify_drive(
    conn: sqlite3.Connection,
    drive: str,
    mount_path: str | None = None,
    *,
    deep: bool = False,
    options: WalkOptions | None = None,
) -> VerifyResult:
    """Walk the live drive and compare with the catalog.

    new:      on disk, not cataloged        missing:  cataloged, not on disk
    modified: size or mtime differs         mismatch: (--deep) stored hash differs from content
    A drive that disappears mid-walk raises ``DriveGone``; nothing partial is reported as OK.
    """
    row = DB.require_drive(conn, drive)
    mount = os.path.abspath(mount_path or row["mount_path"])
    if not os.path.isdir(mount):
        raise DB.CatalogError(f"not mounted: {mount}")
    guard = Root(mount, row["root_ino"], not row["cross_devices"])
    _check_root_identity(mount, row)
    cat = {
        r["rel_path"]: r
        for r in conn.execute(
            "SELECT rel_path, file_size, mtime_ns, partial_hash, full_hash FROM files WHERE drive_name = ?",
            (drive,),
        )
    }
    res = VerifyResult(drive)
    seen: set[str] = set()
    walker = Walker(mount, options)
    for e in walker:  # (the walker itself raises DriveGone if the root changes during the walk)
        key = path_to_db(e.rel)
        seen.add(key)
        res.checked += 1
        c = cat.get(key)
        if c is None:
            res.new_files.append(display_path(key))
            continue
        if c["file_size"] != e.size or c["mtime_ns"] != e.mtime_ns:
            res.modified_files.append(display_path(key))
            continue
        if deep and not (c["full_hash"] or c["partial_hash"]):
            res.content_unverified.append(display_path(key))
        elif deep:
            path = os.path.join(mount, db_to_path(key))
            stamp = Stamp(e.size, e.mtime_ns)
            try:
                if c["full_hash"]:
                    bad = full_hash(path, stamp, guard) != c["full_hash"]
                else:
                    bad = partial_hash(path, stamp, guard) != c["partial_hash"]
                    res.partial_only.append(display_path(key))
            except (OSError, FileChanged):
                res.unreadable.append(display_path(key))
                continue
            if bad:
                res.hash_mismatches.append(display_path(key))
    if row["root_ino"] is not None and walker.root_ino != row["root_ino"]:
        raise DB.CatalogError(_NOT_CATALOGED.format(mount=mount))
    res.unreadable += [display_path(path_to_db(p)) for p, _ in walker.stats.errors]
    unreadable_prefixes = tuple(path_to_db(p).rstrip("/") + "/" for p, _ in walker.stats.errors)
    res.missing_files = sorted(
        display_path(k) for k in cat if k not in seen and not k.startswith(unreadable_prefixes)
    )
    return res


def render_text(res: VerifyResult, *, deep: bool, limit: int = 15) -> str:
    out = [
        f"VERIFY {res.drive}: {res.checked:,} files on disk ({'deep' if deep else 'metadata'} check)"
    ]
    if res.ok and deep and not res.fully_verified:
        out.append(
            "NO DIFFERENCES FOUND, but content was NOT verified for every file: "
            f"{len(res.content_unverified):,} have no stored hash and {len(res.partial_only):,} "
            "have only a head+tail hash. Only size and modification time were compared for those."
        )
        for lst in (res.content_unverified, res.partial_only):
            out += [f"      {p}" for p in lst[:limit]]
        out.append(
            "Run `driveaudit hash DRIVE --baseline` (reads every file) to store full hashes, "
            "then verify again."
        )
        return "\n".join(out)
    if res.ok:
        what = "stored hashes and metadata" if deep else "size and modification time"
        out.append(f"PASS - the drive matches the catalog ({what}).")
        return "\n".join(out)
    out.append("DIFFERENCES FOUND - the catalog is out of date or the drive changed:")
    for title, lst in (
        ("MISSING from disk", res.missing_files),
        ("CONTENT DIFFERS from stored hash", res.hash_mismatches),
        ("MODIFIED (size/mtime)", res.modified_files),
        ("NEW, not in catalog", res.new_files),
        ("UNREADABLE", res.unreadable),
    ):
        if lst:
            out.append(f"  {title}: {len(lst):,}")
            out += [f"      {p}" for p in lst[:limit]]
            if len(lst) > limit:
                out.append(f"      ... and {len(lst) - limit:,} more")
    out.append("Re-run `driveaudit catalog` to refresh before trusting any reconcile result.")
    return "\n".join(out)
