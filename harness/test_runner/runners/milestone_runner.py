"""
Milestone-level test runner.

This module provides programmatic access to milestone test running functionality.
For CLI usage, see run_milestone_tests.py.
"""

from typing import Optional, List, Dict, Any
from pathlib import Path

# Import core functions from the main runner module
from ..run_milestone_tests import (
    run_single_milestone,
    run_single_attempt,
    merge_attempt_results,
    merge_pytest_json_reports,
    discover_milestones,
    load_base_commits,
    infer_repo_info,
    get_switch_cmd,
    get_default_test_cmd,
)
from ..core.types import MilestoneTestConfig, MilestoneTestMode

__all__ = [
    # Main runner functions
    "run_single_milestone",
    "run_single_attempt",
    # Merge functions
    "merge_attempt_results",
    "merge_pytest_json_reports",
    # Helper functions
    "discover_milestones",
    "load_base_commits",
    "infer_repo_info",
    "get_switch_cmd",
    "get_default_test_cmd",
    # Config types
    "MilestoneTestConfig",
    "MilestoneTestMode",
    # Batch runner class
    "BatchMilestoneRunner",
]


class BatchMilestoneRunner:
    """
    Batch runner for multiple milestones.

    This class provides a convenient interface for running tests
    across multiple milestones with shared configuration.

    Example:
        runner = BatchMilestoneRunner(
            milestones=["M001", "M002", "M003"],
            work_dir=Path("DATA/harness_workspace/repo/scenario"),
            output_dir=Path("DATA/harness_workspace/repo/scenario/test_results"),
        )
        results = runner.run_all(workers=16, timeout=300, max_retries=3)
    """

    def __init__(
        self,
        milestones: List[str],
        work_dir: Path,
        output_dir: Optional[Path] = None,
        include_original: bool = False,
    ):
        """
        Initialize BatchMilestoneRunner.

        Args:
            milestones: List of milestone IDs to run
            work_dir: Working directory containing dockerfiles/, metadata.json, etc.
            output_dir: Output directory (default: {work_dir}/test_results)
            include_original: Whether to test at original state (base_commit)
        """
        self.milestones = milestones
        self.work_dir = Path(work_dir)
        self.output_dir = output_dir or (self.work_dir / "test_results")
        self.include_original = include_original

        # Load base commits
        self.base_commits = load_base_commits(self.work_dir)

        # Infer repo info
        self.repo_id, self.test_scenario = infer_repo_info(self.work_dir)

    def run_all(
        self,
        workers: int = 30,
        timeout: int = 300,
        max_retries: int = 3,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Run tests for all milestones.

        Args:
            workers: Number of parallel pytest workers
            timeout: Timeout for test execution in seconds
            max_retries: Maximum number of retry attempts
            verbose: Enable verbose output

        Returns:
            Dict mapping milestone IDs to their results
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        all_results = {}
        for milestone_id in self.milestones:
            base_commit = self.base_commits.get(milestone_id)

            result = run_single_milestone(
                milestone_id=milestone_id,
                work_dir=self.work_dir,
                output_dir=self.output_dir,
                repo_id=self.repo_id,
                test_scenario=self.test_scenario,
                base_commit=base_commit,
                workers=workers,
                timeout=timeout,
                max_retries=max_retries,
                include_original=self.include_original,
                verbose=verbose,
            )
            all_results[milestone_id] = result

        return all_results

    def run_milestone(
        self,
        milestone_id: str,
        workers: int = 30,
        timeout: int = 300,
        max_retries: int = 3,
        verbose: bool = False,
    ) -> Dict[str, Any]:
        """
        Run tests for a single milestone.

        Args:
            milestone_id: Milestone ID to test
            workers: Number of parallel pytest workers
            timeout: Timeout for test execution in seconds
            max_retries: Maximum number of retry attempts
            verbose: Enable verbose output

        Returns:
            Result dictionary for the milestone
        """
        self.output_dir.mkdir(parents=True, exist_ok=True)

        base_commit = self.base_commits.get(milestone_id)

        return run_single_milestone(
            milestone_id=milestone_id,
            work_dir=self.work_dir,
            output_dir=self.output_dir,
            repo_id=self.repo_id,
            test_scenario=self.test_scenario,
            base_commit=base_commit,
            workers=workers,
            timeout=timeout,
            max_retries=max_retries,
            include_original=self.include_original,
            verbose=verbose,
        )
