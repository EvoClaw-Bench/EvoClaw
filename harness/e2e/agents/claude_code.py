"""Claude Code agent framework implementation."""

import logging
import os
from pathlib import Path
from typing import List, Optional

from harness.e2e.agents.base import AgentFramework, register_framework

logger = logging.getLogger(__name__)


@register_framework("claude-code")
class ClaudeCodeFramework(AgentFramework):
    """Agent framework implementation for Claude Code CLI.

    Supports two authentication modes:
    1. API mode: Uses UNIFIED_API_KEY and UNIFIED_BASE_URL environment variables
    2. File mode: Uses ~/.claude/.credentials.json file mount

    API mode takes precedence when UNIFIED_API_KEY is set.

    Environment variables:
        UNIFIED_API_KEY: API key (mapped to ANTHROPIC_API_KEY in container)
        UNIFIED_BASE_URL: Base URL (mapped to ANTHROPIC_BASE_URL in container)
    """

    FRAMEWORK_NAME = "claude-code"

    # Mapping from harness reasoning effort levels to Claude Code CLI --effort values.
    # Claude Code accepts: low, medium, high, max
    # Harness uses: low, medium, high, xhigh
    EFFORT_MAP = {
        "low": "low",
        "medium": "medium",
        "high": "high",
        "xhigh": "max",
    }

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        **kwargs,
    ):
        """Initialize Claude Code framework.

        Args:
            api_key: API key. If not provided, uses UNIFIED_API_KEY env var.
            base_url: Base URL. If not provided, uses UNIFIED_BASE_URL env var.
            reasoning_effort: Reasoning effort level ("low", "medium", "high", "xhigh").
                             Mapped to Claude Code CLI --effort flag.
                             "xhigh" is mapped to "max" for Claude Code.
            **kwargs: Additional arguments (ignored for compatibility).
        """
        self._api_key = api_key or os.environ.get("UNIFIED_API_KEY")
        self._base_url = base_url or os.environ.get("UNIFIED_BASE_URL")
        self._reasoning_effort = reasoning_effort or "high"

    def get_effective_reasoning_effort(self) -> Optional[str]:
        """Return effective reasoning effort (default: high)."""
        return self._reasoning_effort

    def _build_effort_args(self) -> List[str]:
        """Return Claude Code CLI args for reasoning effort.

        Maps harness reasoning effort levels to Claude Code --effort values.
        """
        if self._reasoning_effort and self._reasoning_effort in self.EFFORT_MAP:
            return ["--effort", self.EFFORT_MAP[self._reasoning_effort]]
        return []

    def get_container_env_vars(self) -> List[str]:
        """Return Docker environment variable arguments.

        Maps unified env vars to Claude-specific env vars:
        - UNIFIED_API_KEY -> ANTHROPIC_API_KEY
        - UNIFIED_BASE_URL -> ANTHROPIC_BASE_URL

        Returns:
            List of -e arguments for docker run
        """
        env_vars = []
        if self._api_key:
            env_vars.extend(["-e", f"ANTHROPIC_API_KEY={self._api_key}"])
        if self._base_url:
            env_vars.extend(["-e", f"ANTHROPIC_BASE_URL={self._base_url}"])
        return env_vars

    def get_container_mounts(self) -> List[str]:
        """Return Docker volume mount arguments for Claude credentials.

        When API key is provided via environment, credential file mounts are optional.

        Returns:
            List of -v arguments for docker run
        """
        mounts = []
        home = Path.home()

        # Claude credentials (optional when using API mode)
        claude_creds = home / ".claude/.credentials.json"
        if claude_creds.exists():
            mounts.extend(["-v", f"{claude_creds}:/tmp/host-claude-credentials/.credentials.json:ro"])
        elif not self._api_key:
            logger.warning("No API key and no credentials file found - authentication may fail")

        # Claude share directory (config files)
        claude_share = home / ".local/share/claude"
        if claude_share.exists():
            mounts.extend(["-v", f"{claude_share}:/tmp/host-claude-share:ro"])

        # Note: Claude binary is installed inside the container via the init script
        # using the standalone installer (no Node.js dependency).

        # extract_claude_logs.py for claude-extract tool
        extract_script = self._find_extract_script()
        if extract_script and extract_script.exists():
            mounts.extend(["-v", f"{extract_script}:/tmp/extract_claude_logs.py:ro"])
            logger.debug(f"Mounted extract_claude_logs.py from {extract_script}")
        else:
            logger.warning("extract_claude_logs.py not found - claude-extract will not be available")

        return mounts

    def _find_extract_script(self) -> Optional[Path]:
        """Find extract_claude_logs.py script."""
        # Try venv first
        venv_root = Path(__file__).parent.parent.parent.parent / ".venv"
        extract_script = venv_root / "lib" / "python3.11" / "site-packages" / "extract_claude_logs.py"
        if extract_script.exists():
            return extract_script

        # Try system packages
        try:
            import extract_claude_logs

            return Path(extract_claude_logs.__file__)
        except ImportError:
            return None

    def get_container_init_script(self, agent_name: str) -> str:
        """Return Python init script for Claude Code setup.

        The script:
        1. Sets up Claude directories and copies credentials
        2. Creates claude-extract wrapper

        Args:
            agent_name: Git user name for agent commits

        Returns:
            Python script as a string
        """
        return f'''
# === Claude Code: Install standalone binary ===
try:
    import subprocess
    import shutil

    def run_cmd(cmd, shell=False):
        try:
            result = subprocess.run(
                cmd, shell=shell, capture_output=True, text=True, timeout=300
            )
            return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
        except Exception as e:
            return False, '', str(e)

    # Check if claude is already installed and working
    success, version, _ = run_cmd(['claude', '--version'])
    if success:
        print(f"Claude Code already installed: {{version}}")
    else:
        print("Installing Claude Code standalone binary...")

        # Ensure curl is available
        if not shutil.which('curl'):
            print("Installing curl...")
            run_cmd(['apt-get', 'update'])
            run_cmd(['apt-get', 'install', '-y', 'curl', 'ca-certificates'])

        # Install via standalone installer (no Node.js required)
        success, stdout, stderr = run_cmd(
            'curl -fsSL https://claude.ai/install.sh | bash',
            shell=True
        )
        if success:
            import os

            # Resolve the actual binary path (installer creates symlink chains under /root/)
            claude_link = '/root/.local/bin/claude'
            claude_real = os.path.realpath(claude_link)
            print(f"Claude Code installed at: {{claude_real}}")

            # Copy the actual binary to /usr/local/bin/ so fakeroot user can access it
            # (fakeroot cannot traverse /root/ directory)
            shutil.copy2(claude_real, '/usr/local/bin/claude')
            os.chmod('/usr/local/bin/claude', 0o755)
            print("Copied claude binary to /usr/local/bin/claude")

            success, version, _ = run_cmd(['/usr/local/bin/claude', '--version'])
            print(f"Claude Code ready: {{version}}")
        else:
            print(f"Failed to install Claude Code: {{stderr}}")
            raise Exception("Claude Code installation failed")

except Exception as e:
    print(f"Error installing Claude Code: {{e}}")

# === Claude Code: Setup Claude directories ===
try:
    import os
    import pwd
    import shutil
    from pathlib import Path

    fake_user = pwd.getpwnam('fakeroot')
    uid, gid = fake_user.pw_uid, fake_user.pw_gid

    # Create Claude directories
    claude_dir = Path('/home/fakeroot/.claude')
    claude_debug = claude_dir / 'debug'
    claude_share = Path('/home/fakeroot/.local/share/claude')

    claude_debug.mkdir(parents=True, exist_ok=True)
    claude_share.mkdir(parents=True, exist_ok=True)

    # Copy credentials file
    cred_src = Path('/tmp/host-claude-credentials/.credentials.json')
    cred_dst = claude_dir / '.credentials.json'
    if cred_src.exists():
        shutil.copy2(cred_src, cred_dst)
        os.chmod(cred_dst, 0o600)
        os.chown(cred_dst, uid, gid)
        print(f"Copied credentials to {{cred_dst}}")

    # Copy config files from share directory
    share_src = Path('/tmp/host-claude-share')
    if share_src.exists() and share_src.is_dir():
        for item in share_src.iterdir():
            dst = claude_share / item.name
            if item.is_file():
                shutil.copy2(item, dst)
            elif item.is_dir():
                shutil.copytree(item, dst, dirs_exist_ok=True)
        print(f"Copied config files from {{share_src}}")

    # Set ownership for Claude directories
    for root, dirs, files in os.walk('/home/fakeroot/.claude'):
        os.chown(root, uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)
    for root, dirs, files in os.walk('/home/fakeroot/.local'):
        os.chown(root, uid, gid)
        for f in files:
            os.chown(os.path.join(root, f), uid, gid)

except Exception as e:
    print(f"Error setting up Claude directories: {{e}}")

# === Claude Code: Create claude-extract wrapper ===
try:
    extract_script = Path('/tmp/extract_claude_logs.py')
    if extract_script.exists():
        wrapper_content = """#!/usr/bin/env python3
import sys
import os

# Add the script directory to Python path
sys.path.insert(0, '/tmp')

# Import and run the extraction tool
from extract_claude_logs import launch_interactive
sys.exit(launch_interactive())
"""
        wrapper_path = Path('/usr/local/bin/claude-extract')
        with open(wrapper_path, 'w') as f:
            f.write(wrapper_content)
        os.chmod(wrapper_path, 0o755)
        print("Created claude-extract wrapper")
    else:
        print("extract_claude_logs.py not found, claude-extract will not be available")
except Exception as e:
    print(f"Error creating claude-extract wrapper: {{e}}")
'''

    def build_run_command(
        self,
        model: str,
        session_id: str,
        prompt_path: str,
    ) -> str:
        """Build the Claude CLI command for running the agent.

        Args:
            model: Model identifier
            session_id: Session ID for conversation tracking
            prompt_path: Path to prompt file inside container

        Returns:
            Shell command string
        """
        cmd_parts = [
            "claude",
            "--model",
            model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--session-id",
            session_id,
        ]

        cmd_parts.extend(self._build_effort_args())

        cmd_parts.extend(["<", prompt_path])

        return " ".join(cmd_parts)

    def build_resume_command(
        self,
        model: str,
        session_id: str,
        message_path: str,
    ) -> str:
        """Build the Claude CLI command for resuming a session.

        Args:
            model: Model identifier
            session_id: Session ID to resume
            message_path: Path to message file inside container

        Returns:
            Shell command string
        """
        cmd_parts = [
            "claude",
            "--model",
            model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--resume",
            session_id,
        ]

        cmd_parts.extend(self._build_effort_args())

        cmd_parts.extend(["<", message_path])

        return " ".join(cmd_parts)
