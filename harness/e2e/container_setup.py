"""Container setup utilities for agent execution.

This module provides shared container initialization logic used by both
run_milestone.py (single milestone mode) and orchestrator.py (E2E mode).
"""

import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from harness.e2e.agents import AgentFramework, get_agent_framework

logger = logging.getLogger("e2e.container_setup")


class ContainerSetup:
    """Docker container initialization with fakeroot user and Claude credentials."""

    def __init__(
        self,
        container_name: str,
        image_name: str,
        workdir: str = "/testbed",
        agent_name: str = "claude-code",
        e2e_workspace_path: Optional[Path] = None,
        agent_framework_name: str = "claude-code",
    ):
        """Initialize container setup.

        Args:
            container_name: Name for the Docker container
            image_name: Docker image to use
            workdir: Working directory inside container (default: /testbed)
            agent_name: Git user name for agent commits (default: claude)
            e2e_workspace_path: Path to mount as /e2e_workspace (for E2E mode)
            agent_framework_name: Agent framework to use (default: claude-code)
        """
        self.container_name = container_name
        self.image_name = image_name
        self.workdir = workdir
        self.agent_name = agent_name
        self.e2e_workspace_path = Path(e2e_workspace_path) if e2e_workspace_path else None
        self._framework: AgentFramework = get_agent_framework(agent_framework_name)

    def get_agent_mounts(self) -> list[str]:
        """Return Docker volume mount arguments for the agent.

        Delegates to the agent framework for agent-specific mounts.

        Returns:
            List of -v arguments for docker run
        """
        return self._framework.get_container_mounts()

    def get_agent_env_vars(self) -> list[str]:
        """Return Docker environment variable arguments for the agent.

        Delegates to the agent framework for agent-specific env vars.

        Returns:
            List of -e arguments for docker run
        """
        return self._framework.get_container_env_vars()

    # Backward compatibility alias
    def get_claude_mounts(self) -> list[str]:
        """Return Docker volume mount arguments for Claude credentials.

        Deprecated: Use get_agent_mounts() instead.

        Returns:
            List of -v arguments for docker run
        """
        return self.get_agent_mounts()

    def _get_base_init_script(self) -> str:
        """Return the base Python init script for container setup.

        This sets up common infrastructure:
        1. Installs sudo
        2. Creates fakeroot user
        3. Sets ownership for /testbed and other directories
        4. Configures git

        Returns:
            Python script as a string
        """
        return f'''
import os
import pwd
import stat
import shutil
from pathlib import Path
import subprocess

# === Step 1: Install sudo ===
try:
    result = subprocess.run(['which', 'sudo'], capture_output=True)
    if result.returncode != 0:
        # Try apt-get first (Debian/Ubuntu)
        apt_result = subprocess.run(['apt-get', 'update'], capture_output=True)
        if apt_result.returncode == 0:
            subprocess.run(['apt-get', 'install', '-y', '-qq', 'sudo'], capture_output=True)
        else:
            # Try apk (Alpine)
            subprocess.run(['apk', 'add', '--no-cache', 'sudo'], capture_output=True)
except Exception as e:
    print(f"Warning: Could not install sudo: {{e}}")

# === Step 2: Create fakeroot user ===
try:
    try:
        pwd.getpwnam('fakeroot')
        print("fakeroot user already exists")
    except KeyError:
        # Find next available UID >= 1000
        existing_uids = [u.pw_uid for u in pwd.getpwall()]
        uid = 1000
        while uid in existing_uids:
            uid += 1

        # Add to /etc/passwd (use GID 0 = root group for more permissions)
        with open('/etc/passwd', 'a') as f:
            f.write(f'fakeroot:x:{{uid}}:0:Fakeroot User:/home/fakeroot:/bin/bash\\n')

        # Also create a fakeroot group for compatibility
        with open('/etc/group', 'a') as f:
            f.write(f'fakeroot:x:{{uid}}:\\n')

        # Add fakeroot to root group (GID 0) explicitly
        # Read current /etc/group and add fakeroot to root group
        with open('/etc/group', 'r') as f:
            group_content = f.read()

        # Add fakeroot to root group if not already there
        lines = group_content.split('\\n')
        new_lines = []
        for line in lines:
            if line.startswith('root:'):
                parts = line.split(':')
                if len(parts) >= 4:
                    members = parts[3].split(',') if parts[3] else []
                    if 'fakeroot' not in members:
                        members.append('fakeroot')
                        parts[3] = ','.join(m for m in members if m)
                    line = ':'.join(parts)
            new_lines.append(line)

        with open('/etc/group', 'w') as f:
            f.write('\\n'.join(new_lines))
        print("Added fakeroot to root group (GID 0)")

        # Create home directory
        os.makedirs('/home/fakeroot', exist_ok=True)
        os.chown('/home/fakeroot', uid, 0)  # GID 0 = root group
        os.chmod('/home/fakeroot', 0o755)

        print(f"Created fakeroot user with UID={{uid}}, GID=0 (root group)")

        # Setup sudo access
        if os.path.isdir('/etc/sudoers.d'):
            with open('/etc/sudoers.d/fakeroot', 'w') as f:
                f.write('fakeroot ALL=(ALL) NOPASSWD:ALL\\n')
            os.chmod('/etc/sudoers.d/fakeroot', 0o440)
            print("Configured sudo access for fakeroot")
except Exception as e:
    print(f"Error creating fakeroot user: {{e}}")

# === Step 3: Set ownership ===
try:
    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid

    # Set ownership for home directory
    for root, dirs, files in os.walk('/home/fakeroot'):
        os.chown(root, uid, gid)
        os.chmod(root, os.stat(root).st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        for f in files:
            filepath = os.path.join(root, f)
            os.chown(filepath, uid, gid)
            os.chmod(filepath, os.stat(filepath).st_mode | stat.S_IRUSR | stat.S_IWUSR)

    # Set ownership for /testbed
    if os.path.exists('/testbed'):
        print(f"Setting ownership of /testbed to fakeroot (uid={{uid}}, gid={{gid}})")
        result = subprocess.run(['chown', '-R', f'{{uid}}:{{gid}}', '/testbed'], capture_output=True, text=True)
        if result.returncode == 0:
            print("Successfully set /testbed ownership to fakeroot")
        else:
            print(f"chown failed: {{result.stderr}}")

    # Set ownership for /e2e_workspace if exists
    if os.path.exists('/e2e_workspace'):
        result = subprocess.run(['chown', '-R', f'{{uid}}:{{gid}}', '/e2e_workspace'], capture_output=True, text=True)
        if result.returncode == 0:
            print("Successfully set /e2e_workspace ownership to fakeroot")

    # === Fix toolchain directories permissions (Cargo, Rustup, npm, etc.) ===
    # Give fakeroot full access to these directories
    toolchain_dirs = [
        '/usr/local/cargo',      # Cargo home
        '/usr/local/rustup',     # Rustup home
        '/root/.cargo',          # Alternative cargo location
        '/root/.rustup',         # Alternative rustup location
        '/usr/local/go',         # Go installation
        '/go',                   # Go workspace (GOPATH default in many images)
        '/root/go',              # Go workspace (alternative)
        '/usr/local/lib/node_modules',  # Global npm modules
        '/root/.npm',            # npm cache
        '/root/.cache',          # General cache (pip, etc.)
    ]

    for toolchain_dir in toolchain_dirs:
        if os.path.exists(toolchain_dir):
            # Option 1: Change ownership to fakeroot (most permissive)
            result = subprocess.run(['chown', '-R', f'{{uid}}:0', toolchain_dir], capture_output=True, text=True)
            if result.returncode == 0:
                print(f"Changed ownership of {{toolchain_dir}} to fakeroot")
            else:
                # Option 2: If chown fails, at least make it group-writable for root group
                result2 = subprocess.run(['chmod', '-R', 'g+rwX', toolchain_dir], capture_output=True, text=True)
                if result2.returncode == 0:
                    print(f"Made {{toolchain_dir}} group-writable")
                else:
                    print(f"Failed to fix permissions for {{toolchain_dir}}")

    # Ensure /tmp has correct permissions (some tools need it)
    if os.path.exists('/tmp'):
        os.chmod('/tmp', 0o1777)
        print("Set /tmp to 1777")
except Exception as e:
    print(f"Error setting ownership: {{e}}")

# === Step 4: Configure git ===
try:
    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid

    # Create gitconfig for fakeroot user
    gitconfig_path = '/home/fakeroot/.gitconfig'
    gitconfig_content = """[core]
\\tattributesFile = /home/fakeroot/.config/git/attributes
[user]
\\tname = {self.agent_name}
\\temail = agent@example.com
[safe]
\\tdirectory = /testbed
"""

    with open(gitconfig_path, 'w') as f:
        f.write(gitconfig_content)

    os.chown(gitconfig_path, uid, gid)
    os.chmod(gitconfig_path, 0o644)

    # Create .config/git directory
    git_config_dir = '/home/fakeroot/.config/git'
    os.makedirs(git_config_dir, exist_ok=True)
    os.chown(git_config_dir, uid, gid)
    os.chmod(git_config_dir, 0o755)

    # Create empty attributes file
    attributes_path = os.path.join(git_config_dir, 'attributes')
    with open(attributes_path, 'w') as f:
        pass
    os.chown(attributes_path, uid, gid)
    os.chmod(attributes_path, 0o644)

    print("Configured git for fakeroot user")
except Exception as e:
    print(f"Error configuring git: {{e}}")

print("Base container initialization complete!")
'''

    def get_init_script(self) -> str:
        """Return Python init script for container setup.

        Combines base initialization with agent-specific initialization.
        The base script sets up fakeroot user, sudo, git config.
        The agent-specific script sets up credentials, tools, etc.

        Returns:
            Combined Python script as a string
        """
        base_script = self._get_base_init_script()
        agent_script = self._framework.get_container_init_script(self.agent_name)

        return f"""{base_script}

# === Agent-specific initialization ===
{agent_script}

print("Container initialization complete!")
"""

    def start_container(self, extra_mounts: Optional[list[str]] = None, force: bool = False) -> None:
        """Start Docker container with proper initialization.

        Args:
            extra_mounts: Additional -v mount arguments
            force: If True, remove existing container first
        """
        # Check for existing container
        if self.container_exists():
            if force:
                logger.info(f"Removing existing container {self.container_name}...")
                subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
            else:
                if self.is_running():
                    logger.info(f"Container {self.container_name} already running")
                    return
                else:
                    logger.info(f"Starting existing container {self.container_name}...")
                    subprocess.run(["docker", "start", self.container_name], check=True)
                    return

        # Verify image exists
        result = subprocess.run(
            ["docker", "image", "inspect", self.image_name],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker image not found: {self.image_name}")

        logger.info(f"Launching container {self.container_name} from {self.image_name}...")

        # Build docker run command
        # Use --init to properly reap zombie child processes (e.g., plugin processes)
        docker_options = [
            "docker",
            "run",
            "-d",
            "--init",
            "--name",
            self.container_name,
            "--ulimit",
            "nofile=65535:65535",
            "-w",
            self.workdir,
            "-e",
            "HOME=/root",  # Start as root for setup
        ]

        # Add agent mounts (credentials, binaries, etc.)
        docker_options.extend(self.get_agent_mounts())

        # Add agent environment variables (API keys, etc.)
        docker_options.extend(self.get_agent_env_vars())

        # Add e2e_workspace mount if specified
        if self.e2e_workspace_path:
            self.e2e_workspace_path.mkdir(parents=True, exist_ok=True)
            docker_options.extend(["-v", f"{self.e2e_workspace_path.resolve()}:/e2e_workspace"])

        # Add extra mounts
        if extra_mounts:
            docker_options.extend(extra_mounts)

        # Add image and command
        cmd = docker_options + [self.image_name, "tail", "-f", "/dev/null"]

        logger.debug(f"Docker run command: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

        # Ensure Python3 is available for init script
        self._ensure_python3()

        # Run initialization script
        logger.info("Running container initialization...")
        init_script = self.get_init_script()
        result = subprocess.run(
            ["docker", "exec", self.container_name, "python3", "-c", init_script],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                logger.info(f"  {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    logger.warning(f"  {line}")

        # Wait for fakeroot user
        self._wait_for_fakeroot()

        logger.info(f"Container {self.container_name} launched and initialized.")

    def _ensure_python3(self) -> None:
        """Ensure Python3 is available in the container.

        If Python3 is not found, attempts to install it using the container's
        package manager (apt-get, apk, or yum).
        """
        # Check if python3 exists
        result = subprocess.run(
            ["docker", "exec", self.container_name, "which", "python3"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            logger.info("Python3 already available in container")
            return

        logger.info("Python3 not found, attempting to install...")

        # Try apt-get (Debian/Ubuntu) - preserve stderr for debugging
        install_script = """
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq python3-minimal
    exit $?
elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3
    exit $?
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q python3
    exit $?
else
    echo "No supported package manager found" >&2
    exit 1
fi
"""
        # Retry up to 3 times with exponential backoff
        max_retries = 3
        last_error = ""
        for attempt in range(max_retries):
            if attempt > 0:
                wait_time = 2**attempt  # 2, 4 seconds
                logger.info(
                    f"Retrying Python3 installation (attempt {attempt + 1}/{max_retries}) after {wait_time}s..."
                )
                time.sleep(wait_time)

            result = subprocess.run(
                ["docker", "exec", self.container_name, "/bin/sh", "-c", install_script],
                capture_output=True,
                text=True,
                timeout=180,  # 3 minute timeout for package installation
            )

            if result.returncode == 0:
                logger.info("Successfully installed Python3")
                return
            else:
                last_error = result.stderr.strip() if result.stderr else "Unknown error"
                logger.warning(f"Python3 installation attempt {attempt + 1} failed: {last_error}")

        # Final verification after all retries failed
        verify = subprocess.run(
            ["docker", "exec", self.container_name, "which", "python3"],
            capture_output=True,
            text=True,
        )
        if verify.returncode == 0:
            logger.info("Python3 is available despite installation errors")
            return

        raise RuntimeError(f"Python3 is required but could not be installed in the container: {last_error}")

    def _wait_for_fakeroot(self, max_wait: int = 10) -> bool:
        """Wait for fakeroot user to be created.

        Args:
            max_wait: Maximum seconds to wait

        Returns:
            True if fakeroot user is ready
        """
        logger.info("Waiting for fakeroot user...")
        for i in range(max_wait):
            time.sleep(1)
            result = subprocess.run(
                ["docker", "exec", self.container_name, "id", "fakeroot"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("fakeroot user created successfully")
                return True
            if i == max_wait - 1:
                logger.warning(f"Timeout waiting for fakeroot user (waited {max_wait}s)")
        return False

    def truncate_git_history(self, main_branch: str = "main") -> None:
        """Truncate git history to prevent agent from seeing future commits.

        This removes all tags, branches (except main), remotes, reflog,
        and runs garbage collection to remove unreachable objects.

        Args:
            main_branch: Name of the main branch to keep
        """
        logger.info(f"Truncating git history (main_branch={main_branch})...")

        truncate_script = f"""
set -e
cd /testbed

# Ensure git trusts this directory (avoid "dubious ownership" error)
git config --global --add safe.directory /testbed 2>/dev/null || true

MAIN_BRANCH="{main_branch}"

echo "=== Git History Truncation ==="
echo "Current HEAD: $(git rev-parse HEAD)"
echo "Current branch: $(git branch --show-current 2>/dev/null || echo 'detached')"
echo "Target main branch: $MAIN_BRANCH"

# Step 1: Delete all tags
echo ""
echo "Step 1: Deleting all tags..."
TAG_COUNT=$(git tag -l | wc -l)
if [ "$TAG_COUNT" -gt 0 ]; then
    git tag -l | xargs git tag -d
    echo "  Deleted $TAG_COUNT tags"
else
    echo "  No tags to delete"
fi

# Step 2: Reset main branch to HEAD
echo ""
echo "Step 2: Resetting $MAIN_BRANCH branch to current HEAD..."
CURRENT_HEAD=$(git rev-parse HEAD)

# Delete all branches
BRANCHES=$(git for-each-ref --format='%(refname:short)' refs/heads/)
for branch in $BRANCHES; do
    git branch -D "$branch" 2>/dev/null && echo "  Deleted branch: $branch" || true
done

# Create/reset main branch at current HEAD
git checkout -B "$MAIN_BRANCH" $CURRENT_HEAD 2>/dev/null
echo "  Created $MAIN_BRANCH branch at HEAD ($CURRENT_HEAD)"

# Step 3: Delete all remote tracking branches (fast method)
echo ""
echo "Step 3: Deleting remote tracking branches..."
REMOTE_BRANCHES=$(git branch -r 2>/dev/null | wc -l)
if [ "$REMOTE_BRANCHES" -gt 0 ]; then
    # Fast deletion: remove refs directory and packed-refs entries directly
    rm -rf .git/refs/remotes 2>/dev/null || true
    # Remove remote refs from packed-refs file if it exists
    if [ -f .git/packed-refs ]; then
        grep -v 'refs/remotes/' .git/packed-refs > .git/packed-refs.tmp 2>/dev/null || true
        mv .git/packed-refs.tmp .git/packed-refs 2>/dev/null || true
    fi
    # Remove remote config entries
    git config --remove-section remote.origin 2>/dev/null || true
    echo "  Removed all remotes ($REMOTE_BRANCHES tracking branches)"
else
    echo "  No remote branches"
fi

# Step 4: Clear reflog
echo ""
echo "Step 4: Clearing reflog..."
git reflog expire --expire=now --all 2>/dev/null || true
echo "  Reflog cleared"

# Step 5: Garbage collect
echo ""
echo "Step 5: Running garbage collection..."
git gc --prune=now --aggressive 2>/dev/null || git gc --prune=now || true
echo "  GC completed"

# Step 6: Verify
echo ""
echo "=== Verification ==="
echo "Tags remaining: $(git tag -l | wc -l)"
echo "Branches remaining: $(git branch | wc -l)"
echo "Remote branches: $(git branch -r 2>/dev/null | wc -l || echo 0)"
echo "HEAD: $(git rev-parse --short HEAD)"
echo "Current branch: $(git branch --show-current)"

echo ""
echo "Git history truncated successfully"
"""

        result = subprocess.run(
            [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                "-w",
                "/testbed",
                self.container_name,
                "/bin/sh",
                "-c",
                truncate_script,
            ],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                logger.info(f"  {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n"):
                if line.strip():
                    logger.warning(f"  {line}")

        if result.returncode != 0:
            logger.warning(f"Git history truncation returned non-zero exit code: {result.returncode}")
        else:
            logger.info("Git history truncation completed")

    def docker_exec(
        self,
        cmd: list[str],
        user: str = "fakeroot",
        check: bool = True,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """Execute command in container.

        Args:
            cmd: Command to execute
            user: User to run as (default: fakeroot)
            check: If True, raise on non-zero exit
            capture_output: If True, capture stdout/stderr

        Returns:
            CompletedProcess result
        """
        docker_cmd = [
            "docker",
            "exec",
            "--user",
            user,
            "-e",
            f"HOME=/home/{user}" if user != "root" else "HOME=/root",
            "-w",
            self.workdir,
            self.container_name,
        ] + cmd

        return subprocess.run(docker_cmd, capture_output=capture_output, text=True, check=check)

    def docker_exec_git(self, *git_args) -> subprocess.CompletedProcess:
        """Execute git command in container as fakeroot user.

        Args:
            *git_args: Git command arguments

        Returns:
            CompletedProcess result
        """
        # Use -c safe.directory to avoid ownership warnings when running as fakeroot
        return self.docker_exec(["git", "-c", f"safe.directory={self.workdir}", *git_args], check=False)

    def container_exists(self) -> bool:
        """Check if container exists (running or stopped)."""
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{self.container_name}$"],
            capture_output=True,
            text=True,
        )
        return self.container_name in result.stdout

    def is_running(self) -> bool:
        """Check if container is currently running."""
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.container_name],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() == "true"

    def cleanup(self, remove: bool = True) -> None:
        """Cleanup container.

        Args:
            remove: If True, remove container; otherwise just stop it
        """
        if not self.container_exists():
            return

        if remove:
            logger.info(f"Removing container {self.container_name}...")
            subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)
        else:
            logger.info(f"Stopping container {self.container_name}...")
            subprocess.run(["docker", "stop", self.container_name], capture_output=True)
