"""VaultFlow engine: the single coordination point for the rename pipeline.

The engine ties together the lower-level modules and exposes a small,
exception-safe API::

    scan_directory -> plan_batch -> execute_plan -> (rollback_latest)

Lower-level modules (scanner, rules, planner, executor, journal) may raise
``OSError``, ``ValueError``, ``planner.CollisionError`` and so on.  The engine
converts every one of those into a :class:`VaultFlowError` subclass, always
chaining the original exception (``raise ... from exc``) so no debugging
information is lost.

Run the self-test from the project root (relative imports require ``-m``)::

    python -m vaultflow.engine

Interfaces assumed from sibling modules (adjust the adapter lines marked
``# ADAPTER`` if your signatures differ):

* ``scanner.scan_directory(root: Path, filters: list[str] | None) -> list[FileEntry]``
* ``planner.generate_rename_plan(entries, rules, output_dir) -> RenamePlan``
  (raises ``planner.CollisionError`` on name collisions)
* ``executor.execute_plan(plan, dry_run: bool, journal_dir: Path) -> list[RenameOp]``
  (writes the JSONL journal through ``journal.py`` itself)
* ``journal.load_latest_transaction(journal_dir: Path) -> dict | None`` where the
  dict looks like ``{"transaction_id": str, "ops": [{"src": str, "dst": str,
  "status": str}, ...]}``
* ``journal.mark_rolled_back(journal_dir, transaction_id)`` (optional)
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import executor, journal, planner, scanner
from .planner import CollisionError, RenameOp, RenamePlan
from .rules import NamingRule
from .scanner import FileEntry

__all__ = [
    "VaultFlowEngine",
    "VaultFlowError",
    "InvalidRootError",
    "ScanError",
    "PlanError",
    "PlanCollisionError",
    "ExecutionError",
    "RollbackError",
]

logger = logging.getLogger(__name__)

# Journal statuses whose rename actually happened on disk and can be undone.
_REVERSIBLE_STATUSES: frozenset[str] = frozenset({"DONE", "COMPLETED", "SUCCESS", "RENAMED"})


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class VaultFlowError(Exception):
    """Base class for every error raised by the VaultFlow engine."""


class InvalidRootError(VaultFlowError):
    """The engine root path does not exist or is not a directory."""


class ScanError(VaultFlowError):
    """Directory discovery failed."""


class PlanError(VaultFlowError):
    """A rename plan could not be generated."""


class PlanCollisionError(PlanError):
    """The planned names collide with each other or with existing files."""


class ExecutionError(VaultFlowError):
    """Executing (or dry-running) a plan failed."""


class RollbackError(VaultFlowError):
    """The latest transaction could not be loaded or reversed."""


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class VaultFlowEngine:
    """Coordinates scanning, planning, executing and rolling back renames.

    Args:
        root: Directory the engine operates on.
        journal_dir: Where JSONL transaction journals are stored.  Defaults to
            ``<root>/.vaultflow`` (hidden, so scans never pick it up).

    Raises:
        InvalidRootError: If ``root`` is missing or not a directory.
    """

    JOURNAL_DIRNAME = ".vaultflow"

    def __init__(self, root: str | os.PathLike[str], journal_dir: Path | None = None) -> None:
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise InvalidRootError(f"Root directory not accessible: {root!s}") from exc
        if not resolved.is_dir():
            raise InvalidRootError(f"Root is not a directory: {resolved}")

        self._root: Path = resolved
        self._journal_dir: Path = (
            Path(journal_dir).expanduser().resolve()
            if journal_dir is not None
            else resolved / self.JOURNAL_DIRNAME
        )

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """Resolved root directory."""
        return self._root

    @property
    def journal_dir(self) -> Path:
        """Directory holding the JSONL journals."""
        return self._journal_dir

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #
    def scan_directory(self, filters: list[str] | None = None) -> list[FileEntry]:
        """Discover files under the root using ``scanner.py``.

        Args:
            filters: Optional scanner filters (e.g. extensions or patterns).

        Returns:
            The discovered :class:`FileEntry` objects.

        Raises:
            ScanError: On invalid filters or any scanner/filesystem failure.
        """
        if filters is not None:
            if isinstance(filters, str) or not all(isinstance(f, str) and f for f in filters):
                raise ScanError("filters must be a list of non-empty strings")

        try:
            entries = scanner.scan_directory(self._root, filters)  # ADAPTER
        except VaultFlowError:
            raise
        except (OSError, ValueError) as exc:
            raise ScanError(f"Failed to scan {self._root}: {exc}") from exc
        except Exception as exc:  # defensive: never leak foreign exception types
            raise ScanError(f"Unexpected scanner failure in {self._root}: {exc}") from exc

        result = list(entries)
        logger.info("Scanned %s: %d file(s) found", self._root, len(result))
        return result

    def plan_batch(
        self,
        entries: list[FileEntry],
        rules: list[NamingRule],
        output_dir: Path | None = None,
    ) -> RenamePlan:
        """Generate a preview plan using ``planner.py`` (nothing is touched on disk).

        Args:
            entries: Files to rename, normally from :meth:`scan_directory`.
            rules: Ordered naming rules applied to each entry.
            output_dir: Optional destination directory for renamed files.

        Returns:
            A :class:`RenamePlan` the caller can inspect before executing.

        Raises:
            PlanCollisionError: If generated names collide.
            PlanError: On invalid input or any other planning failure.
        """
        if not entries:
            raise PlanError("Cannot plan an empty batch: no entries supplied")
        if not rules:
            raise PlanError("At least one naming rule is required")

        resolved_output: Path | None = None
        if output_dir is not None:
            resolved_output = Path(output_dir).expanduser().resolve()
            if resolved_output.exists() and not resolved_output.is_dir():
                raise PlanError(f"output_dir exists but is not a directory: {resolved_output}")

        try:
            plan = planner.generate_rename_plan(  # ADAPTER
                list(entries), list(rules), resolved_output
            )
        except VaultFlowError:
            raise
        except CollisionError as exc:
            raise PlanCollisionError(f"Rename collision detected: {exc}") from exc
        except (OSError, ValueError, TypeError) as exc:
            raise PlanError(f"Failed to generate rename plan: {exc}") from exc
        except Exception as exc:
            raise PlanError(f"Unexpected planner failure: {exc}") from exc

        logger.info("Planned batch of %d entr(y/ies)", len(entries))
        return plan

    def execute_plan(self, plan: RenamePlan, dry_run: bool = False) -> list[RenameOp]:
        """Execute a plan through ``executor.py``.

        The executor journals every operation to JSONL via ``journal.py``,
        which is what makes :meth:`rollback_latest` possible.

        Args:
            plan: Plan produced by :meth:`plan_batch`.
            dry_run: If True, simulate only; no files are renamed.

        Returns:
            The resulting :class:`RenameOp` records.

        Raises:
            ExecutionError: On any executor, journal or filesystem failure.
        """
        if plan is None:
            raise ExecutionError("plan must not be None")

        try:
            ops = executor.execute_plan(  # ADAPTER
                plan, dry_run=dry_run, journal_dir=self._journal_dir
            )
        except VaultFlowError:
            raise
        except (OSError, ValueError) as exc:
            raise ExecutionError(f"Plan execution failed: {exc}") from exc
        except Exception as exc:
            raise ExecutionError(f"Unexpected executor failure: {exc}") from exc

        result = list(ops)
        logger.info("%s %d operation(s)", "Simulated" if dry_run else "Executed", len(result))
        return result

    def rollback_latest(self) -> dict[str, Any] | None:
        """Reverse the most recent journaled transaction, if needed.

        Operations are undone in reverse order.  An operation is only reversed
        when its renamed file still exists and the original path is free, so
        the engine never overwrites data created since the rename.

        Returns:
            ``None`` if no transaction exists; otherwise a summary dict::

                {
                    "transaction_id": str | None,
                    "already_rolled_back": bool,
                    "reversed": [{"src": str, "dst": str}, ...],
                    "skipped": [{"src": str, "dst": str, "reason": str}, ...],
                    "complete": bool,   # False if any reversible op was blocked
                }

        Raises:
            RollbackError: If the journal cannot be read or is malformed.
        """
        try:
            transaction = journal.load_latest_transaction(self._journal_dir)  # ADAPTER
        except VaultFlowError:
            raise
        except (OSError, ValueError) as exc:
            raise RollbackError(f"Failed to load latest transaction: {exc}") from exc
        except Exception as exc:
            raise RollbackError(f"Unexpected journal failure: {exc}") from exc

        if transaction is None:
            logger.info("No transactions to roll back")
            return None

        transaction_id = transaction.get("transaction_id")
        summary: dict[str, Any] = {
            "transaction_id": transaction_id,
            "already_rolled_back": False,
            "reversed": [],
            "skipped": [],
            "complete": True,
        }

        if transaction.get("rolled_back"):
            summary["already_rolled_back"] = True
            return summary

        raw_ops = transaction.get("ops", [])
        if not isinstance(raw_ops, Sequence) or isinstance(raw_ops, (str, bytes)):
            raise RollbackError(f"Malformed transaction {transaction_id!r}: 'ops' must be a list")

        for op in reversed(raw_ops):
            try:
                src, dst = Path(op["src"]), Path(op["dst"])
            except (KeyError, TypeError) as exc:
                raise RollbackError(
                    f"Malformed operation in transaction {transaction_id!r}: {op!r}"
                ) from exc

            status = str(op.get("status", "")).upper()
            record = {"src": str(src), "dst": str(dst)}

            if status not in _REVERSIBLE_STATUSES:
                summary["skipped"].append({**record, "reason": f"status {status or 'UNKNOWN'}: nothing to undo"})
                continue

            reason = self._reverse_one(src, dst)
            if reason is None:
                summary["reversed"].append(record)
            else:
                summary["skipped"].append({**record, "reason": reason})
                summary["complete"] = False

        if summary["complete"]:
            self._mark_rolled_back(transaction_id)

        logger.info(
            "Rollback of %s: %d reversed, %d skipped",
            transaction_id,
            len(summary["reversed"]),
            len(summary["skipped"]),
        )
        return summary

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _reverse_one(src: Path, dst: Path) -> str | None:
        """Move ``dst`` back to ``src``. Return None on success, else a reason."""
        if not dst.exists():
            return "renamed file no longer exists"
        if src.exists():
            return "original path is occupied"
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            os.rename(dst, src)
        except OSError as exc:
            return f"OS error: {exc}"
        return None

    def _mark_rolled_back(self, transaction_id: str | None) -> None:
        """Best-effort journal annotation; failures never abort a finished rollback."""
        mark = getattr(journal, "mark_rolled_back", None)
        if not callable(mark) or transaction_id is None:
            return
        try:
            mark(self._journal_dir, transaction_id)  # ADAPTER
        except Exception as exc:  # noqa: BLE001 - files are already restored
            logger.warning("Could not mark transaction %s as rolled back: %s", transaction_id, exc)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
class _PrefixRule:
    """Minimal stand-in rule for the self-test (prefixes the file name).

    Exposes both ``apply`` and ``__call__`` so it works whichever way
    ``planner.py`` invokes a NamingRule.
    """

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def apply(self, name: str) -> str:
        return f"{self.prefix}{name}"

    __call__ = apply


def _self_test() -> None:
    """Exercise scan -> plan -> dry-run -> execute -> rollback in a temp dir."""
    tmp = Path(tempfile.mkdtemp(prefix="vaultflow_selftest_"))
    try:
        originals = {"clip_a.mp4", "clip_b.mp4", "clip_c.mp4"}
        for name in originals:
            (tmp / name).write_text("x")
        (tmp / ".hidden.mp4").write_text("x")  # must be ignored by the scanner

        engine = VaultFlowEngine(tmp)

        entries = engine.scan_directory()
        assert len(entries) == len(originals), f"expected {len(originals)} entries, got {len(entries)}"
        print(f"[1/5] scan     OK ({len(entries)} files)")

        plan = engine.plan_batch(entries, [_PrefixRule("V1001_")])
        print("[2/5] plan     OK")

        engine.execute_plan(plan, dry_run=True)
        assert {p.name for p in tmp.iterdir() if p.is_file()} >= originals, "dry run modified files"
        print("[3/5] dry-run  OK (no files touched)")

        ops = engine.execute_plan(plan)
        names = {p.name for p in tmp.iterdir() if p.is_file()}
        expected = {f"V1001_{n}" for n in originals}
        assert expected <= names, f"missing renamed files: {expected - names}"
        assert not (originals & names), "original names still present after execute"
        print(f"[4/5] execute  OK ({len(ops)} ops)")

        result = engine.rollback_latest()
        assert result is not None and result["complete"], f"rollback incomplete: {result}"
        names = {p.name for p in tmp.iterdir() if p.is_file()}
        assert originals <= names, "rollback did not restore original names"
        print(f"[5/5] rollback OK ({len(result['reversed'])} reversed)")

        print("VaultFlowEngine self-test passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        _self_test()
    except (VaultFlowError, AssertionError) as exc:
        print(f"Self-test FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
