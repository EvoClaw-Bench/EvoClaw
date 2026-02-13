"""Claude Code agent log parser implementation."""

import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.e2e.log_parser.base import AgentLogParser, register_parser
from harness.e2e.log_parser.models import ToolCallRecord

logger = logging.getLogger(__name__)


@register_parser("claude-code")
class ClaudeCodeLogParser(AgentLogParser):
    """Parser for Claude Code JSONL logs."""

    FRAMEWORK_NAME = "claude-code"

    # Claude Code home directory in container
    CLAUDE_HOME = "/home/fakeroot/.claude"
    PROJECTS_DIR = f"{CLAUDE_HOME}/projects"

    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract Claude Code logs from container.

        Copies the ~/.claude/projects/-testbed/ contents directly to output.

        Args:
            container_name: Docker container name
            output_dir: Directory to store extracted logs (typically log/)
            session_id: Optional session ID (not used for Claude Code, extracts all)

        Returns:
            Path to extracted logs directory
        """
        # Store in {output_dir}/claude_code/ (agent name as directory)
        logs_dir = output_dir / "claude_code"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Copy contents of -testbed/ directly (flatten directory structure)
        # Container path: ~/.claude/projects/-testbed/{session}.jsonl
        testbed_path = f"{self.PROJECTS_DIR}/-testbed"

        try:
            result = subprocess.run(
                ["docker", "cp", f"{container_name}:{testbed_path}/.", str(logs_dir)],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minute timeout
            )

            if result.returncode != 0:
                logger.warning(f"Failed to extract Claude logs: {result.stderr}")
            else:
                logger.info(f"Extracted Claude logs to {logs_dir}")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout extracting Claude logs")
        except Exception as e:
            logger.warning(f"Error extracting Claude logs: {e}")

        return logs_dir

    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from Claude Code JSONL logs.

        Args:
            log_dir: Directory containing extracted JSONL logs

        Returns:
            List of tool call records sorted by timestamp
        """
        all_calls = []

        # Find all JSONL files recursively
        jsonl_files = list(log_dir.rglob("*.jsonl"))

        for jsonl_file in jsonl_files:
            is_subagent = jsonl_file.name.startswith("agent-")
            try:
                calls = self._parse_jsonl(jsonl_file, is_subagent=is_subagent)
                all_calls.extend(calls)
            except Exception as e:
                logger.warning(f"Error parsing {jsonl_file}: {e}")

        # Sort by timestamp
        all_calls.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)

        logger.info(f"Parsed {len(all_calls)} tool calls from {len(jsonl_files)} JSONL files")
        return all_calls

    def _parse_jsonl(self, jsonl_path: Path, is_subagent: bool = False) -> List[ToolCallRecord]:
        """Parse a single JSONL file for tool calls.

        Args:
            jsonl_path: Path to JSONL file
            is_subagent: Whether this is a subagent log file

        Returns:
            List of tool call records
        """
        calls = []

        with open(jsonl_path, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    logger.debug(f"Invalid JSON at {jsonl_path}:{line_num}: {e}")
                    continue

                # Extract tool calls from the record
                tool_calls = self._extract_tool_calls_from_record(record, is_subagent)
                calls.extend(tool_calls)

        return calls

    def _extract_tool_calls_from_record(
        self,
        record: Dict[str, Any],
        is_subagent: bool,
    ) -> List[ToolCallRecord]:
        """Extract tool call records from a JSONL record.

        Claude Code JSONL format has various record types:
        - "assistant" type with "message" containing "content" array with "tool_use" blocks
        - "tool_result" type with results for tool calls

        Args:
            record: Parsed JSONL record
            is_subagent: Whether from subagent

        Returns:
            List of tool call records
        """
        calls = []

        record_type = record.get("type")

        # Handle assistant messages with tool use
        if record_type == "assistant":
            message = record.get("message", {})
            content = message.get("content", [])

            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    call = self._create_tool_call_from_tool_use(
                        item,
                        record,
                        is_subagent,
                    )
                    if call:
                        calls.append(call)

        # Handle tool_result records to update success status
        # (The tool call itself was already recorded from assistant message)

        return calls

    def _create_tool_call_from_tool_use(
        self,
        tool_use: Dict[str, Any],
        record: Dict[str, Any],
        is_subagent: bool,
    ) -> Optional[ToolCallRecord]:
        """Create a ToolCallRecord from a tool_use content block.

        Args:
            tool_use: Tool use content block
            record: Parent JSONL record
            is_subagent: Whether from subagent

        Returns:
            ToolCallRecord or None if invalid
        """
        tool_id = tool_use.get("id", "")
        tool_name = tool_use.get("name", "unknown")
        tool_input = tool_use.get("input", {})

        # Calculate input size
        input_size = len(json.dumps(tool_input, ensure_ascii=False).encode("utf-8"))

        # Parse timestamp from record
        timestamp = None
        timestamp_str = record.get("timestamp")
        if timestamp_str:
            try:
                # Handle various ISO formats
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                # Convert to naive datetime for consistency
                timestamp = timestamp.replace(tzinfo=None)
            except ValueError:
                pass

        # Default success to True (would need tool_result to determine actual success)
        success = True

        return ToolCallRecord(
            id=tool_id,
            name=tool_name,
            timestamp=timestamp,
            success=success,
            input_size=input_size,
            output_size=0,  # Would need tool_result record
            milestone_id=None,  # Assigned later
            is_subagent=is_subagent,
        )

    def parse_stdout_stats(self, stdout_file: Path, logs_dir: Optional[Path] = None) -> Dict:
        """Parse agent_stdout.txt for accumulated statistics.

        The agent_stdout.txt file is in JSONL format with one JSON object
        per Claude Code session (resume creates new sessions).

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            Dictionary with accumulated statistics
        """
        total_cost = 0.0
        total_turns = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 0

        if not stdout_file.exists():
            logger.warning(f"stdout file not found: {stdout_file}")
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
            }

        with open(stdout_file, encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    # Not a JSON line, skip
                    continue

                # Check if this looks like a Claude Code result
                if "total_cost_usd" not in data and "num_turns" not in data:
                    continue

                session_count += 1
                total_cost += data.get("total_cost_usd", 0)
                total_turns += data.get("num_turns", 0)

                # Accumulate model usage
                for model, usage in data.get("modelUsage", {}).items():
                    if not isinstance(usage, dict):
                        continue
                    for key, val in usage.items():
                        if isinstance(val, (int, float)):
                            model_usage[model][key] += val

        # Convert defaultdicts to regular dicts
        model_usage_dict = {model: dict(usage) for model, usage in model_usage.items()}

        logger.info(f"Parsed stdout: {session_count} sessions, " f"{total_turns} turns, ${total_cost:.2f}")

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": model_usage_dict,
            "session_count": session_count,
        }

    def parse_tool_results(self, log_dir: Path, tool_calls: List[ToolCallRecord]) -> None:
        """Update tool calls with result information.

        Parses tool_result blocks from JSONL files and updates the
        corresponding tool call records with success status and output size.

        Claude Code JSONL format stores tool results in two ways:
        1. Nested inside "user" records in message.content[] array
        2. As top-level records with type "tool_result" (legacy format)

        Modifies tool_calls in place.

        Args:
            log_dir: Directory containing JSONL logs
            tool_calls: List of tool call records to update
        """
        # Build a lookup map by tool call ID
        calls_by_id = {tc.id: tc for tc in tool_calls}

        if not calls_by_id:
            return

        jsonl_files = list(log_dir.rglob("*.jsonl"))

        for jsonl_path in jsonl_files:
            try:
                with open(jsonl_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue

                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        record_type = record.get("type")

                        # Handle tool_result nested in user records (Claude Code format)
                        if record_type == "user":
                            message = record.get("message", {})
                            content = message.get("content", []) if isinstance(message, dict) else []
                            if isinstance(content, list):
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "tool_result":
                                        self._update_tool_call_from_result(item, calls_by_id)

                        # Handle top-level tool_result records (legacy format)
                        elif record_type == "tool_result":
                            self._update_tool_call_from_result(record, calls_by_id)

            except Exception as e:
                logger.debug(f"Error parsing tool results from {jsonl_path}: {e}")

    def _update_tool_call_from_result(
        self,
        result: Dict[str, Any],
        calls_by_id: Dict[str, ToolCallRecord],
    ) -> None:
        """Update a tool call record from a tool_result block.

        Args:
            result: Tool result data with tool_use_id, content, is_error
            calls_by_id: Mapping of tool call ID to ToolCallRecord
        """
        tool_use_id = result.get("tool_use_id")
        if not tool_use_id or tool_use_id not in calls_by_id:
            return

        tc = calls_by_id[tool_use_id]
        tc.success = not result.get("is_error", False)

        # Calculate output size
        content = result.get("content", "")
        if isinstance(content, str):
            tc.output_size = len(content.encode("utf-8"))
        elif isinstance(content, list):
            tc.output_size = len(json.dumps(content, ensure_ascii=False).encode("utf-8"))

    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract agent trace using claude-extract inside container.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to save trace files

        Returns:
            True if successful
        """
        logger.info("Extracting agent trace using claude-extract...")

        try:
            container_trace_dir = "/tmp/agent_trace"

            extract_cmd = [
                "docker",
                "exec",
                "--user",
                "fakeroot",
                "-e",
                "HOME=/home/fakeroot",
                container_name,
                "claude-extract",
                "--detailed",
                "--format",
                "markdown",
                "--output",
                container_trace_dir,
                "--recent",
                "1",
            ]

            result = subprocess.run(
                extract_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
                timeout=60,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to extract agent trace: {result.stderr}")
                return False

            # Copy to host
            output_dir.mkdir(parents=True, exist_ok=True)
            copy_cmd = [
                "docker",
                "cp",
                f"{container_name}:{container_trace_dir}/.",
                str(output_dir) + "/",
            ]

            copy_result = subprocess.run(
                copy_cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if copy_result.returncode != 0:
                logger.warning(f"Failed to copy trace files: {copy_result.stderr}")
                return False

            logger.info(f"Agent trace extracted to: {output_dir}")
            return True

        except subprocess.TimeoutExpired:
            logger.warning("Timeout extracting agent trace")
            return False
        except Exception as e:
            logger.warning(f"Failed to extract trace: {e}")
            return False
