"""How two files are judged to be "the same". One place, used by dedupe, reconcile and plan.

Evidence levels, strongest first:

    verified   same size and same full SHA-256 (both hashed over the whole file)
    probable   same size and same partial hash (first + last 8 KiB), full hash missing on at
               least one side. Files differing only in the middle also look like this.
    name_size  same size and same (Unicode-normalized) file name, no usable hashes.
               Nothing more than a hint.
    none       anything else. A size match alone is never a match.

If both files have a full hash and they differ, the answer is ``none`` no matter what else agrees.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import IntEnum

from .util import basename_of


class Evidence(IntEnum):
    NONE = 0
    NAME_SIZE = 1
    PROBABLE = 2
    VERIFIED = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class FileRec:
    drive: str
    rel: str  # stored (db) form of the path
    size: int
    mtime_ns: int
    partial: str | None
    full: str | None
    link_key: str | None = None

    @property
    def name(self) -> str:
        return basename_of(self.rel)


def compare(a: FileRec, b: FileRec) -> Evidence:
    """Evidence that ``a`` and ``b`` are the same content (reference implementation)."""
    if a.size != b.size:
        return Evidence.NONE
    if a.full and b.full:
        return Evidence.VERIFIED if a.full == b.full else Evidence.NONE
    if a.partial and b.partial:
        return Evidence.PROBABLE if a.partial == b.partial else Evidence.NONE
    return Evidence.NAME_SIZE if a.name == b.name else Evidence.NONE


def load_files(conn: sqlite3.Connection, drive: str) -> list[FileRec]:
    rows = conn.execute(
        """SELECT drive_name, rel_path, file_size, mtime_ns, partial_hash, full_hash, link_key
           FROM files WHERE drive_name = ?""",
        (drive,),
    )
    return [FileRec(*tuple(r)) for r in rows]


class ReferenceIndex:
    """Fast "best evidence that this file is on a reference drive" lookups.

    Equivalent to taking the max of ``compare()`` over every reference file, without the
    quadratic scan.
    """

    def __init__(self, files: list[FileRec]):
        self._full: dict[tuple[int, str], FileRec] = {}
        self._partial_any: dict[tuple[int, str], FileRec] = {}
        self._partial_nofull: dict[tuple[int, str], FileRec] = {}
        self._name_any: dict[tuple[int, str], FileRec] = {}
        self._name_unhashed: dict[tuple[int, str], FileRec] = {}
        for f in files:
            if f.full:
                self._full.setdefault((f.size, f.full), f)
            if f.partial:
                self._partial_any.setdefault((f.size, f.partial), f)
                if not f.full:
                    self._partial_nofull.setdefault((f.size, f.partial), f)
            else:
                self._name_unhashed.setdefault((f.size, f.name), f)
            self._name_any.setdefault((f.size, f.name), f)

    def best(self, f: FileRec) -> tuple[Evidence, FileRec | None]:
        if f.full:
            hit = self._full.get((f.size, f.full))
            if hit:
                return Evidence.VERIFIED, hit
            hit = self._partial_nofull.get((f.size, f.partial)) if f.partial else None
            if hit:
                return Evidence.PROBABLE, hit
        elif f.partial:
            hit = self._partial_any.get((f.size, f.partial))
            if hit:
                return Evidence.PROBABLE, hit
        # name+size only counts when at least one side has no partial hash to contradict it
        if f.partial:
            hit = self._name_unhashed.get((f.size, f.name))
        else:
            hit = self._name_any.get((f.size, f.name))
        if hit:
            return Evidence.NAME_SIZE, hit
        return Evidence.NONE, None
