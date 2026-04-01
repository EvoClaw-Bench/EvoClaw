#!/usr/bin/env python3
"""
Main entry point for fixing test/src separation in milestone patches.

Usage:
    python -m harness.prepare_repo.split_test_patches.main \\
        /path/to/baseline_004 \\
        --suffix _v2 \\
        [--milestone milestone_seed_xxx_1] \\
        [--dry-run]
"""

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .analyzer import analyze_baseline, get_test_hunks_detail, print_analysis
from .prompts import generate_apply_test_prompt, generate_retry_prompt
from .agent_runner import run_test_apply_agent
from .patch_generator import (
    create_old_start_tag,
    checkout_tag,
    update_start_tag,
    generate_milestone_patches,
    copy_unchanged_patches,
    run_git_command,
    update_metadata_for_milestones,
)

from .verify_test_separation import analyze_patch_test_hunks

logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def verify_separation(new_dir: Path, milestone: Optional[str] = None) -> dict:
    """
    Verify test/src separation using verify_test_separation.py check command.

    Args:
        new_dir: Path to the new baseline directory
        milestone: Optional specific milestone to verify

    Returns:
        Dict with verification results:
        - all_pass: bool
        - failed_milestones: {milestone: reason}
        - failure_details: {milestone: {src_patch_issues, test_patch_issues, src_in_test_patch, ...}}
        - details: full check results
    """
    base_path = Path(new_dir)
    src_patches_dir = base_path / "milestone_patches"
    test_patches_dir = base_path / "milestone_patches" / "start_diff_patches"

    result = {
        "all_pass": True,
        "failed_milestones": {},
        "failure_details": {},  # Detailed failure info for retry
        "details": {},
    }

    if not src_patches_dir.exists():
        result["all_pass"] = False
        result["failed_milestones"]["_error"] = f"milestone_patches not found: {src_patches_dir}"
        return result

    # Get milestones to check
    if milestone:
        milestones = [milestone]
    else:
        src_milestones = set(f.stem for f in src_patches_dir.glob("*.patch"))
        test_milestones = set(f.stem for f in test_patches_dir.glob("*.patch")) if test_patches_dir.exists() else set()
        milestones = sorted(src_milestones | test_milestones)

    for m in milestones:
        m_result = {"src_patch": {}, "test_patch": {}}
        m_failure = {
            "src_patch_issues": [],
            "test_patch_issues": [],
            "test_in_src_patch": [],  # Test files left in src patch
            "src_in_test_patch": [],  # Src files incorrectly in test patch
            "mixed_not_split": [],  # Mixed hunks not properly split
        }

        # Check milestone_patches (should have 0 test hunks, 0 mixed hunks)
        src_patch_path = src_patches_dir / f"{m}.patch"
        if src_patch_path.exists():
            src_analysis = analyze_patch_test_hunks(str(base_path), m, patches_subdir="milestone_patches")
            if "error" not in src_analysis:
                test_count = src_analysis["summary"].get("test_hunks", 0)
                mixed_count = src_analysis["summary"].get("mixed_hunks", 0)
                m_result["src_patch"] = {
                    "test_hunks": test_count,
                    "mixed_hunks": mixed_count,
                    "pass": test_count == 0 and mixed_count == 0,
                }
                if not m_result["src_patch"]["pass"]:
                    result["all_pass"] = False
                    result["failed_milestones"][
                        m
                    ] = f"milestone_patches has {test_count} test, {mixed_count} mixed hunks"

                    # Collect detailed failure info
                    for hunk in src_analysis.get("test_hunks", []):
                        m_failure["src_patch_issues"].append(
                            f"Test hunk in {hunk['file']} lines {hunk.get('new_lines', 'N/A')}"
                        )
                        m_failure["test_in_src_patch"].append(hunk["file"])
                    for hunk in src_analysis.get("mixed_hunks", []):
                        m_failure["src_patch_issues"].append(
                            f"Mixed hunk in {hunk['file']} lines {hunk.get('new_lines', 'N/A')}"
                        )
                        m_failure["mixed_not_split"].append(hunk["file"])

        # Check start_diff_patches (should have 0 src hunks, 0 mixed hunks)
        test_patch_path = test_patches_dir / f"{m}.patch"
        if test_patch_path.exists():
            old_start_tag = f"milestone-{m}-start-old"
            test_analysis = analyze_patch_test_hunks(
                str(base_path), m, patches_subdir="milestone_patches/start_diff_patches", ref_tag=old_start_tag
            )
            if "error" not in test_analysis:
                src_count = test_analysis["summary"].get("src_hunks", 0)
                mixed_count = test_analysis["summary"].get("mixed_hunks", 0)
                m_result["test_patch"] = {
                    "src_hunks": src_count,
                    "mixed_hunks": mixed_count,
                    "pass": src_count == 0 and mixed_count == 0,
                }
                if not m_result["test_patch"]["pass"]:
                    result["all_pass"] = False
                    if m not in result["failed_milestones"]:
                        result["failed_milestones"][
                            m
                        ] = f"start_diff_patches has {src_count} src, {mixed_count} mixed hunks"
                    else:
                        result["failed_milestones"][
                            m
                        ] += f"; start_diff_patches has {src_count} src, {mixed_count} mixed hunks"

                    # Collect detailed failure info
                    for hunk in test_analysis.get("src_hunks", []):
                        m_failure["test_patch_issues"].append(
                            f"Src hunk in {hunk['file']} lines {hunk.get('new_lines', 'N/A')}"
                        )
                        m_failure["src_in_test_patch"].append(hunk["file"])
                    for hunk in test_analysis.get("mixed_hunks", []):
                        m_failure["test_patch_issues"].append(
                            f"Mixed hunk in {hunk['file']} lines {hunk.get('new_lines', 'N/A')}"
                        )
                        if hunk["file"] not in m_failure["mixed_not_split"]:
                            m_failure["mixed_not_split"].append(hunk["file"])

        result["details"][m] = m_result
        if m in result["failed_milestones"]:
            result["failure_details"][m] = m_failure

    return result


def retry_failed_milestone(
    new_dir: Path,
    base_path: Path,
    milestone: str,
    failure_details: dict,
    attempt_number: int,
    log_dir: Path,
    skip_agent: bool = False,
    timeout_ms: int = 600_000,
) -> bool:
    """
    Retry fixing a failed milestone with enhanced prompt.

    Args:
        new_dir: Path to new baseline directory
        base_path: Path to original baseline directory
        milestone: Milestone ID to retry
        failure_details: Dict with failure info from verification
        attempt_number: Current attempt number (2, 3, ...)
        log_dir: Directory for logs
        skip_agent: Skip agent execution
        timeout_ms: Timeout in milliseconds for agent execution (default 10 minutes)

    Returns:
        True if retry succeeded, False otherwise
    """
    new_testbed = new_dir / "testbed"
    start_old_tag = f"milestone-{milestone}-start-old"

    logger.info(f"\n--- Retry {attempt_number} for: {milestone} ---")

    try:
        # Reset to old start tag
        logger.info(f"Resetting to {start_old_tag}")
        checkout_tag(new_testbed, start_old_tag)

        # Reset any uncommitted changes
        run_git_command(new_testbed, ["reset", "--hard", start_old_tag])

        # Get test hunks detail from original baseline
        detail = get_test_hunks_detail(str(base_path), milestone)

        if not detail["test_hunks"] and not detail["mixed_hunks"]:
            logger.warning(f"No test/mixed hunks found for {milestone}")
            return False

        # Generate retry prompt with failure details
        prompt = generate_retry_prompt(
            testbed_path=str(new_testbed),
            milestone=milestone,
            start_old_tag=start_old_tag,
            test_hunks=detail["test_hunks"],
            mixed_hunks=detail["mixed_hunks"],
            patch_content=detail["patch_content"],
            attempt_number=attempt_number,
            failure_details=failure_details,
        )

        if skip_agent:
            logger.info(f"[SKIP AGENT] Would retry agent for {milestone}")
            return False

        # Run Claude agent
        logger.info(f"Running Claude agent (attempt {attempt_number}) for {milestone}...")
        success, output = run_test_apply_agent(
            testbed_path=new_testbed,
            prompt=prompt,
            log_dir=log_dir,
            milestone=f"{milestone}_retry{attempt_number - 1}",
            timeout_ms=timeout_ms,
        )

        if not success:
            logger.error(f"Agent retry failed for {milestone}: {output}")
            return False

        logger.info(f"Agent completed for {milestone}")

        # Update start tag to current HEAD
        update_start_tag(new_testbed, milestone)

        # Regenerate patches
        from .patch_generator import generate_milestone_patches

        patch_results = generate_milestone_patches(new_testbed, new_dir, milestone)

        if patch_results["errors"]:
            for err in patch_results["errors"]:
                logger.error(f"Patch error for {milestone}: {err}")
            return False

        return True

    except Exception as e:
        logger.error(f"Retry failed for {milestone}: {e}")
        return False


def retry_single_milestone(
    target_dir: str,
    milestone: str,
    max_retries: int = 3,
    skip_agent: bool = False,
    timeout_ms: int = 600_000,
) -> dict:
    """
    Retry a single milestone on an existing directory.

    This is a lightweight mode that:
    1. Verifies the current state
    2. Retries the specified milestone if it fails verification
    3. Does NOT copy any files or create new directories

    Args:
        target_dir: Path to existing target directory (e.g., baseline_004__v2)
        milestone: Milestone ID to retry
        max_retries: Maximum retry attempts
        skip_agent: Skip agent execution
        timeout_ms: Timeout in milliseconds for agent execution (default 10 minutes)

    Returns:
        Dict with retry results
    """
    target_path = Path(target_dir)
    log_dir = target_path / "milestone_patches" / "fix_logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Find the original baseline directory (remove suffix)
    # e.g., baseline_004__v2 -> baseline_004
    base_name = target_path.name
    for suffix in ["_v2", "_v3", "_v4", "_fixed"]:
        if base_name.endswith(suffix):
            original_name = base_name[: -len(suffix)]
            break
    else:
        original_name = base_name + "_original"

    base_path = target_path.parent / original_name

    results = {"milestone": milestone, "attempts": [], "success": False, "errors": []}

    logger.info(f"Retry mode for milestone: {milestone}")
    logger.info(f"Target directory: {target_path}")
    logger.info(f"Original baseline: {base_path}")

    # Find the highest existing retry number for this milestone
    existing_retry_num = 0
    for log_file in log_dir.glob(f"{milestone}_retry*.log"):
        # Extract retry number from filename like "M002_retry3.log"
        match = re.search(rf"{re.escape(milestone)}_retry(\d+)\.log$", log_file.name)
        if match:
            num = int(match.group(1))
            existing_retry_num = max(existing_retry_num, num)

    if existing_retry_num > 0:
        logger.info(
            f"Found existing logs up to retry{existing_retry_num}, continuing from retry{existing_retry_num + 1}"
        )

    for attempt in range(1, max_retries + 1):
        # Calculate the actual retry number (continue from existing)
        retry_num = existing_retry_num + attempt

        logger.info("\n" + "=" * 70)
        logger.info(f"Attempt {attempt}/{max_retries} for {milestone} (retry{retry_num})")
        logger.info("=" * 70)

        # Check if patch file exists
        src_patch_path = target_path / "milestone_patches" / f"{milestone}.patch"
        patch_missing = not src_patch_path.exists()

        # If patch missing, check if we just need to generate it (tags already moved)
        if patch_missing:
            logger.warning(f"Patch file missing: {src_patch_path}")
            # Check if start-old and start point to different commits
            start_old_tag = f"milestone-{milestone}-start-old"
            start_tag = f"milestone-{milestone}-start"
            testbed_path = target_path / "testbed"
            try:
                from .patch_generator import run_git_command, generate_milestone_patches

                old_sha = run_git_command(testbed_path, ["rev-parse", start_old_tag]).stdout.strip()
                new_sha = run_git_command(testbed_path, ["rev-parse", start_tag]).stdout.strip()
                if old_sha != new_sha:
                    # Tags already moved, just generate patch
                    logger.info(f"Tags already moved ({old_sha[:8]} -> {new_sha[:8]}), generating patch...")
                    patch_result = generate_milestone_patches(testbed_path, target_path, milestone)
                    if not patch_result["errors"]:
                        logger.info(f"Patch generated successfully")
                        # Re-verify after generating patch
                        verification = verify_separation(str(target_path), milestone)
                        if verification["all_pass"]:
                            patch_missing = False
                        else:
                            logger.warning(f"Patch generated but verification failed")
                    else:
                        logger.error(f"Failed to generate patch: {patch_result['errors']}")
            except Exception as e:
                logger.warning(f"Could not check tags: {e}")

        # Verify current state
        verification = verify_separation(str(target_path), milestone)

        # Only pass if verification passes AND patch exists
        if verification["all_pass"] and not patch_missing:
            logger.info(f"✅ {milestone} passed verification!")
            results["success"] = True
            results["attempts"].append({"attempt": attempt, "result": "passed"})
            break

        if patch_missing:
            logger.warning("Patch file missing and tags not moved, must run agent")

        failure_details = verification.get("failure_details", {}).get(milestone, {})
        logger.warning(f"Verification failed: {verification.get('failed_milestones', {}).get(milestone, 'unknown')}")

        # Retry
        success = retry_failed_milestone(
            new_dir=target_path,
            base_path=base_path,
            milestone=milestone,
            failure_details=failure_details,
            attempt_number=retry_num + 1,  # +1 so log filename becomes _retry{retry_num}
            log_dir=log_dir,
            skip_agent=skip_agent,
            timeout_ms=timeout_ms,
        )

        results["attempts"].append({"attempt": attempt, "result": "retry_completed" if success else "retry_failed"})

        if not success:
            logger.error(f"Retry failed for attempt {attempt}")
            results["errors"].append(f"Attempt {attempt} failed")

    # Final verification - also check patch exists
    final_verification = verify_separation(str(target_path), milestone)
    src_patch_path = target_path / "milestone_patches" / f"{milestone}.patch"
    final_patch_exists = src_patch_path.exists()

    if final_verification["all_pass"] and final_patch_exists:
        logger.info(f"\n✅ SUCCESS: {milestone} now passes verification!")
        results["success"] = True

        # Update metadata.json for this milestone
        if not skip_agent:
            logger.info(f"Updating metadata.json for {milestone}...")
            metadata_result = update_metadata_for_milestones(
                baseline_dir=target_path, successful_milestones=[milestone]
            )
            results["metadata_update"] = metadata_result

            if metadata_result["errors"]:
                for err in metadata_result["errors"]:
                    logger.error(f"Metadata update error: {err}")
                    results["errors"].append(f"Metadata: {err}")
            elif metadata_result["updated"]:
                logger.info(f"Updated metadata.json for {milestone}")
    else:
        logger.error(f"\n❌ FAILED: {milestone} still fails verification")
        if not final_patch_exists:
            logger.error(f"Patch file missing: {src_patch_path}")
            results["errors"].append("Patch file not generated")
        else:
            logger.error(f"Issues: {final_verification.get('failed_milestones', {}).get(milestone, 'unknown')}")
            results["errors"].append("Final verification failed")

    return results


def fix_baseline(
    baseline_dir: str,
    suffix: str = "_v2",
    milestone: Optional[str] = None,
    dry_run: bool = False,
    skip_agent: bool = False,
    max_retries: int = 3,
    force: bool = False,
    selected_milestones_file: Optional[str] = None,
    timeout_ms: int = 600_000,
) -> dict:
    """
    Fix test/src separation for a baseline.

    Args:
        baseline_dir: Path to baseline directory (e.g., .../baseline_004)
        suffix: Suffix for new directory (default: "_v2")
        milestone: Optional specific milestone to fix
        dry_run: If True, only analyze without making changes
        skip_agent: If True, skip agent execution (for testing)
        max_retries: Maximum number of retry attempts for failed milestones (default: 2)
        force: If True, delete existing target directory and recreate
        selected_milestones_file: Optional file containing list of milestones to process
        timeout_ms: Timeout in milliseconds for agent execution (default 10 minutes)

    Returns:
        Dict with fix results
    """
    base_path = Path(baseline_dir).resolve()
    new_dir = Path(str(base_path) + suffix)
    testbed_path = base_path / "testbed"

    results = {
        "baseline_dir": str(base_path),
        "new_dir": str(new_dir),
        "analyzed": [],
        "fixed": [],
        "skipped": [],
        "errors": [],
    }

    # Step 1: Analyze baseline
    logger.info("=" * 70)
    logger.info("Step 1: Analyzing baseline for test/src separation issues")
    logger.info("=" * 70)

    try:
        analysis = analyze_baseline(str(base_path), milestone)
        print_analysis(analysis, verbose=True)
        results["analyzed"] = list(analysis.milestone_details.keys())
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        results["errors"].append(f"Analysis failed: {e}")
        return results

    # Parse selected milestones file if provided
    selected_milestones: Optional[set] = None
    if selected_milestones_file:
        selected_milestones_path = Path(selected_milestones_file)
        if not selected_milestones_path.exists():
            logger.error(f"Selected milestones file not found: {selected_milestones_file}")
            results["errors"].append(f"File not found: {selected_milestones_file}")
            return results

        selected_milestones = set()
        with open(selected_milestones_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Handle format: "     1→milestone_seed_119407d_1_sub-01"
                if "→" in line:
                    parts = line.split("→", 1)
                    if len(parts) == 2 and parts[1].strip():
                        selected_milestones.add(parts[1].strip())
                else:
                    selected_milestones.add(line)

        logger.info(f"Loaded {len(selected_milestones)} milestones from {selected_milestones_file}")

        # Filter analysis results
        original_problematic = len(analysis.problematic_milestones)
        original_clean = len(analysis.clean_milestones)

        analysis.problematic_milestones = [m for m in analysis.problematic_milestones if m in selected_milestones]
        analysis.clean_milestones = [m for m in analysis.clean_milestones if m in selected_milestones]

        logger.info(
            f"Filtered: problematic {original_problematic} -> {len(analysis.problematic_milestones)}, "
            f"clean {original_clean} -> {len(analysis.clean_milestones)}"
        )

    # Check if any milestones need fixing
    if not analysis.problematic_milestones:
        logger.info("\nNo milestones need fixing. All patches are src-only.")
        return results

    if dry_run:
        logger.info("\n[DRY RUN] Would fix the following milestones:")
        for m in analysis.problematic_milestones:
            logger.info(f"  - {m}")
        return results

    # Step 2: Copy baseline to new directory
    logger.info("\n" + "=" * 70)
    logger.info("Step 2: Creating new baseline directory")
    logger.info("=" * 70)

    if new_dir.exists():
        if force:
            logger.warning(f"Removing existing directory: {new_dir}")
            shutil.rmtree(new_dir)
        else:
            logger.error(f"New directory already exists: {new_dir}")
            logger.error("Use --force to delete and recreate")
            results["errors"].append(f"Directory exists: {new_dir}")
            return results

    logger.info(f"Copying {base_path} -> {new_dir}")
    shutil.copytree(base_path, new_dir)

    # Rename milestone_patches to milestone_patches_original
    new_patches_dir = new_dir / "milestone_patches"
    original_patches_dir = new_dir / "milestone_patches_original"
    if new_patches_dir.exists():
        logger.info(f"Renaming milestone_patches -> milestone_patches_original")
        new_patches_dir.rename(original_patches_dir)

    # Create new empty directories
    (new_dir / "milestone_patches").mkdir(exist_ok=True)
    (new_dir / "milestone_patches" / "start_diff_patches").mkdir(exist_ok=True)

    new_testbed = new_dir / "testbed"
    log_dir = new_dir / "milestone_patches" / "fix_logs"
    log_dir.mkdir(exist_ok=True)

    # Step 3: Fix each problematic milestone
    logger.info("\n" + "=" * 70)
    logger.info("Step 3: Fixing problematic milestones")
    logger.info("=" * 70)

    for m in analysis.problematic_milestones:
        logger.info(f"\n--- Fixing: {m} ---")

        try:
            # 3a. Create old start tag
            old_start_tag = create_old_start_tag(new_testbed, m)

            # 3b. Checkout to old start tag
            checkout_tag(new_testbed, old_start_tag)

            # 3c. Get test hunks detail
            # Use original baseline for analysis (patches are there)
            detail = get_test_hunks_detail(str(base_path), m)

            if not detail["test_hunks"] and not detail["mixed_hunks"]:
                logger.warning(f"No test/mixed hunks found for {m}, skipping agent")
                results["skipped"].append(m)
                continue

            # 3d. Generate prompt
            start_old_tag = f"milestone-{m}-start-old"
            prompt = generate_apply_test_prompt(
                testbed_path=str(new_testbed),
                milestone=m,
                start_old_tag=start_old_tag,
                test_hunks=detail["test_hunks"],
                mixed_hunks=detail["mixed_hunks"],
                patch_content=detail["patch_content"],
            )

            if skip_agent:
                logger.info(f"[SKIP AGENT] Would run agent for {m}")
                results["skipped"].append(m)
                continue

            # 3e. Run Claude agent
            logger.info(f"Running Claude agent for {m}...")
            success, output = run_test_apply_agent(
                testbed_path=new_testbed,
                prompt=prompt,
                log_dir=log_dir,
                milestone=m,
                timeout_ms=timeout_ms,
            )

            if not success:
                logger.error(f"Agent failed for {m}: {output}")
                results["errors"].append(f"Agent failed for {m}: {output}")
                continue

            logger.info(f"Agent completed for {m}")

            # 3f. Update start tag to current HEAD
            update_start_tag(new_testbed, m)

            # 3g. Generate new patches
            patch_results = generate_milestone_patches(new_testbed, new_dir, m)

            if patch_results["errors"]:
                for err in patch_results["errors"]:
                    logger.error(f"Patch error for {m}: {err}")
                    results["errors"].append(f"{m}: {err}")
            else:
                results["fixed"].append(m)
                logger.info(f"Successfully fixed: {m}")

        except Exception as e:
            logger.error(f"Failed to fix {m}: {e}")
            results["errors"].append(f"Failed to fix {m}: {e}")

    # Step 4: Copy unchanged milestones
    logger.info("\n" + "=" * 70)
    logger.info("Step 4: Copying unchanged milestones")
    logger.info("=" * 70)

    unchanged = analysis.clean_milestones
    if unchanged:
        copy_unchanged_patches(base_path, new_dir, unchanged)
        logger.info(f"Copied {len(unchanged)} unchanged milestones")

    # Step 5: Verify test/src separation
    logger.info("\n" + "=" * 70)
    logger.info("Step 5: Verifying test/src separation")
    logger.info("=" * 70)

    verification_result = verify_separation(new_dir, milestone)
    results["verification"] = verification_result

    if verification_result["all_pass"]:
        logger.info("Verification PASSED: All patches correctly separated")
    else:
        logger.warning("Verification FAILED: Some patches still have issues")
        for m, detail in verification_result.get("failed_milestones", {}).items():
            logger.warning(f"  - {m}: {detail}")

        # Step 5.5: Retry failed milestones
        if max_retries > 0 and not skip_agent:
            failed_milestones = list(verification_result.get("failed_milestones", {}).keys())
            retry_results = {"retried": [], "succeeded": [], "failed": []}

            for attempt in range(2, max_retries + 2):  # attempt 2, 3, ...
                if not failed_milestones:
                    break

                logger.info("\n" + "=" * 70)
                logger.info(
                    f"Step 5.{attempt-1}: Retry attempt {attempt-1} for {len(failed_milestones)} failed milestones"
                )
                logger.info("=" * 70)

                still_failed = []
                for m in failed_milestones:
                    failure_details = verification_result.get("failure_details", {}).get(m, {})
                    retry_results["retried"].append(m)

                    success = retry_failed_milestone(
                        new_dir=new_dir,
                        base_path=base_path,
                        milestone=m,
                        failure_details=failure_details,
                        attempt_number=attempt,
                        log_dir=log_dir,
                        skip_agent=skip_agent,
                        timeout_ms=timeout_ms,
                    )

                    if success:
                        # Verify this milestone again
                        m_verify = verify_separation(new_dir, m)
                        if m_verify["all_pass"]:
                            logger.info(f"Retry SUCCEEDED for {m}")
                            retry_results["succeeded"].append(m)
                            results["fixed"].append(m)
                        else:
                            logger.warning(f"Retry completed but verification still failed for {m}")
                            still_failed.append(m)
                            # Update failure details for next retry
                            verification_result["failure_details"][m] = m_verify.get("failure_details", {}).get(m, {})
                    else:
                        still_failed.append(m)

                failed_milestones = still_failed

            retry_results["failed"] = failed_milestones
            results["retry_results"] = retry_results

            # Final verification
            if failed_milestones:
                logger.warning(f"\nAfter {max_retries} retries, {len(failed_milestones)} milestones still failed:")
                for m in failed_milestones:
                    logger.warning(f"  - {m}")
                results["errors"].append(f"Verification failed for {len(failed_milestones)} milestones after retries")
            else:
                logger.info("\nAll milestones passed after retries!")
                # Update verification result
                verification_result["all_pass"] = True
                verification_result["failed_milestones"] = {}
        else:
            results["errors"].append("Verification failed")

    # Step 6: Update metadata.json for successful milestones
    if results["fixed"] and not skip_agent:
        logger.info("\n" + "=" * 70)
        logger.info("Step 6: Updating metadata.json")
        logger.info("=" * 70)

        metadata_result = update_metadata_for_milestones(baseline_dir=new_dir, successful_milestones=results["fixed"])
        results["metadata_update"] = metadata_result

        if metadata_result["errors"]:
            for err in metadata_result["errors"]:
                logger.error(f"Metadata update error: {err}")
                results["errors"].append(f"Metadata: {err}")
        else:
            logger.info(f"Updated {len(metadata_result['updated'])} milestone(s) in metadata.json")

    # Step 7: Summary
    logger.info("\n" + "=" * 70)
    logger.info("Step 7: Summary")
    logger.info("=" * 70)
    logger.info(f"New baseline: {new_dir}")
    logger.info(f"Fixed milestones: {len(results['fixed'])}")
    logger.info(f"Skipped milestones: {len(results['skipped'])}")
    logger.info(f"Verification: {'PASS' if verification_result['all_pass'] else 'FAIL'}")
    logger.info(f"Errors: {len(results['errors'])}")

    if results["errors"]:
        logger.warning("\nErrors encountered:")
        for err in results["errors"]:
            logger.warning(f"  - {err}")

    return results


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Fix test/src separation in milestone patches using Claude agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyze baseline (dry-run)
  python -m harness.prepare_repo.split_test_patches.main \\
      DATA/harness_workspace/repo/baseline_004 --dry-run

  # Fix all problematic milestones
  python -m harness.prepare_repo.split_test_patches.main \\
      DATA/harness_workspace/repo/baseline_004 --suffix _v2

  # Fix only milestones listed in a file
  python -m harness.prepare_repo.split_test_patches.main \\
      DATA/harness_workspace/repo/baseline_004 --suffix _v2 \\
      --selected-milestones-file DATA/harness_workspace/repo/baseline_004/selected_milestone_ids.txt

  # Fix specific milestone
  python -m harness.prepare_repo.split_test_patches.main \\
      DATA/harness_workspace/repo/baseline_004 \\
      --milestone milestone_seed_xxx_1 --suffix _v2
        """,
    )

    parser.add_argument("baseline_dir", help="Path to baseline directory (e.g., .../baseline_004)")
    parser.add_argument("--suffix", default="_v2", help="Suffix for new directory (default: _v2)")
    parser.add_argument("--milestone", help="Fix specific milestone only")
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, do not make changes")
    parser.add_argument("--skip-agent", action="store_true", help="Skip agent execution (for testing setup)")
    parser.add_argument(
        "--max-retries", type=int, default=3, help="Maximum retry attempts for failed milestones (default: 3)"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Timeout in seconds for agent execution (default: 600 = 10 minutes)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument(
        "--retry-only",
        action="store_true",
        help="Only retry specified milestone on existing directory (requires --milestone). "
        "The baseline_dir should point to the _v2 directory directly.",
    )
    parser.add_argument("--force", action="store_true", help="Force delete existing target directory and recreate")
    parser.add_argument(
        "--selected-milestones-file",
        help="Only process milestones listed in this file (one per line, supports '→' format)",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate --milestone usage
    if args.milestone and not args.retry_only and not args.dry_run:
        parser.error("--milestone must be used with --dry-run or --retry-only")

    # Handle --retry-only mode
    if args.retry_only:
        if not args.milestone:
            parser.error("--retry-only requires --milestone")

        results = retry_single_milestone(
            target_dir=args.baseline_dir,
            milestone=args.milestone,
            max_retries=args.max_retries,
            skip_agent=args.skip_agent,
            timeout_ms=args.timeout * 1000,  # Convert seconds to milliseconds
        )
    else:
        results = fix_baseline(
            baseline_dir=args.baseline_dir,
            suffix=args.suffix,
            milestone=args.milestone,
            dry_run=args.dry_run,
            skip_agent=args.skip_agent,
            max_retries=args.max_retries,
            force=args.force,
            selected_milestones_file=args.selected_milestones_file,
            timeout_ms=args.timeout * 1000,  # Convert seconds to milliseconds
        )

    # Exit with error code if there were errors
    if results.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    main()
