"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from . import db as DB
from .catalog import catalog_drive, hash_drive, pending_counts
from .config import Settings, load_settings
from .dedupe import find_duplicates
from .dedupe import render_json as dedupe_json
from .dedupe import render_text as dedupe_text
from .matching import Evidence
from .plan import (
    build_plan,
    plan_from_json,
    plan_to_json,
    render_copy_text,
    verify_copy,
    write_file_lists,
)
from .plan import render_text as plan_text
from .reconcile import reconcile
from .reconcile import render_json as reconcile_json
from .reconcile import render_text as reconcile_text
from .util import db_to_path, display_path, fmt_bytes, is_within, write_private
from .verify import render_text as verify_text
from .verify import verify_drive
from .volumes import list_mounts
from .walker import DriveGone, WalkOptions
from .watch import Watcher


def _walk_options(s: Settings, args: argparse.Namespace) -> WalkOptions:
    return WalkOptions(
        skip_dirs=s.skip_dirs,
        skip_files=s.skip_files,
        skip_extensions=s.skip_extensions,
        include_time_machine=s.include_time_machine or getattr(args, "include_time_machine", False),
        cross_devices=s.cross_devices or getattr(args, "cross_devices", False),
    )


def _announce_config(s: Settings) -> None:
    if s.source:
        print(f"config: {s.source}", file=sys.stderr)


def _protected(conn) -> list[str]:
    return [r["mount_path"] for r in DB.get_drives(conn)]


def _emit(text: str, args: argparse.Namespace, conn) -> None:
    """Print, or write to --output (never onto a cataloged drive, never over an existing file)."""
    out = getattr(args, "output", None)
    if not out:
        print(text)
        return
    path = write_private(out, text + "\n", force=args.force, protected_roots=_protected(conn))
    print(f"wrote {path}")


# --- commands -------------------------------------------------------------------------------


def cmd_catalog(args, s: Settings) -> int:
    _announce_config(s)
    # before DB.connect(), which would create the database file
    mount = os.path.abspath(os.path.expanduser(args.path))
    if is_within(s.db, mount):
        raise DB.CatalogError(
            f"the catalog database ({s.db}) is inside the tree being cataloged; "
            "put it somewhere else with --db"
        )
    with DB.connect(s.db) as conn:
        res = catalog_drive(
            conn,
            args.path,
            name=args.name,
            reference=True if args.reference else None,
            options=_walk_options(s, args),
            hash_candidates=args.hash,
            allow_shrink=args.allow_shrink,
            allow_overlap=args.allow_overlap,
            db_path=s.db,
        )
    return 0 if res.complete else 1


def cmd_hash(args, s: Settings) -> int:
    with DB.connect(s.db) as conn:
        targets: list[tuple[str, str]] = []
        if args.drive:
            row = DB.require_drive(conn, args.drive)
            targets.append((args.drive, args.mount or row["mount_path"]))
        else:
            for d in DB.get_drives(conn):
                if os.path.isdir(d["mount_path"]):
                    targets.append((d["drive_name"], d["mount_path"]))
        if not targets:
            print("no cataloged drive is currently mounted")
            return 0
        stages = ("partial", "full", "baseline") if args.baseline else ("partial", "full")
        unreadable = changed = 0
        skipped: set[str] = set()
        for stage in stages:  # all drives' partial hashes exist before any full pass
            for name, mount in targets:
                if name in skipped:
                    continue
                print(f"{stage} hashing {name} at {mount}")
                try:
                    r = hash_drive(conn, name, mount, stages=(stage,))
                    unreadable += r.unreadable
                    changed += r.changed_during_hash
                except DB.CatalogError as e:
                    skipped.add(name)
                    print(f"skipping {name}: {e}", file=sys.stderr)
        if unreadable or changed:
            print(
                f"{unreadable} files could not be read and {changed} changed since they were "
                "cataloged; they were not hashed (re-catalog changed ones)",
                file=sys.stderr,
            )
        missing = 0
        if args.baseline:
            for name, _ in targets:
                missing += conn.execute(
                    "SELECT COUNT(*) FROM files WHERE drive_name = ? AND full_hash IS NULL",
                    (name,),
                ).fetchone()[0]
        if args.baseline and (missing or skipped):
            print(
                f"baseline incomplete: {missing:,} files still have no full hash"
                + (f"; skipped drives: {', '.join(sorted(skipped))}" if skipped else "")
                + ". `verify --deep` cannot check their content.",
                file=sys.stderr,
            )
            return 1
    return 0


def cmd_watch(args, s: Settings) -> int:
    _announce_config(s)
    roots = tuple(args.root) if args.root else s.watch_roots
    w = Watcher(
        db_path=s.db,
        roots=roots,
        options=_walk_options(s, args),
        hash_candidates=args.hash or s.watch_hash,
        include_existing=args.include_existing,
        ignore=tuple(args.ignore) + s.watch_ignore,
        reconcile_after=not args.no_reconcile,
        settle=args.settle if args.settle is not None else s.watch_settle,
        mounts_fn=lambda r: list_mounts(list(r)),
    )
    interval = args.interval if args.interval is not None else s.watch_interval
    events = w.run(interval, max_scans=1 if args.once else None)
    return 1 if any(e.kind == "failed" for e in events) else 0


def cmd_drives(args, s: Settings) -> int:
    import datetime

    with DB.connect(s.db) as conn:
        rows = DB.get_drives(conn)
        if not rows:
            print(f"no drives cataloged yet (database: {s.db})")
            return 0
        print(f"database: {s.db}")
        fmt = "{:<24} {:>10} {:>10}  {:<10} {:<16} {}"
        print(fmt.format("DRIVE", "FILES", "SIZE", "ROLE", "CATALOGED", "NOTES"))
        for d in rows:
            when = (
                datetime.datetime.fromtimestamp(d["catalog_end"]).strftime("%Y-%m-%d %H:%M")
                if d["catalog_end"]
                else "-"
            )
            need_partial, need_full = pending_counts(conn, d["drive_name"])
            notes = []
            if not d["walk_complete"]:
                notes.append("INCOMPLETE")
            if d["error_count"]:
                notes.append(f"{d['error_count']} unreadable")
            if need_partial or need_full:
                notes.append(f"hash pending: {need_partial:,} partial, {need_full:,} full")
            print(
                fmt.format(
                    d["drive_name"],
                    f"{d['total_files']:,}",
                    fmt_bytes(d["total_bytes"]),
                    "reference" if d["is_reference"] else "-",
                    when,
                    ", ".join(notes),
                )
            )
    return 0


def cmd_mark(args, s: Settings) -> int:
    with DB.connect(s.db) as conn:
        DB.set_reference(conn, args.drive, not args.no_reference)
    print(
        f"{args.drive}: {'no longer a reference' if args.no_reference else 'now a reference drive'}"
    )
    return 0


def _refs(conn, s: Settings, against: list[str] | None) -> list[str]:
    return sorted(set(against)) if against else DB.reference_names(conn, s.reference)


def cmd_dedupe(args, s: Settings) -> int:
    with DB.connect(s.db) as conn:
        rep = find_duplicates(conn, min_size=args.min_size, weak=args.weak)
        text = dedupe_json(rep) if args.format == "json" else dedupe_text(rep, limit=args.limit)
        _emit(text, args, conn)
    return 0


def cmd_reconcile(args, s: Settings) -> int:
    with DB.connect(s.db) as conn:
        res = reconcile(
            conn,
            args.drive,
            _refs(conn, s, args.against),
            min_evidence=Evidence.PROBABLE
            if args.min_evidence == "probable"
            else Evidence.VERIFIED,
            live=args.live,
        )
        if args.list and args.null:
            data = b"".join(
                os.fsencode(db_to_path(i.rec.rel)) + b"\0" for i in res.bucket(args.list)
            )
            if args.output:
                path = write_private(
                    args.output, data, force=args.force, protected_roots=_protected(conn)
                )
                print(f"wrote {path}")
            else:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            return 0 if res.fully_covered else 1
        if args.list:
            text = "\n".join(display_path(i.rec.rel) for i in res.bucket(args.list))
        elif args.format == "json":
            text = reconcile_json(res, include_files=args.include_files)
        else:
            text = reconcile_text(res)
        _emit(text, args, conn)
    return 0 if res.fully_covered else 1


def cmd_plan(args, s: Settings) -> int:
    with DB.connect(s.db) as conn:
        refs = _refs(conn, s, args.against)
        plan = build_plan(
            conn,
            args.drive,
            refs,
            args.dest,
            min_evidence=Evidence.PROBABLE
            if args.min_evidence == "probable"
            else Evidence.VERIFIED,
        )
        lists = {}
        if args.export_lists:
            lists_dir = Path(args.export_lists).expanduser()
            for root in _protected(conn):
                if is_within(lists_dir, root):
                    raise ValueError(f"refusing to write inside a cataloged drive: {lists_dir}")
            paths = write_file_lists(plan, args.export_lists)
            lists = {p.stem: p for p in paths}
            # drive names map 1:1 to stems except where sanitized; look up by sanitized name
            from .plan import safe_dir_name

            lists = {d: lists[safe_dir_name(d)] for d in plan.sources if safe_dir_name(d) in lists}
        print(plan_text(plan, lists))
        if args.output:
            path = write_private(
                args.output,
                plan_to_json(plan) + "\n",
                force=args.force,
                protected_roots=_protected(conn),
            )
            print(f"\nwrote plan {path}")
    return 1 if plan.blockers else 0


def cmd_verify(args, s: Settings) -> int:
    _announce_config(s)
    with DB.connect(s.db) as conn:
        res = verify_drive(
            conn, args.drive, args.mount, deep=args.deep, options=_walk_options(s, args)
        )
    print(verify_text(res, deep=args.deep))
    return 0 if res.ok and (not args.deep or res.fully_verified) else 1


def cmd_verify_copy(args, s: Settings) -> int:
    plan = plan_from_json(Path(args.plan).read_text(encoding="utf-8"))
    overrides = {}
    for item in args.source or []:
        name, sep, path = item.partition("=")
        if not sep:
            raise DB.CatalogError("--source expects DRIVE=PATH")
        overrides[name] = path
    res = verify_copy(plan, source_overrides=overrides, quick=args.quick)
    print(render_copy_text(res, quick=args.quick))
    return 0 if res.passed else 1


def cmd_forget(args, s: Settings) -> int:
    with DB.connect(s.db) as conn:
        row = DB.require_drive(conn, args.drive)
        if not args.yes:
            if not sys.stdin.isatty():
                raise DB.CatalogError(
                    "refusing to forget a drive without --yes when not interactive"
                )
            ans = input(
                f"Remove '{args.drive}' ({row['total_files']:,} files) from the catalog? "
                "The drive itself is not touched. [y/N] "
            )
            if ans.strip().lower() != "y":
                print("cancelled")
                return 0
        DB.forget_drive(conn, args.drive)
    print(f"removed '{args.drive}' from the catalog")
    return 0


def cmd_config(args, s: Settings) -> int:
    print(f"config file:   {s.source or '(none found; using defaults)'}")
    print(f"database:      {s.db}")
    print(f"reference:     {', '.join(s.reference) or '(none in config)'}")
    print(f"watch roots:   {', '.join(s.watch_roots)}")
    print(f"watch interval/settle: {s.watch_interval:g}s / {s.watch_settle:g}s")
    print(f"extra skipped dirs: {len(s.skip_dirs)} total")
    return 0


# --- parser ---------------------------------------------------------------------------------


def _output_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--output", "-o", metavar="FILE", help="write here instead of stdout")
    p.add_argument(
        "--force", action="store_true", help="allow --output to overwrite an existing file"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="driveaudit",
        description="Catalog drives, find duplicates across them, and reconcile each against a "
        "declared source of truth. Read-only: driveaudit never deletes, moves or writes to a drive.",
    )
    p.add_argument("--version", action="version", version=f"driveaudit {__version__}")
    p.add_argument(
        "--db",
        metavar="PATH",
        help="catalog database (default: $DRIVEAUDIT_DB or per-user data dir)",
    )
    p.add_argument("--config", metavar="FILE", help="TOML config file")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("catalog", help="walk a mounted drive into the catalog")
    c.add_argument("path", help="mount point or folder to catalog")
    c.add_argument("--name", help="catalog name (default: the volume label)")
    c.add_argument("--reference", action="store_true", help="mark this drive as a source of truth")
    c.add_argument("--hash", action="store_true", help="also hash files that could be duplicates")
    c.add_argument(
        "--allow-shrink",
        action="store_true",
        help="accept a walk that finds far fewer files than before",
    )
    c.add_argument(
        "--allow-overlap", action="store_true", help="allow a path overlapping a cataloged drive"
    )
    c.add_argument("--include-time-machine", action="store_true")
    c.add_argument(
        "--cross-devices",
        action="store_true",
        help="descend into other volumes mounted inside the tree",
    )
    c.set_defaults(func=cmd_catalog)

    h = sub.add_parser("hash", help="hash duplicate candidates on mounted drives")
    h.add_argument(
        "drive", nargs="?", help="catalog name (default: every cataloged drive that is mounted)"
    )
    h.add_argument(
        "--mount", help="where the drive is mounted now (default: where it was cataloged)"
    )
    h.add_argument(
        "--baseline",
        action="store_true",
        help="also full-hash every file that has no full hash (reads them all), so that "
        "`verify --deep` can check their content",
    )
    h.set_defaults(func=cmd_hash)

    w = sub.add_parser("watch", help="catalog volumes as they are plugged in")
    w.add_argument(
        "--root",
        action="append",
        help="directory whose children are mounts (repeatable; default /Volumes)",
    )
    w.add_argument("--interval", type=float, help="seconds between polls")
    w.add_argument("--settle", type=float, help="seconds to wait after a volume appears")
    w.add_argument("--hash", action="store_true", help="hash duplicate candidates after each walk")
    w.add_argument(
        "--include-existing",
        action="store_true",
        help="also catalog volumes already mounted at start",
    )
    w.add_argument(
        "--ignore",
        action="append",
        default=[],
        metavar="LABEL",
        help="volume label to ignore (repeatable)",
    )
    w.add_argument("--no-reconcile", action="store_true", help="skip the quick reconcile summary")
    w.add_argument("--once", action="store_true", help="scan once and exit")
    w.add_argument("--include-time-machine", action="store_true")
    w.add_argument("--cross-devices", action="store_true")
    w.set_defaults(func=cmd_watch)

    sub.add_parser("drives", help="list cataloged drives").set_defaults(func=cmd_drives)
    sub.add_parser("status", help="alias of drives").set_defaults(func=cmd_drives)

    m = sub.add_parser("mark", help="set or clear a drive's role as source of truth")
    m.add_argument("drive")
    m.add_argument("--no-reference", action="store_true")
    m.set_defaults(func=cmd_mark)

    d = sub.add_parser("dedupe", help="report duplicate files across drives")
    d.add_argument("--min-size", type=int, default=1, metavar="BYTES")
    d.add_argument(
        "--weak", action="store_true", help="also list name+size matches among unhashed files"
    )
    d.add_argument("--limit", type=int, default=20)
    d.add_argument("--format", choices=["text", "json"], default="text")
    _output_args(d)
    d.set_defaults(func=cmd_dedupe)

    r = sub.add_parser("reconcile", help="what on this drive exists only here?")
    r.add_argument("drive")
    r.add_argument(
        "--against",
        action="append",
        metavar="NAME",
        help="reference drive (repeatable; default: all marked)",
    )
    r.add_argument("--min-evidence", choices=["verified", "probable"], default="verified")
    r.add_argument(
        "--live", action="store_true", help="re-stat matched files on mounted reference drives"
    )
    r.add_argument(
        "--list",
        choices=["only-here", "unconfirmed", "not-covered", "covered"],
        help="print file paths of one group",
    )
    r.add_argument(
        "-0", "--null", action="store_true", help="with --list: NUL-separated raw paths (xargs -0)"
    )
    r.add_argument("--format", choices=["text", "json"], default="text")
    r.add_argument(
        "--include-files", action="store_true", help="json: include the not-covered file list"
    )
    _output_args(r)
    r.set_defaults(func=cmd_reconcile)

    pl = sub.add_parser("plan", help="plan the copy that consolidates drives onto a destination")
    pl.add_argument("drive", nargs="+")
    pl.add_argument(
        "--dest", required=True, help="destination folder (layout: DEST/<drive>/<path>)"
    )
    pl.add_argument("--against", action="append", metavar="NAME")
    pl.add_argument("--min-evidence", choices=["verified", "probable"], default="verified")
    pl.add_argument("--export-lists", metavar="DIR", help="write rsync --files-from lists here")
    _output_args(pl)
    pl.set_defaults(func=cmd_plan)

    v = sub.add_parser("verify", help="re-check a mounted drive against the catalog")
    v.add_argument("drive")
    v.add_argument("--mount", help="current mount point (default: as cataloged)")
    v.add_argument(
        "--deep", action="store_true", help="re-read files and compare against stored hashes"
    )
    v.add_argument("--include-time-machine", action="store_true")
    v.add_argument("--cross-devices", action="store_true")
    v.set_defaults(func=cmd_verify)

    vc = sub.add_parser("verify-copy", help="check a finished copy against its plan")
    vc.add_argument("plan", help="plan JSON written by `plan --output`")
    vc.add_argument(
        "--source", action="append", metavar="DRIVE=PATH", help="source drive's current mount point"
    )
    vc.add_argument("--quick", action="store_true", help="compare sizes only")
    vc.set_defaults(func=cmd_verify_copy)

    f = sub.add_parser("forget", help="remove a drive from the catalog (never touches the drive)")
    f.add_argument("drive")
    f.add_argument("-y", "--yes", action="store_true")
    f.set_defaults(func=cmd_forget)

    sub.add_parser("config", help="show effective settings").set_defaults(func=cmd_config)
    return p


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config, args.db)
        code = args.func(args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        code = 130
    except BrokenPipeError:
        code = 0
    except DriveGone as e:
        print(f"driveaudit: the drive went away: {e}", file=sys.stderr)
        code = 2
    except (DB.CatalogError, FileNotFoundError, ValueError, OSError) as e:
        print(f"driveaudit: {e}", file=sys.stderr)
        code = 2
    sys.exit(code)
