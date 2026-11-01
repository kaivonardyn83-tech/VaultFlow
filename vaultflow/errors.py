"""
vaultflow.errors
~~~~~~~~~~~~~~~~
Custom exception hierarchy for VaultFlow.

All exceptions derive from VaultFlowError so callers can catch the entire
family with a single ``except VaultFlowError`` clause, or intercept specific
sub-classes for finer-grained error handling.
"""

from __future__ import annotations


class VaultFlowError(Exception):
    """Base class for every VaultFlow exception.

    Attributes
    ----------
    message:
        Human-readable description of what went wrong.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}({self.message!r})"


# ── Directory errors ───────────────────────────────────────────────────────────

class InvalidDirectoryError(VaultFlowError):
    """Raised when a path does not point to a valid, existing directory.

    Example triggers:
    - The path does not exist.
    - The path exists but is a file, not a directory.
    """


class DirectoryAccessError(VaultFlowError):
    """Raised when a directory exists but cannot be read or written.

    Example triggers:
    - Insufficient OS permissions to list directory contents.
    - Directory is on a locked or unmounted volume.
    """


# ── Planning errors ────────────────────────────────────────────────────────────

class RankingError(VaultFlowError):
    """Raised when file ranking cannot be determined or produces an ambiguous
    ordering.

    Example triggers:
    - Sort key is missing or unsupported.
    - Two files produce identical rank keys when a stable order is required.
    """


class NamingError(VaultFlowError):
    """Raised when a NamingRule produces an invalid or un-representable name.

    Example triggers:
    - Computed name is empty after stripping.
    - Name contains characters illegal on the target filesystem.
    - Width is too small to accommodate the required index digits.
    """


class CollisionError(VaultFlowError):
    """Raised when two or more rename operations would write to the same
    destination path, making the plan unsafe to execute.

    Example triggers:
    - Two source files map to identical target names.
    - A computed target name matches an existing file not in the rename set.
    """


# ── Execution errors ───────────────────────────────────────────────────────────

class RenameError(VaultFlowError):
    """Raised when a rename operation fails during execution.

    Example triggers:
    - OS-level rename/move fails (permissions, cross-device link, etc.).
    - Source file disappeared between planning and execution.

    Attributes
    ----------
    src:
        Source path that could not be renamed (str or None if unknown).
    dst:
        Destination path that was attempted (str or None if unknown).
    """

    def __init__(self, message: str = "", src: str | None = None, dst: str | None = None) -> None:
        super().__init__(message)
        self.src = src
        self.dst = dst

    def __repr__(self) -> str:  # pragma: no cover
        return f"{type(self).__name__}(src={self.src!r}, dst={self.dst!r}, message={self.message!r})"


class JournalError(VaultFlowError):
    """Raised when the undo journal cannot be read, written, or parsed.

    Example triggers:
    - Journal file is corrupt or written by an incompatible version.
    - Filesystem prevents writing the journal (read-only mount, quota exceeded).
    """
