"""OpenHands CLI agent log parser implementation."""

import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.e2e.log_parser.base import AgentLogParser, register_parser
from harness.e2e.log_parser.models import ToolCallRecord, TrialStats

logger = logging.getLogger(__name__)


@register_parser("openhands")
class OpenHandsLogParser(AgentLogParser):
    """Parser for OpenHands CLI logs.

    OpenHands CLI outputs JSONL when run with --json flag.
    Detailed logs are stored in ~/.openhands/conversations/ directory.

    JSONL output format (expected):
    ```jsonl
    {"type": "action", "action": "run", "args": {"command": "ls -la"}, ...}
    {"type": "observation", "content": "...", ...}
    {"type": "action", "action": "write", "args": {"path": "...", "content": "..."}, ...}
    ```
    """

    FRAMEWORK_NAME = "openhands"

    # OpenHands home directory in container
    OPENHANDS_HOME = "/home/fakeroot/.openhands"
    CONVERSATIONS_DIR = f"{OPENHANDS_HOME}/conversations"

    # Token pricing per 1M tokens (varies by model via LiteLLM proxy)
    # These are estimates - actual costs depend on the underlying model
    TOKEN_PRICING = {
        # LiteLLM proxy models (mapped to underlying model costs)
        "litellm_proxy/gemini-3-flash-preview": {"input": 0.075, "output": 0.30},
        "litellm_proxy/claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
        "litellm_proxy/claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
        "litellm_proxy/gpt-4o": {"input": 2.50, "output": 10.0},
        "litellm_proxy/gpt-4o-mini": {"input": 0.15, "output": 0.60},
        # Direct model names
        "gemini-3-flash-preview": {"input": 0.075, "output": 0.30},
        "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
        "gpt-4o": {"input": 2.50, "output": 10.0},
    }

    # Action types that map to tool calls
    ACTION_TYPES = {
        "run": "Bash",
        "run_ipython": "IPython",
        "write": "Write",
        "read": "Read",
        "browse": "Browser",
        "browse_interactive": "BrowserInteractive",
        "message": "Message",
        "finish": "Finish",
        "delegate": "Delegate",
        "think": "Think",
    }

    # Action kinds that indicate sub-agent/micro-agent delegation
    # See: https://docs.openhands.dev/sdk/guides/agent-delegation
    DELEGATE_ACTION_KINDS = {
        "AgentDelegateAction",  # Main delegation action type
        "DelegateAction",  # Alternative name
        "MicroAgentAction",  # Micro agent invocation
    }

    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract OpenHands execution trace.

        OpenHands doesn't have a dedicated trace extraction tool.
        We copy the raw logs and rely on JSONL output captured during execution.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store trace files

        Returns:
            True if successful (always returns True as we use stdout logs)
        """
        logger.info("OpenHands trace extraction: using stdout JSONL logs and conversation files")
        # OpenHands traces are captured via --json output during execution
        # Additional extraction from ~/.openhands/conversations/ for detailed logs
        return True

    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract OpenHands logs from container.

        Copies the ~/.openhands/conversations/ contents to output directory.
        Structure: ~/.openhands/conversations/<conversation_id>/

        Args:
            container_name: Docker container name
            output_dir: Directory to store extracted logs (typically log/)
            session_id: Optional session_id (conversation_id) to filter

        Returns:
            Path to extracted logs directory
        """
        # Store in {output_dir}/openhands/ (agent name as directory)
        logs_dir = output_dir / "openhands"
        logs_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Check if conversations directory exists
            check_result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "test",
                    "-d",
                    self.CONVERSATIONS_DIR,
                ],
                capture_output=True,
                timeout=10,
            )

            if check_result.returncode != 0:
                logger.warning(f"OpenHands conversations directory not found: {self.CONVERSATIONS_DIR}")
                return logs_dir

            # If session_id is provided, only copy that conversation
            if session_id:
                # OpenHands stores conversations without hyphens in the ID
                # Try both formats: with hyphens and without
                session_id_no_hyphen = session_id.replace("-", "")
                session_ids_to_try = [session_id, session_id_no_hyphen]

                found = False
                for sid in session_ids_to_try:
                    conversation_dir = f"{self.CONVERSATIONS_DIR}/{sid}"
                    check_session = subprocess.run(
                        ["docker", "exec", container_name, "test", "-d", conversation_dir],
                        capture_output=True,
                        timeout=10,
                    )

                    if check_session.returncode == 0:
                        local_session_dir = logs_dir / sid
                        local_session_dir.mkdir(parents=True, exist_ok=True)

                        cp_result = subprocess.run(
                            ["docker", "cp", f"{container_name}:{conversation_dir}/.", str(local_session_dir)],
                            capture_output=True,
                            timeout=120,
                        )
                        if cp_result.returncode == 0:
                            logger.info(f"Extracted OpenHands conversation {sid} to {local_session_dir}")
                            found = True
                            break
                        else:
                            logger.warning(f"Failed to copy conversation {sid}: {cp_result.stderr.decode()}")

                if not found:
                    logger.warning(f"Conversation {session_id} not found (tried with/without hyphens)")

            else:
                # Copy all conversations
                # First, list conversation directories
                find_result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container_name,
                        "find",
                        self.CONVERSATIONS_DIR,
                        "-mindepth",
                        "1",
                        "-maxdepth",
                        "1",
                        "-type",
                        "d",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )

                if find_result.returncode == 0 and find_result.stdout.strip():
                    conversation_dirs = find_result.stdout.strip().split("\n")
                    copied_count = 0

                    for conv_dir in conversation_dirs:
                        if not conv_dir:
                            continue

                        conv_id = conv_dir.split("/")[-1]
                        local_conv_dir = logs_dir / conv_id
                        local_conv_dir.mkdir(parents=True, exist_ok=True)

                        cp_result = subprocess.run(
                            ["docker", "cp", f"{container_name}:{conv_dir}/.", str(local_conv_dir)],
                            capture_output=True,
                            timeout=120,
                        )
                        if cp_result.returncode == 0:
                            copied_count += 1
                            logger.debug(f"Copied conversation: {conv_id}")

                    logger.info(f"Extracted {copied_count} OpenHands conversations to {logs_dir}")
                else:
                    # Fallback: copy entire conversations directory
                    subprocess.run(
                        ["docker", "cp", f"{container_name}:{self.CONVERSATIONS_DIR}/.", str(logs_dir)],
                        capture_output=True,
                        timeout=120,
                    )
                    logger.info(f"Extracted OpenHands conversations directory to {logs_dir}")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout extracting OpenHands logs")
        except Exception as e:
            logger.warning(f"Error extracting OpenHands logs: {e}")

        return logs_dir

    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from OpenHands logs.

        Priority:
        1. Raw event files from conversations (event-*.json)
        2. Fallback to stdout JSON events

        Args:
            log_dir: Directory containing extracted logs (e.g., /tmp/test/openhands/)

        Returns:
            List of tool call records
        """
        all_calls = []

        # First, try to parse from raw event files
        # OpenHands stores events as: events/event-00000-<uuid>.json
        event_files = sorted(log_dir.rglob("event-*.json"))
        if event_files:
            for event_file in event_files:
                try:
                    event = json.loads(event_file.read_text(encoding="utf-8"))
                    extracted = self._extract_tool_call_from_event(event, len(all_calls))
                    if extracted:
                        all_calls.append(extracted)
                except Exception as e:
                    logger.debug(f"Error parsing event file {event_file}: {e}")

            if all_calls:
                logger.info(f"Parsed {len(all_calls)} tool calls from raw event files")

        # Fallback to stdout if no raw events found
        if not all_calls:
            stdout_file = log_dir.parent / "agent_stdout.txt"
            if stdout_file.exists():
                calls = self._parse_tool_calls_from_stdout(stdout_file)
                all_calls.extend(calls)
                logger.info(f"Parsed {len(calls)} tool calls from stdout (fallback)")

        # Sort by timestamp
        all_calls.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)

        logger.info(f"Total parsed tool calls: {len(all_calls)}")
        return all_calls

    def _parse_tool_calls_from_stdout(self, stdout_file: Path) -> List[ToolCallRecord]:
        """Parse tool calls from OpenHands stdout.

        OpenHands outputs formatted JSON events with --json flag, prefixed by
        `--JSON Event--` markers. The JSON is pretty-printed (multi-line).

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            List of ToolCallRecord objects
        """
        calls = []

        try:
            content = stdout_file.read_text(encoding="utf-8")
            if not content:
                return calls

            # Extract JSON events between --JSON Event-- markers
            json_events = self._extract_json_events(content)

            for event_index, event in enumerate(json_events):
                extracted = self._extract_tool_call_from_event(event, event_index)
                if extracted:
                    calls.append(extracted)

        except Exception as e:
            logger.warning(f"Error parsing tool calls from stdout: {e}")

        return calls

    def _extract_json_events(self, content: str) -> List[Dict[str, Any]]:
        """Extract JSON events from OpenHands formatted output.

        OpenHands outputs events in format:
        ```
        --JSON Event--
        [spinner output]
        {
          "kind": "ActionEvent",
          ...
        }
        ```

        The spinner output (ANSI codes, "Agent is working", etc.) is interleaved
        with the JSON output and needs to be cleaned.

        Args:
            content: Raw stdout content

        Returns:
            List of parsed JSON objects
        """
        import re

        events = []

        # First, clean up the entire content
        # Remove ANSI escape codes
        cleaned = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", content)
        # Remove spinner lines like "[2K⠸ Agent is working" or just "⠸ Agent is working"
        cleaned = re.sub(r"^\[2K.*$", "", cleaned, flags=re.MULTILINE)
        # Remove lines that are just spinner + "Agent is working"
        cleaned = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏].*Agent is working.*$", "", cleaned, flags=re.MULTILINE)
        # Remove standalone spinner characters at start of lines
        cleaned = re.sub(r"^[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*", "", cleaned, flags=re.MULTILINE)

        # Split by the JSON Event marker
        parts = cleaned.split("--JSON Event--")

        for part in parts[1:]:  # Skip content before first marker
            part = part.strip()
            if not part:
                continue

            # Find JSON object in this part
            json_start = part.find("{")
            if json_start == -1:
                continue

            # Extract JSON by finding matching braces
            json_text = part[json_start:]

            # Fix malformed JSON: OpenHands outputs multi-line strings with literal
            # newlines that need to be escaped for valid JSON
            json_text = self._fix_json_newlines(json_text)

            # Use a JSON decoder to parse
            decoder = json.JSONDecoder()
            try:
                obj, end_idx = decoder.raw_decode(json_text)
                events.append(obj)
            except json.JSONDecodeError as e:
                # If parsing fails, try to extract just the JSON portion
                # by finding the next --JSON Event-- marker or end of text
                logger.debug(f"JSON parse error at position {e.pos}: {e.msg}")
                continue

        logger.debug(f"Extracted {len(events)} JSON events from stdout")
        return events

    def _fix_json_newlines(self, json_text: str) -> str:
        """Fix malformed JSON with unescaped newlines in string values.

        OpenHands CLI outputs JSON with literal newlines inside strings,
        which is invalid JSON. This method escapes those newlines.

        Args:
            json_text: Raw JSON text with potential unescaped newlines

        Returns:
            Fixed JSON text with properly escaped newlines
        """
        result = []
        in_string = False
        escape_next = False
        i = 0

        while i < len(json_text):
            char = json_text[i]

            if escape_next:
                result.append(char)
                escape_next = False
                i += 1
                continue

            if char == "\\":
                result.append(char)
                escape_next = True
                i += 1
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                result.append(char)
                i += 1
                continue

            if in_string and char == "\n":
                # Replace literal newline with escaped newline
                result.append("\\n")
                i += 1
                continue

            if in_string and char == "\t":
                # Replace literal tab with escaped tab
                result.append("\\t")
                i += 1
                continue

            result.append(char)
            i += 1

        return "".join(result)

    def _parse_tool_calls_from_event_file(self, event_file: Path) -> List[ToolCallRecord]:
        """Parse tool calls from OpenHands event file.

        Args:
            event_file: Path to events.json or JSONL file

        Returns:
            List of ToolCallRecord objects
        """
        calls = []

        try:
            content = event_file.read_text(encoding="utf-8").strip()
            if not content:
                return calls

            # Try parsing as single JSON array first
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    for idx, event in enumerate(data):
                        extracted = self._extract_tool_call_from_event(event, idx)
                        if extracted:
                            calls.append(extracted)
                    return calls
            except json.JSONDecodeError:
                pass

            # Parse as JSONL
            for line_num, line in enumerate(content.split("\n"), 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    event = json.loads(line)
                    extracted = self._extract_tool_call_from_event(event, line_num)
                    if extracted:
                        calls.append(extracted)
                except json.JSONDecodeError:
                    continue

        except Exception as e:
            logger.warning(f"Error parsing event file {event_file}: {e}")

        return calls

    def _extract_tool_call_from_event(
        self,
        event: Dict[str, Any],
        event_index: int,
    ) -> Optional[ToolCallRecord]:
        """Extract a tool call record from an OpenHands event.

        OpenHands CLI events have structure:
        {
            "kind": "ActionEvent",
            "action": {
                "kind": "TerminalAction" | "FileEditorAction" | etc.,
                "command": "...",
                ...
            },
            "tool_name": "terminal",
            "timestamp": "...",
            ...
        }

        Args:
            event: Parsed JSON event
            event_index: Index for ID generation

        Returns:
            ToolCallRecord or None if not an action event
        """
        event_kind = event.get("kind", "")

        # Only process ActionEvent events
        if event_kind != "ActionEvent":
            return None

        # Get action details
        action = event.get("action", {})
        action_kind = action.get("kind", "unknown")

        # Map action kind to tool name
        # OpenHands action kinds: TerminalAction, FileEditorAction, etc.
        action_kind_map = {
            "TerminalAction": "Bash",
            "FileEditorAction": "Edit",
            "BrowserAction": "Browser",
            "MessageAction": "Message",
            "FinishAction": "Finish",
            "ThinkAction": "Think",
            # Delegation/micro-agent actions
            "AgentDelegateAction": "Delegate",
            "DelegateAction": "Delegate",
            "MicroAgentAction": "MicroAgent",
        }
        tool_name = event.get("tool_name", action_kind_map.get(action_kind, action_kind))

        # Generate tool call ID
        tool_id = event.get("id", f"{action_kind}_{event_index}")

        # Calculate input size from action
        input_size = len(json.dumps(action, ensure_ascii=False).encode("utf-8"))

        # Parse timestamp
        timestamp = self._parse_timestamp(event.get("timestamp"))

        # Check for success/error status
        success = True
        if event.get("error") or event.get("exception"):
            success = False

        # Check if this is a subagent/micro-agent/delegate action
        is_subagent = action_kind in self.DELEGATE_ACTION_KINDS

        return ToolCallRecord(
            id=str(tool_id),
            name=tool_name,
            timestamp=timestamp or datetime.now(),
            success=success,
            input_size=input_size,
            output_size=0,  # Will be updated by parse_tool_results
            milestone_id=None,
            is_subagent=is_subagent,
        )

    def _parse_timestamp(self, timestamp_val: Any) -> Optional[datetime]:
        """Parse timestamp from various formats.

        Args:
            timestamp_val: Timestamp value (string, int, float, or None)

        Returns:
            datetime or None
        """
        if timestamp_val is None:
            return None

        try:
            if isinstance(timestamp_val, (int, float)):
                return datetime.fromtimestamp(timestamp_val)
            else:
                # Handle ISO format
                ts_str = str(timestamp_val)
                # Remove timezone suffix
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                return ts.replace(tzinfo=None)
        except (ValueError, OSError):
            return None

    def parse_tool_results(
        self,
        log_dir: Path,
        tool_calls: List[ToolCallRecord],
    ) -> None:
        """Update tool calls with result information from observation events.

        OpenHands observation events contain results for action events.
        ObservationEvent has `action_id` field that links to ActionEvent `id`.

        Modifies tool_calls in place.

        Args:
            log_dir: Directory containing extracted logs
            tool_calls: List of tool call records to update
        """
        # Build lookup by action ID
        calls_by_id = {tc.id: tc for tc in tool_calls}

        # Parse observation events from raw event files
        event_files = sorted(log_dir.rglob("event-*.json"))

        for event_file in event_files:
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
                event_kind = event.get("kind", "")

                # Only process ObservationEvent
                if event_kind != "ObservationEvent":
                    continue

                # Find matching action by action_id
                action_id = event.get("action_id", "")
                if action_id not in calls_by_id:
                    continue

                tc = calls_by_id[action_id]
                observation = event.get("observation", {})

                # Check success status
                is_error = observation.get("is_error", False)
                tc.success = not is_error

                # Calculate output size from observation content
                content = observation.get("content", [])
                output_text = ""

                if isinstance(content, list):
                    # Content is array of {type, text} objects
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            output_text += item.get("text", "")
                elif isinstance(content, str):
                    output_text = content

                if output_text:
                    tc.output_size = len(output_text.encode("utf-8"))

                logger.debug(f"Updated tool call {action_id[:8]}: output_size={tc.output_size}")

            except Exception as e:
                logger.debug(f"Error parsing observation from {event_file}: {e}")

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost based on token usage.

        Args:
            model: Model name
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens

        Returns:
            Estimated cost in USD
        """
        # Default pricing if model not found
        pricing = self.TOKEN_PRICING.get(model, {"input": 0.075, "output": 0.30})
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        return input_cost + output_cost

    def parse_stdout_stats(self, stdout_file: Path, log_dir: Optional[Path] = None) -> Dict:
        """Parse statistics from raw logs or stdout.

        Priority:
        1. Raw event files from log_dir (if provided)
        2. Fallback to stdout JSON events

        Args:
            stdout_file: Path to agent_stdout.txt
            log_dir: Optional path to extracted logs directory (e.g., /tmp/test/openhands/)

        Returns:
            Dictionary with accumulated statistics including:
            - total_cost_usd, total_turns, modelUsage, session_count
            - duration_ms: total execution time (if available)
            - tool_calls: list of tool call info
            - tool_call_breakdown: {tool_name: count}
        """
        # Try to parse from raw logs first
        if log_dir and log_dir.exists():
            stats = self._parse_stats_from_raw_logs(log_dir)
            if stats and stats.get("total_turns", 0) > 0:
                logger.info(f"Parsed stats from raw logs: {stats.get('total_turns')} turns")
                return stats

        # Fallback to stdout parsing
        return self._parse_stats_from_stdout(stdout_file)

    def _parse_stats_from_raw_logs(self, log_dir: Path) -> Dict:
        """Parse statistics from raw event files and base_state.json.

        Args:
            log_dir: Path to extracted logs directory

        Returns:
            Dictionary with statistics
        """
        total_cost = 0.0
        total_turns = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 1  # We're parsing a single session's logs
        tool_calls: List[Dict[str, Any]] = []
        tool_call_breakdown: Dict[str, int] = defaultdict(int)
        first_timestamp = None
        last_timestamp = None

        # Find all event files
        event_files = sorted(log_dir.rglob("event-*.json"))
        if not event_files:
            return {}

        for event_file in event_files:
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
                event_kind = event.get("kind", "")

                # Track timestamps
                timestamp = self._parse_timestamp(event.get("timestamp"))
                if timestamp:
                    if first_timestamp is None:
                        first_timestamp = timestamp
                    last_timestamp = timestamp

                # Count ActionEvent for tool call breakdown (not turns - turns = LLM API calls)
                if event_kind == "ActionEvent":
                    action = event.get("action", {})
                    action_kind = action.get("kind", "") if isinstance(action, dict) else ""

                    action_kind_map = {
                        "TerminalAction": "terminal",
                        "FileEditorAction": "file_editor",
                        "BrowserAction": "browser",
                        "MessageAction": "message",
                        "FinishAction": "finish",
                        "ThinkAction": "think",
                    }
                    tool_name = event.get("tool_name", action_kind_map.get(action_kind, action_kind))

                    tool_call_breakdown[tool_name] += 1
                    tool_calls.append(
                        {
                            "name": tool_name,
                            "action": action_kind,
                            "id": event.get("id", ""),
                            "success": True,
                        }
                    )

            except Exception as e:
                logger.debug(f"Error parsing event file {event_file}: {e}")

        # Parse cost and token usage from base_state.json
        base_state_files = list(log_dir.rglob("base_state.json"))
        for base_state_file in base_state_files:
            try:
                base_state = json.loads(base_state_file.read_text(encoding="utf-8"))
                stats = base_state.get("stats", {})
                usage_to_metrics = stats.get("usage_to_metrics", {})

                for usage_id, metrics in usage_to_metrics.items():
                    # Get accumulated cost
                    accumulated_cost = metrics.get("accumulated_cost", 0.0)
                    total_cost += accumulated_cost

                    # Get token usage, strip litellm_proxy/ prefix from model name
                    model_name = metrics.get("model_name", "unknown")
                    if model_name.startswith("litellm_proxy/"):
                        model_name = model_name[len("litellm_proxy/") :]
                    token_usage = metrics.get("accumulated_token_usage", {})

                    # Count LLM API calls from costs array
                    api_calls = len(metrics.get("costs", []))
                    total_turns += api_calls

                    if token_usage:
                        model_usage[model_name]["inputTokens"] = token_usage.get("prompt_tokens", 0)
                        model_usage[model_name]["outputTokens"] = token_usage.get("completion_tokens", 0)
                        model_usage[model_name]["cacheReadTokens"] = token_usage.get("cache_read_tokens", 0)
                        model_usage[model_name]["reasoningTokens"] = token_usage.get("reasoning_tokens", 0)
                        model_usage[model_name]["costUSD"] = accumulated_cost
                        model_usage[model_name]["apiRequests"] = api_calls

                logger.info(f"Parsed base_state.json: cost=${total_cost:.4f}")

            except Exception as e:
                logger.debug(f"Error parsing base_state.json: {e}")

        # Calculate duration
        total_duration_ms = 0
        if first_timestamp and last_timestamp:
            total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

        logger.info(
            f"Parsed raw logs: {session_count} session, {total_turns} turns, "
            f"${total_cost:.4f}, {len(tool_calls)} tool calls, {total_duration_ms}ms"
        )

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": {k: dict(v) for k, v in model_usage.items()},
            "session_count": session_count,
            "duration_ms": total_duration_ms,
            "tool_calls": tool_calls,
            "tool_call_breakdown": dict(tool_call_breakdown),
        }

    def _parse_stats_from_stdout(self, stdout_file: Path) -> Dict:
        """Parse statistics from stdout (fallback method).

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            Dictionary with statistics
        """
        total_cost = 0.0
        total_turns = 0
        total_duration_ms = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 0
        tool_calls: List[Dict[str, Any]] = []
        tool_call_breakdown: Dict[str, int] = defaultdict(int)

        if not stdout_file.exists():
            logger.warning(f"stdout file not found: {stdout_file}")
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
                "duration_ms": 0,
                "tool_calls": [],
                "tool_call_breakdown": {},
            }

        content = stdout_file.read_text(encoding="utf-8")
        if not content.strip():
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
                "duration_ms": 0,
                "tool_calls": [],
                "tool_call_breakdown": {},
            }

        # Track session
        seen_conversation_ids = set()
        first_timestamp = None
        last_timestamp = None

        # Extract JSON events from formatted output
        json_events = self._extract_json_events(content)

        for event in json_events:

            # OpenHands events use "kind" field (e.g., "ActionEvent", "ObservationEvent")
            event_kind = event.get("kind", "")
            # Also support "type" field for alternative formats
            event_type = event.get("type", "")

            # Track conversation/session
            conv_id = event.get("conversation_id") or event.get("session_id")
            if conv_id and conv_id not in seen_conversation_ids:
                seen_conversation_ids.add(conv_id)
                session_count += 1

            # Track timestamps for duration calculation
            timestamp = self._parse_timestamp(event.get("timestamp"))
            if timestamp:
                if first_timestamp is None:
                    first_timestamp = timestamp
                last_timestamp = timestamp

            # Count ActionEvent for tool call breakdown (not turns - turns = LLM API calls)
            # OpenHands CLI uses "kind": "ActionEvent" with nested "action" object
            if event_kind == "ActionEvent" or event_type == "action":
                # Get action details from nested "action" object or directly
                action = event.get("action", {})
                action_kind = action.get("kind", "") if isinstance(action, dict) else event.get("action", "")

                # Map action kind to tool name
                action_kind_map = {
                    "TerminalAction": "Bash",
                    "FileEditorAction": "Edit",
                    "BrowserAction": "Browser",
                    "MessageAction": "Message",
                    "FinishAction": "Finish",
                    "ThinkAction": "Think",
                }
                # Use tool_name from event if available, otherwise map from action_kind
                tool_name = event.get("tool_name", action_kind_map.get(action_kind, action_kind))
                if not tool_name:
                    tool_name = self.ACTION_TYPES.get(action_kind, action_kind)

                tool_call_breakdown[tool_name] += 1
                tool_calls.append(
                    {
                        "name": tool_name,
                        "action": action_kind,
                        "success": True,  # Updated later if observation shows error
                    }
                )

            # Extract token usage from metrics events (if available)
            # Each metrics event represents one LLM API call
            if event_kind == "MetricsEvent" or event_type == "metrics" or "usage" in event:
                usage = event.get("usage", event)
                model = usage.get("model", "unknown")

                input_tokens = usage.get("input_tokens", usage.get("prompt_tokens", 0))
                output_tokens = usage.get("output_tokens", usage.get("completion_tokens", 0))

                if input_tokens or output_tokens:
                    model_usage[model]["inputTokens"] += input_tokens
                    model_usage[model]["outputTokens"] += output_tokens
                    model_usage[model]["apiRequests"] += 1
                    total_turns += 1  # Each metrics event = one LLM API call

                    # Calculate cost
                    session_cost = self._calculate_cost(model, input_tokens, output_tokens)
                    model_usage[model]["costUSD"] = model_usage[model].get("costUSD", 0.0) + session_cost
                    total_cost += session_cost

            # Extract cost directly if provided
            if "cost" in event:
                cost = event.get("cost", 0)
                if isinstance(cost, (int, float)):
                    total_cost += cost

        # Calculate duration from timestamps
        if first_timestamp and last_timestamp:
            total_duration_ms = int((last_timestamp - first_timestamp).total_seconds() * 1000)

        # Ensure at least 1 session if we have any data
        if session_count == 0 and (total_turns > 0 or tool_calls):
            session_count = 1

        # Convert defaultdicts to regular dicts
        model_usage_dict = {model: dict(usage) for model, usage in model_usage.items()}

        logger.info(
            f"Parsed stdout: {session_count} sessions, {total_turns} turns, "
            f"${total_cost:.4f}, {len(tool_calls)} tool calls"
        )

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": model_usage_dict,
            "session_count": session_count,
            "duration_ms": total_duration_ms,
            "tool_calls": tool_calls,
            "tool_call_breakdown": dict(tool_call_breakdown),
        }

    def compute_trial_stats(
        self,
        trial_name: str,
        model: str,
        tool_calls: List[ToolCallRecord],
        stdout_stats: Dict,
        milestone_times: Optional[Dict[str, Dict]] = None,
        reasoning_effort: Optional[str] = None,
        session_history_path: Optional[Path] = None,
    ) -> TrialStats:
        """Compute complete trial statistics for OpenHands.

        Args:
            trial_name: Name of the trial
            model: Model identifier
            tool_calls: List of parsed tool calls
            stdout_stats: Statistics from agent stdout (includes duration_ms)
            milestone_times: Optional milestone time boundaries
            reasoning_effort: Optional reasoning effort level

        Returns:
            Complete TrialStats object
        """
        # Use duration from stdout_stats
        duration_ms = stdout_stats.get("duration_ms", 0)

        # Calculate start/end time based on duration
        end_time = datetime.now()
        start_time = end_time - timedelta(milliseconds=duration_ms) if duration_ms > 0 else end_time

        # Compute tool call breakdown
        tool_call_breakdown = stdout_stats.get("tool_call_breakdown", {})
        if not tool_call_breakdown:
            tool_call_breakdown = {}
            for tc in tool_calls:
                tool_call_breakdown[tc.name] = tool_call_breakdown.get(tc.name, 0) + 1

        # Count subagent calls
        total_subagent_calls = sum(1 for tc in tool_calls if tc.is_subagent)

        # Strip litellm_proxy/ prefix from model name for display
        display_model = model
        if display_model.startswith("litellm_proxy/"):
            display_model = display_model[len("litellm_proxy/") :]

        # Get model usage and add reasoning_effort only for GPT models
        model_usage = stdout_stats.get("modelUsage", {})

        # Only include reasoning_effort for GPT models (gpt-4o, gpt-5, etc.)
        effective_reasoning_effort = None
        if reasoning_effort and "gpt" in display_model.lower():
            effective_reasoning_effort = reasoning_effort
            model_usage = self._add_reasoning_effort_to_model_usage(model_usage, reasoning_effort)

        # Assign milestones to tool calls and compute milestone stats
        if milestone_times:
            self._assign_milestones_to_tool_calls(tool_calls, milestone_times)
        milestone_stats = self._compute_milestone_stats(milestone_times or {}, tool_calls, stdout_stats)

        # Detect sessions for transparency (duration_ms already correct from API latency)
        sessions = self.detect_sessions_from_tool_calls(tool_calls)

        return TrialStats(
            trial_name=trial_name,
            agent_framework=self.FRAMEWORK_NAME,
            model=display_model,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            wall_clock_ms=duration_ms,  # For OpenHands, API latency is the best we have
            total_cost_usd=stdout_stats.get("total_cost_usd", 0.0),
            total_turns=stdout_stats.get("total_turns", 0),
            total_tool_calls=len(tool_calls),
            total_subagent_calls=total_subagent_calls,
            session_count=stdout_stats.get("session_count", 0),
            sessions=sessions,
            reasoning_effort=effective_reasoning_effort,
            model_usage=model_usage,
            tool_call_breakdown=tool_call_breakdown,
            milestone_stats=milestone_stats,
            all_tool_calls=tool_calls,
        )
