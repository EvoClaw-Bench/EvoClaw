"""Milestone verification for test/src separation."""

from pathlib import Path
from typing import Dict

from .models import Hunk
from .patch_parser import parse_patch_hunks
from .test_detector import find_test_ranges_from_content
from .hunk_classifier import (
    is_test_path,
    is_hunk_in_test_region,
    hunk_contains_test_code,
    hunks_are_similar,
)
from .git_utils import get_file_at_git_ref


def verify_milestone(old_dir: str, new_dir: str, milestone: str) -> Dict:
    """Verify a milestone at hunk level.

    Compares old and new patch files to verify that:
    1. Removed files should be test files
    2. Removed hunks should be in test code regions
    3. New patch should not contain test code changes

    Args:
        old_dir: Path to old baseline directory (with original patches)
        new_dir: Path to new baseline directory (with updated patches)
        milestone: Milestone ID

    Returns:
        Dict with verification results
    """
    old_patch_path = Path(old_dir) / "milestone_patches" / f"{milestone}.patch"
    new_patch_path = Path(new_dir) / "milestone_patches" / f"{milestone}.patch"
    new_testbed = Path(new_dir) / "testbed"
    old_testbed = Path(old_dir) / "testbed"

    if not old_patch_path.exists() or not new_patch_path.exists():
        return {"error": "Patch not found", "overall_success": False}

    with open(old_patch_path) as f:
        old_patch = f.read()
    with open(new_patch_path) as f:
        new_patch = f.read()

    old_hunks = parse_patch_hunks(old_patch)
    new_hunks = parse_patch_hunks(new_patch)

    old_files = set(old_hunks.keys())
    new_files = set(new_hunks.keys())

    result = {
        "milestone": milestone,
        "file_level": {
            "removed_test_files": [],
            "removed_src_files": [],  # violation
        },
        "hunk_level": {
            "removed_test_hunks": [],
            "removed_src_hunks": [],  # violation
        },
        "new_patch": {
            "test_path_changes": [],  # violation
            "test_module_changes": [],  # violation (strict)
            "src_changes": [],
        },
    }

    # === File-level: Check removed files ===
    # For files not in tests/ directory, check if ALL hunks are in #[cfg(test)] regions
    removed_files = old_files - new_files
    old_start_tag = f"milestone-{milestone}-start"
    for f in removed_files:
        if is_test_path(f):
            result["file_level"]["removed_test_files"].append(f)
        else:
            # Check if all hunks in this file are within test regions
            file_hunks = old_hunks.get(f, [])
            if file_hunks:
                # Get test ranges from start tag state
                file_content = get_file_at_git_ref(old_testbed, f, old_start_tag)
                if file_content:
                    test_ranges = find_test_ranges_from_content(file_content, f)
                else:
                    test_ranges = []

                # Check if ALL hunks are in test regions
                all_in_test = True
                for hunk in file_hunks:
                    if not is_hunk_in_test_region(hunk, test_ranges):
                        all_in_test = False
                        break

                if all_in_test and test_ranges:
                    result["file_level"]["removed_test_files"].append(f)
                else:
                    result["file_level"]["removed_src_files"].append(f)
            else:
                result["file_level"]["removed_src_files"].append(f)

    # === Hunk-level: Check removed hunks in common files ===
    # Use start tag state for test region detection (consistent with analyze)
    common_files = old_files & new_files
    for f in common_files:
        old_file_hunks = old_hunks[f]
        new_file_hunks = new_hunks[f]

        # Get test ranges from START tag state (before patch applied)
        file_content = get_file_at_git_ref(old_testbed, f, old_start_tag)
        if file_content:
            test_ranges = find_test_ranges_from_content(file_content, f)
        else:
            test_ranges = []

        # Find hunks in old but not in new (approximately)
        for old_hunk in old_file_hunks:
            found_match = False
            for new_hunk in new_file_hunks:
                if hunks_are_similar(old_hunk, new_hunk):
                    found_match = True
                    break

            if not found_match:
                # This hunk was removed - check if it's test code
                # Method 1: Check if in test region (using start tag state)
                in_test_region = is_hunk_in_test_region(old_hunk, test_ranges)

                # Method 2: Check hunk content for test markers
                has_test_content, test_reason = hunk_contains_test_code(old_hunk)

                hunk_info = {
                    "file": f,
                    "old_lines": f"{old_hunk.old_start}-{old_hunk.old_start + old_hunk.old_count}",
                    "new_lines": f"{old_hunk.new_start}-{old_hunk.new_start + old_hunk.new_count}",
                    "in_test_region": in_test_region,
                    "has_test_content": has_test_content,
                    "test_reason": (
                        test_reason
                        if has_test_content
                        else (f"In #[cfg(test)] region {test_ranges}" if in_test_region else "")
                    ),
                }

                if in_test_region or has_test_content or is_test_path(f):
                    result["hunk_level"]["removed_test_hunks"].append(hunk_info)
                else:
                    result["hunk_level"]["removed_src_hunks"].append(hunk_info)

    # === New patch: Check for test code ===
    # Use start tag state for test region detection (hunk line numbers are relative to start tag)
    start_tag = f"milestone-{milestone}-start"
    for f, hunks in new_hunks.items():
        # Get test ranges from START tag state (before patch applied)
        file_content = get_file_at_git_ref(new_testbed, f, start_tag)
        if file_content:
            test_ranges = find_test_ranges_from_content(file_content, f)
        else:
            test_ranges = []

        for hunk in hunks:
            hunk_info = {
                "file": f,
                "lines": f"{hunk.new_start}-{hunk.new_start + hunk.new_count}",
            }

            if is_test_path(f):
                result["new_patch"]["test_path_changes"].append(hunk_info)
            else:
                # Check actual modified lines (not entire hunk range with context)
                in_test = is_hunk_in_test_region(hunk, test_ranges)
                if in_test:
                    hunk_info["test_region"] = test_ranges
                    result["new_patch"]["test_module_changes"].append(hunk_info)
                else:
                    result["new_patch"]["src_changes"].append(hunk_info)

    # === Determine success ===
    result["file_level"]["success"] = len(result["file_level"]["removed_src_files"]) == 0
    result["hunk_level"]["success"] = len(result["hunk_level"]["removed_src_hunks"]) == 0
    result["new_patch"]["success"] = (
        len(result["new_patch"]["test_path_changes"]) == 0 and len(result["new_patch"]["test_module_changes"]) == 0
    )

    result["overall_success"] = (
        result["file_level"]["success"] and result["hunk_level"]["success"] and result["new_patch"]["success"]
    )

    return result
