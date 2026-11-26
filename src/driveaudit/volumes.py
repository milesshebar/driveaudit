"""Find mounted volumes and identify them.

Identity matters: two different drives can share a label ("Untitled", "Backup"). Merging them
under one name would make a later walk look like it deleted the first drive's files, so a
volume UUID (when the OS will tell us) is recorded and a clash gets a distinct name.
The UUID lookup shells out to ``diskutil`` (macOS) or ``findmnt`` (Linux) with an argument
list and a timeout; if neither works the volume is identified by name only.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Volume:
    mount_path: str
    label: str
    uuid: str | None = None


def safe_name(label: str) -> str:
    """A catalog-safe drive name derived from a volume label or folder name."""
    name = re.sub(r"[^\w.\-]+", "_", label, flags=re.UNICODE).strip("_.")
    return name or "drive"


def list_mounts(roots: tuple[str, ...] | list[str], *, depth: int = 2) -> list[str]:
    """Mount points directly under ``roots``.

    macOS volumes sit one level down (``/Volumes/X``); on Linux desktop-mounted drives sit two
    (``/media/<user>/X``), so ``depth`` defaults to 2. Symlinks (macOS lists the boot volume
    as a symlink to ``/``) and hidden entries are skipped, and only real mount points count.
    """
    found: list[str] = []

    def scan(directory: str, level: int) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            if name.startswith("."):
                continue
            path = os.path.join(directory, name)
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            if os.path.ismount(path):
                found.append(path)
            elif level < depth:
                scan(path, level + 1)

    for root in roots:
        scan(root, 1)
    return found


def _tool(name: str, *fixed: str) -> str:
    """Prefer the OS's own binary over whatever is first on $PATH."""
    for d in fixed:
        if os.access(os.path.join(d, name), os.X_OK):
            return os.path.join(d, name)
    return name


def _run(cmd: list[str]) -> bytes | None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def volume_uuid(mount_path: str) -> str | None:
    """Best-effort filesystem UUID for a mount point; None when unavailable."""
    if sys.platform == "darwin":
        out = _run([_tool("diskutil", "/usr/sbin"), "info", "-plist", mount_path])
        if out:
            try:
                info = plistlib.loads(out)
            except Exception:
                return None
            return info.get("VolumeUUID") or info.get("DiskUUID")
        return None
    out = _run([_tool("findmnt", "/usr/bin", "/bin"), "-no", "UUID", "--target", mount_path])
    text = out.decode("utf-8", "replace").strip() if out else ""
    return text or None


def identify(mount_path: str) -> Volume:
    p = Path(mount_path)
    return Volume(mount_path=str(p), label=p.name or "drive", uuid=volume_uuid(str(p)))
