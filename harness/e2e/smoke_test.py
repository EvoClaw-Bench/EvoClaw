#!/usr/bin/env python3
import argparse
import logging
import multiprocessing
import sys
import time
import shutil
import subprocess
from pathlib import Path
from typing import Optional

# Ensure project root is in sys.path
project_root = Path(__file__).parent.parent.parent.resolve()
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from harness.e2e.orchestrator import E2EOrchestrator

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("e2e.smoke_test")


def get_next_trial_name(base_name: str, result_dir: Path) -> str:
    """Generate next trial name with auto-incrementing suffix.

    Args:
        base_name: Base name for the trial (e.g., 'smoke_test')
        result_dir: Parent directory where trials are stored

    Returns:
        Trial name with suffix (e.g., 'smoke_test_001', 'smoke_test_002')
    """
    trial_path = Path(base_name)
    parent_dir = result_dir / trial_path.parent if trial_path.parent != Path(".") else result_dir
    short_name = trial_path.name

    if not parent_dir.exists():
        return f"{base_name}_001"

    max_num = 0
    found_existing = False

    exact = parent_dir / short_name
    if exact.exists():
        found_existing = True

    for entry in parent_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if not name.startswith(f"{short_name}_"):
            continue
        suffix = name[len(short_name) + 1 :]
        if len(suffix) != 3 or not suffix.isdigit():
            continue
        found_existing = True
        max_num = max(max_num, int(suffix))

    if found_existing:
        new_short_name = f"{short_name}_{max_num + 1:03d}"
        if trial_path.parent != Path("."):
            return str(trial_path.parent / new_short_name)
        return new_short_name
    return f"{base_name}_001"


class MockAgent:
    """A dumb agent that applies gold patches based on task status.

    Strategy: For each milestone, checkout to the milestone-end tag in the testbed
    (which already exists in the base image), then create the agent-impl tag.
    This ensures the src directory is always in the correct state for evaluation.
    """

    # Map repository names to their base tags
    REPO_BASE_TAGS = {
        "urllib3_urllib3_2.0.6_2.3.0": "2.0.6",
        # Add other repos here as needed
    }

    def __init__(self, container_name: str, workspace_root: Path, repo_name: str, use_gold_patch: bool = True):
        self.container_name = container_name
        self.workspace_root = workspace_root
        self.repo_name = repo_name
        self.patches_dir = workspace_root / "milestone_patches"
        self.use_gold_patch = use_gold_patch
        self.submitted_tasks = set()  # Track submitted tasks to avoid re-submission

    def run_step(self) -> bool:
        """
        Run a single step:
        1. Check status
        2. If task available, checkout to gold state & submit
        3. Return True if submitted, False if waiting
        """
        # 1. Read Task Queue (silent mode - no status info)
        queue = self._read_container_file("/e2e_workspace/TASK_QUEUE.md")
        if not queue:
            logger.info("[MockAgent] No task queue file yet. Waiting...")
            return False

        # 2. Parse Next Task
        next_task = self._parse_next_task(queue)
        if not next_task:
            logger.info("[MockAgent] No available tasks found in queue. Waiting...")
            return False

        logger.info(f"[MockAgent] Found available task: {next_task}")

        # 3. Apply src patch (container is already at milestone-{id}-start state)
        if self.use_gold_patch:
            logger.info(f"[MockAgent] Applying src patch for {next_task}...")
            self._apply_src_patch(next_task)
        else:
            # Simple mode - just append to README
            logger.info(f"[MockAgent] Simple mode: simulating work for {next_task}")
            self._docker_exec("sh", "-c", f"echo '# Implemented {next_task}' >> README.md", check=True)

        # 4. Submit (Tag)
        self._create_agent_tag(next_task)

        logger.info(f"[MockAgent] 🚀 Submitted {next_task}. Exiting step to wait for evaluation.")
        return True

    def _read_container_file(self, path: str) -> str:
        res = subprocess.run(["docker", "exec", self.container_name, "cat", path], capture_output=True, text=True)
        if res.returncode != 0:
            return ""
        return res.stdout

    def _parse_next_task(self, queue_content: str) -> Optional[str]:
        """Parse TASK_QUEUE.md to find first available task not yet submitted.

        New format (silent mode):
        ## Available Tasks
        - M001: See SRS at /e2e_workspace/srs/M001_SRS.md
        - M002: See SRS at /e2e_workspace/srs/M002_SRS.md
        """
        import re

        available_tasks = []
        lines = queue_content.split("\n")
        for line in lines:
            # Format: "- M001: See SRS at ..."
            if line.strip().startswith("- M"):
                match = re.search(r"(M\d+)", line)
                if match:
                    task = match.group(1)
                    if task not in self.submitted_tasks:
                        available_tasks.append(task)

        if not available_tasks:
            return None

        # Return first available task (sorted for determinism)
        return sorted(available_tasks)[0]

    def _docker_exec(self, *args, **kwargs):
        """Execute command in container as fakeroot user."""
        cmd = [
            "docker",
            "exec",
            "--user",
            "fakeroot",
            "-e",
            "HOME=/home/fakeroot",
            "-w",
            "/testbed",
            self.container_name,
            *args,
        ]
        return subprocess.run(cmd, **kwargs)

    def _apply_src_patch(self, milestone_id: str):
        """Apply the src-only gold patch for the milestone.

        The Docker image already has /testbed at milestone-{id}-start state.
        We only need to apply the _src.patch which contains source code changes.
        Test files may have ENV-PATCH applied by Dockerfile, so we only touch src/.
        """
        logger.info(f"[MockAgent] Applying src patch for {milestone_id}...")

        # Get patch path - use _src.patch which only contains src/ changes
        patch_path = self.workspace_root / "milestone_patches" / f"{milestone_id}_src.patch"
        if not patch_path.exists():
            raise FileNotFoundError(f"Src patch not found for {milestone_id} at {patch_path}")

        logger.info(f"[MockAgent] Using patch: {patch_path}")

        # Copy patch to container
        container_patch_path = f"/tmp/{milestone_id}_src.patch"
        subprocess.run(["docker", "cp", str(patch_path), f"{self.container_name}:{container_patch_path}"], check=True)

        # Apply patch (no checkout needed - container is already at correct start state)
        # Use --3way to handle context offset when applying multiple patches
        res = self._docker_exec(
            "git", "apply", "--3way", "--verbose", container_patch_path, capture_output=True, text=True
        )
        if res.returncode != 0:
            logger.error(f"[MockAgent] Failed to apply src patch for {milestone_id}: {res.stderr}")
            # Dump status for debugging
            self._docker_exec("git", "status")
            raise RuntimeError(
                f"Failed to apply src patch for {milestone_id}. Container should be at milestone-{milestone_id}-start state."
            )

        logger.info(f"[MockAgent] ✓ Applied src patch for {milestone_id}")

        # Commit the changes so we can tag them
        self._docker_exec("git", "add", "src/")
        self._docker_exec("git", "commit", "-m", f"Implemented {milestone_id} via gold src patch")

    def _create_agent_tag(self, milestone_id: str):
        """Create agent-impl-* tag at current HEAD."""
        tag_name = f"agent-impl-{milestone_id}"

        # Create tag (force to overwrite if exists)
        res = self._docker_exec("git", "tag", "-f", tag_name, capture_output=True, text=True)
        if res.returncode != 0:
            logger.error(f"[MockAgent] git tag failed: {res.stderr}")
            raise RuntimeError(f"Failed to create tag {tag_name}: {res.stderr}")

        logger.info(f"[MockAgent] ✓ Created tag {tag_name}")

        # Track submitted task to avoid re-submission
        self.submitted_tasks.add(milestone_id)


def start_watcher(orchestrator):
    """Entry point for the watcher process."""
    try:
        orchestrator.start_watcher()
    except Exception as e:
        logger.error(f"Watcher process died: {e}", exc_info=True)


def start_mock_agent(container_name: str, workspace_root: Path, repo_name: str, use_gold_patch: bool = True):
    """Entry point for the mock agent process (Loop)."""
    # Wait for watcher to initialize
    time.sleep(10)

    agent = MockAgent(container_name, workspace_root, repo_name, use_gold_patch=use_gold_patch)

    while True:
        try:
            # Run one step (Check -> Apply -> Submit)
            submitted = agent.run_step()

            if submitted:
                # If submitted, we wait for the system to react.
                # In a real agent runner, we might exit here and be restarted.
                # Here we just sleep and loop.
                # Wait for status to change from 'Available' to 'Evaluating' or 'Completed'
                # Actually, we just wait a bit. The next run_step will see "Evaluating" (no ToDo) or "Completed" (next ToDo).
                time.sleep(5)
            else:
                # No task available (maybe evaluating, or done). Sleep longer.
                time.sleep(5)

        except Exception as e:
            logger.error(f"Mock Agent loop error: {e}")
            time.sleep(10)


def main():
    parser = argparse.ArgumentParser(description="Smoke Test for E2E Framework")
    parser.add_argument("--repo-name", required=True, help="Repository name (e.g., urllib3_urllib3_2.0.6_2.3.0)")
    parser.add_argument(
        "--image", required=True, help="Base docker image (should have /testbed with correct code version)"
    )
    parser.add_argument(
        "--workspace-root", type=Path, required=True, help="Input Data Root (contains dependencies.csv, patches)"
    )
    parser.add_argument(
        "--simple-mode",
        action="store_true",
        help="Simple smoke test mode: only append comment to README.md (default: apply gold patch)",
    )
    # Note: --base-repo-path removed - we now use the image's built-in /testbed for version compatibility

    args = parser.parse_args()
    use_gold_patch = not args.simple_mode

    # Setup Paths
    workspace_root = args.workspace_root.resolve()

    # Create e2e_trial directory for all trials
    e2e_trial_dir = workspace_root / "e2e_trial"
    e2e_trial_dir.mkdir(parents=True, exist_ok=True)

    # Generate next trial name with auto-incrementing suffix
    trial_name = get_next_trial_name("smoke_test", e2e_trial_dir)
    trial_root = e2e_trial_dir / trial_name

    logger.info(f"Creating new trial: {trial_name}")
    trial_root.mkdir(parents=True, exist_ok=True)

    # Cleanup previous container to avoid stale state
    container_name = f"{args.repo_name}-e2e-runner"
    subprocess.run(
        ["docker", "rm", "-f", container_name], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # Standard paths within workspace
    dag_path = workspace_root / "dependencies.csv"
    srs_root = workspace_root / "srs"

    # Note: No base_repo_path needed - we use the image's built-in /testbed
    # This ensures Python version compatibility between code and runtime

    logger.info(f"Workspace: {workspace_root}")
    logger.info(f"Trial Root: {trial_root}")
    logger.info(f"Docker Image: {args.image} (using built-in /testbed)")
    logger.info(f"Mode: {'Gold Patch (real evaluation)' if use_gold_patch else 'Simple (pipeline test only)'}")

    # Initialize Orchestrator
    orchestrator = E2EOrchestrator(
        repo_name=args.repo_name,
        milestone_version="test_multi_stage_v2",
        image_name=args.image,
        dag_path=dag_path,
        srs_root=srs_root,
        trial_root=trial_root,
        workspace_root=workspace_root,
        # Note: No base_repo_path - using image's built-in /testbed
        agent_name="mock_agent",
        model="mock_model",
    )

    # Start Processes
    p_watcher = multiprocessing.Process(target=start_watcher, args=(orchestrator,))
    p_agent = multiprocessing.Process(
        target=start_mock_agent, args=(orchestrator.container_name, workspace_root, args.repo_name, use_gold_patch)
    )

    logger.info("Starting Smoke Test...")
    p_watcher.start()
    p_agent.start()

    try:
        # Wait for completion or timeout
        # In a real test, we might monitor the dag status from here too.
        while True:
            if not p_watcher.is_alive():
                logger.error("Watcher died!")
                break
            if not p_agent.is_alive():
                logger.error("Agent died!")
                break

            # Check if DAG is done (by checking local Orchestrator instance? No, state is in process)
            # We can check the TASK_QUEUE.md file in the container or trial root
            # But Orchestrator updates trial_root/TASK_QUEUE.md
            queue_file = trial_root / "TASK_QUEUE.md"
            if queue_file.exists():
                content = queue_file.read_text()
                # In silent mode, check if no tasks are available
                if "(No tasks currently available)" in content:
                    # This means all tasks are either done or blocked
                    # Let's just run forever until user Ctrl+C for this interactive smoke test.
                    pass

            time.sleep(5)

    except KeyboardInterrupt:
        logger.info("Stopping Smoke Test...")
        p_watcher.terminate()
        p_agent.terminate()

    logger.info(f"Smoke Test artifacts are in: {trial_root}")


if __name__ == "__main__":
    main()
