"""
Core components for test runner framework.
"""

from .types import (
    TestMode,
    CommitTestConfig,
    MilestoneTestMode,
    MilestoneTestConfig,
)
from .docker import (
    DockerExecutor,
    DockerRunner,
    check_image_exists,
    build_docker_image,
    cleanup_docker_image,
)
from .merger import ResultMerger, merge_outcome, is_flaky
from .classifier import TestClassifier

__all__ = [
    # Commit-level types
    "TestMode",
    "CommitTestConfig",
    # Milestone-level types
    "MilestoneTestMode",
    "MilestoneTestConfig",
    # Docker utilities
    "DockerExecutor",
    "DockerRunner",
    "check_image_exists",
    "build_docker_image",
    "cleanup_docker_image",
    # Merger utilities
    "ResultMerger",
    "merge_outcome",
    "is_flaky",
    # Classifier
    "TestClassifier",
]
