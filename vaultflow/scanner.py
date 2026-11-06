"""
VaultFlow - Directory Scanner
=============================

Module: vaultflow/scanner.py

Purpose:
    Scan directories and collect file metadata for VaultFlow's
    local-first batch file renaming workflow.

Features:
    - Directory validation and permission checks.
    - Efficient directory scanning using os.scandir().
    - Optional recursive scanning.
    - Hidden file and directory filtering.
    - Strict, unconditional exclusion of the internal ".vaultflow" directory.
    - Automatic symbolic link exclusion.
    - Flexible name filtering: extensions and glob patterns, accepted through
      either ``filters`` (positional-friendly) or ``extensions`` (keyword).
    - File metadata collection.
    - Deterministic natural filename sorting.
    - Per-entry permission error handling.

Filter semantics:
    Every token in ``filters`` is classified automatically:

    * Contains a glob character (``*``, ``?``, ``[``)  -> filename pattern,
      e.g. ``"clip_*"`` or ``"*final*.pdf"``.
    * Anything else -> extension, e.g. ``".mp4"``, ``"MP4"`` or ``".tar.gz"``.

    Tokens from ``filters`` are merged with ``extensions``, and a file is
    kept when it matches ANY extension or ANY pattern (OR semantics).
    All matching is case-insensitive and applies to the file name only.
    ``None``, an empty collection, or blank tokens mean "no filtering".

Compatibility:
    Python 3.11+

Dependencies:
    Standard library only.

Project dependencies:
    vaultflow.models
    vaultflow.errors

Self-test:
    python -m vaultflow.scanner --self-test
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import stat

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vaultflow.models import FileEntry
from vaultflow.errors import (
    InvalidDirectoryError,
    DirectoryAccessError,
)

__all__ = [
    "FileEntry",
    "VAULTFLOW_INTERNAL_DIR",
    "scan_directory",
    "validate_root",
]


# ============================================================
# LOGGING CONFIGURATION
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

# VaultFlow's internal working directory must never be scanned.
VAULTFLOW_INTERNAL_DIR = ".vaultflow"

# Windows file attributes.
FILE_ATTRIBUTE_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
FILE_ATTRIBUTE_SYSTEM = getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)

# Characters that mark a filter token as a glob pattern rather than an extension.
_GLOB_CHARS = frozenset("*?[")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def _natural_sort_key(filename: str) -> tuple[tuple[int, int | str], ...]:
    """
    Generate a natural, case-insensitive sorting key.

    Examples:
        file1.mp4
        file2.mp4
        file10.mp4

    This ensures numeric portions are compared numerically
    rather than alphabetically.

    Notes:
        ``re.split`` with a single capture group alternates between text
        and digit runs, so digit runs always sit at odd indices.  Using the
        index (instead of ``str.isdigit()``) avoids a crash on characters
        such as "²", which are "digits" to ``isdigit()`` but not to ``int()``.
    """

    parts = re.split(r"(\d+)", filename.casefold())

    return tuple(
        (1, int(part)) if index % 2 else (0, part)
        for index, part in enumerate(parts)
    )


def _is_hidden(name: str, entry_stat: os.stat_result) -> bool:
    """
    Determine whether a file or directory is hidden.

    Checks:
        1. Unix-style dotfile naming.
        2. Windows hidden attribute.
        3. Windows system attribute.

    Returns:
        True if hidden or marked as a system entry.
        False otherwise.
    """

    if name.startswith("."):
        return True

    attributes = getattr(entry_stat, "st_file_attributes", 0)

    hidden_mask = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM

    return bool(attributes & hidden_mask)


def _normalize_extensions(
    extensions: Iterable[str] | None,
) -> set[str] | None:
    """
    Normalize extension filters.

    Example:
        {'.MP4', 'mov', ' .Tar.GZ '} -> {'.mp4', '.mov', '.tar.gz'}

    Extensions without a leading dot are also accepted.  Blank tokens and
    tokens consisting only of dots are ignored.

    Returns:
        A set of lowercase, dot-prefixed extensions, or None if
        ``extensions`` is None.
    """

    if extensions is None:
        return None

    normalized: set[str] = set()

    for extension in extensions:
        extension = extension.strip().casefold()

        if not extension.strip("."):
            continue

        if not extension.startswith("."):
            extension = f".{extension}"

        normalized.add(extension)

    return normalized


def _coerce_tokens(values: object, label: str) -> list[str]:
    """
    Turn a user-supplied filter collection into a clean list of tokens.

    A bare string is treated as a single token (rather than being iterated
    character by character).  Surrounding whitespace is stripped and blank
    tokens are dropped.

    Raises:
        TypeError:
            If ``values`` is not an iterable of strings.
    """

    if values is None:
        return []

    if isinstance(values, str):
        values = [values]

    try:
        iterator = iter(values)  # type: ignore[call-overload]
    except TypeError as exc:
        raise TypeError(
            f"{label} must be a list or set of strings, "
            f"got {type(values).__name__}"
        ) from exc

    tokens: list[str] = []

    for value in iterator:
        if not isinstance(value, str):
            raise TypeError(
                f"{label} must contain only strings, "
                f"got {type(value).__name__}: {value!r}"
            )

        value = value.strip()

        if value:
            tokens.append(value)

    return tokens


@dataclass(frozen=True, slots=True)
class _NameFilter:
    """
    Compiled, case-insensitive file-name filter.

    A name passes if it ends with ANY extension or matches ANY pattern.
    Extensions are matched with ``endswith`` so multi-part extensions such
    as ".tar.gz" work; a name must be longer than the extension itself, so
    a file literally called ".mp4" is not treated as an "mp4" file.
    """

    extensions: frozenset[str]
    patterns: tuple[re.Pattern[str], ...]

    def matches(self, filename: str) -> bool:
        folded = filename.casefold()

        for extension in self.extensions:
            if len(folded) > len(extension) and folded.endswith(extension):
                return True

        return any(pattern.match(folded) for pattern in self.patterns)


def _build_name_filter(
    filters: Iterable[str] | None,
    extensions: Iterable[str] | None,
) -> _NameFilter | None:
    """
    Merge ``filters`` and ``extensions`` into one compiled name filter.

    Args:
        filters:
            General filter tokens.  Glob tokens become patterns; everything
            else is treated as an extension.

        extensions:
            Explicit extension tokens (merged with extension-like filters).

    Returns:
        A :class:`_NameFilter`, or None when no effective filter remains
        (so the caller can skip filtering entirely).

    Raises:
        TypeError:
            If either argument contains non-string items.
    """

    filter_tokens = _coerce_tokens(filters, "filters")
    extension_tokens = _coerce_tokens(extensions, "extensions")

    pattern_sources: set[str] = set()

    for token in filter_tokens:
        if _GLOB_CHARS.intersection(token):
            pattern_sources.add(token.casefold())
        else:
            extension_tokens.append(token)

    extension_set = _normalize_extensions(extension_tokens) or set()

    if not extension_set and not pattern_sources:
        return None

    patterns = tuple(
        re.compile(fnmatch.translate(source))
        for source in sorted(pattern_sources)
    )

    name_filter = _NameFilter(
        extensions=frozenset(extension_set),
        patterns=patterns,
    )

    logger.debug(
        "Name filter: extensions=%s patterns=%s",
        sorted(name_filter.extensions),
        sorted(pattern_sources),
    )

    return name_filter


def _to_datetime(timestamp: float) -> datetime:
    """
    Convert a filesystem timestamp into a datetime object.
    """

    return datetime.fromtimestamp(timestamp).astimezone()


def _make_file_entry(
    entry: os.DirEntry[str],
    entry_stat: os.stat_result,
    hidden: bool,
) -> FileEntry:
    """
    Build a FileEntry from a scandir entry and its (already fetched) stat.
    """

    entry_name = entry.name
    name_path = Path(entry_name)

    # Creation time is not consistently supported across operating systems.
    creation_timestamp = getattr(
        entry_stat,
        "st_birthtime",
        entry_stat.st_ctime,
    )

    return FileEntry(
        path=Path(entry.path).absolute(),
        name=entry_name,
        stem=name_path.stem,
        suffix=name_path.suffix,
        size=entry_stat.st_size,
        mtime=_to_datetime(entry_stat.st_mtime),
        ctime=_to_datetime(creation_timestamp),
        is_hidden=hidden,
    )


# ============================================================
# ROOT DIRECTORY VALIDATION
# ============================================================

def validate_root(root: Path | str) -> Path:
    """
    Validate and resolve the root directory.

    Args:
        root:
            Directory path as a pathlib.Path or string.

    Returns:
        Validated absolute pathlib.Path.

    Raises:
        InvalidDirectoryError:
            If the path does not exist or is not a directory.

        DirectoryAccessError:
            If the directory cannot be read or written.

    Notes:
        Permission checks use os.access() and are supplemented
        with directory enumeration and a write-access check.

        No files are created or modified during validation.
    """

    try:
        root_path = Path(root).expanduser().resolve(strict=True)

    except (OSError, RuntimeError, ValueError) as exc:
        raise InvalidDirectoryError(
            f"Directory does not exist or cannot be resolved: {root}"
        ) from exc

    if not root_path.exists() or not root_path.is_dir():
        raise InvalidDirectoryError(
            f"Path is not a valid directory: {root_path}"
        )

    # Check read and write access.
    if not os.access(root_path, os.R_OK | os.W_OK):
        raise DirectoryAccessError(
            f"Directory lacks read/write permissions: {root_path}"
        )

    # Verify that directory enumeration is actually possible.
    try:
        with os.scandir(root_path):
            pass

    except PermissionError as exc:
        raise DirectoryAccessError(
            f"Cannot read directory: {root_path}"
        ) from exc

    except OSError as exc:
        raise DirectoryAccessError(
            f"Cannot access directory: {root_path}"
        ) from exc

    return root_path


# ============================================================
# DIRECTORY SCANNING
# ============================================================

def scan_directory(
    root: Path | str,
    filters: list[str] | set[str] | None = None,
    *,
    recursive: bool = False,
    include_hidden: bool = False,
    extensions: set[str] | None = None,
) -> list[FileEntry]:
    """
    Scan a directory and collect file metadata.

    Both call styles are supported::

        scan_directory(root, [".mp4", "clip_*"])           # engine facade
        scan_directory(root, filters=[".mp4"], recursive=True)
        scan_directory(root, extensions={".mp4", ".mov"})  # original style

    Args:
        root:
            Root directory to scan.

        filters:
            Optional list/set of filter tokens.  Tokens containing a glob
            character (``*``, ``?``, ``[``) are filename patterns; all other
            tokens are extensions (leading dot optional, e.g. ``"mp4"``).
            Merged with ``extensions``; a file is kept if it matches ANY
            extension or pattern.  None or empty means no filtering.

        recursive:
            If True, scan subdirectories recursively.

        include_hidden:
            If True, include hidden files and directories.
            The internal .vaultflow directory is ALWAYS excluded.

        extensions:
            Optional set of file extensions to include.

            Example:
                {'.mp4', '.mov', '.pdf'}

            Matching is case-insensitive.

    Returns:
        A deterministically sorted list of FileEntry objects.

    Metadata collected:
        - Absolute path.
        - Filename.
        - Filename stem.
        - File extension.
        - File size in bytes.
        - Modification time.
        - Creation time (best effort).
        - Hidden status.

    Behavior:
        - Symbolic links are skipped and logged.
        - Directories are not returned as FileEntry objects.
        - Permission errors on individual entries are logged.
        - Directory traversal continues after recoverable errors.
        - The .vaultflow directory is always excluded.

    Raises:
        TypeError:
            If ``filters`` or ``extensions`` contain non-string items.

        InvalidDirectoryError:
            If the root directory is invalid.

        DirectoryAccessError:
            If the root directory cannot be accessed.
    """

    # Fail fast on bad filters before touching the filesystem.
    name_filter = _build_name_filter(filters, extensions)

    root_path = validate_root(root)

    discovered_files: list[FileEntry] = []

    skipped: Counter[str] = Counter()

    # Stack-based traversal avoids Python recursion depth limits.
    directories_to_scan: list[Path] = [root_path]

    while directories_to_scan:

        current_directory = directories_to_scan.pop()

        try:
            with os.scandir(current_directory) as entries:

                for entry in entries:

                    try:
                        entry_name = entry.name

                        # ------------------------------------------------
                        # NEVER SCAN VAULTFLOW INTERNAL STORAGE
                        # ------------------------------------------------

                        if entry_name.casefold() == VAULTFLOW_INTERNAL_DIR.casefold():
                            skipped["internal"] += 1
                            continue

                        # ------------------------------------------------
                        # SYMBOLIC LINK FILTERING
                        # ------------------------------------------------

                        if entry.is_symlink():
                            logger.warning(
                                "Skipping symbolic link: %s",
                                entry.path,
                            )
                            skipped["symlink"] += 1
                            continue

                        # ------------------------------------------------
                        # ENTRY METADATA
                        # ------------------------------------------------

                        entry_stat = entry.stat(follow_symlinks=False)

                        hidden = _is_hidden(entry_name, entry_stat)

                        # ------------------------------------------------
                        # HIDDEN FILE/DIRECTORY FILTERING
                        # ------------------------------------------------

                        if hidden and not include_hidden:
                            skipped["hidden"] += 1
                            continue

                        # ------------------------------------------------
                        # DIRECTORY HANDLING
                        # ------------------------------------------------

                        if stat.S_ISDIR(entry_stat.st_mode):

                            if recursive:
                                directories_to_scan.append(
                                    Path(entry.path)
                                )

                            continue

                        # ------------------------------------------------
                        # REGULAR FILE FILTERING
                        # ------------------------------------------------

                        if not stat.S_ISREG(entry_stat.st_mode):
                            skipped["non_regular"] += 1
                            continue

                        if name_filter is not None and not name_filter.matches(
                            entry_name
                        ):
                            skipped["filtered"] += 1
                            continue

                        # ------------------------------------------------
                        # FILE ENTRY CONSTRUCTION
                        # ------------------------------------------------

                        discovered_files.append(
                            _make_file_entry(entry, entry_stat, hidden)
                        )

                    except PermissionError:
                        logger.warning(
                            "Permission denied while accessing entry: %s",
                            entry.path,
                        )
                        skipped["error"] += 1
                        continue

                    except OSError as exc:
                        logger.warning(
                            "Unable to process entry %s: %s",
                            entry.path,
                            exc,
                        )
                        skipped["error"] += 1
                        continue

        except PermissionError:
            logger.warning(
                "Permission denied while scanning directory: %s",
                current_directory,
            )
            skipped["error"] += 1
            continue

        except OSError as exc:
            logger.warning(
                "Unable to scan directory %s: %s",
                current_directory,
                exc,
            )
            skipped["error"] += 1
            continue

    # ------------------------------------------------------------
    # DETERMINISTIC NATURAL SORTING
    # ------------------------------------------------------------

    discovered_files.sort(
        key=lambda file: (
            _natural_sort_key(file.name),
            str(file.path).casefold(),
            str(file.path),
        )
    )

    logger.debug(
        "Scan of %s found %d file(s); skipped: %s",
        root_path,
        len(discovered_files),
        dict(skipped) or "nothing",
    )

    return discovered_files


# ============================================================
# COMMAND-LINE TEST
# ============================================================

def _run_self_test() -> None:
    """
    Build a throwaway directory tree and verify scanner behavior.

    Covers: engine-style positional calls, keyword calls, extension and
    pattern filters, multi-part extensions, hidden handling, strict
    .vaultflow exclusion, recursion, natural sorting, symlink skipping and
    argument validation.  Raises AssertionError on the first failure.
    """

    import tempfile

    def names(results: list[FileEntry]) -> list[str]:
        return [file.name for file in results]

    with tempfile.TemporaryDirectory(prefix="vaultflow_scanner_") as tmp:

        root = Path(tmp)

        for name in (
            "clip10.mp4", "clip2.MP4", "clip1.mp4", "notes.txt",
            "photo.JPG", "archive.tar.gz", "report_final.pdf", ".hidden.mp4",
        ):
            (root / name).write_text("x")

        (root / ".vaultflow").mkdir()
        (root / ".vaultflow" / "journal.jsonl").write_text("{}")
        (root / "sub").mkdir()
        (root / "sub" / "deep.mp4").write_text("x")
        (root / "sub" / "deep2.txt").write_text("x")
        (root / "sub" / ".vaultflow").mkdir()
        (root / "sub" / ".vaultflow" / "stray.mp4").write_text("x")

        symlink_made = False
        try:
            os.symlink(root / "notes.txt", root / "link.txt")
            symlink_made = True
        except (OSError, NotImplementedError):
            print("  (symlink creation unavailable; skipping symlink check)")

        everything = [
            "archive.tar.gz", "clip1.mp4", "clip2.MP4", "clip10.mp4",
            "notes.txt", "photo.JPG", "report_final.pdf",
        ]
        mp4s = ["clip1.mp4", "clip2.MP4", "clip10.mp4"]

        # 1. Default scan: natural order, hidden/.vaultflow/symlink/subdir excluded.
        assert names(scan_directory(root)) == everything, names(scan_directory(root))
        print("  [ok] default scan, natural sort, exclusions")

        # 2. Engine-style positional filters (the original collision).
        assert names(scan_directory(root, [".mp4"])) == mp4s
        assert names(scan_directory(str(root), {"mp4"})) == mp4s
        print("  [ok] positional filters (list, set, str root)")

        # 3. Keyword forms and the legacy extensions argument.
        assert names(scan_directory(root, filters=[".MP4"])) == mp4s
        assert names(scan_directory(root, extensions={".mp4"})) == mp4s
        print("  [ok] keyword filters / extensions")

        # 4. Multi-part extensions.
        assert names(scan_directory(root, [".tar.gz"])) == ["archive.tar.gz"]
        assert names(scan_directory(root, ["gz"])) == ["archive.tar.gz"]
        print("  [ok] multi-part extensions")

        # 5. Patterns (case-insensitive) and mixed extension + pattern.
        assert names(scan_directory(root, ["report_*"])) == ["report_final.pdf"]
        assert names(scan_directory(root, ["*FINAL*"])) == ["report_final.pdf"]
        assert names(scan_directory(root, [".txt", "report_*"])) == [
            "notes.txt", "report_final.pdf",
        ]
        print("  [ok] pattern and mixed filters")

        # 6. filters merged with extensions (OR semantics).
        merged = scan_directory(root, ["*final*"], extensions={".txt"})
        assert names(merged) == ["notes.txt", "report_final.pdf"]
        print("  [ok] filters + extensions merged")

        # 7. Empty / blank filters mean "no filtering".
        assert names(scan_directory(root, [])) == everything
        assert names(scan_directory(root, ["", "  "])) == everything
        assert names(scan_directory(root, None)) == everything
        print("  [ok] empty filters disable filtering")

        # 8. include_hidden never exposes .vaultflow.
        hidden_run = scan_directory(root, include_hidden=True, recursive=True)
        assert ".hidden.mp4" in names(hidden_run)
        assert all(VAULTFLOW_INTERNAL_DIR not in f.path.parts for f in hidden_run)
        assert "journal.jsonl" not in names(hidden_run)
        assert "stray.mp4" not in names(hidden_run)
        print("  [ok] hidden handling + strict .vaultflow exclusion")

        # 9. Recursion.
        recursive_run = names(scan_directory(root, [".mp4", ".txt"], recursive=True))
        assert "deep.mp4" in recursive_run and "deep2.txt" in recursive_run
        assert "stray.mp4" not in recursive_run
        print("  [ok] recursive scan")

        # 10. Symlinks are skipped.
        if symlink_made:
            assert "link.txt" not in names(scan_directory(root))
            print("  [ok] symlink skipped")

        # 11. Validation.
        for bad in ([1], [".mp4", None], 5):
            try:
                scan_directory(root, bad)  # type: ignore[arg-type]
            except TypeError:
                continue
            raise AssertionError(f"TypeError expected for filters={bad!r}")

        try:
            scan_directory(root / "does_not_exist")
        except InvalidDirectoryError:
            pass
        else:
            raise AssertionError("InvalidDirectoryError expected for missing root")
        print("  [ok] argument and root validation")

        # 12. Natural-sort robustness with superscript digits.
        _natural_sort_key("a1\u00b2")
        print("  [ok] natural sort key handles non-decimal digits")


def _main(argv: list[str] | None = None) -> int:
    """
    Command-line entry point.  Returns a process exit code.
    """

    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="VaultFlow directory scanner test."
    )

    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory).",
    )

    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Include subdirectories.",
    )

    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include hidden files and directories.",
    )

    parser.add_argument(
        "--extensions",
        nargs="*",
        default=None,
        help="Optional extensions, e.g. .mp4 .mov .pdf.",
    )

    parser.add_argument(
        "--filters",
        nargs="*",
        default=None,
        help="Extensions and/or glob patterns, e.g. .mp4 'clip_*'.",
    )

    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the built-in self-test in a temporary directory and exit.",
    )

    args = parser.parse_args(argv)

    if args.self_test:
        print("Running VaultFlow scanner self-test...")
        try:
            _run_self_test()
        except AssertionError as exc:
            logger.error("Self-test FAILED: %s", exc)
            return 1
        print("Self-test passed.")
        return 0

    try:

        results = scan_directory(
            args.directory,
            args.filters,
            recursive=args.recursive,
            include_hidden=args.include_hidden,
            extensions=(
                set(args.extensions)
                if args.extensions is not None
                else None
            ),
        )

        print("\n" + "=" * 75)
        print("VAULTFLOW DIRECTORY SCAN RESULTS")
        print("=" * 75)

        print(f"Root directory : {Path(args.directory).expanduser().resolve()}")
        print(f"Files found    : {len(results)}")
        print("-" * 75)

        for index, file in enumerate(results, start=1):

            print(f"\n[{index}] {file.name}")
            print(f"    Path       : {file.path}")
            print(f"    Stem       : {file.stem}")
            print(f"    Extension  : {file.suffix}")
            print(f"    Size       : {file.size:,} bytes")
            print(f"    Modified   : {file.mtime}")
            print(f"    Created    : {file.ctime}")
            print(f"    Hidden     : {file.is_hidden}")

        print("\n" + "=" * 75)
        print("SCAN COMPLETED")
        print("=" * 75 + "\n")

    except InvalidDirectoryError as exc:

        logger.error("Invalid directory: %s", exc)
        return 1

    except DirectoryAccessError as exc:

        logger.error("Directory access error: %s", exc)
        return 1

    except TypeError as exc:

        logger.error("Invalid filter: %s", exc)
        return 1

    return 0


if __name__ == "__main__":

    raise SystemExit(_main())
