"""Hunk classification utilities for test/src separation."""

import re
from typing import List, Tuple

from .models import Hunk
from .patch_parser import get_modified_lines_with_content, get_insertion_point


def is_test_path(file_path: str) -> bool:
    """Check if path is in tests/ directory."""
    return "/tests/" in file_path or file_path.startswith("tests/")


def classify_hunk(hunk: Hunk, test_ranges: List[Tuple[int, int]]) -> Tuple[str, int, int]:
    """Classify a hunk as 'src', 'test', or 'mixed'.

    Returns (classification, test_lines_count, src_lines_count).

    - 'test': ALL modified lines are in test regions
    - 'src': NO modified lines are in test regions
    - 'mixed': SOME modified lines are in test regions, some are not

    Note: Empty/whitespace-only lines are ignored in classification since they
    don't represent meaningful code changes.
    """
    old_deleted_with_content, new_added_with_content = get_modified_lines_with_content(hunk)

    # Filter out empty/whitespace-only lines - they don't count as src or test
    old_deleted = [(ln, content) for ln, content in old_deleted_with_content if content.strip()]
    new_added = [(ln, content) for ln, content in new_added_with_content if content.strip()]

    if not old_deleted and not new_added:
        return "src", 0, 0

    def line_in_test(line: int) -> bool:
        return any(ts <= line <= te for ts, te in test_ranges)

    # For deletions: count lines in/out of test regions
    if old_deleted:
        in_test = sum(1 for ln, _ in old_deleted if line_in_test(ln))
        not_in_test = len(old_deleted) - in_test

        if in_test == 0:
            return "src", 0, not_in_test
        elif not_in_test == 0:
            return "test", in_test, 0
        else:
            return "mixed", in_test, not_in_test

    # For pure additions: check insertion point
    if new_added:
        insertion_point = get_insertion_point(hunk)
        for test_start, test_end in test_ranges:
            if test_start < insertion_point <= test_end:
                return "test", len(new_added), 0
        return "src", 0, len(new_added)

    return "src", 0, 0


def is_hunk_in_test_region(hunk: Hunk, test_ranges: List[Tuple[int, int]]) -> bool:
    """Check if hunk's actual modifications are ENTIRELY within test regions.

    Only considers actual modified lines ('+' and '-' lines), not context.
    Test range is [test_start, test_end] inclusive.

    Returns True only if ALL modified lines are in test regions.
    A hunk that deletes both src and test code returns False.
    """
    classification, _, _ = classify_hunk(hunk, test_ranges)
    return classification == "test"


def hunk_contains_test_code(hunk: Hunk) -> Tuple[bool, str]:
    """Check if hunk content contains test code markers."""
    test_patterns = [
        (r"^\+.*#\[test\]", "Adds #[test] attribute"),
        (r"^\+.*#\[cfg\(test\)\]", "Adds #[cfg(test)] attribute"),
        (r"^\+\s*mod\s+tests\s*\{", "Adds mod tests block"),
    ]
    for pattern, reason in test_patterns:
        if re.search(pattern, hunk.content, re.MULTILINE):
            return True, reason
    return False, ""


def hunks_are_similar(h1: Hunk, h2: Hunk, tolerance: int = 5) -> bool:
    """Check if two hunks are likely the same (similar position)."""
    return abs(h1.new_start - h2.new_start) <= tolerance and abs(h1.new_count - h2.new_count) <= tolerance
