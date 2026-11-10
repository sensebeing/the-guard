"""Revision reads the topology guard compares the working tree against."""

from __future__ import annotations

import difflib

from ..git_helpers import changed_files as changed_files


def _compute_changed_lines(base_text: str, current_text: str) -> set[int]:
    base_lines = base_text.splitlines()
    current_lines = current_text.splitlines()
    matcher = difflib.SequenceMatcher(None, base_lines, current_lines)
    changed: set[int] = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"replace", "insert"}:
            changed.update(range(j1 + 1, j2 + 1))
    return changed
