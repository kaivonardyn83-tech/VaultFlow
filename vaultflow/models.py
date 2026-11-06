"""
vaultflow.models
~~~~~~~~~~~~~~~~
Core data models for VaultFlow.

Design constraints
------------------
- ``FileEntry`` and ``RenamePlan`` are *frozen*.  ``RenameOp`` is mutable on
  purpose: the executor reports progress by updating ``op.status``.
- No third-party dependencies — standard library only.
- Python 3.11+ features are used where they improve clarity.

Naming rules live in :mod:`vaultflow.rules` (``NamingRule`` / ``RuleKind``);
there is deliberately no second ``NamingRule`` here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ── FileEntry ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FileEntry:
    """A snapshot of a single file's metadata at scan time.

    Parameters
    ----------
    path:
        Absolute ``Path`` to the file.
    name:
        Full filename including extension (``path.name``).
    stem:
        Filename without extension (``path.stem``).
    suffix:
        File extension including the leading dot, e.g. ``".mp4"``
        (``path.suffix``).  Empty string for files with no extension.
    size:
        File size in bytes as reported by ``os.stat``.
    mtime:
        Last-modified time as a POSIX timestamp (float seconds since epoch).
    ctime:
        Creation time (Windows) or last metadata-change time (Unix) as a
        POSIX timestamp.
    is_hidden:
        ``True`` when the file is considered hidden:
        - On Unix: name starts with ``.``.
        - On Windows: the ``FILE_ATTRIBUTE_HIDDEN`` bit is set.
    """

    path: Path
    name: str
    stem: str
    suffix: str
    size: int
    mtime: float
    ctime: float
    is_hidden: bool

    # ── convenience constructors ───────────────────────────────────────────────

    @classmethod
    def from_path(cls, path: Path) -> "FileEntry":
        """Build a ``FileEntry`` by reading metadata from *path*.

        Parameters
        ----------
        path:
            Path to an existing file.

        Raises
        ------
        OSError
            If ``os.stat`` fails (missing file, permission denied, etc.).
        """
        stat = path.stat()

        # Determine hidden status in a cross-platform way.
        if os.name == "nt":
            # Windows: check the FILE_ATTRIBUTE_HIDDEN bit (0x02).
            import stat as stat_module  # local import to keep top-level clean
            is_hidden = bool(stat.st_file_attributes & stat_module.FILE_ATTRIBUTE_HIDDEN)
        else:
            is_hidden = path.name.startswith(".")

        return cls(
            path=path.resolve(),
            name=path.name,
            stem=path.stem,
            suffix=path.suffix,
            size=stat.st_size,
            mtime=stat.st_mtime,
            ctime=stat.st_ctime,
            is_hidden=is_hidden,
        )


# ── RenameOp ───────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class RenameOp:
    """A single rename instruction: move *source_path* -> *target_path*.

    This is the shape ``planner.py`` builds, ``executor.py`` mutates (it
    updates ``status`` / ``error`` as it works) and ``journal.py`` records.
    It is therefore deliberately NOT frozen: the executor reports progress by
    setting ``op.status``.

    Parameters
    ----------
    source_path:
        Absolute path of the file as it currently exists on disk.
    target_path:
        Absolute path the file will have after the rename.
    status:
        Lifecycle marker.  ``PENDING`` when planned; the executor then sets
        ``RUNNING``, ``COMPLETED``, ``FAILED``, ``ROLLED_BACK``,
        ``ROLLBACK_FAILED``, ``SKIPPED`` or ``DRY_RUN``.
    error:
        Human-readable failure description, or ``None``.
    rank:
        Optional sort key used to order the file (auditing only).
    index:
        Zero-based position of this operation in the planned sequence.
    """

    source_path: Path
    target_path: Path
    status: str = "PENDING"
    error: str | None = None
    rank: float | str | int | None = None
    index: int = 0

    # Short aliases kept for callers that use the src/dst vocabulary.
    @property
    def src(self) -> Path:
        return self.source_path

    @property
    def dst(self) -> Path:
        return self.target_path

    @property
    def is_noop(self) -> bool:
        """``True`` when the target path is identical (same spelling) to the source."""
        return os.fspath(self.source_path) == os.fspath(self.target_path)


# ── RenamePlan ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RenamePlan:
    """The complete, validated output of the planning stage.

    Parameters
    ----------
    ops:
        Ordered :class:`RenameOp` instances, one per file.
    warnings:
        Non-fatal advisory messages.
    conflicts:
        Destination paths that make the plan unsafe (normally empty, because
        the planner raises ``CollisionError`` instead of returning them).
    plan_id:
        Unique identifier; the executor requires it to label the journal entry.
        Generated automatically.
    total_count:
        Number of operations (filled from ``ops`` when left at 0).
    created_at:
        UTC creation timestamp.
    """

    ops: Sequence[RenameOp]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    conflicts: frozenset[Path] = field(default_factory=frozenset)
    plan_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    total_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if not self.total_count:
            object.__setattr__(self, "total_count", len(self.ops))

    @property
    def is_safe(self) -> bool:
        """``True`` when the plan has no conflicts and can be executed as-is."""
        return len(self.conflicts) == 0

    @property
    def effective_ops(self) -> tuple[RenameOp, ...]:
        """All operations that actually change the file name (no-ops excluded)."""
        return tuple(op for op in self.ops if not op.is_noop)

    def __len__(self) -> int:
        return len(self.ops)

    def __iter__(self):  # type: ignore[override]
        return iter(self.ops)
