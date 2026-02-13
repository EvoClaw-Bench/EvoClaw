"""
E2E Trial Resume Module

Provides functionality to load and restore trial state from a previous run,
enabling resumption of interrupted E2E trials.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger("e2e.resume")


@dataclass
class TrialState:
    """Captured trial state for resume.

    This dataclass holds all the state needed to resume an interrupted trial.
    """

    trial_root: Path
    container_name: str

    # DAG state
    completed_milestones: Set[str] = field(default_factory=set)
    failed_milestones: Set[str] = field(default_factory=set)
    skipped_milestones: Set[str] = field(default_factory=set)
    submitted_milestones: Set[str] = field(default_factory=set)
    early_unlocked_milestones: Set[str] = field(default_factory=set)

    # Resume watcher state
    pending_debounce: Dict[str, dict] = field(default_factory=dict)  # mid -> debounce state dict
    pending_evaluations: Dict[str, dict] = field(default_factory=dict)  # key (mid#attempt) -> eval state dict

    # Evaluation state (for deduplication)
    evaluated_hashes: Dict[str, str] = field(default_factory=dict)  # milestone_id -> tag_hash

    # Agent state
    agent_session_id: Optional[str] = None

    # Original config (from trial_metadata.json)
    original_config: Dict = field(default_factory=dict)


class TrialStateLoader:
    """Load trial state from an existing trial directory.

    This class validates the trial directory structure and loads the state
    needed to resume an interrupted trial.
    """

    def __init__(self, trial_root: Path):
        """Initialize the loader.

        Args:
            trial_root: Path to the trial directory (e.g., e2e_trial/agent_run_001)
        """
        self.trial_root = trial_root.resolve()

    def validate(self) -> Tuple[bool, List[str]]:
        """Validate that the trial directory has all required files.

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        errors = []

        # Check trial_root exists
        if not self.trial_root.exists():
            errors.append(f"Trial directory not found: {self.trial_root}")
            return False, errors

        if not self.trial_root.is_dir():
            errors.append(f"Trial path is not a directory: {self.trial_root}")
            return False, errors

        # Check required files
        required_files = [
            ("trial_metadata.json", "Trial metadata"),
            ("evaluation/summary.json", "Evaluation summary"),
        ]

        for rel_path, description in required_files:
            file_path = self.trial_root / rel_path
            if not file_path.exists():
                errors.append(f"{description} not found: {file_path}")

        # Validate JSON files are parseable
        if (self.trial_root / "trial_metadata.json").exists():
            try:
                with open(self.trial_root / "trial_metadata.json") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                errors.append(f"Invalid trial_metadata.json: {e}")

        if (self.trial_root / "evaluation" / "summary.json").exists():
            try:
                with open(self.trial_root / "evaluation" / "summary.json") as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                errors.append(f"Invalid evaluation/summary.json: {e}")

        return len(errors) == 0, errors

    def load(self) -> TrialState:
        """Load trial state from the directory.

        Returns:
            TrialState object with all recovered state

        Raises:
            FileNotFoundError: If required files are missing
            json.JSONDecodeError: If JSON files are corrupted
        """
        # Load trial metadata
        metadata_path = self.trial_root / "trial_metadata.json"
        with open(metadata_path) as f:
            metadata = json.load(f)

        # Extract container name from metadata
        # Container name format: {repo_name}-{trial_name}
        repo_name = metadata.get("repo_name", "")
        trial_name = self.trial_root.name
        safe_repo_name = repo_name.replace(":", "_")
        container_name = f"{safe_repo_name}-{trial_name}"

        # Load evaluation summary
        summary_path = self.trial_root / "evaluation" / "summary.json"
        with open(summary_path) as f:
            summary = json.load(f)

        milestone_status = (
            summary.get("milestone_status", {}) if isinstance(summary.get("milestone_status"), dict) else {}
        )
        error_list = milestone_status.get("error", []) if isinstance(milestone_status.get("error"), list) else []
        error_base = {m for m in error_list if isinstance(m, str) and "-retry" not in m}

        resume_state = summary.get("resume_state") if isinstance(summary.get("resume_state"), dict) else None
        resume_dag = resume_state.get("dag") if resume_state and isinstance(resume_state.get("dag"), dict) else None

        if resume_dag:
            completed = set(resume_dag.get("completed", []) or [])
            failed = set(resume_dag.get("failed", []) or [])
            skipped = set(resume_dag.get("skipped", []) or [])
            submitted = set(resume_dag.get("submitted", []) or [])
            early_unlocked = set(resume_dag.get("early_unlocked", []) or [])

            pending_debounce = (
                resume_state.get("pending_debounce", {})
                if isinstance(resume_state.get("pending_debounce"), dict)
                else {}
            )
            pending_evaluations = (
                resume_state.get("pending_evaluations", {})
                if isinstance(resume_state.get("pending_evaluations"), dict)
                else {}
            )
        else:
            # Legacy fallback: resume based on final summary lists only.
            completed = set(milestone_status.get("passed", []))
            failed = set(milestone_status.get("failed", []))
            skipped = set(milestone_status.get("skipped", []))
            submitted = set(milestone_status.get("submitted", []))
            early_unlocked = set(milestone_status.get("early_unlocked", []))
            pending_debounce = {}
            pending_evaluations = {}

        # Early unlocked milestones must remain completed for DAG progression.
        completed = completed | early_unlocked

        # Error milestones should be retried on resume (normal mode). Do not roll back early-unlocked progress.
        retryable_errors = error_base - early_unlocked
        if retryable_errors:
            logger.info(f"  Error milestones (will retry): {sorted(retryable_errors)}")
        completed = completed - retryable_errors
        failed = failed - retryable_errors
        submitted = submitted - retryable_errors

        # Extract evaluated hashes from terminal results only (avoid poisoning with pending entries).
        evaluated_hashes: Dict[str, str] = {}
        results = summary.get("results", {}) if isinstance(summary.get("results"), dict) else {}
        for key, result in results.items():
            # Skip retry entries (e.g., "M001-retry1")
            if not isinstance(key, str) or "-retry" in key:
                continue
            if not isinstance(result, dict):
                continue
            eval_status = result.get("eval_status")
            if eval_status not in {"passed", "failed"}:
                continue
            tag_hash = result.get("tag_hash")
            if isinstance(tag_hash, str) and tag_hash:
                evaluated_hashes[key] = tag_hash

        # Drop stale pending evaluation entries if a terminal result was already recorded.
        if pending_evaluations and isinstance(pending_evaluations, dict) and results:
            cleaned_pending: Dict[str, dict] = {}
            for k, payload in pending_evaluations.items():
                if not isinstance(k, str) or not isinstance(payload, dict):
                    continue
                mid = payload.get("milestone_id")
                if not isinstance(mid, str) or not mid:
                    mid = k.split("#", 1)[0] if "#" in k else ""
                attempt = payload.get("attempt", 0)
                try:
                    attempt = int(attempt)
                except Exception:
                    attempt = 0
                result_key = mid if attempt == 0 else f"{mid}-retry{attempt}"
                existing = results.get(result_key)
                if isinstance(existing, dict) and existing.get("eval_status") in {"passed", "failed", "error"}:
                    continue
                cleaned_pending[k] = payload
            pending_evaluations = cleaned_pending

        # Load agent session ID if available
        agent_session_id = None
        session_candidates = [
            self.trial_root / "log" / ".agent_session_id",
            self.trial_root / "agent_logs" / ".agent_session_id",  # backward compatibility
        ]
        for session_id_file in session_candidates:
            if session_id_file.exists():
                agent_session_id = session_id_file.read_text().strip()
                break

        # Create and return TrialState
        state = TrialState(
            trial_root=self.trial_root,
            container_name=container_name,
            completed_milestones=completed,
            failed_milestones=failed,
            skipped_milestones=skipped,
            submitted_milestones=submitted,
            early_unlocked_milestones=early_unlocked,
            pending_debounce=pending_debounce,
            pending_evaluations=pending_evaluations,
            evaluated_hashes=evaluated_hashes,
            agent_session_id=agent_session_id,
            original_config=metadata,
        )

        logger.info(f"Loaded trial state from {self.trial_root}")
        logger.info(f"  Container: {container_name}")
        logger.info(f"  Completed: {len(completed)} milestones")
        logger.info(f"  Failed: {len(failed)} milestones")
        logger.info(f"  Skipped: {len(skipped)} milestones")
        logger.info(f"  Submitted: {len(submitted)} milestones")
        logger.info(f"  Pending debounce: {len(pending_debounce)}")
        logger.info(f"  Pending evaluations: {len(pending_evaluations)}")
        logger.info(f"  Evaluated hashes: {len(evaluated_hashes)}")
        logger.info(f"  Agent session: {agent_session_id[:8] if agent_session_id else 'None'}...")

        return state


def verify_container_for_resume(container_name: str) -> Tuple[bool, List[str]]:
    """Verify that a container is suitable for resume.

    Args:
        container_name: Name of the Docker container

    Returns:
        Tuple of (is_valid, list of issues)
    """
    import subprocess

    issues = []

    # Check if container exists
    result = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        issues.append(f"Container '{container_name}' does not exist")
        return False, issues

    # Check if container is running
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
        capture_output=True,
        text=True,
    )

    is_running = result.stdout.strip() == "true"

    if not is_running:
        # Try to start it
        logger.info(f"Container {container_name} is stopped, attempting to start...")
        start_result = subprocess.run(
            ["docker", "start", container_name],
            capture_output=True,
            text=True,
        )
        if start_result.returncode != 0:
            issues.append(f"Container '{container_name}' exists but cannot be started: {start_result.stderr}")
            return False, issues
        logger.info(f"Container {container_name} started successfully")

    # Verify /testbed exists in container
    result = subprocess.run(
        ["docker", "exec", container_name, "test", "-d", "/testbed"],
        capture_output=True,
    )
    if result.returncode != 0:
        issues.append("Container /testbed directory not found")

    # Verify git repo is intact
    result = subprocess.run(
        ["docker", "exec", container_name, "git", "-C", "/testbed", "status"],
        capture_output=True,
    )
    if result.returncode != 0:
        issues.append("Git repository in /testbed is not valid")

    return len(issues) == 0, issues
