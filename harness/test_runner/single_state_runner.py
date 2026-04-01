#!/usr/bin/env python3
"""
Single-State Test Runner for Docker Containers.

This module provides a unified interface for running tests in a single state
(e.g., the current state of a Docker image) and collecting results.

Unlike run_commit_tests.py which runs tests in two states (before/after) and
classifies results, this module is designed for simpler use cases:
- Base/final image validation
- Single commit testing
- Environment verification

Features:
    - Supports multiple languages/frameworks (pytest, maven, gradle, go_test, cargo, jest, mocha)
    - Unified result format using report_parser
    - Docker volume mounting for test output collection
    - Configurable timeout and workers
    - CLI and Python API

CLI Usage:
    # Run tests with default settings (Python/pytest)
    python -m harness.test_runner.single_state_runner \\
        --image myproject/base:latest \\
        --output-dir ./test_results

    # Run with specific settings
    python -m harness.test_runner.single_state_runner \\
        --image myproject/base:latest \\
        --output-dir ./test_results \\
        --language python \\
        --test-framework pytest \\
        --test-dir tests/ \\
        --workers 4 \\
        --timeout 900

    # Output JSON result
    python -m harness.test_runner.single_state_runner \\
        --image myproject/base:latest \\
        --output-dir ./test_results \\
        --json

Python API:
    from harness.test_runner.single_state_runner import SingleStateTestRunner
    from harness.test_runner.core.types import BaseValidationConfig

    config = BaseValidationConfig.from_defaults("python", "pytest")
    runner = SingleStateTestRunner("my-image:latest", config)

    result = runner.run_tests(
        test_dir="test/",
        output_dir=Path("./test_results"),
        timeout=600,
        workers=4,
    )

    print(f"Passed: {result.summary['passed']}, Failed: {result.summary['failed']}")
"""

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core.docker import DockerRunner
from .core.report_parser import parse_test_report, convert_to_summary
from .core.types import BaseValidationConfig

logger = logging.getLogger(__name__)


@dataclass
class TestRunResult:
    """
    Result of a single-state test run.

    Attributes:
        success: Whether the test run completed (not whether all tests passed)
        message: Human-readable status message
        summary: Test summary statistics (passed, failed, skipped, error, total)
        output_file: Path to the raw test output file
        summary_file: Path to the parsed summary file (if generated)
        duration: Test run duration in seconds
        framework: Test framework used
        failed_tests: List of failed test node IDs (first 20)
        details: Additional details about the test run
    """

    success: bool
    message: str
    summary: Dict[str, int] = field(default_factory=dict)
    output_file: Optional[Path] = None
    summary_file: Optional[Path] = None
    duration: float = 0.0
    framework: str = "unknown"
    failed_tests: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = asdict(self)
        # Convert Path objects to strings
        if self.output_file:
            result["output_file"] = str(self.output_file)
        if self.summary_file:
            result["summary_file"] = str(self.summary_file)
        return result

    def save(self, output_path: Path) -> None:
        """Save result to JSON file."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)


class SingleStateTestRunner:
    """
    Runs tests in a Docker container and collects results.

    This runner executes tests in the current state of a Docker image,
    without any git state switching. It's suitable for:
    - Validating base/final Docker images
    - Running tests at a specific commit
    - Environment verification testing

    The runner handles:
    - Docker container creation with volume mounts
    - Test command execution with timeout
    - Result parsing for multiple frameworks
    - Summary generation
    """

    def __init__(
        self,
        image_name: str,
        config: Optional[BaseValidationConfig] = None,
    ):
        """
        Initialize the test runner.

        Args:
            image_name: Docker image name to run tests in
            config: Validation configuration (defaults to Python/pytest)
        """
        self.image_name = image_name
        self.config = config or BaseValidationConfig.from_defaults("python", "pytest")
        self._docker_runner = DockerRunner(image_name)

    @property
    def framework(self) -> str:
        """Get the test framework name."""
        return self.config.test_framework

    @property
    def language(self) -> str:
        """Get the programming language."""
        return self.config.language

    def _get_output_filename(self) -> str:
        """
        Determine the output filename based on framework.

        Returns:
            Filename for test output (e.g., "output.json", "output.jsonl", "output.log")
        """
        framework = self.framework

        if framework == "pytest":
            return "output.json"
        elif framework == "go_test":
            return "output.jsonl"
        elif framework in ("maven", "gradle", "cargo", "unittest"):
            return "output.log"
        elif framework in ("jest", "mocha", "vitest"):
            return "output.json"
        else:
            return "output.log"

    def _build_test_command(
        self,
        test_dir: str,
        output_file: str,
        timeout: int,
        workers: int,
        extra_args: str = "",
        mode_name: str = "default",
    ) -> str:
        """
        Build the test command from configuration.

        Args:
            test_dir: Test directory path
            output_file: Output filename
            timeout: Test timeout in seconds
            workers: Number of parallel workers
            extra_args: Additional test arguments
            mode_name: Test mode name from config

        Returns:
            Formatted test command string
        """
        # Get command template from config
        test_cmd_template = self.config.get_run_cmd(mode_name)

        if not test_cmd_template:
            # No command template, can't run tests
            raise ValueError(f"No test command configured for mode '{mode_name}'")

        # Format the command with parameters
        test_cmd = test_cmd_template.format(
            test_dir=test_dir,
            output_file=output_file,
            timeout=timeout,
            workers=workers,
            extra_args=extra_args,
        )

        return test_cmd

    def run_tests(
        self,
        test_dir: str = "test/",
        output_dir: Optional[Path] = None,
        timeout: int = 600,
        workers: int = 1,
        extra_args: str = "",
        mode_name: str = "default",
        verbose: bool = True,
    ) -> TestRunResult:
        """
        Run tests and collect results.

        Args:
            test_dir: Test directory path (relative to /testbed in container)
            output_dir: Directory to save test results (mounted as /output in container)
            timeout: Test timeout in seconds
            workers: Number of parallel workers (for pytest -n)
            extra_args: Additional test arguments
            mode_name: Test mode name from config
            verbose: Print progress messages

        Returns:
            TestRunResult with execution status and parsed results
        """
        start_time = time.time()

        if output_dir is None:
            return TestRunResult(
                success=False,
                message="No output directory specified for test results",
                duration=0.0,
                framework=self.framework,
            )

        # Create output directory
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        # Determine output filename
        output_file = self._get_output_filename()
        output_path = output_dir / output_file

        # Build test command
        try:
            test_cmd = self._build_test_command(
                test_dir=test_dir,
                output_file=output_file,
                timeout=timeout,
                workers=workers,
                extra_args=extra_args,
                mode_name=mode_name,
            )
        except ValueError as e:
            return TestRunResult(
                success=False,
                message=str(e),
                duration=time.time() - start_time,
                framework=self.framework,
            )

        if verbose:
            logger.info(f"Running tests in {self.image_name}...")
            logger.info(f"  Test dir: {test_dir}")
            logger.info(f"  Output: {output_dir}")

        # Build the full command
        # For pytest/go_test, output is handled by the test command itself
        # For other frameworks, we may need tee for log capture
        is_self_reporting = self.framework in ("pytest", "go_test", "jest", "mocha")

        if is_self_reporting:
            full_cmd = test_cmd
        elif "| tee " in test_cmd or "> /output" in test_cmd:
            # Command already has output redirection
            full_cmd = test_cmd
        else:
            # Add tee for log capture
            full_cmd = f"{test_cmd} 2>&1 | tee /output/{output_file}"

        # Run in container with volume mount
        try:
            returncode, stdout, stderr = self._docker_runner.run(
                script=full_cmd,
                timeout=timeout,
                extra_volumes={str(output_dir.resolve()): "/output"},
            )
        except Exception as e:
            return TestRunResult(
                success=False,
                message=f"Failed to run tests: {e}",
                duration=time.time() - start_time,
                framework=self.framework,
            )

        duration = time.time() - start_time

        # Check if output file was created
        if not output_path.exists():
            # Try to extract info from stdout/stderr
            return TestRunResult(
                success=False,
                message=f"Test output file not created: {output_path}",
                duration=duration,
                framework=self.framework,
                details={
                    "returncode": returncode,
                    "stdout": stdout[-2000:] if stdout else "",
                    "stderr": stderr[-2000:] if stderr else "",
                },
            )

        # Parse results
        try:
            parsed = parse_test_report(output_path, self.framework)
            summary = parsed.get("summary", {})

            # Generate human-readable summary file
            summary_file = output_dir / "summary.json"
            try:
                convert_to_summary(output_path, summary_file, self.framework)
            except Exception as e:
                logger.warning(f"Failed to generate summary: {e}")
                summary_file = None

            # Extract failed tests list (first 20)
            failed_tests = []
            for test in parsed.get("tests", []):
                if test.get("outcome") in ("failed", "error"):
                    failed_tests.append(test.get("nodeid", "unknown"))
                    if len(failed_tests) >= 20:
                        break

            # Build result message
            passed = summary.get("passed", 0)
            failed = summary.get("failed", 0)
            skipped = summary.get("skipped", 0)
            error = summary.get("error", 0)
            total = summary.get("total", 0)

            message = f"Tests completed: {passed} passed, {failed} failed, {skipped} skipped"
            if error > 0:
                message += f", {error} errors"

            # Consider success if tests actually ran
            # (having failures doesn't mean the run failed - the env worked)
            success = total > 0

            result = TestRunResult(
                success=success,
                message=message,
                summary=summary,
                output_file=output_path,
                summary_file=summary_file,
                duration=duration,
                framework=self.framework,
                failed_tests=failed_tests,
                details={
                    "returncode": returncode,
                    "mode": mode_name,
                    "workers": workers,
                },
            )

            # Save result to output directory
            result_file = output_dir / "test_result.json"
            result.save(result_file)

            return result

        except Exception as e:
            return TestRunResult(
                success=False,
                message=f"Failed to parse test results: {e}",
                output_file=output_path,
                duration=duration,
                framework=self.framework,
                details={
                    "returncode": returncode,
                    "parse_error": str(e),
                },
            )


def run_docker_command(
    image_name: str,
    command: str,
    timeout: int = 120,
    workdir: str = "/testbed",
) -> tuple[bool, str, str]:
    """
    Run a command inside a Docker container (convenience function).

    This is a simpler interface for running single commands without
    full test infrastructure.

    Args:
        image_name: Docker image name
        command: Command to run inside container
        timeout: Command timeout in seconds
        workdir: Working directory inside container

    Returns:
        Tuple of (success, stdout, stderr)
    """
    # Use --init to properly reap zombie child processes
    full_command = f'docker run --rm --init -w {workdir} {image_name} bash -c "{command}"'

    try:
        result = subprocess.run(
            full_command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        success = result.returncode == 0
        return success, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"Command timed out after {timeout}s"
    except Exception as e:
        return False, "", str(e)


def main():
    """CLI entry point for single-state test runner."""
    parser = argparse.ArgumentParser(
        description="Run tests in a Docker container and collect results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run tests with default settings (Python/pytest)
  python -m harness.test_runner.single_state_runner \\
      --image myproject/base:latest \\
      --output-dir ./test_results

  # Run tests with specific language/framework
  python -m harness.test_runner.single_state_runner \\
      --image myproject/base:latest \\
      --output-dir ./test_results \\
      --language python \\
      --test-framework pytest \\
      --test-dir tests/

  # Run with parallel workers and custom timeout
  python -m harness.test_runner.single_state_runner \\
      --image myproject/base:latest \\
      --output-dir ./test_results \\
      --workers 4 \\
      --timeout 900

  # Output JSON result to stdout
  python -m harness.test_runner.single_state_runner \\
      --image myproject/base:latest \\
      --output-dir ./test_results \\
      --json
        """,
    )

    # Required arguments
    parser.add_argument("--image", required=True, help="Docker image name to run tests in")
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to save test results")

    # Language and framework
    parser.add_argument("--language", default="python", help="Programming language (default: python)")
    parser.add_argument("--test-framework", default="pytest", help="Test framework (default: pytest)")
    parser.add_argument("--test-dir", default="test/", help="Test directory path relative to /testbed (default: test/)")

    # Test configuration
    parser.add_argument("--timeout", type=int, default=600, help="Test timeout in seconds (default: 600)")
    parser.add_argument("-n", "--workers", type=int, default=1, help="Number of parallel workers (default: 1)")
    parser.add_argument("--extra-args", default="", help="Additional arguments to pass to test command")
    parser.add_argument("--mode", default="default", help="Test mode name from config (default: default)")

    # Output options
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Output result as JSON to stdout")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output (only show result)")

    args = parser.parse_args()

    # Configure logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")
    elif args.quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Create configuration
    try:
        config = BaseValidationConfig.from_defaults(args.language, args.test_framework)
    except Exception as e:
        print(f"Error: Failed to create config for {args.language}/{args.test_framework}: {e}", file=sys.stderr)
        sys.exit(1)

    # Create runner
    runner = SingleStateTestRunner(args.image, config)

    if not args.quiet:
        print(f"Running tests in: {args.image}")
        print(f"Language: {args.language}, Framework: {args.test_framework}")
        print(f"Test directory: {args.test_dir}")
        print(f"Output directory: {args.output_dir}")
        print(f"Timeout: {args.timeout}s, Workers: {args.workers}")
        print()

    # Run tests
    result = runner.run_tests(
        test_dir=args.test_dir,
        output_dir=args.output_dir,
        timeout=args.timeout,
        workers=args.workers,
        extra_args=args.extra_args,
        mode_name=args.mode,
        verbose=not args.quiet,
    )

    # Output result
    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        # Print summary
        print()
        print("=" * 60)
        status_icon = "✅" if result.success else "❌"
        print(f"{status_icon} {result.message}")
        print(f"Duration: {result.duration:.2f}s")

        if result.summary:
            s = result.summary
            print(
                f"Summary: {s.get('passed', 0)} passed, {s.get('failed', 0)} failed, "
                f"{s.get('skipped', 0)} skipped, {s.get('error', 0)} errors"
            )

        if result.failed_tests:
            print(f"\nFailed tests (first {len(result.failed_tests)}):")
            for test_id in result.failed_tests[:10]:
                print(f"  - {test_id}")

        if result.output_file:
            print(f"\nOutput file: {result.output_file}")
        print("=" * 60)

    # Exit with appropriate code
    sys.exit(0 if result.success else 1)


if __name__ == "__main__":
    main()
