"""vaultflow.rules - naming rule engine.

Every function in this module works on a file *stem* (the name without its
extension) and returns a new stem, so extensions are never touched. Use
:func:`apply_rules_to_filename` when you start from a full file name.

Standard library only (Python 3.11+).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import PurePath
from typing import Callable, Sequence

__all__ = [
    "DEFAULT_LEVEL_PREFIX",
    "NamingRule",
    "RuleKind",
    "add_prefix",
    "add_suffix",
    "apply_leveling_rule",
    "apply_rule",
    "apply_rules",
    "apply_rules_to_filename",
    "clean_spaces",
    "replace_pattern",
    "to_lower",
    "to_title",
    "to_upper",
]

DEFAULT_LEVEL_PREFIX = "lv1.0."
_FORBIDDEN_CHARS = ("/", "\\", "\x00")
_WORD = re.compile(r"[^\W_]+")


# --------------------------------------------------------------------------- #
# Rule description
# --------------------------------------------------------------------------- #
class RuleKind(StrEnum):
    """Supported transformation kinds."""

    UPPER = "UPPER"
    LOWER = "LOWER"
    TITLE = "TITLE"
    CLEAN_SPACES = "CLEAN_SPACES"
    PREFIX = "PREFIX"
    SUFFIX = "SUFFIX"
    REPLACE = "REPLACE"
    LEVELING = "LEVELING"


@dataclass(frozen=True, slots=True)
class NamingRule:
    """An immutable, declarative naming rule.

    Attributes:
        kind: Which transformation to run.
        text: Prefix/suffix text for PREFIX/SUFFIX; the version prefix for
            LEVELING (falls back to ``DEFAULT_LEVEL_PREFIX`` when empty).
        pattern: Regular expression for REPLACE.
        replacement: ``re.sub`` replacement template for REPLACE.
    """

    kind: RuleKind
    text: str = ""
    pattern: str = ""
    replacement: str = ""

    def __post_init__(self) -> None:
        # Accept plain strings such as "UPPER" and fail early on bad input.
        object.__setattr__(self, "kind", RuleKind(self.kind))
        if self.kind is RuleKind.REPLACE:
            if not self.pattern:
                raise ValueError("REPLACE rule requires a non-empty pattern")
            _compile(self.pattern)


# --------------------------------------------------------------------------- #
# Individual transformations (stem -> stem)
# --------------------------------------------------------------------------- #
def to_upper(stem: str) -> str:
    """Return ``stem`` in upper case."""
    return stem.upper()


def to_lower(stem: str) -> str:
    """Return ``stem`` in lower case."""
    return stem.lower()


def to_title(stem: str) -> str:
    """Capitalise each alphanumeric word, keeping separators (``_``, ``-``, space).

    ``"my_lecture-notes"`` becomes ``"My_Lecture-Notes"``.
    """
    return _WORD.sub(lambda m: m.group(0).capitalize(), stem)


def clean_spaces(stem: str) -> str:
    """Trim the stem and collapse every run of whitespace into one space."""
    return " ".join(stem.split())


def add_prefix(stem: str, prefix: str) -> str:
    """Return ``prefix + stem``."""
    return f"{prefix}{stem}"


def add_suffix(stem: str, suffix: str) -> str:
    """Return ``stem + suffix`` (the suffix goes before the file extension)."""
    return f"{stem}{suffix}"


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression {pattern!r}: {exc}") from exc


def replace_pattern(stem: str, pattern: str, replacement: str) -> str:
    """Apply ``re.sub(pattern, replacement, stem)``.

    Raises:
        ValueError: if ``pattern`` or ``replacement`` is not a valid regex /
            template.
    """
    try:
        return _compile(pattern).sub(replacement, stem)
    except re.error as exc:
        raise ValueError(f"Invalid replacement {replacement!r}: {exc}") from exc


def apply_leveling_rule(stem: str, index: int, prefix: str = DEFAULT_LEVEL_PREFIX) -> str:
    """Append a versioned level tag: ``f"{stem}_{prefix}{index}"``.

    >>> apply_leveling_rule("lecture", 0)
    'lecture_lv1.0.0'
    >>> apply_leveling_rule("lecture", 1)
    'lecture_lv1.0.1'

    Raises:
        ValueError: if ``index`` is not a non-negative integer.
    """
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError(f"index must be a non-negative integer, got {index!r}")
    return f"{stem}_{prefix}{index}"


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
def _validate_stem(stem: str) -> str:
    """Make sure a stem is usable as a file name component."""
    if not stem or stem in {".", ".."}:
        raise ValueError(f"Rules produced an unusable stem: {stem!r}")
    if any(ch in stem for ch in _FORBIDDEN_CHARS):
        raise ValueError(f"Rules produced a stem with path separators or NUL: {stem!r}")
    return stem


_SIMPLE: dict[RuleKind, Callable[[str], str]] = {
    RuleKind.UPPER: to_upper,
    RuleKind.LOWER: to_lower,
    RuleKind.TITLE: to_title,
    RuleKind.CLEAN_SPACES: clean_spaces,
}


def apply_rule(stem: str, rule: NamingRule, index: int = 0) -> str:
    """Apply one rule to ``stem``. ``index`` is only used by LEVELING."""
    kind = rule.kind
    if kind in _SIMPLE:
        return _SIMPLE[kind](stem)
    if kind is RuleKind.PREFIX:
        return add_prefix(stem, rule.text)
    if kind is RuleKind.SUFFIX:
        return add_suffix(stem, rule.text)
    if kind is RuleKind.REPLACE:
        return replace_pattern(stem, rule.pattern, rule.replacement)
    if kind is RuleKind.LEVELING:
        return apply_leveling_rule(stem, index, rule.text or DEFAULT_LEVEL_PREFIX)
    raise ValueError(f"Unsupported rule kind: {kind!r}")  # pragma: no cover


def apply_rules(stem: str, rules: Sequence[NamingRule], index: int = 0) -> str:
    """Apply ``rules`` in order and return the validated new stem.

    Args:
        stem: Original stem (no extension).
        rules: Rules applied left to right.
        index: Position of the file in the batch (drives LEVELING rules).

    Raises:
        ValueError: if a rule is invalid or the result is not a legal stem.
    """
    result = stem
    for rule in rules:
        result = apply_rule(result, rule, index)
    return _validate_stem(result)


def apply_rules_to_filename(filename: str, rules: Sequence[NamingRule], index: int = 0) -> str:
    """Apply ``rules`` to the stem of ``filename`` and re-attach its extension."""
    path = PurePath(filename)
    return f"{apply_rules(path.stem, rules, index)}{path.suffix}"


# --------------------------------------------------------------------------- #
# Lightweight self-test: python -m vaultflow.rules
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    assert to_upper("lecture") == "LECTURE"
    assert to_lower("LECTURE") == "lecture"
    assert to_title("my_lecture-notes") == "My_Lecture-Notes"
    assert clean_spaces("  a   b \t c ") == "a b c"
    assert add_prefix("notes", "V1001_") == "V1001_notes"
    assert add_suffix("notes", "_final") == "notes_final"
    assert replace_pattern("IMG_0042", r"IMG_(\d+)", r"photo-\1") == "photo-0042"
    assert apply_leveling_rule("lecture", 0) == "lecture_lv1.0.0"
    assert apply_leveling_rule("lecture", 1) == "lecture_lv1.0.1"
    assert apply_leveling_rule("lecture", 2, prefix="v2.") == "lecture_v2.2"

    rules = [
        NamingRule(RuleKind.CLEAN_SPACES),
        NamingRule(RuleKind.REPLACE, pattern=r"\s+", replacement="_"),
        NamingRule(RuleKind.LOWER),
        NamingRule(RuleKind.LEVELING),
    ]
    assert apply_rules_to_filename("My   Lecture.MP4", rules, 3) == "my_lecture_lv1.0.3.MP4"

    for bad in (
        lambda: apply_leveling_rule("x", -1),
        lambda: NamingRule(RuleKind.REPLACE, pattern="("),
        lambda: apply_rules("x", [NamingRule(RuleKind.REPLACE, pattern="x", replacement="a/b")]),
    ):
        try:
            bad()
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")

    print("vaultflow.rules: all checks passed")
