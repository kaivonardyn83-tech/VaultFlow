# VaultFlow — Requirements: File Workspace

**Spec:** `file_workspace`
**Product:** VaultFlow, a local-first batch file renamer (Kiroween 2026)
**Principles:** offline-only, no telemetry, no cloud calls, non-destructive by default, every rename reversible.

---

## 1. Scope

VaultFlow lets a user pick a folder, manually rank the files in it, and apply a deterministic, zero-padded (or custom-prefixed) naming scheme in one atomic batch. All processing, state, and logs stay on the local disk.

**Out of scope (Week 1):** GUI polish, recursive renames across nested folders, cloud sync, content-based auto-ranking, file content edits.

---

## 2. User Stories

| ID | As a... | I want to... | So that... |
|----|---------|--------------|------------|
| US-1 | creator with a messy folder | select a directory from disk | I can work on files without importing or uploading them |
| US-2 | user | see only the files I care about (hidden/system files excluded) | I never rename `.DS_Store`, `Thumbs.db`, or dotfiles by accident |
| US-3 | user | see name, size, and modified date for each file | I can decide the order with context |
| US-4 | user | assign each file a rank (1, 2, 3, ...) manually | the final order reflects my intent, not alphabetical accident |
| US-5 | user | choose zero-padded sequential naming (`01_clip.mp4`) | files sort correctly in any file manager |
| US-6 | user | choose a custom prefix scheme (`V1001_clip.mp4`) | I can match a studio or asset-tracking convention |
| US-7 | cautious user | preview every rename before it happens (dry run) | I catch mistakes before touching disk |
| US-8 | cautious user | undo the last batch | a bad rename is never permanent |
| US-9 | privacy-minded user | know nothing leaves my machine | I can use VaultFlow on sensitive material |

---

## 3. Functional Requirements

Each requirement uses EARS-style phrasing: *WHEN/IF ... THE SYSTEM SHALL ...*

### 3.1 Directory Selection

- **FR-1.1** WHEN the user supplies a directory path, THE SYSTEM SHALL resolve it to an absolute `pathlib.Path` and verify that it exists and is a directory.
- **FR-1.2** IF the path does not exist, is not a directory, or is not readable and writable, THE SYSTEM SHALL abort with a clear error and SHALL NOT modify anything.
- **FR-1.3** THE SYSTEM SHALL scan only the top level of the selected directory by default; recursion SHALL be opt-in (`recursive=True`).
- **FR-1.4** THE SYSTEM SHALL NOT follow symlinks and SHALL skip them with a logged warning.
- **FR-1.5** THE SYSTEM SHALL exclude hidden files and directories from results by default:
  - names beginning with `.`
  - Windows entries with `FILE_ATTRIBUTE_HIDDEN` or `FILE_ATTRIBUTE_SYSTEM`
  - macOS entries with the `UF_HIDDEN` flag
  - VaultFlow's own `.vaultflow/` state folder
- **FR-1.6** THE SYSTEM SHALL allow `include_hidden=True` to override FR-1.5 (except the `.vaultflow/` folder).
- **FR-1.7** WHERE an extension filter is provided (e.g. `.mp4`, `.mov`), THE SYSTEM SHALL return only matching files (case-insensitive).
- **FR-1.8** FOR every discovered file, THE SYSTEM SHALL collect: absolute path, name, stem, suffix, size in bytes, modified time, created time (best effort per OS), and hidden flag.

### 3.2 Manual File Ranking

- **FR-2.1** THE SYSTEM SHALL let the user assign an integer rank `>= 1` to any file.
- **FR-2.2** THE SYSTEM SHALL reject duplicate ranks within one batch and report the conflicting files.
- **FR-2.3** THE SYSTEM SHALL reject ranks `< 1` or non-integer values.
- **FR-2.4** THE SYSTEM SHALL permit gaps in ranks (e.g. 1, 2, 5) and SHALL compact them to a contiguous sequence when numbering.
- **FR-2.5** WHERE some files are unranked, THE SYSTEM SHALL place them after all ranked files, ordered by a deterministic fallback (natural filename sort, case-insensitive).
- **FR-2.6** THE SYSTEM SHALL support a `ranked_only` mode that renames only ranked files and leaves the rest untouched.
- **FR-2.7** THE SYSTEM SHALL let the user save and reload a ranking as a local JSON file keyed by filename.

### 3.3 Padded Naming Rules

- **FR-3.1 Padded mode.** THE SYSTEM SHALL generate names as `{index:0{width}d}_{original_name}`.
  - `width = max(2, len(str(total_files)))`, so 9 files give `01`, 120 files give `001`.
  - The user MAY override `width` explicitly.
  - Example: `trailer.mp4` ranked 3 of 12 becomes `03_trailer.mp4`.
- **FR-3.2 Custom prefix mode.** THE SYSTEM SHALL generate names as `{prefix}{start + n}_{original_name}`.
  - Inputs: `prefix` (string, may be empty), `start` (integer, default `1`), `step` (integer, default `1`).
  - Example: `prefix="V"`, `start=1001` yields `V1001_intro.mp4`, `V1002_scene.mp4`, ...
  - Zero-padding of the numeric part applies only if the user sets `width`.
- **FR-3.4** THE SYSTEM SHALL preserve the original file extension exactly, including case.
- **FR-3.5** THE SYSTEM SHALL validate prefixes against characters illegal on Windows, macOS, or Linux (`<>:"/\|?*`, control characters) and reject them.
- **FR-3.6** THE SYSTEM SHALL enforce a maximum filename length of 255 bytes after renaming and reject any rename that exceeds it.
- **FR-3.7** WHERE a file already carries a VaultFlow prefix from an earlier run (matching `^\d+_` or the active custom pattern), THE SYSTEM SHALL offer `strip_existing=True` to prevent stacked prefixes (`01_01_clip.mp4`).

### 3.4 Safety, Preview, and Undo

- **FR-4.1** THE SYSTEM SHALL default to **dry-run**: it computes and displays a full `old -> new` plan, and writes nothing until the user explicitly commits.
- **FR-4.2** BEFORE applying, THE SYSTEM SHALL detect and block:
  - target-name collisions within the batch
  - collisions with files that are not part of the batch
  - case-only collisions on case-insensitive filesystems
- **FR-4.3** THE SYSTEM SHALL apply renames with a collision-safe strategy so that overlapping or swapping names (e.g. `01_a` and `02_b` exchanging positions) never overwrite data.
- **FR-4.4** THE SYSTEM SHALL NEVER overwrite an existing file.
- **FR-4.5** IF any rename fails mid-batch, THE SYSTEM SHALL roll back the operations already completed in that batch and report the failure.
- **FR-4.6** THE SYSTEM SHALL write a journal entry for every rename operation (see 3.5) so a full batch can be reversed.
- **FR-4.7** THE SYSTEM SHALL provide `undo` for the most recent batch, verifying that each file still exists at its new name before reverting.

### 3.5 Logging and Local-First Storage

- **FR-5.1** THE SYSTEM SHALL store all state in a `.vaultflow/` folder inside the target directory (journal, saved rankings).
- **FR-5.2** THE SYSTEM SHALL write an append-only JSON Lines journal, one record per operation, with batch ID, UTC timestamp, source path, destination path, status, and file size.
- **FR-5.3** THE SYSTEM SHALL make no network requests and SHALL have no runtime dependency on any remote service.
- **FR-5.4** THE SYSTEM SHALL use only the Python standard library for the core engine.

---

## 4. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-1 | **Local-first:** works fully offline; no account, no telemetry. |
| NFR-2 | **Safety:** zero data loss in the tested failure scenarios (collision, permission error, interrupted batch). |
| NFR-3 | **Performance:** scan 10,000 files in under 2 seconds on a typical SSD; plan generation under 500 ms. |
| NFR-4 | **Portability:** Windows 10+, macOS 12+, and Linux; Python 3.11+. |
| NFR-5 | **Determinism:** the same input state and settings always produce the same plan. |
| NFR-6 | **Testability:** scanner, renamer, and logger are independently testable with `tmp_path` fixtures, with no GUI needed. |
| NFR-7 | **Unicode:** correct handling of non-ASCII filenames (NFC normalization for comparisons). |

---

## 5. Acceptance Criteria

1. **Given** a folder with `b.mp4`, `a.mp4`, `c.mp4`, **when** the user ranks `c=1, a=2, b=3` and runs padded mode, **then** the plan is `c.mp4 -> 01_c.mp4`, `a.mp4 -> 02_a.mp4`, `b.mp4 -> 03_b.mp4`.
2. **Given** `prefix="V"`, `start=1001`, **when** two files are renamed, **then** they become `V1001_...` and `V1002_...`.
3. **Given** a folder containing `.DS_Store` and `.hidden.txt`, **when** scanned with defaults, **then** neither appears in results.
4. **Given** two files ranked `2`, **when** the plan is built, **then** it fails with a duplicate-rank error and nothing on disk changes.
5. **Given** a committed batch, **when** `undo` runs, **then** every file returns to its original name and the journal records the reversal.
6. **Given** a target name that already exists outside the batch, **when** the plan is built, **then** the collision is reported and commit is blocked.

---

## 6. Glossary

- **Batch:** one committed set of renames sharing a `batch_id`.
- **Plan:** the ordered list of `old -> new` operations produced before commit.
- **Journal:** the append-only local log used for audit and undo.
