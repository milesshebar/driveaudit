"""Read-only tree walker.

Guarantees: never follows a symlink below the root (every directory is opened relative to
its parent's descriptor with ``O_NOFOLLOW`` and listed with ``scandir(fd)``), never leaves
the starting filesystem (unless asked), never opens a file, reports every directory it could
not read, and raises ``DriveGone`` the moment the root stops being the same directory (same
device and inode) it started on. Residual window: a directory swapped between a listing
entry being classified and that directory being opened is refused at open time; a file
replaced between being listed and being stat'ed is simply recorded as it was stat'ed.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import (
    DEFAULT_SKIP_DIRS,
    DEFAULT_SKIP_EXTENSIONS,
    DEFAULT_SKIP_FILES,
    TIME_MACHINE_DIRS,
)


class DriveGone(Exception):
    """The root of the walk is no longer there (or is no longer the same volume)."""


@dataclass(frozen=True)
class Entry:
    rel: str  # path relative to the root, '/'-separated
    size: int
    mtime_ns: int
    link_key: str | None  # "dev:ino" for files with more than one hard link


@dataclass
class WalkStats:
    files: int = 0
    bytes: int = 0
    dirs: int = 0
    symlinks: int = 0
    special: int = 0
    other_mounts: int = 0
    skipped_dirs: int = 0
    skipped_files: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)  # (rel dir or file, message)


@dataclass(frozen=True)
class WalkOptions:
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    skip_files: frozenset[str] = DEFAULT_SKIP_FILES
    skip_extensions: frozenset[str] = DEFAULT_SKIP_EXTENSIONS
    include_time_machine: bool = False
    cross_devices: bool = False


def _device_of(entry: os.DirEntry) -> int:
    return entry.stat(follow_symlinks=False).st_dev


class Walker:
    """Iterate a tree depth-first in sorted order; ``stats`` fills in as it goes."""

    CHECK_EVERY = 256  # directories between "is the root still there?" checks

    def __init__(self, root: str | Path, options: WalkOptions | None = None):
        self.root = os.path.abspath(str(root))
        self.opts = options or WalkOptions()
        self.stats = WalkStats()
        self._root_dev: int | None = None
        self.root_ino: int | None = None
        self._dirs_seen = 0

    # -- root liveness ------------------------------------------------------------------
    def _check_root(self) -> None:
        try:
            st = os.stat(self.root)
        except OSError as e:
            raise DriveGone(f"{self.root} is no longer accessible ({e})") from e
        if not stat.S_ISDIR(st.st_mode):
            raise DriveGone(f"{self.root} is no longer a directory")
        if self._root_dev is not None and (st.st_dev, st.st_ino) != (self._root_dev, self.root_ino):
            raise DriveGone(f"{self.root} is now a different volume or directory")

    def _skip_dir(self, name: str) -> bool:
        if name in self.opts.skip_dirs:
            return True
        return not self.opts.include_time_machine and name in TIME_MACHINE_DIRS

    def _skip_file(self, name: str) -> bool:
        if name in self.opts.skip_files:
            return True
        return os.path.splitext(name)[1].lower() in self.opts.skip_extensions

    def _open_dir(self, root_fd: int, rel_dir: str) -> int:
        """Open a directory below the root without following any symlink on the way."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if not rel_dir:
            return os.dup(root_fd)
        fd = root_fd
        try:
            for part in rel_dir.split("/"):
                nxt = os.open(part, flags, dir_fd=fd)
                if fd != root_fd:
                    os.close(fd)
                fd = nxt
            if not self.opts.cross_devices and os.fstat(fd).st_dev != self._root_dev:
                raise OSError(errno.EXDEV, "directory is on another filesystem")
            return fd
        except BaseException:
            if fd != root_fd:
                os.close(fd)
            raise

    def __iter__(self) -> Iterator[Entry]:
        self._check_root()
        root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            rst = os.fstat(root_fd)
            self._root_dev, self.root_ino = rst.st_dev, rst.st_ino
            yield from self._walk(root_fd)
        finally:
            os.close(root_fd)

    def _walk(self, root_fd: int) -> Iterator[Entry]:
        stack: list[str] = [""]  # relative directory paths still to list
        while stack:
            rel_dir = stack.pop()
            self._dirs_seen += 1
            if self._dirs_seen % self.CHECK_EVERY == 0:
                self._check_root()
            try:
                dfd = self._open_dir(root_fd, rel_dir)
            except OSError as e:
                self._record_error(rel_dir or ".", e)
                continue
            try:  # entries stat relative to dfd, so it stays open while they are classified
                try:
                    with os.scandir(dfd) as it:
                        entries = sorted(it, key=lambda e: e.name)
                except OSError as e:
                    self._record_error(rel_dir or ".", e)
                    continue
                self.stats.dirs += 1
                subdirs: list[str] = []
                for entry in entries:
                    rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                    try:
                        if entry.is_symlink():
                            self.stats.symlinks += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if self._skip_dir(entry.name):
                                self.stats.skipped_dirs += 1
                                continue
                            if not self.opts.cross_devices and _device_of(entry) != self._root_dev:
                                self.stats.other_mounts += 1
                                continue
                            subdirs.append(rel)
                        elif entry.is_file(follow_symlinks=False):
                            if self._skip_file(entry.name):
                                self.stats.skipped_files += 1
                                continue
                            st = entry.stat(follow_symlinks=False)
                            link_key = f"{st.st_dev}:{st.st_ino}" if st.st_nlink > 1 else None
                            self.stats.files += 1
                            self.stats.bytes += st.st_size
                            yield Entry(rel, st.st_size, st.st_mtime_ns, link_key)
                        else:
                            self.stats.special += 1
                    except OSError as e:
                        self._record_error(rel, e)
                # reversed so the stack pops in sorted order
                stack.extend(reversed(subdirs))
            finally:
                os.close(dfd)
        self._check_root()  # still the same directory at the end of the walk

    def _record_error(self, rel: str, e: OSError) -> None:
        self.stats.errors.append((rel, f"{type(e).__name__}: {e.strerror or e}"))
        # A vanished volume looks like a pile of I/O errors; tell the two apart.
        self._check_root()
