"""Small shared helpers: byte formatting, path codec, output-path safety."""

from __future__ import annotations

import base64
import contextlib
import os
import unicodedata
from pathlib import Path

_B64_PREFIX = "\x01b64:"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def path_to_db(rel: str) -> str:
    """Make a relative path storable as SQLite TEXT.

    Names that are not valid UTF-8 come out of ``os.scandir`` as strings holding lone
    surrogates, which SQLite cannot store. Those are kept reversibly as base64 of the raw
    bytes behind a marker prefix; every other path is stored unchanged.
    """
    try:
        rel.encode("utf-8")
        if not rel.startswith(_B64_PREFIX):  # keep the codec injective
            return rel
    except UnicodeEncodeError:
        pass
    return _B64_PREFIX + base64.b64encode(os.fsencode(rel)).decode("ascii")


def db_to_path(stored: str) -> str:
    if stored.startswith(_B64_PREFIX):
        return os.fsdecode(base64.b64decode(stored[len(_B64_PREFIX) :]))
    return stored


def printable(text: str) -> str:
    """Escape control characters (ESC, CR, LF, ...) so untrusted names cannot drive the terminal."""
    return "".join(c if c.isprintable() else f"\\x{ord(c):02x}" for c in text)


def display_path(stored: str) -> str:
    """Printable form of a stored path: undecodable bytes and control characters shown
    escaped, never raising and never able to forge output lines."""
    return printable(db_to_path(stored).encode("utf-8", "backslashreplace").decode("utf-8"))


def norm_name(name: str) -> str:
    """Name used for name+size matching: NFC, so macOS (NFD) and Linux (NFC) names agree."""
    return unicodedata.normalize("NFC", name)


def basename_of(stored: str) -> str:
    return norm_name(stored.rsplit("/", 1)[-1])


def is_safe_rel(rel: str) -> bool:
    """True if ``rel`` is a plain relative path with no escape (no '..', not absolute)."""
    if not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
        return False
    parts = rel.replace("\\", "/").split("/")
    return all(p not in ("", ".", "..") for p in parts)


def is_within(path: str | Path, root: str | Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
        return True
    except ValueError:
        return False


def check_output_path(path: str | Path, *, force: bool = False, protected_roots=()) -> Path:
    """Validate a user-chosen output path.

    Refuses: a symlink (even a dangling one, and even with ``force``), anything whose real
    location is inside a cataloged drive, and an existing file unless ``force``.
    """
    p = Path(path).expanduser()
    if p.is_symlink():
        raise ValueError(f"refusing to write through a symlink: {p}")
    real = Path(os.path.realpath(p))
    for root in protected_roots:
        if is_within(real, root):
            raise ValueError(f"refusing to write inside a cataloged drive: {p} (under {root})")
    if p.exists() and not force:
        raise ValueError(f"{p} already exists (use --force to overwrite)")
    return p


def write_private(
    path: str | Path, data: bytes | str, *, force: bool = False, protected_roots=()
) -> Path:
    """Write ``data`` to a fresh private (0600) file.

    The data goes to a new temporary file next to the target, which is then renamed over
    it, so an existing inode (a hard link into a drive, a 0644 file) is never opened,
    truncated or kept. Without ``force`` the target must not exist (``O_EXCL``).
    """
    p = check_output_path(path, force=force, protected_roots=protected_roots)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not force:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        return p
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return p
