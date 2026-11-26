"""Shared fixtures: synthetic "drives" are just temp directories with known contents."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from driveaudit import db as DB
from driveaudit.catalog import catalog_drive, hash_drive
from driveaudit.volumes import Volume

FIXED_MTIME = 1_700_000_000


def make_tree(root: Path, spec: dict[str, bytes | str], mtime: int | None = FIXED_MTIME) -> Path:
    """Create files under ``root``. ``spec`` maps relative path -> content."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in spec.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode() if isinstance(content, str) else content)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
    return root


def blob(tag: str, size: int = 40_000) -> bytes:
    """Deterministic, distinct content of an exact size (bigger than 16 KiB by default)."""
    seed = (tag + "|").encode()
    return (seed * (size // len(seed) + 1))[:size]


def vol(label: str, uuid: str | None = None) -> Volume:
    return Volume(mount_path=label, label=label, uuid=uuid)


@pytest.fixture
def conn(tmp_path):
    with DB.connect(tmp_path / "state" / "catalog.db") as c:
        yield c


@pytest.fixture
def dbpath(tmp_path) -> Path:
    return tmp_path / "state" / "catalog.db"


@pytest.fixture
def cat(conn):
    """cat(path, name, reference=False, hash=True) -> CatalogResult, fully quiet."""

    def _cat(path, name=None, *, reference=None, hash=True, **kw):
        res = catalog_drive(
            conn,
            str(path),
            name=name,
            reference=reference,
            hash_candidates=False,
            volume=vol(name or Path(path).name),
            log=lambda *_: None,
            **kw,
        )
        return res

    def hash_all():
        """Hash every cataloged drive at its recorded mount point: partial pass, then full."""
        for stage in ("partial", "full"):
            for d in DB.get_drives(conn):
                hash_drive(
                    conn,
                    d["drive_name"],
                    d["mount_path"],
                    expected_uuid_check=False,
                    stages=(stage,),
                    log=lambda *_: None,
                )

    _cat.hash_all = hash_all
    return _cat
