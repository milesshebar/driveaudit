import pytest

from driveaudit.config import load_settings
from driveaudit.util import check_output_path, display_path, is_safe_rel

from .conftest import make_tree


def test_defaults_have_no_machine_specific_paths(monkeypatch, tmp_path):
    for k in ("DRIVEAUDIT_DB", "DRIVEAUDIT_CONFIG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.chdir(tmp_path)
    s = load_settings()
    assert s.db == tmp_path / "data" / "driveaudit" / "catalog.db" and s.source is None
    assert s.watch_roots == ("/Volumes",)


def test_precedence_flag_over_env_over_file(monkeypatch, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        'db = "/from/file.db"\nreference = ["nas"]\n[scan]\nextra_skip_dirs = ["node_modules"]\nextra_skip_extensions = [".BAK"]\n'
    )
    monkeypatch.delenv("DRIVEAUDIT_DB", raising=False)
    s = load_settings(str(cfg))
    assert str(s.db) == "/from/file.db" and s.reference == ["nas"]
    assert "node_modules" in s.skip_dirs and ".bak" in s.skip_extensions
    monkeypatch.setenv("DRIVEAUDIT_DB", "/from/env.db")
    assert str(load_settings(str(cfg)).db) == "/from/env.db"
    assert str(load_settings(str(cfg), db="/from/flag.db").db) == "/from/flag.db"


def test_bad_config_is_rejected(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text("reference = 'nas'\n")
    with pytest.raises(ValueError):
        load_settings(str(cfg))
    with pytest.raises(FileNotFoundError):
        load_settings(str(tmp_path / "missing.toml"))


def test_safe_rel():
    assert is_safe_rel("a/b/c.txt") and is_safe_rel("..hidden/ok")
    for bad in ("", "/abs", "../x", "a/../../x", "a//b", "a/./b", "a\\..\\b", "x\x00y"):
        assert not is_safe_rel(bad), bad


def test_check_output_path(tmp_path):
    drive = make_tree(tmp_path / "drive", {"a": "a"})
    with pytest.raises(ValueError, match="inside a cataloged drive"):
        check_output_path(drive / "sub" / "out.txt", protected_roots=[str(drive)])
    existing = tmp_path / "x.txt"
    existing.write_text("1")
    with pytest.raises(ValueError, match="already exists"):
        check_output_path(existing)
    assert check_output_path(existing, force=True) == existing
    assert check_output_path(tmp_path / "new.txt") == tmp_path / "new.txt"


def test_display_path_never_raises():
    assert "\\udcff" in display_path(
        __import__("driveaudit.util", fromlist=["x"]).path_to_db("a-\udcff")
    )
