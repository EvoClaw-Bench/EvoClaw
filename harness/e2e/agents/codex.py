"""OpenAI Codex agent framework implementation."""

import logging
import os
from typing import List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework

logger = logging.getLogger(__name__)


@register_framework("codex")
class CodexFramework(AgentFramework):
    """Agent framework implementation for OpenAI Codex CLI.

    Codex CLI is OpenAI's coding agent that runs in the terminal.
    https://github.com/openai/codex

    This implementation uses API mode with unified environment variables,
    supporting proxy servers that route to multiple providers.

    Environment variables:
        UNIFIED_API_KEY: API key for the unified proxy
        UNIFIED_BASE_URL: Base URL for the unified proxy
    """

    FRAMEWORK_NAME = "codex"

    # Default model for Codex
    DEFAULT_MODEL = "gpt-5.2-codex"

    # Valid reasoning effort levels
    VALID_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ):
        """Initialize Codex framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL for API. If not provided, uses UNIFIED_BASE_URL env var.
            reasoning_effort: Reasoning effort level ("low", "medium", "high").
                             Controls how much the model "thinks" before responding.
                             Only applicable to reasoning models like gpt-5.2-codex.
        """
        self.api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self.base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        self.reasoning_effort = reasoning_effort

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for Codex.

        For API mode, no credential files need to be mounted.
        The API key is passed via environment variable.

        Returns:
            List of -v arguments for docker run (empty for API mode)
        """
        # API mode doesn't need file mounts - key is passed via env var
        return []

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps UNIFIED_* env vars to Codex-specific env vars:
        - CODEX_API_KEY: Required for `codex exec` non-interactive mode
        - OPENAI_BASE_URL: For custom API endpoints/proxies

        Returns:
            List of -e arguments for docker run
        """
        env_vars = []
        if self.api_key:
            # CODEX_API_KEY is required for `codex exec` mode
            env_vars.extend(["-e", f"CODEX_API_KEY={self.api_key}"])
        if self.base_url:
            env_vars.extend(["-e", f"OPENAI_BASE_URL={self.base_url}"])
        return env_vars

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for Codex setup.

        The script:
        1. Installs Codex CLI via npm (if not present)
        2. Verifies installation

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        return """
# === Codex: Install Codex CLI ===
try:
    import subprocess
    import shutil

    def run_cmd(cmd, shell=False):
        try:
            result = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, '', str(e)

    # Check current Node.js version
    success, node_version, _ = run_cmd(['node', '--version'])
    if success:
        print(f"Current Node.js version: {node_version}")
        try:
            major = int(node_version.lstrip('v').split('.')[0])
            need_upgrade = major < 20
        except:
            need_upgrade = True
    else:
        print("Node.js not found")
        need_upgrade = True

    # Install Node.js 20 if needed
    if need_upgrade:
        print("Installing Node.js 20 via NodeSource...")

        # Ensure curl is available
        if not shutil.which('curl'):
            print("Installing curl...")
            run_cmd(['apt-get', 'update'])
            run_cmd(['apt-get', 'install', '-y', 'curl', 'ca-certificates'])

        # Add NodeSource repository and install Node.js 20
        success, stdout, stderr = run_cmd(
            'curl -fsSL https://deb.nodesource.com/setup_20.x | bash -',
            shell=True
        )
        if not success:
            print(f"Warning: NodeSource setup output: {stderr}")

        success, stdout, stderr = run_cmd(['apt-get', 'install', '-y', 'nodejs'])
        if success:
            success, node_version, _ = run_cmd(['node', '--version'])
            print(f"Node.js installed: {node_version}")
        else:
            print(f"Failed to install Node.js: {stderr}")
            raise Exception("Node.js installation failed")

    # Check if codex is already installed
    result = subprocess.run(['which', 'codex'], capture_output=True, text=True)
    if result.returncode == 0:
        print(f"Codex already installed at: {result.stdout.strip()}")
    else:
        print("Installing Codex CLI via npm...")

        # Install codex globally
        install_result = subprocess.run(
            ['npm', 'i', '-g', '@openai/codex'],
            capture_output=True,
            text=True
        )
        if install_result.returncode == 0:
            print("Codex CLI installed successfully")
        else:
            print(f"Failed to install Codex: {install_result.stderr}")

    # Verify installation
    version_result = subprocess.run(['codex', '--version'], capture_output=True, text=True)
    if version_result.returncode == 0:
        print(f"Codex version: {version_result.stdout.strip()}")
    else:
        print("Warning: Could not verify Codex installation")

except Exception as e:
    print(f"Error setting up Codex: {e}")
"""

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the Codex CLI command for running the agent.

        Uses `codex exec` for non-interactive execution.
        Note: Codex generates its own thread_id, the session_id param is ignored.
        The actual thread_id must be extracted from stdout JSON output.

        Args:
            model: Model identifier (e.g., "gpt-5.2-codex")
            session_id: Ignored - Codex generates its own thread_id
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        actual_model = model if model else self.DEFAULT_MODEL

        cmd_parts = [
            "codex",
            "exec",
            "--model",
            actual_model,
            "--json",  # JSON output for parsing (includes thread_id)
            "--dangerously-bypass-approvals-and-sandbox",  # Bypass all restrictions
        ]

        # Add reasoning effort if specified
        if self.reasoning_effort and self.reasoning_effort in self.VALID_REASONING_EFFORTS:
            cmd_parts.extend(["-c", f'reasoning_effort="{self.reasoning_effort}"'])

        # Add prompt from file
        cmd_parts.append(f'"$(cat {prompt_path})"')

        return " ".join(cmd_parts)

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the Codex CLI command for resuming a session.

        Uses `codex exec resume <thread_id>` for session resumption.
        The session_id must be the thread_id from a previous Codex run.

        Args:
            model: Model identifier
            session_id: Codex thread_id from previous run (from stdout JSON)
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        actual_model = model if model else self.DEFAULT_MODEL

        cmd_parts = [
            "codex",
            "exec",
            "resume",
            session_id,  # This must be the thread_id from Codex output
            "--model",
            actual_model,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",  # Bypass all restrictions
        ]

        # Add reasoning effort if specified
        if self.reasoning_effort and self.reasoning_effort in self.VALID_REASONING_EFFORTS:
            cmd_parts.extend(["-c", f'reasoning_effort="{self.reasoning_effort}"'])

        # Add message from file
        cmd_parts.append(f'"$(cat {message_path})"')

        return " ".join(cmd_parts)

    @staticmethod
    def extract_thread_id(stdout_content: str) -> Optional[str]:
        """Extract thread_id from Codex JSON output.

        Codex outputs: {"type":"thread.started","thread_id":"xxx"}

        Args:
            stdout_content: Content of agent_stdout.txt

        Returns:
            thread_id if found, None otherwise
        """
        import json

        for line in stdout_content.strip().split("\n"):
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started":
                    thread_id = event.get("thread_id")
                    if thread_id:
                        return thread_id
            except json.JSONDecodeError:
                continue

        return None
