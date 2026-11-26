# Architecture

A small, dependency-free pipeline over one SQLite file.

```
 mounted drive ──► walker ──► catalog (SQLite) ──► matching ──► dedupe / reconcile / plan
        │                         ▲                                      │
        └──► hasher ──────────────┘                                      ▼
                                                           verify (catalog vs disk), verify-copy
 volumes ──► watch (poll + identify + catalog)
```

## Modules (`src/driveaudit/`)

| module | job |
|---|---|
| `walker.py` | Read-only depth-first walk. No symlink following, no crossing volumes, error list, `DriveGone` when the root disappears or changes device. |
| `hasher.py` | Partial (head+tail 8 KiB) and full SHA-256 via `pread` on an `O_NOFOLLOW` fd; refuses a file whose size/mtime differ from what was cataloged (`FileChanged`). |
| `db.py` | Schema (`drives`, `files`, `walk_errors`), `user_version` check, small shared queries. |
| `catalog.py` | Orchestrates a walk into the DB (generation sweep, truncation guard, name resolution) and the staged hashing. |
| `volumes.py` | Find mount points under roots; best-effort volume UUID via `diskutil`/`findmnt` (argument lists, timeouts, no shell). |
| `watch.py` | Polling loop; baseline/settle/replug logic. All system access is injected, so tests need no mounts. |
| `matching.py` | The one definition of "same file": `Evidence` levels, `compare()`, and `ReferenceIndex` (an O(1) lookup that is tested to agree with `compare()` exhaustively). |
| `dedupe.py` / `reconcile.py` / `plan.py` / `verify.py` | The reports. They only read the catalog (and, for `--live`, `verify`, `verify-copy`, stat/read the drives). |
| `cli.py` | argparse front end, output-path guards, exit codes. |
| `config.py` / `util.py` | Settings resolution; path codec, NFC names, safe-path helpers. |

## Data model

`drives(drive_name PK, mount_path, volume_uuid, is_reference, walk_gen, walk_complete, totals, skipped_* , error_count)`
`files(drive_name, rel_path UNIQUE, file_size, mtime_ns, link_key, partial_hash, full_hash, seen_gen)`

- **Generations.** Each walk bumps `walk_gen`; every file seen is upserted with `seen_gen = gen`. Only after
  a *complete* walk are rows with an older `seen_gen` deleted, except under folders the walk could not read.
  Interrupt it, unplug it, or get a truncated listing and nothing is deleted.
- **Hash validity.** The upsert keeps `partial_hash`/`full_hash` only when size and mtime are unchanged;
  hashing stores a hash only when the file still matches its catalog row. A hash therefore always describes
  the bytes that had the recorded size and mtime.
- **Paths** are stored relative to the mount, `/`-separated. Names that are not valid UTF-8 are stored as
  base64 behind a `\x01b64:` marker so SQLite can hold them and they round-trip.

## Staged hashing

1. Walk (no content reads).
2. *Partial* stage: files whose size equals another file's (any drive) get a head+tail hash. Files ≤ 16 KiB
   are read whole, so their partial hash is also their full hash.
3. *Full* stage: files whose partial hash equals another file's get a whole-file hash.

Across several drives the partial stage must finish on every drive before any full stage (`cmd_hash`,
`hash_with_references`), because a full-hash candidate is defined by another drive's partial hash.

## Evidence and coverage

`matching.Evidence` is `NONE < NAME_SIZE < PROBABLE < VERIFIED`. `reconcile` takes the best evidence of any
reference file for each file on the drive and compares it with `--min-evidence` (default `verified`).
`plan` copies everything below that bar, once per distinct (size, full hash) within a plan.

## Design choices worth knowing

- **Why no delete, ever:** the cost of a false "safe" is data loss; the cost of a missing command is typing
  `rm`. The tool's job ends at a trustworthy list and a verified copy.
- **Why polling in `watch`:** one code path for macOS and Linux, trivially testable, and a 5-second latency
  is irrelevant for plugging in a drive.
- **Why SQLite and no dependencies:** the catalog is the product; it can be queried with any SQLite tool,
  and an install is `pip install .`.
- **Why not hard-link/dedupe in place:** out of scope; that is a write path.

## Testing

`tests/` builds synthetic drives from temp dirs; `conftest.py` has the helpers. Mounts, volume UUIDs and the
clock are injected or monkeypatched, so no test touches a real volume. `make check` = ruff + pytest.
