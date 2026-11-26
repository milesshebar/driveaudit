import json
import os

import pytest

from driveaudit import db as DB
from driveaudit.matching import Evidence
from driveaudit.reconcile import reconcile, render_json, render_text

from .conftest import blob, make_tree


def setup_pair(tmp_path, cat, nas_spec, usb_spec):
    make_tree(tmp_path / "nas", nas_spec)
    make_tree(tmp_path / "usb", usb_spec)
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "usb", "usb")
    cat.hash_all()


def by_path(res):
    return {i.rec.rel: i.evidence for i in res.items}


def test_covered_only_here_and_renames(tmp_path, conn, cat):
    setup_pair(
        tmp_path,
        cat,
        {"Projects/a.mov": blob("a"), "Projects/b.mov": blob("b"), "misc/c.txt": "ccc"},
        {
            "a.mov": blob("a"),  # same file, different folder
            "renamed_totally.mov": blob("b"),  # same bytes, different name: still matched by hash
            "unique.mov": blob("unique"),
            "c.txt": "ccc",
            "tiny_unique.txt": "only here",
        },
    )
    res = reconcile(conn, "usb", ["nas"])
    ev = by_path(res)
    assert ev["a.mov"] == ev["renamed_totally.mov"] == ev["c.txt"] == Evidence.VERIFIED
    assert ev["unique.mov"] == ev["tiny_unique.txt"] == Evidence.NONE
    assert not res.fully_covered
    assert sorted(i.rec.rel for i in res.bucket("only-here")) == ["tiny_unique.txt", "unique.mov"]
    assert "ONLY HERE" in render_text(res) and "NOT fully covered" in render_text(res)


def test_size_and_mtime_collision_with_different_content_is_not_covered(tmp_path, conn, cat):
    """Same name, same size, same mtime, different bytes: the classic false 'duplicate'."""
    setup_pair(
        tmp_path,
        cat,
        {"take1.wav": blob("original", 60_000)},
        {"take1.wav": blob("different", 60_000)},
    )
    res = reconcile(conn, "usb", ["nas"])
    assert by_path(res)["take1.wav"] == Evidence.NONE


def test_unhashed_name_and_size_match_is_only_a_hint(tmp_path, conn, cat):
    make_tree(tmp_path / "nas", {"a/take.wav": blob("zz", 60_000)})
    make_tree(tmp_path / "usb", {"take.wav": blob("zz", 60_000)})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "usb", "usb")  # no hashing at all
    res = reconcile(conn, "usb", ["nas"])
    assert by_path(res)["take.wav"] == Evidence.NAME_SIZE
    assert not res.fully_covered  # name+size is never enough
    assert not reconcile(conn, "usb", ["nas"], min_evidence=Evidence.PROBABLE).fully_covered


def test_probable_needs_explicit_opt_in(tmp_path, conn, cat):
    from driveaudit.catalog import hash_drive

    make_tree(tmp_path / "nas", {"x.bin": blob("pp", 60_000)})
    make_tree(tmp_path / "usb", {"y.bin": blob("pp", 60_000)})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "usb", "usb")
    for d in ("nas", "usb"):
        hash_drive(
            conn,
            d,
            str(tmp_path / d),
            expected_uuid_check=False,
            stages=("partial",),
            log=lambda *_: None,
        )
    assert not reconcile(conn, "usb", ["nas"]).fully_covered
    assert reconcile(conn, "usb", ["nas"], min_evidence=Evidence.PROBABLE).fully_covered


def test_fully_covered_drive(tmp_path, conn, cat):
    setup_pair(tmp_path, cat, {"a": blob("a"), "b": blob("b")}, {"a2": blob("a"), "b2": blob("b")})
    res = reconcile(conn, "usb", ["nas"])
    assert res.fully_covered
    assert "never deletes" in render_text(res)
    assert json.loads(render_json(res))["fully_covered"] is True


def test_incomplete_catalog_blocks_the_verdict(tmp_path, conn, cat):
    setup_pair(tmp_path, cat, {"a": blob("a")}, {"a2": blob("a")})
    conn.execute("UPDATE drives SET walk_complete = 0 WHERE drive_name = 'usb'")
    conn.commit()
    res = reconcile(conn, "usb", ["nas"])
    assert not res.fully_covered and res.blockers


def test_live_check_demotes_matches_whose_reference_copy_vanished(tmp_path, conn, cat):
    setup_pair(tmp_path, cat, {"a": blob("a"), "b": blob("b")}, {"a2": blob("a"), "b2": blob("b")})
    os.unlink(tmp_path / "nas" / "b")  # the catalog still believes the NAS has it
    assert reconcile(conn, "usb", ["nas"]).fully_covered  # the stale catalog is fooled...
    res = reconcile(conn, "usb", ["nas"], live=True)  # ...the live check is not
    assert not res.fully_covered
    only = res.bucket("only-here")
    assert [i.rec.rel for i in only] == ["b2"] and "missing or changed" in only[0].note


def test_live_check_reports_unmountable_references(tmp_path, conn, cat):
    import shutil

    setup_pair(tmp_path, cat, {"a": blob("a")}, {"a2": blob("a")})
    shutil.rmtree(tmp_path / "nas")
    res = reconcile(conn, "usb", ["nas"], live=True)
    assert res.live_unchecked == 1 and any("could not be checked live" in w for w in res.warnings)


def test_multiple_references_and_guards(tmp_path, conn, cat):
    make_tree(tmp_path / "ref1", {"a": blob("a")})
    make_tree(tmp_path / "ref2", {"b": blob("b")})
    make_tree(tmp_path / "usb", {"a2": blob("a"), "b2": blob("b")})
    cat(tmp_path / "ref1", "ref1", reference=True)
    cat(tmp_path / "ref2", "ref2", reference=True)
    cat(tmp_path / "usb", "usb")
    cat.hash_all()
    assert reconcile(conn, "usb", DB.reference_names(conn)).fully_covered
    with pytest.raises(DB.CatalogError, match="no reference"):
        reconcile(conn, "usb", [])
    with pytest.raises(DB.CatalogError, match="itself a reference"):
        reconcile(conn, "ref1", ["ref1"])
    with pytest.raises(DB.CatalogError, match="not in the catalog"):
        reconcile(conn, "nope", ["ref1"])


def test_stale_catalog_is_a_documented_failure_mode_and_verify_catches_it(tmp_path, conn, cat):
    """The catalog says covered; the drive has since gained a file. reconcile alone cannot know."""
    from driveaudit.verify import verify_drive

    setup_pair(tmp_path, cat, {"a": blob("a")}, {"a2": blob("a")})
    make_tree(tmp_path / "usb", {"new_footage.mov": blob("new")})
    assert reconcile(conn, "usb", ["nas"]).fully_covered
    assert any("run `driveaudit verify`" in w for w in reconcile(conn, "usb", ["nas"]).warnings)
    assert not verify_drive(conn, "usb").ok
