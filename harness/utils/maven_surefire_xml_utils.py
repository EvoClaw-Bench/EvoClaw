"""
Maven Surefire XML Report Parsing Utilities

This module provides utilities for parsing Maven Surefire XML test reports
and converting them to a standardized format with METHOD-LEVEL granularity.

Key Features:
- Method-level test results (not just class-level)
- Module path prefix in test IDs to avoid naming collisions
- Detailed failure/error messages and stack traces
- Support for both individual XML files and archived reports

XML Report Structure (TEST-*.xml):
    <?xml version="1.0" encoding="UTF-8"?>
    <testsuite name="org.example.TestClass" tests="5" errors="1" failures="1" skipped="1" time="10.5">
        <testcase name="testMethod1" classname="org.example.TestClass" time="0.5"/>
        <testcase name="testMethod2" classname="org.example.TestClass" time="1.0">
            <failure type="AssertionError" message="expected true">stack trace...</failure>
        </testcase>
        <testcase name="testMethod3" classname="org.example.TestClass" time="0.1">
            <skipped message="Disabled"/>
        </testcase>
    </testsuite>

Output Format:
    {
        "tests": [
            {
                "nodeid": "module/path::org.example.TestClass::testMethod1",
                "outcome": "passed",
                "duration": 0.5,
                "module": "module/path",
                "class_name": "org.example.TestClass",
                "method_name": "testMethod1"
            },
            ...
        ],
        "summary": {"passed": int, "failed": int, "error": int, "skipped": int, "total": int},
        "duration": float,
        "modules": ["module1", "module2", ...]
    }

Usage:
    from harness.utils.maven_surefire_xml_utils import (
        parse_surefire_xml_file,
        parse_surefire_reports_dir,
        collect_all_surefire_reports,
        parse_surefire_archive,
    )

    # Parse a single XML file
    tests = parse_surefire_xml_file(Path("TEST-org.example.TestClass.xml"), module_name="my-module")

    # Parse all XML files in a surefire-reports directory
    result = parse_surefire_reports_dir(Path("target/surefire-reports"), module_name="my-module")

    # Collect from entire project (all modules)
    result = collect_all_surefire_reports(Path("/testbed"))

    # Parse from archived reports
    result = parse_surefire_archive(Path("surefire_reports.tar.gz"))
"""

import json
import logging
import os
import re
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TestMethodResult:
    """Result for a single test method."""

    nodeid: str
    outcome: str  # passed, failed, error, skipped
    duration: float = 0.0
    module: str = ""
    class_name: str = ""
    method_name: str = ""
    failure_message: str = ""
    failure_type: str = ""
    stack_trace: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "nodeid": self.nodeid,
            "outcome": self.outcome,
            "duration": self.duration,
        }
        # Include optional fields if they have values
        if self.module:
            result["module"] = self.module
        if self.class_name:
            result["class_name"] = self.class_name
        if self.method_name:
            result["method_name"] = self.method_name
        if self.failure_message:
            result["failure_message"] = self.failure_message
        if self.failure_type:
            result["failure_type"] = self.failure_type
        if self.stack_trace:
            result["stack_trace"] = self.stack_trace[:1000]  # Truncate long traces
        return result


@dataclass
class SurefireXmlSummary:
    """Aggregated summary of Surefire XML reports."""

    tests: List[TestMethodResult] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    error: int = 0
    skipped: int = 0
    total: int = 0
    duration: float = 0.0
    modules: Set[str] = field(default_factory=set)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to standardized format for the test runner."""
        return {
            "tests": [t.to_dict() for t in self.tests],
            "collectors": [],  # Not applicable for Surefire
            "summary": {
                "passed": self.passed,
                "failed": self.failed,
                "error": self.error,
                "skipped": self.skipped,
                "total": self.total,
            },
            "duration": round(self.duration, 2),
            "modules": sorted(self.modules),
            "_framework": "maven",
            "_parse_mode": "surefire_xml",
        }


def parse_surefire_xml_file(
    xml_path: Path,
    module_name: str = "",
) -> List[TestMethodResult]:
    """
    Parse a single Surefire XML report file.

    Args:
        xml_path: Path to the TEST-*.xml file
        module_name: Module name to prefix in nodeid (e.g., "dubbo-config/dubbo-config-api")

    Returns:
        List of TestMethodResult objects
    """
    if not xml_path.exists():
        logger.warning(f"Surefire XML file not found: {xml_path}")
        return []

    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
    except ET.ParseError as e:
        logger.warning(f"Failed to parse XML file {xml_path}: {e}")
        return []

    tests = []

    # Handle both <testsuite> (single) and <testsuites> (aggregated) formats
    if root.tag == "testsuites":
        testsuites = root.findall("testsuite")
    elif root.tag == "testsuite":
        testsuites = [root]
    else:
        logger.warning(f"Unknown root element in {xml_path}: {root.tag}")
        return []

    for testsuite in testsuites:
        suite_name = testsuite.get("name", "")
        suite_time = float(testsuite.get("time", 0) or 0)

        for testcase in testsuite.findall("testcase"):
            method_name = testcase.get("name", "")
            class_name = testcase.get("classname", suite_name)
            duration = float(testcase.get("time", 0) or 0)

            # Determine outcome
            failure_elem = testcase.find("failure")
            error_elem = testcase.find("error")
            skipped_elem = testcase.find("skipped")

            failure_message = ""
            failure_type = ""
            stack_trace = ""

            if failure_elem is not None:
                outcome = "failed"
                failure_message = failure_elem.get("message", "")
                failure_type = failure_elem.get("type", "")
                stack_trace = failure_elem.text or ""
            elif error_elem is not None:
                outcome = "error"
                failure_message = error_elem.get("message", "")
                failure_type = error_elem.get("type", "")
                stack_trace = error_elem.text or ""
            elif skipped_elem is not None:
                outcome = "skipped"
                failure_message = skipped_elem.get("message", "")
            else:
                outcome = "passed"

            # Build nodeid with module prefix to avoid collisions
            # Format: module::class_name::method_name
            if module_name:
                nodeid = f"{module_name}::{class_name}::{method_name}"
            else:
                nodeid = f"{class_name}::{method_name}"

            tests.append(
                TestMethodResult(
                    nodeid=nodeid,
                    outcome=outcome,
                    duration=duration,
                    module=module_name,
                    class_name=class_name,
                    method_name=method_name,
                    failure_message=failure_message,
                    failure_type=failure_type,
                    stack_trace=stack_trace,
                )
            )

    return tests


def parse_surefire_reports_dir(
    reports_dir: Path,
    module_name: str = "",
) -> SurefireXmlSummary:
    """
    Parse all Surefire XML reports in a directory.

    Args:
        reports_dir: Path to the surefire-reports directory
        module_name: Module name to prefix in nodeids

    Returns:
        SurefireXmlSummary with all test results
    """
    summary = SurefireXmlSummary()

    if not reports_dir.exists() or not reports_dir.is_dir():
        logger.warning(f"Surefire reports directory not found: {reports_dir}")
        return summary

    # Find all TEST-*.xml files
    xml_files = list(reports_dir.glob("TEST-*.xml"))
    if not xml_files:
        logger.warning(f"No TEST-*.xml files found in {reports_dir}")
        return summary

    for xml_file in xml_files:
        tests = parse_surefire_xml_file(xml_file, module_name)
        summary.tests.extend(tests)

    # Calculate statistics
    for test in summary.tests:
        if test.outcome == "passed":
            summary.passed += 1
        elif test.outcome == "failed":
            summary.failed += 1
        elif test.outcome == "error":
            summary.error += 1
        elif test.outcome == "skipped":
            summary.skipped += 1
        summary.duration += test.duration
        if test.module:
            summary.modules.add(test.module)

    summary.total = len(summary.tests)

    return summary


def collect_all_surefire_reports(
    testbed_root: Path,
    exclude_patterns: Optional[List[str]] = None,
) -> SurefireXmlSummary:
    """
    Collect and parse all Surefire reports from a multi-module Maven project.

    This function walks through the project directory, finds all
    target/surefire-reports directories, and parses their XML reports.
    Module names are derived from the relative path.

    Args:
        testbed_root: Root directory of the Maven project
        exclude_patterns: List of patterns to exclude (e.g., ["**/IT*.xml"])

    Returns:
        SurefireXmlSummary with all test results from all modules
    """
    summary = SurefireXmlSummary()
    exclude_patterns = exclude_patterns or []

    if not testbed_root.exists():
        logger.warning(f"Testbed root not found: {testbed_root}")
        return summary

    # Find all surefire-reports directories
    surefire_dirs = list(testbed_root.glob("**/target/surefire-reports"))

    for surefire_dir in surefire_dirs:
        # Extract module name from path
        # e.g., /testbed/dubbo-config/dubbo-config-api/target/surefire-reports
        # -> dubbo-config/dubbo-config-api
        relative_path = surefire_dir.relative_to(testbed_root)
        module_parts = relative_path.parts[:-2]  # Remove "target/surefire-reports"
        module_name = "/".join(module_parts) if module_parts else ""

        # Parse this module's reports
        module_summary = parse_surefire_reports_dir(surefire_dir, module_name)

        # Merge into overall summary
        summary.tests.extend(module_summary.tests)
        summary.passed += module_summary.passed
        summary.failed += module_summary.failed
        summary.error += module_summary.error
        summary.skipped += module_summary.skipped
        summary.duration += module_summary.duration
        summary.modules.update(module_summary.modules)

    summary.total = len(summary.tests)

    logger.info(
        f"Collected {summary.total} tests from {len(summary.modules)} modules: "
        f"passed={summary.passed}, failed={summary.failed}, error={summary.error}, skipped={summary.skipped}"
    )

    return summary


def parse_surefire_archive(
    archive_path: Path,
    extract_dir: Optional[Path] = None,
) -> SurefireXmlSummary:
    """
    Parse Surefire reports from a tar.gz archive.

    The archive should contain a directory structure like:
        surefire_reports/
        ├── module1/
        │   ├── TEST-org.example.Test1.xml
        │   └── TEST-org.example.Test2.xml
        └── module2/
            └── TEST-org.example.Test3.xml

    Args:
        archive_path: Path to the .tar.gz archive
        extract_dir: Optional directory to extract to (uses temp dir if not specified)

    Returns:
        SurefireXmlSummary with all test results
    """
    summary = SurefireXmlSummary()

    if not archive_path.exists():
        logger.warning(f"Surefire archive not found: {archive_path}")
        return summary

    # Create temp directory if not specified
    cleanup_temp = False
    if extract_dir is None:
        extract_dir = Path(tempfile.mkdtemp(prefix="surefire_"))
        cleanup_temp = True

    try:
        # Extract archive
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(extract_dir)

        # Find the extracted reports directory
        # Try common structures
        reports_root = None
        for candidate in [
            extract_dir / "surefire_reports",
            extract_dir / "surefire-reports",
            extract_dir,
        ]:
            if candidate.exists() and candidate.is_dir():
                # Check if it has XML files or subdirectories with XML files
                if list(candidate.glob("TEST-*.xml")) or list(candidate.glob("*/TEST-*.xml")):
                    reports_root = candidate
                    break

        if reports_root is None:
            logger.warning(f"No surefire reports found in archive {archive_path}")
            return summary

        # Parse each module directory
        # First check if XML files are directly in reports_root (single module)
        direct_xmls = list(reports_root.glob("TEST-*.xml"))
        if direct_xmls and not list(reports_root.glob("*/TEST-*.xml")):
            # Single module case
            summary = parse_surefire_reports_dir(reports_root, "")
        else:
            # Multi-module case: each subdirectory is a module
            for module_dir in sorted(reports_root.iterdir()):
                if module_dir.is_dir():
                    # Module name from directory structure
                    # Handle nested module paths like "dubbo-config/dubbo-config-api"
                    module_name = module_dir.name

                    # Check for nested structure
                    nested_xmls = list(module_dir.rglob("TEST-*.xml"))
                    if nested_xmls:
                        # Find the deepest directory containing XML files
                        xml_dirs = set(xml_file.parent for xml_file in nested_xmls)
                        for xml_dir in xml_dirs:
                            rel_path = xml_dir.relative_to(reports_root)
                            full_module_name = str(rel_path).replace(os.sep, "/")
                            module_summary = parse_surefire_reports_dir(xml_dir, full_module_name)
                            summary.tests.extend(module_summary.tests)
                            summary.passed += module_summary.passed
                            summary.failed += module_summary.failed
                            summary.error += module_summary.error
                            summary.skipped += module_summary.skipped
                            summary.duration += module_summary.duration
                            summary.modules.update(module_summary.modules)
                    else:
                        # Direct XML files in module directory
                        module_summary = parse_surefire_reports_dir(module_dir, module_name)
                        summary.tests.extend(module_summary.tests)
                        summary.passed += module_summary.passed
                        summary.failed += module_summary.failed
                        summary.error += module_summary.error
                        summary.skipped += module_summary.skipped
                        summary.duration += module_summary.duration
                        summary.modules.update(module_summary.modules)

        summary.total = len(summary.tests)

    except tarfile.TarError as e:
        logger.error(f"Failed to extract archive {archive_path}: {e}")
    finally:
        # Cleanup temp directory
        if cleanup_temp and extract_dir.exists():
            import shutil

            shutil.rmtree(extract_dir, ignore_errors=True)

    return summary


def convert_surefire_to_standard_format(
    summary: SurefireXmlSummary,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Convert SurefireXmlSummary to standardized format and optionally save to file.

    Args:
        summary: Parsed Surefire summary
        output_path: Optional path to save JSON output

    Returns:
        Standardized test report dictionary
    """
    result = summary.to_dict()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2)

    return result


def get_tests_by_module(summary: SurefireXmlSummary) -> Dict[str, List[TestMethodResult]]:
    """
    Group tests by module name.

    Args:
        summary: Parsed Surefire summary

    Returns:
        Dictionary mapping module names to lists of test results
    """
    by_module: Dict[str, List[TestMethodResult]] = {}
    for test in summary.tests:
        module = test.module or "(root)"
        if module not in by_module:
            by_module[module] = []
        by_module[module].append(test)
    return by_module


def get_failed_tests(summary: SurefireXmlSummary) -> List[TestMethodResult]:
    """Get list of failed tests (including errors)."""
    return [t for t in summary.tests if t.outcome in ("failed", "error")]


def get_flaky_candidates(
    summary1: SurefireXmlSummary,
    summary2: SurefireXmlSummary,
) -> List[str]:
    """
    Find tests with different outcomes between two runs.

    This helps identify potentially flaky tests.

    Args:
        summary1: First test run summary
        summary2: Second test run summary

    Returns:
        List of nodeids with different outcomes
    """
    outcomes1 = {t.nodeid: t.outcome for t in summary1.tests}
    outcomes2 = {t.nodeid: t.outcome for t in summary2.tests}

    flaky = []
    all_tests = set(outcomes1.keys()) | set(outcomes2.keys())

    for nodeid in all_tests:
        o1 = outcomes1.get(nodeid)
        o2 = outcomes2.get(nodeid)
        if o1 and o2 and o1 != o2:
            flaky.append(nodeid)

    return flaky


def print_summary(summary: SurefireXmlSummary) -> None:
    """Print a human-readable summary of test results."""
    print(f"\n{'=' * 70}")
    print("Maven Surefire XML Test Summary (Method-Level)")
    print(f"{'=' * 70}")
    print(f"Total Tests:  {summary.total}")
    print(f"Passed:       {summary.passed}")
    print(f"Failed:       {summary.failed}")
    print(f"Errors:       {summary.error}")
    print(f"Skipped:      {summary.skipped}")
    print(f"Duration:     {summary.duration:.2f}s")
    print(f"Modules:      {len(summary.modules)}")

    if summary.modules:
        print(f"\n{'─' * 70}")
        print("Modules:")
        for module in sorted(summary.modules):
            module_tests = [t for t in summary.tests if t.module == module]
            passed = sum(1 for t in module_tests if t.outcome == "passed")
            failed = sum(1 for t in module_tests if t.outcome in ("failed", "error"))
            print(f"  {module}: {len(module_tests)} tests ({passed} passed, {failed} failed)")

    failed_tests = get_failed_tests(summary)
    if failed_tests:
        print(f"\n{'─' * 70}")
        print(f"Failed Tests ({len(failed_tests)}):")
        for t in failed_tests[:20]:  # Limit to first 20
            print(f"  {'[ERROR]' if t.outcome == 'error' else '[FAIL]'} {t.nodeid}")
            if t.failure_message:
                print(f"         {t.failure_message[:100]}")
        if len(failed_tests) > 20:
            print(f"  ... and {len(failed_tests) - 20} more")

    print(f"{'=' * 70}\n")


# CLI support
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python maven_surefire_xml_utils.py <path> [output.json]")
        print("  <path> can be:")
        print("    - A single TEST-*.xml file")
        print("    - A surefire-reports directory")
        print("    - A project root directory (will find all surefire-reports)")
        print("    - A .tar.gz archive of surefire reports")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    if not input_path.exists():
        print(f"Error: Path not found: {input_path}")
        sys.exit(1)

    # Determine input type and parse accordingly
    if input_path.suffix == ".gz" or input_path.name.endswith(".tar.gz"):
        print(f"Parsing archive: {input_path}")
        summary = parse_surefire_archive(input_path)
    elif input_path.suffix == ".xml":
        print(f"Parsing single XML file: {input_path}")
        tests = parse_surefire_xml_file(input_path)
        summary = SurefireXmlSummary(tests=tests, total=len(tests))
        for t in tests:
            if t.outcome == "passed":
                summary.passed += 1
            elif t.outcome == "failed":
                summary.failed += 1
            elif t.outcome == "error":
                summary.error += 1
            elif t.outcome == "skipped":
                summary.skipped += 1
    elif input_path.is_dir():
        if input_path.name == "surefire-reports":
            print(f"Parsing surefire-reports directory: {input_path}")
            summary = parse_surefire_reports_dir(input_path)
        else:
            print(f"Collecting from project root: {input_path}")
            summary = collect_all_surefire_reports(input_path)
    else:
        print(f"Error: Unknown input type: {input_path}")
        sys.exit(1)

    # Print summary
    print_summary(summary)

    # Save to file if specified
    if output_file:
        convert_surefire_to_standard_format(summary, output_file)
        print(f"Saved to: {output_file}")

    # Exit with non-zero if there are failures
    if summary.failed > 0 or summary.error > 0:
        sys.exit(1)
