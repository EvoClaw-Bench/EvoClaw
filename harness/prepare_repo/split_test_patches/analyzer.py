#!/usr/bin/env python3
"""
Analyze milestone patches for test/src separation issues.

Reuses logic from verify_test_separation.py to identify:
- Milestones with test hunks in milestone_patches
- Milestones with mixed hunks (both test and src code)
"""

from pathlib import Path
from typing import Dict, List, Optional
from dataclasses import dataclass

from .verify_test_separation import (
    parse_patch_hunks,
    analyze_patch_test_hunks,
    get_file_at_git_ref,
    find_test_ranges_from_content,
    classify_hunk,
    is_test_path,
    get_actual_modified_lines,
    get_insertion_point,
    Hunk,
)


@dataclass
class MilestoneAnalysis:
    """Analysis result for a single milestone."""

    milestone: str
    has_issues: bool
    test_hunks: List[Dict]
    src_hunks: List[Dict]
    mixed_hunks: List[Dict]
    error: Optional[str] = None


@dataclass
class BaselineAnalysis:
    """Analysis result for entire baseline."""

    baseline_dir: str
    total_milestones: int
    problematic_milestones: List[str]
    clean_milestones: List[str]
    milestone_details: Dict[str, MilestoneAnalysis]


def analyze_milestone(base_dir: str, milestone: str) -> MilestoneAnalysis:
    """
    Analyze a single milestone's patch for test/src separation.

    Args:
        base_dir: Path to baseline directory
        milestone: Milestone ID (e.g., 'milestone_seed_xxx_1')

    Returns:
        MilestoneAnalysis with detailed hunk information
    """
    result = analyze_patch_test_hunks(base_dir, milestone)

    if "error" in result:
        return MilestoneAnalysis(
            milestone=milestone, has_issues=False, test_hunks=[], src_hunks=[], mixed_hunks=[], error=result["error"]
        )

    test_hunks = result.get("test_hunks", [])
    src_hunks = result.get("src_hunks", [])
    mixed_hunks = result.get("mixed_hunks", [])

    # Has issues if there are any test or mixed hunks
    has_issues = len(test_hunks) > 0 or len(mixed_hunks) > 0

    return MilestoneAnalysis(
        milestone=milestone, has_issues=has_issues, test_hunks=test_hunks, src_hunks=src_hunks, mixed_hunks=mixed_hunks
    )


def analyze_baseline(baseline_dir: str, milestone: Optional[str] = None) -> BaselineAnalysis:
    """
    Analyze all milestones in a baseline for test/src separation issues.

    Args:
        baseline_dir: Path to baseline directory (e.g., .../baseline_004)
        milestone: Optional specific milestone to analyze

    Returns:
        BaselineAnalysis with summary and details
    """
    base_path = Path(baseline_dir)
    patches_dir = base_path / "milestone_patches"

    if not patches_dir.exists():
        raise FileNotFoundError(f"milestone_patches not found: {patches_dir}")

    # Get milestones to analyze
    if milestone:
        milestones = [milestone]
    else:
        milestones = sorted(f.stem for f in patches_dir.glob("*.patch"))

    problematic = []
    clean = []
    details = {}

    for m in milestones:
        analysis = analyze_milestone(baseline_dir, m)
        details[m] = analysis

        if analysis.error:
            continue

        if analysis.has_issues:
            problematic.append(m)
        else:
            clean.append(m)

    return BaselineAnalysis(
        baseline_dir=baseline_dir,
        total_milestones=len(milestones),
        problematic_milestones=problematic,
        clean_milestones=clean,
        milestone_details=details,
    )


def get_patch_content(base_dir: str, milestone: str) -> str:
    """Get the raw patch content for a milestone."""
    patch_path = Path(base_dir) / "milestone_patches" / f"{milestone}.patch"
    if not patch_path.exists():
        raise FileNotFoundError(f"Patch not found: {patch_path}")
    return patch_path.read_text()


def get_test_hunks_detail(base_dir: str, milestone: str) -> Dict:
    """
    Get detailed information about test hunks for agent processing.

    Returns dict with:
    - test_hunks: List of test hunk details with file, lines, and content
    - mixed_hunks: List of mixed hunk details that need intelligent splitting
    - patch_content: Full patch content for reference
    """
    analysis = analyze_milestone(base_dir, milestone)
    patch_content = get_patch_content(base_dir, milestone)

    # Parse patch to get actual hunk content
    hunks_by_file = parse_patch_hunks(patch_content)

    # Enrich test hunks with content
    test_hunks_detail = []
    for hunk_info in analysis.test_hunks:
        file_path = hunk_info["file"]
        # Find matching hunk content
        if file_path in hunks_by_file:
            for hunk in hunks_by_file[file_path]:
                hunk_lines = f"{hunk.new_start}-{hunk.new_start + hunk.new_count}"
                if hunk_lines == hunk_info.get("new_lines"):
                    test_hunks_detail.append(
                        {
                            "file": file_path,
                            "old_lines": hunk_info.get("old_lines"),
                            "new_lines": hunk_lines,
                            "reason": hunk_info.get("reason", ""),
                            "content": hunk.content,
                        }
                    )
                    break

    # Enrich mixed hunks with content
    mixed_hunks_detail = []
    for hunk_info in analysis.mixed_hunks:
        file_path = hunk_info["file"]
        if file_path in hunks_by_file:
            for hunk in hunks_by_file[file_path]:
                hunk_lines = f"{hunk.new_start}-{hunk.new_start + hunk.new_count}"
                if hunk_lines == hunk_info.get("new_lines"):
                    mixed_hunks_detail.append(
                        {
                            "file": file_path,
                            "old_lines": hunk_info.get("old_lines"),
                            "new_lines": hunk_lines,
                            "reason": hunk_info.get("reason", ""),
                            "test_lines": hunk_info.get("test_lines", 0),
                            "src_lines": hunk_info.get("src_lines", 0),
                            "content": hunk.content,
                        }
                    )
                    break

    return {
        "milestone": milestone,
        "test_hunks": test_hunks_detail,
        "mixed_hunks": mixed_hunks_detail,
        "src_hunks_count": len(analysis.src_hunks),
        "patch_content": patch_content,
    }


def print_analysis(analysis: BaselineAnalysis, verbose: bool = False):
    """Print analysis results to console."""
    print(f"\nBaseline Analysis: {analysis.baseline_dir}")
    print("=" * 70)
    print(f"Total milestones: {analysis.total_milestones}")
    print(f"Clean milestones: {len(analysis.clean_milestones)}")
    print(f"Problematic milestones: {len(analysis.problematic_milestones)}")

    if analysis.problematic_milestones:
        print(f"\nProblematic milestones requiring fix:")
        for m in analysis.problematic_milestones:
            detail = analysis.milestone_details[m]
            parts = []
            if detail.test_hunks:
                parts.append(f"{len(detail.test_hunks)} test")
            if detail.mixed_hunks:
                parts.append(f"{len(detail.mixed_hunks)} mixed")
            print(f"  - {m}: {', '.join(parts)} hunks")

            if verbose:
                for h in detail.test_hunks[:3]:
                    print(f"      [test] {h['file']} lines {h['new_lines']}")
                for h in detail.mixed_hunks[:3]:
                    print(f"      [mixed] {h['file']} lines {h['new_lines']}")

    if analysis.clean_milestones and verbose:
        print(f"\nClean milestones (src-only):")
        for m in analysis.clean_milestones:
            detail = analysis.milestone_details[m]
            print(f"  - {m}: {len(detail.src_hunks)} src hunks")

    print("=" * 70)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Analyze milestone patches for test/src separation")
    parser.add_argument("baseline_dir", help="Path to baseline directory")
    parser.add_argument("--milestone", help="Analyze specific milestone only")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed hunk info")

    args = parser.parse_args()

    analysis = analyze_baseline(args.baseline_dir, args.milestone)
    print_analysis(analysis, args.verbose)
