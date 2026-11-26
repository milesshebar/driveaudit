"""Regressions for the pre-release adversarial review (one test per finding)."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat

import pytest

from driveaudit import db as DB
from driveaudit.catalog import catalog_drive, hash_drive
from driveaudit.cli import main
from driveaudit.plan import Plan, PlanOp, plan_from_json, plan_to_json
from driveaudit.reconcile import reconcile
from driveaudit.util import write_private
from driveaudit.verify import render_text, verify_drive
from driveaudit.walker import Walker

from .conftest import blob, make_tree, vol


def test_force_output_never_truncates_a_hard_linked_protected_file(tmp_path):
    drive = make_tree(tmp_path / "drive", {"precious.txt": "irreplaceable"})
    out = tmp_path / "out.txt"
    os.link(drive / "precious.txt", out)  # the output is the same inode as a drive file
    write_private(out, "new data", force=True, protected_roots=[str(tmp_path / "other")])
    assert (drive / "precious.txt").read_text() == "irreplaceable"
    assert out.read_text() == "new data"


def test_stale_concurrent_walk_cannot_overwrite_newer_metadata(tmp_path, conn, monkeypatch):
    root = make_tree(tmp_path / "d", {"f.txt": "abc"})
    catalog_drive(conn, str(root), name="d", volume=vol("d"), log=lambda *_: None)
    real_iter = Walker.__iter__
    fired = {"done": False}

    def slow_iter(self):
        for e in real_iter(self):
            if not fired["done"]:  # the OLD walk saw size 3, then stalls...
                fired["done"] = True
                stale = e
                (root / "f.txt").write_text("a much longer content")
                # ...while a newer walk runs to completion
                catalog_drive(conn, str(root), name="d", volume=vol("d"), log=lambda *_: None)
                yield stale
            else:
                yield e

    monkeypatch.setattr(Walker, "__iter__", slow_iter)
    res = catalog_drive(conn, str(root), name="d", volume=vol("d"), log=lambda *_: None)
    row = conn.execute("SELECT file_size FROM files WHERE rel_path = 'f.txt'").fetchone()
    assert row["file_size"] == len("a much longer content")
    assert not res.complete


def _pair(tmp_path, cat):
    make_tree(tmp_path / "nas", {"a": blob("a")})
    make_tree(tmp_path / "usb", {"a2": blob("a")})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "usb", "usb")


def test_live_check_with_unmounted_reference_is_not_a_pass(tmp_path, conn, cat):
    _pair(tmp_path, cat)
    cat.hash_all()
    shutil.rmtree(tmp_path / "nas")
    res = reconcile(conn, "usb", ["nas"], live=True)
    assert not res.fully_covered
    assert any(b.startswith("BLOCKER") and "live" in b for b in res.blockers)


def test_hashing_refuses_an_ancestor_symlink(tmp_path, conn, cat):
    secret = make_tree(tmp_path / "outside", {"sub/f.bin": blob("secret", 100)})
    drive = make_tree(
        tmp_path / "drive", {"sub/f.bin": blob("x", 100), "sub/g.bin": blob("y", 100)}
    )
    cat(drive, "d")
    shutil.rmtree(drive / "sub")
    os.symlink(secret / "sub", drive / "sub")  # the parent directory became a symlink
    os.utime(secret / "sub" / "f.bin", (1_700_000_000, 1_700_000_000))
    hash_drive(conn, "d", str(drive), expected_uuid_check=False, log=lambda *_: None)
    rows = conn.execute("SELECT partial_hash, full_hash FROM files WHERE rel_path LIKE 'sub/%'")
    assert all(r["partial_hash"] is None and r["full_hash"] is None for r in rows)


def test_db_inside_the_drive_is_rejected_before_it_is_created(tmp_path, monkeypatch, capsys):
    drive = make_tree(tmp_path / "drive", {"a.txt": "a"})
    db = drive / "state" / "catalog.db"
    monkeypatch.setenv("DRIVEAUDIT_DB", str(db))
    monkeypatch.delenv("DRIVEAUDIT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as e:
        main(["catalog", str(drive), "--name", "d"])
    assert e.value.code == 2
    assert not db.exists() and not db.parent.exists()


def test_deep_verify_does_not_claim_pass_for_files_it_never_read(tmp_path, conn, cat):
    root = make_tree(tmp_path / "d", {"u.txt": "abc"})
    cat(root, "d", hash=False)
    (root / "u.txt").write_text("xyz")  # same size...
    os.utime(root / "u.txt", (1_700_000_000, 1_700_000_000))  # ...and mtime
    res = verify_drive(conn, "d", deep=True)
    assert res.content_unverified == ["u.txt"]
    text = render_text(res, deep=True)
    assert "the drive matches the catalog" not in text and "NOT verified" in text
    assert not res.fully_verified


def test_baseline_hash_makes_deep_verify_catch_the_edit(tmp_path, conn, cat):
    root = make_tree(tmp_path / "d", {"u.txt": "abc"})
    cat(root, "d", hash=False)
    hash_drive(conn, "d", str(root), stages=("baseline",), expected_uuid_check=False, log=print)
    (root / "u.txt").write_text("xyz")
    os.utime(root / "u.txt", (1_700_000_000, 1_700_000_000))
    res = verify_drive(conn, "d", deep=True)
    assert res.hash_mismatches == ["u.txt"]


def test_forced_output_ends_up_private(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text("old")
    out.chmod(0o644)
    write_private(out, "paths", force=True)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_plan_with_non_utf8_filename_round_trips(tmp_path):
    rel = os.fsdecode(b"bad-\xff-name.txt")  # what scandir yields for undecodable bytes
    plan = Plan(
        dest="/x", created=0.0, sources={"d": "/src"}, ops=[PlanOp("d", rel, 3, None, "only-here")]
    )
    out = write_private(tmp_path / "plan.json", plan_to_json(plan))
    back = plan_from_json(out.read_text(encoding="utf-8"))
    assert back.ops[0].rel == rel


def test_docs_have_no_stale_links_or_versions():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    text = (root / "README.md").read_text() + (root / "CHANGELOG.md").read_text()
    assert "AUDIT.md" not in text and "v0.1" not in text


# --- round 2 ----------------------------------------------------------------------------------


def _quiet(**kw):
    return {"volume": vol("d"), "log": lambda *_: None, **kw}


def test_old_walk_cannot_restore_completion_after_a_newer_failed_walk(tmp_path, dbpath):
    root = make_tree(tmp_path / "d", {"a.txt": "a", "locked/b.txt": "b"})
    with DB.connect(dbpath) as c1, DB.connect(dbpath) as c2:
        catalog_drive(c1, str(root), name="d", **_quiet())
        real_iter = Walker.__iter__
        state = {"fired": False}

        def stalled(self):
            for e in real_iter(self):
                if not state["fired"]:
                    state["fired"] = True
                    (root / "locked").chmod(0)  # the NEWER walk cannot read this
                    try:
                        newer = catalog_drive(c2, str(root), name="d", **_quiet())
                    finally:
                        (root / "locked").chmod(0o755)
                    assert not newer.complete
                yield e

        Walker.__iter__ = stalled
        try:
            older = catalog_drive(c1, str(root), name="d", **_quiet())
        finally:
            Walker.__iter__ = real_iter
        assert not older.complete
        row = c2.execute("SELECT walk_complete FROM drives WHERE drive_name = 'd'").fetchone()
        assert row["walk_complete"] == 0


def test_walk_metadata_check_and_writes_share_one_write_lock(tmp_path, dbpath, monkeypatch):
    import sqlite3

    root = make_tree(tmp_path / "d", {"a.txt": "a"})
    probes = []
    real = DB.refresh_totals

    def probing(conn, name, **kw):
        other = sqlite3.connect(str(dbpath), timeout=0.05)
        try:
            other.execute("UPDATE drives SET walk_gen = walk_gen + 1")
            probes.append("writable")  # a competitor got in between check and write
        except sqlite3.OperationalError:
            probes.append("locked")
        finally:
            other.close()
        return real(conn, name, **kw)

    monkeypatch.setattr(DB, "refresh_totals", probing)
    with DB.connect(dbpath) as c1:
        catalog_drive(c1, str(root), name="d", **_quiet())
    assert probes and set(probes) == {"locked"}


def test_hashing_refuses_a_catalog_root_replaced_by_a_symlink(tmp_path, conn, cat):
    drive = make_tree(tmp_path / "drive", {"f.bin": blob("x", 100)})
    outside = make_tree(tmp_path / "outside", {"f.bin": blob("secret", 100)})
    cat(drive, "d")
    drive.rename(tmp_path / "drive-moved")
    os.symlink(outside, tmp_path / "drive")
    os.utime(outside / "f.bin", (1_700_000_000, 1_700_000_000))
    with contextlib.suppress(DB.CatalogError):
        hash_drive(
            conn,
            "d",
            str(tmp_path / "drive"),
            stages=("baseline",),
            expected_uuid_check=False,
            log=lambda *_: None,
        )
    rows = conn.execute("SELECT full_hash FROM files").fetchall()
    assert all(r["full_hash"] is None for r in rows)


def test_walker_does_not_enter_a_directory_swapped_for_a_symlink_mid_walk(tmp_path):
    root = make_tree(tmp_path / "d", {"a/one.txt": "1", "b/two.txt": "2"})
    make_tree(tmp_path / "outside", {"secret.txt": "s"})
    w = Walker(root)
    it = iter(w)
    first = next(it)
    assert first.rel == "a/one.txt"
    shutil.rmtree(root / "b")
    os.symlink(tmp_path / "outside", root / "b")  # swapped after it was queued
    rest = [e.rel for e in it]
    assert "b/secret.txt" not in rest


def test_walker_notices_the_root_being_replaced(tmp_path):
    from driveaudit.walker import DriveGone

    root = make_tree(tmp_path / "d", {"a/one.txt": "1", "b/two.txt": "2"})
    other = make_tree(tmp_path / "other", {"a/x.txt": "1"})
    it = iter(Walker(root, None))
    next(it)
    root.rename(tmp_path / "d-moved")
    os.symlink(other, root)
    with pytest.raises(DriveGone):
        list(it)


def test_open_beneath_enforces_the_device_boundary():
    from driveaudit.hasher import open_beneath

    if os.stat("/").st_dev == os.stat("/dev").st_dev:
        pytest.skip("/dev is on the root filesystem here")
    with pytest.raises(OSError):
        open_beneath("/", "dev/null", os.O_RDONLY, same_device=True)
    os.close(open_beneath("/", "dev/null", os.O_RDONLY, same_device=False))


def test_baseline_exits_nonzero_while_baselines_are_missing(tmp_path, monkeypatch, capsys):
    if os.geteuid() == 0:
        pytest.skip("root can read anything")
    drive = make_tree(tmp_path / "d", {"ok.txt": "fine", "bad.txt": "secret"})
    monkeypatch.setenv("DRIVEAUDIT_DB", str(tmp_path / "state" / "c.db"))
    monkeypatch.delenv("DRIVEAUDIT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["catalog", str(drive), "--name", "d"])
    (drive / "bad.txt").chmod(0)
    try:
        with pytest.raises(SystemExit) as e:
            main(["hash", "d", "--baseline"])
    finally:
        (drive / "bad.txt").chmod(0o644)
    assert e.value.code == 1
    assert "baseline" in capsys.readouterr().err.lower()


# --- round 3 ----------------------------------------------------------------------------------


def _replaced_root_setup(tmp_path, conn, cat, files):
    drive = make_tree(tmp_path / "drive", files)
    cat(drive, "d")
    clone = make_tree(tmp_path / "clone", files)  # same names, sizes and mtimes
    drive.rename(tmp_path / "drive-moved")
    os.symlink(clone, tmp_path / "drive")
    return tmp_path / "drive"


@pytest.mark.parametrize("deep", [False, True])
def test_verify_refuses_a_replaced_root(tmp_path, conn, cat, deep):
    root = _replaced_root_setup(tmp_path, conn, cat, {"a.txt": "aaa", "b/c.txt": "ccc"})
    with pytest.raises(DB.CatalogError, match="cataloged"):
        verify_drive(conn, "d", str(root), deep=deep)


def test_verify_refuses_a_replaced_root_of_an_empty_catalog(tmp_path, conn, cat):
    drive = tmp_path / "drive"
    drive.mkdir()
    cat(drive, "d")
    drive.rename(tmp_path / "drive-moved")
    (tmp_path / "clone").mkdir()
    os.symlink(tmp_path / "clone", drive)
    with pytest.raises(DB.CatalogError, match="cataloged"):
        verify_drive(conn, "d", str(drive))


def test_registration_holds_one_write_lock_across_overlap_check_and_upsert(
    tmp_path, dbpath, monkeypatch
):
    import sqlite3

    from driveaudit import catalog as CAT

    root = make_tree(tmp_path / "d", {"a.txt": "a"})
    seen = []
    real = CAT._refuse_overlap

    def probing(*a, **kw):
        other = sqlite3.connect(str(dbpath), timeout=0.05)
        try:
            other.execute("BEGIN IMMEDIATE")
            seen.append("writable")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            other.close()
        return real(*a, **kw)

    monkeypatch.setattr(CAT, "_refuse_overlap", probing)
    with DB.connect(dbpath) as c1:
        catalog_drive(c1, str(root), name="d", **_quiet())
    assert seen == ["locked"]


def test_same_uuid_under_different_paths_is_an_overlap_blocker(tmp_path, conn, cat):
    make_tree(tmp_path / "nas", {"a": blob("a")})
    make_tree(tmp_path / "usb", {"a2": blob("a")})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "usb", "usb")
    conn.execute("UPDATE drives SET volume_uuid = 'SAME-UUID'")  # as a lost registration race would
    conn.commit()
    cat.hash_all()
    assert ("nas", "usb") in DB.overlapping_drives(conn)
    res = reconcile(conn, "usb", ["nas"])
    assert not res.fully_covered and any("same or nested" in b for b in res.blockers)


def _make_legacy_db(path):
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    schema = DB.SCHEMA.replace(
        "    notes             TEXT,\n    root_ino          INTEGER,"
        "        -- inode of the cataloged root; hashing/verify must find the same one\n"
        "    cross_devices     INTEGER NOT NULL DEFAULT 0\n",
        "    notes             TEXT\n",
    )
    assert "root_ino" not in schema
    c.executescript(schema)
    c.execute("PRAGMA user_version = 1")
    c.commit()
    c.close()


def test_legacy_migration_takes_the_write_lock_before_inspecting_columns(
    tmp_path, dbpath, monkeypatch
):
    import sqlite3

    _make_legacy_db(dbpath)
    seen = []
    real_connect = sqlite3.connect

    class Probe(sqlite3.Connection):
        def execute(self, sql, *a):
            if sql.startswith("PRAGMA table_info(drives)"):
                other = real_connect(str(dbpath), timeout=0.05)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    seen.append("writable")
                except sqlite3.OperationalError:
                    seen.append("locked")
                finally:
                    other.close()
            return super().execute(sql, *a)

    monkeypatch.setattr(
        DB.sqlite3, "connect", lambda p, timeout=5: real_connect(p, timeout=timeout, factory=Probe)
    )
    with DB.connect(dbpath):
        pass
    assert seen == ["locked"]


def test_many_connections_can_migrate_a_legacy_catalog_at_once(tmp_path, dbpath):
    import threading

    _make_legacy_db(dbpath)
    errors = []
    barrier = threading.Barrier(6)

    def go():
        try:
            barrier.wait()
            with DB.connect(dbpath):
                pass
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []


def test_quick_verify_copy_refuses_a_source_ancestor_symlink(tmp_path):
    from driveaudit.plan import verify_copy

    src = make_tree(tmp_path / "src", {"sub/f.bin": blob("x", 100)})
    out = make_tree(tmp_path / "outside", {"f.bin": blob("x", 100)})
    dest = make_tree(tmp_path / "dest", {"d/sub/f.bin": blob("x", 100)})
    mt = (src / "sub" / "f.bin").stat().st_mtime_ns
    shutil.rmtree(src / "sub")
    os.symlink(out, src / "sub")  # an ancestor of the source file is now a link
    os.utime(out / "f.bin", ns=(mt, mt))
    plan = Plan(
        dest=str(dest),
        created=0.0,
        sources={"d": str(src)},
        ops=[PlanOp("d", "sub/f.bin", 100, None, "only-here", mt)],
    )
    assert not verify_copy(plan, quick=True).passed


def test_incomplete_walk_sweeps_nothing(tmp_path, conn, cat):
    make_tree(tmp_path / "d", {"gone.txt": "g", "keep.txt": "k", "locked/in.txt": "i"})
    cat(tmp_path / "d", "d")
    (tmp_path / "d" / "gone.txt").unlink()
    (tmp_path / "d" / "locked").chmod(0)
    try:
        res = cat(tmp_path / "d", "d")
    finally:
        (tmp_path / "d" / "locked").chmod(0o755)
    assert not res.complete and res.removed == 0
    names = {r["rel_path"] for r in conn.execute("SELECT rel_path FROM files")}
    assert "gone.txt" in names  # unconfirmed, so kept; the drive is marked incomplete
