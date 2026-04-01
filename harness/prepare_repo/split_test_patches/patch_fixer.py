#!/usr/bin/env python3
"""
Fix patches by removing TEST hunks.

This module provides utilities to:
1. Extract TEST hunks from a patch
2. Apply those TEST hunks to the start tag
3. Update the start tag
4. Regenerate the patch (now without TEST hunks)
"""

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .analyzer import analyze_patch_test_hunks
from .patch_parser import parse_patch_hunks
from .models import Hunk

logger = logging.getLogger(__name__)


def extract_test_hunks_as_patch(patch_content: str, test_hunk_infos: List[Dict]) -> str:
    """
    Extract TEST hunks from a patch and return them as a new patch.

    Args:
        patch_content: Full patch content
        test_hunk_infos: List of test hunk info dicts from analyze_patch_test_hunks

    Returns:
        New patch containing only the TEST hunks
    """
    if not test_hunk_infos:
        return ""

    # Build a set of (file, old_lines) to identify test hunks
    test_hunk_keys = set()
    for h in test_hunk_infos:
        test_hunk_keys.add((h["file"], h["old_lines"]))

    # Parse the patch
    hunks_by_file = parse_patch_hunks(patch_content)

    # Collect test hunks by file
    test_hunks_by_file: Dict[str, List[Hunk]] = {}
    for file_path, hunks in hunks_by_file.items():
        for hunk in hunks:
            old_lines = f"{hunk.old_start}-{hunk.old_start + hunk.old_count}"
            if (file_path, old_lines) in test_hunk_keys:
                if file_path not in test_hunks_by_file:
                    test_hunks_by_file[file_path] = []
                test_hunks_by_file[file_path].append(hunk)

    if not test_hunks_by_file:
        return ""

    # Reconstruct patch with only test hunks
    lines = []
    for file_path, hunks in sorted(test_hunks_by_file.items()):
        # File header
        lines.append(f"diff --git a/{file_path} b/{file_path}")

        # Check if this is a new file (old_start == 0 or 1 with old_count == 0)
        is_new_file = all(h.old_start <= 1 and h.old_count == 0 for h in hunks)

        if is_new_file:
            lines.append("new file mode 100644")
            lines.append("--- /dev/null")
        else:
            lines.append(f"--- a/{file_path}")
        lines.append(f"+++ b/{file_path}")

        # Add hunks
        for hunk in hunks:
            lines.append(hunk.content)

    return "\n".join(lines) + "\n"


def apply_patch_to_ref(testbed: Path, patch_content: str, ref: str) -> Tuple[bool, str]:
    """
    Apply a patch to a specific git ref.

    Args:
        testbed: Path to testbed git repository
        patch_content: Patch content to apply
        ref: Git ref to checkout and apply to

    Returns:
        Tuple of (success, message)
    """
    if not patch_content.strip():
        return True, "Empty patch, nothing to apply"

    # Checkout to ref
    result = subprocess.run(["git", "checkout", ref], capture_output=True, text=True, cwd=testbed)
    if result.returncode != 0:
        return False, f"Failed to checkout {ref}: {result.stderr}"

    # Reset hard to ensure clean state
    subprocess.run(["git", "reset", "--hard", ref], capture_output=True, cwd=testbed)

    # Write patch to temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(patch_content)
        patch_file = f.name

    try:
        # Apply patch
        result = subprocess.run(["git", "apply", "--check", patch_file], capture_output=True, text=True, cwd=testbed)
        if result.returncode != 0:
            # Try with --3way
            result = subprocess.run(["git", "apply", "--3way", patch_file], capture_output=True, text=True, cwd=testbed)
            if result.returncode != 0:
                return False, f"Failed to apply patch: {result.stderr}"
        else:
            # Apply for real
            result = subprocess.run(["git", "apply", patch_file], capture_output=True, text=True, cwd=testbed)
            if result.returncode != 0:
                return False, f"Failed to apply patch: {result.stderr}"

        return True, "Patch applied successfully"
    finally:
        Path(patch_file).unlink()


def commit_changes(testbed: Path, message: str) -> Tuple[bool, str]:
    """
    Stage and commit all changes.

    Args:
        testbed: Path to testbed git repository
        message: Commit message

    Returns:
        Tuple of (success, message)
    """
    # Stage all changes
    result = subprocess.run(["git", "add", "-A"], capture_output=True, text=True, cwd=testbed)
    if result.returncode != 0:
        return False, f"Failed to stage changes: {result.stderr}"

    # Check if there are changes to commit
    result = subprocess.run(["git", "diff", "--cached", "--quiet"], capture_output=True, text=True, cwd=testbed)
    if result.returncode == 0:
        return True, "No changes to commit"

    # Commit
    result = subprocess.run(["git", "commit", "-m", message], capture_output=True, text=True, cwd=testbed)
    if result.returncode != 0:
        return False, f"Failed to commit: {result.stderr}"

    return True, "Changes committed"


def update_tag(testbed: Path, tag_name: str) -> Tuple[bool, str]:
    """
    Update a tag to point to current HEAD.

    Args:
        testbed: Path to testbed git repository
        tag_name: Name of the tag to update

    Returns:
        Tuple of (success, message)
    """
    # Delete tag if exists
    subprocess.run(["git", "tag", "-d", tag_name], capture_output=True, text=True, cwd=testbed)

    # Create tag at HEAD
    result = subprocess.run(["git", "tag", tag_name], capture_output=True, text=True, cwd=testbed)
    if result.returncode != 0:
        return False, f"Failed to create tag: {result.stderr}"

    return True, f"Tag {tag_name} updated to HEAD"


def regenerate_patch(testbed: Path, from_ref: str, to_ref: str, output_path: Path) -> Tuple[bool, str]:
    """
    Regenerate a patch file.

    Args:
        testbed: Path to testbed git repository
        from_ref: Source git ref
        to_ref: Target git ref
        output_path: Path to save the patch

    Returns:
        Tuple of (success, message)
    """
    result = subprocess.run(["git", "diff", from_ref, to_ref], capture_output=True, text=True, cwd=testbed)
    if result.returncode != 0:
        return False, f"Failed to generate diff: {result.stderr}"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout, encoding="utf-8")

    return True, f"Patch saved to {output_path}"


def fix_milestone_patch(base_dir: str, milestone: str, dry_run: bool = False) -> Dict:
    """
    Fix a milestone patch by applying TEST hunks to start tag.

    This function:
    1. Analyzes the current patch for TEST hunks
    2. Extracts those TEST hunks as a separate patch
    3. Applies them to the start tag
    4. Updates the start tag
    5. Regenerates both patches

    Args:
        base_dir: Path to baseline directory
        milestone: Milestone ID
        dry_run: If True, only analyze without making changes

    Returns:
        Dict with fix results
    """
    base_path = Path(base_dir)
    testbed = base_path / "testbed"
    milestone_patch_path = base_path / "milestone_patches" / f"{milestone}.patch"
    start_diff_path = base_path / "milestone_patches" / "start_diff_patches" / f"{milestone}.patch"

    start_tag = f"milestone-{milestone}-start"
    end_tag = f"milestone-{milestone}-end"

    result = {
        "milestone": milestone,
        "test_hunks_found": 0,
        "fixed": False,
        "dry_run": dry_run,
        "steps": [],
        "errors": [],
    }

    # Step 1: Analyze current patch
    analysis = analyze_patch_test_hunks(base_dir, milestone, patches_subdir="milestone_patches", ref_tag=start_tag)

    if "error" in analysis:
        result["errors"].append(f"Analysis failed: {analysis['error']}")
        return result

    test_hunks = analysis.get("test_hunks", [])
    result["test_hunks_found"] = len(test_hunks)

    if not test_hunks:
        result["steps"].append("No TEST hunks found, patch is clean")
        result["fixed"] = True
        return result

    result["steps"].append(f"Found {len(test_hunks)} TEST hunks to fix")
    for h in test_hunks:
        result["steps"].append(f"  - {h['file']} lines {h['new_lines']} ({h.get('reason', '')})")

    if dry_run:
        result["steps"].append("[DRY RUN] Would apply these changes")
        return result

    # Step 2: Read patch and extract TEST hunks
    with open(milestone_patch_path) as f:
        patch_content = f.read()

    test_patch = extract_test_hunks_as_patch(patch_content, test_hunks)

    if not test_patch.strip():
        result["errors"].append("Failed to extract TEST hunks as patch")
        return result

    result["steps"].append(f"Extracted TEST hunks as patch ({len(test_patch)} bytes)")

    # Step 3: Check if start-old tag exists, create if not
    check_result = subprocess.run(
        ["git", "tag", "-l", f"milestone-{milestone}-start-old"], capture_output=True, text=True, cwd=testbed
    )
    has_start_old = bool(check_result.stdout.strip())

    if not has_start_old:
        # Create start-old from current start
        subprocess.run(["git", "tag", f"milestone-{milestone}-start-old", start_tag], capture_output=True, cwd=testbed)
        result["steps"].append(f"Created milestone-{milestone}-start-old tag")

    # Step 4: Apply TEST patch to start tag
    success, msg = apply_patch_to_ref(testbed, test_patch, start_tag)
    if not success:
        result["errors"].append(f"Failed to apply patch: {msg}")
        return result
    result["steps"].append(f"Applied TEST patch to {start_tag}")

    # Step 5: Commit changes
    success, msg = commit_changes(testbed, f"Apply test changes for {milestone}")
    if not success:
        result["errors"].append(f"Failed to commit: {msg}")
        return result
    result["steps"].append(f"Committed changes: {msg}")

    # Step 6: Update start tag
    success, msg = update_tag(testbed, start_tag)
    if not success:
        result["errors"].append(f"Failed to update tag: {msg}")
        return result
    result["steps"].append(f"Updated {start_tag} to new commit")

    # Step 7: Regenerate milestone patch (start -> end)
    success, msg = regenerate_patch(testbed, start_tag, end_tag, milestone_patch_path)
    if not success:
        result["errors"].append(f"Failed to regenerate milestone patch: {msg}")
        return result
    result["steps"].append("Regenerated milestone patch")

    # Step 8: Regenerate start_diff patch if start-old exists
    if has_start_old or True:  # We created it above
        start_old_tag = f"milestone-{milestone}-start-old"
        success, msg = regenerate_patch(testbed, start_old_tag, start_tag, start_diff_path)
        if not success:
            result["errors"].append(f"Failed to regenerate start_diff patch: {msg}")
            return result
        result["steps"].append("Regenerated start_diff patch")

    # Step 9: Verify fix
    verification = analyze_patch_test_hunks(base_dir, milestone, patches_subdir="milestone_patches", ref_tag=start_tag)

    remaining_test = len(verification.get("test_hunks", []))
    if remaining_test == 0:
        result["fixed"] = True
        result["steps"].append("✓ Verification passed: 0 TEST hunks remaining")
    else:
        result["steps"].append(f"✗ Verification failed: {remaining_test} TEST hunks still remain")
        for h in verification.get("test_hunks", []):
            result["steps"].append(f"    - {h['file']} lines {h['new_lines']}")

    return result


def fix_baseline_patches(base_dir: str, milestone: Optional[str] = None, dry_run: bool = False) -> Dict:
    """
    Fix all patches in a baseline that have TEST hunks.

    Args:
        base_dir: Path to baseline directory
        milestone: Optional specific milestone to fix
        dry_run: If True, only analyze without making changes

    Returns:
        Dict with fix results for all milestones
    """
    base_path = Path(base_dir)
    patches_dir = base_path / "milestone_patches"

    if milestone:
        milestones = [milestone]
    else:
        milestones = sorted(f.stem for f in patches_dir.glob("*.patch"))

    results = {
        "base_dir": base_dir,
        "dry_run": dry_run,
        "milestones": {},
        "summary": {"total": 0, "needs_fix": 0, "fixed": 0, "failed": 0},
    }

    for m in milestones:
        results["summary"]["total"] += 1

        # First analyze
        analysis = analyze_patch_test_hunks(
            base_dir, m, patches_subdir="milestone_patches", ref_tag=f"milestone-{m}-start"
        )

        if "error" in analysis:
            results["milestones"][m] = {"error": analysis["error"]}
            continue

        test_count = len(analysis.get("test_hunks", []))
        if test_count == 0:
            results["milestones"][m] = {"status": "clean", "test_hunks": 0}
            continue

        results["summary"]["needs_fix"] += 1

        # Fix
        fix_result = fix_milestone_patch(base_dir, m, dry_run=dry_run)
        results["milestones"][m] = fix_result

        if fix_result.get("fixed"):
            results["summary"]["fixed"] += 1
        elif fix_result.get("errors"):
            results["summary"]["failed"] += 1

    return results


if __name__ == "__main__":
    import argparse
    import json

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")

    parser = argparse.ArgumentParser(description="Fix patches by removing TEST hunks")
    parser.add_argument("base_dir", help="Path to baseline directory")
    parser.add_argument("--milestone", help="Fix specific milestone only")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only")
    parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    results = fix_baseline_patches(args.base_dir, milestone=args.milestone, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\nBaseline: {results['base_dir']}")
        print("=" * 70)

        for m, r in results["milestones"].items():
            if r.get("status") == "clean":
                print(f"  {m}: ✓ clean")
            elif r.get("error"):
                print(f"  {m}: ✗ error - {r['error']}")
            else:
                status = "✓ fixed" if r.get("fixed") else "✗ failed"
                print(f"  {m}: {status} ({r.get('test_hunks_found', 0)} TEST hunks)")
                for step in r.get("steps", []):
                    print(f"    {step}")
                for err in r.get("errors", []):
                    print(f"    ERROR: {err}")

        print("=" * 70)
        s = results["summary"]
        print(f"Summary: {s['total']} total, {s['needs_fix']} need fix, {s['fixed']} fixed, {s['failed']} failed")
