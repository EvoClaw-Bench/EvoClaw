#!/usr/bin/env python3
"""
Claude CLI agent runner for applying test changes.

Uses the Claude CLI to execute prompts non-interactively.
"""

import json
import logging
import re
import subprocess
import uuid
from pathlib import Path
from typing import Tuple, Optional
from dataclasses import dataclass


@dataclass
class AgentResult:
    """Result from Claude agent execution."""

    success: bool
    output: str
    session_id: str
    duration_ms: int = 0
    exit_code: int = 0
    error: Optional[str] = None


class ClaudeAgentRunner:
    """Run Claude CLI agent for code modifications."""

    def __init__(
        self, workspace: Path, log_dir: Optional[Path] = None, timeout_ms: int = 600_000, model: str = "ultrathink"
    ):
        """
        Initialize agent runner.

        Args:
            workspace: Working directory for Claude (typically testbed path)
            log_dir: Directory to save logs (optional)
            timeout_ms: Timeout in milliseconds (default 10 minutes)
            model: Model to use (default "ultrathink")
        """
        self.workspace = Path(workspace)
        self.log_dir = Path(log_dir) if log_dir else None
        self.timeout_ms = timeout_ms
        self.model = model
        self.logger = logging.getLogger(__name__)

        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def run(self, prompt: str, milestone: str = "agent") -> AgentResult:
        """
        Run Claude CLI with the given prompt.

        Args:
            prompt: The prompt text to send to Claude
            milestone: Milestone ID for log file naming

        Returns:
            AgentResult with success status and output
        """
        import time

        start_time = time.time()

        session_id = str(uuid.uuid4())
        self.logger.info(f"Starting Claude session: {session_id[:8]}...")

        cmd = [
            "claude",
            "-p",
            self.model,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--session-id",
            session_id,
        ]

        try:
            # Run Claude
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.workspace),
                text=True,
            )

            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=self.timeout_ms / 1000.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
                duration_ms = int((time.time() - start_time) * 1000)

                # Save logs even on timeout
                self._save_logs(
                    milestone=milestone,
                    cmd=cmd,
                    session_id=session_id,
                    exit_code=-1,
                    duration_ms=duration_ms,
                    stdout=stdout or "",
                    stderr=stderr or "",
                    prompt=prompt,
                    status="timeout",
                )

                self.logger.error(f"Claude execution timed out after {self.timeout_ms}ms")
                return AgentResult(
                    success=False,
                    output="",
                    session_id=session_id,
                    duration_ms=duration_ms,
                    exit_code=-1,
                    error=f"Timeout after {self.timeout_ms}ms",
                )

            duration_ms = int((time.time() - start_time) * 1000)

            # Save structured log
            self._save_logs(
                milestone=milestone,
                cmd=cmd,
                session_id=session_id,
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                stdout=stdout or "",
                stderr=stderr or "",
                prompt=prompt,
                status="ok" if proc.returncode == 0 else "error",
            )

            # Extract conversation trace
            self._extract_conversation(session_id, milestone)

            if proc.returncode != 0:
                self.logger.error(f"Claude exited with code {proc.returncode}")
                self.logger.error(f"stderr: {stderr[:500] if stderr else 'empty'}")
                return AgentResult(
                    success=False,
                    output="",
                    session_id=session_id,
                    duration_ms=duration_ms,
                    exit_code=proc.returncode,
                    error=f"Exit code {proc.returncode}: {stderr[:200] if stderr else 'unknown error'}",
                )

            # Parse JSON output
            try:
                result = json.loads(stdout)
                output_text = result.get("result", "")
                return AgentResult(
                    success=True, output=output_text, session_id=session_id, duration_ms=duration_ms, exit_code=0
                )
            except json.JSONDecodeError:
                self.logger.warning("Failed to parse JSON output, using raw stdout")
                return AgentResult(
                    success=True, output=stdout, session_id=session_id, duration_ms=duration_ms, exit_code=0
                )

        except FileNotFoundError:
            duration_ms = int((time.time() - start_time) * 1000)
            self.logger.error("Claude CLI not found. Is it installed and in PATH?")
            return AgentResult(
                success=False,
                output="",
                session_id=session_id,
                duration_ms=duration_ms,
                exit_code=127,
                error="Claude CLI not found",
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self.logger.error(f"Failed to execute Claude: {e}")
            return AgentResult(
                success=False, output="", session_id=session_id, duration_ms=duration_ms, exit_code=1, error=str(e)
            )

    def _save_logs(
        self,
        milestone: str,
        cmd: list,
        session_id: str,
        exit_code: int,
        duration_ms: int,
        stdout: str,
        stderr: str,
        prompt: str,
        status: str,
    ) -> None:
        """Save structured agent log file."""
        if not self.log_dir:
            return

        log_content = f"""$ {' '.join(cmd)}
SESSION_ID={session_id}
STATUS={status}
EXIT={exit_code}
DURATION={duration_ms}ms

==== STDOUT ====
{stdout}

==== STDERR ====
{stderr}
"""

        log_file = self.log_dir / f"{milestone}.log"
        log_file.write_text(log_content, encoding="utf-8")
        self.logger.debug(f"Saved agent log to: {log_file}")

    def _extract_conversation(self, session_id: str, milestone: str) -> None:
        """Extract full conversation trace using claude-extract."""
        if not self.log_dir:
            return

        try:
            # Find session number by ID
            session_num = self._find_session_number(session_id)
            if session_num is None:
                self.logger.warning(f"Could not find session {session_id[:8]}... for extraction")
                return

            # Extract conversation to a temp file, then rename
            result = subprocess.run(
                [
                    "claude-extract",
                    "--extract",
                    str(session_num),
                    "--format",
                    "markdown",
                    "--detailed",
                    "--output",
                    str(self.log_dir),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                # claude-extract creates files with format: claude-conversation-{date}-{session_prefix}.md
                # Find the most recently created .md file and rename it
                md_files = list(self.log_dir.glob("claude-conversation-*.md"))
                if md_files:
                    # Get the newest file
                    newest = max(md_files, key=lambda f: f.stat().st_mtime)
                    target = self.log_dir / f"{milestone}_conversation.md"
                    newest.rename(target)
                    self.logger.debug(f"Saved conversation to: {target}")
                else:
                    self.logger.warning("claude-extract succeeded but no output file found")
            else:
                self.logger.warning(
                    f"claude-extract failed: {result.stderr[:200] if result.stderr else 'unknown error'}"
                )

        except FileNotFoundError:
            self.logger.warning("claude-extract not found. Install with: pip install claude-conversation-extractor")
        except subprocess.TimeoutExpired:
            self.logger.warning("claude-extract timed out")
        except Exception as e:
            self.logger.warning(f"Failed to extract conversation: {e}")

    def _find_session_number(self, session_id: str) -> Optional[int]:
        """Find session number by session ID from claude-extract --list."""
        try:
            result = subprocess.run(
                ["claude-extract", "--list", "--limit", "100"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return None

            # Parse output to find session ID
            # Format: "1. 📁 ..." followed by "   📄 Session: abc123..."
            lines = result.stdout.split("\n")
            current_num = None

            for line in lines:
                # Match session number line: "1. 📁 ..."
                num_match = re.match(r"^(\d+)\.\s", line)
                if num_match:
                    current_num = int(num_match.group(1))

                # Match session ID line: "   📄 Session: abc123..."
                if "Session:" in line and current_num is not None:
                    match = re.search(r"Session:\s*([a-f0-9-]+)", line)
                    if match:
                        found_id = match.group(1)
                        # Require exact 8-char prefix match to avoid false positives
                        # e.g., "abd01cc1" should not match "agent-ad"
                        if len(found_id) >= 8 and len(session_id) >= 8:
                            if found_id[:8] == session_id[:8]:
                                return current_num

            return None

        except Exception:
            return None


def run_test_apply_agent(
    testbed_path: Path,
    prompt: str,
    log_dir: Optional[Path] = None,
    milestone: str = "unknown",
    timeout_ms: int = 600_000,
) -> Tuple[bool, str]:
    """
    Convenience function to run the test apply agent.

    Args:
        testbed_path: Path to testbed git repository
        prompt: The generated prompt for applying test changes
        log_dir: Optional directory to save logs
        milestone: Milestone ID for logging
        timeout_ms: Timeout in milliseconds (default 10 minutes)

    Returns:
        Tuple of (success, output_or_error)
    """
    runner = ClaudeAgentRunner(workspace=testbed_path, log_dir=log_dir, timeout_ms=timeout_ms)

    result = runner.run(prompt, milestone=milestone)

    if result.success:
        return True, result.output
    else:
        return False, result.error or "Unknown error"


if __name__ == "__main__":
    # Simple test
    import argparse

    parser = argparse.ArgumentParser(description="Test Claude agent runner")
    parser.add_argument("--workspace", required=True, help="Workspace directory")
    parser.add_argument("--prompt", default='Hello, please respond with "OK"', help="Test prompt")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    runner = ClaudeAgentRunner(workspace=Path(args.workspace))
    result = runner.run(args.prompt, milestone="test")

    print(f"Success: {result.success}")
    print(f"Session: {result.session_id}")
    print(f"Duration: {result.duration_ms}ms")
    print(f"Output: {result.output[:500] if result.output else 'empty'}")
    if result.error:
        print(f"Error: {result.error}")
