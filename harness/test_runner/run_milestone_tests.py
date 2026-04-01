#!/usr/bin/env python3
"""
Milestone Test Runner CLI

Run tests for milestone states (original, start, end) using the unified test runner framework.

Features:
  - Automatic image building if not exists
  - Two-level merging:
    1. Within-attempt: merge multiple pytest runs (default, integration, etc.) into start.json/end.json
    2. Cross-attempt: merge results from multiple retries with flaky test detection
  - Automatic test classification (start->end comparison)
  - Support for single milestone or batch processing

Config file format (test_config.json):
    [
      {
        "name": "default",
        "test_states": ["start", "end"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file}",
        "description": "Normal tests"
      },
      {
        "name": "integration",
        "test_states": ["end"],
        "test_cmd": "pytest -n {workers} --json-report --json-report-file=/output/{output_file} --integration",
        "description": "Integration tests"
      }
    ]

Output Structure:
    {output_dir}/
    ├── {milestone_id}/
    │   ├── attempt_1/
    │   │   ├── start_default.json
    │   │   ├── start_integration.json  (if configured)
    │   │   ├── start.json              # Merged from all start_*.json
    │   │   ├── end_default.json
    │   │   ├── end_integration.json    (if configured)
    │   │   ├── end.json                # Merged from all end_*.json
    │   │   ├── original.json           (if --include-original)
    │   │   └── classification.json
    │   ├── attempt_2/
    │   │   └── ...
    │   ├── {milestone_id}_classification.json  # Merged from all attempts
    │   └── test_summary.json

Usage:
    # Single milestone
    python -m harness.test_runner.run_milestone_tests M001 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # Multiple milestones
    python -m harness.test_runner.run_milestone_tests M001 M002 M003 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # All milestones
    python -m harness.test_runner.run_milestone_tests --all \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

    # With custom settings
    python -m harness.test_runner.run_milestone_tests M001 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
        --workers 16 --timeout 60 --max-retries 5

    # Force rerun (disable caching)
    python -m harness.test_runner.run_milestone_tests M001 \\
        --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
        --no-cache

Caching Behavior:
    By default, the runner caches completed attempts:
    - If attempt_N/ contains valid start.json and end.json, it will be skipped
    - Use --no-cache to force rerun all attempts

    Example scenarios:
    | Previous run          | Current run           | Behavior                                      |
    |-----------------------|-----------------------|-----------------------------------------------|
    | --max-retries 1       | --max-retries 1       | Skip attempt_1 (cached)                       |
    | --max-retries 1       | --max-retries 3       | Skip attempt_1 (cached), run attempt_2 and 3  |
    | --max-retries 3       | --max-retries 1       | Skip attempt_1 (cached)                       |
    | --max-retries 1       | --max-retries 3 --no-cache | Rerun all 3 attempts                     |
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Dict, Any, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

from .core.types import MilestoneTestConfig
from .core.docker import (
    check_image_exists,
    build_docker_image,
    cleanup_docker_image,
    DockerRunner,
)
from .core.merger import merge_outcome, is_flaky
from .core.classifier import TestClassifier
from .core.report_parser import (
    parse_test_report,
    merge_test_reports,
    convert_to_summary,
    get_file_extension,
    get_report_format,
    FRAMEWORK_CONFIG,
)
from harness.utils.pytest_report_utils import convert_pytest_report_to_summary

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)


# =============================================================================
# Pytest Report Merging (within-attempt)
# =============================================================================


def merge_pytest_json_reports(report_files: List[Path], output_file: Path, verbose: bool = False) -> bool:
    """
    Merge multiple pytest-json-report results into a single report.

    This is used when test_config.json specifies multiple test runs
    (e.g., normal tests + integration tests that use exclusive flags).

    Merge Strategy:
    - For the same test (same nodeid), prioritize non-skipped results
    - If multiple non-skipped results exist, use the first one
    - Recalculate summary statistics after merging

    Args:
        report_files: List of pytest-json-report JSON files to merge
        output_file: Output file path for merged report
        verbose: Whether to print verbose output

    Returns:
        True if merge was successful, False otherwise
    """
    # Outcome priority: lower number = higher priority (prefer actual runs over skipped)
    OUTCOME_PRIORITY = {
        "passed": 1,
        "failed": 1,
        "error": 1,
        "xfailed": 2,
        "xpassed": 2,
        "skipped": 3,
    }

    merged_tests = {}  # nodeid -> test_result
    total_duration = 0
    last_created = 0
    all_collectors = []
    seen_collector_nodeids = set()
    environment = {}
    root = ""
    valid_reports = 0

    for report_file in report_files:
        if not report_file.exists():
            if verbose:
                logger.warning(f"Report file not found: {report_file}")
            continue

        try:
            with open(report_file, "r") as f:
                report = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            if verbose:
                logger.warning(f"Failed to load report {report_file}: {e}")
            continue

        valid_reports += 1
        total_duration += report.get("duration", 0)
        last_created = max(last_created, report.get("created", 0))

        if not environment:
            environment = report.get("environment", {})
        if not root:
            root = report.get("root", "")

        # Merge collectors (deduplicate by nodeid)
        for collector in report.get("collectors", []):
            nodeid = collector.get("nodeid", "")
            if nodeid not in seen_collector_nodeids:
                seen_collector_nodeids.add(nodeid)
                all_collectors.append(collector)

        # Merge tests
        for test in report.get("tests", []):
            nodeid = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")

            if nodeid not in merged_tests:
                merged_tests[nodeid] = test
            else:
                existing = merged_tests[nodeid]
                existing_priority = OUTCOME_PRIORITY.get(existing.get("outcome", "unknown"), 3)
                new_priority = OUTCOME_PRIORITY.get(outcome, 3)

                if new_priority < existing_priority:
                    merged_tests[nodeid] = test

    if valid_reports == 0:
        if verbose:
            logger.warning("No valid report files to merge")
        return False

    # Calculate summary statistics
    summary = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "xfailed": 0,
        "xpassed": 0,
    }

    for test in merged_tests.values():
        outcome = test.get("outcome", "unknown")
        if outcome in summary:
            summary[outcome] += 1

    summary["total"] = len(merged_tests)
    summary["collected"] = len(merged_tests)

    # Build merged report
    merged_report = {
        "created": last_created,
        "duration": total_duration,
        "exitcode": 1 if summary.get("failed", 0) > 0 or summary.get("error", 0) > 0 else 0,
        "root": root,
        "environment": environment,
        "summary": summary,
        "collectors": all_collectors,
        "tests": list(merged_tests.values()),
        "_merge_info": {
            "source_files": [str(f) for f in report_files],
            "merged_at": time.time(),
            "valid_reports": valid_reports,
            "total_reports": len(report_files),
        },
    }

    try:
        with open(output_file, "w") as f:
            json.dump(merged_report, f, indent=2)
        return True
    except IOError as e:
        if verbose:
            logger.error(f"Failed to write merged report: {e}")
        return False


# =============================================================================
# Cross-attempt Result Merging
# =============================================================================


def merge_attempt_results(
    attempt_dirs: List[Path],
    verbose: bool = False,
    framework: str = "pytest",
) -> Optional[Dict[str, Any]]:
    """
    Merge test results from multiple attempts to eliminate flaky test effects.

    For each test:
    - Collect start_outcome from all attempts
    - Collect end_outcome from all attempts
    - Merge using pass-any logic (pass > skip > fail)
    - Classify based on merged outcomes

    Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha).

    Args:
        attempt_dirs: List of attempt directories containing start.json and end.json
        verbose: Whether to print verbose output
        framework: Test framework name (determines how to parse report files)

    Returns:
        Dictionary with merged classification results, or None if no valid attempts
    """
    all_start_results = []
    all_end_results = []
    valid_attempt_dirs = []

    for attempt_dir in attempt_dirs:
        start_file = attempt_dir / "start.json"
        end_file = attempt_dir / "end.json"

        if start_file.exists() and end_file.exists():
            try:
                # Use unified parser for framework-agnostic file reading
                start_data = parse_test_report(start_file, framework)
                end_data = parse_test_report(end_file, framework)
                all_start_results.append(start_data)
                all_end_results.append(end_data)
                valid_attempt_dirs.append(attempt_dir)
            except (json.JSONDecodeError, IOError) as e:
                if verbose:
                    logger.warning(f"Failed to load results from {attempt_dir}: {e}")

    if not all_start_results or not all_end_results:
        return None

    # Collect outcomes for each test from all attempts
    start_outcomes_per_test: Dict[str, List[str]] = {}
    end_outcomes_per_test: Dict[str, List[str]] = {}

    for results in all_start_results:
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            if test_id not in start_outcomes_per_test:
                start_outcomes_per_test[test_id] = []
            start_outcomes_per_test[test_id].append(outcome)

    for results in all_end_results:
        for test in results.get("tests", []):
            test_id = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")
            if test_id not in end_outcomes_per_test:
                end_outcomes_per_test[test_id] = []
            end_outcomes_per_test[test_id].append(outcome)

    # Handle collection errors from start state
    start_failed_collectors: Set[str] = set()
    for results in all_start_results:
        for collector in results.get("collectors", []):
            if collector.get("outcome") == "failed":
                nodeid = collector.get("nodeid", "")
                if nodeid:
                    start_failed_collectors.add(nodeid)

    for test_id in end_outcomes_per_test.keys():
        test_file = test_id.split("::")[0] if "::" in test_id else test_id
        if test_file in start_failed_collectors and test_id not in start_outcomes_per_test:
            start_outcomes_per_test[test_id] = ["error"]

    # Merge outcomes for each test using pass-any logic
    merged_start: Dict[str, str] = {
        test_id: merge_outcome(outcomes) for test_id, outcomes in start_outcomes_per_test.items()
    }
    merged_end: Dict[str, str] = {
        test_id: merge_outcome(outcomes) for test_id, outcomes in end_outcomes_per_test.items()
    }

    # Get all unique test IDs
    all_tests = set(merged_start.keys()) | set(merged_end.keys())

    # Classification categories (matching TestClassifier)
    classification = {
        "pass_to_pass": [],
        "pass_to_fail": [],
        "pass_to_skipped": [],
        "fail_to_pass": [],
        "fail_to_fail": [],
        "fail_to_skipped": [],
        "skipped_to_pass": [],
        "skipped_to_fail": [],
        "skipped_to_skipped": [],
        # Fine-grained categories for new/removed tests
        "none_to_pass": [],
        "none_to_fail": [],
        "none_to_skipped": [],
        "pass_to_none": [],
        "fail_to_none": [],
        "skipped_to_none": [],
        # Aggregated lists (for backward compatibility)
        "new_tests": [],
        "removed_tests": [],
    }

    def normalize_outcome(outcome: str) -> str:
        """Normalize outcome to: pass, fail, skipped."""
        if outcome == "skipped":
            return "skipped"
        elif outcome == "passed":
            return "pass"
        else:
            return "fail"

    for test_id in all_tests:
        start_outcome = merged_start.get(test_id, "missing")
        end_outcome = merged_end.get(test_id, "missing")

        if start_outcome == "missing":
            # New test: none_to_pass, none_to_fail, or none_to_skipped
            end_state = normalize_outcome(end_outcome)
            category = f"none_to_{end_state}"
            classification[category].append(test_id)
            # Also add to aggregated list
            classification["new_tests"].append({"test_id": test_id, "end_outcome": end_outcome})
            continue

        if end_outcome == "missing":
            # Removed test: pass_to_none, fail_to_none, or skipped_to_none
            start_state = normalize_outcome(start_outcome)
            category = f"{start_state}_to_none"
            classification[category].append(test_id)
            # Also add to aggregated list
            classification["removed_tests"].append({"test_id": test_id, "start_outcome": start_outcome})
            continue

        # Normalize and categorize
        start_state = normalize_outcome(start_outcome)
        end_state = normalize_outcome(end_outcome)
        category = f"{start_state}_to_{end_state}"
        classification[category].append(test_id)

    # Flaky test detection
    flaky_tests = []
    flaky_test_ids: Set[str] = set()

    for test_id in all_tests:
        start_outcomes = start_outcomes_per_test.get(test_id, [])
        end_outcomes = end_outcomes_per_test.get(test_id, [])

        start_is_flaky = is_flaky(start_outcomes)
        end_is_flaky = is_flaky(end_outcomes)

        if start_is_flaky or end_is_flaky:
            flaky_in = "both" if start_is_flaky and end_is_flaky else ("start" if start_is_flaky else "end")

            # Find category
            category = None
            for cat_name, cat_tests in classification.items():
                for t in cat_tests:
                    t_id = t["test_id"] if isinstance(t, dict) else t
                    if t_id == test_id:
                        category = cat_name
                        break
                if category:
                    break

            flaky_tests.append(
                {
                    "test_id": test_id,
                    "category": category,
                    "flaky_in": flaky_in,
                    "start_outcomes": start_outcomes,
                    "end_outcomes": end_outcomes,
                    "start_merged": merged_start.get(test_id, "missing"),
                    "end_merged": merged_end.get(test_id, "missing"),
                }
            )
            flaky_test_ids.add(test_id)

    # Generate stable_classification (excluding flaky tests)
    stable_classification = {}
    for cat_name, cat_tests in classification.items():
        stable_tests = []
        for t in cat_tests:
            t_id = t["test_id"] if isinstance(t, dict) else t
            if t_id not in flaky_test_ids:
                stable_tests.append(t)
        stable_classification[cat_name] = stable_tests

    # Calculate statistics (flat structure matching TestClassifier)
    stats = {
        # Category counts (9 standard transitions)
        "pass_to_pass": len(classification["pass_to_pass"]),
        "pass_to_fail": len(classification["pass_to_fail"]),
        "pass_to_skipped": len(classification["pass_to_skipped"]),
        "fail_to_pass": len(classification["fail_to_pass"]),
        "fail_to_fail": len(classification["fail_to_fail"]),
        "fail_to_skipped": len(classification["fail_to_skipped"]),
        "skipped_to_pass": len(classification["skipped_to_pass"]),
        "skipped_to_fail": len(classification["skipped_to_fail"]),
        "skipped_to_skipped": len(classification["skipped_to_skipped"]),
        # Fine-grained counts for new tests (none_to_*)
        "none_to_pass": len(classification["none_to_pass"]),
        "none_to_fail": len(classification["none_to_fail"]),
        "none_to_skipped": len(classification["none_to_skipped"]),
        # Fine-grained counts for removed tests (*_to_none)
        "pass_to_none": len(classification["pass_to_none"]),
        "fail_to_none": len(classification["fail_to_none"]),
        "skipped_to_none": len(classification["skipped_to_none"]),
        # Aggregated counts (for backward compatibility)
        "new_tests": len(classification["new_tests"]),
        "removed_tests": len(classification["removed_tests"]),
        # Totals
        "total_before": len(all_tests) - len(classification["new_tests"]),
        "total_after": len(all_tests) - len(classification["removed_tests"]),
        # Flaky stats
        "flaky_total": len(flaky_tests),
        "flaky_in_start": len([t for t in flaky_tests if t["flaky_in"] in ["start", "both"]]),
        "flaky_in_end": len([t for t in flaky_tests if t["flaky_in"] in ["end", "both"]]),
    }

    # Put summary first in output (matching single attempt structure)
    return {
        "summary": stats,
        "classification": classification,
        "stable_classification": stable_classification,
        "flaky_tests": flaky_tests,
        "merged_from_attempts": len(valid_attempt_dirs),
    }


# =============================================================================
# Cache Helper Functions
# =============================================================================


def check_attempt_completed(attempt_dir: Path) -> bool:
    """
    Check if an attempt has been completed successfully.

    An attempt is considered complete if both start.json and end.json exist
    and contain valid JSON data.

    Args:
        attempt_dir: Path to the attempt directory

    Returns:
        True if attempt is complete, False otherwise
    """
    if not attempt_dir.exists():
        return False

    start_file = attempt_dir / "start.json"
    end_file = attempt_dir / "end.json"

    if not start_file.exists() or not end_file.exists():
        return False

    # Validate that the files contain valid JSON
    try:
        with open(start_file) as f:
            start_data = json.load(f)
        with open(end_file) as f:
            end_data = json.load(f)

        # Basic sanity check: ensure they have test data
        if "tests" not in start_data or "tests" not in end_data:
            return False

        return True
    except (json.JSONDecodeError, IOError):
        return False


def load_cached_attempt_result(
    attempt_dir: Path,
    attempt_num: int,
    milestone_id: str,
    framework: str = "pytest",
) -> Optional[Dict[str, Any]]:
    """
    Load cached result from a completed attempt.

    Args:
        attempt_dir: Path to the attempt directory
        attempt_num: Attempt number
        milestone_id: Milestone ID for logging
        framework: Test framework name

    Returns:
        Dictionary with attempt result, or None if loading fails
    """
    classification_file = attempt_dir / "classification.json"

    if not classification_file.exists():
        return None

    try:
        with open(classification_file) as f:
            classification = json.load(f)

        summary = classification.get("summary", {})

        logger.info(
            f"[{milestone_id}] Attempt {attempt_num} (cached): "
            f"fail_to_pass={summary.get('fail_to_pass', 0)}, "
            f"pass_to_fail={summary.get('pass_to_fail', 0)}"
        )

        return {
            "attempt": attempt_num,
            "status": "success",
            "statistics": summary,
            "classification_file": str(classification_file.name),
            "cached": True,
        }
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"[{milestone_id}] Failed to load cached result from {attempt_dir}: {e}")
        return None


# =============================================================================
# Helper Functions
# =============================================================================


def discover_milestones(work_dir: Path) -> List[str]:
    """
    Discover available milestones from dockerfiles directory.

    Args:
        work_dir: Working directory containing dockerfiles/

    Returns:
        Sorted list of milestone IDs
    """
    dockerfiles_dir = work_dir / "dockerfiles"
    if not dockerfiles_dir.exists():
        return []

    milestones = []
    for item in dockerfiles_dir.iterdir():
        if item.is_dir() and item.name.startswith("M"):
            # Check for required files
            if (item / "Dockerfile").exists() or (item / "test_config.json").exists():
                milestones.append(item.name)

    return sorted(milestones)


def load_base_commits(work_dir: Path) -> Dict[str, str]:
    """
    Load base commits from metadata.json.

    Args:
        work_dir: Working directory

    Returns:
        Dict mapping milestone ID to base commit SHA
    """
    metadata_path = work_dir / "metadata.json"
    if not metadata_path.exists():
        return {}

    with open(metadata_path) as f:
        metadata = json.load(f)

    base_commits = {}
    for milestone in metadata.get("milestones", []):
        milestone_id = milestone.get("id")
        base_commit = milestone.get("base_commit")
        if milestone_id and base_commit:
            base_commits[milestone_id] = base_commit

    return base_commits


def infer_repo_info(work_dir: Path) -> tuple:
    """
    Infer repo_id and test_scenario from work_dir path.

    Expected path format: .../harness_workspace/{repo_id}/{test_scenario}/...

    Returns:
        Tuple of (repo_id, test_scenario)
    """
    parts = work_dir.resolve().parts
    try:
        idx = parts.index("harness_workspace")
        repo_id = parts[idx + 1]
        test_scenario = parts[idx + 2]
        return repo_id, test_scenario
    except (ValueError, IndexError):
        return None, None


def get_switch_cmd(state: str, milestone_id: str, base_commit: Optional[str] = None) -> str:
    """
    Get git command to switch to a specific milestone state.

    Uses 'git checkout -f' to forcefully discard local changes before switching.
    This is necessary because Dockerfiles may apply compilation patches that modify
    tracked files, and these modifications would otherwise block state switching.

    Args:
        state: State name ("original", "start", "end")
        milestone_id: Milestone ID (e.g., "M001")
        base_commit: Base commit SHA for original state

    Returns:
        Git checkout command with -f flag
    """
    if state == "original":
        if not base_commit:
            raise ValueError("base_commit required for original state")
        return f"git checkout -f {base_commit}"
    elif state == "start":
        return f"git checkout -f milestone-{milestone_id}-start"
    elif state == "end":
        return f"git checkout -f milestone-{milestone_id}-end"
    else:
        raise ValueError(f"Unknown state: {state}")


def get_default_test_cmd(
    workers: int,
    timeout: int,
    output_file: str,
    framework: str = "pytest",
) -> str:
    """
    Get default test command for a given framework.

    Args:
        workers: Number of parallel workers
        timeout: Test timeout in seconds
        output_file: Output file name for test results
        framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha)

    Returns:
        Test command string
    """
    # Framework-specific default commands
    commands = {
        "pytest": f"pytest -n {workers} --timeout={timeout} --json-report --json-report-file=/output/{output_file}",
        "unittest": f"python -m pytest -n {workers} --timeout={timeout} --json-report --json-report-file=/output/{output_file}",
        "go_test": f"go test -json -timeout {timeout}s -parallel {workers} ./... 2>&1 | tee /output/{output_file}",
        "maven": f"mvn test -Dmaven.test.failure.ignore=true -Dsurefire.timeout={timeout} 2>&1 | tee /output/{output_file}",
        "gradle": f"gradle test --continue -Dtest.parallel=true -Dtest.maxParallelForks={workers} 2>&1 | tee /output/{output_file}",
        "cargo": f"cargo test --no-fail-fast -- --test-threads={workers} 2>&1 | tee /output/{output_file}",
        "jest": f"npx jest --json --outputFile=/output/{output_file} --testTimeout={timeout * 1000} --maxWorkers={workers}",
        "mocha": f"npx mocha --reporter json --timeout {timeout * 1000} --parallel --jobs {workers} > /output/{output_file}",
    }

    return commands.get(framework, commands["pytest"])


# =============================================================================
# Single Attempt Runner
# =============================================================================


def run_single_attempt(
    attempt_num: int,
    attempt_dir: Path,
    milestone_id: str,
    runner: DockerRunner,
    config: MilestoneTestConfig,
    base_commit: Optional[str],
    workers: int,
    timeout: int,
    verbose: bool,
    framework: str = "pytest",
) -> Dict[str, Any]:
    """
    Run a single test attempt for the milestone.

    Returns:
        Dictionary with attempt result
    """
    attempt_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[{milestone_id}] Attempt {attempt_num}...")

    # Get all unique states from config
    all_states = set(config.get_all_states())

    # Add original if configured
    if config.include_original and base_commit:
        all_states.add("original")

    # Run each (state, mode) pair
    # Store tuples of (Path, framework) to support per-mode framework overrides
    state_mode_files: Dict[str, List[Tuple[Path, str]]] = {state: [] for state in all_states}

    # Determine file extension based on framework
    file_ext = get_file_extension(framework)

    for state, mode in config.get_all_state_mode_pairs():
        # Use mode-specific framework if specified, otherwise use global framework
        mode_framework = mode.framework or framework
        mode_file_ext = get_file_extension(mode_framework)
        output_file = f"{state}_{mode.name}{mode_file_ext}"

        # Build test command
        if mode.test_cmd:
            test_cmd = mode.test_cmd.format(
                workers=workers,
                timeout=timeout,
                output_file=output_file,
                milestone_id=milestone_id,
            )
        else:
            test_cmd = get_default_test_cmd(workers, timeout, output_file, framework)

        # Build switch command
        switch_cmd = get_switch_cmd(state, milestone_id, base_commit)

        # Build surefire collection script for Maven projects
        # This collects XML reports with module path prefixes for method-level granularity
        surefire_collect_script = ""
        if framework in ("maven", "gradle"):
            surefire_archive_name = f"{state}_surefire_reports.tar.gz"
            surefire_collect_script = f"""
echo ">>> Collecting Surefire XML reports for {state} state..."
mkdir -p /tmp/surefire_reports
# Find all surefire-reports directories and copy with module structure
find /testbed -path "*/target/surefire-reports" -type d | while read dir; do
    # Extract module path relative to /testbed
    module_path=$(dirname "$dir" | sed 's|/testbed/||' | sed 's|/target||')
    if [ -n "$module_path" ]; then
        mkdir -p "/tmp/surefire_reports/$module_path"
        cp -f "$dir"/TEST-*.xml "/tmp/surefire_reports/$module_path/" 2>/dev/null || true
    fi
done
# Create archive if any reports were collected
if [ -n "$(find /tmp/surefire_reports -name 'TEST-*.xml' 2>/dev/null)" ]; then
    cd /tmp && tar -czf /output/{surefire_archive_name} surefire_reports/
    echo ">>> Surefire reports archived to {surefire_archive_name}"
else
    echo ">>> No Surefire XML reports found"
fi
rm -rf /tmp/surefire_reports
"""

        # Build script
        # After git checkout, call apply_patches.sh if it exists to re-apply compilation fixes
        script = f"""
set -e
cd /testbed
echo ">>> Switching to {state} state..."
{switch_cmd}
# Apply compilation patches if script exists
if [ -x /usr/local/bin/apply_patches.sh ]; then
    echo ">>> Applying compilation patches..."
    /usr/local/bin/apply_patches.sh
fi
# Force recompilation of test files for Rust projects to ensure new tests are included
if [ -f Cargo.toml ] || [ -f cargo.toml ]; then
    echo ">>> Touching test files to force recompilation..."
    find . -path '*/tests/*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    find . -path '*/src/*test*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    echo ">>> Test files touched, forcing recompilation"
fi
echo ">>> Running {mode.name} tests for {state} state..."
{test_cmd} || true
echo ">>> Done with {state}/{mode.name}"
{surefire_collect_script}
"""

        # Run in container
        returncode, stdout, stderr = runner.run(
            script,
            timeout=timeout * 60,
            extra_volumes={str(attempt_dir.absolute()): "/output"},
        )

        if verbose:
            logger.debug(f"[{milestone_id}] {state}_{mode.name}: returncode={returncode}")

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files[state].append((output_path, mode_framework))

    # Run original state if configured (with default mode only)
    if config.include_original and base_commit and "original" not in [s for s, m in config.get_all_state_mode_pairs()]:
        output_file = f"original_default{file_ext}"
        test_cmd = get_default_test_cmd(workers, timeout, output_file, framework)
        switch_cmd = get_switch_cmd("original", milestone_id, base_commit)

        script = f"""
set -e
cd /testbed
echo ">>> Switching to original state..."
{switch_cmd}
# Apply compilation patches if script exists
if [ -x /usr/local/bin/apply_patches.sh ]; then
    echo ">>> Applying compilation patches..."
    /usr/local/bin/apply_patches.sh
fi
# Force recompilation of test files for Rust projects to ensure new tests are included
if [ -f Cargo.toml ] || [ -f cargo.toml ]; then
    echo ">>> Touching test files to force recompilation..."
    find . -path '*/tests/*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    find . -path '*/src/*test*.rs' -type f -exec touch {{}} \\; 2>/dev/null || true
    echo ">>> Test files touched, forcing recompilation"
fi
echo ">>> Running tests for original state..."
{test_cmd} || true
echo ">>> Done with original state"
"""
        runner.run(
            script,
            timeout=timeout * 60,
            extra_volumes={str(attempt_dir.absolute()): "/output"},
        )

        output_path = attempt_dir / output_file
        if output_path.exists():
            state_mode_files["original"].append((output_path, framework))

    # Merge results per state (within-attempt merging)
    # mode_files is now List[Tuple[Path, str]] where str is the framework
    for state in all_states:
        mode_file_tuples = state_mode_files.get(state, [])
        if mode_file_tuples:
            merged_path = attempt_dir / f"{state}.json"
            if len(mode_file_tuples) == 1:
                # Single mode: parse and convert to standardized JSON format
                # This is important for non-JSON frameworks (maven, cargo, etc.)
                file_path, file_framework = mode_file_tuples[0]
                try:
                    parsed = parse_test_report(file_path, file_framework)
                    with open(merged_path, "w") as f:
                        json.dump(parsed, f, indent=2)
                except Exception as e:
                    logger.warning(f"[{milestone_id}] Failed to parse {state} report: {e}")
            else:
                # Multiple modes with potentially different frameworks
                # Parse each file with its own framework, then merge standardized results
                all_parsed_tests = []
                total_duration = 0.0
                summary_totals = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}

                for file_path, file_framework in mode_file_tuples:
                    try:
                        parsed = parse_test_report(file_path, file_framework)
                        all_parsed_tests.extend(parsed.get("tests", []))
                        total_duration += parsed.get("duration", 0)
                        for key in summary_totals:
                            summary_totals[key] += parsed.get("summary", {}).get(key, 0)
                    except Exception as e:
                        logger.warning(f"[{milestone_id}] Failed to parse {file_path}: {e}")

                # Build merged result
                summary_totals["total"] = len(all_parsed_tests)
                merged_result = {
                    "tests": all_parsed_tests,
                    "collectors": [],
                    "summary": summary_totals,
                    "duration": total_duration,
                    "_framework": "mixed",
                    "_merge_info": {
                        "source_files": [str(fp) for fp, _ in mode_file_tuples],
                        "frameworks": [fw for _, fw in mode_file_tuples],
                    },
                }
                with open(merged_path, "w") as f:
                    json.dump(merged_result, f, indent=2)

    # Verify both start.json and end.json exist
    start_file = attempt_dir / "start.json"
    end_file = attempt_dir / "end.json"

    if not start_file.exists() or not end_file.exists():
        return {
            "attempt": attempt_num,
            "status": "error",
            "error": f"Missing output files: start={start_file.exists()}, end={end_file.exists()}",
        }

    # Generate summary files with fail/skip reasons
    try:
        convert_to_summary(start_file, attempt_dir / "start_summary.json", framework)
        convert_to_summary(end_file, attempt_dir / "end_summary.json", framework)
        if config.include_original and (attempt_dir / "original.json").exists():
            convert_to_summary(attempt_dir / "original.json", attempt_dir / "original_summary.json", framework)
    except Exception as e:
        logger.warning(f"[{milestone_id}] Failed to generate summary files: {e}")

    # Classify results for this attempt (using framework-aware classifier)
    classifier = TestClassifier(framework=framework)
    classification = classifier.classify_from_files(start_file, end_file)
    summary = classifier.generate_summary(classification)

    # Put summary first in output
    classification_result = {"summary": summary, **classification}

    # Save classification for this attempt
    classification_file = attempt_dir / "classification.json"
    with open(classification_file, "w") as f:
        json.dump(classification_result, f, indent=2)

    logger.info(
        f"[{milestone_id}] Attempt {attempt_num} success: "
        f"fail_to_pass={summary['fail_to_pass']}, "
        f"pass_to_fail={summary['pass_to_fail']}"
    )

    return {
        "attempt": attempt_num,
        "status": "success",
        "statistics": summary,
        "classification_file": str(classification_file.name),
        "run_configs": [m.name for m in config.modes],
    }


# =============================================================================
# Main Milestone Runner
# =============================================================================


def run_single_milestone(
    milestone_id: str,
    work_dir: Path,
    output_dir: Path,
    repo_id: str,
    test_scenario: str,
    base_commit: Optional[str],
    workers: int,
    timeout: int,
    max_retries: int,
    include_original: bool,
    verbose: bool,
    framework: str = "pytest",
    force_rebuild_image: bool = False,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """
    Run tests for a single milestone using Docker with retry support.

    Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha).

    Args:
        force_rebuild_image: If True, rebuild the Docker image even if it already exists.
        use_cache: If True (default), skip already completed attempts and reuse their results.
                   If False, rerun all attempts from scratch.
    """
    milestone_output_dir = output_dir / milestone_id
    milestone_output_dir.mkdir(parents=True, exist_ok=True)

    dockerfile_dir = work_dir / "dockerfiles" / milestone_id

    # Determine image name
    image_name = f"{repo_id}/{test_scenario}/{milestone_id}:latest".lower()
    # # Determine image name (sanitize for valid Docker tag)
    # # Docker image names must match: [a-z0-9]+(?:[._-][a-z0-9]+)*
    # import re
    # raw_name = f"{repo_id}_{test_scenario}_{milestone_id}".lower()
    # # Replace any non-alphanumeric chars with underscore, collapse multiples
    # sanitized = re.sub(r'[^a-z0-9]+', '_', raw_name).strip('_')
    # image_name = f"{sanitized}:latest"

    # Build image if not exists or force rebuild
    image_exists = check_image_exists(image_name)
    if force_rebuild_image or not image_exists:
        dockerfile_path = dockerfile_dir / "Dockerfile"
        if not dockerfile_path.exists():
            return {
                "status": "error",
                "milestone_id": milestone_id,
                "error": f"Dockerfile not found: {dockerfile_path}",
            }

        if force_rebuild_image and image_exists:
            logger.info(f"[{milestone_id}] Force rebuilding image {image_name}...")
        else:
            logger.info(f"[{milestone_id}] Building image {image_name}...")

        build_context = work_dir / "testbed"
        if not build_context.exists():
            return {
                "status": "error",
                "milestone_id": milestone_id,
                "error": f"Build context not found: {build_context}",
            }

        if not build_docker_image(str(dockerfile_path), image_name, build_context, verbose):
            return {
                "status": "error",
                "milestone_id": milestone_id,
                "error": f"Failed to build image {image_name}",
            }

    # Load test config first (needed to check Docker socket requirements)
    config_path = dockerfile_dir / "test_config.json"
    if config_path.exists():
        config = MilestoneTestConfig.from_file(config_path, include_original)
        logger.info(f"[{milestone_id}] Loaded config with {len(config.modes)} mode(s)")
    else:
        config = MilestoneTestConfig.default(include_original)
        logger.info(f"[{milestone_id}] Using default config")

    # Create Docker runner (with Docker socket if e2e tests need it)
    enable_docker_socket = config.requires_docker_socket_any()
    if enable_docker_socket:
        logger.info(f"[{milestone_id}] Enabling Docker socket mounting for e2e tests")
    # For e2e tests with testcontainers, we need:
    # - Docker socket mounting for testcontainers to create containers
    # - Host network so the test can reach containers spawned by testcontainers
    # - TESTCONTAINERS_RYUK_DISABLED=true to prevent Ryuk connection issues
    runner = DockerRunner(
        image_name,
        enable_docker_socket=enable_docker_socket,
        use_host_network=enable_docker_socket,  # Use host network when testcontainers enabled
        extra_env={"TESTCONTAINERS_RYUK_DISABLED": "true"} if enable_docker_socket else {},
    )

    # Run test attempts with retries
    all_attempts = []
    successful_attempts = []
    total_duration = 0
    start_time = time.time()
    cached_count = 0

    for attempt_num in range(1, max_retries + 1):
        attempt_dir = milestone_output_dir / f"attempt_{attempt_num}"

        # Check cache: skip if attempt is already completed
        if use_cache and check_attempt_completed(attempt_dir):
            cached_result = load_cached_attempt_result(attempt_dir, attempt_num, milestone_id, framework)
            if cached_result:
                cached_result["duration"] = 0  # No time spent on cached attempts
                all_attempts.append(cached_result)
                successful_attempts.append(cached_result)
                cached_count += 1
                continue
            else:
                # Classification file missing or invalid, need to regenerate
                logger.info(
                    f"[{milestone_id}] Attempt {attempt_num} has data but missing classification, regenerating..."
                )

        attempt_start = time.time()

        attempt_result = run_single_attempt(
            attempt_num=attempt_num,
            attempt_dir=attempt_dir,
            milestone_id=milestone_id,
            runner=runner,
            config=config,
            base_commit=base_commit,
            workers=workers,
            timeout=timeout,
            verbose=verbose,
            framework=framework,
        )

        attempt_result["duration"] = time.time() - attempt_start
        all_attempts.append(attempt_result)
        total_duration += attempt_result["duration"]

        if attempt_result["status"] == "success":
            successful_attempts.append(attempt_result)

    # Log cache statistics
    if cached_count > 0:
        logger.info(
            f"[{milestone_id}] Used {cached_count} cached attempt(s), ran {len(all_attempts) - cached_count} new attempt(s)"
        )

    elapsed = time.time() - start_time

    # Determine final status
    if successful_attempts:
        final_status = "success"

        # Merge results from all successful attempts (cross-attempt merging)
        successful_attempt_dirs = [
            milestone_output_dir / f"attempt_{attempt['attempt']}" for attempt in successful_attempts
        ]
        merged_classification = merge_attempt_results(successful_attempt_dirs, verbose, framework)

        if merged_classification:
            # Save merged classification
            classification_file = milestone_output_dir / f"{milestone_id}_classification.json"
            with open(classification_file, "w") as f:
                json.dump(merged_classification, f, indent=2)

            merged_summary = merged_classification["summary"]
        else:
            # Fallback to last successful attempt
            best_result = successful_attempts[-1]
            merged_summary = best_result["statistics"]

        # Save test summary
        summary_file = milestone_output_dir / "test_summary.json"
        with open(summary_file, "w") as f:
            json.dump(
                {
                    "milestone_id": milestone_id,
                    "final_status": final_status,
                    "total_attempts": len(all_attempts),
                    "successful_attempts": len(successful_attempts),
                    "failed_attempts": len(all_attempts) - len(successful_attempts),
                    "cached_attempts": cached_count,
                    "total_duration": total_duration,
                    "summary": merged_summary,
                    "all_attempts": all_attempts,
                },
                f,
                indent=2,
            )

        return {
            "status": final_status,
            "milestone_id": milestone_id,
            "elapsed_seconds": round(elapsed, 2),
            "total_attempts": len(all_attempts),
            "successful_attempts": len(successful_attempts),
            "summary": merged_summary,
        }
    else:
        # All attempts failed
        last_error = all_attempts[-1].get("error", "Unknown error") if all_attempts else "No attempts were made"

        summary_file = milestone_output_dir / "test_summary.json"
        with open(summary_file, "w") as f:
            json.dump(
                {
                    "milestone_id": milestone_id,
                    "final_status": "failed",
                    "total_attempts": len(all_attempts),
                    "successful_attempts": 0,
                    "failed_attempts": len(all_attempts),
                    "cached_attempts": cached_count,
                    "total_duration": total_duration,
                    "all_attempts": all_attempts,
                    "last_error": last_error,
                },
                f,
                indent=2,
            )

        return {
            "status": "error",
            "milestone_id": milestone_id,
            "elapsed_seconds": round(elapsed, 2),
            "error": f"All {len(all_attempts)} attempts failed. Last error: {last_error}",
        }


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="Run milestone tests using the unified test runner framework",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single milestone (Python/pytest - default)
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

  # Multiple milestones
  python -m harness.test_runner.run_milestone_tests M001 M002 M003 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2

  # All milestones in parallel
  python -m harness.test_runner.run_milestone_tests --all \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --parallel 4

  # Go project (go test)
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/go_project/test_scenario \\
      --language go --test-framework go_test

  # Java/Maven project
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/java_project/test_scenario \\
      --language java --test-framework maven

  # Rust/Cargo project
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/rust_project/test_scenario \\
      --language rust --test-framework cargo

  # JavaScript/Jest project
  python -m harness.test_runner.run_milestone_tests M001 \\
      --work-dir DATA/harness_workspace/js_project/test_scenario \\
      --language javascript --test-framework jest

  # With original state testing
  python -m harness.test_runner.run_milestone_tests --milestone-id M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --include-original

  # Custom settings
  python -m harness.test_runner.run_milestone_tests --milestone-id M001 \\
      --work-dir DATA/harness_workspace/urllib3_urllib3_2.0.6_2.3.0/test_multi_stage_v2.2 \\
      --workers 16 --timeout 60 --max-retries 5
        """,
    )

    # Positional arguments
    parser.add_argument("milestones", nargs="*", help="Milestone ID(s) to test (e.g., M001, M002)")

    # Named milestone argument (alternative to positional)
    parser.add_argument(
        "--milestone-id",
        type=str,
        default=None,
        help="Single milestone ID to test (alternative to positional argument)",
    )

    # Mode selection
    parser.add_argument("--all", action="store_true", help="Run tests for all available milestones")

    # Required paths
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="Working directory (contains dockerfiles/, metadata.json, etc.)"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="Output directory (default: {work_dir}/test_results)"
    )

    # Test configuration
    parser.add_argument("-n", "--workers", type=int, default=30, help="Number of parallel test workers (default: 30)")
    parser.add_argument("--timeout", type=int, default=300, help="Timeout for test execution in seconds (default: 300)")
    parser.add_argument("--max-retries", type=int, default=3, help="Maximum number of retry attempts (default: 3)")
    parser.add_argument("--include-original", action="store_true", help="Also test at original state (base_commit)")
    parser.add_argument(
        "--force-rebuild-image",
        action="store_true",
        help="Force rebuild Docker image even if it already exists",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable caching: rerun all attempts even if they already completed",
    )

    # Language and framework options
    parser.add_argument(
        "--language",
        type=str,
        default="python",
        help="Programming language (default: python). Options: python, java, javascript, go, rust",
    )
    parser.add_argument(
        "--test-framework",
        type=str,
        default="pytest",
        help=(
            "Test framework (default: pytest). Options: "
            "pytest, unittest (Python); maven, gradle (Java); "
            "jest, mocha (JavaScript); go_test (Go); cargo (Rust)"
        ),
    )

    # Execution options
    parser.add_argument(
        "-p", "--parallel", type=int, default=1, help="Number of milestones to process in parallel (default: 1)"
    )

    # Output options
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")

    args = parser.parse_args()

    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate work_dir
    work_dir = args.work_dir.resolve()
    if not work_dir.exists():
        print(f"Error: Work directory not found: {work_dir}", file=sys.stderr)
        return 1

    # Set output directory
    output_dir = args.output_dir or (work_dir / "test_results")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine milestones to run
    if args.all:
        milestones = discover_milestones(work_dir)
        if not milestones:
            print(f"Error: No milestones found in {work_dir / 'dockerfiles'}", file=sys.stderr)
            return 1
        print(f"Found {len(milestones)} milestones: {', '.join(milestones)}")
    elif args.milestone_id:
        # Named argument takes precedence if positional is empty
        milestones = [args.milestone_id]
    elif args.milestones:
        milestones = args.milestones
    else:
        print("Error: Specify milestone ID(s), use --milestone-id, or use --all", file=sys.stderr)
        return 1

    # Load base commits
    base_commits = load_base_commits(work_dir)

    # Infer repo info
    repo_id, test_scenario = infer_repo_info(work_dir)
    if not repo_id:
        print(f"Error: Cannot infer repo_id from work_dir path", file=sys.stderr)
        return 1

    # Validate framework
    framework = args.test_framework
    if framework not in FRAMEWORK_CONFIG:
        print(f"Error: Unsupported test framework '{framework}'", file=sys.stderr)
        print(f"Available frameworks: {', '.join(FRAMEWORK_CONFIG.keys())}", file=sys.stderr)
        return 1

    print(f"Repository: {repo_id}")
    print(f"Test scenario: {test_scenario}")
    print(f"Language: {args.language}, Framework: {framework}")
    print(f"Output directory: {output_dir}")
    print(f"Workers: {args.workers}, Timeout: {args.timeout}s, Max retries: {args.max_retries}")
    if args.include_original:
        print(f"Include original: Yes")
    if args.force_rebuild_image:
        print(f"Force rebuild image: Yes")
    if args.no_cache:
        print(f"Cache disabled: Yes (will rerun all attempts)")
    else:
        print(f"Cache enabled: Yes (will skip completed attempts)")
    print()

    # Run tests
    all_results = {}
    start_time = time.time()

    if args.parallel > 1 and len(milestones) > 1:
        # Parallel execution
        print(f"Running {len(milestones)} milestones in parallel (max {args.parallel} concurrent)")

        with ThreadPoolExecutor(max_workers=args.parallel) as executor:
            futures = {}
            for milestone_id in milestones:
                base_commit = base_commits.get(milestone_id)

                future = executor.submit(
                    run_single_milestone,
                    milestone_id=milestone_id,
                    work_dir=work_dir,
                    output_dir=output_dir,
                    repo_id=repo_id,
                    test_scenario=test_scenario,
                    base_commit=base_commit,
                    workers=args.workers,
                    timeout=args.timeout,
                    max_retries=args.max_retries,
                    include_original=args.include_original,
                    verbose=args.verbose,
                    framework=framework,
                    force_rebuild_image=args.force_rebuild_image,
                    use_cache=not args.no_cache,
                )
                futures[future] = milestone_id

            for future in as_completed(futures):
                milestone_id = futures[future]
                try:
                    result = future.result()
                    all_results[milestone_id] = result
                    status = "OK" if result["status"] == "success" else "FAILED"
                    print(f"[{milestone_id}] {status}")
                except Exception as e:
                    all_results[milestone_id] = {"status": "error", "error": str(e)}
                    print(f"[{milestone_id}] ERROR: {e}")
    else:
        # Sequential execution
        for i, milestone_id in enumerate(milestones):
            print(f"[{i+1}/{len(milestones)}] Testing {milestone_id}...")
            base_commit = base_commits.get(milestone_id)

            result = run_single_milestone(
                milestone_id=milestone_id,
                work_dir=work_dir,
                output_dir=output_dir,
                repo_id=repo_id,
                test_scenario=test_scenario,
                base_commit=base_commit,
                workers=args.workers,
                timeout=args.timeout,
                max_retries=args.max_retries,
                include_original=args.include_original,
                verbose=args.verbose,
                framework=framework,
                force_rebuild_image=args.force_rebuild_image,
                use_cache=not args.no_cache,
            )
            all_results[milestone_id] = result

            status = "OK" if result["status"] == "success" else "FAILED"
            elapsed = result.get("elapsed_seconds", 0)
            print(f"[{milestone_id}] {status} ({elapsed:.1f}s)")

    # Summary
    total_time = time.time() - start_time
    success_count = sum(1 for r in all_results.values() if r["status"] == "success")
    error_count = len(all_results) - success_count

    print()
    print("=" * 60)
    print(f"Summary: {success_count} succeeded, {error_count} failed")
    print(f"Total time: {total_time:.1f}s")

    # Save overall summary
    summary_path = output_dir / "batch_summary.json"
    summary = {
        "total_milestones": len(milestones),
        "success": success_count,
        "failed": error_count,
        "total_time_seconds": round(total_time, 2),
        "language": args.language,
        "test_framework": framework,
        "milestones": all_results,
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"Summary saved to: {summary_path}")

    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
