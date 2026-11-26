"""Watch for newly mounted volumes and catalog each one as it appears.

A polling loop (no OS hooks, so it behaves the same on macOS and Linux). Volumes already
mounted when the watcher starts are left alone unless ``include_existing``; a volume that is
unplugged and plugged back in is treated as new again. Everything it does to a drive is
read-only. It never ejects, mounts, or writes to a volume.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from . import db as DB
from .catalog import CatalogResult, catalog_drive
from .reconcile import reconcile
from .util import fmt_bytes, is_within, printable
from .volumes import Volume, identify, list_mounts
from .walker import WalkOptions


@dataclass
class WatchEvent:
    mount: str
    kind: str  # cataloged | failed | skipped | gone
    result: CatalogResult | None = None
    detail: str = ""


@dataclass
class Watcher:
    db_path: Path
    roots: tuple[str, ...]
    options: WalkOptions = field(default_factory=WalkOptions)
    hash_candidates: bool = False
    include_existing: bool = False
    ignore: tuple[str, ...] = ()
    reconcile_after: bool = False
    settle: float = 3.0
    log: Callable[[str], None] = print
    # seams for tests
    mounts_fn: Callable[[Iterable[str]], list[str]] = list_mounts
    identify_fn: Callable[[str], Volume] = identify
    sleep_fn: Callable[[float], None] = time.sleep
    catalog_fn: Callable[..., CatalogResult] = catalog_drive

    def __post_init__(self) -> None:
        self._baseline: set[str] = set()
        self._handled: set[str] = set()  # mounts already cataloged (or failed) this plug-in
        self._started = False

    def _current(self) -> list[str]:
        out = []
        for m in self.mounts_fn(self.roots):
            if os.path.basename(m) in self.ignore:
                continue
            out.append(m)
        return out

    def scan_once(self) -> list[WatchEvent]:
        """One pass: forget unplugged volumes, catalog newly appeared ones."""
        current = self._current()
        present = set(current)
        events: list[WatchEvent] = []
        if not self._started:
            self._started = True
            if not self.include_existing:
                self._baseline = set(present)
                if present:
                    self.log(
                        f"ignoring {len(present)} volume(s) already mounted: "
                        + ", ".join(sorted(printable(os.path.basename(p)) for p in present))
                    )
        # unplugged volumes become "new" again if they come back
        for m in list(self._handled | self._baseline):
            if m not in present:
                self._handled.discard(m)
                self._baseline.discard(m)
                events.append(WatchEvent(m, "gone"))
        for m in current:
            if m in self._handled or m in self._baseline:
                continue
            self._handled.add(m)
            events.append(self._handle(m))
        return events

    def _handle(self, mount: str) -> WatchEvent:
        self.log(f"new volume: {printable(mount)}")
        if self.settle > 0:
            self.sleep_fn(self.settle)
        if mount not in self.mounts_fn(self.roots):
            return WatchEvent(mount, "skipped", detail="unplugged while settling")
        try:
            os.listdir(mount)
        except OSError as e:
            return WatchEvent(mount, "failed", detail=f"not readable: {e}")
        if is_within(self.db_path, mount):
            return WatchEvent(
                mount, "failed", detail="the catalog database is inside this volume; not cataloging"
            )
        try:
            vol = self.identify_fn(mount)
            with DB.connect(self.db_path) as conn:
                result = self.catalog_fn(
                    conn,
                    mount,
                    options=self.options,
                    hash_candidates=self.hash_candidates,
                    volume=vol,
                    db_path=self.db_path,
                    log=self.log,
                )
                if self.reconcile_after:
                    self._summarize(conn, result)
        except (DB.CatalogError, OSError) as e:
            self.log(f"failed to catalog {mount}: {e}")
            return WatchEvent(mount, "failed", detail=str(e))
        return WatchEvent(mount, "cataloged", result=result)

    def _summarize(self, conn, result: CatalogResult) -> None:
        refs = DB.reference_names(conn)
        if not refs or result.drive_name in refs:
            return
        res = reconcile(conn, result.drive_name, refs)
        for b in res.blockers:
            self.log(f"  ! {printable(b)}")
        only = res.bucket("only-here")
        self.log(
            f"  vs reference {', '.join(refs)}: {len(res.bucket('covered')):,} covered, "
            f"{len(only):,} only here ({fmt_bytes(sum(i.rec.size for i in only))})"
        )

    def run(self, interval: float, *, max_scans: int | None = None) -> list[WatchEvent]:
        """Poll until interrupted (or ``max_scans`` passes, for tests and ``--once``)."""
        self.log(f"watching {', '.join(self.roots)} every {interval:g}s (Ctrl-C to stop)")
        all_events: list[WatchEvent] = []
        scans = 0
        try:
            while max_scans is None or scans < max_scans:
                all_events += self.scan_once()
                scans += 1
                if max_scans is not None and scans >= max_scans:
                    break
                self.sleep_fn(interval)
        except KeyboardInterrupt:
            self.log("stopped")
        return all_events
