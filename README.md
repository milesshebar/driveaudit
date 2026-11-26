<p align="center"><img src="docs/driveaudit.png" alt="driveaudit: Catalog & Verify" width="712"></p>

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

# driveaudit

You have a shelf of old drives and a NAS, and you want to know which of those drives you can actually retire. Disk catalogers tell you what is on a drive, and duplicate finders tell you what is repeated; driveaudit answers the question in between: **which files exist only on this drive, compared with a source of truth you declare, and how strongly is each match proven?** It never changes a drive.

## What it does

- **Catalogs** each drive into one SQLite file (a metadata walk; by default SHA-256 hashing only for files that could be duplicates), and can `watch` for drives as you plug them in.
- **Reconciles** any drive against a source of truth such as your NAS, with evidence tiers: `verified` (full SHA-256), `probable` (first + last 8 KiB only) or `name_size` (a hint). Only `verified` counts as covered unless you say otherwise.
- **Finds duplicates** across all cataloged drives.
- **Plans copies** for what exists only on the old drive (writes rsync lists; you run the copy), then **verifies** the finished copy, and re-checks a drive against its catalog entry before you trust it.
- **Never deletes.** It never deletes, moves, renames or writes to a drive, and has no delete command. Python 3.11+ standard library only; macOS and Linux (not tested on Windows).

## Quick start

```sh
uv tool install git+https://github.com/milesshebar/driveaudit     # or: pipx install git+https://github.com/milesshebar/driveaudit
driveaudit catalog /Volumes/NAS --name nas --reference --hash      # the source of truth
driveaudit catalog "/Volumes/Old Drive" --name old --hash          # the drive you want to retire
driveaudit dedupe                                                  # what do I have twice?
driveaudit reconcile old                                           # what exists ONLY on the old drive? (exit 1 if anything)
driveaudit reconcile old --list only-here                          # the paths
```

With two tiny invented drives (`nas` and `old`, two files in common) that looks like this:

```text
$ driveaudit catalog /mnt/nas --name nas --reference --hash
cataloging nas (/mnt/nas)
partial-hashing 0 size-collision files on nas
full-hashing 0 partial-collision files on nas
nas: 2 files, 22.0 B (complete; 0 unreadable, 0 symlinks skipped)

$ driveaudit catalog /mnt/old --name old --hash
cataloging old (/mnt/old)
partial-hashing 2 size-collision files on old
partial-hashing 2 size-collision files on nas
full-hashing 0 partial-collision files on old
full-hashing 0 partial-collision files on nas
old: 4 files, 65.0 B (complete; 0 unreadable, 0 symlinks skipped)

$ driveaudit dedupe
DUPLICATES
======================================================================
! catalog may be out of date: run `driveaudit verify` on a drive before acting on this list
verified (full SHA-256 match): 2 groups, 22.0 B reclaimable
probable (head+tail hash only): 0 groups, 0.0 B (NOT proven identical)

VERIFIED - top 2 by reclaimable space
----------------------------------------------------------------------
12.0 B x 2 = 12.0 B reclaimable on nas, old
    nas: docs/report.txt
    old: misc/report-old.txt
10.0 B x 2 = 10.0 B reclaimable on nas, old
    nas: photos/IMG_001.jpg
    old: photos/IMG_001_copy.jpg

$ driveaudit reconcile old
RECONCILE old  against  nas
======================================================================
files: 4  (65.0 B)   required evidence: verified

  verified           2 files     22.0 B   copy on reference, full SHA-256 identical
  probable           0 files      0.0 B   copy on reference, head+tail hash identical (middle unchecked)
  name_size          0 files      0.0 B   same name and size on reference, no hash to confirm
  none               2 files     43.0 B   ONLY HERE - no match on any reference

  ! 'old' was last cataloged 0.0 hours ago; run `driveaudit verify`
  ! reference 'nas' was last cataloged 0.0 hours ago

RESULT: NOT fully covered - 2 files (43.0 B) need attention. Do not retire this drive.

Where they are (top-level folders, largest first):
      22.0 B         1 files  misc
      21.0 B         1 files  photos

List them: driveaudit reconcile old --list only-here|unconfirmed

$ driveaudit reconcile old --list only-here
misc/notes.txt
photos/IMG_002.jpg
```

To work on it: `git clone https://github.com/milesshebar/driveaudit.git && cd driveaudit && uv sync && make check` (ruff + pytest).

## Walkthrough: a NAS and one old drive

```sh
# 1. Catalog the source of truth and an old drive. --hash also hashes files that could be
#    duplicates (same size as another file); it reads those files, so the drives must be mounted.
driveaudit catalog /Volumes/NAS --name nas --reference --hash
driveaudit catalog "/Volumes/Old Drive" --name old --hash

# 2. What do I have twice?
driveaudit dedupe

# 3. What exists ONLY on the old drive?
driveaudit reconcile old                       # summary; exit code 1 if anything is not covered
driveaudit reconcile old --list only-here      # the actual paths (control characters shown escaped)
driveaudit reconcile old --list only-here -0   # raw NUL-separated paths, for xargs -0

# 4. Plan the copy for the stragglers (nothing is copied), run it yourself, then check it.
driveaudit plan old --dest /Volumes/NAS/rescued --output plan.json --export-lists lists/
#    ...run the rsync command it prints (add -n first for a dry run)...
driveaudit verify-copy plan.json               # re-reads source and copy, compares SHA-256

# 5. Before you retire anything: is the catalog still true?
driveaudit hash old --baseline                 # once: full-hash every file, so --deep can check all content
driveaudit verify old --deep                   # re-walk and re-hash the drive against the catalog
driveaudit reconcile old --live                # re-stat the reference copies it relied on
```

Deleting is up to you. driveaudit has no delete command by design.

### Cataloging drives as you plug them in

```sh
driveaudit watch --hash --ignore "Macintosh HD"
```

Polls `/Volumes` (macOS) every few seconds. Volumes already mounted when it starts are ignored
(`--include-existing` changes that); each newly mounted one is cataloged, hashed (with `--hash`) and
summarized against your reference drives. Unplug and re-plug a drive and it is re-cataloged,
incrementally. On Linux, point it at the right place: `--root /media --root /mnt` (it looks one level
down, and two for `/media/<user>/<label>`). `--once` scans once and exits. macOS may ask permission
for the terminal to read removable volumes the first time.

## Commands

| command | what it does |
|---|---|
| `catalog PATH [--name N] [--reference] [--hash]` | walk a mounted drive (or any folder) into the catalog |
| `hash [DRIVE]` | hash duplicate candidates on mounted drives (partial pass on all, then full) |
| `watch` | catalog volumes as they appear |
| `drives` / `status` | list cataloged drives, role, age, incomplete/unreadable, hashing pending |
| `mark NAME [--no-reference]` | declare (or un-declare) a drive as a source of truth |
| `dedupe [--weak] [--format json]` | duplicate groups across and within drives, by evidence level |
| `reconcile DRIVE [--against N] [--min-evidence probable] [--live] [--list KIND [-0]]` | what exists only on this drive |
| `plan DRIVE... --dest DIR [--output plan.json] [--export-lists DIR]` | copy plan + rsync lists (never copies) |
| `verify DRIVE [--deep]` | re-check a mounted drive against its catalog entry |
| `verify-copy plan.json [--quick]` | check a finished copy against its plan |
| `forget NAME` | drop a drive from the **catalog** (never touches the drive) |
| `config` | show effective settings |

Exit codes: `0` ok · `1` the check found something (`reconcile` not fully covered, `verify` /
`verify-copy` differences, incomplete `catalog`) · `2` error · `130` interrupted.

## Configuration

Flags beat `$DRIVEAUDIT_DB` / `$DRIVEAUDIT_CONFIG` beat the config file beat defaults. The config file
is `--config`, else `$DRIVEAUDIT_CONFIG`, else `~/.config/driveaudit/config.toml`. A
`driveaudit.toml` in the current directory is deliberately **not** picked up (it could be sitting on the
drive you are cataloging and silently exclude its files). The config in use is printed on stderr. See
[`examples/driveaudit.toml`](examples/driveaudit.toml). The catalog defaults to
`~/.local/share/driveaudit/catalog.db`. No paths are baked in.

## Safety model

1. **Drives are only read.** Files are opened read-only. The only things driveaudit writes are its own
   SQLite catalog and files you ask for with `--output` / `--export-lists`. It refuses to write those onto
   a cataloged drive or over an existing file (without `--force`), never through a symlink at the end of
   the path. A new output file is created directly with mode 0600; with `--force` a fresh 0600 file is
   written next to it and renamed over it, so an existing inode (a hard link, a world-readable file) is
   replaced, never reused. It refuses to keep its catalog inside a tree it is cataloging, and refuses a
   plan destination inside a source drive.
2. **Nothing deletes.** There is no delete, move or "clean up" code path anywhere. `forget` edits the
   catalog only.
3. **Symlinks below the root are not followed, and filesystem boundaries are checked** (see "Isolation and
   its limits" below for exactly what is and is not guaranteed). Other volumes mounted inside a tree are not
   crossed (`--cross-devices` to opt in). Skipped counts are stored and shown. Sockets, devices and the
   like are skipped. Hard links are listed once per path but not counted as wasted space.
4. **A half-seen drive is not trusted.** If a drive is unplugged mid-walk, or any folder is unreadable, no
   rows are removed anywhere (not just under the unreadable folder), the drive is marked INCOMPLETE and
   `reconcile` will not call it covered. A walk that finds nothing in a catalog that had files is always held
   back, and so is one that finds under half of the previously cataloged files (`--allow-shrink` to
   accept); that half-size rule applies only once the catalog already had at least 100 files, so a
   smaller catalog that shrinks (but not to empty) is accepted as a normal complete walk. The rows and sizes the
   walk did reach before stopping are still updated, so the catalog is not byte-for-byte what it was.
5. **Hashes are tied to the file they describe, by size and mtime.** A hash is stored only if the file
   still has the size and mtime it was cataloged with (and the catalog row still does), and is discarded
   when a re-walk sees either change. Pointing `hash` at the wrong volume is caught when the OS reports a
   volume UUID that differs, and by the recorded root inode and the size and mtime check; where no UUID is
   available a different volume holding files with identical size and mtime would be hashed. The limit: an
   edit that keeps both size and mtime (`cp -p`, `touch -r`, FAT's 2-second mtimes) is invisible to
   metadata. Only `verify --deep` re-reads content, and only for files that have a stored hash: it
   reports files with no full hash as content-unverified (exit 1), and files with only a head+tail hash
   as partially checked. `driveaudit hash DRIVE --baseline` full-hashes every file that has no full hash
   (this reads those files, including ones that are unique by size); it exits 1 if any remain missing.
6. **Two drives with one label are not merged, and one drive is not counted twice.** Where the OS gives
   a volume UUID, a second physical drive called "Backup" gets its own entry; if the UUID is unavailable
   on either side and the mount point differs, it also gets its own entry. A path that overlaps (same,
   nested, or same UUID as) an already-cataloged drive is refused unless `--allow-overlap`, because the
   same files would otherwise look like their own backup.
7. **Concurrent catalogs of one drive do not overwrite each other.** Each walk gets a generation number
   inside a write transaction; row batches, the sweep that removes vanished files and the final
   completion record are each written only while the walk is still the newest one, under the same
   write lock as the check. An older walk finishing late writes nothing more and is reported
   incomplete. This covers walks of the same catalog file; it says nothing about two different
   catalog files.

### Isolation and its limits

- Walking, hashing, `verify --deep`, `reconcile --live` and `verify-copy` open every directory below the
  root relative to its parent's file descriptor with `O_NOFOLLOW`, so a symlink below the root is refused
  rather than followed. Which filesystem policy each uses differs: a walk (`catalog`, and the walk inside
  ordinary `verify`) uses the options given on the current command line and config; hashing, `verify
  --deep` hashing and `reconcile --live` use the policy stored when the drive was cataloged (stay on the
  root's filesystem unless it was cataloged with `--cross-devices`); `verify-copy` always stays on one
  filesystem per folder and ignores that setting.
- The root path itself may be a symlink when you give it (that is your choice). The walker remembers the
  root's device and inode and stops with "different volume or directory" if the path stops leading to
  it. Hashing, `reconcile --live` and every `verify` (metadata, deep, and for an empty catalog too)
  compare the root's inode with the one recorded at catalog time before reading anything; a mismatch is
  an error. Catalogs made before the inode was recorded cannot be compared, so `verify` refuses a root
  that is a symlink for them and hashing skips the comparison.
- Not guaranteed: the walker lists a directory and then stats its entries, so a file replaced between
  those steps is recorded as it was at stat time. `verify-copy` (quick and full) reads source and copy
  without following a symlink below their folders and always stays on one filesystem per folder; plan
  files do not record `--cross-devices`, so a plan whose source needed it cannot be verified, and
  `verify-copy` has no recorded root inode to compare (it uses the plan's paths as given). Someone who
  can write to a drive while you catalog it can make the catalog wrong (it is a snapshot), but cannot, by
  swapping directories for symlinks, make driveaudit read files outside the root. Root inode comparison
  is weak for a volume root (volume roots commonly share one inode number); it is meant to catch a
  replaced folder or a symlink, not to prove which volume is mounted.

## How matching works, and where it stops

| level | meaning | counts as "covered"? |
|---|---|---|
| `verified` | same size **and** same full SHA-256 | yes (default) |
| `probable` | same size and same partial hash: SHA-256 of the first 8 KiB + last 8 KiB (whole file if ≤ 16 KiB) | only with `--min-evidence probable` |
| `name_size` | same size and same Unicode-normalized file name, no hashes | never |
| none | everything else; a size match alone is never a match | never |

If both files have a full hash and they differ, they are different, whatever else agrees.

**Hashing is staged to stay cheap.** Walking reads no file contents. `hash` partial-hashes only files that
share a size with another file, then full-hashes only files whose partial hash collides with another
file's. Unique-size files are not read unless you ask with `hash --baseline`, which is why the default is
fast on big drives, and why a drive you have not hashed shows `name_size` at best.

**Limits you should know about**

- *Partial hashes are not proof.* Two same-size files that differ only in the middle (a re-render, an
  edited container) share a partial hash. That is why `probable` is not enough by default and why the full
  stage exists. Run `hash` until `drives` shows nothing pending.
- *Verification needs the files readable.* A full hash needs the drive mounted. Matches against a drive
  that is not mounted rest on hashes from when it last was.
- *The catalog is a snapshot.* `reconcile` reports what the catalog believes. Drives change. Before
  retiring anything, run `verify` (and `--deep` if it matters) on the drive, and `reconcile --live` for
  the reference copies (live is a metadata check only: it re-stats each matched reference file for size and
  modification time, and fails if a reference is not mounted; it does not re-read content). For example, a drive cataloged months earlier read as
  almost entirely redundant, and was mostly unique once it was re-walked.
- *Skipped is not covered.* Folders and files matched by the skip rules (`.Trashes`, `@Recycle`, Time
  Machine trees, `.DS_Store`, `*.tmp`, anything you add in `[scan]`), symlinks, special files, other
  volumes and unreadable entries are outside the catalog. Each is counted, stored and shown as a
  warning by `reconcile`. Look at that line before trusting a "covered" result; the skip rules match
  folder names at any depth.
- *Content, not metadata.* Matching ignores mtime and permissions; macOS extended attributes, resource
  forks and sidecar metadata are not compared.
- *Scale.* Reconcile and dedupe hold the relevant file records in memory (fine for a few million files).
- *A truncated listing can still slip through.* A walk that finds less than half of what was cataloged
  is held back, but only for catalogs of at least 100 files (a walk that finds nothing in a non-empty
  catalog is always held back), and an unmounted share that lists 60% of its files looks like a
  shrunken drive. `verify` and `--live` exist for exactly this.
- *Plans are checked, not trusted.* `plan` exits 1 if its inputs had blockers (incomplete drive, overlap)
  and `verify-copy` then cannot pass; it also checks the source's modification times and volume UUID
  against the plan, and that every copy is a regular file really under the destination folder.

## Privacy

The catalog, plan files and rsync lists contain **every file path on your drives**. They are created
new files created readable by you only (0600). A catalog folder that driveaudit has to create is 0700. Folders
that already exist (a catalog folder you made, a `--export-lists` folder, a plan's parent) keep whatever
permissions they have, and so does an existing catalog file, so check them. Do not post or share them. `forget` removes a drive from
the catalog and vacuums the file; the SQLite `-wal`/`-shm` files next to it are temporary.

The CLI is the whole interface; `--format json` is there for anything you want to build on top.

## More

[ARCHITECTURE.md](ARCHITECTURE.md) · [CHANGELOG.md](CHANGELOG.md)

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Miles Shebar.
