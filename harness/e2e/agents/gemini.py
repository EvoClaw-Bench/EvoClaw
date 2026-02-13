"""Google Gemini CLI agent framework implementation."""

import logging
import os
from typing import List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework

logger = logging.getLogger(__name__)


@register_framework("gemini-cli")
class GeminiFramework(AgentFramework):
    """Agent framework implementation for Google Gemini CLI.

    Gemini CLI is Google's coding agent that runs in the terminal.
    https://github.com/google-gemini/gemini-cli

    This implementation uses unified environment variables for API access:
    - UNIFIED_API_KEY -> GEMINI_API_KEY
    - UNIFIED_BASE_URL -> GOOGLE_GEMINI_BASE_URL

    Environment variables:
        UNIFIED_API_KEY: API key (mapped to GEMINI_API_KEY in container)
        UNIFIED_BASE_URL: Base URL (mapped to GOOGLE_GEMINI_BASE_URL in container)
    """

    FRAMEWORK_NAME = "gemini-cli"

    # Default model for Gemini
    DEFAULT_MODEL = "gemini-3-flash-preview"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        **kwargs,  # Accept and ignore extra params like reasoning_effort
    ):
        """Initialize Gemini framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL. If not provided, uses UNIFIED_BASE_URL env var.
            **kwargs: Additional arguments (ignored for compatibility).
        """
        self._api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self._base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        # Ignore unsupported kwargs like reasoning_effort

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for Gemini.

        For API mode, no credential files need to be mounted.
        The API key is passed via environment variable.

        Returns:
            List of -v arguments for docker run (empty for API mode)
        """
        # API mode doesn't need file mounts - key is passed via env var
        return []

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps unified env vars to Gemini-specific env vars:
        - UNIFIED_API_KEY -> GEMINI_API_KEY
        - UNIFIED_BASE_URL -> GOOGLE_GEMINI_BASE_URL

        Returns:
            List of -e arguments for docker run
        """
        env_vars = []
        if self._api_key:
            env_vars.extend(["-e", f"GEMINI_API_KEY={self._api_key}"])
        if self._base_url:
            env_vars.extend(["-e", f"GOOGLE_GEMINI_BASE_URL={self._base_url}"])
        return env_vars

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for Gemini CLI setup.

        The script:
        1. Installs Node.js 20+ (required for Gemini CLI 0.25.1+)
        2. Installs Gemini CLI via npm
        3. Verifies installation

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        return """
# === Gemini: Install Node.js 20+ and Gemini CLI ===
try:
    import subprocess
    import os
    import shutil

    def run_cmd(cmd, shell=False):
        '''Run command and return (success, stdout, stderr)'''
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

    # Check if gemini is already installed and working
    gemini_path = shutil.which('gemini')
    if gemini_path:
        success, version, _ = run_cmd(['gemini', '--version'])
        if success:
            print(f"Gemini CLI already installed: {version}")
        else:
            # Gemini exists but doesn't work (probably wrong Node version)
            print("Reinstalling Gemini CLI...")
            run_cmd(['npm', 'uninstall', '-g', '@google/gemini-cli'])
            gemini_path = None

    # Install Gemini CLI if needed
    if not gemini_path:
        print("Installing Gemini CLI via npm...")
        success, stdout, stderr = run_cmd(['npm', 'install', '-g', '@google/gemini-cli'])
        if success:
            print("Gemini CLI installed successfully")
        else:
            print(f"npm install output: {stderr}")

    # Verify final installation
    success, version, stderr = run_cmd(['gemini', '--version'])
    if success:
        print(f"Gemini CLI ready: {version}")
    else:
        print(f"Gemini verification failed: {stderr}")
        raise Exception("Gemini CLI installation failed")

except Exception as e:
    print(f"Error setting up Gemini: {e}")
    import traceback
    traceback.print_exc()
"""

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the Gemini CLI command for running the agent.

        Uses positional prompt for non-interactive execution.

        Args:
            model: Model identifier (e.g., "gemini-2.5-flash")
            session_id: Session ID for conversation tracking (not used by Gemini)
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        actual_model = model if model else self.DEFAULT_MODEL

        # Use positional prompt (--prompt is deprecated)
        cmd_parts = [
            "gemini",
            "--model",
            actual_model,
            "--output-format",
            "json",
            "--yolo",  # Auto-approve all operations (bypass sandbox)
            "--include-directories",
            "/e2e_workspace",
            f'"$(cat {prompt_path})"',  # Positional prompt at the end
        ]

        return " ".join(cmd_parts)

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the Gemini CLI command for resuming a session.

        Uses `gemini --resume <session_id>` for session resumption.

        Args:
            model: Model identifier
            session_id: Session ID to resume (use "latest" or index number)
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        actual_model = model if model else self.DEFAULT_MODEL

        cmd_parts = [
            "gemini",
            "--resume",
            session_id,
            "--model",
            actual_model,
            "--output-format",
            "json",
            "--yolo",
            "--include-directories",
            "/e2e_workspace",
            f'"$(cat {message_path})"',  # Positional prompt at the end
        ]

        return " ".join(cmd_parts)

    @staticmethod
    def extract_session_id(stdout_content: str) -> Optional[str]:
        """Extract session_id from Gemini JSON output.

        Gemini outputs session info in its JSON response (pretty-printed).

        Args:
            stdout_content: Content of agent_stdout.txt

        Returns:
            session_id if found, None otherwise
        """
        import json

        # Try parsing as a single JSON object first (pretty-printed)
        content = stdout_content.strip()
        try:
            event = json.loads(content)
            session_id = event.get("session_id") or event.get("sessionId")
            if session_id:
                return session_id
        except json.JSONDecodeError:
            pass

        # Try parsing concatenated JSON objects using raw_decode
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(content):
            # Skip whitespace
            while idx < len(content) and content[idx] in " \t\n\r":
                idx += 1
            if idx >= len(content):
                break

            try:
                obj, end_idx = decoder.raw_decode(content, idx)
                session_id = obj.get("session_id") or obj.get("sessionId")
                if session_id:
                    return session_id
                idx += end_idx
            except json.JSONDecodeError:
                # Find next potential JSON start
                next_brace = content.find("{", idx + 1)
                if next_brace == -1:
                    break
                idx = next_brace

        return None
