
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
    - Automatic symbolic link exclusion.
    - Extension filtering (case-insensitive).
    - File metadata collection.
    - Deterministic natural filename sorting.
    - Per-entry permission error handling.

Compatibility:
    Python 3.11+

Dependencies:
    Standard library only.

Project dependencies:
    vaultflow.models
    vaultflow.errors
"""

from __future__ import annotations

import logging
import os
import re
import stat

from pathlib import Path
from datetime import datetime
from typing import Iterator

from vaultflow.models import FileEntry
from vaultflow.errors import (
    InvalidDirectoryError,
    DirectoryAccessError,
)


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


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def _natural_sort_key(filename: str) -> tuple:
    """
    Generate a natural, case-insensitive sorting key.

    Examples:
        file1.mp4
        file2.mp4
        file10.mp4

    This ensures numeric portions are compared numerically
    rather than alphabetically.
    """

    parts = re.split(r"(\d+)", filename.casefold())

    return tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in parts
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
    extensions: set[str] | None,
) -> set[str] | None:
    """
    Normalize extension filters.

    Example:
        {'.MP4', '.MOV'} -> {'.mp4', '.mov'}

    Extensions without a leading dot are also accepted.
    """

    if extensions is None:
        return None

    normalized = set()

    for extension in extensions:
        extension = extension.strip().casefold()

        if not extension:
            continue

        if not extension.startswith("."):
            extension = f".{extension}"

        normalized.add(extension)

    return normalized


def _matches_extension(
    filename: str,
    extensions: set[str] | None,
) -> bool:
    """
    Check whether a filename matches the extension filter.
    """

    if extensions is None:
        return True

    return Path(filename).suffix.casefold() in extensions


def _to_datetime(timestamp: float) -> datetime:
    """
    Convert a filesystem timestamp into a datetime object.
    """

    return datetime.fromtimestamp(timestamp).astimezone()


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
    *,
    recursive: bool = False,
    include_hidden: bool = False,
    extensions: set[str] | None = None,
) -> list[FileEntry]:
    """
    Scan a directory and collect file metadata.

    Args:
        root:
            Root directory to scan.

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
        InvalidDirectoryError:
            If the root directory is invalid.

        DirectoryAccessError:
            If the root directory cannot be accessed.
    """

    root_path = validate_root(root)

    extension_filter = _normalize_extensions(extensions)

    discovered_files: list[FileEntry] = []

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
                            continue

                        # ------------------------------------------------
                        # SYMBOLIC LINK FILTERING
                        # ------------------------------------------------

                        if entry.is_symlink():
                            logger.warning(
                                "Skipping symbolic link: %s",
                                entry.path,
                            )
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
                            continue

                        if not _matches_extension(
                            entry_name,
                            extension_filter,
                        ):
                            continue

                        # ------------------------------------------------
                        # FILE METADATA
                        # ------------------------------------------------

                        absolute_path = Path(entry.path).absolute()

                        size_bytes = entry_stat.st_size

                        modified_time = _to_datetime(
                            entry_stat.st_mtime
                        )

                        # Creation time is not consistently supported
                        # across operating systems.
                        creation_timestamp = getattr(
                            entry_stat,
                            "st_birthtime",
                            entry_stat.st_ctime,
                        )

                        creation_time = _to_datetime(
                            creation_timestamp
                        )

                        # ------------------------------------------------
                        # FILE ENTRY CONSTRUCTION
                        # ------------------------------------------------

                        file_entry = FileEntry(
                            path=absolute_path,
                            name=entry_name,
                            stem=Path(entry_name).stem,
                            suffix=Path(entry_name).suffix,
                            size=size_bytes,
                            mtime=modified_time,
                            ctime=creation_time,
                            is_hidden=hidden,
                        )

                        discovered_files.append(file_entry)

                    except PermissionError:
                        logger.warning(
                            "Permission denied while accessing entry: %s",
                            entry.path,
                        )
                        continue

                    except OSError as exc:
                        logger.warning(
                            "Unable to process entry %s: %s",
                            entry.path,
                            exc,
                        )
                        continue

        except PermissionError:
            logger.warning(
                "Permission denied while scanning directory: %s",
                current_directory,
            )
            continue

        except OSError as exc:
            logger.warning(
                "Unable to scan directory %s: %s",
                current_directory,
                exc,
            )
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

    return discovered_files


# ============================================================
# COMMAND-LINE TEST
# ============================================================

if __name__ == "__main__":

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

    args = parser.parse_args()

    try:

        results = scan_directory(
            args.directory,
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

    except DirectoryAccessError as exc:

        logger.error("Directory access error: %s", exc)
