"""OpenAI Codex agent log parser implementation."""

import json
import logging
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from harness.e2e.log_parser.base import AgentLogParser, register_parser
from harness.e2e.log_parser.models import NativeUsageUnit, ToolCallRecord

logger = logging.getLogger(__name__)


@register_parser("codex")
class CodexLogParser(AgentLogParser):
    """Parser for OpenAI Codex logs.

    Codex outputs newline-delimited JSON events when run with --json flag.
    """

    FRAMEWORK_NAME = "codex"

    # Codex home directory in container
    CODEX_HOME = "/home/fakeroot/.codex"
    SESSIONS_DIR = f"{CODEX_HOME}/sessions"

    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract Codex execution trace.

        Codex doesn't have a dedicated trace extraction tool like claude-extract.
        We rely on the JSON output captured during execution.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store trace files

        Returns:
            True if successful (always returns True as we use stdout logs)
        """
        logger.info("Codex trace extraction: using stdout JSON logs")
        # Codex traces are captured via --json output during execution
        # No additional extraction needed
        return True

    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract Codex logs from container.

        Copies the ~/.codex/sessions/ contents to output directory.
        Session files are stored as: sessions/{year}/{month}/{day}/rollout-{ts}-{thread_id}.jsonl

        Args:
            container_name: Docker container name
            output_dir: Directory to store extracted logs (typically log/)
            session_id: Optional thread_id to filter - only extract matching session file

        Returns:
            Path to extracted logs directory
        """
        # Store in {output_dir}/codex/ (agent name as directory, consistent with claude_code)
        logs_dir = output_dir / "codex"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Copy session JSONL files (flatten the date directory structure)
        try:
            # First, find all JSONL files in sessions directory
            find_result = subprocess.run(
                ["docker", "exec", container_name, "find", self.SESSIONS_DIR, "-name", "*.jsonl", "-type", "f"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if find_result.returncode == 0 and find_result.stdout.strip():
                jsonl_files = find_result.stdout.strip().split("\n")
                copied_count = 0

                for remote_path in jsonl_files:
                    if not remote_path:
                        continue

                    # Extract just the filename
                    filename = Path(remote_path).name

                    # Filter by session_id (thread_id) if provided
                    # Filename format: rollout-{timestamp}-{thread_id}.jsonl
                    if session_id:
                        if session_id not in filename:
                            continue

                    local_path = logs_dir / filename

                    # Copy each file
                    subprocess.run(
                        ["docker", "cp", f"{container_name}:{remote_path}", str(local_path)],
                        capture_output=True,
                        timeout=30,
                    )
                    copied_count += 1

                logger.info(f"Extracted {copied_count} Codex session files to {logs_dir}")
            else:
                logger.warning("No Codex session files found")

        except subprocess.TimeoutExpired:
            logger.warning("Timeout extracting Codex logs")
        except Exception as e:
            logger.warning(f"Error extracting Codex logs: {e}")

        return logs_dir

    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from Codex JSON logs.

        Codex JSON events include tool/function calls in its output.

        Args:
            log_dir: Directory containing extracted logs

        Returns:
            List of tool call records sorted by timestamp
        """
        all_calls = []

        # Find all JSON/JSONL files
        json_files = list(log_dir.rglob("*.json")) + list(log_dir.rglob("*.jsonl"))

        for json_file in json_files:
            try:
                calls = self._parse_json_file(json_file)
                all_calls.extend(calls)
            except Exception as e:
                logger.warning(f"Error parsing {json_file}: {e}")

        # Sort by timestamp
        all_calls.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)

        logger.info(f"Parsed {len(all_calls)} tool calls from {len(json_files)} files")
        return all_calls

    def _parse_json_file(self, json_path: Path) -> List[ToolCallRecord]:
        """Parse a single JSON/JSONL file for tool calls.

        Args:
            json_path: Path to JSON file

        Returns:
            List of tool call records
        """
        calls = []

        with open(json_path, encoding="utf-8") as f:
            content = f.read().strip()

            # Try parsing as single JSON first
            try:
                data = json.loads(content)
                calls.extend(self._extract_tool_calls_from_event(data))
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
                    calls.extend(self._extract_tool_calls_from_event(event))
                except json.JSONDecodeError as e:
                    logger.debug(f"Invalid JSON at {json_path}:{line_num}: {e}")

        return calls

    def _extract_tool_calls_from_event(
        self,
        event: Dict[str, Any],
    ) -> List[ToolCallRecord]:
        """Extract tool call records from a Codex JSON event.

        Codex events may contain:
        - "type": "function_call" or "tool_use"
        - Function/tool information in the event
        - Nested in "payload" for response_item events

        Args:
            event: Parsed JSON event

        Returns:
            List of tool call records
        """
        calls = []

        event_type = event.get("type", "")

        # Handle response_item events (Codex wraps function calls in payload)
        if event_type == "response_item" and "payload" in event:
            payload = event["payload"]
            payload_type = payload.get("type", "")

            if payload_type in ("function_call", "custom_tool_call"):
                # Add timestamp from outer event if not in payload
                if "timestamp" not in payload and "timestamp" in event:
                    payload["timestamp"] = event["timestamp"]
                call = self._create_tool_call_record(payload)
                if call:
                    calls.append(call)

            elif payload_type in ("function_call_output", "custom_tool_call_output"):
                # This is a result event - handled by parse_tool_results
                pass

        # Handle direct function calls (top-level)
        elif event_type in ("function_call", "tool_use", "tool_call"):
            call = self._create_tool_call_record(event)
            if call:
                calls.append(call)

        # Handle nested tool calls in messages
        if "tool_calls" in event:
            for tc in event.get("tool_calls", []):
                call = self._create_tool_call_record(tc)
                if call:
                    calls.append(call)

        # Handle Codex-specific command events
        if event_type == "command" or (event_type not in ("response_item",) and "command" in event):
            call = self._create_command_record(event)
            if call:
                calls.append(call)

        return calls

    def _create_tool_call_record(
        self,
        data: Dict[str, Any],
    ) -> Optional[ToolCallRecord]:
        """Create a ToolCallRecord from tool call data.

        Args:
            data: Tool call data

        Returns:
            ToolCallRecord or None if invalid
        """
        tool_id = data.get("id", data.get("call_id", ""))
        tool_name = data.get("name", data.get("function", {}).get("name", "unknown"))
        tool_input = data.get("input", data.get("arguments", data.get("function", {}).get("arguments", {})))

        # Parse input if it's a string
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                tool_input = {"raw": tool_input}

        # Calculate input size
        input_size = len(json.dumps(tool_input, ensure_ascii=False).encode("utf-8"))

        # Parse timestamp
        timestamp = None
        timestamp_str = data.get("timestamp", data.get("created_at"))
        if timestamp_str:
            try:
                if isinstance(timestamp_str, (int, float)):
                    timestamp = datetime.fromtimestamp(timestamp_str)
                else:
                    timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                    timestamp = timestamp.replace(tzinfo=None)
            except (ValueError, OSError):
                pass

        # Extract raw command for Bash-like tool calls (used by verification classifier)
        bash_command = None
        if tool_name in ("shell_command", "exec_command") and isinstance(tool_input, dict):
            bash_command = tool_input.get("command") or tool_input.get("cmd")

        return ToolCallRecord(
            id=tool_id,
            name=tool_name,
            timestamp=timestamp,
            success=not data.get("is_error", False),
            input_size=input_size,
            output_size=0,
            milestone_id=None,
            is_subagent=False,
            _bash_command=bash_command,
        )

    def _create_command_record(
        self,
        event: Dict[str, Any],
    ) -> Optional[ToolCallRecord]:
        """Create a ToolCallRecord from a command execution event.

        Args:
            event: Command event data

        Returns:
            ToolCallRecord or None
        """
        command = event.get("command", "")
        if isinstance(command, dict):
            command = command.get("command", "")

        if not command:
            return None

        timestamp = None
        timestamp_str = event.get("timestamp")
        if timestamp_str:
            try:
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                timestamp = timestamp.replace(tzinfo=None)
            except ValueError:
                pass

        return ToolCallRecord(
            id=event.get("id", ""),
            name="shell_command",
            timestamp=timestamp,
            success=event.get("exit_code", 0) == 0,
            input_size=len(command.encode("utf-8")),
            output_size=len(event.get("output", "").encode("utf-8")),
            milestone_id=None,
            is_subagent=False,
            _bash_command=command if command else None,
        )

    # Token pricing per 1M tokens.
    # Verified against https://developers.openai.com/api/docs/models on 2026-04-08.
    #
    # Pricing entries can be either:
    # - flat: {"input": ..., "cached_input": ..., "output": ...}
    # - tiered: {"tiers": [{"threshold": ..., ...}, ...]}
    #
    # For GPT-5.4, prompts above 272K input tokens are billed at 2x input
    # and 1.5x output for the full session.
    # Reasoning/thought tokens are billed as output tokens.
    TOKEN_PRICING = {
        "gpt-5.4": {
            "tiers": [
                {"threshold": 272_000, "input": 2.50, "cached_input": 0.25, "output": 15.00},
                {"threshold": float("inf"), "input": 5.00, "cached_input": 0.50, "output": 22.50},
            ]
        },
        "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
        "gpt-5.2-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
        "gpt-5.2": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
        "gpt-5.2-pro": {"input": 21.00, "cached_input": 2.10, "output": 168.00},
        "gpt-4o": {"input": 2.50, "cached_input": 1.25, "output": 10.00},
        "gpt-4o-mini": {"input": 0.15, "cached_input": 0.075, "output": 0.60},
        "gpt-4-turbo": {"input": 10.00, "cached_input": 5.00, "output": 30.00},
        "gpt-4": {"input": 30.00, "cached_input": 15.00, "output": 60.00},
        "gpt-3.5-turbo": {"input": 0.50, "cached_input": 0.25, "output": 1.50},
    }

    @classmethod
    def _resolve_pricing(
        cls, model: str, input_tokens: int, context_window: Optional[int] = None
    ) -> Dict[str, float]:
        """Resolve flat or tiered pricing for a model.

        For tiered models, the threshold is evaluated against the per-request
        input token count.  When *context_window* is provided it is used as
        the upper-bound for per-request size (a single request can never
        exceed the context window).  This prevents session-level aggregates
        (which can be millions of tokens) from incorrectly triggering the
        higher pricing tier.
        """
        pricing = cls.TOKEN_PRICING.get(model)
        if pricing is None:
            logger.warning(
                f"Unknown model '{model}' for cost calculation, using default pricing "
                f"(input: $1.75/1M, cached: $0.175/1M, output: $14.00/1M)"
            )
            return {"input": 1.75, "cached_input": 0.175, "output": 14.00}

        tiers = pricing.get("tiers")
        if not tiers:
            return pricing

        # Use context_window as the tier selector when available, since
        # input_tokens may be a session-level aggregate rather than a
        # per-request count.
        tier_key = context_window if context_window is not None else input_tokens

        for tier in tiers:
            threshold = tier["threshold"]
            if threshold == float("inf") or tier_key <= int(threshold):
                return {
                    "input": float(tier["input"]),
                    "cached_input": float(tier["cached_input"]),
                    "output": float(tier["output"]),
                }

        last_tier = tiers[-1]
        return {
            "input": float(last_tier["input"]),
            "cached_input": float(last_tier["cached_input"]),
            "output": float(last_tier["output"]),
        }

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0,
        reasoning_tokens: int = 0,
        context_window: Optional[int] = None,
    ) -> float:
        """Calculate cost based on token usage.

        Cost formula:
            cost = input_cost + cached_cost + output_cost
        Where:
            - input_cost = (input_tokens - cached_tokens) / 1M × input_price
            - cached_cost = cached_tokens / 1M × cached_input_price
            - output_cost = (output_tokens + reasoning_tokens) / 1M × output_price

        Note: For Codex models, output_tokens from turn.completed already includes
        reasoning tokens, so reasoning_tokens should typically be 0 to avoid
        double-counting.

        Args:
            model: Model name
            input_tokens: Total input tokens (including cached)
            output_tokens: Output tokens (already includes reasoning tokens for Codex)
            cached_tokens: Cached input tokens (subset of input_tokens)
            reasoning_tokens: Additional reasoning/thought tokens (default 0,
                only use if output_tokens does NOT include reasoning)
            context_window: Model context window size. Used for tiered pricing
                to determine the correct tier (per-request size cannot exceed
                context window).

        Returns:
            Estimated cost in USD
        """
        pricing = self._resolve_pricing(model, input_tokens, context_window=context_window)

        # Non-cached input tokens
        non_cached_input = max(0, input_tokens - cached_tokens)
        input_cost = (non_cached_input / 1_000_000) * pricing["input"]

        # Cached input tokens (discounted rate)
        cached_cost = (cached_tokens / 1_000_000) * pricing["cached_input"]

        # Output tokens (including reasoning tokens)
        total_output = output_tokens + reasoning_tokens
        output_cost = (total_output / 1_000_000) * pricing["output"]

        return input_cost + cached_cost + output_cost

    def parse_native_usage_units(
        self,
        log_dir: Path,
        stdout_file: Path,
    ) -> List[NativeUsageUnit]:
        """Parse native turn-level usage units from Codex token_count events."""
        units: List[NativeUsageUnit] = []
        jsonl_files = sorted(log_dir.glob("*.jsonl"))
        if not jsonl_files:
            return units

        context_window: Optional[int] = None

        for jsonl_file in jsonl_files:
            current_model = "unknown"
            seen_total_usage_keys = set()

            try:
                with open(jsonl_file, encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type", "")
                        if event_type == "turn_context":
                            payload = event.get("payload", {})
                            if isinstance(payload, dict):
                                model = payload.get("model")
                                if isinstance(model, str) and model:
                                    current_model = model
                            continue

                        if event_type != "event_msg":
                            continue
                        payload = event.get("payload", {})
                        if not isinstance(payload, dict) or payload.get("type") != "token_count":
                            continue

                        info = payload.get("info", {})
                        if not isinstance(info, dict):
                            continue
                        if context_window is None:
                            cw = info.get("model_context_window")
                            if isinstance(cw, int) and cw > 0:
                                context_window = cw

                        last_usage = info.get("last_token_usage", {})
                        total_usage = info.get("total_token_usage", {})
                        if not isinstance(last_usage, dict) or not isinstance(total_usage, dict):
                            continue

                        total_key = (
                            int(total_usage.get("input_tokens", 0) or 0),
                            int(total_usage.get("cached_input_tokens", 0) or 0),
                            int(total_usage.get("output_tokens", 0) or 0),
                            int(total_usage.get("reasoning_output_tokens", 0) or 0),
                        )
                        # Codex may emit duplicate token_count snapshots; dedupe by
                        # cumulative totals within each rollout file.
                        if total_key in seen_total_usage_keys:
                            continue
                        seen_total_usage_keys.add(total_key)

                        input_tokens = int(last_usage.get("input_tokens", 0) or 0)
                        cached_tokens = int(last_usage.get("cached_input_tokens", 0) or 0)
                        output_tokens = int(last_usage.get("output_tokens", 0) or 0)
                        reasoning_tokens = int(last_usage.get("reasoning_output_tokens", 0) or 0)
                        if input_tokens <= 0 and cached_tokens <= 0 and output_tokens <= 0 and reasoning_tokens <= 0:
                            continue

                        timestamp = None
                        ts_str = event.get("timestamp")
                        if isinstance(ts_str, str) and ts_str:
                            try:
                                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                            except ValueError:
                                timestamp = None

                        cost = self._calculate_cost(
                            current_model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cached_tokens=cached_tokens,
                            reasoning_tokens=reasoning_tokens,
                            context_window=context_window,
                        )
                        units.append(
                            NativeUsageUnit(
                                id=f"{jsonl_file.name}:{line_num}",
                                source_type="turn",
                                timestamp=timestamp,
                                model=current_model,
                                token_usage={
                                    "inputTokens": input_tokens,
                                    "outputTokens": output_tokens + reasoning_tokens,
                                    "cacheReadInputTokens": cached_tokens,
                                    "reasoningOutputTokens": reasoning_tokens,
                                },
                                cost_usd=cost,
                            )
                        )
            except Exception as e:
                logger.debug(f"Error parsing native usage units from {jsonl_file}: {e}")

        logger.info(f"Parsed {len(units)} native usage units from Codex logs")
        return units

    def parse_stdout_stats(self, stdout_file: Path, logs_dir: Optional[Path] = None) -> Dict:
        """Parse agent_stdout.txt and JSONL files for accumulated statistics.

        Codex JSON output includes usage information in turn.completed events.
        JSONL files contain more detailed token_count events with context window info.

        Args:
            stdout_file: Path to agent_stdout.txt
            logs_dir: Optional path to logs directory containing JSONL files

        Returns:
            Dictionary with accumulated statistics
        """
        total_cost = 0.0
        total_turns = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 0
        unique_session_ids: set[str] = set()
        current_model = "unknown"
        context_window = None
        reasoning_tokens = 0
        # Track previous cumulative usage from turn.completed events.
        # Codex turn.completed reports thread-level cumulative totals that
        # increase monotonically across resume sessions.  We need deltas.
        prev_cumulative: Dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
        }

        if not stdout_file.exists():
            logger.warning(f"stdout file not found: {stdout_file}")
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
                "unique_session_count": 0,
            }

        # Determine log directory: prefer logs_dir if provided, otherwise use stdout_file.parent/codex
        # Note: logs_dir from extract_raw_logs already points to the codex/ subdirectory
        log_dir = logs_dir if logs_dir else stdout_file.parent / "codex"
        if log_dir.exists():
            for jsonl_file in sorted(log_dir.glob("*.jsonl")):
                try:
                    with open(jsonl_file, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                                event_type = event.get("type", "")

                                # Count turn_context events as LLM API calls (turns)
                                # Each turn_context represents one LLM inference call
                                if event_type == "turn_context":
                                    total_turns += 1
                                    model = event.get("payload", {}).get("model")
                                    if model:
                                        current_model = model

                                # Extract detailed token info from event_msg
                                elif event_type == "event_msg":
                                    payload = event.get("payload", {})
                                    if payload.get("type") == "token_count":
                                        info = payload.get("info")
                                        if info:
                                            # Get context window (same for all events)
                                            if context_window is None:
                                                context_window = info.get("model_context_window")

                                            # Get total usage from last token_count event
                                            total_usage = info.get("total_token_usage", {})
                                            if total_usage:
                                                reasoning_tokens = total_usage.get("reasoning_output_tokens", 0)

                            except json.JSONDecodeError:
                                continue
                except Exception as e:
                    logger.debug(f"Error parsing JSONL {jsonl_file}: {e}")

        # Parse stdout for session counts and final usage
        with open(stdout_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type", "")

                # Count sessions from thread.started
                if event_type == "thread.started":
                    session_count += 1
                    thread_id = data.get("thread_id")
                    if not thread_id and isinstance(data.get("thread"), dict):
                        thread_id = data["thread"].get("id")
                    if not thread_id:
                        thread_id = data.get("session_id")
                    if isinstance(thread_id, str) and thread_id:
                        unique_session_ids.add(thread_id)

                # Extract usage from turn.completed events
                # Note: total_turns is counted from turn_context events in JSONL files
                #
                # IMPORTANT: turn.completed reports thread-level CUMULATIVE token
                # totals.  When a trial resumes (same thread_id), each session's
                # turn.completed carries the running total so far.  We must
                # compute the delta vs the previous turn.completed to get the
                # per-session increment.
                if event_type == "turn.completed":
                    usage = data.get("usage", {})

                    if usage:
                        # Codex uses input_tokens/output_tokens (not prompt_tokens/completion_tokens)
                        cum_input = usage.get("input_tokens", usage.get("prompt_tokens", 0))
                        cum_output = usage.get("output_tokens", usage.get("completion_tokens", 0))
                        cum_cached = usage.get("cached_input_tokens", 0)

                        # Compute per-session delta
                        delta_input = cum_input - prev_cumulative["input_tokens"]
                        delta_output = cum_output - prev_cumulative["output_tokens"]
                        delta_cached = cum_cached - prev_cumulative["cached_tokens"]

                        # Update cumulative tracker
                        prev_cumulative["input_tokens"] = cum_input
                        prev_cumulative["output_tokens"] = cum_output
                        prev_cumulative["cached_tokens"] = cum_cached

                        model_usage[current_model]["inputTokens"] += delta_input
                        model_usage[current_model]["outputTokens"] += delta_output
                        model_usage[current_model]["cachedInputTokens"] += delta_cached

                        # Calculate cost per model using delta (with cached tokens)
                        # Note: output_tokens already includes reasoning tokens.
                        # Pass context_window so tiered pricing uses per-request
                        # size (not session-level delta which can be millions).
                        turn_cost = self._calculate_cost(
                            current_model,
                            delta_input,
                            delta_output,
                            cached_tokens=delta_cached,
                            context_window=context_window,
                        )
                        model_usage[current_model]["costUSD"] = (
                            model_usage[current_model].get("costUSD", 0.0) + turn_cost
                        )
                        total_cost += turn_cost

        # Add context window and reasoning tokens to model usage.
        # turn.completed output_tokens does NOT include reasoning tokens,
        # so we must add the reasoning cost separately.
        if current_model in model_usage:
            if context_window:
                model_usage[current_model]["contextWindow"] = context_window
            if reasoning_tokens > 0:
                model_usage[current_model]["reasoningOutputTokens"] = reasoning_tokens
                pricing = self._resolve_pricing(
                    current_model, 0, context_window=context_window
                )
                reasoning_cost = (reasoning_tokens / 1_000_000) * pricing["output"]
                model_usage[current_model]["costUSD"] = (
                    model_usage[current_model].get("costUSD", 0.0) + reasoning_cost
                )
                total_cost += reasoning_cost

        # Convert defaultdicts to regular dicts
        model_usage_dict = {model: dict(usage) for model, usage in model_usage.items()}

        unique_session_count = len(unique_session_ids) if unique_session_ids else session_count

        logger.info(
            f"Parsed stdout: {session_count} sessions, {unique_session_count} unique sessions, "
            f"{total_turns} turns, ${total_cost:.4f}"
        )

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": model_usage_dict,
            "session_count": session_count,
            "unique_session_count": unique_session_count,
        }

    def parse_tool_results(
        self,
        log_dir: Path,
        tool_calls: List[ToolCallRecord],
    ) -> None:
        """Update tool calls with result information from Codex logs.

        Parses result events from JSONL files and updates corresponding
        tool call records with success status and output size.

        Modifies tool_calls in place.

        Args:
            log_dir: Directory containing extracted logs
            tool_calls: List of tool call records to update
        """
        # Build lookup map by tool call ID
        calls_by_id = {tc.id: tc for tc in tool_calls if tc.id}

        if not calls_by_id:
            return

        # Find all JSON/JSONL files
        json_files = list(log_dir.rglob("*.json")) + list(log_dir.rglob("*.jsonl"))

        for json_file in json_files:
            try:
                with open(json_file, encoding="utf-8") as f:
                    content = f.read().strip()

                # Try parsing as single JSON first
                try:
                    data = json.loads(content)
                    self._update_tool_results_from_event(data, calls_by_id)
                    continue
                except json.JSONDecodeError:
                    pass

                # Parse as JSONL
                for line in content.split("\n"):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        event = json.loads(line)
                        self._update_tool_results_from_event(event, calls_by_id)
                    except json.JSONDecodeError:
                        continue

            except Exception as e:
                logger.debug(f"Error parsing tool results from {json_file}: {e}")

    def _update_tool_results_from_event(
        self,
        event: Dict[str, Any],
        calls_by_id: Dict[str, ToolCallRecord],
    ) -> None:
        """Update tool call records from a result event.

        Args:
            event: Parsed JSON event
            calls_by_id: Mapping of tool call ID to ToolCallRecord
        """
        event_type = event.get("type", "")

        # Handle response_item with function_call_output payload (Codex format)
        if event_type == "response_item" and "payload" in event:
            payload = event["payload"]
            payload_type = payload.get("type", "")

            if payload_type in ("function_call_output", "custom_tool_call_output"):
                call_id = payload.get("call_id", "")
                if call_id and call_id in calls_by_id:
                    tc = calls_by_id[call_id]
                    output = payload.get("output", "")

                    # Check for error based on output content
                    if payload_type == "custom_tool_call_output":
                        # custom_tool_call_output uses "status" field
                        tc.success = payload.get("status") != "error"
                    else:
                        tc.success = "Exit code: 0" in output or not output.startswith("Error")

                    if isinstance(output, str):
                        tc.output_size = len(output.encode("utf-8"))
                    elif isinstance(output, (dict, list)):
                        tc.output_size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))
                return

        # Handle function/tool results (direct format)
        if event_type in ("function_call_result", "tool_result", "tool_call_result"):
            call_id = event.get("call_id", event.get("tool_use_id", event.get("id", "")))
            if call_id and call_id in calls_by_id:
                tc = calls_by_id[call_id]
                tc.success = not event.get("is_error", False)

                # Calculate output size
                output = event.get("output", event.get("content", event.get("result", "")))
                if isinstance(output, str):
                    tc.output_size = len(output.encode("utf-8"))
                elif isinstance(output, (dict, list)):
                    tc.output_size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))

        # Handle nested tool_results in messages
        if "tool_results" in event:
            for result in event.get("tool_results", []):
                call_id = result.get("call_id", result.get("id", ""))
                if call_id and call_id in calls_by_id:
                    tc = calls_by_id[call_id]
                    tc.success = not result.get("is_error", False)

                    output = result.get("output", result.get("content", ""))
                    if isinstance(output, str):
                        tc.output_size = len(output.encode("utf-8"))
                    elif isinstance(output, (dict, list)):
                        tc.output_size = len(json.dumps(output, ensure_ascii=False).encode("utf-8"))
