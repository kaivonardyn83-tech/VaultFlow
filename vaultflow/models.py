"""
vaultflow.models
~~~~~~~~~~~~~~~~
Core immutable data models for VaultFlow.

Design constraints
------------------
- All dataclasses are *frozen* (immutable after construction) to make plans
  safe to share across threads and easy to reason about.
- No third-party dependencies — standard library only.
- Python 3.11+ features (``Self``, ``StrEnum``, match statements) are used
  where they improve clarity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
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


# ── NamingMode ─────────────────────────────────────────────────────────────────

class NamingMode(str, Enum):
    """Strategy used to generate the numeric portion of renamed files.

    Attributes
    ----------
    PADDED:
        Zero-padded sequential index, e.g. ``photo_001.jpg``.
        The width of the numeric field is controlled by :attr:`NamingRule.width`.
    CUSTOM:
        User-supplied prefix/counter combination; width and step are still
        respected but the prefix string may contain arbitrary text.
    """

    PADDED = "padded"
    CUSTOM = "custom"


# ── NamingRule ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NamingRule:
    """Declarative specification of how output file names are constructed.

    Parameters
    ----------
    mode:
        One of :class:`NamingMode`.  Defaults to ``PADDED``.
    width:
        Minimum digit width for the numeric counter, zero-padded.
        E.g. ``width=3`` → ``001``, ``042``, ``100``.  Must be >= 1.
    prefix:
        String prepended to the counter.  May be empty.
    start:
        Initial counter value (inclusive).  Must be >= 0.
    step:
        Increment applied between consecutive files.  Must be >= 1.
    strip_existing:
        When ``True``, attempt to remove any existing numeric suffix from the
        original stem before applying the new name pattern.
    """

    mode: NamingMode = NamingMode.PADDED
    width: int = 3
    prefix: str = ""
    start: int = 1
    step: int = 1
    strip_existing: bool = False

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError(f"NamingRule.width must be >= 1, got {self.width!r}")
        if self.start < 0:
            raise ValueError(f"NamingRule.start must be >= 0, got {self.start!r}")
        if self.step < 1:
            raise ValueError(f"NamingRule.step must be >= 1, got {self.step!r}")

    def format_index(self, index: int) -> str:
        """Return the formatted counter string for *index*.

        Parameters
        ----------
        index:
            Zero-based position of the file in the ranked sequence.

        Returns
        -------
        str
            Prefix + zero-padded counter, e.g. ``"photo_007"``.
        """
        counter = self.start + index * self.step
        return f"{self.prefix}{counter:0{self.width}d}"


# ── RenameOp ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RenameOp:
    """A single, atomic rename instruction: move *src* → *dst*.

    Parameters
    ----------
    src:
        Absolute source path (the file as it currently exists on disk).
    dst:
        Absolute destination path (the file's new name/location).
    rank:
        The sort key value used to order this file within the plan.
        Stored for auditing and undo-journal purposes; the exact type
        depends on the ranking strategy (e.g. ``float`` for mtime,
        ``str`` for name-based sort).
    index:
        Zero-based position of this operation in the final sorted sequence.
    """

    src: Path
    dst: Path
    rank: float | str | int
    index: int

    @property
    def is_noop(self) -> bool:
        """``True`` when *src* and *dst* resolve to the same path."""
        return self.src.resolve() == self.dst.resolve()


# ── RenamePlan ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class RenamePlan:
    """The complete, validated output of the planning stage.

    A ``RenamePlan`` is ready to hand to the executor if and only if
    :attr:`is_safe` is ``True``.

    Parameters
    ----------
    ops:
        Ordered sequence of :class:`RenameOp` instances, one per file.
    warnings:
        Non-fatal advisory messages (e.g. skipped hidden files).
    conflicts:
        Destination paths that appear more than once across *ops*, or that
        collide with existing files outside the rename set.  A non-empty
        ``conflicts`` set means the plan is *unsafe*.
    """

    ops: tuple[RenameOp, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    conflicts: frozenset[Path] = field(default_factory=frozenset)

    @property
    def is_safe(self) -> bool:
        """``True`` when the plan has no conflicts and can be executed as-is."""
        return len(self.conflicts) == 0

    # ── convenience helpers ────────────────────────────────────────────────────

    @property
    def effective_ops(self) -> tuple[RenameOp, ...]:
        """All operations that actually change the file name (no-ops excluded)."""
        return tuple(op for op in self.ops if not op.is_noop)

    def __len__(self) -> int:
        return len(self.ops)

    def __iter__(self):  # type: ignore[override]
        return iter(self.ops)
