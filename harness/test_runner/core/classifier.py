"""
Test result classifier for comparing results between states.

Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha)
by using a standardized report format.
"""

from typing import Dict, List, Any, Set, Optional, Tuple
from pathlib import Path
import json
import logging

from harness.utils.test_id_normalizer import TestIdNormalizer

logger = logging.getLogger(__name__)

# Framework-specific outcome mappings to normalized outcomes
OUTCOME_MAPPINGS = {
    "pytest": {
        "passed": "pass",
        "failed": "fail",
        "skipped": "skipped",
        "error": "fail",
        "xfailed": "pass",  # Expected failure = success
        "xpassed": "fail",  # Unexpected pass = failure
    },
    "go_test": {
        "pass": "pass",
        "fail": "fail",
        "skip": "skipped",
        "passed": "pass",
        "failed": "fail",
        "skipped": "skipped",
    },
    "maven": {
        "passed": "pass",
        "failed": "fail",
        "skipped": "skipped",
        "error": "fail",
    },
    "cargo": {
        "ok": "pass",
        "passed": "pass",
        "FAILED": "fail",
        "failed": "fail",
        "ignored": "skipped",
        "skipped": "skipped",
    },
    "jest": {
        "passed": "pass",
        "failed": "fail",
        "pending": "skipped",
        "skipped": "skipped",
    },
    "mocha": {
        "passed": "pass",
        "failed": "fail",
        "pending": "skipped",
        "skipped": "skipped",
    },
}


class TestClassifier:
    """
    Classifies test result changes between two states.

    Supports multiple test frameworks (pytest, go_test, maven, cargo, jest, mocha)
    by normalizing framework-specific outcomes to standard outcomes.

    Categories:
    - pass_to_pass: Tests that consistently pass
    - pass_to_fail: Tests that passed before but fail now
    - pass_to_skipped: Tests that passed before but are skipped now
    - fail_to_pass: Tests that failed before but pass now
    - fail_to_fail: Tests that consistently fail
    - fail_to_skipped: Tests that failed before but are skipped now
    - skipped_to_pass: Tests that were skipped before but pass now
    - skipped_to_fail: Tests that were skipped before but fail now
    - skipped_to_skipped: Tests that are skipped in both states
    - new_tests: Tests that only exist in the after state
    - removed_tests: Tests that only exist in the before state
    """

    def __init__(self, framework: str = "pytest", enable_test_id_normalization: bool = True):
        """
        Initialize classifier with framework-specific outcome mappings.

        Args:
            framework: Test framework name (pytest, go_test, maven, cargo, jest, mocha)
            enable_test_id_normalization: Whether to normalize test IDs before comparison.
                This helps handle parameterized/fuzz tests with random subtest IDs.
                Default is True for go_test framework.
        """
        self.framework = framework
        self.outcome_map = OUTCOME_MAPPINGS.get(framework, OUTCOME_MAPPINGS["pytest"])
        self.enable_test_id_normalization = enable_test_id_normalization
        self.normalizer = TestIdNormalizer(framework=framework, enable_normalization=enable_test_id_normalization)

    def classify(self, before_results: Dict[str, Any], after_results: Dict[str, Any]) -> Dict[str, List[str]]:
        """
        Classify test changes between two states.

        Args:
            before_results: Test results from the before state
            after_results: Test results from the after state

        Returns:
            Dict with classification categories as keys and test IDs as values
        """
        # Build outcome maps (original test ID -> outcome)
        before_outcomes = self._build_outcome_map(before_results)
        after_outcomes = self._build_outcome_map(after_results)

        # Build normalized outcome maps for matching
        # normalized_id -> (original_id, outcome)
        before_normalized, before_norm_to_orig = self._build_normalized_outcome_map(before_outcomes)
        after_normalized, after_norm_to_orig = self._build_normalized_outcome_map(after_outcomes)

        # Get all normalized test IDs for comparison
        all_normalized_tests = set(before_normalized.keys()) | set(after_normalized.keys())

        # Classify each test
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

        for norm_test_id in all_normalized_tests:
            before_outcome = before_normalized.get(norm_test_id)
            after_outcome = after_normalized.get(norm_test_id)

            # Get original test IDs for reporting
            # Prefer after_id if available (it's the "current" test), otherwise use before_id
            if norm_test_id in after_norm_to_orig:
                report_test_id = after_norm_to_orig[norm_test_id]
            else:
                report_test_id = before_norm_to_orig.get(norm_test_id, norm_test_id)

            category = self._categorize(before_outcome, after_outcome)
            classification[category].append(report_test_id)

            # Also populate aggregated lists
            if before_outcome is None:
                classification["new_tests"].append({"test_id": report_test_id, "end_outcome": after_outcome})
            elif after_outcome is None:
                classification["removed_tests"].append({"test_id": report_test_id, "start_outcome": before_outcome})

        # Sort lists for consistent output
        for category in classification:
            if category in ("new_tests", "removed_tests"):
                # These are lists of dicts, sort by test_id
                classification[category].sort(key=lambda x: x["test_id"])
            else:
                # These are lists of strings (test IDs)
                classification[category].sort()

        return classification

    def _build_normalized_outcome_map(self, outcome_map: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
        """
        Build a normalized outcome map for test ID matching.

        When multiple tests normalize to the same ID (e.g., parameterized tests),
        we keep the outcome if all instances have the same outcome, otherwise
        we use 'fail' if any instance failed.

        Args:
            outcome_map: Original test ID -> outcome mapping

        Returns:
            Tuple of (normalized_id -> outcome, normalized_id -> original_id)
        """
        normalized_outcomes: Dict[str, str] = {}
        normalized_to_original: Dict[str, str] = {}
        normalized_all_outcomes: Dict[str, List[str]] = {}

        for test_id, outcome in outcome_map.items():
            norm_id = self.normalizer.normalize(test_id)

            if norm_id not in normalized_all_outcomes:
                normalized_all_outcomes[norm_id] = []
                normalized_to_original[norm_id] = test_id  # Keep first original ID

            normalized_all_outcomes[norm_id].append(outcome)

        # Determine final outcome for each normalized ID
        for norm_id, outcomes in normalized_all_outcomes.items():
            # Normalize all outcomes first
            norm_outcomes = [self._normalize_outcome(o) for o in outcomes]

            # If any test failed, the group fails
            if "fail" in norm_outcomes:
                normalized_outcomes[norm_id] = "fail"
            # If all passed, group passes
            elif all(o == "pass" for o in norm_outcomes):
                normalized_outcomes[norm_id] = "pass"
            # If all skipped, group is skipped
            elif all(o == "skipped" for o in norm_outcomes):
                normalized_outcomes[norm_id] = "skipped"
            # Mixed pass/skipped - consider as pass (skipped tests don't invalidate passing ones)
            else:
                normalized_outcomes[norm_id] = "pass"

        return normalized_outcomes, normalized_to_original

    def _build_outcome_map(self, results: Dict[str, Any]) -> Dict[str, str]:
        """
        Build a map of test ID to outcome.

        Args:
            results: Test results dict

        Returns:
            Dict mapping test ID to outcome
        """
        outcome_map = {}
        for test in results.get("tests", []):
            test_id = test.get("nodeid")
            if test_id:
                outcome_map[test_id] = test.get("outcome", "unknown")
        return outcome_map

    def _categorize(self, before_outcome: Optional[str], after_outcome: Optional[str]) -> str:
        """
        Categorize a single test based on before/after outcomes.

        Args:
            before_outcome: Outcome in before state (None if test didn't exist)
            after_outcome: Outcome in after state (None if test doesn't exist)

        Returns:
            Category string (e.g., "pass_to_fail", "none_to_pass", "fail_to_none")
        """
        if before_outcome is None:
            # New test: none_to_pass, none_to_fail, or none_to_skipped
            after_state = self._normalize_outcome(after_outcome)
            return f"none_to_{after_state}"

        if after_outcome is None:
            # Removed test: pass_to_none, fail_to_none, or skipped_to_none
            before_state = self._normalize_outcome(before_outcome)
            return f"{before_state}_to_none"

        # Normalize outcomes to: pass, fail, skipped
        before_state = self._normalize_outcome(before_outcome)
        after_state = self._normalize_outcome(after_outcome)

        return f"{before_state}_to_{after_state}"

    def _normalize_outcome(self, outcome: str) -> str:
        """
        Normalize outcome to one of: pass, fail, skipped.

        Uses framework-specific outcome mappings to handle different
        test frameworks (pytest, go_test, maven, cargo, jest, mocha).

        Args:
            outcome: Raw test outcome string

        Returns:
            Normalized state: "pass", "fail", or "skipped"
        """
        # Use framework-specific mapping if available
        normalized = self.outcome_map.get(outcome)
        if normalized:
            return normalized

        # Fallback logic for unknown outcomes
        if outcome in ("skipped", "skip", "ignored", "pending"):
            return "skipped"
        elif outcome in ("passed", "pass", "ok", "xfail"):
            return "pass"
        else:
            # failed, error, xpass, FAILED, etc. count as fail
            return "fail"

    def _is_pass(self, outcome: str) -> bool:
        """
        Check if an outcome is considered a pass.

        Args:
            outcome: Test outcome string

        Returns:
            True if outcome is a pass
        """
        return outcome in ("passed", "xfail")  # xfail is expected failure, counts as pass

    def classify_from_files(self, before_file: Path, after_file: Path) -> Dict[str, List[str]]:
        """
        Classify test changes from result files.

        Supports multiple test frameworks by using the unified report parser.
        Also handles pre-parsed JSON files in standardized format.

        Args:
            before_file: Path to before state results JSON
            after_file: Path to after state results JSON

        Returns:
            Classification dict
        """
        # Import here to avoid circular dependency
        from .report_parser import parse_test_report

        # Try to load as standardized JSON format first
        # (merged reports already have "tests" array with "nodeid" and "outcome")
        before_results = self._load_or_parse(before_file)
        after_results = self._load_or_parse(after_file)

        return self.classify(before_results, after_results)

    def _load_or_parse(self, file_path: Path) -> Dict[str, Any]:
        """
        Load a file as standardized JSON, or parse it using framework-specific parser.

        If the file is already in standardized format (has "tests" array with
        "nodeid" and "outcome"), use it directly. Otherwise, use the framework
        parser to convert from raw format.

        Args:
            file_path: Path to test results file

        Returns:
            Standardized test results dict
        """
        from .report_parser import parse_test_report

        if not file_path.exists():
            return {"tests": [], "summary": {}}

        # Try loading as JSON first
        if file_path.suffix == ".json":
            try:
                with open(file_path) as f:
                    data = json.load(f)
                # Check if it's already in standardized format
                if "tests" in data and isinstance(data["tests"], list):
                    # Verify it has the expected structure
                    if not data["tests"] or ("nodeid" in data["tests"][0] and "outcome" in data["tests"][0]):
                        return data
            except (json.JSONDecodeError, KeyError, IndexError):
                pass

        # Fall back to framework-specific parsing
        return parse_test_report(file_path, self.framework)

    def generate_summary(self, classification: Dict[str, List[str]]) -> Dict[str, Any]:
        """
        Generate a summary of the classification.

        Args:
            classification: Classification dict from classify()

        Returns:
            Summary dict with counts (flat structure, no nested 'counts' key)
        """
        # Flat counts for each category
        summary = {k: len(v) for k, v in classification.items()}

        # Calculate totals
        # Before state: all tests that existed before (excludes new tests: none_to_*)
        summary["total_before"] = (
            summary.get("pass_to_pass", 0)
            + summary.get("pass_to_fail", 0)
            + summary.get("pass_to_skipped", 0)
            + summary.get("pass_to_none", 0)
            + summary.get("fail_to_pass", 0)
            + summary.get("fail_to_fail", 0)
            + summary.get("fail_to_skipped", 0)
            + summary.get("fail_to_none", 0)
            + summary.get("skipped_to_pass", 0)
            + summary.get("skipped_to_fail", 0)
            + summary.get("skipped_to_skipped", 0)
            + summary.get("skipped_to_none", 0)
        )

        # After state: all tests that exist after (excludes removed tests: *_to_none)
        summary["total_after"] = (
            summary.get("pass_to_pass", 0)
            + summary.get("pass_to_fail", 0)
            + summary.get("pass_to_skipped", 0)
            + summary.get("fail_to_pass", 0)
            + summary.get("fail_to_fail", 0)
            + summary.get("fail_to_skipped", 0)
            + summary.get("skipped_to_pass", 0)
            + summary.get("skipped_to_fail", 0)
            + summary.get("skipped_to_skipped", 0)
            + summary.get("none_to_pass", 0)
            + summary.get("none_to_fail", 0)
            + summary.get("none_to_skipped", 0)
        )

        return summary

    def save_classification(
        self, classification: Dict[str, List[str]], output_path: Path, include_summary: bool = True
    ):
        """
        Save classification to a JSON file.

        Args:
            classification: Classification dict
            output_path: Output file path
            include_summary: Whether to include summary in output
        """
        output = dict(classification)

        if include_summary:
            output["summary"] = self.generate_summary(classification)

        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
