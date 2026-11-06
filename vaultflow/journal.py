
"""
vaultflow/journal.py

Local JSONL transaction journal for VaultFlow.

Journal files are stored in:
    <root_dir>/.vaultflow/journal/

Each journal file contains:
    1. A transaction header.
    2. One record per rename operation.
    3. A transaction completion record.

Python: 3.11+
Dependencies: Standard library and vaultflow.models
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import uuid

from collections.abc import Mapping
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vaultflow.models import RenameOp


JOURNAL_DIRECTORY = ".vaultflow"
JOURNAL_SUBDIRECTORY = "journal"
JOURNAL_FORMAT_VERSION = 1


def _json_safe(value: Any) -> Any:
    """
    Convert common Python objects into JSON-compatible values.

    Paths become strings, enums become their underlying values,
    and dataclasses become dictionaries.
    """
    if isinstance(value, Path):
        return str(value)

    if isinstance(value, Enum):
        return _json_safe(value.value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_safe(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }

    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    # Support ordinary objects with attributes.
    if hasattr(value, "__dict__"):
        return _json_safe(vars(value))

    # Keep unexpected model values auditable instead of failing
    # serialization entirely.
    return repr(value)


def _get_attribute(
    obj: Any,
    names: tuple[str, ...],
    default: Any = None,
) -> Any:
    """Return the first available attribute or mapping value."""
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)

    return default


def _operation_snapshot(op: RenameOp) -> dict[str, Any]:
    """Create a serializable snapshot of a rename operation."""
    if dataclasses.is_dataclass(op) and not isinstance(op, type):
        details = {
            field.name: getattr(op, field.name)
            for field in dataclasses.fields(op)
        }
    elif isinstance(op, Mapping):
        details = dict(op)
    elif hasattr(op, "__dict__"):
        details = vars(op).copy()
    else:
        details = {}

    source = _get_attribute(
        op,
        ("source", "source_path", "src"),
    )
    target = _get_attribute(
        op,
        ("target", "target_path", "dst"),
    )
    status = _get_attribute(op, ("status",), "UNKNOWN")

    return {
        "source": _json_safe(source),
        "target": _json_safe(target),
        "status": _json_safe(status),
        "operation": _json_safe(details),
    }


def _validate_root(root_dir: Path) -> Path:
    """Resolve and validate the VaultFlow root directory."""
    root = Path(root_dir).expanduser().resolve(strict=True)

    if not root.is_dir():
        raise NotADirectoryError(
            f"VaultFlow root is not a directory: {root}"
        )

    return root


def write_journal_entry(
    root_dir: Path,
    plan_id: str,
    ops: list[RenameOp],
) -> Path:
    """
    Write one complete transaction to a timestamped JSONL file.

    Args:
        root_dir: Root directory being managed by VaultFlow.
        plan_id: Identifier of the rename plan.
        ops: Operations belonging to the transaction.

    Returns:
        Path to the newly written journal file.

    Notes:
        The file is written to a temporary file first and then
        atomically moved into its final location.
    """
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise ValueError("plan_id must be a non-empty string.")

    root = _validate_root(root_dir)

    journal_dir = (
        root
        / JOURNAL_DIRECTORY
        / JOURNAL_SUBDIRECTORY
    )

    journal_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc)
    transaction_id = uuid.uuid4().hex

    timestamp_string = timestamp.strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )

    filename = (
        f"transaction-{timestamp_string}-"
        f"{transaction_id[:8]}.jsonl"
    )

    final_path = journal_dir / filename
    temporary_path: Path | None = None

    header = {
        "record_type": "transaction",
        "format_version": JOURNAL_FORMAT_VERSION,
        "transaction_id": transaction_id,
        "plan_id": plan_id,
        "timestamp_utc": timestamp.isoformat(),
        "operation_count": len(ops),
    }

    records = [header]

    for index, op in enumerate(ops):
        records.append({
            "record_type": "operation",
            "transaction_id": transaction_id,
            "index": index,
            **_operation_snapshot(op),
        })

    records.append({
        "record_type": "transaction_end",
        "transaction_id": transaction_id,
        "operation_count": len(ops),
        "completed_at_utc": datetime.now(
            timezone.utc
        ).isoformat(),
    })

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=".vaultflow-",
            suffix=".tmp",
            dir=journal_dir,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)

            for record in records:
                stream.write(
                    json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        allow_nan=False,
                    )
                )
                stream.write("\n")

            stream.flush()
            os.fsync(stream.fileno())

        # Publish the complete journal atomically.
        os.replace(temporary_path, final_path)
        temporary_path = None

        return final_path

    finally:
        # Remove incomplete temporary files after an error.
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _parse_journal_file(journal_file: Path) -> dict[str, Any]:
    """
    Read and validate one transaction journal.

    Raises:
        ValueError: If the journal is malformed or incomplete.
        OSError: If the journal cannot be read.
    """
    records: list[dict[str, Any]] = []

    with journal_file.open(
        "r",
        encoding="utf-8",
    ) as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in {journal_file.name}, "
                    f"line {line_number}: {exc}"
                ) from exc

            if not isinstance(record, dict):
                raise ValueError(
                    f"Invalid journal record on line {line_number}."
                )

            records.append(record)

    if len(records) < 2:
        raise ValueError(
            f"Incomplete journal transaction: {journal_file}"
        )

    header = records[0]
    footer = records[-1]

    if header.get("record_type") != "transaction":
        raise ValueError("Journal transaction header is missing.")

    if footer.get("record_type") != "transaction_end":
        raise ValueError("Journal completion record is missing.")

    transaction_id = header.get("transaction_id")

    if footer.get("transaction_id") != transaction_id:
        raise ValueError("Journal transaction IDs do not match.")

    operation_records = [
        record
        for record in records[1:-1]
        if record.get("record_type") == "operation"
    ]

    expected_count = header.get("operation_count")

    if len(operation_records) != expected_count:
        raise ValueError(
            "Journal operation count does not match its header."
        )

    return {
        **header,
        "operations": operation_records,
        "completed_at_utc": footer.get("completed_at_utc"),
        "journal_path": str(journal_file),
    }


def _is_dry_run(transaction: Mapping[str, Any]) -> bool:
    """True when every journaled operation is a DRY_RUN (nothing changed on disk)."""
    operations = transaction.get("operations") or []

    return bool(operations) and all(
        str(operation.get("status", "")).upper() == "DRY_RUN"
        for operation in operations
    )


def load_latest_journal(
    root_dir: Path,
    *,
    skip_dry_runs: bool = False,
) -> dict | None:
    """
    Load the most recent complete transaction.

    Args:
        root_dir: VaultFlow root directory.
        skip_dry_runs: If True, ignore journals that only record a dry run
            and return the newest transaction that really changed files.
            Defaults to False (plain "newest journal file").

    Returns:
        A dictionary containing the transaction header,
        operation records, and journal path.

        Returns None if no journal files exist (or, with
        ``skip_dry_runs=True``, if every journal is a dry run).

    Raises:
        ValueError: If a journal that must be examined is malformed
            or incomplete.
        OSError: If the journal cannot be read.
    """
    root = _validate_root(root_dir)

    journal_dir = (
        root
        / JOURNAL_DIRECTORY
        / JOURNAL_SUBDIRECTORY
    )

    if not journal_dir.is_dir():
        return None

    journal_files = sorted(
        journal_dir.glob("transaction-*.jsonl"),
        key=lambda path: path.name,
        reverse=True,
    )

    for journal_file in journal_files:
        transaction = _parse_journal_file(journal_file)

        if skip_dry_runs and _is_dry_run(transaction):
            continue

        return transaction

    return None


if __name__ == "__main__":
    """Lightweight command-line journal self-test."""

    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    with TemporaryDirectory(prefix="vaultflow-journal-test-") as temp:
        test_root = Path(temp)

        sample_ops = [
            SimpleNamespace(
                source=test_root / "original.txt",
                target=test_root / "renamed.txt",
                status="COMPLETED",
            )
        ]

        journal_path = write_journal_entry(
            test_root,
            "journal-self-test",
            sample_ops,
        )

        latest = load_latest_journal(test_root)

        assert journal_path.is_file()
        assert latest is not None
        assert latest["plan_id"] == "journal-self-test"
        assert len(latest["operations"]) == 1

        print("Journal self-test passed.")
        print(f"Journal: {journal_path}")
