import os
import shutil
import unicodedata

import pytest

from driveaudit import db as DB
from driveaudit.catalog import catalog_drive, resolve_name
from driveaudit.hasher import FileChanged  # noqa: F401  (imported for symmetry with hasher tests)
from driveaudit.util import db_to_path, path_to_db
from driveaudit.walker import DriveGone, Walker

from .conftest import blob, make_tree, vol

quiet = lambda *_: None  # noqa: E731


def rows(conn, drive):
    return {
        r["rel_path"]: r for r in conn.execute("SELECT * FROM files WHERE drive_name=?", (drive,))
    }


def test_walk_records_files_and_skips_housekeeping(tmp_path, conn, cat):
    make_tree(
        tmp_path / "d",
        {
            "a.txt": "hello",
            "sub/b.bin": blob("b"),
            ".DS_Store": "x",
            "@Recycle/old.txt": "x",
            "sub/.Trashes/t": "x",
            "sub/scratch.tmp": "x",
        },
    )
    res = cat(tmp_path / "d", "d")
    assert res.complete and res.files == 2
    assert set(rows(conn, "d")) == {"a.txt", "sub/b.bin"}
    d = DB.get_drive(conn, "d")
    assert d["total_bytes"] == 5 + 40_000 and d["walk_complete"] == 1


def test_rewalk_keeps_hashes_only_for_unchanged_files(tmp_path, conn, cat):
    make_tree(tmp_path / "a", {"same.bin": blob("s"), "edit.bin": blob("e")})
    make_tree(tmp_path / "b", {"same.bin": blob("s"), "edit.bin": blob("e")})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    cat.hash_all()
    assert all(r["full_hash"] for r in rows(conn, "a").values())
    # edit one file in place, same size, new mtime
    p = tmp_path / "a" / "edit.bin"
    data = bytearray(p.read_bytes())
    data[100] ^= 0xFF
    p.write_bytes(bytes(data))
    os.utime(p, (1_800_000_000, 1_800_000_000))
    cat(tmp_path / "a", "a")
    r = rows(conn, "a")
    assert r["same.bin"]["full_hash"] and r["same.bin"]["partial_hash"]
    assert r["edit.bin"]["full_hash"] is None and r["edit.bin"]["partial_hash"] is None


def test_deleted_and_moved_files_are_swept_after_complete_walk(tmp_path, conn, cat):
    make_tree(tmp_path / "d", {"keep.txt": "k", "gone.txt": "g", "mv/old.txt": "m"})
    cat(tmp_path / "d", "d")
    (tmp_path / "d" / "gone.txt").unlink()
    os.rename(tmp_path / "d" / "mv" / "old.txt", tmp_path / "d" / "new.txt")
    res = cat(tmp_path / "d", "d")
    assert res.removed == 2
    assert set(rows(conn, "d")) == {"keep.txt", "new.txt"}


def test_symlinks_are_never_followed(tmp_path, conn, cat):
    outside = make_tree(tmp_path / "outside", {"secret.txt": "do not catalog", "deep/x.txt": "x"})
    make_tree(tmp_path / "d", {"real.txt": "r"})
    os.symlink(outside, tmp_path / "d" / "link_to_dir")
    os.symlink(outside / "secret.txt", tmp_path / "d" / "link_to_file")
    os.symlink(tmp_path / "d", tmp_path / "d" / "loop")  # cycle back to the root
    res = cat(tmp_path / "d", "d")
    assert set(rows(conn, "d")) == {"real.txt"}
    assert res.symlinks == 3
    assert DB.get_drive(conn, "d")["skipped_symlinks"] == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permissions")
def test_unreadable_directory_is_reported_and_its_old_rows_survive(tmp_path, conn, cat):
    make_tree(tmp_path / "d", {"ok.txt": "ok", "locked/inner.txt": "inner"})
    cat(tmp_path / "d", "d")
    locked = tmp_path / "d" / "locked"
    locked.chmod(0)
    try:
        res = cat(tmp_path / "d", "d")
    finally:
        locked.chmod(0o755)
    assert not res.complete and res.errors == 1
    assert "locked/inner.txt" in rows(conn, "d")  # not swept: we could not look
    assert DB.get_drive(conn, "d")["walk_complete"] == 0
    assert conn.execute("SELECT COUNT(*) FROM walk_errors").fetchone()[0] == 1


def test_drive_unplugged_mid_walk_keeps_catalog_and_marks_incomplete(
    tmp_path, conn, cat, monkeypatch
):
    spec = {f"dir{i:02d}/f{j}.txt": f"{i}-{j}" for i in range(10) for j in range(3)}
    make_tree(tmp_path / "d", spec)
    cat(tmp_path / "d", "d")
    before = set(rows(conn, "d"))

    real_scandir = os.scandir
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 4:  # the "cable is pulled" moment
            shutil.rmtree(tmp_path / "d")
        return real_scandir(path)

    monkeypatch.setattr("driveaudit.walker.os.scandir", flaky)
    res = catalog_drive(conn, str(tmp_path / "d"), name="d", volume=vol("d"), log=quiet)
    assert res.complete is False
    assert any("disappeared" in w for w in res.warnings)
    assert set(rows(conn, "d")) == before  # nothing was swept
    assert DB.get_drive(conn, "d")["walk_complete"] == 0


def test_walker_raises_drivegone_directly(tmp_path, monkeypatch):
    make_tree(tmp_path / "d", {"a/1": "1", "b/2": "2", "c/3": "3"})
    w = Walker(tmp_path / "d")
    it = iter(w)
    next(it)
    shutil.rmtree(tmp_path / "d")
    monkeypatch.setattr(Walker, "CHECK_EVERY", 1)
    with pytest.raises(DriveGone):
        list(it)


def test_truncated_listing_is_not_treated_as_mass_deletion(tmp_path, conn, cat):
    make_tree(tmp_path / "d", {f"f{i}.txt": str(i) for i in range(200)})
    cat(tmp_path / "d", "d")
    for i in range(150):  # simulate an unmounted share that now lists almost nothing
        (tmp_path / "d" / f"f{i}.txt").unlink()
    res = cat(tmp_path / "d", "d")
    assert not res.complete and res.removed == 0
    assert len(rows(conn, "d")) == 200
    assert any("truncated" in w for w in res.warnings)
    res = cat(tmp_path / "d", "d", allow_shrink=True)
    assert res.complete and res.removed == 150 and len(rows(conn, "d")) == 50


def test_odd_and_unicode_names(tmp_path, conn, cat):
    nfd = unicodedata.normalize("NFD", "café é.txt")
    names = [
        "naïve 日本語 🎬.txt",
        "-dash.txt",
        "sp ace  double.txt",
        "tab\tname.txt",
        "new\nline.txt",
        nfd,
    ]
    created = []
    for n in names:
        try:
            make_tree(tmp_path / "d", {f"odd/{n}": n})
            created.append(n)
        except OSError:
            pass
    assert created
    cat(tmp_path / "d", "d")
    stored = {db_to_path(k) for k in rows(conn, "d")}
    for n in created:
        assert f"odd/{n}" in stored or unicodedata.normalize("NFC", f"odd/{n}") in {
            unicodedata.normalize("NFC", s) for s in stored
        }


def test_path_codec_roundtrips_undecodable_bytes():
    raw = b"bad-\xff\xfe-name.txt"
    s = os.fsdecode(raw)
    stored = path_to_db(s)
    stored.encode("utf-8")  # must be storable
    assert db_to_path(stored) == s and os.fsencode(db_to_path(stored)) == raw
    assert path_to_db("plain/é.txt") == "plain/é.txt"


def test_undecodable_filename_on_disk_is_cataloged(tmp_path, conn, cat):
    root = tmp_path / "d"
    root.mkdir()
    try:
        with open(os.path.join(os.fsencode(root), b"bad-\xff.txt"), "wb") as f:
            f.write(b"x")
    except OSError:
        pytest.skip("filesystem refuses non-UTF-8 names")
    res = cat(root, "d")
    assert res.complete and res.files == 1


def test_hard_links_are_flagged(tmp_path, conn, cat):
    make_tree(tmp_path / "d", {"a.bin": blob("h")})
    os.link(tmp_path / "d" / "a.bin", tmp_path / "d" / "b.bin")
    cat(tmp_path / "d", "d")
    keys = {r["link_key"] for r in rows(conn, "d").values()}
    assert len(keys) == 1 and None not in keys


def test_other_volumes_inside_tree_are_not_crossed(tmp_path, conn, cat, monkeypatch):
    make_tree(tmp_path / "d", {"a.txt": "a", "inner/b.txt": "b"})
    real = os.stat(tmp_path / "d").st_dev
    monkeypatch.setattr(
        "driveaudit.walker._device_of", lambda e: real + 1 if e.name == "inner" else real
    )
    res = cat(tmp_path / "d", "d")
    assert set(rows(conn, "d")) == {"a.txt"} and res.other_mounts == 1


def test_database_inside_cataloged_tree_is_refused(tmp_path, conn):
    make_tree(tmp_path / "d", {"a": "a"})
    with pytest.raises(DB.CatalogError, match="inside the tree"):
        catalog_drive(
            conn, str(tmp_path / "d"), db_path=tmp_path / "d" / "x.db", volume=vol("d"), log=quiet
        )


def test_two_different_volumes_with_one_label_are_not_merged(tmp_path, conn):
    make_tree(tmp_path / "one", {"a.txt": "a"})
    make_tree(tmp_path / "two", {"b.txt": "b"})
    catalog_drive(conn, str(tmp_path / "one"), volume=vol("Backup", "UUID-AAAA-1111"), log=quiet)
    name, warns = resolve_name(conn, None, vol("Backup", "UUID-BBBB-2222"))
    assert name == "Backup-uuid-bbb" and warns
    catalog_drive(conn, str(tmp_path / "two"), volume=vol("Backup", "UUID-BBBB-2222"), log=quiet)
    names = {d["drive_name"] for d in DB.get_drives(conn)}
    assert names == {"Backup", "Backup-uuid-bbb"}
    assert {
        r["rel_path"] for r in conn.execute("SELECT rel_path FROM files WHERE drive_name='Backup'")
    } == {"a.txt"}
    with pytest.raises(DB.CatalogError):  # an explicit clashing --name is an error, not a merge
        resolve_name(conn, "Backup", vol("Backup", "UUID-CCCC-3333"))


def test_same_volume_replugged_is_the_same_drive(tmp_path, conn):
    make_tree(tmp_path / "one", {"a.txt": "a"})
    catalog_drive(conn, str(tmp_path / "one"), volume=vol("Backup", "UUID-AAAA"), log=quiet)
    assert resolve_name(conn, None, vol("Backup", "UUID-AAAA")) == ("Backup", [])


def test_catalog_requires_a_directory(tmp_path, conn):
    with pytest.raises(DB.CatalogError):
        catalog_drive(conn, str(tmp_path / "nope"), volume=vol("x"), log=quiet)
