"""vaultflow.planner - build a collision-free rename plan.

The planner is pure with respect to the file system: it only *reads* (to detect
files that would be overwritten) and never renames anything.

Assumed shapes of the shared models (adjust the two marked spots in
``generate_rename_plan`` if yours differ):

* ``FileEntry``  - exposes ``.path`` (``Path``).
* ``RenameOp``   - frozen; ``RenameOp(source_path=..., target_path=..., status=...)``.
* ``RenamePlan`` - ``RenamePlan(ops=[...], total_count=int, created_at=datetime)``.
* ``CollisionError`` - accepts a single message string.

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from vaultflow.errors import CollisionError
from vaultflow.models import FileEntry, RenameOp, RenamePlan
from vaultflow.rules import NamingRule, apply_rules

__all__ = ["generate_rename_plan"]


def _key(path: Path) -> str:
    """Normalised comparison key (absolute, case-folded on case-insensitive OSes)."""
    return os.path.normcase(os.path.abspath(path))


def _same_file(a: Path, b: Path) -> bool:
    """True when both paths exist and point at the same file (case-only renames)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def generate_rename_plan(
    entries: Sequence[FileEntry],
    rules: Sequence[NamingRule],
    output_dir: Path | None = None,
) -> RenamePlan:
    """Create a :class:`RenamePlan` for ``entries`` using ``rules``.

    Entries are processed in the order given (so manual rankings are kept), and
    the position of each entry is the ``index`` fed to LEVELING rules.

    Args:
        entries: Files to rename.
        rules: Naming rules applied, in order, to each file's stem.
        output_dir: Destination directory. ``None`` renames in place.

    Returns:
        A plan whose ops all have ``status="PENDING"``. Entries whose name does
        not change still produce an op (source == target).

    Raises:
        CollisionError: two entries map to the same target, or a target already
            exists on disk and is not itself being renamed away.
        ValueError: invalid rules, an unusable resulting name, duplicate
            entries, or ``output_dir`` pointing at a file.
    """
    if output_dir is not None:
        output_dir = Path(output_dir)
        if output_dir.exists() and not output_dir.is_dir():
            raise ValueError(f"output_dir is not a directory: {output_dir}")

    # 1-2. Deterministic pass: compute (source, target) pairs.
    pairs: list[tuple[Path, Path]] = []
    source_keys: dict[str, Path] = {}
    for index, entry in enumerate(entries):
        source = Path(entry.path)  # <- adapt here if FileEntry names it differently
        skey = _key(source)
        if skey in source_keys:
            raise ValueError(f"Duplicate entry in rename set: {source}")
        source_keys[skey] = source

        new_stem = apply_rules(source.stem, rules, index)
        target_dir = output_dir if output_dir is not None else source.parent
        pairs.append((source, target_dir / f"{new_stem}{source.suffix}"))

    # 3a. Internal collisions: several sources -> one target.
    by_target: dict[str, list[Path]] = defaultdict(list)
    target_paths: dict[str, Path] = {}
    for source, target in pairs:
        tkey = _key(target)
        by_target[tkey].append(source)
        target_paths.setdefault(tkey, target)
    conflicts = {k: v for k, v in by_target.items() if len(v) > 1}
    if conflicts:
        lines = [
            f"  {target_paths[k]} <- " + ", ".join(str(p) for p in sources)
            for k, sources in conflicts.items()
        ]
        raise CollisionError(
            "Multiple files would be renamed to the same path:\n" + "\n".join(lines)
        )

    # 3b. Existing-file overwrites: target exists and is not part of the rename set.
    overwrites = [
        (source, target)
        for source, target in pairs
        if os.path.lexists(target)
        and _key(target) not in source_keys
        and not _same_file(source, target)
    ]
    if overwrites:
        lines = [f"  {source} -> {target}" for source, target in overwrites]
        raise CollisionError(
            "Target paths already exist on disk and would be overwritten:\n"
            + "\n".join(lines)
        )

    # 4-5. Build frozen ops and the plan.
    ops = [
        RenameOp(source_path=source, target_path=target, status="PENDING")
        for source, target in pairs
    ]
    return RenamePlan(  # <- adapt here if RenamePlan names its fields differently
        ops=ops,
        total_count=len(ops),
        created_at=datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
# Lightweight self-test: python -m vaultflow.planner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile
    from types import SimpleNamespace

    from vaultflow.rules import RuleKind

    def _entries(*paths: Path) -> list[SimpleNamespace]:
        # The planner only reads ``.path``, so a stand-in keeps this demo
        # independent of FileEntry's constructor.
        return [SimpleNamespace(path=p) for p in paths]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # Happy path: ordering, leveling, extension preserved.
        first, second = root / "My  Lecture.MP4", root / "Other Lecture.MP4"
        first.touch()
        second.touch()
        rules = [
            NamingRule(RuleKind.CLEAN_SPACES),
            NamingRule(RuleKind.REPLACE, pattern=r"\s+", replacement="_"),
            NamingRule(RuleKind.LOWER),
            NamingRule(RuleKind.LEVELING),
        ]
        plan = generate_rename_plan(_entries(first, second), rules)
        assert [op.target_path.name for op in plan.ops] == [
            "my_lecture_lv1.0.0.MP4",
            "other_lecture_lv1.0.1.MP4",
        ]
        assert plan.total_count == 2 and all(op.status == "PENDING" for op in plan.ops)
        print(f"OK  plan: {plan.total_count} ops at {plan.created_at.isoformat()}")

        # Internal collision: two sources -> same target in output_dir.
        (root / "a").mkdir()
        (root / "b").mkdir()
        (root / "a" / "x.txt").touch()
        (root / "b" / "x.txt").touch()
        try:
            generate_rename_plan(
                _entries(root / "a" / "x.txt", root / "b" / "x.txt"), [], root / "out"
            )
        except CollisionError as exc:
            print("OK  internal collision detected:\n" + str(exc))
        else:
            raise AssertionError("expected CollisionError (internal)")

        # Existing-file overwrite: d.txt exists and is not being renamed.
        (root / "c.txt").touch()
        (root / "d.txt").touch()
        try:
            generate_rename_plan(
                _entries(root / "c.txt"),
                [NamingRule(RuleKind.REPLACE, pattern="c", replacement="d")],
            )
        except CollisionError as exc:
            print("OK  overwrite detected:\n" + str(exc))
        else:
            raise AssertionError("expected CollisionError (overwrite)")

    print("vaultflow.planner: all checks passed")
