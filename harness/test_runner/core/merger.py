"""
Result merger for combining multiple test attempt results.

Adopts the merge strategy from harness/filter_tests/run_milestone_tests.py:
- Pass-any logic: passed > skipped > failed
- Flaky test detection and exclusion from stable results
- Detailed flaky test tracking
"""

from typing import List, Dict, Any, Optional, Set
from pathlib import Path
import json
import logging

logger = logging.getLogger(__name__)


def merge_outcome(outcomes: List[str]) -> str:
    """
    Merge outcomes from multiple attempts using pass-any logic.

    Priority: passed > skipped > failed/error
    - If any attempt has 'passed' → 'passed'
    - If no pass but any 'skipped' → 'skipped'
    - Otherwise → 'failed'

    Args:
        outcomes: List of outcomes from multiple attempts

    Returns:
        Merged outcome string
    """
    if not outcomes:
        return "unknown"
    if "passed" in outcomes:
        return "passed"
    if "skipped" in outcomes:
        return "skipped"
    # failed, error, or other outcomes
    return "failed"


def is_flaky(outcomes: List[str]) -> bool:
    """
    Check if a test is flaky based on its outcomes across multiple attempts.

    A test is considered flaky if its outcomes are not consistent across attempts.

    Args:
        outcomes: List of outcomes from multiple attempts

    Returns:
        True if the test is flaky (inconsistent outcomes), False otherwise
    """
    if len(outcomes) <= 1:
        return False
    # Normalize outcomes: treat 'error' as 'failed' for flaky detection
    normalized = []
    for o in outcomes:
        if o in ["failed", "error"]:
            normalized.append("failed")
        else:
            normalized.append(o)
    return len(set(normalized)) > 1


class ResultMerger:
    """
    Merges multiple test attempt results into a single result.

    Uses pass-any merge strategy:
    - If any attempt has 'passed' → final outcome is 'passed'
    - If no pass but any 'skipped' → final outcome is 'skipped'
    - Otherwise → final outcome is 'failed'

    Also detects flaky tests (tests with inconsistent outcomes across attempts).
    """

    def __init__(self):
        """Initialize ResultMerger."""
        pass

    def merge(self, attempt_files: List[Path]) -> Dict[str, Any]:
        """
        Merge multiple attempt result files.

        Args:
            attempt_files: List of paths to attempt result JSON files

        Returns:
            Merged result dict with:
            - tests: List of test results with final outcome
            - summary: Test summary statistics
            - merge_info: Information about the merge process
            - flaky_tests: List of flaky test IDs
        """
        if not attempt_files:
            return {"tests": [], "summary": {}, "merge_info": {"attempts": 0}}

        # Load all attempt results
        all_results = []
        for f in attempt_files:
            if f.exists():
                try:
                    with open(f) as fp:
                        all_results.append(json.load(fp))
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse {f}: {e}")

        if not all_results:
            return {"tests": [], "summary": {}, "merge_info": {"attempts": 0}}

        # Collect outcomes for each test across attempts
        test_outcomes: Dict[str, List[str]] = {}
        test_metadata: Dict[str, Dict] = {}  # Store additional test metadata

        for result in all_results:
            for test in result.get("tests", []):
                test_id = test.get("nodeid")
                if not test_id:
                    continue

                if test_id not in test_outcomes:
                    test_outcomes[test_id] = []
                    test_metadata[test_id] = {
                        "nodeid": test_id,
                        "lineno": test.get("lineno"),
                        "keywords": test.get("keywords", []),
                    }

                test_outcomes[test_id].append(test.get("outcome", "unknown"))

        # Determine final outcome for each test using pass-any logic
        final_tests = []
        flaky_tests = []
        flaky_test_ids: Set[str] = set()

        for test_id, outcomes in test_outcomes.items():
            final_outcome = merge_outcome(outcomes)
            test_is_flaky = is_flaky(outcomes)

            test_result = {
                **test_metadata[test_id],
                "outcome": final_outcome,
                "attempts": outcomes,
                "is_flaky": test_is_flaky,
            }

            final_tests.append(test_result)

            if test_is_flaky:
                flaky_tests.append(
                    {
                        "test_id": test_id,
                        "outcomes": outcomes,
                        "merged_outcome": final_outcome,
                    }
                )
                flaky_test_ids.add(test_id)

        # Calculate summary
        summary = self._calculate_summary(final_tests)

        # Calculate stable summary (excluding flaky tests)
        stable_tests = [t for t in final_tests if not t.get("is_flaky")]
        stable_summary = self._calculate_summary(stable_tests)

        return {
            "tests": final_tests,
            "summary": summary,
            "stable_summary": stable_summary,
            "merge_info": {
                "attempts": len(all_results),
                "strategy": "pass_any",
                "flaky_count": len(flaky_tests),
                "flaky_tests": flaky_tests[:20],  # Limit to first 20
            },
            "flaky_test_ids": list(flaky_test_ids),
        }

    def _calculate_summary(self, tests: List[Dict]) -> Dict[str, Any]:
        """
        Calculate test summary statistics.

        Args:
            tests: List of test results

        Returns:
            Summary dict with counts
        """
        total = len(tests)
        passed = sum(1 for t in tests if t.get("outcome") == "passed")
        failed = sum(1 for t in tests if t.get("outcome") == "failed")
        skipped = sum(1 for t in tests if t.get("outcome") == "skipped")
        error = sum(1 for t in tests if t.get("outcome") == "error")
        flaky = sum(1 for t in tests if t.get("is_flaky"))

        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "error": error,
            "flaky": flaky,
            "pass_rate": round(passed / total * 100, 2) if total > 0 else 0,
        }

    def merge_from_directory(self, directory: Path, pattern: str = "*_attempt*.json") -> Dict[str, Dict[str, Any]]:
        """
        Merge all attempt files in a directory grouped by execution+mode.

        Args:
            directory: Directory containing attempt files
            pattern: Glob pattern for attempt files

        Returns:
            Dict mapping output_key to merged results
        """
        import re

        attempt_files = list(directory.glob(pattern))

        # Group files by execution+mode (remove _attemptN suffix)
        groups: Dict[str, List[Path]] = {}
        attempt_pattern = re.compile(r"(.+)_attempt\d+\.json$")

        for f in attempt_files:
            match = attempt_pattern.match(f.name)
            if match:
                key = match.group(1)
                if key not in groups:
                    groups[key] = []
                groups[key].append(f)

        # Sort attempt files and merge each group
        results = {}
        for key, files in groups.items():
            sorted_files = sorted(files, key=lambda x: x.name)
            results[key] = self.merge(sorted_files)

        return results
