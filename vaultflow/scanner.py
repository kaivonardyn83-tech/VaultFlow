"""
vaultflow.scanner
~~~~~~~~~~~~~~~~~
Directory validation and high-performance file scanning.

Public API
----------
validate_root(root)
    Verify a path is a readable/writable directory; raise on any problem.

scan_directory(root, *, recursive, include_hidden, extensions)
    Walk a directory and return a sorted list of :class:`~vaultflow.models.FileEntry`
    objects, one per matching file.

Implementation notes
--------------------
- ``os.scandir`` is used instead of ``Path.iterdir`` because it bundles a
  ``DirEntry`` with the stat result, avoiding a second ``stat()`` syscall per
  file on most platforms (Linux via ``d_type``, Windows always).
- The ``FileEntry`` constructor mirrors the fields produced by ``DirEntry``
  so we never call ``Path.stat()`` on the hot path.
- Recursive descent re-uses the same filter logic at every level so hidden
  *directories* (e.g. ``.git/``) are pruned when ``include_hidden=False``.
- The ``.vaultflow/`` directory is always excluded regardless of other flags;
  it is VaultFlow's own state directory and must never be part of a rename
  plan.
"""

from __future__ import annotations

import logging
import os
import stat as stat_module
from pathlib import Path

from vaultflow.errors import DirectoryAccessError, InvalidDirectoryError
from vaultflow.models import FileEntry

__all__ = ["validate_root", "scan_directory"]

logger = logging.getLogger(__name__)

# Name of the internal state directory — always excluded from scans.
_VAULTFLOW_DIR = ".vaultflow"


# ── helpers ────────────────────────────────────────────────────────────────────

def _is_hidden_entry(entry: os.DirEntry) -> bool:  # type: ignore[type-arg]
    """Return ``True`` when *entry* should be considered hidden.

    Strategy
    --------
    - **Windows**: test the ``FILE_ATTRIBUTE_HIDDEN`` bit (0x02) from
      ``entry.stat(follow_symlinks=False)``.  Fall back to dot-prefix check
      if ``st_file_attributes`` is not available (e.g. network drives).
    - **Unix/macOS**: a leading dot is the sole convention.
    """
    if os.name == "nt":
        try:
            st = entry.stat(follow_symlinks=False)
            return bool(st.st_file_attributes & stat_module.FILE_ATTRIBUTE_HIDDEN)
        except (OSError, AttributeError):
            pass  # fall through to dot-prefix check
    return entry.name.startswith(".")


def _build_entry(entry: os.DirEntry) -> FileEntry:  # type: ignore[type-arg]
    """Construct a :class:`FileEntry` from a ``DirEntry`` without an extra stat call.

    ``os.scandir`` caches the stat result inside ``DirEntry`` on most OSes, so
    calling ``entry.stat()`` here is effectively free.
    """
    st = entry.stat(follow_symlinks=False)
    path = Path(entry.path)

    if os.name == "nt":
        try:
            is_hidden = bool(st.st_file_attributes & stat_module.FILE_ATTRIBUTE_HIDDEN)
        except AttributeError:
            is_hidden = entry.name.startswith(".")
    else:
        is_hidden = entry.name.startswith(".")

    return FileEntry(
        path=path,
        name=path.name,
        stem=path.stem,
        suffix=path.suffix,
        size=st.st_size,
        mtime=st.st_mtime,
        ctime=st.st_ctime,
        is_hidden=is_hidden,
    )


# ── public API ─────────────────────────────────────────────────────────────────

def validate_root(root: Path | str) -> Path:
    """Verify that *root* is a usable working directory for VaultFlow.

    Checks performed (in order)
    ----------------------------
    1. The path exists.
    2. The path is a directory (not a file, symlink-to-file, etc.).
    3. The current process has both **read** and **write** access.

    Parameters
    ----------
    root:
        Path to validate.  Accepts both ``str`` and :class:`~pathlib.Path`.

    Returns
    -------
    Path
        The resolved, absolute form of *root* (useful for callers that pass a
        relative path).

    Raises
    ------
    InvalidDirectoryError
        If the path does not exist or is not a directory.
    DirectoryAccessError
        If the directory cannot be read or written by the current process.
    """
    root = Path(root).resolve()

    if not root.exists():
        raise InvalidDirectoryError(
            f"Path does not exist: {root}"
        )

    if not root.is_dir():
        raise InvalidDirectoryError(
            f"Path is not a directory: {root}"
        )

    # os.access respects effective UID/GID on Unix and ACLs on Windows.
    if not os.access(root, os.R_OK):
        raise DirectoryAccessError(
            f"Directory is not readable: {root}"
        )

    if not os.access(root, os.W_OK):
        raise DirectoryAccessError(
            f"Directory is not writable: {root}"
        )

    return root


def scan_directory(
    root: Path | str,
    *,
    recursive: bool = False,
    include_hidden: bool = False,
    extensions: set[str] | frozenset[str] | list[str] | None = None,
) -> list[FileEntry]:
    """Scan *root* and return a sorted list of :class:`FileEntry` objects.

    Parameters
    ----------
    root:
        Directory to scan.  Must pass :func:`validate_root`.
    recursive:
        When ``True``, descend into sub-directories.  Sub-directories that are
        hidden (or named ``.vaultflow``) are pruned according to the same rules
        as files.
    include_hidden:
        When ``False`` (default), hidden files *and* hidden directories are
        excluded from the results.  The ``.vaultflow/`` directory is **always**
        excluded regardless of this flag.
    extensions:
        An optional collection of file extensions to include, each with a
        leading dot (e.g. ``{".jpg", ".png"}``).  The comparison is
        **case-insensitive**.  When ``None``, all extensions are accepted.

    Returns
    -------
    list[FileEntry]
        Files matching all active filters, sorted ascending by
        ``(entry.name.lower(), entry.name)`` — a locale-friendly,
        case-insensitive primary key with the original case as a tiebreaker.

    Raises
    ------
    InvalidDirectoryError
        Propagated from :func:`validate_root` if *root* is not a directory.
    DirectoryAccessError
        Propagated from :func:`validate_root` if *root* is not accessible.

    Notes
    -----
    - ``PermissionError`` raised while reading *individual* entries (not the
      root itself) is caught, logged at ``WARNING`` level, and skipped so the
      scan completes even when a sub-tree is inaccessible.
    - Symlinks are *not* followed; symlink targets are intentionally excluded
      from rename plans to avoid accidental data loss.
    """
    root = validate_root(root)

    # Normalise the extension filter once, outside the hot loop.
    ext_filter: frozenset[str] | None = None
    if extensions is not None:
        ext_filter = frozenset(e.lower() for e in extensions)

    results: list[FileEntry] = []
    _collect(root, recursive, include_hidden, ext_filter, results)

    results.sort(key=lambda e: (e.name.lower(), e.name))
    return results


# ── internal traversal ─────────────────────────────────────────────────────────

def _collect(
    directory: Path,
    recursive: bool,
    include_hidden: bool,
    ext_filter: frozenset[str] | None,
    out: list[FileEntry],
) -> None:
    """Recursively (or shallowly) populate *out* with matching ``FileEntry`` objects.

    Separated from :func:`scan_directory` so the public function handles
    validation and sorting while this function focuses purely on traversal.
    """
    try:
        scanner = os.scandir(directory)
    except PermissionError as exc:
        logger.warning("Cannot read directory, skipping: %s — %s", directory, exc)
        return

    with scanner:
        for entry in scanner:
            # ── always-excluded items ──────────────────────────────────────────
            if entry.name == _VAULTFLOW_DIR:
                logger.debug("Skipping VaultFlow state directory: %s", entry.path)
                continue

            # ── hidden filter ──────────────────────────────────────────────────
            hidden = _is_hidden_entry(entry)
            if hidden and not include_hidden:
                logger.debug("Skipping hidden entry: %s", entry.path)
                continue

            # ── recurse into sub-directories ───────────────────────────────────
            if entry.is_dir(follow_symlinks=False):
                if recursive:
                    _collect(
                        Path(entry.path),
                        recursive,
                        include_hidden,
                        ext_filter,
                        out,
                    )
                # Directories are never added as FileEntry objects.
                continue

            # ── skip symlinks ──────────────────────────────────────────────────
            if entry.is_symlink():
                logger.debug("Skipping symlink: %s", entry.path)
                continue

            # ── extension filter ───────────────────────────────────────────────
            if ext_filter is not None:
                suffix = Path(entry.name).suffix.lower()
                if suffix not in ext_filter:
                    continue

            # ── build FileEntry (may fail with PermissionError on some FSes) ───
            try:
                file_entry = _build_entry(entry)
            except PermissionError as exc:
                logger.warning("Cannot stat file, skipping: %s — %s", entry.path, exc)
                continue
            except OSError as exc:
                # Catch broader OS errors (e.g. stale NFS handle) without
                # aborting the whole scan.
                logger.warning("OS error reading file, skipping: %s — %s", entry.path, exc)
                continue

            out.append(file_entry)
