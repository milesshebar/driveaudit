import os
import shutil

import pytest

from driveaudit import db as DB
from driveaudit.verify import render_text, verify_drive
from driveaudit.walker import DriveGone

from .conftest import blob, make_tree


def build(tmp_path, cat):
    make_tree(tmp_path / "a", {"f1.bin": blob("1", 40_000), "d/f2.bin": blob("2", 40_000)})
    make_tree(tmp_path / "b", {"g1.bin": blob("1", 40_000), "g2.bin": blob("2", 40_000)})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    cat.hash_all()


def test_clean_drive_passes(tmp_path, conn, cat):
    build(tmp_path, cat)
    res = verify_drive(conn, "a")
    assert res.ok and res.checked == 2
    assert "PASS" in render_text(res, deep=False)


def test_new_missing_modified(tmp_path, conn, cat):
    build(tmp_path, cat)
    (tmp_path / "a" / "d" / "f2.bin").unlink()
    make_tree(tmp_path / "a", {"added.txt": "new"})
    os.utime(tmp_path / "a" / "f1.bin", (5, 5))
    res = verify_drive(conn, "a")
    assert not res.ok
    assert res.missing_files == ["d/f2.bin"]
    assert res.new_files == ["added.txt"]
    assert res.modified_files == ["f1.bin"]
    assert "DIFFERENCES" in render_text(res, deep=False)


def test_deep_verify_catches_silent_corruption_that_keeps_size_and_mtime(tmp_path, conn, cat):
    build(tmp_path, cat)
    p = tmp_path / "a" / "f1.bin"
    st = os.stat(p)
    data = bytearray(p.read_bytes())
    data[20_000] ^= 0xFF  # flipped bit in the middle
    p.write_bytes(bytes(data))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert verify_drive(conn, "a").ok  # metadata alone cannot see it
    deep = verify_drive(conn, "a", deep=True)
    assert deep.hash_mismatches == ["f1.bin"] and not deep.ok


def test_verify_does_not_modify_the_catalog(tmp_path, conn, cat):
    build(tmp_path, cat)
    before = conn.execute("SELECT COUNT(*), SUM(file_size), MAX(seen_gen) FROM files").fetchone()[:]
    (tmp_path / "a" / "f1.bin").unlink()
    verify_drive(conn, "a")
    assert (
        conn.execute("SELECT COUNT(*), SUM(file_size), MAX(seen_gen) FROM files").fetchone()[:]
        == before
    )


def test_unmounted_and_unplugged_drives(tmp_path, conn, cat, monkeypatch):
    build(tmp_path, cat)
    with pytest.raises(DB.CatalogError):
        verify_drive(conn, "a", str(tmp_path / "not-there"))
    shutil.rmtree(tmp_path / "a")
    with pytest.raises(DB.CatalogError):
        verify_drive(conn, "a")
    # vanishing mid-walk raises rather than reporting a partial result as OK
    make_tree(tmp_path / "c", {f"d{i}/f": "x" for i in range(5)})
    cat(tmp_path / "c", "c")
    real = os.scandir
    n = {"i": 0}

    def flaky(path):
        n["i"] += 1
        if n["i"] == 3:
            shutil.rmtree(tmp_path / "c")
        return real(path)

    monkeypatch.setattr("driveaudit.walker.os.scandir", flaky)
    with pytest.raises(DriveGone):
        verify_drive(conn, "c")
