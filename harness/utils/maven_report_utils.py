"""
Maven Test Report Parsing Utilities

This module provides utilities for parsing Maven Surefire test output logs
and converting them to a standardized summary format.

IMPORTANT: All statistics use CLASS-LEVEL granularity, not method-level.
- total/passed/failed/skipped counts are number of test CLASSES
- passed: classes with no failures/errors and not fully skipped
- failed: classes with any failures or errors
- skipped: classes where ALL tests were skipped

This is because Maven console output only provides class-level summaries.
Individual test method names are not available from console output.

Supported patterns:
- Test class execution: [INFO] Running org.example.TestClass
- Test results: Tests run: X, Failures: Y, Errors: Z, Skipped: W, Time elapsed: T s -- in org.example.TestClass
- Individual failures: [ERROR] org.example.TestClass.testMethod -- Time elapsed: T s <<< FAILURE!
- Individual errors: [ERROR] org.example.TestClass.testMethod -- Time elapsed: T s <<< ERROR!
- Stack traces following failure/error markers

Usage:
    from harness.utils.maven_report_utils import convert_maven_log_to_summary

    summary = convert_maven_log_to_summary(Path("output.log"))
    # Or save to file:
    summary = convert_maven_log_to_summary(Path("output.log"), Path("summary.json"))
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class TestClassResult:
    """Result for a single test class."""

    class_name: str
    tests_run: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    time_elapsed: float = 0.0
    status: str = "INFO"  # INFO, WARNING, ERROR


@dataclass
class TestFailure:
    """Details of a single test failure or error."""

    test_class: str
    test_method: str
    failure_type: str  # "FAILURE" or "ERROR"
    time_elapsed: float = 0.0
    message: str = ""
    stack_trace: str = ""

    @property
    def nodeid(self) -> str:
        """Return pytest-compatible nodeid format."""
        return f"{self.test_class}.{self.test_method}"


@dataclass
class MavenTestSummary:
    """Aggregated summary of Maven test execution."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    test_classes: List[TestClassResult] = field(default_factory=list)
    failures: List[TestFailure] = field(default_factory=list)
    error_tests: List[TestFailure] = field(default_factory=list)


# Regex patterns for parsing Maven output
PATTERNS = {
    # [INFO] Running org.apache.dubbo.xxx.XxxTest
    "running": re.compile(r"^\[INFO\] Running (.+)$"),
    # [INFO/WARNING/ERROR] Tests run: X, Failures: Y, Errors: Z, Skipped: W, Time elapsed: T s [<<< FAILURE!] -- in org.xxx.Test
    "class_result": re.compile(
        r"^\[(INFO|WARNING|ERROR)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)"
        r"(?:, Time elapsed: ([\d.]+) s)?(?:\s*<<<\s*FAILURE!)?(?: -- in (.+))?$"
    ),
    # [ERROR] org.xxx.Test.testMethod -- Time elapsed: T s <<< FAILURE!/ERROR!
    "test_failure": re.compile(r"^\[ERROR\] (.+?)\.(\w+)(?: -- Time elapsed: ([\d.]+) s)? <<< (FAILURE|ERROR)!$"),
    # Module summary: Tests run: X, Failures: Y, Errors: Z, Skipped: W (no "-- in")
    "module_summary": re.compile(
        r"^\[(INFO|WARNING|ERROR)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)$"
    ),
    # Build duration: Total time: X.XXX s or Total time: Xm Xs
    "build_time": re.compile(r"^\[INFO\] Total time:\s+(?:(\d+)m\s+)?(\d+(?:\.\d+)?)\s*s?"),
    # Exception class detection for stack traces
    "exception_start": re.compile(r"^([a-zA-Z_][\w.]*(?:Exception|Error|Throwable)): (.*)$"),
    # Stack trace line
    "stack_trace": re.compile(r"^\s+at\s+.+"),
    # Caused by line
    "caused_by": re.compile(r"^Caused by:\s+(.+)$"),
}


def parse_maven_test_log(log_path: Path) -> MavenTestSummary:
    """
    Parse a Maven Surefire test output log file.

    Args:
        log_path: Path to the Maven test output log

    Returns:
        MavenTestSummary with parsed test results
    """
    summary = MavenTestSummary()

    if not log_path.exists():
        return summary

    content = log_path.read_text(errors="replace")
    lines = content.splitlines()

    current_failure: Optional[TestFailure] = None
    collecting_stack_trace = False
    stack_trace_lines: List[str] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for test class result
        match = PATTERNS["class_result"].match(line)
        if match:
            status, tests, failures, errors, skipped, time_str, class_name = match.groups()

            if class_name:  # This is a per-class result, not module summary
                result = TestClassResult(
                    class_name=class_name,
                    tests_run=int(tests),
                    failures=int(failures),
                    errors=int(errors),
                    skipped=int(skipped),
                    time_elapsed=float(time_str) if time_str else 0.0,
                    status=status,
                )
                summary.test_classes.append(result)
            i += 1
            continue

        # Check for individual test failure/error
        match = PATTERNS["test_failure"].match(line)
        if match:
            # Save previous failure if exists
            if current_failure and collecting_stack_trace:
                current_failure.stack_trace = "\n".join(stack_trace_lines)
                _add_failure_to_summary(summary, current_failure)

            test_class, test_method, time_str, failure_type = match.groups()
            current_failure = TestFailure(
                test_class=test_class,
                test_method=test_method,
                failure_type=failure_type,
                time_elapsed=float(time_str) if time_str else 0.0,
            )
            collecting_stack_trace = True
            stack_trace_lines = []
            i += 1
            continue

        # Collect stack trace lines
        if collecting_stack_trace:
            # Check if this line is part of a stack trace
            if (
                PATTERNS["exception_start"].match(line)
                or PATTERNS["stack_trace"].match(line)
                or PATTERNS["caused_by"].match(line)
                or line.startswith("\t")
            ):
                stack_trace_lines.append(line)

                # Extract exception message from first line
                exc_match = PATTERNS["exception_start"].match(line)
                if exc_match and not current_failure.message:
                    current_failure.message = f"{exc_match.group(1)}: {exc_match.group(2)}"

                i += 1
                continue
            else:
                # End of stack trace
                if current_failure:
                    current_failure.stack_trace = "\n".join(stack_trace_lines)
                    _add_failure_to_summary(summary, current_failure)
                    current_failure = None
                collecting_stack_trace = False
                stack_trace_lines = []

        # Check for build duration
        match = PATTERNS["build_time"].match(line)
        if match:
            minutes, seconds = match.groups()
            summary.duration = float(seconds)
            if minutes:
                summary.duration += int(minutes) * 60
            i += 1
            continue

        i += 1

    # Handle last failure if still collecting
    if current_failure and collecting_stack_trace:
        current_failure.stack_trace = "\n".join(stack_trace_lines)
        _add_failure_to_summary(summary, current_failure)

    # Calculate totals from test classes
    _calculate_totals(summary)

    return summary


def _add_failure_to_summary(summary: MavenTestSummary, failure: TestFailure) -> None:
    """Add a failure or error to the appropriate list in summary."""
    if failure.failure_type == "ERROR":
        summary.error_tests.append(failure)
    else:
        summary.failures.append(failure)


def _calculate_totals(summary: MavenTestSummary) -> None:
    """Calculate total counts from individual test class results (class-level granularity)."""
    for tc in summary.test_classes:
        summary.total += 1  # Count classes, not methods

        # Class-level status determination:
        # - failed: has any failures or errors
        # - skipped: all tests were skipped (skipped == tests_run)
        # - passed: has tests run, no failures/errors, not all skipped
        if tc.failures > 0 or tc.errors > 0:
            summary.failed += 1
            if tc.errors > 0:
                summary.errors += 1
        elif tc.skipped == tc.tests_run and tc.tests_run > 0:
            summary.skipped += 1
        elif tc.tests_run > 0:
            summary.passed += 1

    # Calculate duration from test classes if not found in build time
    if summary.duration == 0:
        summary.duration = sum(tc.time_elapsed for tc in summary.test_classes)


def convert_maven_log_to_summary(log_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convert Maven test log to a standardized summary format (class-level granularity).

    All counts and lists are at test CLASS level, not method level.
    - passed: classes with no failures/errors and not fully skipped
    - failed: classes with any failures or errors
    - skipped: classes where all tests were skipped

    Args:
        log_path: Path to Maven test output log
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with structure:
        {
            "duration": float,
            "summary": {"total": int, "passed": int, "failed": int, "error": int, "skipped": int},
            "results": {
                "failed": [{"nodeid": str, "message": str}, ...],
                "error": [{"nodeid": str, "message": str}, ...],
                "skipped": [class_name, ...],
                "passed": [class_name, ...]
            }
        }
    """
    maven_summary = parse_maven_test_log(log_path)

    # Convert to standardized format (class-level granularity)
    result = {
        "duration": round(maven_summary.duration, 2),
        "summary": {
            "total": maven_summary.total,
            "passed": maven_summary.passed,
            "failed": maven_summary.failed,
            "error": maven_summary.errors,
            "skipped": maven_summary.skipped,
        },
        "results": {
            "failed": [{"nodeid": f.nodeid, "message": f.message or "Test failed"} for f in maven_summary.failures],
            "error": [{"nodeid": e.nodeid, "message": e.message or "Test error"} for e in maven_summary.error_tests],
            "skipped": _get_skipped_classes(maven_summary),
            "passed": _get_passed_classes(maven_summary),
        },
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


def _get_skipped_classes(summary: MavenTestSummary) -> List[str]:
    """
    Get list of fully skipped test classes (all tests in class were skipped).

    Class-level granularity: only includes classes where skipped == tests_run.
    """
    return [tc.class_name for tc in summary.test_classes if tc.skipped == tc.tests_run and tc.tests_run > 0]


def _get_passed_classes(summary: MavenTestSummary) -> List[str]:
    """
    Get list of passed test classes.

    Class-level granularity: classes with no failures/errors and not fully skipped.
    """
    return [
        tc.class_name
        for tc in summary.test_classes
        if tc.failures == 0 and tc.errors == 0 and tc.tests_run > 0 and tc.skipped != tc.tests_run
    ]


def get_failed_test_methods(summary: MavenTestSummary) -> List[str]:
    """
    Get list of failed test method nodeids.

    Args:
        summary: Parsed MavenTestSummary

    Returns:
        List of nodeids in format "org.example.TestClass.testMethod"
    """
    return [f.nodeid for f in summary.failures]


def get_error_test_methods(summary: MavenTestSummary) -> List[str]:
    """
    Get list of error test method nodeids.

    Args:
        summary: Parsed MavenTestSummary

    Returns:
        List of nodeids in format "org.example.TestClass.testMethod"
    """
    return [e.nodeid for e in summary.error_tests]


def extract_failure_message(failure: TestFailure) -> str:
    """
    Extract a clean failure message from a TestFailure.

    Args:
        failure: TestFailure object

    Returns:
        Clean failure message string
    """
    if failure.message:
        return failure.message

    # Try to extract from stack trace
    if failure.stack_trace:
        lines = failure.stack_trace.splitlines()
        if lines:
            return lines[0][:500]  # Truncate long messages

    return f"{failure.failure_type} in {failure.test_method}"


def get_test_classes_with_failures(summary: MavenTestSummary) -> List[str]:
    """
    Get list of test classes that have failures or errors.

    Args:
        summary: Parsed MavenTestSummary

    Returns:
        List of test class names with failures
    """
    return [tc.class_name for tc in summary.test_classes if tc.failures > 0 or tc.errors > 0]


def parse_maven_with_surefire(
    log_path: Optional[Path] = None,
    surefire_path: Optional[Path] = None,
    prefer_xml: bool = True,
) -> Dict[str, Any]:
    """
    Parse Maven test results using both console log and Surefire XML reports.

    This function provides a unified interface that can use either:
    1. Surefire XML reports (method-level granularity, preferred)
    2. Console log output (class-level granularity, fallback)

    The XML parsing provides:
    - Method-level test results instead of just class-level
    - Module path prefix in test IDs to avoid naming collisions
    - Detailed failure messages and stack traces

    Args:
        log_path: Path to Maven console output log (optional)
        surefire_path: Path to Surefire reports (can be directory, archive, or project root)
        prefer_xml: If True, prefer XML over console log when both are available

    Returns:
        Standardized test report dictionary with structure:
        {
            "tests": [{"nodeid": str, "outcome": str, ...}, ...],
            "collectors": [],
            "summary": {"passed": int, "failed": int, "error": int, "skipped": int, "total": int},
            "duration": float,
            "_framework": "maven",
            "_parse_mode": "surefire_xml" or "console_log"
        }
    """
    from harness.utils.maven_surefire_xml_utils import (
        parse_surefire_archive,
        parse_surefire_reports_dir,
        collect_all_surefire_reports,
    )

    result = None

    # Try Surefire XML first if preferred and available
    if prefer_xml and surefire_path:
        surefire_path = Path(surefire_path)
        if surefire_path.exists():
            if surefire_path.suffix == ".gz" or surefire_path.name.endswith(".tar.gz"):
                summary = parse_surefire_archive(surefire_path)
            elif surefire_path.is_dir():
                if surefire_path.name == "surefire-reports":
                    summary = parse_surefire_reports_dir(surefire_path)
                else:
                    summary = collect_all_surefire_reports(surefire_path)
            else:
                summary = None

            if summary and summary.total > 0:
                result = summary.to_dict()

    # Fall back to console log if no XML results
    if result is None and log_path:
        log_path = Path(log_path)
        if log_path.exists():
            maven_summary = parse_maven_test_log(log_path)

            # Convert to standardized format
            tests = []
            for tc in maven_summary.test_classes:
                if tc.failures == 0 and tc.errors == 0:
                    if tc.skipped == tc.tests_run and tc.tests_run > 0:
                        outcome = "skipped"
                    elif tc.tests_run > 0:
                        outcome = "passed"
                    else:
                        continue
                else:
                    outcome = "error" if tc.errors > 0 else "failed"

                tests.append(
                    {
                        "nodeid": tc.class_name,
                        "outcome": outcome,
                    }
                )

            result = {
                "tests": tests,
                "collectors": [],
                "summary": {
                    "passed": maven_summary.passed,
                    "failed": maven_summary.failed,
                    "error": maven_summary.errors,
                    "skipped": maven_summary.skipped,
                    "total": maven_summary.total,
                },
                "duration": round(maven_summary.duration, 2),
                "_framework": "maven",
                "_parse_mode": "console_log",
            }

    # Return empty result if nothing parsed
    if result is None:
        result = {
            "tests": [],
            "collectors": [],
            "summary": {
                "passed": 0,
                "failed": 0,
                "error": 0,
                "skipped": 0,
                "total": 0,
            },
            "duration": 0,
            "_framework": "maven",
            "_parse_mode": "none",
        }

    return result


def print_summary(summary: MavenTestSummary) -> None:
    """
    Print a human-readable summary of test results.

    Args:
        summary: Parsed MavenTestSummary
    """
    print(f"\n{'=' * 60}")
    print("Maven Test Summary")
    print(f"{'=' * 60}")
    print(f"Total Tests:  {summary.total}")
    print(f"Passed:       {summary.passed}")
    print(f"Failed:       {summary.failed}")
    print(f"Errors:       {summary.errors}")
    print(f"Skipped:      {summary.skipped}")
    print(f"Duration:     {summary.duration:.2f}s")

    if summary.failures:
        print(f"\n{'─' * 60}")
        print("Failed Tests:")
        for f in summary.failures:
            print(f"  ✗ {f.nodeid}")
            if f.message:
                print(f"    {f.message[:100]}")

    if summary.error_tests:
        print(f"\n{'─' * 60}")
        print("Error Tests:")
        for e in summary.error_tests:
            print(f"  ✗ {e.nodeid}")
            if e.message:
                print(f"    {e.message[:100]}")

    print(f"{'=' * 60}\n")


# CLI support
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python maven_report_utils.py <log_file> [output_json]")
        sys.exit(1)

    log_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not log_file.exists():
        print(f"Error: Log file not found: {log_file}")
        sys.exit(1)

    summary_dict = convert_maven_log_to_summary(log_file, output_file)

    # Parse again for detailed print
    maven_summary = parse_maven_test_log(log_file)
    print_summary(maven_summary)

    if output_file:
        print(f"Summary saved to: {output_file}")

    # Exit with non-zero if there are failures
    if maven_summary.failed > 0 or maven_summary.errors > 0:
        sys.exit(1)
