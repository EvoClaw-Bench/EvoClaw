#!/usr/bin/env python3
"""
Generate patches after test/src separation.

Creates:
- milestone_patches/{m}.patch: start -> end (src only)
- milestone_patches/start_diff_patches/{m}.patch: start-old -> start (test only)
"""

import subprocess
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def run_git_command(testbed: Path, args: list, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command in the testbed."""
    cmd = ["git"] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=testbed, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"Git command failed: {' '.join(cmd)}\n{result.stderr}")
    return result


def tag_exists(testbed: Path, tag_name: str) -> bool:
    """Check if a git tag exists."""
    result = run_git_command(testbed, ["tag", "-l", tag_name], check=False)
    return tag_name in result.stdout.strip().split("\n")


def create_old_start_tag(testbed: Path, milestone: str) -> str:
    """
    Create the old start tag by copying current start tag.

    Args:
        testbed: Path to testbed git repository
        milestone: Milestone ID

    Returns:
        Name of the old start tag
    """
    start_tag = f"milestone-{milestone}-start"
    old_start_tag = f"milestone-{milestone}-start-old"

    # Check if start tag exists
    if not tag_exists(testbed, start_tag):
        raise ValueError(f"Start tag not found: {start_tag}")

    # Delete old-start tag if it exists
    if tag_exists(testbed, old_start_tag):
        logger.info(f"Removing existing tag: {old_start_tag}")
        run_git_command(testbed, ["tag", "-d", old_start_tag])

    # Create old-start tag pointing to same commit as start tag
    logger.info(f"Creating tag: {old_start_tag} -> {start_tag}")
    run_git_command(testbed, ["tag", old_start_tag, start_tag])

    return old_start_tag


def checkout_tag(testbed: Path, tag_name: str):
    """Checkout a specific tag."""
    logger.info(f"Checking out: {tag_name}")
    run_git_command(testbed, ["checkout", tag_name])


def update_start_tag(testbed: Path, milestone: str):
    """
    Update the start tag to point to current HEAD.

    This is called after the agent has committed test changes.
    """
    start_tag = f"milestone-{milestone}-start"

    # Delete current start tag
    if tag_exists(testbed, start_tag):
        run_git_command(testbed, ["tag", "-d", start_tag])

    # Create new start tag at HEAD
    logger.info(f"Updating tag: {start_tag} -> HEAD")
    run_git_command(testbed, ["tag", start_tag])


def generate_patch(testbed: Path, from_ref: str, to_ref: str, output_path: Path) -> bool:
    """
    Generate a patch file from one ref to another.

    Args:
        testbed: Path to testbed git repository
        from_ref: Source git ref (tag/commit)
        to_ref: Target git ref (tag/commit)
        output_path: Path to save the patch file

    Returns:
        True if patch was created, False if no changes
    """
    result = run_git_command(testbed, ["diff", from_ref, to_ref], check=False)

    if result.returncode != 0:
        raise RuntimeError(f"Failed to generate diff: {result.stderr}")

    patch_content = result.stdout

    if not patch_content.strip():
        logger.info(f"No changes between {from_ref} and {to_ref}")
        return False

    # Ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write patch
    output_path.write_text(patch_content, encoding="utf-8")
    logger.info(f"Generated patch: {output_path} ({len(patch_content)} bytes)")

    return True


def generate_milestone_patches(testbed: Path, output_dir: Path, milestone: str) -> dict:
    """
    Generate both milestone and start_diff patches for a milestone.

    Assumes:
    - milestone-{m}-start-old: Original start (pure src baseline)
    - milestone-{m}-start: New start (with test code applied)
    - milestone-{m}-end: Final state

    Creates:
    - milestone_patches/{m}.patch: start -> end (src only)
    - milestone_patches/start_diff_patches/{m}.patch: start-old -> start (test only)

    Args:
        testbed: Path to testbed git repository
        output_dir: Path to output directory (new baseline)
        milestone: Milestone ID

    Returns:
        Dict with patch generation results
    """
    start_old_tag = f"milestone-{milestone}-start-old"
    start_tag = f"milestone-{milestone}-start"
    end_tag = f"milestone-{milestone}-end"

    results = {"milestone": milestone, "milestone_patch": None, "start_diff_patch": None, "errors": []}

    # Validate tags exist
    for tag in [start_old_tag, start_tag, end_tag]:
        if not tag_exists(testbed, tag):
            results["errors"].append(f"Tag not found: {tag}")

    if results["errors"]:
        return results

    # Generate milestone_patches/{m}.patch (start -> end, src only)
    milestone_patch_path = output_dir / "milestone_patches" / f"{milestone}.patch"
    try:
        if generate_patch(testbed, start_tag, end_tag, milestone_patch_path):
            results["milestone_patch"] = str(milestone_patch_path)
    except Exception as e:
        results["errors"].append(f"Failed to generate milestone patch: {e}")

    # Generate milestone_patches/start_diff_patches/{m}.patch (start-old -> start, test only)
    start_diff_path = output_dir / "milestone_patches" / "start_diff_patches" / f"{milestone}.patch"
    try:
        if generate_patch(testbed, start_old_tag, start_tag, start_diff_path):
            results["start_diff_patch"] = str(start_diff_path)
    except Exception as e:
        results["errors"].append(f"Failed to generate start_diff patch: {e}")

    return results


def copy_unchanged_patches(src_dir: Path, dst_dir: Path, milestones: list):
    """
    Copy patches for milestones that don't need fixing.

    Args:
        src_dir: Source baseline directory
        dst_dir: Destination baseline directory
        milestones: List of milestone IDs to copy
    """
    import shutil

    for m in milestones:
        # Copy milestone patch
        src_patch = src_dir / "milestone_patches" / f"{m}.patch"
        if src_patch.exists():
            dst_patch = dst_dir / "milestone_patches" / f"{m}.patch"
            dst_patch.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_patch, dst_patch)
            logger.info(f"Copied unchanged patch: {m}")

        # Copy start_diff patch if exists
        src_diff = src_dir / "milestone_patches" / "start_diff_patches" / f"{m}.patch"
        if src_diff.exists():
            dst_diff = dst_dir / "milestone_patches" / "start_diff_patches" / f"{m}.patch"
            dst_diff.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_diff, dst_diff)


def get_tag_sha(testbed: Path, tag_name: str) -> Optional[str]:
    """
    Get the SHA for a git tag.

    Args:
        testbed: Path to testbed git repository
        tag_name: Name of the tag

    Returns:
        SHA string or None if tag doesn't exist
    """
    if not tag_exists(testbed, tag_name):
        return None

    result = run_git_command(testbed, ["rev-parse", tag_name], check=False)

    if result.returncode != 0:
        return None

    return result.stdout.strip()


def update_metadata_for_milestones(baseline_dir: Path, successful_milestones: list) -> dict:
    """
    Update metadata.json for successfully processed milestones.

    For each successful milestone:
    - Update commit_sha_start to the new start tag SHA
    - Add commit_sha_start_original with the original SHA (from start-old tag)

    Args:
        baseline_dir: Path to baseline directory (new _v2 directory)
        successful_milestones: List of milestone IDs that were successfully processed

    Returns:
        Dict with update results:
        - updated: list of updated milestone IDs
        - skipped: list of skipped milestone IDs (tag not found, etc.)
        - errors: list of error messages
    """
    import json

    results = {"updated": [], "skipped": [], "errors": []}

    if not successful_milestones:
        logger.info("No milestones to update in metadata")
        return results

    metadata_path = baseline_dir / "metadata.json"
    testbed_path = baseline_dir / "testbed"

    if not metadata_path.exists():
        results["errors"].append(f"metadata.json not found: {metadata_path}")
        return results

    if not testbed_path.exists():
        results["errors"].append(f"testbed not found: {testbed_path}")
        return results

    # Read metadata
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception as e:
        results["errors"].append(f"Failed to read metadata.json: {e}")
        return results

    # Update each successful milestone
    milestones_list = metadata.get("milestones", [])
    updated_count = 0

    for m_data in milestones_list:
        m_id = m_data.get("id")
        if m_id not in successful_milestones:
            continue

        # Get tag names
        start_tag = f"milestone-{m_id}-start"
        start_old_tag = f"milestone-{m_id}-start-old"

        # Get SHAs
        new_start_sha = get_tag_sha(testbed_path, start_tag)
        original_start_sha = get_tag_sha(testbed_path, start_old_tag)

        if not new_start_sha:
            logger.warning(f"Start tag not found for {m_id}, skipping")
            results["skipped"].append(m_id)
            continue

        if not original_start_sha:
            logger.warning(f"Start-old tag not found for {m_id}, skipping")
            results["skipped"].append(m_id)
            continue

        # Update metadata
        old_sha = m_data.get("commit_sha_start")
        m_data["commit_sha_start"] = new_start_sha
        m_data["commit_sha_start_original"] = original_start_sha

        logger.info(f"Updated {m_id}: commit_sha_start {old_sha[:8]}... -> {new_start_sha[:8]}...")
        results["updated"].append(m_id)
        updated_count += 1

    # Write updated metadata
    if updated_count > 0:
        try:
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            logger.info(f"Saved metadata.json with {updated_count} milestone(s) updated")
        except Exception as e:
            results["errors"].append(f"Failed to write metadata.json: {e}")

    return results


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Generate patches for a milestone")
    parser.add_argument("testbed", help="Path to testbed git repository")
    parser.add_argument("output_dir", help="Path to output directory")
    parser.add_argument("milestone", help="Milestone ID")

    args = parser.parse_args()

    results = generate_milestone_patches(Path(args.testbed), Path(args.output_dir), args.milestone)

    print(f"Results: {results}")
