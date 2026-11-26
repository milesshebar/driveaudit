"""SHA-256 hashing: a cheap partial hash (first + last 8 KiB) and a full streaming hash.

A partial hash is evidence, not proof: two files that differ only in the middle share it.
Files of 16 KiB or less are read whole, so for them partial == full.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass

from .config import FULL_CHUNK, PARTIAL_CHUNK, SMALL_FILE


class FileChanged(Exception):
    """The file changed (size or mtime) between cataloging and hashing, or while hashing."""


@dataclass(frozen=True)
class Stamp:
    size: int
    mtime_ns: int


def is_small(size: int) -> bool:
    return size <= SMALL_FILE


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class Root:
    """A cataloged root to read beneath: where it is, which inode it must be, and whether
    the walk was allowed to leave its filesystem."""

    path: str
    ino: int | None = None  # None: not recorded (catalog older than this check)
    same_device: bool = True


def as_root(root: str | Root) -> Root:
    return root if isinstance(root, Root) else Root(root)


def open_beneath(root: str | Root, rel: str, flags: int, *, same_device: bool | None = None) -> int:
    """Open ``rel`` under ``root`` walking one directory descriptor at a time.

    Every component below the root is opened with ``O_NOFOLLOW`` relative to its parent's
    descriptor, so a symlink anywhere below it raises ``OSError`` instead of being followed.
    The root itself may be named through a symlink by the caller, but if an inode was
    recorded for it, a different directory is refused. With ``same_device`` (the default)
    no component may sit on another filesystem than the root. Residual window: the checks
    happen at open time on descriptors, so what is read is what was checked.
    """
    r = as_root(root)
    must_match = r.same_device if same_device is None else same_device
    parts = rel.split("/")
    if not rel or any(p in ("", ".", "..") for p in parts):
        raise OSError(f"unsafe relative path: {rel!r}")
    dflag = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dirfd = os.open(r.path, dflag)
    try:
        rst = os.fstat(dirfd)
        if r.ino is not None and rst.st_ino != r.ino:
            raise OSError(f"{r.path} is not the directory that was cataloged (inode differs)")
        for part in parts[:-1]:
            nxt = os.open(part, dflag | _NOFOLLOW, dir_fd=dirfd)
            os.close(dirfd)
            dirfd = nxt
            if must_match and os.fstat(dirfd).st_dev != rst.st_dev:
                raise OSError(f"{part}: on another filesystem than the cataloged root")
        fd = os.open(parts[-1], flags | _NOFOLLOW, dir_fd=dirfd)
        if must_match and os.fstat(fd).st_dev != rst.st_dev:
            os.close(fd)
            raise OSError(f"{rel}: on another filesystem than the cataloged root")
        return fd
    finally:
        os.close(dirfd)


def stat_beneath(root: str | Root, rel: str) -> os.stat_result:
    """``fstat`` of ``rel`` under ``root``, refusing symlinks at every component."""
    fd = open_beneath(root, rel, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        return os.fstat(fd)
    finally:
        os.close(fd)


def _open_regular(path: str, root: str | Root | None = None) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    if root is not None:
        fd = open_beneath(root, os.path.relpath(path, as_root(root).path), flags)
    else:
        fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise FileChanged(f"not a regular file: {path}")
        return fd, st
    except BaseException:
        os.close(fd)
        raise


def _check(st: os.stat_result, expect: Stamp | None, path: str) -> None:
    if expect and (st.st_size != expect.size or st.st_mtime_ns != expect.mtime_ns):
        raise FileChanged(f"changed since cataloged: {path}")


def partial_hash(path: str, expect: Stamp | None = None, root: str | Root | None = None) -> str:
    """Hash of the head and tail of ``path``; the whole file if it is small.

    Raises FileChanged if ``expect`` is given and the file no longer matches it. With
    ``root``, no component of the path below it may be a symlink.
    """
    fd, st = _open_regular(path, root)
    try:
        _check(st, expect, path)
        h = hashlib.sha256()
        h.update(os.pread(fd, PARTIAL_CHUNK, 0))
        if st.st_size > PARTIAL_CHUNK:
            h.update(os.pread(fd, PARTIAL_CHUNK, max(PARTIAL_CHUNK, st.st_size - PARTIAL_CHUNK)))
        _check(os.fstat(fd), Stamp(st.st_size, st.st_mtime_ns), path)
        return h.hexdigest()
    finally:
        os.close(fd)


def full_hash(path: str, expect: Stamp | None = None, root: str | Root | None = None) -> str:
    """SHA-256 of the whole file, streamed. Raises FileChanged on a size/mtime change."""
    fd, st = _open_regular(path, root)
    try:
        _check(st, expect, path)
        h = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(fd, FULL_CHUNK, offset)
            if not chunk:
                break
            h.update(chunk)
            offset += len(chunk)
        _check(os.fstat(fd), Stamp(st.st_size, st.st_mtime_ns), path)
        if offset != st.st_size:
            raise FileChanged(f"short read ({offset} of {st.st_size} bytes): {path}")
        return h.hexdigest()
    finally:
        os.close(fd)
