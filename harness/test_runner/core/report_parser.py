"""
Unified Test Report Parser for Multiple Languages and Frameworks.

This module provides a unified interface for parsing test reports from different
test frameworks (pytest, go_test, maven, cargo, jest, vitest, mocha, playwright) and converting them
to a standardized format for milestone test classification.

Supported Frameworks:
    - pytest (Python): JSON report from --json-report
    - go_test (Go): JSON Lines from go test -json, or verbose text from go test -v
    - ginkgo (Go): JSON report from ginkgo --json-report
    - maven (Java): Console log from mvn test
    - cargo (Rust): Console log from cargo test
    - jest (JavaScript): JSON report from --json
    - vitest (JavaScript): JSON report from --reporter=json
    - mocha (JavaScript): JSON report from --reporter json
    - playwright (JavaScript): JSON report from --reporter=json

Standardized Output Format:
    {
        "tests": [
            {"nodeid": "test_id", "outcome": "passed|failed|skipped|error"},
            ...
        ],
        "collectors": [...],  # Optional
        "summary": {"passed": int, "failed": int, "skipped": int, "error": int, "total": int},
        "duration": float,
    }

Usage:
    from harness.test_runner.core.report_parser import parse_test_report, get_report_format

    # Parse a report file
    result = parse_test_report(Path("output.json"), framework="pytest")

    # Auto-detect format from file extension
    result = parse_test_report(Path("output.jsonl"), framework="go_test")
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import existing parsers
from harness.utils.pytest_report_utils import convert_pytest_report_to_summary
from harness.utils.go_report_utils import (
    parse_go_test_output,
    parse_go_test_jsonl,
    parse_go_test_verbose,
    parse_ginkgo_json_report,
    convert_ginkgo_report_to_dict,
)
from harness.utils.maven_report_utils import parse_maven_test_log, parse_maven_with_surefire
from harness.utils.maven_surefire_xml_utils import (
    parse_surefire_archive,
    parse_surefire_reports_dir,
    collect_all_surefire_reports,
    SurefireXmlSummary,
)
from harness.utils.cargo_report_utils import parse_cargo_test_log
from harness.utils.django_report_utils import parse_django_test_log


# Framework-specific configurations
FRAMEWORK_CONFIG = {
    "pytest": {
        "report_format": "json",
        "file_extension": ".json",
        "outcome_map": {
            "passed": "passed",
            "failed": "failed",
            "skipped": "skipped",
            "error": "error",
            "xfailed": "passed",  # Expected failure = success
            "xpassed": "failed",  # Unexpected pass = failure
        },
    },
    "go_test": {
        "report_format": "jsonl",
        "file_extension": ".jsonl",
        "outcome_map": {
            "pass": "passed",
            "fail": "failed",
            "skip": "skipped",
        },
    },
    "ginkgo": {
        "report_format": "json",
        "file_extension": ".json",
        "outcome_map": {
            "passed": "passed",
            "failed": "failed",
            "skipped": "skipped",
            "pending": "skipped",
            "panicked": "error",
            "interrupted": "error",
            "aborted": "error",
        },
    },
    "maven": {
        "report_format": "mixed",  # Supports both log and surefire XML
        "file_extension": ".log",
        "surefire_archive": "surefire_reports.tar.gz",  # Collected surefire reports
        "prefer_surefire_xml": True,  # Prefer XML for method-level granularity
        "outcome_map": {
            "SUCCESS": "passed",
            "FAILURE": "failed",
            "ERROR": "error",
            "SKIPPED": "skipped",
        },
    },
    "cargo": {
        "report_format": "log",
        "file_extension": ".log",
        "outcome_map": {
            "ok": "passed",
            "FAILED": "failed",
            "ignored": "skipped",
        },
    },
    "jest": {
        "report_format": "json",
        "file_extension": ".json",
        "outcome_map": {
            "passed": "passed",
            "failed": "failed",
            "pending": "skipped",
            "skipped": "skipped",
        },
    },
    "vitest": {
        "report_format": "json",
        "file_extension": ".json",
        "outcome_map": {
            "passed": "passed",
            "failed": "failed",
            "pending": "skipped",
            "skipped": "skipped",
        },
    },
    "mocha": {
        "report_format": "json",
        "file_extension": ".json",
        "outcome_map": {
            "passed": "passed",
            "failed": "failed",
            "pending": "skipped",
        },
    },
    "unittest": {
        "report_format": "log",
        "file_extension": ".log",
        "outcome_map": {
            "ok": "passed",
            "FAIL": "failed",
            "ERROR": "error",
            "skipped": "skipped",
        },
    },
    "gradle": {
        "report_format": "log",
        "file_extension": ".log",
        "outcome_map": {
            "PASSED": "passed",
            "FAILED": "failed",
            "SKIPPED": "skipped",
        },
    },
    "playwright": {
        "report_format": "json",
        "file_extension": ".json",
        "outcome_map": {
            "expected": "passed",
            "unexpected": "failed",
            "skipped": "skipped",
            "flaky": "passed",  # Flaky tests eventually passed
            "passed": "passed",
            "failed": "failed",
            "timedOut": "failed",
            "interrupted": "error",
        },
    },
    "django_runtests": {
        "report_format": "log",
        "file_extension": ".log",
        "outcome_map": {
            "ok": "passed",
            "FAIL": "failed",
            "ERROR": "error",
            "skipped": "skipped",
        },
    },
    "nushell_script": {
        "report_format": "log",
        "file_extension": ".json",  # We output to .json but it's actually a log
        "outcome_map": {
            "passed": "passed",
            "failed": "failed",
        },
    },
}


def get_report_format(framework: str) -> str:
    """
    Get the report format for a given test framework.

    Args:
        framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha)

    Returns:
        Report format string: "json", "jsonl", or "log"
    """
    config = FRAMEWORK_CONFIG.get(framework, FRAMEWORK_CONFIG["pytest"])
    return config["report_format"]


def get_file_extension(framework: str) -> str:
    """
    Get the expected file extension for a framework's report.

    Args:
        framework: Test framework name

    Returns:
        File extension string (e.g., ".json", ".jsonl", ".log")
    """
    config = FRAMEWORK_CONFIG.get(framework, FRAMEWORK_CONFIG["pytest"])
    return config["file_extension"]


def normalize_outcome(outcome: str, framework: str) -> str:
    """
    Normalize a framework-specific outcome to standard outcome.

    Standard outcomes: "passed", "failed", "skipped", "error"

    Args:
        outcome: Framework-specific outcome string
        framework: Test framework name

    Returns:
        Normalized outcome string
    """
    config = FRAMEWORK_CONFIG.get(framework, FRAMEWORK_CONFIG["pytest"])
    outcome_map = config["outcome_map"]
    return outcome_map.get(outcome, "failed")  # Default unknown outcomes to failed


def parse_pytest_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a pytest JSON report file and convert to standardized format.

    Args:
        report_path: Path to pytest-json-report output file

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    with open(report_path) as f:
        data = json.load(f)

    # pytest-json-report already has the format we need
    # Just ensure consistent structure
    tests = []
    for test in data.get("tests", []):
        tests.append(
            {
                "nodeid": test.get("nodeid", ""),
                "outcome": test.get("outcome", "unknown"),
            }
        )

    summary = data.get("summary", {})

    return {
        "tests": tests,
        "collectors": data.get("collectors", []),
        "summary": {
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "skipped": summary.get("skipped", 0),
            "error": summary.get("error", 0),
            "total": summary.get("total", len(tests)),
        },
        "duration": data.get("duration", 0),
        "_framework": "pytest",
        "_raw_data": data,
    }


def parse_go_test_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Go test report (JSONL or verbose log) and convert to standardized format.

    Args:
        report_path: Path to Go test output file

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    # Use the existing parser which auto-detects format
    parsed = parse_go_test_output(report_path)

    # Convert to our standardized test format
    tests = []

    # Add passed tests
    for nodeid in parsed.get("results", {}).get("passed", []):
        tests.append({"nodeid": nodeid, "outcome": "passed"})

    # Add failed tests
    for failed in parsed.get("results", {}).get("failed", []):
        nodeid = failed.get("nodeid", "") if isinstance(failed, dict) else failed
        tests.append({"nodeid": nodeid, "outcome": "failed"})

    # Add skipped tests
    for skip_group in parsed.get("results", {}).get("skipped", []):
        if isinstance(skip_group, dict):
            for nodeid in skip_group.get("tests", []):
                tests.append({"nodeid": nodeid, "outcome": "skipped"})
        else:
            tests.append({"nodeid": skip_group, "outcome": "skipped"})

    # Add error tests
    for error in parsed.get("results", {}).get("error", []):
        nodeid = error.get("nodeid", "") if isinstance(error, dict) else error
        tests.append({"nodeid": nodeid, "outcome": "error"})

    summary = parsed.get("summary", {})

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": summary.get("passed", 0),
            "failed": summary.get("failed", 0),
            "skipped": summary.get("skipped", 0),
            "error": summary.get("error", 0),
            "total": summary.get("total", len(tests)),
        },
        "duration": parsed.get("duration", 0),
        "_framework": "go_test",
        "_raw_data": parsed,
    }


def _detect_go_module(report_path: Path) -> str:
    """
    Try to detect Go module path from go.mod file near the report.

    Looks for go.mod in the report directory and parent directories.

    Args:
        report_path: Path to the test report file

    Returns:
        Go module path if found, empty string otherwise
    """
    # Look for go.mod in parent directories
    search_dirs = [report_path.parent]
    # Also check common locations relative to report
    if report_path.parent.name in ("attempt_1", "attempt_2", "attempt_3"):
        search_dirs.append(report_path.parent.parent.parent / "testbed")
    if "test_results" in str(report_path):
        # Look for testbed sibling to test_results
        parts = report_path.parts
        for i, part in enumerate(parts):
            if part == "test_results":
                testbed_path = Path(*parts[:i]) / "testbed"
                if testbed_path.exists():
                    search_dirs.append(testbed_path)
                break

    for search_dir in search_dirs:
        go_mod = search_dir / "go.mod"
        if go_mod.exists():
            try:
                content = go_mod.read_text()
                for line in content.splitlines():
                    line = line.strip()
                    if line.startswith("module "):
                        return line[7:].strip()
            except IOError:
                pass

    return ""


def parse_ginkgo_report(
    report_path: Path,
    go_module: str = "",
) -> Dict[str, Any]:
    """
    Parse a Ginkgo JSON report and convert to standardized format.

    Ginkgo generates JSON reports with --json-report flag. These reports contain
    spec-level details including the full hierarchy of Describe/Context/It blocks.

    Test ID format:
        {package}::{Describe > Context > ... > It}
        Example: github.com/navidrome/navidrome/plugins::Adapter Media Agent > Album methods > should return album

    Args:
        report_path: Path to Ginkgo JSON report file
        go_module: Optional Go module path for constructing full package paths.
                   If not provided, will try to detect from go.mod.

    Returns:
        Standardized test report dict with structure:
        {
            "tests": [{"nodeid": str, "outcome": str}, ...],
            "summary": {...},
            "duration": float,
            "_framework": "ginkgo"
        }
    """
    if not report_path.exists():
        return _empty_report()

    # Try to auto-detect go_module if not provided
    if not go_module:
        go_module = _detect_go_module(report_path)

    # Use the Ginkgo parser from go_report_utils
    return convert_ginkgo_report_to_dict(report_path, go_module)


def parse_maven_report(
    report_path: Path,
    surefire_path: Optional[Path] = None,
    prefer_surefire_xml: bool = True,
) -> Dict[str, Any]:
    """
    Parse a Maven test report and convert to standardized format.

    Supports two parsing modes:
    1. Surefire XML reports (method-level granularity, preferred)
       - Provides individual test method results
       - Includes module path prefix to avoid naming collisions
       - Contains detailed failure messages

    2. Console log output (class-level granularity, fallback)
       - Only provides class-level pass/fail status
       - No module differentiation for same-named classes

    Args:
        report_path: Path to Maven test output log
        surefire_path: Optional path to Surefire reports (archive, directory, or project root)
        prefer_surefire_xml: If True, prefer XML parsing when available

    Returns:
        Standardized test report dict with structure:
        {
            "tests": [{"nodeid": str, "outcome": str, ...}, ...],
            "collectors": [],
            "summary": {...},
            "duration": float,
            "_framework": "maven",
            "_parse_mode": "surefire_xml" or "console_log"
        }
    """
    # Try Surefire XML first if preferred and path is provided
    if prefer_surefire_xml and surefire_path and surefire_path.exists():
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
            return summary.to_dict()

    # Also check for surefire archive in the same directory as log file
    if prefer_surefire_xml and report_path.exists():
        # Check for state-specific archive first (e.g., start_surefire_reports.tar.gz)
        # Extract state from filename like "start_default.log" -> "start"
        state_prefix = report_path.stem.split("_")[0] if "_" in report_path.stem else ""

        archive_candidates = [
            report_path.parent / f"{state_prefix}_surefire_reports.tar.gz",  # state-specific
            report_path.parent / "surefire_reports.tar.gz",  # generic
        ]

        for surefire_archive in archive_candidates:
            if surefire_archive.exists():
                summary = parse_surefire_archive(surefire_archive)
                if summary and summary.total > 0:
                    return summary.to_dict()

    # Fall back to console log parsing
    if not report_path.exists():
        return _empty_report()

    maven_summary = parse_maven_test_log(report_path)

    tests = []

    # Add passed classes
    for tc in maven_summary.test_classes:
        if tc.failures == 0 and tc.errors == 0:
            if tc.skipped == tc.tests_run and tc.tests_run > 0:
                tests.append({"nodeid": tc.class_name, "outcome": "skipped"})
            elif tc.tests_run > 0:
                tests.append({"nodeid": tc.class_name, "outcome": "passed"})
        else:
            # Has failures or errors
            if tc.errors > 0:
                tests.append({"nodeid": tc.class_name, "outcome": "error"})
            else:
                tests.append({"nodeid": tc.class_name, "outcome": "failed"})

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": maven_summary.passed,
            "failed": maven_summary.failed,
            "skipped": maven_summary.skipped,
            "error": maven_summary.errors,
            "total": maven_summary.total,
        },
        "duration": maven_summary.duration,
        "_framework": "maven",
        "_parse_mode": "console_log",
        "_raw_data": None,  # Don't serialize the dataclass
    }


def parse_cargo_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Cargo test log and convert to standardized format.

    Args:
        report_path: Path to Cargo test output log

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    cargo_summary = parse_cargo_test_log(report_path)

    tests = []

    # Add all tests from test suites
    for suite in cargo_summary.test_suites:
        for test in suite.tests:
            outcome = normalize_outcome(test.status, "cargo")
            tests.append({"nodeid": test.name, "outcome": outcome})

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": cargo_summary.passed,
            "failed": cargo_summary.failed,
            "skipped": cargo_summary.ignored,
            "error": 0,
            "total": cargo_summary.total,
        },
        "duration": cargo_summary.duration,
        "_framework": "cargo",
        "_raw_data": None,
    }


def parse_jest_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Jest/Vitest JSON report and convert to standardized format.

    Jest/Vitest JSON format:
    {
        "numTotalTests": int,
        "numPassedTests": int,
        "numFailedTests": int,
        "numPendingTests": int,
        "testResults": [
            {
                "name": "path/to/test.js",
                "assertionResults": [
                    {
                        "fullName": "test name",
                        "title": "it block title",
                        "ancestorTitles": ["describe block", ...],
                        "status": "passed|failed|pending"
                    }
                ]
            }
        ]
    }

    The nodeid format matches baseline generation:
    - File path: relative path without /testbed prefix (e.g., "ui/src/test.jsx")
    - Test name: ancestorTitles joined with " > " plus title (e.g., "Describe > It")

    Args:
        report_path: Path to Jest/Vitest JSON output

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    with open(report_path) as f:
        data = json.load(f)

    tests = []

    for test_file in data.get("testResults", []):
        file_path = test_file.get("name", "")

        # Normalize file path: remove /testbed prefix to match baseline format
        if file_path.startswith("/testbed/"):
            file_path = file_path[len("/testbed/") :]

        for assertion in test_file.get("assertionResults", []):
            # Build test name from ancestorTitles and title, joined with " > "
            # This matches the baseline format (e.g., "Describe > Context > It")
            ancestor_titles = assertion.get("ancestorTitles", [])
            title = assertion.get("title", "")

            if ancestor_titles or title:
                # Join ancestor titles with " > ", then add title
                name_parts = list(ancestor_titles) + ([title] if title else [])
                test_name = " > ".join(name_parts)
            else:
                # Fall back to fullName if no ancestorTitles/title
                test_name = assertion.get("fullName", "")

            status = assertion.get("status", "failed")
            nodeid = f"{file_path}::{test_name}" if file_path else test_name
            outcome = normalize_outcome(status, "jest")
            tests.append({"nodeid": nodeid, "outcome": outcome})

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": data.get("numPassedTests", 0),
            "failed": data.get("numFailedTests", 0),
            "skipped": data.get("numPendingTests", 0),
            "error": 0,
            "total": data.get("numTotalTests", len(tests)),
        },
        "duration": data.get("testRuntime", 0) / 1000 if "testRuntime" in data else 0,
        "_framework": "jest",
        "_raw_data": data,
    }


def parse_vitest_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Vitest JSON report and convert to standardized format.

    Vitest JSON format (similar to Jest but with some differences):
    {
        "numTotalTests": int,
        "numPassedTests": int,
        "numFailedTests": int,
        "numPendingTests": int,
        "numTodoTests": int,
        "startTime": int,
        "success": bool,
        "testResults": [
            {
                "name": "/path/to/test.js",
                "status": "passed|failed",
                "assertionResults": [
                    {
                        "ancestorTitles": ["describe block", "nested describe"],
                        "fullName": "describe block > nested describe > test name",
                        "title": "test name",
                        "status": "passed|failed",
                        "duration": float,
                        "failureMessages": []
                    }
                ]
            }
        ]
    }

    Test ID format:
        {relative_file_path}::{ancestorTitles > title}
        Example: ui/src/utils/formatters.test.js::formatDuration2 > handles null values

    Args:
        report_path: Path to Vitest JSON output

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    with open(report_path) as f:
        data = json.load(f)

    tests = []
    total_duration = 0.0

    for test_file in data.get("testResults", []):
        # Get relative file path (remove container prefixes like /testbed/)
        file_path = test_file.get("name", "")
        # Try to make path relative by removing container/environment prefixes only
        # Keep project-relative paths like ui/src/... intact for consistency with test extraction
        for prefix in ["/testbed/", "/app/", "/workspace/"]:
            if file_path.startswith(prefix):
                file_path = file_path[len(prefix) :]
                break

        for assertion in test_file.get("assertionResults", []):
            # Build test name from ancestorTitles and title
            ancestor_titles = assertion.get("ancestorTitles", [])
            title = assertion.get("title", "")

            # Construct the full test name
            if ancestor_titles:
                test_name = " > ".join(ancestor_titles + [title])
            else:
                test_name = title or assertion.get("fullName", "")

            status = assertion.get("status", "failed")
            nodeid = f"{file_path}::{test_name}" if file_path else test_name
            outcome = normalize_outcome(status, "vitest")

            test_entry = {"nodeid": nodeid, "outcome": outcome}

            # Include failure messages if present
            failure_messages = assertion.get("failureMessages", [])
            if failure_messages:
                test_entry["message"] = failure_messages[0][:500]  # Truncate long messages

            tests.append(test_entry)

            # Accumulate duration
            duration = assertion.get("duration", 0)
            if duration:
                total_duration += duration / 1000  # Convert ms to seconds

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": data.get("numPassedTests", 0),
            "failed": data.get("numFailedTests", 0),
            "skipped": data.get("numPendingTests", 0) + data.get("numTodoTests", 0),
            "error": 0,
            "total": data.get("numTotalTests", len(tests)),
        },
        "duration": total_duration,
        "_framework": "vitest",
        "_raw_data": data,
    }


def parse_mocha_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Mocha JSON report and convert to standardized format.

    Mocha JSON format:
    {
        "stats": {"passes": int, "failures": int, "pending": int, "duration": int},
        "passes": [{"fullTitle": "test name"}, ...],
        "failures": [{"fullTitle": "test name", "err": {...}}, ...],
        "pending": [{"fullTitle": "test name"}, ...]
    }

    Args:
        report_path: Path to Mocha JSON output

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    with open(report_path) as f:
        data = json.load(f)

    tests = []
    stats = data.get("stats", {})

    for test in data.get("passes", []):
        tests.append(
            {
                "nodeid": test.get("fullTitle", ""),
                "outcome": "passed",
            }
        )

    for test in data.get("failures", []):
        tests.append(
            {
                "nodeid": test.get("fullTitle", ""),
                "outcome": "failed",
            }
        )

    for test in data.get("pending", []):
        tests.append(
            {
                "nodeid": test.get("fullTitle", ""),
                "outcome": "skipped",
            }
        )

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": stats.get("passes", 0),
            "failed": stats.get("failures", 0),
            "skipped": stats.get("pending", 0),
            "error": 0,
            "total": len(tests),
        },
        "duration": stats.get("duration", 0) / 1000 if "duration" in stats else 0,
        "_framework": "mocha",
        "_raw_data": data,
    }


def parse_playwright_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Playwright JSON report and convert to standardized format.

    Playwright JSON format (from --reporter=json):
    {
        "config": {...},
        "suites": [
            {
                "title": "suite name",
                "file": "path/to/test.spec.ts",
                "suites": [...],  // nested suites
                "specs": [
                    {
                        "title": "test name",
                        "file": "path/to/test.spec.ts",
                        "line": 10,
                        "tests": [
                            {
                                "expectedStatus": "passed",
                                "status": "expected|unexpected|skipped|flaky",
                                "projectName": "chromium",
                                "results": [{"status": "passed|failed|timedOut", "duration": 1234}]
                            }
                        ]
                    }
                ]
            }
        ],
        "errors": []
    }

    Test ID format:
        {relative_file_path}::{suite title(s) > spec title}
        Example: tests/login.spec.ts::Login Page > should login successfully

    Args:
        report_path: Path to Playwright JSON output

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    with open(report_path) as f:
        data = json.load(f)

    tests = []
    total_duration = 0.0
    summary_counts = {"passed": 0, "failed": 0, "skipped": 0, "error": 0}

    def process_suite(suite: Dict[str, Any], parent_titles: List[str]) -> None:
        """Recursively process suites and their specs."""
        nonlocal total_duration

        suite_title = suite.get("title", "")
        current_titles = parent_titles + [suite_title] if suite_title else parent_titles

        # Process specs in this suite
        for spec in suite.get("specs", []):
            spec_title = spec.get("title", "")
            file_path = spec.get("file", "")

            # Remove container prefixes from file path
            for prefix in ["/testbed/", "/app/", "/workspace/"]:
                if file_path.startswith(prefix):
                    file_path = file_path[len(prefix) :]
                    break

            # Build full test name from suite hierarchy
            all_titles = current_titles + [spec_title]
            # Filter out empty titles
            all_titles = [t for t in all_titles if t]
            test_name = " > ".join(all_titles) if all_titles else spec_title

            # Each spec can have multiple tests (one per project/browser)
            for test in spec.get("tests", []):
                project_name = test.get("projectName", "")
                status = test.get("status", "unexpected")

                # Construct nodeid with optional project suffix
                if project_name:
                    nodeid = f"{file_path}::{test_name} [{project_name}]"
                else:
                    nodeid = f"{file_path}::{test_name}" if file_path else test_name

                # Normalize outcome
                outcome = normalize_outcome(status, "playwright")

                # Track summary counts
                if outcome in summary_counts:
                    summary_counts[outcome] += 1

                # Get duration from results
                for result in test.get("results", []):
                    duration_ms = result.get("duration", 0)
                    total_duration += duration_ms / 1000

                test_entry = {"nodeid": nodeid, "outcome": outcome}

                # Include error message if failed
                if outcome in ("failed", "error"):
                    for result in test.get("results", []):
                        error = result.get("error", {})
                        if error:
                            message = error.get("message", "")
                            if message:
                                test_entry["message"] = message[:500]
                                break

                tests.append(test_entry)

        # Recursively process nested suites
        for nested_suite in suite.get("suites", []):
            process_suite(nested_suite, current_titles)

    # Process all top-level suites
    for suite in data.get("suites", []):
        process_suite(suite, [])

    # Handle top-level errors (e.g., syntax errors preventing test execution)
    for error in data.get("errors", []):
        error_msg = error.get("message", "Unknown error")
        tests.append(
            {
                "nodeid": f"<error>::{error_msg[:100]}",
                "outcome": "error",
                "message": error_msg[:500],
            }
        )
        summary_counts["error"] += 1

    summary_counts["total"] = len(tests)

    return {
        "tests": tests,
        "collectors": [],
        "summary": summary_counts,
        "duration": total_duration,
        "_framework": "playwright",
        "_raw_data": data,
    }


def parse_django_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Django runtests.py log and convert to standardized format.

    Django runtests.py output format (with --verbosity=2):
        test_name (module.TestClass) ... ok/FAIL/ERROR/skipped 'reason'

        Ran X tests in Y.YYYs
        FAILED (failures=N, errors=M, skipped=K)

    Args:
        report_path: Path to Django test output log

    Returns:
        Standardized test report dict
    """
    return parse_django_test_log(report_path)


def _extract_nushell_test_functions(nu_file_path: Path) -> List[str]:
    """
    Extract @test function names from a .nu test file.

    Looks for patterns like:
        @test
        def test_name [] {
        or
        @test
        def "test name with spaces" [] {
        or
        #[test]
        def test_name [] {

    Args:
        nu_file_path: Path to the .nu test file

    Returns:
        List of test function names
    """
    import re

    if not nu_file_path.exists():
        return []

    try:
        content = nu_file_path.read_text(errors="replace")
    except Exception:
        return []

    test_functions = []

    # Pattern: @test or #[test] followed by def function_name
    # The @test decorator is on a line by itself, followed by def
    lines = content.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Check for @test annotation
        if line == "@test" or line == "#[test]":
            # Look for the next def line
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if next_line.startswith("def "):
                    # Extract function name - handle both quoted and unquoted names
                    # Pattern 1: def "function name with spaces" [
                    # Pattern 2: def function_name [
                    quoted_match = re.match(r'def\s+"([^"]+)"', next_line)
                    if quoted_match:
                        # Replace spaces with underscores for consistency
                        func_name = quoted_match.group(1).replace(" ", "_")
                        test_functions.append(func_name)
                    else:
                        unquoted_match = re.match(r"def\s+([a-zA-Z_][a-zA-Z0-9_-]*)", next_line)
                        if unquoted_match:
                            test_functions.append(unquoted_match.group(1))
                    break
                elif next_line and not next_line.startswith("#"):
                    # Non-empty, non-comment line that's not a def - stop looking
                    break
                j += 1
        i += 1

    return test_functions


def _find_testbed_path(report_path: Path) -> Optional[Path]:
    """
    Find the testbed directory from a report path.

    Looks for testbed in parent directories of the report file.
    Expected structure:
        .../baseline_xxx/testbed/
        .../baseline_xxx/test_results/milestone_xxx/attempt_1/report.json

    Args:
        report_path: Path to the test report file

    Returns:
        Path to testbed directory if found, None otherwise
    """
    # Navigate up from report path to find testbed
    current = report_path.parent
    for _ in range(10):  # Limit search depth
        testbed = current / "testbed"
        if testbed.exists() and testbed.is_dir():
            return testbed
        # Also check sibling directories
        if current.parent:
            testbed = current.parent / "testbed"
            if testbed.exists() and testbed.is_dir():
                return testbed
        current = current.parent
        if current == current.parent:  # Reached root
            break
    return None


def parse_nushell_script_report(report_path: Path) -> Dict[str, Any]:
    """
    Parse a Nushell script test log and convert to standardized format.

    Nushell script test output format:
        === TEST FILE: crates/nu-std/tests/test_foo.nu ===
        (optional error output)
        FAILED: crates/nu-std/tests/test_foo.nu
        === TEST FILE: crates/nu-std/tests/test_bar.nu ===
        (no error = passed)

    For each test file, extracts individual @test functions and generates
    fine-grained test results with nodeid format:
        {file_stem}::{function_name}
        Example: test_asserts::assert_basic

    Args:
        report_path: Path to Nushell script test output log

    Returns:
        Standardized test report dict
    """
    if not report_path.exists():
        return _empty_report()

    try:
        content = report_path.read_text(errors="replace")
    except Exception:
        return _empty_report()

    # Find testbed to read .nu files
    testbed_path = _find_testbed_path(report_path)

    tests = []
    passed = 0
    failed = 0

    # Track failed test files
    failed_tests = set()

    lines = content.split("\n")

    # First pass: collect all failed tests
    for line in lines:
        if line.startswith("FAILED: "):
            failed_test = line[8:].strip()
            failed_tests.add(failed_test)

    # Second pass: process test files and extract @test functions
    for line in lines:
        if line.startswith("=== TEST FILE: ") and line.endswith(" ==="):
            # Extract test file path (e.g., crates/nu-std/tests/test_asserts.nu)
            test_file = line[15:-4].strip()
            file_outcome = "failed" if test_file in failed_tests else "passed"

            # Try to extract individual test functions
            test_functions = []
            if testbed_path:
                nu_file_path = testbed_path / test_file
                test_functions = _extract_nushell_test_functions(nu_file_path)

            if test_functions:
                # Generate fine-grained test results
                # Use file stem (without extension) as module name
                file_stem = Path(test_file).stem  # e.g., test_asserts
                for func_name in test_functions:
                    nodeid = f"{file_stem}::{func_name}"
                    tests.append(
                        {
                            "nodeid": nodeid,
                            "outcome": file_outcome,
                        }
                    )
                    if file_outcome == "passed":
                        passed += 1
                    else:
                        failed += 1
            else:
                # Fallback: use file-level granularity
                tests.append(
                    {
                        "nodeid": test_file,
                        "outcome": file_outcome,
                    }
                )
                if file_outcome == "passed":
                    passed += 1
                else:
                    failed += 1

    return {
        "tests": tests,
        "collectors": [],
        "summary": {
            "passed": passed,
            "failed": failed,
            "skipped": 0,
            "error": 0,
            "total": passed + failed,
        },
        "duration": 0,
        "_framework": "nushell_script",
        "_raw_data": content,
    }


def parse_test_report(report_path: Path, framework: str = "pytest") -> Dict[str, Any]:
    """
    Parse a test report file from any supported framework.

    This is the main entry point for parsing test reports. It dispatches
    to the appropriate parser based on the framework.

    If the file is already in standardized format (has "tests" array with
    "nodeid" and "outcome"), it returns the data directly without re-parsing.

    Args:
        report_path: Path to the test report file
        framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha, playwright)

    Returns:
        Standardized test report dict with structure:
        {
            "tests": [{"nodeid": str, "outcome": str}, ...],
            "collectors": [...],
            "summary": {"passed": int, "failed": int, "skipped": int, "error": int, "total": int},
            "duration": float,
            "_framework": str,
            "_raw_data": Any  # Original parsed data (may be None)
        }
    """
    if not report_path.exists():
        return _empty_report()

    # Try loading as pre-parsed standardized JSON first
    if report_path.suffix == ".json":
        try:
            with open(report_path) as f:
                data = json.load(f)
            # Check if it's already in standardized format
            if "tests" in data and isinstance(data["tests"], list):
                # Verify it has the expected structure
                if not data["tests"] or ("nodeid" in data["tests"][0] and "outcome" in data["tests"][0]):
                    return data
        except (json.JSONDecodeError, KeyError, IndexError):
            pass

    # Fall back to framework-specific parsing
    parsers = {
        "pytest": parse_pytest_report,
        "unittest": parse_pytest_report,  # unittest can use same format if using pytest runner
        "go_test": parse_go_test_report,
        "ginkgo": parse_ginkgo_report,  # Ginkgo with --json-report for spec-level granularity
        "maven": parse_maven_report,
        "gradle": parse_maven_report,  # Similar log format
        "cargo": parse_cargo_report,
        "jest": parse_jest_report,
        "vitest": parse_vitest_report,  # Vitest (similar to Jest but with ancestorTitles)
        "mocha": parse_mocha_report,
        "playwright": parse_playwright_report,
        "django_runtests": parse_django_report,  # Django runtests.py verbose output
        "nushell_script": parse_nushell_script_report,  # Nushell .nu script tests
    }

    parser = parsers.get(framework, parse_pytest_report)
    return parser(report_path)


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
        },
        "duration": 0,
        "_framework": "unknown",
        "_raw_data": None,
    }


def _detect_framework_from_filename(filename: str, default_framework: str) -> str:
    """
    Detect the test framework from the filename.

    Files named with specific suffixes indicate the framework:
    - *_e2e.json -> playwright
    - *_playwright.json -> playwright
    - *_jest.json -> jest
    - *_pytest.json -> pytest
    - etc.

    Args:
        filename: The filename to check
        default_framework: Framework to use if detection fails

    Returns:
        Detected framework name
    """
    name_lower = filename.lower()

    # Check for framework-specific suffixes
    framework_suffixes = {
        "_e2e": "playwright",
        "_playwright": "playwright",
        "_jest": "jest",
        "_mocha": "mocha",
        "_vitest": "vitest",
        "_pytest": "pytest",
        "_go_test": "go_test",
        "_cargo": "cargo",
        "_maven": "maven",
        "_nu_std_scripts": "nushell_script",
        "_nushell": "nushell_script",
    }

    for suffix, framework in framework_suffixes.items():
        if suffix in name_lower:
            return framework

    return default_framework


def merge_test_reports(
    report_files: List[Path],
    output_file: Path,
    framework: str = "pytest",
    verbose: bool = False,
) -> bool:
    """
    Merge multiple test reports into a single report.

    This is used when test_config.json specifies multiple test runs
    (e.g., normal tests + integration tests that use exclusive flags).

    Merge Strategy:
    - For the same test (same nodeid), prioritize non-skipped results
    - If multiple non-skipped results exist, use the first one
    - Recalculate summary statistics after merging
    - Auto-detect framework from filename (e.g., *_e2e.json uses playwright)

    Args:
        report_files: List of test report files to merge
        output_file: Output file path for merged report
        framework: Default test framework (used when detection fails)
        verbose: Whether to print verbose output

    Returns:
        True if merge was successful, False otherwise
    """
    import logging

    logger = logging.getLogger(__name__)

    # Outcome priority: lower number = higher priority (prefer actual runs over skipped)
    OUTCOME_PRIORITY = {
        "passed": 1,
        "failed": 1,
        "error": 1,
        "skipped": 3,
    }

    merged_tests: Dict[str, Dict[str, Any]] = {}  # nodeid -> test_result
    total_duration = 0.0
    all_collectors = []
    seen_collector_nodeids = set()
    valid_reports = 0
    frameworks_used = set()

    for report_file in report_files:
        if not report_file.exists():
            if verbose:
                logger.warning(f"Report file not found: {report_file}")
            continue

        # Auto-detect framework from filename
        detected_framework = _detect_framework_from_filename(report_file.name, framework)
        frameworks_used.add(detected_framework)

        try:
            parsed = parse_test_report(report_file, detected_framework)
        except Exception as e:
            if verbose:
                logger.warning(f"Failed to parse report {report_file} with framework {detected_framework}: {e}")
            continue

        valid_reports += 1
        total_duration += parsed.get("duration", 0)

        # Merge collectors (deduplicate by nodeid)
        for collector in parsed.get("collectors", []):
            nodeid = collector.get("nodeid", "")
            if nodeid and nodeid not in seen_collector_nodeids:
                seen_collector_nodeids.add(nodeid)
                all_collectors.append(collector)

        # Merge tests
        for test in parsed.get("tests", []):
            nodeid = test.get("nodeid", "")
            outcome = test.get("outcome", "unknown")

            # Normalize outcome using the detected framework
            normalized_outcome = (
                normalize_outcome(outcome, detected_framework) if outcome not in OUTCOME_PRIORITY else outcome
            )

            if nodeid not in merged_tests:
                merged_tests[nodeid] = {"nodeid": nodeid, "outcome": normalized_outcome}
            else:
                existing = merged_tests[nodeid]
                existing_priority = OUTCOME_PRIORITY.get(existing.get("outcome", "unknown"), 3)
                new_priority = OUTCOME_PRIORITY.get(normalized_outcome, 3)

                if new_priority < existing_priority:
                    merged_tests[nodeid] = {"nodeid": nodeid, "outcome": normalized_outcome}

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
    }

    for test in merged_tests.values():
        outcome = test.get("outcome", "unknown")
        if outcome in summary:
            summary[outcome] += 1

    summary["total"] = len(merged_tests)

    # Build merged report in standardized format
    merged_report = {
        "tests": list(merged_tests.values()),
        "collectors": all_collectors,
        "summary": summary,
        "duration": total_duration,
        "_framework": framework,
        "_merge_info": {
            "source_files": [str(f) for f in report_files],
            "frameworks": sorted(frameworks_used),
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


def convert_to_summary(
    report_path: Path,
    output_path: Optional[Path] = None,
    framework: str = "pytest",
) -> Dict[str, Any]:
    """
    Convert a test report to a human-readable summary format.

    This provides a consistent interface for generating summaries across
    all supported frameworks.

    Args:
        report_path: Path to test report file
        output_path: Optional path to save summary JSON
        framework: Test framework name

    Returns:
        Summary dict with results grouped by outcome
    """
    # Use framework-specific summary converters when available
    if framework == "pytest":
        return convert_pytest_report_to_summary(report_path, output_path)

    # For other frameworks, parse and build summary
    parsed = parse_test_report(report_path, framework)

    # Group results
    failed_list = []
    error_list = []
    skipped_groups: Dict[str, List[str]] = {}
    passed_list = []

    for test in parsed.get("tests", []):
        nodeid = test.get("nodeid", "")
        outcome = test.get("outcome", "")

        if outcome == "passed":
            passed_list.append(nodeid)
        elif outcome == "failed":
            failed_list.append({"nodeid": nodeid, "message": "Test failed"})
        elif outcome == "error":
            error_list.append({"nodeid": nodeid, "message": "Test error"})
        elif outcome == "skipped":
            reason = "Skipped"
            if reason not in skipped_groups:
                skipped_groups[reason] = []
            skipped_groups[reason].append(nodeid)

    skipped_by_reason = [
        {"reason": reason, "count": len(tests), "tests": tests} for reason, tests in skipped_groups.items()
    ]

    summary_result = {
        "duration": parsed.get("duration", 0),
        "summary": parsed.get("summary", {}),
        "results": {
            "failed": failed_list,
            "error": error_list,
            "skipped": skipped_by_reason,
            "passed": passed_list,
        },
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(summary_result, f, indent=2)

    return summary_result
