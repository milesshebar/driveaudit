"""Find duplicate files across (and within) cataloged drives."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from . import db as DB
from .matching import Evidence, FileRec
from .util import display_path, fmt_bytes


@dataclass
class DuplicateGroup:
    evidence: Evidence  # VERIFIED (full hash) or PROBABLE (partial hash only)
    size: int
    key: str
    copies: list[FileRec]
    unverified: list[FileRec] = field(default_factory=list)  # partial-hash match, no full hash yet

    @property
    def storage_units(self) -> int:
        """Distinct physical copies: hard links to one inode count once."""
        seen = set()
        for f in self.copies:
            seen.add((f.drive, f.link_key) if f.link_key else (f.drive, f.rel))
        return len(seen)

    @property
    def wasted_bytes(self) -> int:
        return self.size * max(0, self.storage_units - 1)

    @property
    def drives(self) -> list[str]:
        return sorted({f.drive for f in self.copies})


@dataclass
class DedupeReport:
    groups: list[DuplicateGroup]
    weak_groups: list[list[FileRec]]  # name+size only, no hashes at all
    unhashed_candidates: int
    unhashed_bytes: int
    warnings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> list[DuplicateGroup]:
        return [g for g in self.groups if g.evidence == Evidence.VERIFIED]

    @property
    def probable(self) -> list[DuplicateGroup]:
        return [g for g in self.groups if g.evidence == Evidence.PROBABLE]

    def wasted(self, evidence: Evidence) -> int:
        return sum(g.wasted_bytes for g in self.groups if g.evidence == evidence)


def find_duplicates(
    conn: sqlite3.Connection, *, min_size: int = 1, weak: bool = False
) -> DedupeReport:
    rows = conn.execute(
        """SELECT drive_name, rel_path, file_size, mtime_ns, partial_hash, full_hash, link_key
           FROM files WHERE file_size >= ?""",
        (min_size,),
    )
    by_full: dict[tuple[int, str], list[FileRec]] = defaultdict(list)
    nofull_by_partial: dict[tuple[int, str], list[FileRec]] = defaultdict(list)
    unhashed: list[FileRec] = []
    for r in rows:
        f = FileRec(*tuple(r))
        if f.full:
            by_full[(f.size, f.full)].append(f)
        elif f.partial:
            nofull_by_partial[(f.size, f.partial)].append(f)
        else:
            unhashed.append(f)

    groups: list[DuplicateGroup] = []
    verified_partials: dict[tuple[int, str], DuplicateGroup] = {}
    for (size, full), members in by_full.items():
        if len(members) < 2:
            continue
        g = DuplicateGroup(Evidence.VERIFIED, size, full, sorted(members, key=_sort_key))
        groups.append(g)
        if members[0].partial:
            verified_partials.setdefault((size, members[0].partial), g)

    for (size, partial), members in nofull_by_partial.items():
        owner = verified_partials.get((size, partial))
        if owner is not None:
            owner.unverified.extend(members)
        elif len(members) >= 2:
            groups.append(
                DuplicateGroup(Evidence.PROBABLE, size, partial, sorted(members, key=_sort_key))
            )

    weak_groups: list[list[FileRec]] = []
    if weak:
        by_name: dict[tuple[int, str], list[FileRec]] = defaultdict(list)
        for f in unhashed:
            by_name[(f.size, f.name)].append(f)
        weak_groups = [sorted(v, key=_sort_key) for v in by_name.values() if len(v) >= 2]
        weak_groups.sort(key=lambda g: -g[0].size * len(g))

    size_counts: dict[int, int] = defaultdict(int)
    for (size, _), v in by_full.items():
        size_counts[size] += len(v)
    for (size, _), v in nofull_by_partial.items():
        size_counts[size] += len(v)
    for f in unhashed:
        size_counts[f.size] += 1
    cand = [f for f in unhashed if size_counts[f.size] > 1]

    groups.sort(key=lambda g: (-g.wasted_bytes, g.key))
    warnings = [
        f"drive '{d['drive_name']}' catalog is incomplete; its duplicates may be missing or stale"
        for d in DB.get_drives(conn)
        if not d["walk_complete"]
    ]
    warnings += [
        f"'{a}' and '{b}' are the same or nested mount points; their 'duplicates' are the same files"
        for a, b in DB.overlapping_drives(conn)
    ]
    warnings.append(
        "catalog may be out of date: run `driveaudit verify` on a drive before acting on this list"
    )
    return DedupeReport(groups, weak_groups, len(cand), sum(f.size for f in cand), warnings)


def _sort_key(f: FileRec) -> tuple[str, str]:
    return (f.drive, f.rel)


def render_text(report: DedupeReport, *, limit: int = 20) -> str:
    out: list[str] = []
    w = out.append
    w("DUPLICATES")
    w("=" * 70)
    for warn in report.warnings:
        w(f"! {warn}")
    w(
        f"verified (full SHA-256 match): {len(report.verified):,} groups, "
        f"{fmt_bytes(report.wasted(Evidence.VERIFIED))} reclaimable"
    )
    w(
        f"probable (head+tail hash only): {len(report.probable):,} groups, "
        f"{fmt_bytes(report.wasted(Evidence.PROBABLE))} (NOT proven identical)"
    )
    if report.unhashed_candidates:
        w(
            f"unhashed size collisions: {report.unhashed_candidates:,} files "
            f"({fmt_bytes(report.unhashed_bytes)}); run `driveaudit hash` with those drives mounted"
        )
    if report.weak_groups:
        w(f"name+size only (no hashes): {len(report.weak_groups):,} groups (a hint, not evidence)")
    for title, groups in (("VERIFIED", report.verified), ("PROBABLE", report.probable)):
        if not groups:
            continue
        w("")
        w(f"{title} - top {min(limit, len(groups))} by reclaimable space")
        w("-" * 70)
        for g in groups[:limit]:
            w(
                f"{fmt_bytes(g.size)} x {g.storage_units} = {fmt_bytes(g.wasted_bytes)} reclaimable "
                f"on {', '.join(g.drives)}"
            )
            for f in g.copies[:5]:
                w(f"    {f.drive}: {display_path(f.rel)}")
            if len(g.copies) > 5:
                w(f"    ... and {len(g.copies) - 5} more")
            if g.unverified:
                w(f"    (+{len(g.unverified)} partial-hash matches awaiting a full hash)")
    if report.weak_groups:
        w("")
        w(f"NAME+SIZE ONLY - top {min(limit, len(report.weak_groups))}")
        w("-" * 70)
        for grp in report.weak_groups[:limit]:
            w(f"{fmt_bytes(grp[0].size)} x {len(grp)}: {grp[0].name}")
            for f in grp[:4]:
                w(f"    {f.drive}: {display_path(f.rel)}")
    return "\n".join(out)


def render_json(report: DedupeReport) -> str:
    def copy(f: FileRec) -> dict:
        return {"drive": f.drive, "path": display_path(f.rel)}

    data = {
        "summary": {
            "verified_groups": len(report.verified),
            "verified_reclaimable_bytes": report.wasted(Evidence.VERIFIED),
            "probable_groups": len(report.probable),
            "probable_reclaimable_bytes": report.wasted(Evidence.PROBABLE),
            "unhashed_size_collision_files": report.unhashed_candidates,
            "unhashed_size_collision_bytes": report.unhashed_bytes,
        },
        "warnings": report.warnings,
        "groups": [
            {
                "evidence": g.evidence.label,
                "size": g.size,
                "hash": g.key,
                "reclaimable_bytes": g.wasted_bytes,
                "copies": [copy(f) for f in g.copies],
                "unverified": [copy(f) for f in g.unverified],
            }
            for g in report.groups
        ],
        "name_size_only": [[copy(f) for f in grp] for grp in report.weak_groups],
    }
    return json.dumps(data, indent=2, ensure_ascii=False)
