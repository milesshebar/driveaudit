"""End-to-end through the real CLI entry point, on synthetic drives."""

from __future__ import annotations

import json
import os

import pytest

from driveaudit.cli import main

from .conftest import blob, make_tree


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DRIVEAUDIT_DB", str(tmp_path / "state" / "catalog.db"))
    monkeypatch.delenv("DRIVEAUDIT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    make_tree(tmp_path / "nas", {"Projects/a.mov": blob("a"), "Projects/b.mov": blob("b")})
    make_tree(
        tmp_path / "usb", {"a_copy.mov": blob("a"), "only_here.mov": blob("z"), "b.mov": blob("b")}
    )
    return tmp_path


def run(capsys, *argv):
    with pytest.raises(SystemExit) as e:
        main(list(argv))
    out = capsys.readouterr()
    return e.value.code, out.out, out.err


def test_full_workflow(env, capsys):
    assert (
        run(capsys, "catalog", str(env / "nas"), "--name", "nas", "--reference", "--hash")[0] == 0
    )
    assert run(capsys, "catalog", str(env / "usb"), "--name", "usb", "--hash")[0] == 0
    code, out, _ = run(capsys, "drives")
    assert code == 0 and "nas" in out and "reference" in out and "usb" in out

    code, out, _ = run(capsys, "dedupe")
    assert code == 0 and "verified (full SHA-256 match): 2 groups" in out

    code, out, _ = run(capsys, "reconcile", "usb")
    assert code == 1 and "ONLY HERE" in out  # exit 1: not fully covered, scriptable

    code, out, _ = run(capsys, "reconcile", "usb", "--list", "only-here")
    assert out.strip() == "only_here.mov"

    plan_file = env / "plan.json"
    code, out, _ = run(
        capsys,
        "plan",
        "usb",
        "--dest",
        str(env / "consolidated"),
        "--output",
        str(plan_file),
        "--export-lists",
        str(env / "lists"),
    )
    assert code == 0 and "nothing has been copied" in out and "rsync" in out
    assert json.loads(plan_file.read_text())["ops"][0]["path"] == "only_here.mov"
    assert not (env / "consolidated").exists()  # planning never writes to the destination

    assert run(capsys, "verify-copy", str(plan_file))[0] == 1  # nothing copied yet
    os.makedirs(env / "consolidated" / "usb")
    (env / "consolidated" / "usb" / "only_here.mov").write_bytes(
        (env / "usb" / "only_here.mov").read_bytes()
    )
    code, out, _ = run(capsys, "verify-copy", str(plan_file))
    assert code == 0 and "PASS" in out

    assert run(capsys, "verify", "usb")[0] == 0
    (env / "usb" / "surprise.txt").write_text("new")
    assert run(capsys, "verify", "usb")[0] == 1


def test_output_files_are_never_overwritten_or_written_onto_a_drive(env, capsys):
    run(capsys, "catalog", str(env / "nas"), "--name", "nas", "--reference", "--hash")
    run(capsys, "catalog", str(env / "usb"), "--name", "usb", "--hash")
    out_file = env / "report.txt"
    out_file.write_text("precious")
    code, _, err = run(capsys, "dedupe", "-o", str(out_file))
    assert code == 2 and "already exists" in err and out_file.read_text() == "precious"
    assert run(capsys, "dedupe", "-o", str(out_file), "--force")[0] == 0
    code, _, err = run(capsys, "dedupe", "-o", str(env / "usb" / "report.txt"))
    assert code == 2 and "inside a cataloged drive" in err
    assert not (env / "usb" / "report.txt").exists()


def test_forget_needs_yes_when_not_interactive_and_never_touches_the_drive(env, capsys):
    run(capsys, "catalog", str(env / "usb"), "--name", "usb")
    code, _, err = run(capsys, "forget", "usb")
    assert code == 2 and "--yes" in err
    assert run(capsys, "forget", "usb", "--yes")[0] == 0
    assert (env / "usb" / "only_here.mov").exists()
    assert "no drives cataloged" in run(capsys, "drives")[1]


def test_errors_are_messages_not_tracebacks(env, capsys):
    code, _, err = run(capsys, "reconcile", "ghost")
    assert code == 2 and "not in the catalog" in err and "Traceback" not in err
    code, _, err = run(capsys, "catalog", str(env / "missing"))
    assert code == 2 and "not a directory" in err


def test_mark_reference_and_config_file(env, capsys):
    run(capsys, "catalog", str(env / "nas"), "--name", "nas")
    run(capsys, "catalog", str(env / "usb"), "--name", "usb", "--hash")
    assert "no reference" in run(capsys, "reconcile", "usb")[2]
    assert run(capsys, "mark", "nas")[0] == 0
    assert run(capsys, "reconcile", "usb")[0] in (0, 1)
    assert run(capsys, "mark", "nas", "--no-reference")[0] == 0
    cfg = env / "driveaudit.toml"
    cfg.write_text(f'db = "{env}/other.db"\n[watch]\nroots = ["/mnt"]\ninterval = 9\n')
    monkey_db = os.environ.pop("DRIVEAUDIT_DB")
    try:
        code, out, _ = run(capsys, "--config", str(cfg), "config")
    finally:
        os.environ["DRIVEAUDIT_DB"] = monkey_db
    assert code == 0 and "/mnt" in out and "other.db" in out


def test_watch_once_with_nothing_mounted(env, capsys, tmp_path):
    empty = tmp_path / "emptyroot"
    empty.mkdir()
    code, out, _ = run(capsys, "watch", "--root", str(empty), "--once", "--settle", "0")
    assert code == 0 and "watching" in out
