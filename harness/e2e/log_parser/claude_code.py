"""Claude Code agent log parser implementation."""

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


@register_parser("claude-code")
class ClaudeCodeLogParser(AgentLogParser):
    """Parser for Claude Code JSONL logs."""

    FRAMEWORK_NAME = "claude-code"

    # Claude Code home directory in container
    CLAUDE_HOME = "/home/fakeroot/.claude"
    PROJECTS_DIR = f"{CLAUDE_HOME}/projects"

    # Anthropic-style token pricing (USD / 1M tokens).
    # Cache write multipliers:
    # - 5m write: 1.25x input
    # - 1h write: 2.00x input
    # Anthropic pricing as of 2026-03.
    # Opus 4.5/4.6: $5/$25;  Opus 4.0/4.1 (legacy): $15/$75
    # Sonnet 4.x:   $3/$15
    TOKEN_PRICING = {
        "claude-sonnet": {
            "input": 3.0,
            "output": 15.0,
            "cache_read": 0.3,
            "cache_write_5m": 3.75,
            "cache_write_1h": 6.0,
        },
        "claude-opus": {
            "input": 5.0,
            "output": 25.0,
            "cache_read": 0.5,
            "cache_write_5m": 6.25,
            "cache_write_1h": 10.0,
        },
        "claude-haiku": {
            "input": 1.0,
            "output": 5.0,
            "cache_read": 0.1,
            "cache_write_5m": 1.25,
            "cache_write_1h": 2.0,
        },
        # Z.AI GLM models (official z.ai pricing)
        "glm-5": {
            "input": 1.0,
            "output": 3.2,
            "cache_read": 0.2,
            "cache_write_5m": 1.0,
            "cache_write_1h": 1.0,
        },
        "glm-5.1": {
            "input": 1.4,
            "output": 4.4,
            "cache_read": 0.26,
            "cache_write_5m": 1.4,
            "cache_write_1h": 1.4,
        },
        "glm-4.7": {
            "input": 0.5,
            "output": 2.0,
            "cache_read": 0.1,
            "cache_write_5m": 0.5,
            "cache_write_1h": 0.5,
        },
        # glm-4.5-air: cheaper variant. open.bigmodel.cn silently aliases
        # claude-haiku-* model requests to glm-4.5-air, so we remap haiku
        # token usage to this pricing when re-evaluating bigmodel trials.
        "glm-4.5-air": {
            "input": 0.2,
            "output": 1.1,
            "cache_read": 0.03,
            "cache_write_5m": 0.2,
            "cache_write_1h": 0.2,
        },
    }

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

    def _resolve_pricing(self, model: str) -> Dict[str, float]:
        model_l = (model or "").lower()
        if "opus" in model_l:
            return self.TOKEN_PRICING["claude-opus"]
        if "haiku" in model_l:
            return self.TOKEN_PRICING.get("claude-haiku", self.TOKEN_PRICING["claude-sonnet"])
        # Z.AI GLM models
        if "glm-5.1" in model_l:
            return self.TOKEN_PRICING["glm-5.1"]
        if "glm-5" in model_l:
            return self.TOKEN_PRICING["glm-5"]
        if "glm-4.7" in model_l:
            return self.TOKEN_PRICING["glm-4.7"]
        if "glm-4.5-air" in model_l:
            return self.TOKEN_PRICING["glm-4.5-air"]
        return self.TOKEN_PRICING["claude-sonnet"]

    def _calculate_message_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_creation_5m_tokens: int,
        cache_creation_1h_tokens: int,
    ) -> float:
        pricing = self._resolve_pricing(model)
        input_cost = (input_tokens / 1_000_000) * pricing["input"]
        output_cost = (output_tokens / 1_000_000) * pricing["output"]
        cache_read_cost = (cache_read_tokens / 1_000_000) * pricing["cache_read"]
        cache_write_5m_cost = (cache_creation_5m_tokens / 1_000_000) * pricing["cache_write_5m"]
        cache_write_1h_cost = (cache_creation_1h_tokens / 1_000_000) * pricing["cache_write_1h"]
        return input_cost + output_cost + cache_read_cost + cache_write_5m_cost + cache_write_1h_cost

    def parse_native_usage_units(
        self,
        log_dir: Path,
        stdout_file: Path,
    ) -> List[NativeUsageUnit]:
        """Parse native message-level usage units from Claude JSONL logs."""
        units: List[NativeUsageUnit] = []
        by_message_key: Dict[str, Dict[str, Any]] = {}
        jsonl_files = list(log_dir.rglob("*.jsonl"))

        for jsonl_file in jsonl_files:
            # Skip non-trace metadata history.
            if jsonl_file.name == "session_history.jsonl":
                continue

            is_subagent = "subagents" in str(jsonl_file)
            try:
                with open(jsonl_file, encoding="utf-8") as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if record.get("type") != "assistant":
                            continue

                        message = record.get("message", {})
                        if not isinstance(message, dict):
                            continue
                        usage = message.get("usage", {})
                        if not isinstance(usage, dict):
                            continue

                        input_tokens = int(usage.get("input_tokens", 0) or 0)
                        output_tokens = int(usage.get("output_tokens", 0) or 0)
                        cache_read_tokens = int(usage.get("cache_read_input_tokens", 0) or 0)
                        cache_creation_total = int(usage.get("cache_creation_input_tokens", 0) or 0)
                        cache_creation = usage.get("cache_creation", {})
                        cache_creation_5m = 0
                        cache_creation_1h = 0
                        if isinstance(cache_creation, dict):
                            cache_creation_5m = int(cache_creation.get("ephemeral_5m_input_tokens", 0) or 0)
                            cache_creation_1h = int(cache_creation.get("ephemeral_1h_input_tokens", 0) or 0)
                        if cache_creation_5m + cache_creation_1h == 0 and cache_creation_total > 0:
                            cache_creation_5m = cache_creation_total

                        if (
                            input_tokens <= 0
                            and output_tokens <= 0
                            and cache_read_tokens <= 0
                            and cache_creation_total <= 0
                        ):
                            continue

                        model = str(message.get("model", "unknown"))
                        request_id = str(record.get("requestId", "") or "")
                        message_id = str(message.get("id", "") or "")

                        timestamp = None
                        ts_str = record.get("timestamp")
                        if isinstance(ts_str, str) and ts_str:
                            try:
                                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).replace(tzinfo=None)
                            except ValueError:
                                timestamp = None

                        usage_total = input_tokens + output_tokens + cache_read_tokens + cache_creation_total
                        cost = self._calculate_message_cost(
                            model=model,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            cache_creation_5m_tokens=cache_creation_5m,
                            cache_creation_1h_tokens=cache_creation_1h,
                        )

                        file_key = str(jsonl_file.relative_to(log_dir))
                        if request_id or message_id:
                            # Streaming can emit multiple assistant snapshots for the
                            # same message; keep the final/highest-usage snapshot only.
                            message_key = f"{file_key}|{request_id}|{message_id}"
                        else:
                            # No stable message identity available.
                            message_key = f"{file_key}|line:{line_num}"

                        new_record: Dict[str, Any] = {
                            "id": request_id or message_id or f"{jsonl_file.name}:{line_num}",
                            "source_type": "message",
                            "timestamp": timestamp,
                            "model": model,
                            "token_usage": {
                                "inputTokens": input_tokens,
                                "outputTokens": output_tokens,
                                "cacheReadInputTokens": cache_read_tokens,
                                "cacheCreationInputTokens": cache_creation_total,
                            },
                            "cost_usd": cost,
                            "is_subagent": is_subagent or bool(record.get("isSidechain")),
                            "_usage_total": usage_total,
                        }

                        existing = by_message_key.get(message_key)
                        if existing is None:
                            by_message_key[message_key] = new_record
                            continue

                        existing_usage_total = int(existing.get("_usage_total", 0) or 0)
                        existing_ts = existing.get("timestamp")
                        should_replace = usage_total > existing_usage_total
                        if not should_replace and usage_total == existing_usage_total:
                            if existing_ts is None and timestamp is not None:
                                should_replace = True
                            elif isinstance(existing_ts, datetime) and isinstance(timestamp, datetime):
                                should_replace = timestamp > existing_ts
                        if should_replace:
                            by_message_key[message_key] = new_record
            except Exception as e:
                logger.debug(f"Error parsing native usage units from {jsonl_file}: {e}")

        for record in by_message_key.values():
            units.append(
                NativeUsageUnit(
                    id=record["id"],
                    source_type=record["source_type"],
                    timestamp=record["timestamp"],
                    model=record["model"],
                    token_usage=record["token_usage"],
                    cost_usd=float(record["cost_usd"]),
                    is_subagent=bool(record["is_subagent"]),
                )
            )

        units.sort(key=lambda x: x.timestamp if x.timestamp else datetime.min)
        logger.info(f"Parsed {len(units)} native usage units from Claude logs")
        return units

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

        # Extract raw command for Bash tool calls (used by verification classifier)
        bash_command = None
        if tool_name == "Bash" and isinstance(tool_input, dict):
            bash_command = tool_input.get("command")

        return ToolCallRecord(
            id=tool_id,
            name=tool_name,
            timestamp=timestamp,
            success=success,
            input_size=input_size,
            output_size=0,  # Would need tool_result record
            milestone_id=None,  # Assigned later
            is_subagent=is_subagent,
            _bash_command=bash_command,
        )

    def parse_stdout_stats(self, stdout_file: Path, logs_dir: Optional[Path] = None) -> Dict:
        """Parse agent_stdout.txt for accumulated statistics.

        The agent_stdout.txt file is in JSONL format with one JSON object
        per Claude Code execution attempt. Multiple attempts may reuse the
        same underlying session_id.

        Args:
            stdout_file: Path to agent_stdout.txt

        Returns:
            Dictionary with accumulated statistics
        """
        total_cost = 0.0
        total_turns = 0
        model_usage: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(int))
        session_count = 0
        unique_session_ids: set[str] = set()

        if not stdout_file.exists():
            logger.warning(f"stdout file not found: {stdout_file}")
            return {
                "total_cost_usd": 0.0,
                "total_turns": 0,
                "modelUsage": {},
                "session_count": 0,
                "unique_session_count": 0,
            }

        non_accumulating_numeric_keys = {"contextWindow", "maxOutputTokens"}

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
                session_id = data.get("session_id")
                if isinstance(session_id, str) and session_id:
                    unique_session_ids.add(session_id)
                total_cost += data.get("total_cost_usd", 0)
                total_turns += data.get("num_turns", 0)

                # Accumulate model usage
                for model, usage in data.get("modelUsage", {}).items():
                    if not isinstance(usage, dict):
                        continue
                    for key, val in usage.items():
                        if isinstance(val, (int, float)):
                            if key in non_accumulating_numeric_keys:
                                # Metadata fields should remain stable per model; keep a single value.
                                prev = model_usage[model].get(key)
                                model_usage[model][key] = max(prev, val) if isinstance(prev, (int, float)) else val
                            else:
                                model_usage[model][key] += val

        # Convert defaultdicts to regular dicts
        model_usage_dict = {model: dict(usage) for model, usage in model_usage.items()}

        unique_session_count = len(unique_session_ids) if unique_session_ids else session_count

        # Supplement turn count from JSONL files (assistant messages with usage).
        # The stdout result JSON only contains main-agent turns from completed
        # sessions. Crashed sessions (e.g. context overflow) and subagent calls
        # are missing. JSONL counting captures everything.
        if logs_dir and logs_dir.exists():
            jsonl_turns = 0
            for jsonl_file in logs_dir.rglob("*.jsonl"):
                try:
                    with open(jsonl_file, encoding="utf-8") as f:
                        for line in f:
                            try:
                                obj = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            msg = obj.get("message", {})
                            if isinstance(msg, dict) and msg.get("usage", {}).get("input_tokens"):
                                jsonl_turns += 1
                except Exception:
                    continue
            if jsonl_turns > total_turns:
                logger.info(
                    f"JSONL turn count ({jsonl_turns}) exceeds stdout count ({total_turns}); "
                    f"using JSONL count (covers crashed sessions and subagent calls)"
                )
                total_turns = jsonl_turns

        # Fallback: if claude-code CLI didn't report cost (e.g. unrecognized model
        # like glm-5.1), sum costUSD from modelUsage entries instead.
        if total_cost == 0 and model_usage_dict:
            mu_cost = sum(u.get("costUSD", 0) for u in model_usage_dict.values() if isinstance(u, dict))
            if mu_cost > 0:
                total_cost = mu_cost
                logger.info(f"CLI reported $0 cost; using modelUsage sum: ${total_cost:.2f}")

        logger.info(
            f"Parsed stdout: {session_count} executions, {unique_session_count} unique sessions, "
            f"{total_turns} turns, ${total_cost:.2f}"
        )

        return {
            "total_cost_usd": total_cost,
            "total_turns": total_turns,
            "modelUsage": model_usage_dict,
            "session_count": session_count,
            "unique_session_count": unique_session_count,
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
