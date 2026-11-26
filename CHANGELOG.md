# Changelog

## 1.0.0

First release.

- Package `driveaudit` (src layout, Python 3.11+, no runtime dependencies) and `driveaudit` CLI.
- `catalog`: metadata walk into SQLite; staged SHA-256 hashing (partial where sizes collide, full where
  partial hashes collide); generation-based refresh that drops deleted files only after a complete walk.
- `watch`: poll for newly mounted volumes (macOS `/Volumes`, Linux `/media` `/mnt`), catalog each.
- `dedupe`, `reconcile` (against declared source-of-truth drives, `--live` re-check), `plan` (rsync
  lists, never copies), `verify` (catalog vs disk, `--deep` re-hash), `verify-copy`.
- Config file, environment and flags; nothing machine-specific in the code.
- Test suite on synthetic drives (duplicates, renames, size/mtime collisions, unicode and odd names,
  symlinks, permission errors, unplugged mid-walk, mocked mounts).

### Design decisions

- Coverage requires a **full-hash** match (or `--min-evidence probable`, explicitly); a head+tail match
  alone is never "safe".
- Duplicate groups are keyed on size **and** hash.
- A walk is idempotent and generation-stamped instead of resumable; rows are removed only after a complete
  walk, by the newest walk only.
- Hard links are recorded and not counted as wasted space.
- A drive label shared by two physical drives never merges them; one drive is never cataloged twice.
- Everything that could mislead is counted and shown: skipped folders/files, symlinks, other volumes,
  unreadable entries, incomplete catalogs.
- New output files are created 0600, never written through a symlink at the end of the path or onto a cataloged drive.
- Config is never read from the current directory.
- With `--force`, output is written to a fresh private temporary file and renamed into place, so an
  existing file (a hard link, a world-readable one) is replaced, never truncated or kept; without it the
  file is created directly and must not already exist.
- `verify --deep` says plainly when files had no full-hash baseline (content not verified) and exits 1;
  `hash --baseline` stores full hashes for every file.
- File reads below a cataloged root refuse a symlink at any path component; concurrent catalogs of one
  drive cannot overwrite each other's rows; `reconcile --live` is a blocker if a reference is not mounted.
- Walk bookkeeping (generation bump, completion, totals) is one write transaction conditioned on the walk
  still being the newest; the walker lists directories through descriptors and checks the root's
  device and inode; hashing and verification refuse a changed root inode and a filesystem change;
  `hash --baseline` exits 1 while baselines are missing.
- Registration (name, overlap check, upsert) is one write transaction; the same volume UUID under two paths is an overlap blocker; the schema migration locks before it inspects.
- `verify` checks the recorded root identity before walking (also for empty catalogs); `verify-copy --quick` uses the same descriptor-relative checks as the full mode.
- A walk with unreadable folders removes no rows at all.
