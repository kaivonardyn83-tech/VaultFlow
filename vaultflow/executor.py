
"""
vaultflow/executor.py

Sequential rename-plan execution with:
    - Root-directory path validation.
    - Target-directory creation.
    - Dry-run support.
    - Operation status tracking.
    - Safe reverse-order partial rollback.
    - Case-only renames (Lecture.mp4 -> lecture.mp4) on case-insensitive
      filesystems such as Windows and default macOS.
    - Automatic JSONL transaction journaling.

Python: 3.11+
Dependencies: Standard library and VaultFlow modules.
"""

from __future__ import annotations

import errno
import os
import uuid

from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

from vaultflow import errors as _errors
from vaultflow.models import RenamePlan, RenameOp
from vaultflow.journal import write_journal_entry


def _project_exception(
    names: tuple[str, ...],
    fallback: type[Exception],
    message: str,
) -> Exception:
    """
    Resolve an existing VaultFlow exception by name.

    The candidate names allow compatibility with different
    exception naming conventions in vaultflow.errors.
    """
    for name in names:
        exception_class = getattr(_errors, name, None)

        if (
            isinstance(exception_class, type)
            and issubclass(exception_class, Exception)
        ):
            return exception_class(message)

    return fallback(message)


def _validation_error(message: str) -> Exception:
    """Create the project's preferred validation exception."""
    return _project_exception(
        (
            "PlanValidationError",
            "InvalidPlanError",
            "ValidationError",
            "VaultFlowError",
        ),
        ValueError,
        message,
    )


def _execution_error(message: str) -> Exception:
    """Create the project's preferred execution exception."""
    return _project_exception(
        (
            "ExecutionError",
            "VaultFlowExecutionError",
            "VaultFlowError",
        ),
        RuntimeError,
        message,
    )


def _get_attribute(
    obj: Any,
    names: tuple[str, ...],
    default: Any = None,
) -> Any:
    """Read the first available attribute or mapping value."""
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)

    return default


def _set_attribute(obj: Any, name: str, value: Any) -> None:
    """Set a value on an operation object or mutable mapping."""
    if isinstance(obj, MutableMapping):
        obj[name] = value
    else:
        setattr(obj, name, value)


def _set_status(op: RenameOp, status: str) -> None:
    """Update an operation's execution status."""
    try:
        _set_attribute(op, "status", status)
    except (AttributeError, TypeError) as exc:
        raise _execution_error(
            "RenameOp must have a mutable status field."
        ) from exc


def _set_error(op: RenameOp, message: str) -> None:
    """
    Attach an error message when the operation model supports it.

    The status field remains the required execution indicator.
    """
    try:
        _set_attribute(op, "error", message)
    except (AttributeError, TypeError):
        pass


def _path_exists(path: Path) -> bool:
    """Return True for normal files, directories, and symbolic links."""
    return path.exists() or path.is_symlink()


def _file_identity(path: Path) -> tuple[int, int]:
    """
    Return the device and inode identity of a filesystem entry.

    lstat() is intentional: it identifies a symbolic link itself
    rather than following the link to its destination.
    """
    entry_stat = os.lstat(path)

    return (
        entry_stat.st_dev,
        entry_stat.st_ino,
    )


def is_case_only_rename(source: Path, target: Path) -> bool:
    """
    True when ``target`` is the very same directory entry as ``source``,
    spelled with different letter case.

    On a case-insensitive filesystem (Windows, default macOS) both spellings
    resolve to one file, so a naive "target already exists" check would reject
    a legitimate case change.  On a case-sensitive filesystem the two
    spellings can only coexist as two separate entries, which is a genuine
    collision and is therefore NOT reported as a case-only rename.
    """
    if os.fspath(source) == os.fspath(target):
        return False

    if (
        source.parent != target.parent
        or source.name.casefold() != target.name.casefold()
    ):
        return False

    try:
        if not os.path.samestat(os.lstat(source), os.lstat(target)):
            return False

        siblings = set(os.listdir(source.parent))

    except OSError:
        return False

    # Both spellings listed -> two distinct entries -> real collision.
    return not (source.name in siblings and target.name in siblings)


def rename_entry(source: Path, target: Path, case_only: bool = False) -> None:
    """
    Rename ``source`` to ``target``.

    Case-only renames go through a temporary name in the same directory,
    which works on every filesystem.  If the second step fails the original
    name is restored and the error is re-raised.
    """
    if not case_only:
        source.rename(target)
        return

    temporary = source.with_name(f".vaultflow-case-{uuid.uuid4().hex}.tmp")

    source.rename(temporary)

    try:
        temporary.rename(target)

    except OSError:
        try:
            temporary.rename(source)
        except OSError:
            pass

        raise


def _is_inside(path: Path, parent: Path) -> bool:
    """Check whether a path is lexically contained in a directory."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_parent_inside_root(
    path: Path,
    root_real: Path,
    description: str,
) -> None:
    """
    Check that an existing parent path resolves inside the root.

    This also detects existing symlinked parent directories that
    would otherwise redirect a rename outside the selected root.
    """
    parent_real = path.parent.resolve(strict=False)

    if not _is_inside(parent_real, root_real):
        raise _validation_error(
            f"{description} escapes the VaultFlow root: {path}"
        )


def _normalise_path(
    value: Any,
    root_lexical: Path,
    root_real: Path,
    description: str,
) -> Path:
    """Convert an operation path into a validated absolute path."""
    if value is None:
        raise _validation_error(
            f"Missing {description} path."
        )

    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise _validation_error(
            f"Invalid {description} path: {value!r}"
        ) from exc

    if not path.is_absolute():
        path = root_lexical / path

    # Normalise . and .. without dereferencing the final entry.
    path = Path(os.path.abspath(os.fspath(path)))

    # Accept paths expressed through either the supplied root path
    # or its resolved canonical path.
    if not (
        _is_inside(path, root_lexical)
        or _is_inside(path, root_real)
    ):
        raise _validation_error(
            f"{description} is outside the VaultFlow root: {path}"
        )

    if path == root_lexical or path == root_real:
        raise _validation_error(
            f"The VaultFlow root itself cannot be a {description}."
        )

    _assert_parent_inside_root(
        path,
        root_real,
        description,
    )

    return path


def _get_plan_operations(plan: RenamePlan) -> list[RenameOp]:
    """Extract the operation list from a rename plan."""
    operations = _get_attribute(
        plan,
        ("operations", "ops"),
    )

    if (
        operations is None
        or isinstance(operations, (str, bytes))
        or not isinstance(operations, Sequence)
    ):
        raise _validation_error(
            "RenamePlan must provide an operations sequence."
        )

    return list(operations)


def _get_plan_id(plan: RenamePlan) -> str:
    """Extract the plan identifier."""
    plan_id = _get_attribute(
        plan,
        ("plan_id", "id"),
    )

    if plan_id is None or not str(plan_id).strip():
        raise _validation_error(
            "RenamePlan must provide a non-empty plan_id."
        )

    return str(plan_id)


def _prepare_operations(
    operations: list[RenameOp],
    root_lexical: Path,
    root_real: Path,
) -> list[tuple[RenameOp, Path, Path]]:
    """
    Validate every operation before changing the filesystem.

    Duplicate sources and destinations are rejected to prevent
    ambiguous sequential rename plans.
    """
    prepared: list[tuple[RenameOp, Path, Path]] = []

    seen_sources: set[str] = set()
    seen_targets: set[str] = set()

    for index, op in enumerate(operations):
        source_value = _get_attribute(
            op,
            ("source", "source_path", "src"),
        )

        target_value = _get_attribute(
            op,
            ("target", "target_path", "dst"),
        )

        source = _normalise_path(
            source_value,
            root_lexical,
            root_real,
            f"operation {index} source",
        )

        target = _normalise_path(
            target_value,
            root_lexical,
            root_real,
            f"operation {index} target",
        )

        # Exact spelling: "A.txt" -> "a.txt" is a valid case-only rename.
        if os.fspath(source) == os.fspath(target):
            raise _validation_error(
                f"Operation {index} has identical source and target paths."
            )

        source_key = os.path.normcase(str(source))
        target_key = os.path.normcase(str(target))

        if source_key in seen_sources:
            raise _validation_error(
                f"Duplicate source in rename plan: {source}"
            )

        if target_key in seen_targets:
            raise _validation_error(
                f"Duplicate target in rename plan: {target}"
            )

        if not _path_exists(source):
            raise _validation_error(
                f"Source does not exist: {source}"
            )

        if _path_exists(target) and not is_case_only_rename(source, target):
            raise _validation_error(
                f"Target already exists: {target}"
            )

        _assert_parent_inside_root(
            source,
            root_real,
            "Source",
        )

        _assert_parent_inside_root(
            target,
            root_real,
            "Target",
        )

        # Confirm that the status field can be updated before
        # any filesystem operation begins.
        current_status = _get_attribute(
            op,
            ("status",),
            "PENDING",
        )

        _set_status(op, str(current_status))

        seen_sources.add(source_key)
        seen_targets.add(target_key)

        prepared.append((op, source, target))

    return prepared


def _rollback_completed(
    completed: list[tuple[RenameOp, Path, Path, tuple[int, int]]],
    root_real: Path,
) -> None:
    """
    Reverse successfully executed operations in reverse order.

    A rollback is attempted only if:
        - The original source path is currently unoccupied.
        - The target still exists.
        - The target has the original device/inode identity.
    """
    for op, source, target, original_identity in reversed(completed):
        # Already rolled back or otherwise resolved.
        current_status = _get_attribute(op, ("status",), "")

        if current_status != "COMPLETED":
            continue

        try:
            _assert_parent_inside_root(
                target,
                root_real,
                "Rollback target",
            )

            source.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            _assert_parent_inside_root(
                source,
                root_real,
                "Rollback source",
            )

            case_only = is_case_only_rename(target, source)

            if _path_exists(source) and not case_only:
                raise FileExistsError(
                    errno.EEXIST,
                    "Original source path is occupied.",
                    str(source),
                )

            if not _path_exists(target):
                raise FileNotFoundError(
                    errno.ENOENT,
                    "Renamed target no longer exists.",
                    str(target),
                )

            current_identity = _file_identity(target)

            if current_identity != original_identity:
                raise OSError(
                    errno.ESTALE,
                    "Target identity changed; refusing unsafe rollback.",
                    str(target),
                )

            rename_entry(target, source, case_only)

            _set_status(op, "ROLLED_BACK")
            _set_error(op, None)

        except (OSError, ValueError) as exc:
            _set_status(op, "ROLLBACK_FAILED")
            _set_error(
                op,
                f"Rollback failed: {exc}",
            )


def execute_plan(
    plan: RenamePlan,
    root_dir: Path,
    dry_run: bool = False,
) -> list[RenameOp]:
    """
    Execute a sequential VaultFlow rename plan.

    Args:
        plan: RenamePlan containing the operations to execute.
        root_dir: Root directory managed by VaultFlow.
        dry_run: Validate and journal operations without renaming.

    Returns:
        The original list of RenameOp objects with updated statuses.

    Status values used:
        RUNNING
        COMPLETED
        FAILED
        ROLLED_BACK
        ROLLBACK_FAILED
        SKIPPED
        DRY_RUN

    Failure behaviour:
        An OSError during an operation marks that operation FAILED.
        Previously completed operations are rolled back in reverse
        order. Operations not reached are marked SKIPPED.

    Important:
        The journal is written after the execution batch. If journal
        writing fails after successful renames, the executor attempts
        to reverse those renames before raising an exception.
    """
    root_input = Path(root_dir).expanduser()

    root_lexical = Path(
        os.path.abspath(os.fspath(root_input))
    )

    if not root_lexical.is_dir():
        raise _validation_error(
            f"VaultFlow root does not exist or is not a directory: "
            f"{root_lexical}"
        )

    root_real = root_lexical.resolve(strict=True)
    plan_id = _get_plan_id(plan)
    operations = _get_plan_operations(plan)

    prepared = _prepare_operations(
        operations,
        root_lexical,
        root_real,
    )

    # Dry runs do not create directories or modify source files.
    if dry_run:
        for op, _, _ in prepared:
            _set_status(op, "DRY_RUN")
            _set_error(op, None)

        try:
            write_journal_entry(
                root_lexical,
                plan_id,
                operations,
            )
        except (OSError, ValueError, TypeError) as exc:
            raise _execution_error(
                f"Could not journal dry-run plan {plan_id}: {exc}"
            ) from exc

        return operations

    completed: list[
        tuple[RenameOp, Path, Path, tuple[int, int]]
    ] = []

    # Execute operations sequentially.
    for index, (op, source, target) in enumerate(prepared):
        try:
            _set_status(op, "RUNNING")
            _set_error(op, None)

            # Recheck the source immediately before renaming.
            if not _path_exists(source):
                raise FileNotFoundError(
                    errno.ENOENT,
                    "Source disappeared before execution.",
                    str(source),
                )

            # Create the destination directory as required.
            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            # Recheck directory containment and destination collision.
            _assert_parent_inside_root(
                target,
                root_real,
                "Target",
            )

            case_only = is_case_only_rename(source, target)

            if _path_exists(target) and not case_only:
                raise FileExistsError(
                    errno.EEXIST,
                    "Target appeared before execution.",
                    str(target),
                )

            # Capture the original entry identity for safe rollback.
            original_identity = _file_identity(source)

            # Perform the actual filesystem rename.
            rename_entry(source, target, case_only)

            completed.append((
                op,
                source,
                target,
                original_identity,
            ))

            _set_status(op, "COMPLETED")

        except OSError as exc:
            # Mark the operation that caused the failure.
            _set_status(op, "FAILED")
            _set_error(
                op,
                f"{type(exc).__name__}: {exc}",
            )

            # Reverse all previously completed operations.
            _rollback_completed(
                completed,
                root_real,
            )

            # Mark operations that were never attempted.
            for skipped_op, _, _ in prepared[index + 1:]:
                _set_status(skipped_op, "SKIPPED")

            break

    # Record the final state of the batch, including any failures
    # and rollback results.
    try:
        write_journal_entry(
            root_lexical,
            plan_id,
            operations,
        )

    except (OSError, ValueError, TypeError) as exc:
        # If the journal cannot be written after successful execution,
        # attempt to restore the filesystem to its previous state.
        _rollback_completed(
            completed,
            root_real,
        )

        # Make one additional attempt to journal the resulting states.
        try:
            write_journal_entry(
                root_lexical,
                plan_id,
                operations,
            )
        except (OSError, ValueError, TypeError):
            pass

        raise _execution_error(
            f"Journal writing failed for plan {plan_id}. "
            f"Completed operations were rolled back where possible. "
            f"Journal error: {exc}"
        ) from exc

    return operations


if __name__ == "__main__":
    """Lightweight command-line executor self-test."""

    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    with TemporaryDirectory(prefix="vaultflow-executor-test-") as temp:
        test_root = Path(temp)

        source = test_root / "original.txt"
        target = test_root / "renamed.txt"

        source.write_text(
            "VaultFlow executor self-test.",
            encoding="utf-8",
        )

        sample_op = SimpleNamespace(
            source=source,
            target=target,
            status="PENDING",
            error=None,
        )

        sample_plan = SimpleNamespace(
            plan_id="executor-self-test",
            operations=[sample_op],
        )

        results = execute_plan(
            sample_plan,
            test_root,
        )

        assert source.exists() is False
        assert target.exists() is True
        assert results[0].status == "COMPLETED"

        print("Executor self-test passed.")
        print(f"Operation status: {results[0].status}")
