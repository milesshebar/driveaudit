"""Plan a consolidation copy, and later verify the copy that someone made.

driveaudit never copies files itself. ``build_plan`` writes down what should be copied (every
file that is not already covered on the reference), ready for rsync; ``verify_copy`` then
checks, byte for byte, that the destination really holds each file.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import db as DB
from .hasher import FileChanged, full_hash, stat_beneath
from .matching import Evidence
from .reconcile import reconcile
from .util import db_to_path, display_path, fmt_bytes, is_safe_rel, is_within, printable
from .volumes import volume_uuid

PLAN_VERSION = 1


@dataclass
class PlanOp:
    drive: str
    rel: str  # real relative path on the source drive
    size: int
    full_hash: str | None
    reason: str  # only-here | unconfirmed
    mtime_ns: int | None = None


@dataclass
class Plan:
    dest: str
    created: float
    sources: dict[str, str]  # drive name -> mount path at planning time
    ops: list[PlanOp]
    source_uuids: dict[str, str | None] = field(default_factory=dict)
    covered_by_reference: int = 0
    covered_by_plan: int = 0
    warnings: list[str] = field(default_factory=list)
    dest_free: int | None = None

    @property
    def total_bytes(self) -> int:
        return sum(o.size for o in self.ops)

    @property
    def blockers(self) -> list[str]:
        return [w for w in self.warnings if "BLOCKER" in w]

    @property
    def fits(self) -> bool | None:
        return None if self.dest_free is None else self.total_bytes <= self.dest_free


def dest_free_bytes(dest: str) -> int | None:
    p = Path(dest).expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        st = os.statvfs(p)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def build_plan(
    conn: sqlite3.Connection,
    drives: list[str],
    references: list[str],
    dest: str,
    *,
    min_evidence: Evidence = Evidence.VERIFIED,
    dest_free: int | None = None,
) -> Plan:
    """Copy list for ``drives``: everything the reference does not already cover.

    A file with a verified copy elsewhere in the *same plan* is listed once only. Files
    without a full hash are always copied (no evidence is not coverage).
    """
    dest = os.path.abspath(os.path.expanduser(dest))
    for name in drives:
        mount = DB.require_drive(conn, name)["mount_path"]
        if is_within(dest, mount):
            raise DB.CatalogError(
                f"the destination {dest} is inside the source drive '{name}' ({mount}); "
                "copies made there would be lost when the drive is retired"
            )
    plan = Plan(
        dest=dest,
        created=time.time(),
        sources={},
        ops=[],
        dest_free=dest_free if dest_free is not None else dest_free_bytes(dest),
    )
    planned: dict[tuple[int, str], str] = {}  # (size, full hash) -> "drive:rel"
    for name in drives:
        res = reconcile(conn, name, references, min_evidence=min_evidence)
        row = DB.require_drive(conn, name)
        plan.sources[name] = row["mount_path"]
        plan.source_uuids[name] = row["volume_uuid"]
        plan.warnings += [f"{name}: {w}" for w in res.warnings if w.startswith("BLOCKER")]
        for item in res.items:
            if res.covered(item):
                plan.covered_by_reference += 1
                continue
            f = item.rec
            key = (f.size, f.full) if f.full else None
            if key and key in planned:
                plan.covered_by_plan += 1
                continue
            if key:
                planned[key] = f"{name}:{f.rel}"
            plan.ops.append(
                PlanOp(
                    drive=name,
                    rel=db_to_path(f.rel),
                    size=f.size,
                    full_hash=f.full,
                    reason="only-here" if item.evidence == Evidence.NONE else "unconfirmed",
                    mtime_ns=f.mtime_ns,
                )
            )
    if plan.blockers:
        plan.warnings.insert(
            0, "BLOCKERS above: do not retire a source drive on the strength of this plan"
        )
    if plan.fits is False:
        plan.warnings.append(
            f"destination has {fmt_bytes(plan.dest_free or 0)} free but the plan needs "
            f"{fmt_bytes(plan.total_bytes)}"
        )
    return plan


def plan_to_json(plan: Plan) -> str:
    data = {
        "version": PLAN_VERSION,
        "created": plan.created,
        "dest": plan.dest,
        "layout": "<dest>/<drive name>/<relative path>",
        "sources": plan.sources,
        "source_uuids": plan.source_uuids,
        "covered_by_reference": plan.covered_by_reference,
        "covered_by_plan": plan.covered_by_plan,
        "warnings": plan.warnings,
        "ops": [
            {
                "drive": o.drive,
                "path": o.rel,
                "size": o.size,
                "mtime_ns": o.mtime_ns,
                "full_hash": o.full_hash,
                "reason": o.reason,
            }
            for o in plan.ops
        ],
    }
    return json.dumps(data, indent=2)  # ASCII-escaped: undecodable name bytes round-trip


def plan_from_json(text: str) -> Plan:
    try:
        data = json.loads(text)
        if data.get("version") != PLAN_VERSION:
            raise DB.CatalogError("unsupported plan file version")
        if not isinstance(data["dest"], str) or not isinstance(data["sources"], dict):
            raise TypeError("dest/sources")
        ops = []
        for o in data["ops"]:
            if not (
                isinstance(o["drive"], str)
                and isinstance(o["path"], str)
                and isinstance(o["size"], int)
                and isinstance(o.get("full_hash"), str | type(None))
                and isinstance(o.get("mtime_ns"), int | type(None))
            ):
                raise TypeError("op")
            ops.append(
                PlanOp(
                    o["drive"],
                    o["path"],
                    o["size"],
                    o.get("full_hash"),
                    o.get("reason", ""),
                    o.get("mtime_ns"),
                )
            )
        return Plan(
            dest=data["dest"],
            created=float(data["created"]),
            sources={str(k): str(v) for k, v in data["sources"].items()},
            source_uuids=dict(data.get("source_uuids", {})),
            ops=ops,
            covered_by_reference=int(data.get("covered_by_reference", 0)),
            covered_by_plan=int(data.get("covered_by_plan", 0)),
            warnings=[str(w) for w in data.get("warnings", [])],
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise DB.CatalogError(f"not a valid driveaudit plan file ({type(e).__name__}: {e})") from e


def safe_dir_name(name: str) -> str:
    """Drive names become folder names under the destination; keep them single path parts."""
    return name.replace("/", "_").replace("\\", "_").lstrip(".") or "drive"


def write_file_lists(plan: Plan, out_dir: str | Path) -> list[Path]:
    """One NUL-separated rsync ``--files-from`` list per source drive. Never overwrites."""
    out = Path(out_dir).expanduser()
    if out.is_symlink():
        raise ValueError(f"refusing to write lists through a symlinked directory: {out}")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []
    for drive in plan.sources:
        rels = [o.rel for o in plan.ops if o.drive == drive]
        if not rels:
            continue
        path = out / f"{safe_dir_name(drive)}.files0"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "wb") as f:  # O_EXCL: never clobber an earlier list or a symlink
            for rel in rels:
                f.write(os.fsencode(rel) + b"\0")
        created.append(path)
    return created


def rsync_command(plan: Plan, drive: str, list_path: str | Path) -> str:
    src = os.path.abspath(plan.sources[drive]).rstrip("/") + "/"
    dst = f"{os.path.abspath(plan.dest).rstrip('/')}/{safe_dir_name(drive)}/"
    files_from = f"--files-from={os.path.abspath(list_path)}"
    parts = ["rsync", "-a", "--from0", "--ignore-existing", files_from, "--", src, dst]
    return " ".join(shlex.quote(p) for p in parts)


def render_text(plan: Plan, list_paths: dict[str, Path] | None = None) -> str:
    out: list[str] = []
    w = out.append
    w("CONSOLIDATION PLAN (nothing has been copied)")
    w("=" * 70)
    w(f"destination: {plan.dest}/<drive name>/...")
    w(f"to copy:     {len(plan.ops):,} files, {fmt_bytes(plan.total_bytes)}")
    w(f"already on reference: {plan.covered_by_reference:,} files (skipped)")
    if plan.covered_by_plan:
        w(f"identical to a file already in this plan: {plan.covered_by_plan:,} files (skipped)")
    if plan.dest_free is not None:
        w(f"destination free space: {fmt_bytes(plan.dest_free)}")
    for warn in plan.warnings:
        w(f"! {printable(warn)}")
    for drive in plan.sources:
        ops = [o for o in plan.ops if o.drive == drive]
        if not ops:
            w(f"\n{drive}: nothing to copy")
            continue
        only = sum(1 for o in ops if o.reason == "only-here")
        w(
            f"\n{drive}: {len(ops):,} files, {fmt_bytes(sum(o.size for o in ops))} "
            f"({only:,} only here, {len(ops) - only:,} unconfirmed)"
        )
        for o in ops[:5]:
            w(f"    {display_path(o.rel)}")
        if len(ops) > 5:
            w(f"    ... and {len(ops) - 5:,} more")
        if list_paths and drive in list_paths:
            w("  copy (dry run first: add -n):")
            w("    " + rsync_command(plan, drive, list_paths[drive]))
    w("")
    w(
        "After copying: `driveaudit verify-copy PLAN.json` re-reads source and copy and compares hashes."
    )
    w(
        "rsync is invoked with --ignore-existing, so it will never overwrite a file already at the destination."
    )
    return "\n".join(out)


# --- verifying the copy ---------------------------------------------------------------------


@dataclass
class CopyProblem:
    drive: str
    path: str
    problem: (
        str  # missing_source | missing_dest | size_mismatch | hash_mismatch | unsafe_path | ...
    )


@dataclass
class CopyVerification:
    checked: int = 0
    ok: int = 0
    problems: list[CopyProblem] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.problems and self.checked == self.ok


def verify_copy(
    plan: Plan,
    *,
    source_overrides: dict[str, str] | None = None,
    quick: bool = False,
) -> CopyVerification:
    """Check every planned file exists at the destination and equals its source.

    Full mode streams both copies and compares SHA-256. Quick mode compares sizes only.
    Paths from the plan file are validated; a destination reached through a symlink, or
    sitting inside a source drive, never counts; a plan that carried blockers cannot pass.
    """
    res = CopyVerification()
    overrides = source_overrides or {}
    dest_real = os.path.realpath(plan.dest)
    if plan.blockers:
        res.problems.append(CopyProblem("", "plan", "plan_has_blockers"))
    uuid_ok: dict[str, bool] = {}
    for op in plan.ops:
        res.checked += 1
        shown = f"{op.drive}:{display_path(op.rel)}"
        if not op.drive or not is_safe_rel(op.rel):
            res.problems.append(CopyProblem(op.drive, op.rel, "unsafe_path"))
            continue
        src_root = overrides.get(op.drive) or plan.sources.get(op.drive, "")
        if op.drive not in uuid_ok:
            want = plan.source_uuids.get(op.drive)
            now = volume_uuid(src_root) if (want and os.path.isdir(src_root)) else None
            uuid_ok[op.drive] = not (want and now and want != now)
        if not uuid_ok[op.drive]:
            res.problems.append(CopyProblem(op.drive, shown, "source_is_a_different_volume"))
            continue
        src = os.path.join(src_root, op.rel)
        dst = os.path.join(plan.dest, safe_dir_name(op.drive), op.rel)
        drive_dest = os.path.join(dest_real, safe_dir_name(op.drive))
        try:
            sst = stat_beneath(src_root, op.rel)
        except OSError:
            res.problems.append(CopyProblem(op.drive, shown, "missing_source"))
            continue
        try:
            dst_stat = stat_beneath(os.path.join(plan.dest, safe_dir_name(op.drive)), op.rel)
        except OSError:
            # something is there but is not reachable without a link (or is not a plain file)
            kind = "dest_not_regular_file" if os.path.lexists(dst) else "missing_dest"
            res.problems.append(CopyProblem(op.drive, shown, kind))
            continue
        if (
            os.path.islink(dst)
            or not os.path.isfile(dst)
            or not os.path.realpath(dst).startswith(drive_dest + os.sep)
            or is_within(dst, src_root)
        ):
            res.problems.append(CopyProblem(op.drive, shown, "dest_not_regular_file"))
            continue
        if sst.st_size != op.size or (op.mtime_ns is not None and sst.st_mtime_ns != op.mtime_ns):
            res.problems.append(CopyProblem(op.drive, shown, "source_changed_since_planned"))
            continue
        if dst_stat.st_size != sst.st_size:
            res.problems.append(CopyProblem(op.drive, shown, "size_mismatch"))
            continue
        if not quick:
            try:
                a = full_hash(src, root=src_root)
                b = full_hash(dst, root=os.path.join(plan.dest, safe_dir_name(op.drive)))
            except (OSError, FileChanged):
                res.problems.append(CopyProblem(op.drive, shown, "unreadable"))
                continue
            if a != b or (op.full_hash and op.full_hash != a):
                res.problems.append(CopyProblem(op.drive, shown, "hash_mismatch"))
                continue
        res.ok += 1
    return res


def render_copy_text(res: CopyVerification, *, quick: bool) -> str:
    mode = "sizes only" if quick else "full SHA-256 of source and copy"
    lines = [f"VERIFY-COPY ({mode}): {res.ok:,}/{res.checked:,} files OK"]
    if res.passed:
        if quick:
            lines.append(
                "PASS (sizes only) - every planned file is present with the right size. "
                "This did NOT compare contents; run without --quick before retiring anything."
            )
        else:
            lines.append("PASS - every planned file is present and identical at the destination.")
    else:
        lines.append(f"FAIL - {len(res.problems):,} problems; do not retire the source drives:")
        for p in res.problems[:25]:
            lines.append(f"  {p.problem}: {printable(p.path)}")
        if len(res.problems) > 25:
            lines.append(f"  ... and {len(res.problems) - 25} more")
    return "\n".join(lines)
