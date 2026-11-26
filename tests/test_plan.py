import json
import os
import shlex

import pytest

from driveaudit import db as DB
from driveaudit.plan import (
    build_plan,
    plan_from_json,
    plan_to_json,
    render_text,
    rsync_command,
    verify_copy,
    write_file_lists,
)

from .conftest import blob, make_tree


def scenario(tmp_path, cat):
    make_tree(tmp_path / "nas", {"keep/a.mov": blob("a")})
    make_tree(
        tmp_path / "usb1",
        {
            "a.mov": blob("a"),
            "new/b.mov": blob("b"),
            'it\'s a "weird" $name & co.txt': "odd",
            "shared.bin": blob("s"),
        },
    )
    make_tree(tmp_path / "usb2", {"also_shared.bin": blob("s"), "c.mov": blob("c")})
    for n, ref in (("nas", True), ("usb1", None), ("usb2", None)):
        cat(tmp_path / n, n, reference=ref)
    cat.hash_all()


def copy_like_rsync(plan, dest):
    for op in plan.ops:
        src = os.path.join(plan.sources[op.drive], op.rel)
        dst = os.path.join(dest, op.drive, op.rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(src, "rb") as f, open(dst, "wb") as g:
            g.write(f.read())


def test_plan_copies_only_what_the_reference_lacks_and_dedupes_within_the_plan(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1", "usb2"], ["nas"], str(tmp_path / "dest"))
    paths = {(o.drive, o.rel) for o in plan.ops}
    assert ("usb1", "a.mov") not in paths  # already on the NAS
    assert plan.covered_by_reference == 1
    assert ("usb1", "new/b.mov") in paths and ("usb2", "c.mov") in paths
    assert sum(1 for o in plan.ops if o.size == 40_000 and o.rel.endswith("shared.bin")) == 1
    assert plan.covered_by_plan == 1
    assert all(o.reason == "only-here" for o in plan.ops)


def test_unhashed_files_are_always_copied(tmp_path, conn, cat):
    make_tree(tmp_path / "nas", {"x.bin": blob("x")})
    make_tree(tmp_path / "usb", {"x.bin": blob("x")})
    cat(tmp_path / "nas", "nas", reference=True)
    cat(tmp_path / "usb", "usb")  # same name and size, but never hashed
    plan = build_plan(conn, ["usb"], ["nas"], str(tmp_path / "d"))
    assert len(plan.ops) == 1 and plan.ops[0].reason == "unconfirmed"


def test_rsync_command_quotes_hostile_paths_and_never_overwrites(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1"], ["nas"], str(tmp_path / "dest dir"))
    lists = write_file_lists(plan, tmp_path / "lists")
    cmd = rsync_command(plan, "usb1", lists[0])
    argv = shlex.split(cmd)  # round-trips through a real shell parser
    assert argv[0] == "rsync" and "--ignore-existing" in argv and "--from0" in argv
    assert argv[-1] == str(tmp_path / "dest dir") + "/usb1/"
    names = lists[0].read_bytes().split(b"\0")[:-1]
    assert b'it\'s a "weird" $name & co.txt' in names
    with pytest.raises(FileExistsError):
        write_file_lists(plan, tmp_path / "lists")  # never clobbers an earlier list


def test_plan_json_roundtrip_and_text(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1", "usb2"], ["nas"], str(tmp_path / "dest"))
    again = plan_from_json(plan_to_json(plan))
    assert [(o.drive, o.rel, o.size) for o in again.ops] == [
        (o.drive, o.rel, o.size) for o in plan.ops
    ]
    text = render_text(plan)
    assert "nothing has been copied" in text and "verify-copy" in text


def test_plan_warns_when_destination_is_too_small(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1"], ["nas"], str(tmp_path / "d"), dest_free=10)
    assert plan.fits is False and any("free" in w for w in plan.warnings)


def test_verify_copy_passes_for_a_faithful_copy(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1", "usb2"], ["nas"], str(tmp_path / "dest"))
    copy_like_rsync(plan, str(tmp_path / "dest"))
    res = verify_copy(plan)
    assert res.passed and res.checked == len(plan.ops)
    assert verify_copy(plan, quick=True).passed


def test_verify_copy_catches_missing_truncated_and_silently_corrupted_files(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1", "usb2"], ["nas"], str(tmp_path / "dest"))
    copy_like_rsync(plan, str(tmp_path / "dest"))
    dest = tmp_path / "dest"
    os.unlink(dest / "usb2" / "c.mov")  # missing
    (dest / "usb1" / "new" / "b.mov").write_bytes(b"truncated")  # wrong size
    s = (
        dest / "usb1" / "shared.bin"
        if (dest / "usb1" / "shared.bin").exists()
        else dest / "usb2" / "also_shared.bin"
    )
    data = bytearray(s.read_bytes())
    data[20_000] ^= 1  # same size, one flipped bit mid-file
    s.write_bytes(bytes(data))
    res = verify_copy(plan)
    kinds = {p.problem for p in res.problems}
    assert not res.passed and {"missing_dest", "size_mismatch", "hash_mismatch"} <= kinds
    # quick mode cannot see the flipped bit: that is exactly why full mode is the default
    assert "hash_mismatch" not in {p.problem for p in verify_copy(plan, quick=True).problems}


def test_verify_copy_flags_changed_or_missing_source_and_symlinked_destination(tmp_path, conn, cat):
    scenario(tmp_path, cat)
    plan = build_plan(conn, ["usb1"], ["nas"], str(tmp_path / "dest"))
    copy_like_rsync(plan, str(tmp_path / "dest"))
    victim = plan.ops[0]
    dst = tmp_path / "dest" / victim.drive / victim.rel
    real = tmp_path / "elsewhere"
    real.write_bytes(dst.read_bytes())
    dst.unlink()
    os.symlink(real, dst)  # a symlink is not a copy
    assert "dest_not_regular_file" in {p.problem for p in verify_copy(plan).problems}
    os.unlink(os.path.join(plan.sources[victim.drive], victim.rel))
    assert "missing_source" in {p.problem for p in verify_copy(plan).problems}


def test_verify_copy_rejects_path_traversal_from_a_tampered_plan(tmp_path):
    plan_json = {
        "version": 1,
        "created": 0,
        "dest": str(tmp_path / "dest"),
        "sources": {"d": str(tmp_path)},
        "ops": [
            {"drive": "d", "path": "../../etc/passwd", "size": 1, "full_hash": None},
            {"drive": "d", "path": "/etc/passwd", "size": 1, "full_hash": None},
        ],
    }
    res = verify_copy(plan_from_json(json.dumps(plan_json)))
    assert [p.problem for p in res.problems] == ["unsafe_path", "unsafe_path"]


def test_plan_file_version_is_checked():
    with pytest.raises(DB.CatalogError):
        plan_from_json('{"version": 99}')
