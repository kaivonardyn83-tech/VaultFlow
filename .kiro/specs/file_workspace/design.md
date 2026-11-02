# VaultFlow — Design: File Workspace

**Spec:** `file_workspace`
**Runtime:** Python 3.11+, standard library only (core engine)
**Philosophy:** local-first, deterministic, dry-run by default, reversible.

---

## 1. Architecture Overview

```
            ┌────────────────────────────┐
            │  UI layer (CLI now, GUI    │
            │  later) - thin, no logic   │
            └─────────────┬──────────────┘
                          │ calls
        ┌─────────────────┼──────────────────┐
        ▼                 ▼                  ▼
  ┌───────────┐     ┌────────────┐     ┌───────────┐
  │ scanner.py│────▶│ renamer.py │────▶│ logger.py │
  │ discover  │     │ plan+apply │     │ journal   │
  └───────────┘     └────────────┘     └───────────┘
        │                 │                  │
        └──────── local filesystem only ─────┘
                 <target>/.vaultflow/
```

Data flows one way: **scan -> rank -> plan -> (dry-run | commit) -> journal -> (undo)**.

### Package layout

```
vaultflow/
├── __init__.py
├── models.py        # shared dataclasses and enums
├── scanner.py       # directory traversal, hidden filtering, metadata
├── renamer.py       # ranking, naming rules, plan, apply, rollback
├── logger.py        # JSONL journal, undo source of truth
├── errors.py        # exception hierarchy
└── cli.py           # argparse entry point (Week 1 UI)
tests/
├── test_scanner.py
├── test_renamer.py
└── test_logger.py
```

---

## 2. Shared Models (`models.py`)

```python
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

@dataclass(frozen=True, slots=True)
class FileEntry:
    path: Path            # absolute
    name: str
    stem: str
    suffix: str           # original case, includes dot
    size: int             # bytes
    mtime: float          # epoch seconds
    ctime: float          # best effort per OS
    is_hidden: bool

class NamingMode(Enum):
    PADDED = "padded"     # 01_name.ext
    CUSTOM = "custom"     # V1001_name.ext

@dataclass(frozen=True, slots=True)
class NamingRule:
    mode: NamingMode = NamingMode.PADDED
    width: int | None = None      # None = auto: max(2, len(str(n)))
    prefix: str = ""              # CUSTOM only
    start: int = 1
    step: int = 1
    strip_existing: bool = False

@dataclass(frozen=True, slots=True)
class RenameOp:
    src: Path
    dst: Path
    rank: int | None              # None if unranked
    index: int                    # final 1-based position

@dataclass(slots=True)
class RenamePlan:
    ops: list[RenameOp]
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return not self.conflicts
```

---

## 3. `scanner.py` — Discovery and Metadata

**Responsibility:** read-only. Walk a directory, filter, and return `FileEntry` objects. It never mutates the filesystem.

### Public API

```python
def scan_directory(
    root: Path | str,
    *,
    recursive: bool = False,
    include_hidden: bool = False,
    extensions: set[str] | None = None,   # {".mp4", ".mov"}, case-insensitive
) -> list[FileEntry]: ...

def is_hidden(path: Path, st: os.stat_result | None = None) -> bool: ...
def validate_root(root: Path | str) -> Path: ...
```

### Traversal strategy

- Use `os.scandir()` (one syscall batch per directory, with `DirEntry.stat()` cached) rather than `Path.iterdir()` plus a separate `stat()` per file. This is what meets NFR-3.
- Use `pathlib.Path` for everything exposed to callers (`FileEntry.path`, `validate_root`).
- Recursion is an explicit stack/queue loop, not Python recursion, to avoid depth limits.
- `DirEntry.is_file(follow_symlinks=False)`: symlinks are skipped and recorded as warnings.
- Output is sorted by natural order (case-insensitive, digit-aware) so scans are deterministic.

### Hidden file filtering — `is_hidden()`

Checked in this order, first match wins:

1. **Dotfile:** `name.startswith(".")`
2. **Windows:** `st.st_file_attributes & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)` (via `stat.FILE_ATTRIBUTE_*`)
3. **macOS/BSD:** `st.st_flags & stat.UF_HIDDEN`, accessed with `getattr` since the field is absent on Linux
4. **Reserved:** the `.vaultflow/` state directory is **always** excluded, even with `include_hidden=True`

Platform attributes are read with `getattr(st, "...", 0)`, so the module runs unmodified on all three OSes with no `sys.platform` branching in the hot path.

### Metadata collection

From the cached `os.stat_result`:

| Field | Source |
|-------|--------|
| `size` | `st_size` |
| `mtime` | `st_mtime` |
| `ctime` | `st_birthtime` if present (macOS/Windows 3.12+), else `st_ctime` |
| `suffix` / `stem` | `PurePath(name)` |
| `is_hidden` | `is_hidden()` |

### Error handling

- `validate_root` raises `InvalidDirectoryError` (missing, not a dir) or `DirectoryAccessError` (no read/write permission).
- A single unreadable entry yields a warning and a skip; it never aborts the whole scan (`PermissionError` caught per entry).

---

## 4. `renamer.py` — Ranking, Planning, Applying

**Responsibility:** the only module that mutates filenames. Planning is pure (no disk writes); applying is the only side-effecting step.

### Public API

```python
def apply_ranking(
    entries: list[FileEntry],
    ranks: dict[str, int],          # filename -> rank
    *,
    ranked_only: bool = False,
) -> list[tuple[FileEntry, int | None]]: ...

def build_plan(
    ordered: list[tuple[FileEntry, int | None]],
    rule: NamingRule,
) -> RenamePlan: ...

def apply_plan(
    plan: RenamePlan,
    journal: "Journal",
    *,
    dry_run: bool = True,
) -> "BatchResult": ...

def undo_batch(journal: "Journal", batch_id: str | None = None) -> "BatchResult": ...
```

### Ranking (`apply_ranking`)

1. Validate: ranks are ints `>= 1`, no duplicates, every key exists in `entries`. Violations raise `RankingError` listing offenders.
2. Sort ranked files ascending by rank.
3. Append unranked files in natural filename order (skipped when `ranked_only=True`).
4. Compact gaps: final `index` is positional (1..n), independent of the raw rank values.

### Naming (`build_plan`)

```python
# PADDED
width = rule.width or max(2, len(str(len(ordered))))
new_name = f"{index:0{width}d}_{base}{suffix}"

# CUSTOM
number = rule.start + (index - 1) * rule.step
digits = f"{number:0{rule.width}d}" if rule.width else str(number)
new_name = f"{rule.prefix}{digits}_{base}{suffix}"
```

- `base` is the stem, with an existing prefix stripped when `strip_existing=True` (regex `^\d+_` for padded, `^{re.escape(prefix)}\d+_` for custom).
- Validation: illegal characters, 255-byte limit, empty result.
- Collision detection builds a `dict[casefolded_name, Path]` of the post-rename state (batch targets plus untouched files in the directory). Any duplicate lands in `plan.conflicts`.
- Ops whose `src == dst` are dropped (already correctly named), making repeat runs idempotent.

### Applying — two-phase rename

Naive sequential renames break on overlapping names (e.g. `02_b -> 01_b` while `01_a -> 02_a`). Use two phases:

```
Phase 1:  src  ->  .vf_tmp_<batch_id>_<n>      (every op, in the same directory)
Phase 2:  tmp  ->  dst
```

- Temp names live in the same directory, so `os.rename`/`Path.rename` stays an atomic same-filesystem operation.
- Uses `os.rename` semantics and **pre-checks** `dst` absence rather than `os.replace`, so an existing file is never overwritten.
- Each completed step is journaled immediately (`status="done"`), so a crash can be recovered from the journal.
- **Rollback:** on any exception, reverse completed steps in reverse order (tmp -> src, or dst -> tmp -> src) and journal `status="rolled_back"`. The original exception is re-raised as `RenameError`.
- `dry_run=True` (default) returns a `BatchResult` describing what would happen and writes nothing, including no journal entries.

### Undo (`undo_batch`)

- Reads the target batch (default: latest committed, not yet undone) from the journal.
- Verifies each `dst` still exists and each `src` is free; otherwise aborts with a precise report.
- Reuses the same two-phase mechanism in reverse, then journals `status="undone"`.

---

## 5. `logger.py` — Local Journal

**Responsibility:** durable, append-only, human-readable record. It is the single source of truth for undo.

### Storage

```
<target>/.vaultflow/
├── journal.jsonl        # append-only operation log
└── rankings.json        # optional saved ranking (name -> rank)
```

Stored beside the files, so the journal travels with the folder and stays fully local.

### Record schema (one JSON object per line)

```json
{
  "v": 1,
  "batch_id": "20261101T142530Z-a3f9",
  "ts": "2026-11-01T14:25:30.412Z",
  "event": "rename",
  "src": "trailer.mp4",
  "dst": "03_trailer.mp4",
  "status": "done",
  "size": 48211234,
  "rule": {"mode": "padded", "width": 2}
}
```

- Paths are stored **relative to the target directory** so a folder can be moved and still be undone.
- `event`: `batch_start`, `rename`, `batch_end`, `undo`.
- `status`: `planned | done | failed | rolled_back | undone`.

### Public API

```python
class Journal:
    def __init__(self, root: Path): ...
    def start_batch(self, rule: NamingRule) -> str: ...      # returns batch_id
    def record(self, batch_id: str, src: Path, dst: Path, status: str, **extra) -> None: ...
    def end_batch(self, batch_id: str, status: str) -> None: ...
    def latest_batch(self, *, undoable_only: bool = True) -> list[dict]: ...
    def read_batch(self, batch_id: str) -> list[dict]: ...
    def history(self, limit: int = 20) -> list[dict]: ...
```

### Reliability rules

- Open in append mode, write one line, `flush()` and `os.fsync()` after each record so a crash loses at most the in-flight operation.
- `batch_id` = UTC timestamp plus 4 hex chars from `secrets`; no external dependencies.
- Reading tolerates a truncated final line (skip it and warn).
- The journal is never rewritten or compacted; undo appends new `undo` records instead.
- Python `logging` is used only for console diagnostics; the journal is not a log-level stream.

---

## 6. Errors (`errors.py`)

```
VaultFlowError
├── InvalidDirectoryError
├── DirectoryAccessError
├── RankingError          # duplicates, out-of-range, unknown file
├── NamingError           # illegal chars, too long, bad prefix
├── CollisionError        # plan has conflicts
├── RenameError           # fs failure (after rollback)
└── JournalError          # unreadable or corrupt journal
```

---

## 7. CLI (Week 1 interface)

```
vaultflow scan   <dir> [--recursive] [--hidden] [--ext .mp4 .mov]
vaultflow plan   <dir> --ranks ranks.json [--mode padded|custom] [--prefix V --start 1001] [--width N]
vaultflow apply  <dir> --ranks ranks.json [...same flags...] --yes
vaultflow undo   <dir> [--batch ID]
vaultflow history <dir>
```

`plan` is the default preview and `apply` requires `--yes`. Ranks file format: `{"intro.mp4": 1, "scene.mp4": 2}`.

---

## 8. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| `os.scandir` + `pathlib` | speed from scandir, clean API from pathlib |
| Dry-run by default | renaming is destructive in practice; opt in to commit |
| Two-phase rename | handles swaps and overlaps without overwrite risk |
| JSONL journal in `.vaultflow/` | append-only, greppable, portable, no database dependency |
| Relative paths in journal | folder stays portable and undo keeps working |
| Pure `build_plan` | trivial to unit test; GUI can reuse it for live preview |
| Stdlib only | local-first, easy install, fewer supply-chain concerns |

## 9. Test Strategy

- **scanner:** hidden dotfiles, platform attributes (mocked `st_file_attributes` / `st_flags`), symlink skip, extension filter, unreadable entry, deterministic ordering.
- **renamer:** padded width at 9/10/100 files, custom prefix and step, duplicate ranks, gap compaction, collisions (internal, external, case-only), swap scenario, idempotent re-run, rollback on injected failure.
- **logger:** append and read round trip, truncated final line, relative path portability, latest-batch selection, undo marking.
- All filesystem tests use pytest `tmp_path`; no network, no real user directories.
