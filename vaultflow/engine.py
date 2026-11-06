"""vaultflow.engine - the coordinator for the rename pipeline.

The engine ties the lower-level modules together behind one small API::

    scan_directory -> plan_batch -> execute_plan -> (rollback_latest)

It is written against the *actual* contracts of the sibling modules:

* ``scanner.scan_directory(root, filters)``            -> ``list[FileEntry]``
* ``planner.generate_rename_plan(entries, rules, out)`` -> ``RenamePlan``
  (raises ``errors.CollisionError`` / ``ValueError``)
* ``executor.execute_plan(plan, root_dir, dry_run)``    -> ``list[RenameOp]``
  (journals through ``journal.write_journal_entry`` itself; reports per-op
  failures through ``op.status`` instead of raising)
* ``journal.load_latest_journal(root_dir, skip_dry_runs=True)`` -> ``dict | None``
  (journals always live in ``<root>/.vaultflow/journal``)

Error policy: everything the engine raises is a ``vaultflow.errors.VaultFlowError``.
Specific errors from lower layers (``CollisionError``, ``InvalidDirectoryError``,
``RenameError`` ...) pass through untouched; anything else is wrapped in
``ScanError`` / ``PlanError`` / ``ExecutionError`` / ``RollbackError`` with the
original exception chained (``raise ... from exc``).

Self-test (from the project root)::

    python -m vaultflow.engine
"""

from __future__ import annotations

import dataclasses
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from vaultflow import executor, journal, planner, scanner
from vaultflow.errors import (
    CollisionError,
    DirectoryAccessError,
    InvalidDirectoryError,
    JournalError,
    NamingError,
    RenameError,
    VaultFlowError,
)
from vaultflow.models import FileEntry, RenameOp, RenamePlan
from vaultflow.rules import NamingRule

__all__ = [
    "VaultFlowEngine",
    "ScanError",
    "PlanError",
    "ExecutionError",
    "RollbackError",
    # re-exported so callers can catch everything from one import
    "VaultFlowError",
    "CollisionError",
    "InvalidDirectoryError",
    "DirectoryAccessError",
    "JournalError",
    "NamingError",
    "RenameError",
]

logger = logging.getLogger(__name__)

# Executor statuses (see executor.execute_plan).
_STATUS_COMPLETED = "COMPLETED"
_FAILURE_STATUSES = frozenset({"FAILED", "ROLLBACK_FAILED"})


# --------------------------------------------------------------------------- #
# Engine-level exceptions (all are VaultFlowError subclasses)
# --------------------------------------------------------------------------- #
class ScanError(VaultFlowError):
    """Directory discovery failed for a reason not covered by a directory error."""


class PlanError(VaultFlowError):
    """A rename plan could not be generated from the supplied input."""


class ExecutionError(VaultFlowError):
    """Plan execution failed.

    Attributes:
        ops: The operations with their final statuses, when available
            (useful for reporting which file failed and what was rolled back).
    """

    def __init__(self, message: str = "", ops: Iterable[RenameOp] | None = None) -> None:
        super().__init__(message)
        self.ops: list[RenameOp] = list(ops) if ops else []


class RollbackError(VaultFlowError):
    """The latest journal transaction could not be interpreted or reversed."""


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class VaultFlowEngine:
    """Coordinates scanning, planning, executing and rolling back renames.

    Args:
        root: Directory the engine manages.  Every file it touches (sources,
            targets, ``output_dir``) must live inside it; the executor enforces
            the same boundary.

    Raises:
        InvalidDirectoryError: If ``root`` is missing or not a directory.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        try:
            resolved = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise InvalidDirectoryError(f"Root directory not accessible: {root!s}") from exc
        if not resolved.is_dir():
            raise InvalidDirectoryError(f"Root is not a directory: {resolved}")
        self._root: Path = resolved

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        """Resolved root directory."""
        return self._root

    @property
    def journal_dir(self) -> Path:
        """Where ``journal.py`` stores JSONL transactions for this root."""
        return self._root / journal.JOURNAL_DIRECTORY / journal.JOURNAL_SUBDIRECTORY

    # ------------------------------------------------------------------ #
    # Pipeline
    # ------------------------------------------------------------------ #
    def scan_directory(self, filters: list[str] | None = None) -> list[FileEntry]:
        """Discover files under the root using ``scanner.py``.

        Args:
            filters: Optional extension / glob-pattern tokens, e.g.
                ``[".mp4", "clip_*"]`` (see ``scanner.scan_directory``).

        Returns:
            Naturally sorted :class:`FileEntry` objects (non-recursive; hidden
            files and ``.vaultflow`` are excluded).

        Raises:
            InvalidDirectoryError / DirectoryAccessError: root problems.
            ScanError: invalid filters or any unexpected scanner failure.
        """
        try:
            entries = scanner.scan_directory(self._root, filters)
        except VaultFlowError:
            raise
        except (TypeError, ValueError) as exc:
            raise ScanError(f"Invalid scan request: {exc}") from exc
        except OSError as exc:
            raise DirectoryAccessError(f"Failed to scan {self._root}: {exc}") from exc
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

        Entries are processed in the order given, which is also the ``index``
        fed to LEVELING rules.

        Args:
            entries: Files to rename, normally from :meth:`scan_directory`.
            rules: ``vaultflow.rules.NamingRule`` objects applied in order.
                An empty list is allowed (useful with ``output_dir`` to move files).
            output_dir: Optional destination directory inside the root.  A
                relative path is interpreted relative to the root.

        Returns:
            A :class:`RenamePlan` whose ops all have status ``PENDING``.

        Raises:
            CollisionError: Targets collide with each other or with existing files.
            PlanError: Invalid input or any other planning failure.
        """
        entries = list(entries)
        rules = list(rules)

        if not entries:
            raise PlanError("Cannot plan an empty batch: no entries supplied")

        for rule in rules:
            if not isinstance(rule, NamingRule):
                raise PlanError(
                    "rules must be vaultflow.rules.NamingRule instances, "
                    f"got {type(rule).__name__}"
                )

        resolved_output: Path | None = None
        if output_dir is not None:
            candidate = Path(output_dir).expanduser()
            if not candidate.is_absolute():
                candidate = self._root / candidate
            resolved_output = candidate.resolve(strict=False)
            if not self._within_root(resolved_output):
                raise PlanError(f"output_dir is outside the engine root: {resolved_output}")
            if resolved_output.exists() and not resolved_output.is_dir():
                raise PlanError(f"output_dir exists but is not a directory: {resolved_output}")

        for entry in entries:
            path = getattr(entry, "path", None)
            if path is None:
                raise PlanError(f"Entry has no 'path' attribute: {entry!r}")
            if not self._within_root(Path(path)):
                raise PlanError(f"Entry is outside the engine root: {path}")

        try:
            plan = planner.generate_rename_plan(entries, rules, resolved_output)
        except VaultFlowError:
            raise  # includes CollisionError - already a clean VaultFlow exception
        except ValueError as exc:
            raise PlanError(f"Invalid rename request: {exc}") from exc
        except (TypeError, AttributeError, KeyError) as exc:
            raise PlanError(
                f"Planner received incompatible input or models: {type(exc).__name__}: {exc}"
            ) from exc
        except OSError as exc:
            raise PlanError(f"Filesystem error while planning: {exc}") from exc
        except Exception as exc:
            raise PlanError(f"Unexpected planner failure: {exc}") from exc

        if not getattr(plan, "is_safe", True):
            raise CollisionError(
                "Plan has unresolved conflicts: "
                + ", ".join(str(p) for p in sorted(plan.conflicts))
            )

        logger.info("Planned batch: %d operation(s)", len(plan.ops))
        return plan

    def execute_plan(self, plan: RenamePlan, dry_run: bool = False) -> list[RenameOp]:
        """Execute a plan through ``executor.py``.

        The executor validates every operation before touching the disk and
        journals the batch (including dry runs) via ``journal.py``.  No-op
        operations (target == source) are filtered out first, because the
        executor rejects them.

        Args:
            plan: Plan produced by :meth:`plan_batch`.
            dry_run: Validate and journal only; no file is renamed.

        Returns:
            The executed :class:`RenameOp` objects with updated ``status``
            (``COMPLETED``, or ``DRY_RUN`` for a dry run).  Empty when the plan
            contained nothing to rename.

        Raises:
            ExecutionError: Validation or journaling failed, or a rename failed
                (completed renames of that batch are rolled back by the executor
                first).  ``exc.ops`` carries the per-operation statuses.
        """
        raw_ops = getattr(plan, "ops", None)
        if raw_ops is None:
            raise ExecutionError("plan has no 'ops'; expected a RenamePlan")

        all_ops = list(raw_ops)
        effective = [op for op in all_ops if not getattr(op, "is_noop", False)]

        if not effective:
            logger.info("Nothing to do: every operation is a no-op")
            return []

        run_plan = plan
        if len(effective) != len(all_ops):
            logger.info("Skipping %d no-op operation(s)", len(all_ops) - len(effective))
            try:
                run_plan = dataclasses.replace(
                    plan, ops=tuple(effective), total_count=len(effective)
                )
            except (TypeError, ValueError) as exc:
                raise ExecutionError(f"Could not prepare plan for execution: {exc}") from exc

        try:
            result = executor.execute_plan(run_plan, self._root, dry_run=dry_run)
        except VaultFlowError as exc:
            # Specific errors (RenameError, JournalError, ...) pass through;
            # the executor's generic validation/journal errors get a clear name.
            if type(exc) is VaultFlowError:
                raise ExecutionError(str(exc)) from exc
            raise
        except OSError as exc:
            raise ExecutionError(f"Filesystem error during execution: {exc}") from exc
        except Exception as exc:
            raise ExecutionError(f"Unexpected executor failure: {exc}") from exc

        result = list(result)

        # The executor reports rename failures via status instead of raising.
        failed = [op for op in result if getattr(op, "status", None) in _FAILURE_STATUSES]
        if failed:
            first = failed[0]
            detail = getattr(first, "error", None) or getattr(first, "status", "FAILED")
            stuck = [op for op in failed if op.status == "ROLLBACK_FAILED"]
            suffix = (
                " Some renames could NOT be reversed - inspect the journal."
                if stuck
                else " Completed renames from this batch were rolled back."
            )
            raise ExecutionError(
                f"{len(failed)} operation(s) failed; first: {detail}.{suffix}", ops=result
            )

        logger.info("%s %d operation(s)", "Simulated" if dry_run else "Executed", len(result))
        return result

    def rollback_latest(self) -> dict[str, Any] | None:
        """Reverse the most recent journaled transaction, if needed.

        Only operations journaled as ``COMPLETED`` are reversed, newest first.
        Each reversal is skipped (and reported) rather than forced when the
        renamed file is gone, the original path is occupied, or a path falls
        outside the root, so nothing is ever overwritten.

        Note:
            ``executor.py`` also journals dry runs; those are ignored here, so
            "latest" means the newest transaction that really renamed files.

        Returns:
            ``None`` if no such transaction exists; otherwise::

                {
                    "transaction_id": str, "plan_id": str, "journal_path": str,
                    "reversed": [{"source": str, "target": str}, ...],
                    "skipped":  [{"source": str, "target": str, "reason": str}, ...],
                    "nothing_to_undo": bool,  # no COMPLETED operations journaled
                    "complete": bool,         # every COMPLETED operation was reversed
                }

        Raises:
            JournalError: The journal could not be read or parsed.
            RollbackError: The journal content is not in the expected shape.
        """
        try:
            transaction = journal.load_latest_journal(self._root, skip_dry_runs=True)
        except VaultFlowError:
            raise
        except (OSError, ValueError) as exc:
            raise JournalError(f"Cannot load latest journal: {exc}") from exc
        except Exception as exc:
            raise JournalError(f"Unexpected journal failure: {exc}") from exc

        if transaction is None:
            logger.info("No executed transaction found; nothing to roll back")
            return None

        records = transaction.get("operations")
        if not isinstance(records, list):
            raise RollbackError("Malformed journal: 'operations' must be a list")

        summary: dict[str, Any] = {
            "transaction_id": transaction.get("transaction_id"),
            "plan_id": transaction.get("plan_id"),
            "journal_path": transaction.get("journal_path"),
            "reversed": [],
            "skipped": [],
            "nothing_to_undo": True,
            "complete": True,
        }

        for record in sorted(records, key=lambda r: r.get("index", 0), reverse=True):
            source, target = record.get("source"), record.get("target")
            if not isinstance(source, str) or not isinstance(target, str):
                raise RollbackError(f"Malformed journal operation: {record!r}")

            status = str(record.get("status", "")).upper()
            entry = {"source": source, "target": target}

            if status != _STATUS_COMPLETED:
                summary["skipped"].append(
                    {**entry, "reason": f"status {status or 'UNKNOWN'}: nothing to undo"}
                )
                continue

            summary["nothing_to_undo"] = False
            reason = self._reverse_one(Path(source), Path(target))
            if reason is None:
                summary["reversed"].append(entry)
            else:
                summary["skipped"].append({**entry, "reason": reason})
                summary["complete"] = False

        logger.info(
            "Rollback of %s: %d reversed, %d skipped",
            summary["transaction_id"],
            len(summary["reversed"]),
            len(summary["skipped"]),
        )
        return summary

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _within_root(self, path: Path) -> bool:
        """True if ``path`` resolves to a location inside the root."""
        try:
            Path(path).resolve(strict=False).relative_to(self._root)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    def _reverse_one(self, source: Path, target: Path) -> str | None:
        """Move ``target`` back to ``source``.  Return None on success, else a reason."""
        if not (self._within_root(source) and self._within_root(target)):
            return "path is outside the engine root"
        if not os.path.lexists(target):
            return "renamed file no longer exists"
        # A case-only rename (Lecture.mp4 -> lecture.mp4) looks "occupied" on a
        # case-insensitive filesystem; the executor helper tells the cases apart.
        case_only = executor.is_case_only_rename(target, source)
        if os.path.lexists(source) and not case_only:
            return "original path is occupied"
        try:
            source.parent.mkdir(parents=True, exist_ok=True)
            executor.rename_entry(target, source, case_only)
        except OSError as exc:
            return f"OS error: {exc}"
        return None


# --------------------------------------------------------------------------- #
# Self-test:  python -m vaultflow.engine
# --------------------------------------------------------------------------- #
def _listing(directory: Path) -> set[str]:
    return {p.name for p in directory.iterdir() if p.is_file()}


def _self_test() -> None:
    """Exercise scan -> plan -> dry-run -> execute -> rollback with real rules."""
    from vaultflow.rules import RuleKind

    tmp = Path(tempfile.mkdtemp(prefix="vaultflow_selftest_"))
    empty = Path(tempfile.mkdtemp(prefix="vaultflow_selftest_empty_"))
    try:
        originals = {"clip_a.mp4", "clip_b.mp4", "clip_c.mp4"}
        for name in originals:
            (tmp / name).write_text("x")
        (tmp / ".hidden.mp4").write_text("x")  # must be ignored by the scanner

        engine = VaultFlowEngine(tmp)

        entries = engine.scan_directory()
        assert len(entries) == len(originals), f"expected {len(originals)} entries, got {len(entries)}"
        assert [e.name for e in engine.scan_directory([".mp4"])] == sorted(originals)
        print(f"[1/7] scan      OK ({len(entries)} files)")

        rules = [NamingRule(RuleKind.PREFIX, text="V1001_")]
        plan = engine.plan_batch(entries, rules)
        assert len(plan.ops) == len(originals)
        assert all(op.status == "PENDING" for op in plan.ops)
        print("[2/7] plan      OK")

        dry = engine.execute_plan(plan, dry_run=True)
        assert _listing(tmp) >= originals, "dry run modified files"
        assert all(op.status == "DRY_RUN" for op in dry)
        print("[3/7] dry-run   OK (no files touched)")

        ops = engine.execute_plan(plan)
        expected = {f"V1001_{n}" for n in originals}
        names = _listing(tmp)
        assert expected <= names and not (originals & names), f"unexpected listing: {names}"
        assert all(op.status == "COMPLETED" for op in ops)
        print(f"[4/7] execute   OK ({len(ops)} ops)")

        result = engine.rollback_latest()
        assert result is not None and result["complete"], f"rollback incomplete: {result}"
        assert len(result["reversed"]) == len(originals)
        assert originals <= _listing(tmp), "rollback did not restore original names"
        print(f"[5/7] rollback  OK ({len(result['reversed'])} reversed)")

        again = engine.rollback_latest()
        assert again is not None and not again["reversed"] and not again["complete"], again
        engine.execute_plan(plan, dry_run=True)  # a newer dry-run journal must not hide...
        assert engine.rollback_latest() is not None  # ...the real transaction (now already undone)
        print("[6/7] re-rollback is safe (nothing overwritten, dry runs ignored)")

        collide = [NamingRule(RuleKind.REPLACE, pattern=r"_[abc]", replacement="")]
        try:
            engine.plan_batch(engine.scan_directory(), collide)
        except CollisionError:
            pass
        else:
            raise AssertionError("expected CollisionError for colliding targets")
        try:
            VaultFlowEngine(empty).plan_batch(entries, [object()])  # type: ignore[list-item]
        except PlanError:
            pass
        else:
            raise AssertionError("expected PlanError for non-NamingRule input")
        fresh = VaultFlowEngine(empty)
        assert fresh.rollback_latest() is None
        (empty / "only.txt").write_text("x")
        fresh_plan = fresh.plan_batch(fresh.scan_directory(), rules)
        fresh.execute_plan(fresh_plan, dry_run=True)
        assert fresh.rollback_latest() is None, "a dry run alone must not count as undoable"
        print("[7/7] errors    OK (collision, bad rule, no journal, lone dry run)")

        print("VaultFlowEngine self-test passed.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(empty, ignore_errors=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        _self_test()
    except (VaultFlowError, AssertionError) as exc:
        print(f"Self-test FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
