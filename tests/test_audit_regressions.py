"""One test per finding from the adversarial audit (see AUDIT.md)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from driveaudit import db as DB
from driveaudit.catalog import catalog_drive, resolve_name
from driveaudit.cli import main
from driveaudit.config import load_settings
from driveaudit.dedupe import find_duplicates
from driveaudit.plan import (
    build_plan,
    plan_from_json,
    plan_to_json,
    rsync_command,
    verify_copy,
    write_file_lists,
)
from driveaudit.reconcile import reconcile
from driveaudit.util import check_output_path, db_to_path, display_path, path_to_db, write_private
from driveaudit.volumes import Volume

from .conftest import blob, make_tree, vol

quiet = lambda *_: None  # noqa: E731


def run(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        main(list(argv))
    out = capsys.readouterr()
    return e.value.code, out.out, out.err


# 1. two catalogs of one drive at once ----------------------------------------------------------
def test_concurrent_catalogs_cannot_sweep_each_others_rows(tmp_path, dbpath, monkeypatch):
    make_tree(tmp_path / "d", {f"f{i}.txt": str(i) for i in range(10)})
    import driveaudit.catalog as C

    real_walker = C.Walker
    state = {"inner_done": False}

    class PausingWalker(real_walker):
        def __iter__(self):
            for n, e in enumerate(super().__iter__()):
                if n == 4 and not state["inner_done"]:
                    state["inner_done"] = True
                    monkeypatch.setattr(C, "Walker", real_walker)  # inner run uses the plain one
                    with DB.connect(dbpath) as other:
                        inner = catalog_drive(
                            other, str(tmp_path / "d"), name="d", volume=vol("d"), log=quiet
                        )
                    assert inner.complete
                yield e

    with DB.connect(dbpath) as conn:
        monkeypatch.setattr(C, "Walker", PausingWalker)
        outer = catalog_drive(conn, str(tmp_path / "d"), name="d", volume=vol("d"), log=quiet)
        assert not outer.complete and outer.removed == 0
        assert any("same time" in w for w in outer.warnings)
        assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 10


# 2. incomplete reference ------------------------------------------------------------------------
def test_incomplete_reference_is_a_blocker(tmp_path, conn, cat):
    make_tree(tmp_path / "nas", {f"f{i}.bin": blob(str(i), 20_000) for i in range(120)})
    make_tree(tmp_path / "old", {f"o{i}.bin": blob(str(i), 20_000) for i in range(3)})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "old", "old")
    cat.hash_all()
    for i in range(100):
        (tmp_path / "nas" / f"f{i}.bin").unlink()
    res = cat(
        tmp_path / "nas", "nas"
    )  # truncated listing guard keeps the rows but marks incomplete
    assert not res.complete
    r = reconcile(conn, "old", ["nas"])
    assert not r.fully_covered and any("BLOCKER: reference" in w for w in r.warnings)


# 3. same physical files under two names / nested ------------------------------------------------
def test_overlapping_catalogs_are_refused(tmp_path, conn, cat):
    make_tree(tmp_path / "nas", {"photos/a.jpg": blob("a"), "b.bin": blob("b")})
    cat(tmp_path / "nas", "nas", reference=True)
    with pytest.raises(DB.CatalogError, match="overlaps"):
        cat(tmp_path / "nas", "nas-again")
    with pytest.raises(DB.CatalogError, match="overlaps"):
        cat(tmp_path / "nas" / "photos", "old")
    cat(tmp_path / "nas" / "photos", "old", allow_overlap=True)
    cat.hash_all()
    r = reconcile(conn, "old", ["nas"])
    assert not r.fully_covered and any("BLOCKER" in w and "nested" in w for w in r.warnings)
    assert any("same or nested" in w for w in find_duplicates(conn).warnings)


def test_same_volume_uuid_under_another_name_is_refused(tmp_path, conn):
    make_tree(tmp_path / "one", {"a": "a"})
    make_tree(tmp_path / "two", {"b": "b"})
    catalog_drive(conn, str(tmp_path / "one"), name="one", volume=vol("x", "UUID-1"), log=quiet)
    with pytest.raises(DB.CatalogError, match="overlaps"):
        catalog_drive(conn, str(tmp_path / "two"), name="two", volume=vol("x", "UUID-1"), log=quiet)


# 4. skip rules are visible -----------------------------------------------------------------------
def test_skipped_items_are_counted_and_reported(tmp_path, conn, cat):
    make_tree(
        tmp_path / "nas",
        {"keep.bin": blob("k")},
    )
    make_tree(
        tmp_path / "old",
        {
            "keep.bin": blob("k"),
            "Backups.backupdb/mac/file": "tm",
            "thesis.TMP": "draft",
            "lost+found/#1234": "recovered by fsck",
            ".Trashes/501/x": "trash",
        },
    )
    cat(tmp_path / "nas", "nas", reference=True)
    res = cat(tmp_path / "old", "old")
    cat.hash_all()
    assert (
        res.skipped_dirs == 2 and res.skipped_files == 1
    )  # Backups.backupdb, .Trashes; thesis.TMP
    r = reconcile(conn, "old", ["nas"])
    assert any("skip rules" in w and "not covered" in w for w in r.warnings)
    assert "lost+found/#1234" in {i.rec.rel for i in r.items}  # fsck-recovered data is not skipped


# 5. hash cannot land on a row changed meanwhile ---------------------------------------------------
def test_hash_is_discarded_if_the_row_changed_during_hashing(tmp_path, conn, cat, monkeypatch):
    import driveaudit.catalog as C

    make_tree(tmp_path / "a", {"x.bin": blob("x")})
    make_tree(tmp_path / "b", {"y.bin": blob("x")})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    real = C.partial_hash

    def racing(path, stamp, root=None):
        h = real(path, stamp, root)
        conn.execute("UPDATE files SET mtime_ns = mtime_ns + 1 WHERE rel_path = 'x.bin'")  # re-walk
        conn.commit()
        return h

    monkeypatch.setattr(C, "partial_hash", racing)
    r = C.hash_drive(
        conn, "a", str(tmp_path / "a"), expected_uuid_check=False, stages=("partial",), log=quiet
    )
    assert r.partial_hashed == 0 and r.changed_during_hash == 1
    assert (
        conn.execute("SELECT partial_hash FROM files WHERE rel_path='x.bin'").fetchone()[0] is None
    )


# 6. --output through symlinks ----------------------------------------------------------------------
def test_output_never_follows_symlinks_into_a_drive(tmp_path):
    drive = make_tree(tmp_path / "old", {"keep.txt": "precious"})
    out = tmp_path / "reports"
    out.mkdir()
    os.symlink(drive / "NEW.txt", out / "dangling.txt")
    with pytest.raises(ValueError, match="symlink"):
        write_private(out / "dangling.txt", "x", protected_roots=[str(drive)])
    os.symlink(drive / "keep.txt", out / "keep_link.txt")
    with pytest.raises(ValueError, match="symlink"):
        write_private(out / "keep_link.txt", "x", force=True, protected_roots=[str(drive)])
    os.symlink(drive, tmp_path / "linkdir")
    with pytest.raises(ValueError, match="inside a cataloged drive"):
        check_output_path(tmp_path / "linkdir" / "r.txt", protected_roots=[str(drive)])
    assert not (drive / "NEW.txt").exists() and (drive / "keep.txt").read_text() == "precious"


def test_private_files_are_owner_only(tmp_path):
    p = write_private(tmp_path / "plan.json", "{}")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_catalog_directory_and_file_are_private(tmp_path):
    db = tmp_path / "fresh" / "catalog.db"
    with DB.connect(db):
        pass
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_forget_vacuums_the_paths_out_of_the_file(tmp_path, dbpath):
    make_tree(tmp_path / "d", {"very-secret-project-name.txt": "x"})
    with DB.connect(dbpath) as conn:
        catalog_drive(conn, str(tmp_path / "d"), name="d", volume=vol("d"), log=quiet)
        DB.forget_drive(conn, "d")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert b"very-secret-project-name" not in dbpath.read_bytes()


# 7. plan / verify-copy false PASS ----------------------------------------------------------------
def scenario(tmp_path, cat):
    make_tree(tmp_path / "nas", {"a": blob("a")})
    make_tree(tmp_path / "old", {"a2": blob("a"), "only.bin": blob("only")})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "old", "old")
    cat.hash_all()


def test_plan_refuses_a_destination_inside_a_source_drive(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    with pytest.raises(DB.CatalogError, match="inside the source drive"):
        build_plan(conn, ["old"], ["nas"], str(tmp_path / "old" / "rescued"))


def test_plan_with_blockers_exits_nonzero_and_verify_copy_cannot_pass(
    tmp_path, conn, cat, capsys, monkeypatch
):
    scenario(tmp_path, cat)
    conn.execute("UPDATE drives SET walk_complete = 0 WHERE drive_name = 'old'")
    conn.commit()
    plan = build_plan(conn, ["old"], ["nas"], str(tmp_path / "dest"))
    assert plan.blockers
    dest = tmp_path / "dest" / "old"
    dest.mkdir(parents=True)
    for op in plan.ops:
        (dest / op.rel).write_bytes((tmp_path / "old" / op.rel).read_bytes())
    res = verify_copy(plan_from_json(plan_to_json(plan)))
    assert not res.passed and any(p.problem == "plan_has_blockers" for p in res.problems)
    monkeypatch.setenv("DRIVEAUDIT_DB", str(conn.execute("PRAGMA database_list").fetchone()[2]))
    assert run(capsys, "plan", "old", "--dest", str(tmp_path / "d2"))[0] == 1


def test_verify_copy_detects_source_changed_after_planning_even_with_same_size(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["old"], ["nas"], str(tmp_path / "dest"))
    dest = tmp_path / "dest" / "old"
    dest.mkdir(parents=True)
    for op in plan.ops:
        (dest / op.rel).write_bytes((tmp_path / "old" / op.rel).read_bytes())
    assert verify_copy(plan).passed
    os.utime(tmp_path / "old" / "only.bin", (5, 5))
    assert "source_changed_since_planned" in {p.problem for p in verify_copy(plan).problems}


def test_verify_copy_rejects_a_symlinked_directory_inside_the_destination(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["old"], ["nas"], str(tmp_path / "dest"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for op in plan.ops:
        (elsewhere / op.rel).write_bytes((tmp_path / "old" / op.rel).read_bytes())
    (tmp_path / "dest").mkdir()
    os.symlink(elsewhere, tmp_path / "dest" / "old")  # dest/old -> elsewhere
    res = verify_copy(plan)
    assert not res.passed and {p.problem for p in res.problems} == {"dest_not_regular_file"}


def test_verify_copy_checks_the_source_volume_identity(tmp_path, conn, cat, monkeypatch):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["old"], ["nas"], str(tmp_path / "dest"))
    plan.source_uuids["old"] = "UUID-OF-THE-REAL-DRIVE"
    monkeypatch.setattr("driveaudit.plan.volume_uuid", lambda p: "UUID-OF-A-CLONE")
    dest = tmp_path / "dest" / "old"
    dest.mkdir(parents=True)
    for op in plan.ops:
        (dest / op.rel).write_bytes((tmp_path / "old" / op.rel).read_bytes())
    assert "source_is_a_different_volume" in {p.problem for p in verify_copy(plan).problems}


def test_rsync_command_ends_options_and_is_absolute(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["old"], ["nas"], "relative/-evil-dest")
    lst = write_file_lists(plan, tmp_path / "lists")[0]
    import shlex

    argv = shlex.split(rsync_command(plan, "old", lst))
    assert argv[argv.index("--") + 1].startswith("/") and argv[-1].startswith("/")
    assert stat.S_IMODE(lst.stat().st_mode) == 0o600


def test_malformed_plan_files_are_errors_not_tracebacks():
    for bad in (
        '{"version":1,"ops":[{}]}',
        "not json at all",
        '{"version":1,"dest":5,"sources":{},"ops":[]}',
    ):
        with pytest.raises(DB.CatalogError):
            plan_from_json(bad)
    ok = {
        "version": 1,
        "created": 0,
        "dest": "/d",
        "sources": {},
        "ops": [{"drive": "a", "path": "p", "size": "7"}],
    }
    with pytest.raises(DB.CatalogError):
        plan_from_json(json.dumps(ok))


# 8/9. drive identity ------------------------------------------------------------------------------
def test_same_label_with_one_side_unidentifiable_is_not_merged(tmp_path, conn):
    make_tree(tmp_path / "a", {"a.bin": "a"})
    make_tree(tmp_path / "b", {"b.bin": "b"})
    catalog_drive(
        conn, str(tmp_path / "a"), name="Untitled", volume=vol("Untitled", None), log=quiet
    )
    name, warns = resolve_name(
        conn, None, Volume(str(tmp_path / "b"), "Untitled", "B-UUID"), str(tmp_path / "b")
    )
    assert name != "Untitled" and warns
    name, _ = resolve_name(
        conn, None, Volume(str(tmp_path / "b"), "Untitled", None), str(tmp_path / "b")
    )
    assert name == "Untitled-2"  # unidentifiable and mounted somewhere else: a new entry
    same, _ = resolve_name(
        conn, None, Volume(str(tmp_path / "a"), "Untitled", None), str(tmp_path / "a")
    )
    assert same == "Untitled"  # same label, same place, neither identifiable: the same drive


# 11. codec injectivity ---------------------------------------------------------------------------------
def test_path_codec_is_injective_for_names_that_look_like_the_marker():
    tricky = "\x01b64:Zm9v"
    assert db_to_path(path_to_db(tricky)) == tricky
    assert path_to_db(tricky) != path_to_db("foo")
    assert db_to_path(path_to_db("foo")) == "foo"


# security: output encoding, config trust ------------------------------------------------------------------
def test_control_characters_in_names_are_escaped_for_display():
    shown = display_path("evil\x1b]0;PWNED\x07\x1b[2K.txt")
    assert "\x1b" not in shown and "\x07" not in shown and "\\x1b" in shown
    forged = display_path("a\nPASS - everything is fine.txt")
    assert "\n" not in forged


def test_list_null_gives_raw_paths_and_plain_list_cannot_split_names(
    tmp_path, monkeypatch, capsysbinary
):
    monkeypatch.setenv("DRIVEAUDIT_DB", str(tmp_path / "s" / "c.db"))
    make_tree(tmp_path / "nas", {"x": "x"})
    make_tree(tmp_path / "usb", {"line1\nline2.txt": "u"})
    for argv in (
        ["catalog", str(tmp_path / "nas"), "--name", "nas", "--reference"],
        ["catalog", str(tmp_path / "usb"), "--name", "usb"],
    ):
        with pytest.raises(SystemExit):
            main(argv)
    capsysbinary.readouterr()
    with pytest.raises(SystemExit):
        main(["reconcile", "usb", "--list", "only-here", "-0"])
    assert capsysbinary.readouterr().out == b"line1\nline2.txt\0"
    with pytest.raises(SystemExit):
        main(["reconcile", "usb", "--list", "only-here"])
    assert capsysbinary.readouterr().out == b"line1\\x0aline2.txt\n"


def test_config_in_the_current_directory_is_not_trusted(tmp_path, monkeypatch):
    for k in ("DRIVEAUDIT_DB", "DRIVEAUDIT_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    hostile = tmp_path / "drive"
    hostile.mkdir()
    (hostile / "driveaudit.toml").write_text('[scan]\nextra_skip_dirs = ["Photos"]\n')
    monkeypatch.chdir(hostile)
    s = load_settings()
    assert s.source is None and "Photos" not in s.skip_dirs


def test_config_booleans_must_be_booleans(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[scan]\ninclude_time_machine = "false"\n')
    with pytest.raises(ValueError, match="true or false"):
        load_settings(str(cfg))


# misc findings -----------------------------------------------------------------------------------------
def test_catalog_that_suddenly_lists_nothing_is_never_a_mass_delete(tmp_path, conn, cat):
    make_tree(tmp_path / "d", {f"f{i}": str(i) for i in range(5)})
    cat(tmp_path / "d", "d")
    for i in range(5):
        (tmp_path / "d" / f"f{i}").unlink()
    res = cat(tmp_path / "d", "d")  # a tiny catalog, but an empty listing is still suspicious
    assert not res.complete and conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 5


def test_quick_verify_copy_does_not_claim_content_equality(tmp_path, conn, cat):
    from driveaudit.plan import render_copy_text

    scenario(tmp_path, cat)
    plan = build_plan(conn, ["old"], ["nas"], str(tmp_path / "dest"))
    dest = tmp_path / "dest" / "old"
    dest.mkdir(parents=True)
    for op in plan.ops:
        (dest / op.rel).write_bytes((tmp_path / "old" / op.rel).read_bytes())
    text = render_copy_text(verify_copy(plan, quick=True), quick=True)
    assert "did NOT compare contents" in text and "identical" not in text
