#!/usr/bin/env python3
"""
Rust test region filtering utilities.

This module provides functions to replace agent-written test code with
ground truth tests in Rust source files within Docker containers.

The typical use case is during e2e evaluation:
1. Agent's src files are copied to evaluation container (may contain agent-written tests)
2. This module removes agent's test regions and appends GT test regions
3. Tests are run against GT tests, not agent-written tests

Only processes .rs files that were part of the filtered src snapshot.
"""

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _run_docker_exec(container_name: str, command: str, check: bool = True) -> Tuple[bool, str, str]:
    """
    Run a command in Docker container.

    Args:
        container_name: Name of the Docker container
        command: Command to run
        check: If True, raise on non-zero exit code

    Returns:
        Tuple of (success, stdout, stderr)
    """
    cmd = ["docker", "exec", container_name, "bash", "-c", command]
    result = subprocess.run(cmd, capture_output=True, text=True)

    if check and result.returncode != 0:
        return False, result.stdout, result.stderr

    return result.returncode == 0, result.stdout, result.stderr


def _read_file_from_container(container_name: str, file_path: str) -> Optional[str]:
    """
    Read a file from the container.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file in container (relative to /testbed)

    Returns:
        File content or None if file doesn't exist
    """
    success, stdout, stderr = _run_docker_exec(container_name, f"cat /testbed/{file_path}", check=False)

    if not success:
        return None

    return stdout


def _read_file_from_git_ref(container_name: str, file_path: str, ref: str) -> Optional[str]:
    """
    Read a file from a git ref in the container.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file (relative to repo root)
        ref: Git ref (tag, branch, commit)

    Returns:
        File content or None if file doesn't exist at that ref
    """
    success, stdout, stderr = _run_docker_exec(
        container_name, f"cd /testbed && git show {ref}:{file_path}", check=False
    )

    if not success:
        return None

    return stdout


def _write_file_to_container(
    container_name: str,
    file_path: str,
    content: str,
    owner: str = "fakeroot:fakeroot",
    mode: str = "644",
) -> bool:
    """
    Write a file to the container.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file in container (relative to /testbed)
        content: File content to write
        owner: Owner in "user:group" format (default: fakeroot:fakeroot)
        mode: File permissions (default: 644)

    Returns:
        True if successful
    """
    # Write to temp file on host, then docker cp
    with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False) as f:
        f.write(content)
        temp_path = f.name

    try:
        # Copy to container
        cmd = ["docker", "cp", temp_path, f"{container_name}:/testbed/{file_path}"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            logger.error(f"Failed to write file to container: {result.stderr}")
            return False

        # Restore ownership (docker cp uses host user's uid/gid)
        chown_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            "/testbed",
            container_name,
            "chown",
            owner,
            file_path,
        ]
        subprocess.run(chown_cmd, capture_output=True)

        # Set permissions
        chmod_cmd = [
            "docker",
            "exec",
            "--user",
            "root",
            "-w",
            "/testbed",
            container_name,
            "chmod",
            mode,
            file_path,
        ]
        subprocess.run(chmod_cmd, capture_output=True)

        return True
    finally:
        Path(temp_path).unlink(missing_ok=True)


def find_test_ranges_from_content(content: str, file_path: str, only_root_level: bool = True) -> List[Tuple[int, int]]:
    """
    Find test code ranges in Rust file content.

    This is a wrapper that uses the test_detector module.

    Args:
        content: File content as string
        file_path: File path (used to determine if it's a .rs file)
        only_root_level: If True, only return test regions at file root level.
                         Test regions nested inside mod/impl/trait blocks are excluded.
                         These cannot be safely moved to end of file.

    Returns:
        List of (start_line, end_line) tuples (1-indexed, inclusive)
    """
    if not file_path.endswith(".rs"):
        return []

    from harness.prepare_repo.split_test_patches.test_detector import find_test_code_ranges
    import tempfile
    import os

    # Write content to temp file for ast-grep analysis
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            ranges_with_reason = find_test_code_ranges(tmp_path, only_root_level=only_root_level)

            # Filter out doc tests (must stay with the item they document)
            return [(start, end) for start, end, reason in ranges_with_reason if "doc test" not in reason]
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.warning(f"Failed to find test ranges: {e}")
        return []


def _is_doc_comment_or_empty(line: str) -> bool:
    """Check if a line is a doc comment or empty/whitespace."""
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith("///") or stripped.startswith("//!")


def _expand_range_to_include_doc_comments(lines: List[str], start: int, end: int) -> Tuple[int, int]:
    """
    Expand a range to include preceding doc comments.

    When removing a test function like:
        /// Some doc comment
        #[cfg(test)]
        fn test_foo() { ... }

    We need to also remove the doc comment, otherwise it becomes a dangling
    comment that causes compilation errors.

    Args:
        lines: All file lines (0-indexed)
        start: Start line (1-indexed)
        end: End line (1-indexed)

    Returns:
        Expanded (start, end) tuple (1-indexed)
    """
    start_idx = start - 1  # Convert to 0-indexed

    # Scan backwards from start to find doc comments
    while start_idx > 0:
        prev_line = lines[start_idx - 1]
        if _is_doc_comment_or_empty(prev_line):
            start_idx -= 1
        else:
            break

    # Skip any leading empty lines we picked up (keep them in the file)
    while start_idx < start - 1 and not lines[start_idx].strip():
        start_idx += 1

    return (start_idx + 1, end)  # Convert back to 1-indexed


def remove_test_regions(content: str, ranges: List[Tuple[int, int]]) -> str:
    """
    Remove test regions from content.

    Also removes preceding doc comments that would become dangling.

    Args:
        content: File content
        ranges: List of (start_line, end_line) tuples to remove (1-indexed, inclusive)

    Returns:
        Content with test regions removed
    """
    if not ranges:
        return content

    lines = content.split("\n")

    # Expand ranges to include doc comments, then sort descending
    expanded_ranges = [_expand_range_to_include_doc_comments(lines, start, end) for start, end in ranges]
    sorted_ranges = sorted(expanded_ranges, key=lambda x: x[0], reverse=True)

    for start, end in sorted_ranges:
        # Convert to 0-indexed
        start_idx = start - 1
        end_idx = end  # end is inclusive, so we delete up to end_idx (exclusive in slice)

        # Remove lines
        del lines[start_idx:end_idx]

    return "\n".join(lines)


def extract_test_regions(content: str, ranges: List[Tuple[int, int]]) -> str:
    """
    Extract test regions from content.

    Args:
        content: File content
        ranges: List of (start_line, end_line) tuples to extract (1-indexed, inclusive)

    Returns:
        Concatenated test regions with blank lines between them
    """
    if not ranges:
        return ""

    lines = content.split("\n")
    extracted_parts = []

    # Sort ranges by start line
    sorted_ranges = sorted(ranges, key=lambda x: x[0])

    for start, end in sorted_ranges:
        # Convert to 0-indexed
        start_idx = start - 1
        end_idx = end  # end is inclusive

        # Extract lines
        region_lines = lines[start_idx:end_idx]
        extracted_parts.append("\n".join(region_lines))

    return "\n\n".join(extracted_parts)


def merge_src_with_gt_tests(agent_content: str, gt_content: str, file_path: str) -> Tuple[str, Dict[str, int]]:
    """
    Merge agent's src code with GT test regions.

    Args:
        agent_content: Agent's file content (may contain agent-written tests)
        gt_content: Ground truth file content (contains GT tests)
        file_path: File path (for detection)

    Returns:
        Tuple of (merged_content, stats_dict)
    """
    stats = {
        "agent_test_regions_removed": 0,
        "gt_test_regions_appended": 0,
    }

    # Find test regions in both files
    # Use only_root_level=True to exclude test regions nested inside mod/impl/trait blocks
    # (e.g., nested test modules or #[cfg(test)] fn methods that cannot be moved to file end)
    agent_test_ranges = find_test_ranges_from_content(agent_content, file_path, only_root_level=True)
    gt_test_ranges = find_test_ranges_from_content(gt_content, file_path, only_root_level=True)

    stats["agent_test_regions_removed"] = len(agent_test_ranges)
    stats["gt_test_regions_appended"] = len(gt_test_ranges)

    # Remove agent test regions
    src_only = remove_test_regions(agent_content, agent_test_ranges)

    # Extract GT test regions
    gt_tests = extract_test_regions(gt_content, gt_test_ranges)

    # Merge: src + blank lines + GT tests
    if gt_tests.strip():
        # Ensure src ends with newline
        if not src_only.endswith("\n"):
            src_only += "\n"
        # Add blank line before tests
        merged = src_only + "\n" + gt_tests
    else:
        merged = src_only

    # Ensure file ends with newline
    if not merged.endswith("\n"):
        merged += "\n"

    return merged, stats


def replace_agent_tests_with_ground_truth(
    container_name: str,
    file_path: str,
    milestone_id: str,
    gt_tag_suffix: str = "end",
) -> Dict[str, any]:
    """
    Replace agent's test regions with ground truth tests for a single file.

    Args:
        container_name: Name of the Docker container
        file_path: Path to file (relative to /testbed)
        milestone_id: Milestone ID for git ref

    Returns:
        Dict with operation results
    """
    result = {
        "file": file_path,
        "success": False,
        "skipped": False,
        "reason": "",
        "agent_test_regions_removed": 0,
        "gt_test_regions_appended": 0,
    }

    # Only process .rs files
    if not file_path.endswith(".rs"):
        result["skipped"] = True
        result["reason"] = "not a Rust file"
        return result

    # Read agent's version (current state in container)
    agent_content = _read_file_from_container(container_name, file_path)
    if agent_content is None:
        result["reason"] = "failed to read agent file"
        return result

    # Read GT version from specified tag (end or start)
    # By default uses END tag (complete implementation with tests)
    # Fallback to START tag when agent code only compiles against baseline
    gt_tag = f"milestone-{milestone_id}-{gt_tag_suffix}"
    gt_content = _read_file_from_git_ref(container_name, file_path, gt_tag)

    if gt_content is None:
        # File doesn't exist in GT - might be a new file created by agent
        # Just remove agent's test blocks if any (only root-level ones)
        agent_test_ranges = find_test_ranges_from_content(agent_content, file_path, only_root_level=True)
        if agent_test_ranges:
            src_only = remove_test_regions(agent_content, agent_test_ranges)
            if _write_file_to_container(container_name, file_path, src_only):
                result["success"] = True
                result["agent_test_regions_removed"] = len(agent_test_ranges)
                result["reason"] = "new file, removed agent tests"
            else:
                result["reason"] = "failed to write file"
        else:
            result["skipped"] = True
            result["reason"] = "new file, no tests to remove"
        return result

    # Merge src with GT tests
    merged_content, stats = merge_src_with_gt_tests(agent_content, gt_content, file_path)

    # Check if any changes were made
    if stats["agent_test_regions_removed"] == 0 and stats["gt_test_regions_appended"] == 0:
        result["skipped"] = True
        result["reason"] = "no test regions in either file"
        return result

    # Write merged content back
    if _write_file_to_container(container_name, file_path, merged_content):
        result["success"] = True
        result["agent_test_regions_removed"] = stats["agent_test_regions_removed"]
        result["gt_test_regions_appended"] = stats["gt_test_regions_appended"]
    else:
        result["reason"] = "failed to write merged file"

    return result


def process_rust_files_in_container(
    container_name: str,
    milestone_id: str,
    rust_files: List[str],
    gt_tag_suffix: str = "end",
) -> Dict[str, any]:
    """
    Process all Rust files to replace agent tests with GT tests.

    Args:
        container_name: Name of the Docker container
        milestone_id: Milestone ID
        rust_files: List of .rs file paths (relative to /testbed)

    Returns:
        Dict with processing results
    """
    results = {
        "total_files": len(rust_files),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "total_agent_tests_removed": 0,
        "total_gt_tests_appended": 0,
        "details": [],
    }

    for file_path in rust_files:
        file_result = replace_agent_tests_with_ground_truth(container_name, file_path, milestone_id, gt_tag_suffix)
        results["details"].append(file_result)

        if file_result["skipped"]:
            results["skipped"] += 1
        elif file_result["success"]:
            results["processed"] += 1
            results["total_agent_tests_removed"] += file_result["agent_test_regions_removed"]
            results["total_gt_tests_appended"] += file_result["gt_test_regions_appended"]
        else:
            results["failed"] += 1

    return results


def get_rust_files_from_tar(tar_path: Path) -> List[str]:
    """
    Get list of .rs files from a tar archive.

    Args:
        tar_path: Path to tar file

    Returns:
        List of .rs file paths in the tar
    """
    import tarfile

    rust_files = []

    try:
        with tarfile.open(tar_path, "r") as tar:
            for member in tar.getmembers():
                if member.isfile() and member.name.endswith(".rs"):
                    rust_files.append(member.name)
    except Exception as e:
        logger.error(f"Failed to read tar file: {e}")

    return rust_files
