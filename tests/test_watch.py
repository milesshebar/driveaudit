import os

from driveaudit import db as DB
from driveaudit.volumes import Volume, list_mounts, safe_name
from driveaudit.watch import Watcher

from .conftest import blob, make_tree


class FakeSystem:
    """Stands in for /Volumes: a mutable set of mount paths."""

    def __init__(self):
        self.mounts: list[str] = []
        self.slept: list[float] = []

    def list(self, roots):
        return sorted(self.mounts)

    def sleep(self, s):
        self.slept.append(s)


def make_watcher(tmp_path, system, **kw):
    logs: list[str] = []
    w = Watcher(
        db_path=tmp_path / "state" / "catalog.db",
        roots=("/Volumes",),
        mounts_fn=system.list,
        identify_fn=lambda m: Volume(m, os.path.basename(m), "UUID-" + os.path.basename(m)),
        sleep_fn=system.sleep,
        log=logs.append,
        settle=kw.pop("settle", 0),
        **kw,
    )
    return w, logs


def names(tmp_path):
    with DB.connect(tmp_path / "state" / "catalog.db") as c:
        return {d["drive_name"]: d for d in DB.get_drives(c)}


def test_new_volume_is_cataloged_and_existing_ones_are_ignored(tmp_path):
    existing = make_tree(tmp_path / "vols" / "Macintosh", {"sys": "x"})
    sysm = FakeSystem()
    sysm.mounts = [str(existing)]
    w, logs = make_watcher(tmp_path, sysm)
    assert w.scan_once() == []  # baseline
    new = make_tree(tmp_path / "vols" / "FieldDrive", {"a.mov": blob("a"), "b/c.txt": "c"})
    sysm.mounts.append(str(new))
    events = w.scan_once()
    assert [e.kind for e in events] == ["cataloged"]
    assert events[0].result.files == 2 and events[0].result.complete
    assert set(names(tmp_path)) == {"FieldDrive"}
    assert w.scan_once() == []  # not cataloged twice
    assert any("already mounted" in line for line in logs)


def test_unplug_and_replug_catalogs_again_picking_up_changes(tmp_path):
    drive = make_tree(tmp_path / "vols" / "Cards", {"one.txt": "1"})
    sysm = FakeSystem()
    w, _ = make_watcher(tmp_path, sysm)
    w.scan_once()
    sysm.mounts = [str(drive)]
    assert w.scan_once()[0].result.files == 1
    sysm.mounts = []
    assert [e.kind for e in w.scan_once()] == ["gone"]
    make_tree(drive, {"two.txt": "2"})
    sysm.mounts = [str(drive)]
    events = w.scan_once()
    assert events[0].kind == "cataloged" and events[0].result.files == 2


def test_include_existing_catalogs_what_is_already_mounted(tmp_path):
    d = make_tree(tmp_path / "vols" / "Already", {"x": "x"})
    sysm = FakeSystem()
    sysm.mounts = [str(d)]
    w, _ = make_watcher(tmp_path, sysm, include_existing=True)
    assert [e.kind for e in w.scan_once()] == ["cataloged"]


def test_ignored_labels_are_skipped(tmp_path):
    d = make_tree(tmp_path / "vols" / "TimeMachine", {"x": "x"})
    sysm = FakeSystem()
    w, _ = make_watcher(tmp_path, sysm, ignore=("TimeMachine",))
    w.scan_once()
    sysm.mounts = [str(d)]
    assert w.scan_once() == [] and names(tmp_path) == {}


def test_volume_unplugged_while_settling_is_skipped(tmp_path):
    d = make_tree(tmp_path / "vols" / "Flaky", {"x": "x"})
    sysm = FakeSystem()
    w, _ = make_watcher(tmp_path, sysm, settle=2)
    w.scan_once()
    sysm.mounts = [str(d)]
    sysm.sleep = lambda s: sysm.mounts.clear()  # pulled during the settle delay
    w.sleep_fn = sysm.sleep
    events = w.scan_once()
    assert [e.kind for e in events] == ["skipped"] and names(tmp_path) == {}


def test_one_bad_volume_does_not_stop_the_watcher(tmp_path):
    good = make_tree(tmp_path / "vols" / "Good", {"x": "x"})
    sysm = FakeSystem()
    w, _ = make_watcher(tmp_path, sysm)
    w.scan_once()
    sysm.mounts = [str(tmp_path / "vols" / "Missing"), str(good)]  # first one does not exist
    kinds = [e.kind for e in w.scan_once()]
    assert kinds == ["failed", "cataloged"] or sorted(kinds) == ["cataloged", "failed"]
    assert "Good" in names(tmp_path)


def test_reconcile_summary_against_reference_is_logged(tmp_path):
    nas = make_tree(tmp_path / "nas_src", {"a.mov": blob("a")})
    with DB.connect(tmp_path / "state" / "catalog.db") as c:
        from driveaudit.catalog import catalog_drive

        catalog_drive(
            c,
            str(nas),
            name="nas",
            reference=True,
            volume=Volume("nas", "nas", "U1"),
            log=lambda *_: None,
        )
    usb = make_tree(tmp_path / "vols" / "Usb", {"copy.mov": blob("a"), "fresh.mov": blob("f")})
    sysm = FakeSystem()
    w, logs = make_watcher(tmp_path, sysm, reconcile_after=True, hash_candidates=True)
    w.scan_once()
    sysm.mounts = [str(usb)]
    w.scan_once()
    assert any("vs reference nas" in line and "1 only here" in line for line in logs)


def test_hashing_during_watch(tmp_path):
    a = make_tree(tmp_path / "vols" / "A", {"f.bin": blob("same")})
    b = make_tree(tmp_path / "vols" / "B", {"g.bin": blob("same")})
    sysm = FakeSystem()
    w, _ = make_watcher(tmp_path, sysm, hash_candidates=True)
    w.scan_once()
    sysm.mounts = [str(a)]
    w.scan_once()
    sysm.mounts = [str(a), str(b)]
    w.scan_once()
    with DB.connect(tmp_path / "state" / "catalog.db") as c:
        # B's file collided with A's by size, so it was partial-hashed (and full-hashed vs A's partial later)
        row = c.execute("SELECT partial_hash FROM files WHERE drive_name='B'").fetchone()
        assert row["partial_hash"]


def test_run_loop_polls_and_stops(tmp_path):
    sysm = FakeSystem()
    w, _ = make_watcher(tmp_path, sysm)
    w.run(7, max_scans=3)
    assert sysm.slept == [7, 7]


def test_list_mounts_only_returns_real_mount_points(tmp_path, monkeypatch):
    root = tmp_path / "Volumes"
    for n in ("USB", "Boot", ".hidden", "plain"):
        (root / n).mkdir(parents=True)
    os.symlink("/", root / "Macintosh HD")  # macOS lists the boot volume as a symlink
    (root / "file.txt").write_text("x")
    mounted = {str(root / "USB"), str(root / ".hidden"), str(root / "Macintosh HD")}
    monkeypatch.setattr(os.path, "ismount", lambda p: p in mounted)
    assert list_mounts([str(root)]) == [str(root / "USB")]


def test_list_mounts_finds_linux_style_user_subdirectories(tmp_path, monkeypatch):
    root = tmp_path / "media"
    (root / "alice" / "STICK").mkdir(parents=True)
    monkeypatch.setattr(os.path, "ismount", lambda p: p.endswith("STICK"))
    assert list_mounts([str(root)]) == [str(root / "alice" / "STICK")]
    assert list_mounts([str(root)], depth=1) == []
    assert list_mounts([str(tmp_path / "does-not-exist")]) == []


def test_safe_name():
    assert safe_name("My Passport/Backup (2)") == "My_Passport_Backup_2"
    assert safe_name("日本語 drive") == "日本語_drive"
    assert safe_name("...") == "drive"
