"""
Django runtests.py Output Parser

This module provides utilities for parsing Django's runtests.py test output
and converting it to the standardized test report format used by the harness.

Django runtests.py output format (with --verbosity=2):
    test_name (module.TestClass) ... ok
    test_name (module.TestClass) ... FAIL
    test_name (module.TestClass) ... ERROR
    test_name (module.TestClass) ... skipped 'reason'

    Ran X tests in Y.YYYs

    FAILED (failures=N, errors=M, skipped=K)
    or
    OK (skipped=N)

Usage:
    from harness.utils.django_report_utils import parse_django_test_log

    result = parse_django_test_log(Path("test_output.log"))
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def parse_test_output(output: str) -> List[Dict[str, Any]]:
    """
    Parse Django test output and extract individual test results.

    Parses the verbose output format from runtests.py --verbosity=2:
        test_name (module.TestClass) ... ok/FAIL/ERROR/skipped 'reason'

    Args:
        output: Raw text output from Django runtests.py

    Returns:
        List of test result dicts with 'nodeid' and 'outcome' keys
    """
    tests = []

    # Pattern for individual test results with verbosity=2
    # Matches: test_name (module.TestClass.SubClass) ... ok/FAIL/ERROR/skipped 'reason'
    test_pattern = re.compile(
        r"^(test_\w+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped)(?:\s+['\"]([^'\"]+)['\"])?",
        re.MULTILINE,
    )

    for match in test_pattern.finditer(output):
        test_name = match.group(1)
        test_class = match.group(2)
        outcome_str = match.group(3)
        skip_reason = match.group(4) or ""

        # Build nodeid in pytest-compatible format: module.TestClass::test_name
        nodeid = f"{test_class}::{test_name}"

        # Map Django outcome to standard outcome
        if outcome_str == "ok":
            outcome = "passed"
        elif outcome_str == "FAIL":
            outcome = "failed"
        elif outcome_str == "ERROR":
            outcome = "error"
        elif outcome_str == "skipped":
            outcome = "skipped"
        else:
            outcome = "unknown"

        test_result: Dict[str, Any] = {"nodeid": nodeid, "outcome": outcome}
        if skip_reason:
            test_result["skip_reason"] = skip_reason

        tests.append(test_result)

    return tests


def get_test_summary(output: str) -> Dict[str, int]:
    """
    Extract test summary statistics from Django output.

    Parses the summary line at the end of test output:
        Ran X tests in Y.YYYs
        FAILED (failures=1, errors=2, skipped=3)
        or
        OK (skipped=5)

    Args:
        output: Raw text output from Django runtests.py

    Returns:
        Dict with 'total', 'passed', 'failed', 'skipped', 'error', 'collected' counts
    """
    summary: Dict[str, int] = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "collected": 0,
    }

    # Match "Ran X tests in Y.YYYs"
    ran_match = re.search(r"Ran (\d+) tests? in", output)
    if ran_match:
        summary["total"] = int(ran_match.group(1))
        summary["collected"] = summary["total"]

    # Match summary line like "FAILED (failures=1, errors=2, skipped=3)"
    # or "OK (skipped=5)"
    failures_match = re.search(r"failures?=(\d+)", output)
    if failures_match:
        summary["failed"] = int(failures_match.group(1))

    errors_match = re.search(r"errors?=(\d+)", output)
    if errors_match:
        summary["error"] = int(errors_match.group(1))

    skipped_match = re.search(r"skipped=(\d+)", output)
    if skipped_match:
        summary["skipped"] = int(skipped_match.group(1))

    # Calculate passed count
    summary["passed"] = max(0, summary["total"] - summary["failed"] - summary["error"] - summary["skipped"])

    return summary


def get_duration(output: str) -> float:
    """
    Extract test duration from Django output.

    Args:
        output: Raw text output from Django runtests.py

    Returns:
        Duration in seconds, or 0.0 if not found
    """
    # Match "Ran X tests in Y.YYYs"
    duration_match = re.search(r"Ran \d+ tests? in ([\d.]+)s", output)
    if duration_match:
        return float(duration_match.group(1))
    return 0.0


def parse_django_test_log(log_path: Path) -> Dict[str, Any]:
    """
    Parse a Django runtests.py log file and convert to standardized format.

    This is the main entry point for parsing Django test output. It reads
    the log file and produces a standardized report dict compatible with
    the harness test classification system.

    Args:
        log_path: Path to Django test output log file

    Returns:
        Standardized test report dict with structure:
        {
            "tests": [{"nodeid": str, "outcome": str}, ...],
            "collectors": [],
            "summary": {"passed": int, "failed": int, "skipped": int, "error": int, "total": int},
            "duration": float,
            "_framework": "django_runtests",
            "_raw_output": str (truncated)
        }
    """
    if not log_path.exists():
        return _empty_report()

    try:
        output = log_path.read_text(errors="replace")
    except IOError:
        return _empty_report()

    # Parse test results
    tests = parse_test_output(output)
    summary = get_test_summary(output)
    duration = get_duration(output)

    # If we parsed individual tests, update summary from them
    # (more accurate than regex parsing of summary line)
    if tests:
        outcomes: Dict[str, int] = {}
        for t in tests:
            outcome = t["outcome"]
            outcomes[outcome] = outcomes.get(outcome, 0) + 1

        if sum(outcomes.values()) > 0:
            summary["collected"] = len(tests)
            summary["total"] = len(tests)
            summary["passed"] = outcomes.get("passed", 0)
            summary["failed"] = outcomes.get("failed", 0)
            summary["error"] = outcomes.get("error", 0)
            summary["skipped"] = outcomes.get("skipped", 0)

    return {
        "tests": tests,
        "collectors": [],
        "summary": summary,
        "duration": duration,
        "_framework": "django_runtests",
        "_raw_output": output[:100000] if len(output) > 100000 else output,
    }


def _empty_report() -> Dict[str, Any]:
    """Return an empty standardized report structure."""
    return {
        "tests": [],
        "collectors": [],
        "summary": {
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "error": 0,
            "total": 0,
            "collected": 0,
        },
        "duration": 0.0,
        "_framework": "django_runtests",
        "_raw_output": "",
    }
