"""
Test Runner Module

A unified framework for running tests at different levels:
- Single state: Use SingleStateTestRunner for base/final image validation
- Commit level: Use run_commit_tests.py CLI
- Milestone level: Use run_milestone_tests.py CLI
"""

from .core.types import TestMode, CommitTestConfig, BaseValidationConfig
from .core.docker import DockerRunner, DockerExecutor
from .core.merger import ResultMerger
from .core.classifier import TestClassifier
from .core.report_parser import parse_test_report, merge_test_reports
from .single_state_runner import SingleStateTestRunner, TestRunResult

__all__ = [
    # Types
    "TestMode",
    "CommitTestConfig",
    "BaseValidationConfig",
    # Docker utilities
    "DockerRunner",
    "DockerExecutor",
    # Test execution
    "SingleStateTestRunner",
    "TestRunResult",
    # Result processing
    "ResultMerger",
    "TestClassifier",
    "parse_test_report",
    "merge_test_reports",
]
