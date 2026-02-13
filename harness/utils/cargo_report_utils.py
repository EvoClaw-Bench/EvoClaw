"""
Cargo Test Report Parsing Utilities

This module provides utilities for parsing Cargo (Rust) test output logs
and converting them to a standardized summary format compatible with
maven_report_utils and pytest_report_utils output structure.

Supported patterns:
- Test binary execution: Running `/path/to/test_binary`
- Test count: running X tests
- Individual test results: test module::test_name ... ok/FAILED/ignored
- Test result summary: test result: ok/FAILED. X passed; Y failed; Z ignored; ...
- Doc-tests: Doc-tests crate_name
- Failure details: failures: section with stack traces

Usage:
    from harness.utils.cargo_report_utils import convert_cargo_log_to_summary

    summary = convert_cargo_log_to_summary(Path("output.log"))
    # Or save to file:
    summary = convert_cargo_log_to_summary(Path("output.log"), Path("summary.json"))
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class TestResult:
    """Result for a single test."""

    name: str
    status: str  # "ok", "FAILED", "ignored"
    module: str = ""  # e.g., "glob::tests" or doc-test path
    duration: float = 0.0


@dataclass
class TestSuiteResult:
    """Result for a test suite (binary or doc-tests)."""

    name: str  # Binary name or "Doc-tests crate_name"
    tests_run: int = 0
    passed: int = 0
    failed: int = 0
    ignored: int = 0
    measured: int = 0
    filtered_out: int = 0
    duration: float = 0.0
    tests: List[TestResult] = field(default_factory=list)


@dataclass
class TestFailure:
    """Details of a single test failure."""

    test_name: str
    message: str = ""
    stdout: str = ""
    location: str = ""  # e.g., "src/path.rs:123:5"

    @property
    def nodeid(self) -> str:
        """Return pytest-compatible nodeid format."""
        return self.test_name


@dataclass
class CargoTestSummary:
    """Aggregated summary of Cargo test execution."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    ignored: int = 0
    measured: int = 0
    filtered_out: int = 0
    duration: float = 0.0
    test_suites: List[TestSuiteResult] = field(default_factory=list)
    failures: List[TestFailure] = field(default_factory=list)
    ignored_tests: List[str] = field(default_factory=list)


# Regex patterns for parsing Cargo output
PATTERNS = {
    # Running `/path/to/test_binary` (old format)
    # OR Running unittests path (target/debug/deps/binary) (new format)
    # OR Running tests/path.rs (target/debug/deps/binary) (new format)
    "running_binary": re.compile(r"^\s*Running\s+(?:`(.+)`|(?:unittests\s+)?(.+?)\s+\((.+)\))\s*$"),
    # running X tests
    "running_tests": re.compile(r"^running\s+(\d+)\s+tests?\s*$"),
    # test module::test_name ... ok/FAILED/ignored
    "test_result": re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(ok|FAILED|ignored)\s*$"),
    # test result: ok/FAILED. X passed; Y failed; Z ignored; W measured; V filtered out; finished in T.TTs
    "test_summary": re.compile(
        r"^test result:\s+(ok|FAILED)\.\s+"
        r"(\d+)\s+passed;\s+"
        r"(\d+)\s+failed;\s+"
        r"(\d+)\s+ignored;\s+"
        r"(\d+)\s+measured;\s+"
        r"(\d+)\s+filtered out;\s+"
        r"finished in\s+([\d.]+)s?\s*$"
    ),
    # Doc-tests crate_name
    "doc_tests": re.compile(r"^\s*Doc-tests\s+(.+)\s*$"),
    # failures: (section header)
    "failures_header": re.compile(r"^failures:\s*$"),
    # ---- module::test_name stdout ----
    "failure_stdout": re.compile(r"^----\s+(.+?)\s+stdout\s+----\s*$"),
    # thread 'test_name' panicked at 'message', location
    "panic_message": re.compile(r"thread\s+'(.+?)'\s+panicked\s+at\s+'(.+?)'(?:,\s+(.+?))?$"),
    # thread 'test_name' panicked at location: (Rust 2021+ format)
    "panic_message_new": re.compile(r"thread\s+'(.+?)'\s+panicked\s+at\s+(.+?):\s*$"),
    # assertion failed: ...
    "assertion": re.compile(r"^assertion\s+(.+)$"),
    # Finished test [profile] target(s) in X.XXs
    "finished": re.compile(r"^\s*Finished\s+.*in\s+([\d.]+)s?\s*$"),
}


def parse_cargo_test_log(log_path: Path) -> CargoTestSummary:
    """
    Parse a Cargo test output log file.

    Args:
        log_path: Path to the Cargo test output log

    Returns:
        CargoTestSummary with parsed test results
    """
    summary = CargoTestSummary()

    if not log_path.exists():
        return summary

    content = log_path.read_text(errors="replace")
    lines = content.splitlines()

    current_suite: Optional[TestSuiteResult] = None
    current_failure: Optional[TestFailure] = None
    collecting_failure = False
    failure_stdout_lines: List[str] = []
    in_failures_section = False

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for Running binary
        match = PATTERNS["running_binary"].match(line)
        if match:
            # Handle both old format (group 1) and new format (group 2/3)
            if match.group(1):
                # Old format: Running `/path/to/binary`
                binary_path = match.group(1)
            elif match.group(3):
                # New format: Running unittests/tests path (binary_path)
                binary_path = match.group(3)
            else:
                binary_path = match.group(2) or ""

            # Skip rustdoc (Doc-tests runner)
            if "rustdoc" in binary_path:
                current_suite = None
                i += 1
                continue
            binary_name = Path(binary_path).stem
            current_suite = TestSuiteResult(name=binary_name)
            i += 1
            continue

        # Check for Doc-tests - skip them entirely
        match = PATTERNS["doc_tests"].match(line)
        if match:
            # Skip Doc-tests section until next test suite
            current_suite = None
            i += 1
            continue

        # Check for running X tests
        match = PATTERNS["running_tests"].match(line)
        if match:
            if current_suite:
                current_suite.tests_run = int(match.group(1))
            i += 1
            continue

        # Check for individual test result
        match = PATTERNS["test_result"].match(line)
        if match:
            test_name = match.group(1)
            status = match.group(2)

            # Skip if we're in Doc-tests section (current_suite is None)
            if current_suite is None:
                i += 1
                continue

            test = TestResult(name=test_name, status=status)

            # Extract module from test name (e.g., "glob::tests::any1" -> "glob::tests")
            if "::" in test_name:
                parts = test_name.rsplit("::", 1)
                test.module = parts[0]

            current_suite.tests.append(test)

            # Track ignored tests
            if status == "ignored":
                summary.ignored_tests.append(test_name)

            i += 1
            continue

        # Check for test result summary
        match = PATTERNS["test_summary"].match(line)
        if match:
            status, passed, failed, ignored, measured, filtered, duration = match.groups()

            if current_suite:
                current_suite.passed = int(passed)
                current_suite.failed = int(failed)
                current_suite.ignored = int(ignored)
                current_suite.measured = int(measured)
                current_suite.filtered_out = int(filtered)
                current_suite.duration = float(duration)
                summary.test_suites.append(current_suite)
                current_suite = None

            i += 1
            continue

        # Check for failures section
        if PATTERNS["failures_header"].match(line):
            in_failures_section = True
            i += 1
            continue

        # Check for failure stdout header
        match = PATTERNS["failure_stdout"].match(line)
        if match:
            # Save previous failure if exists
            if current_failure and collecting_failure:
                current_failure.stdout = "\n".join(failure_stdout_lines)
                summary.failures.append(current_failure)

            test_name = match.group(1)
            current_failure = TestFailure(test_name=test_name)
            collecting_failure = True
            failure_stdout_lines = []
            i += 1
            continue

        # Collect failure stdout
        if collecting_failure:
            # Check for panic message
            match = PATTERNS["panic_message"].match(line)
            if match:
                _, message, location = match.groups()
                if current_failure:
                    current_failure.message = message or ""
                    current_failure.location = location or ""
            else:
                match = PATTERNS["panic_message_new"].match(line)
                if match:
                    _, location = match.groups()
                    if current_failure:
                        current_failure.location = location

            # Check if we've reached another failure or end of failures
            if (
                PATTERNS["failure_stdout"].match(line)
                or line.strip() == "failures:"
                or PATTERNS["test_summary"].match(line)
            ):
                if current_failure:
                    current_failure.stdout = "\n".join(failure_stdout_lines)
                    summary.failures.append(current_failure)
                    current_failure = None
                collecting_failure = False
                failure_stdout_lines = []
                continue

            failure_stdout_lines.append(line)

        i += 1

    # Handle last failure if still collecting
    if current_failure and collecting_failure:
        current_failure.stdout = "\n".join(failure_stdout_lines)
        summary.failures.append(current_failure)

    # Calculate totals from test suites
    _calculate_totals(summary)

    return summary


def _calculate_totals(summary: CargoTestSummary) -> None:
    """Calculate total counts from individual test suite results."""
    for suite in summary.test_suites:
        summary.total += suite.passed + suite.failed + suite.ignored
        summary.passed += suite.passed
        summary.failed += suite.failed
        summary.ignored += suite.ignored
        summary.measured += suite.measured
        summary.filtered_out += suite.filtered_out
        summary.duration += suite.duration


def convert_cargo_log_to_summary(log_path: Path, output_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Convert Cargo test log to a standardized summary format.

    This produces output compatible with maven_report_utils and pytest_report_utils,
    allowing unified handling of test results across different frameworks.

    Args:
        log_path: Path to Cargo test output log
        output_path: Optional path to save summary JSON

    Returns:
        Summary dict with structure:
        {
            "duration": float,
            "summary": {"total": int, "passed": int, "failed": int, "error": int, "skipped": int},
            "results": {
                "failed": [{"nodeid": str, "message": str}, ...],
                "error": [],
                "skipped": [{"reason": str, "count": int, "tests": [...]}, ...],
                "passed": [nodeid, ...]
            }
        }
    """
    cargo_summary = parse_cargo_test_log(log_path)

    # Convert to standardized format (compatible with maven/pytest utils)
    result = {
        "duration": round(cargo_summary.duration, 2),
        "summary": {
            "total": cargo_summary.total,
            "passed": cargo_summary.passed,
            "failed": cargo_summary.failed,
            "error": 0,  # Cargo doesn't distinguish errors from failures
            "skipped": cargo_summary.ignored,  # Map "ignored" to "skipped" for consistency
        },
        "results": {
            "failed": [{"nodeid": f.nodeid, "message": f.message or "Test failed"} for f in cargo_summary.failures],
            "error": [],  # Cargo doesn't have separate error category
            "skipped": _group_ignored_tests(cargo_summary),
            "passed": _get_passed_tests(cargo_summary),
        },
    }

    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


def _group_ignored_tests(summary: CargoTestSummary) -> List[Dict[str, Any]]:
    """
    Group ignored tests by module.

    Returns list compatible with maven/pytest utils format.
    """
    if not summary.ignored_tests:
        return []

    # Group by module
    by_module: Dict[str, List[str]] = {}
    for test_name in summary.ignored_tests:
        if "::" in test_name:
            module = test_name.rsplit("::", 1)[0]
        else:
            module = "unknown"

        if module not in by_module:
            by_module[module] = []
        by_module[module].append(test_name)

    return [
        {
            "reason": f"Ignored in {module}",
            "count": len(tests),
            "tests": tests,
        }
        for module, tests in sorted(by_module.items(), key=lambda x: -len(x[1]))
    ]


def _get_passed_tests(summary: CargoTestSummary) -> List[str]:
    """
    Get list of passed tests.

    Returns test names for tests that passed.
    """
    passed_tests = []

    for suite in summary.test_suites:
        for test in suite.tests:
            if test.status == "ok":
                passed_tests.append(test.name)

    return passed_tests


def get_failed_test_names(summary: CargoTestSummary) -> List[str]:
    """
    Get list of failed test names.

    Args:
        summary: Parsed CargoTestSummary

    Returns:
        List of test names that failed
    """
    return [f.test_name for f in summary.failures]


def get_ignored_test_names(summary: CargoTestSummary) -> List[str]:
    """
    Get list of ignored test names.

    Args:
        summary: Parsed CargoTestSummary

    Returns:
        List of test names that were ignored
    """
    return summary.ignored_tests


def print_summary(summary: CargoTestSummary) -> None:
    """
    Print a human-readable summary of test results.

    Args:
        summary: Parsed CargoTestSummary
    """
    print(f"\n{'=' * 60}")
    print("Cargo Test Summary")
    print(f"{'=' * 60}")
    print(f"Total Tests:  {summary.total}")
    print(f"Passed:       {summary.passed}")
    print(f"Failed:       {summary.failed}")
    print(f"Ignored:      {summary.ignored}")
    print(f"Duration:     {summary.duration:.2f}s")
    print(f"Test Suites:  {len(summary.test_suites)}")

    if summary.failures:
        print(f"\n{'─' * 60}")
        print("Failed Tests:")
        for f in summary.failures:
            print(f"  ✗ {f.test_name}")
            if f.message:
                print(f"    {f.message[:100]}")
            if f.location:
                print(f"    at {f.location}")

    if summary.ignored_tests:
        print(f"\n{'─' * 60}")
        print(f"Ignored Tests ({len(summary.ignored_tests)}):")
        for name in summary.ignored_tests[:10]:  # Show first 10
            print(f"  ⊘ {name}")
        if len(summary.ignored_tests) > 10:
            print(f"  ... and {len(summary.ignored_tests) - 10} more")

    print(f"{'=' * 60}\n")


# CLI support
if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python cargo_report_utils.py <log_file> [output_json]")
        sys.exit(1)

    log_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not log_file.exists():
        print(f"Error: Log file not found: {log_file}")
        sys.exit(1)

    summary_dict = convert_cargo_log_to_summary(log_file, output_file)

    # Parse again for detailed print
    cargo_summary = parse_cargo_test_log(log_file)
    print_summary(cargo_summary)

    if output_file:
        print(f"Summary saved to: {output_file}")

    # Exit with non-zero if there are failures
    if cargo_summary.failed > 0:
        sys.exit(1)
