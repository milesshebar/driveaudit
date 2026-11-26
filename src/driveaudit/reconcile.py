"""Reconcile one drive against the declared source of truth ("reference" drives).

For every file on the drive: is there a copy on a reference drive, and how sure are we?
Nothing is changed anywhere. "Covered" means "a reference copy exists with at least the
required evidence", which is a statement about the catalog; ``--live`` re-checks the
reference copies on disk and ``verify`` re-checks the drive itself.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import db as DB
from .hasher import Root, stat_beneath
from .matching import Evidence, FileRec, ReferenceIndex, load_files
from .util import db_to_path, display_path, fmt_bytes, printable


@dataclass
class Item:
    rec: FileRec
    evidence: Evidence
    match: FileRec | None
    note: str = ""


@dataclass
class ReconcileResult:
    drive: str
    references: list[str]
    min_evidence: Evidence
    items: list[Item]
    warnings: list[str] = field(default_factory=list)
    live_checked: bool = False
    live_unchecked: int = 0

    def covered(self, item: Item) -> bool:
        return item.evidence >= self.min_evidence

    def bucket(self, kind: str) -> list[Item]:
        if kind == "covered":
            return [i for i in self.items if self.covered(i)]
        if kind == "only-here":
            return [i for i in self.items if i.evidence == Evidence.NONE]
        if kind == "unconfirmed":
            return [i for i in self.items if Evidence.NONE < i.evidence < self.min_evidence]
        if kind == "not-covered":
            return [i for i in self.items if not self.covered(i)]
        raise ValueError(f"unknown list kind: {kind}")

    def level(self, ev: Evidence) -> list[Item]:
        return [i for i in self.items if i.evidence == ev]

    @property
    def fully_covered(self) -> bool:
        return not self.bucket("not-covered") and not self.blockers

    @property
    def blockers(self) -> list[str]:
        return [w for w in self.warnings if w.startswith("BLOCKER")]


def _age(ts: float | None) -> str:
    if not ts:
        return "never"
    days = (time.time() - ts) / 86400
    return f"{days:.1f} days ago" if days >= 1 else f"{days * 24:.1f} hours ago"


def reconcile(
    conn: sqlite3.Connection,
    drive: str,
    references: list[str],
    *,
    min_evidence: Evidence = Evidence.VERIFIED,
    live: bool = False,
) -> ReconcileResult:
    d = DB.require_drive(conn, drive)
    if not references:
        raise DB.CatalogError(
            "no reference (source of truth) drives: mark one with `driveaudit mark NAME "
            "--reference`, set `reference` in the config, or pass --against NAME"
        )
    if drive in references:
        raise DB.CatalogError(f"'{drive}' is itself a reference drive; pick a different drive")
    if min_evidence not in (Evidence.VERIFIED, Evidence.PROBABLE):
        raise DB.CatalogError("min evidence must be 'verified' or 'probable'")
    ref_rows = [DB.require_drive(conn, r) for r in references]

    warnings: list[str] = []
    if not d["walk_complete"]:
        warnings.append(
            f"BLOCKER: the catalog of '{drive}' is incomplete (interrupted, unreadable "
            "folders or a truncated listing); files not in the catalog are not accounted for"
        )
    for a, b in DB.overlapping_drives(conn):
        other = b if a == drive else a if b == drive else None
        if other in references:
            warnings.append(
                f"BLOCKER: '{drive}' and reference '{other}' are the same or nested mount "
                "points; the same files would be counted as their own backup"
            )
    skipped = (
        ("symlinks", d["skipped_symlinks"]),
        ("special files", d["skipped_special"]),
        ("other-volume folders", d["skipped_mounts"]),
        ("folders matched by skip rules (e.g. .Trashes, Time Machine)", d["skipped_dirs"]),
        ("files matched by skip rules (e.g. .DS_Store, *.tmp)", d["skipped_files"]),
    )
    parts = [f"{n:,} {label}" for label, n in skipped if n]
    if parts:
        warnings.append(f"'{drive}': not cataloged, so not covered: " + "; ".join(parts))
    warnings.append(
        f"'{drive}' was last cataloged {_age(d['catalog_end'])}; run `driveaudit verify`"
    )
    for r in ref_rows:
        if not r["walk_complete"]:
            warnings.append(
                f"BLOCKER: reference '{r['drive_name']}' catalog is incomplete; it may still list "
                "files that are gone, so its copies cannot be relied on. Re-catalog it"
            )
        warnings.append(
            f"reference '{r['drive_name']}' was last cataloged {_age(r['catalog_end'])}"
        )

    ref_files: list[FileRec] = []
    for name in references:
        ref_files.extend(load_files(conn, name))
    index = ReferenceIndex(ref_files)
    items = []
    for f in load_files(conn, drive):
        ev, match = index.best(f)
        items.append(Item(f, ev, match))
    res = ReconcileResult(drive, sorted(references), min_evidence, items, warnings)
    if live:
        _live_check(
            res,
            {
                r["drive_name"]: Root(r["mount_path"], r["root_ino"], not r["cross_devices"])
                for r in ref_rows
            },
        )
    return res


def _live_check(res: ReconcileResult, mounts: dict[str, Root]) -> None:
    """Re-stat each matched reference file. Demote matches whose reference copy is gone/changed."""
    res.live_checked = True
    for item in res.items:
        if item.evidence < Evidence.PROBABLE or item.match is None:
            continue
        m = item.match
        mount = mounts.get(m.drive)
        if mount is None or not os.path.isdir(mount.path):
            res.live_unchecked += 1
            continue
        try:
            st = stat_beneath(mount, db_to_path(m.rel))
        except OSError:
            st = None
        if st is None or st.st_size != m.size or st.st_mtime_ns != m.mtime_ns:
            item.evidence = Evidence.NONE
            item.note = f"reference copy missing or changed: {m.drive}:{display_path(m.rel)}"
    if res.live_unchecked:
        res.warnings.append(
            f"BLOCKER: {res.live_unchecked} reference matches could not be checked live "
            "(reference not mounted); mount it and re-run, or drop --live"
        )


def render_text(res: ReconcileResult, *, top: int = 15) -> str:
    out: list[str] = []
    w = out.append
    total = sum(i.rec.size for i in res.items)
    w(f"RECONCILE {res.drive}  against  {', '.join(res.references)}")
    w("=" * 70)
    w(
        f"files: {len(res.items):,}  ({fmt_bytes(total)})   required evidence: {res.min_evidence.label}"
    )
    w("")
    for ev, desc in (
        (Evidence.VERIFIED, "copy on reference, full SHA-256 identical"),
        (Evidence.PROBABLE, "copy on reference, head+tail hash identical (middle unchecked)"),
        (Evidence.NAME_SIZE, "same name and size on reference, no hash to confirm"),
        (Evidence.NONE, "ONLY HERE - no match on any reference"),
    ):
        lst = res.level(ev)
        w(
            f"  {ev.label:<10} {len(lst):>9,} files {fmt_bytes(sum(i.rec.size for i in lst)):>10}   {desc}"
        )
    stale = [i for i in res.items if i.note]
    if stale:
        w(f"  ({len(stale)} of the only-here files lost their match in the live check)")
    w("")
    for warn in res.warnings:
        w(f"  ! {printable(warn)}")
    w("")
    nc = res.bucket("not-covered")
    if res.fully_covered:
        w("RESULT: every cataloged file is covered by the reference at the required evidence.")
        w(
            "This is a statement about the catalog. Before retiring anything, run `driveaudit verify`"
        )
        w("on this drive and `--live`/`verify` on the reference. driveaudit never deletes.")
    else:
        w(
            f"RESULT: NOT fully covered - {len(nc):,} files ({fmt_bytes(sum(i.rec.size for i in nc))}) "
            "need attention. Do not retire this drive."
        )
        by_top: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for i in nc:
            top_dir = (
                display_path(i.rec.rel).split("/", 1)[0] if "/" in i.rec.rel else "(top level)"
            )
            by_top[top_dir][0] += 1
            by_top[top_dir][1] += i.rec.size
        w("")
        w("Where they are (top-level folders, largest first):")
        for name, (n, b) in sorted(by_top.items(), key=lambda kv: -kv[1][1])[:top]:
            w(f"  {fmt_bytes(b):>10}  {n:>8,} files  {name}")
        w("")
        w("List them: driveaudit reconcile " + res.drive + " --list only-here|unconfirmed")
    return "\n".join(out)


def render_json(res: ReconcileResult, *, include_files: bool = False) -> str:
    def rows(items: list[Item]) -> list[dict]:
        return [
            {
                "path": display_path(i.rec.rel),
                "size": i.rec.size,
                "evidence": i.evidence.label,
                "match": None
                if i.match is None
                else {"drive": i.match.drive, "path": display_path(i.match.rel)},
                "note": i.note,
            }
            for i in items
        ]

    data = {
        "drive": res.drive,
        "references": res.references,
        "required_evidence": res.min_evidence.label,
        "fully_covered": res.fully_covered,
        "counts": {
            ev.label: {"files": len(res.level(ev)), "bytes": sum(i.rec.size for i in res.level(ev))}
            for ev in (Evidence.VERIFIED, Evidence.PROBABLE, Evidence.NAME_SIZE, Evidence.NONE)
        },
        "warnings": res.warnings,
    }
    if include_files:
        data["not_covered"] = rows(res.bucket("not-covered"))
    return json.dumps(data, indent=2, ensure_ascii=False)
