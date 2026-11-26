import hashlib
import os

import pytest

from driveaudit.config import PARTIAL_CHUNK, SMALL_FILE
from driveaudit.hasher import FileChanged, Stamp, full_hash, is_small, partial_hash

from .conftest import blob


def stamp(p):
    st = os.stat(p)
    return Stamp(st.st_size, st.st_mtime_ns)


def test_full_hash_matches_hashlib(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(blob("x", 3_000_000))
    assert full_hash(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_small_files_partial_equals_full(tmp_path):
    for size in (0, 1, PARTIAL_CHUNK, PARTIAL_CHUNK + 1, SMALL_FILE - 1, SMALL_FILE):
        p = tmp_path / f"s{size}"
        p.write_bytes(blob("s", size))
        assert is_small(size)
        assert partial_hash(str(p)) == full_hash(str(p)), size


def test_just_over_small_threshold_is_partial_only(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(blob("big", SMALL_FILE + 1))
    assert not is_small(SMALL_FILE + 1)
    assert partial_hash(str(p)) != full_hash(str(p))


def test_partial_hash_ignores_the_middle_and_full_hash_does_not(tmp_path):
    a, b = bytearray(blob("m", 100_000)), bytearray(blob("m", 100_000))
    b[50_000] ^= 0xFF  # differs only in the middle
    pa, pb = tmp_path / "a", tmp_path / "b"
    pa.write_bytes(bytes(a))
    pb.write_bytes(bytes(b))
    assert partial_hash(str(pa)) == partial_hash(str(pb))  # the documented limit
    assert full_hash(str(pa)) != full_hash(str(pb))


def test_tail_changes_are_caught_by_partial(tmp_path):
    a, b = bytearray(blob("t", 100_000)), bytearray(blob("t", 100_000))
    b[-1] ^= 0xFF
    (tmp_path / "a").write_bytes(bytes(a))
    (tmp_path / "b").write_bytes(bytes(b))
    assert partial_hash(str(tmp_path / "a")) != partial_hash(str(tmp_path / "b"))


def test_changed_file_is_refused(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(blob("c", 50_000))
    s = stamp(p)
    os.utime(p, (1, 1))
    with pytest.raises(FileChanged):
        partial_hash(str(p), s)
    with pytest.raises(FileChanged):
        full_hash(str(p), s)
    p.write_bytes(b"short")
    with pytest.raises(FileChanged):
        full_hash(str(p), Stamp(50_000, os.stat(p).st_mtime_ns))


def test_symlink_and_directory_are_not_hashed(tmp_path):
    (tmp_path / "t").write_bytes(b"data")
    os.symlink(tmp_path / "t", tmp_path / "ln")
    with pytest.raises(OSError):
        full_hash(str(tmp_path / "ln"))  # O_NOFOLLOW
    (tmp_path / "dir").mkdir()
    with pytest.raises((FileChanged, OSError)):
        full_hash(str(tmp_path / "dir"))
