"""Integration tests: the real scanner, rules, planner, executor and journal
wired together through ``VaultFlowEngine``.

These tests deliberately use NO mocks for the pipeline itself, so any drift
between module contracts (field names, signatures, statuses) fails here first.

Run from the project root::

    pytest tests/test_pipeline.py -v
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vaultflow import executor, journal
from vaultflow.engine import (
    CollisionError,
    ExecutionError,
    InvalidDirectoryError,
    PlanError,
    ScanError,
    VaultFlowEngine,
    VaultFlowError,
)
from vaultflow.models import RenameOp, RenamePlan
from vaultflow.rules import NamingRule, RuleKind


# --------------------------------------------------------------------------- #
# Helpers / fixtures
# --------------------------------------------------------------------------- #
def names(directory: Path) -> set[str]:
    """File names (not folders) directly inside ``directory``."""
    return {p.name for p in directory.iterdir() if p.is_file()}


def prefix(text: str = "V1001_") -> NamingRule:
    return NamingRule(RuleKind.PREFIX, text=text)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    for name in ("clip_a.mp4", "clip_b.mp4", "clip_c.mp4", "notes.txt"):
        (tmp_path / name).write_text(name)
    (tmp_path / ".hidden.mp4").write_text("hidden")
    return tmp_path


@pytest.fixture
def engine(workspace: Path) -> VaultFlowEngine:
    return VaultFlowEngine(workspace)


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #
def test_scan_skips_hidden_and_internal_files(engine: VaultFlowEngine) -> None:
    assert [e.name for e in engine.scan_directory()] == [
        "clip_a.mp4", "clip_b.mp4", "clip_c.mp4", "notes.txt",
    ]


def test_scan_accepts_positional_filters(engine: VaultFlowEngine) -> None:
    assert [e.name for e in engine.scan_directory([".txt"])] == ["notes.txt"]
    assert [e.name for e in engine.scan_directory(["clip_*"])] == [
        "clip_a.mp4", "clip_b.mp4", "clip_c.mp4",
    ]


def test_scan_rejects_bad_filters(engine: VaultFlowEngine) -> None:
    with pytest.raises(ScanError):
        engine.scan_directory([1])  # type: ignore[list-item]


def test_engine_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(InvalidDirectoryError):
        VaultFlowEngine(tmp_path / "does_not_exist")


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #
def test_plan_is_a_pure_preview(engine: VaultFlowEngine, workspace: Path) -> None:
    before = names(workspace)
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])

    assert isinstance(plan, RenamePlan)
    assert plan.plan_id and plan.total_count == 3
    assert all(isinstance(op, RenameOp) and op.status == "PENDING" for op in plan.ops)
    assert [op.target_path.name for op in plan.ops] == [
        "V1001_clip_a.mp4", "V1001_clip_b.mp4", "V1001_clip_c.mp4",
    ]
    assert names(workspace) == before


def test_leveling_rule_follows_entry_order(engine: VaultFlowEngine) -> None:
    entries = engine.scan_directory([".mp4"])
    plan = engine.plan_batch(entries, [NamingRule(RuleKind.LEVELING)])

    assert [op.target_path.name for op in plan.ops] == [
        "clip_a_lv1.0.0.mp4", "clip_b_lv1.0.1.mp4", "clip_c_lv1.0.2.mp4",
    ]


def test_colliding_targets_raise_collision_error(engine: VaultFlowEngine) -> None:
    rule = NamingRule(RuleKind.REPLACE, pattern=r"_[abc]", replacement="")
    with pytest.raises(CollisionError):
        engine.plan_batch(engine.scan_directory([".mp4"]), [rule])


def test_existing_file_is_never_overwritten(engine: VaultFlowEngine) -> None:
    only_a = [e for e in engine.scan_directory() if e.name == "clip_a.mp4"]
    rule = NamingRule(RuleKind.REPLACE, pattern="clip_a", replacement="clip_b")

    # clip_b.mp4 already exists and is not part of the rename set.
    with pytest.raises(CollisionError):
        engine.plan_batch(only_a, [rule])


def test_plan_rejects_non_naming_rules(engine: VaultFlowEngine) -> None:
    with pytest.raises(PlanError):
        engine.plan_batch(engine.scan_directory(), [object()])  # type: ignore[list-item]


def test_plan_rejects_empty_batch(engine: VaultFlowEngine) -> None:
    with pytest.raises(PlanError):
        engine.plan_batch([], [prefix()])


def test_output_dir_must_stay_inside_root(
    engine: VaultFlowEngine, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    with pytest.raises(PlanError):
        engine.plan_batch(engine.scan_directory(), [], outside)


# --------------------------------------------------------------------------- #
# Execute
# --------------------------------------------------------------------------- #
def test_dry_run_changes_nothing_but_is_journaled(
    engine: VaultFlowEngine, workspace: Path
) -> None:
    before = names(workspace)
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])

    ops = engine.execute_plan(plan, dry_run=True)

    assert names(workspace) == before
    assert all(op.status == "DRY_RUN" for op in ops)
    assert journal.load_latest_journal(workspace) is not None


def test_execute_renames_files_and_marks_completed(
    engine: VaultFlowEngine, workspace: Path
) -> None:
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])

    ops = engine.execute_plan(plan)

    assert all(op.status == "COMPLETED" for op in ops)
    assert {"V1001_clip_a.mp4", "V1001_clip_b.mp4", "V1001_clip_c.mp4"} <= names(workspace)
    assert not {"clip_a.mp4", "clip_b.mp4", "clip_c.mp4"} & names(workspace)
    assert (workspace / "notes.txt").exists()  # untouched


def test_noop_plan_executes_nothing(engine: VaultFlowEngine) -> None:
    plan = engine.plan_batch(engine.scan_directory(), [NamingRule(RuleKind.LOWER)])
    assert engine.execute_plan(plan) == []


def test_source_vanishing_before_execute_raises(
    engine: VaultFlowEngine, workspace: Path
) -> None:
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])
    (workspace / "clip_a.mp4").unlink()

    with pytest.raises(ExecutionError):
        engine.execute_plan(plan)
    # nothing else was touched
    assert {"clip_b.mp4", "clip_c.mp4"} <= names(workspace)


def test_failed_rename_rolls_back_and_raises(
    engine: VaultFlowEngine, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second rename fails -> the first is rolled back and ExecutionError carries statuses."""
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])
    real_rename = executor.rename_entry
    calls = {"n": 0}

    def flaky(source: Path, target: Path, case_only: bool = False) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise PermissionError("simulated failure")
        real_rename(source, target, case_only)

    monkeypatch.setattr(executor, "rename_entry", flaky)

    with pytest.raises(ExecutionError) as caught:
        engine.execute_plan(plan)

    assert [op.status for op in caught.value.ops] == ["ROLLED_BACK", "FAILED", "SKIPPED"]
    assert {"clip_a.mp4", "clip_b.mp4", "clip_c.mp4"} <= names(workspace)


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
def test_rollback_restores_original_names(
    engine: VaultFlowEngine, workspace: Path
) -> None:
    original = names(workspace)
    engine.execute_plan(engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()]))

    result = engine.rollback_latest()

    assert result is not None and result["complete"] and not result["nothing_to_undo"]
    assert len(result["reversed"]) == 3
    assert names(workspace) == original


def test_rollback_twice_is_safe(engine: VaultFlowEngine, workspace: Path) -> None:
    original = names(workspace)
    engine.execute_plan(engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()]))
    engine.rollback_latest()

    second = engine.rollback_latest()

    assert second is not None and second["reversed"] == [] and not second["complete"]
    assert names(workspace) == original


def test_dry_run_does_not_hide_the_real_transaction(
    engine: VaultFlowEngine, workspace: Path
) -> None:
    original = names(workspace)
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])
    engine.execute_plan(plan)
    engine.execute_plan(
        engine.plan_batch(engine.scan_directory([".mp4"]), [prefix("X_")]), dry_run=True
    )

    result = engine.rollback_latest()

    assert result is not None and len(result["reversed"]) == 3
    assert names(workspace) == original


def test_rollback_without_any_journal_returns_none(engine: VaultFlowEngine) -> None:
    assert engine.rollback_latest() is None


def test_lone_dry_run_is_not_undoable(engine: VaultFlowEngine) -> None:
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()])
    engine.execute_plan(plan, dry_run=True)
    assert engine.rollback_latest() is None


def test_rollback_never_overwrites_a_new_file(
    engine: VaultFlowEngine, workspace: Path
) -> None:
    engine.execute_plan(engine.plan_batch(engine.scan_directory([".mp4"]), [prefix()]))
    (workspace / "clip_a.mp4").write_text("someone else's file")

    result = engine.rollback_latest()

    assert result is not None and not result["complete"]
    assert (workspace / "clip_a.mp4").read_text() == "someone else's file"
    assert (workspace / "V1001_clip_a.mp4").exists()
    assert len(result["reversed"]) == 2


def test_output_dir_move_and_rollback(engine: VaultFlowEngine, workspace: Path) -> None:
    original = names(workspace)
    plan = engine.plan_batch(engine.scan_directory([".mp4"]), [], "sorted")

    engine.execute_plan(plan)
    assert {p.name for p in (workspace / "sorted").iterdir()} == {
        "clip_a.mp4", "clip_b.mp4", "clip_c.mp4",
    }

    engine.rollback_latest()
    assert names(workspace) == original


# --------------------------------------------------------------------------- #
# Case-only renames (the Windows / default-macOS problem)
# --------------------------------------------------------------------------- #
def test_case_only_rename_roundtrip(tmp_path: Path) -> None:
    """On Windows this exercises the case-insensitive path; elsewhere it is a plain rename."""
    (tmp_path / "Lecture.MP4").write_text("x")
    eng = VaultFlowEngine(tmp_path)

    plan = eng.plan_batch(eng.scan_directory(), [NamingRule(RuleKind.LOWER)])
    eng.execute_plan(plan)
    assert names(tmp_path) == {"lecture.MP4"}

    result = eng.rollback_latest()
    assert result is not None and result["complete"]
    assert names(tmp_path) == {"Lecture.MP4"}


def test_rename_entry_two_step_helper(tmp_path: Path) -> None:
    source = tmp_path / "Report.txt"
    source.write_text("x")

    executor.rename_entry(source, tmp_path / "report.txt", case_only=True)

    assert names(tmp_path) == {"report.txt"}  # no temp file left behind


def test_case_only_detection_requires_the_same_directory_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "A.txt"
    source.write_text("x")
    target = tmp_path / "a.txt"

    if target.exists():
        # Real case-insensitive filesystem (Windows, default macOS): both
        # spellings are the same entry, so detection must report a case change.
        assert executor.is_case_only_rename(source, target) is True
    else:
        # Case-sensitive filesystem, target absent: an ordinary rename.
        assert executor.is_case_only_rename(source, target) is False

        # Simulate a case-insensitive filesystem: "a.txt" resolves to A.txt.
        real_lstat = os.lstat

        def insensitive_lstat(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            text = os.fspath(path)
            if text.endswith("a.txt"):
                text = text[:-5] + "A.txt"
            return real_lstat(text, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", insensitive_lstat)
        assert executor.is_case_only_rename(source, target) is True

    # Different names (not a case change) never qualify, nor does identical spelling.
    assert executor.is_case_only_rename(source, tmp_path / "b.txt") is False
    assert executor.is_case_only_rename(source, source) is False


# --------------------------------------------------------------------------- #
# Journal option + error hierarchy
# --------------------------------------------------------------------------- #
def test_journal_can_skip_dry_runs(workspace: Path) -> None:
    eng = VaultFlowEngine(workspace)
    real_plan = eng.plan_batch(eng.scan_directory([".mp4"]), [prefix()])
    eng.execute_plan(real_plan)
    eng.execute_plan(eng.plan_batch(eng.scan_directory([".mp4"]), [prefix("Y_")]), dry_run=True)

    newest = journal.load_latest_journal(workspace)
    real = journal.load_latest_journal(workspace, skip_dry_runs=True)

    assert newest is not None and real is not None
    assert {op["status"] for op in newest["operations"]} == {"DRY_RUN"}
    assert {op["status"] for op in real["operations"]} == {"COMPLETED"}


def test_engine_errors_share_one_base_class() -> None:
    for error in (ScanError, PlanError, ExecutionError, CollisionError, InvalidDirectoryError):
        assert issubclass(error, VaultFlowError)
