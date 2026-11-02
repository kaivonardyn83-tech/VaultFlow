# VaultFlow — Tasks: Week 1 Implementation

**Spec:** `file_workspace`
**Goal for Week 1:** a working CLI that scans a folder, applies manual ranks, previews a plan, renames safely, journals every change, and undoes the last batch.

Work top to bottom; each task is small enough to finish and test in one sitting. Do not start a day's tasks until the previous day's tests pass.

---

## Day 1 — Project Skeleton and Shared Models

- [ ] Create repo layout: `vaultflow/`, `tests/`, `pyproject.toml`, `README.md`
- [ ] Pin Python `>=3.11`; add `pytest` as the only dev dependency
- [ ] Add `.gitignore` (including `.vaultflow/` for local test folders)
- [ ] Write `errors.py` with the `VaultFlowError` hierarchy
- [ ] Write `models.py`: `FileEntry`, `NamingMode`, `NamingRule`, `RenameOp`, `RenamePlan`
- [ ] Add `conftest.py` with a `make_files(tmp_path, names)` fixture helper
- [ ] Confirm `pytest` runs green with a trivial smoke test

## Day 2 — `scanner.py` Core

- [ ] Implement `validate_root()` (exists, is dir, readable and writable)
- [ ] Test: missing path, file-instead-of-dir, permission failure
- [ ] Implement `scan_directory()` non-recursive with `os.scandir`
- [ ] Return `FileEntry` with path, name, stem, suffix, size, mtime, ctime
- [ ] Implement natural, case-insensitive sort for deterministic output
- [ ] Test: ordering (`file2` before `file10`), metadata values match `stat()`

## Day 3 — `scanner.py` Filtering

- [ ] Implement `is_hidden()` with dotfile check
- [ ] Add Windows attribute check (`st_file_attributes`) via `getattr`
- [ ] Add macOS `UF_HIDDEN` check via `getattr(st, "st_flags", 0)`
- [ ] Always exclude `.vaultflow/`, even with `include_hidden=True`
- [ ] Skip symlinks and emit a warning
- [ ] Add `extensions` filter (case-insensitive)
- [ ] Add `recursive=True` using an explicit stack loop
- [ ] Catch per-entry `PermissionError`: warn and continue
- [ ] Tests: dotfiles, mocked platform flags, symlink skip, extension filter, recursion, unreadable entry

## Day 4 — `renamer.py` Ranking and Naming

- [ ] Implement `apply_ranking()` validation (int, `>= 1`, no duplicates, known filenames)
- [ ] Order ranked files by rank, then unranked by natural name order
- [ ] Add `ranked_only` mode
- [ ] Compact rank gaps into positional indices
- [ ] Implement padded naming with auto width `max(2, len(str(n)))`
- [ ] Implement custom prefix naming (`prefix`, `start`, `step`, optional `width`)
- [ ] Add name validation: illegal characters, 255-byte limit, empty result
- [ ] Add `strip_existing` handling for prior `NN_` and custom prefixes
- [ ] Tests: width at 9, 10, 100 files; `V1001_` sequence; duplicate-rank error; gap compaction; unranked ordering

## Day 5 — Plan Building and Collision Detection

- [ ] Implement pure `build_plan()` returning `RenamePlan`
- [ ] Drop ops where `src == dst` so repeat runs are idempotent
- [ ] Detect internal target collisions (two files mapping to one name)
- [ ] Detect collisions with untouched files already in the directory
- [ ] Detect case-only collisions using casefolded names
- [ ] Populate `plan.conflicts` and `plan.warnings`
- [ ] Tests: each collision type, idempotent re-run, NFC/Unicode filenames

## Day 6 — `logger.py` and Safe Apply

- [ ] Implement `Journal` with `.vaultflow/` creation on first write
- [ ] Implement `start_batch()` with timestamp plus `secrets` batch ID
- [ ] Implement `record()` writing JSONL with `flush()` and `os.fsync()`
- [ ] Store paths relative to the target directory
- [ ] Implement `read_batch()`, `latest_batch()`, `history()`
- [ ] Tolerate a truncated final journal line
- [ ] Implement `apply_plan(dry_run=True)` returning a description and writing nothing
- [ ] Implement two-phase rename: `src -> tmp -> dst`
- [ ] Pre-check destination absence; never overwrite
- [ ] Journal each step as it completes
- [ ] Implement rollback on failure and re-raise `RenameError`
- [ ] Tests: swap scenario (`01_a`/`02_b` exchange), injected mid-batch failure, journal round trip, dry-run leaves disk untouched

## Day 7 — Undo, CLI, and Hardening

- [ ] Implement `undo_batch()` with pre-verification of every `dst` and free `src`
- [ ] Journal `undo` records; never rewrite existing journal lines
- [ ] Block double-undo of the same batch
- [ ] Write `cli.py` with subcommands: `scan`, `plan`, `apply`, `undo`, `history`
- [ ] Make `plan` the safe default and require `--yes` for `apply`
- [ ] Load ranks from a JSON file (`{"intro.mp4": 1}`) and report bad entries clearly
- [ ] Save and reload rankings via `.vaultflow/rankings.json`
- [ ] End-to-end test: scan, rank, plan, apply, undo on a temp folder with 12 files
- [ ] Manual smoke test on Windows, macOS, or Linux with a real media folder copy
- [ ] Verify no network imports (`socket`, `urllib`, `requests`) exist anywhere in the package
- [ ] Update `README.md` with install, usage examples, and the local-first guarantee

---

## Week 1 Definition of Done

- [ ] All acceptance criteria in `requirements.md` §5 pass as automated tests
- [ ] `pytest` is green on at least two operating systems
- [ ] A 12-file demo folder can be ranked, renamed (padded and `V1001_`), and fully undone from the CLI
- [ ] Zero third-party runtime dependencies
- [ ] No filesystem writes occur outside the selected directory's files and its `.vaultflow/` folder

## Deferred to Week 2+

- GUI front end (drag-to-rank list over the same `plan` API)
- Live preview panel and saved naming presets
- Recursive batch renames with per-folder numbering
- Hackathon demo polish: sample dataset, screenshot or GIF, submission write-up
