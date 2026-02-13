"""Abstract base class for agent log parsers."""

import logging
import subprocess
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Type

from harness.e2e.log_parser.models import MilestoneStats, SessionInfo, ToolCallRecord, TrialStats

logger = logging.getLogger(__name__)


class AgentLogParser(ABC):
    """Abstract base class for parsing agent logs from various frameworks."""

    FRAMEWORK_NAME: str = "unknown"

    @abstractmethod
    def extract_trace(self, container_name: str, output_dir: Path) -> bool:
        """Extract detailed agent trace using agent-specific tools.

        This method calls agent-specific extraction tools (e.g., claude-extract)
        to produce detailed, human-readable trace files.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store trace files

        Returns:
            True if extraction was successful
        """

    @abstractmethod
    def extract_raw_logs(
        self,
        container_name: str,
        output_dir: Path,
        session_id: Optional[str] = None,
    ) -> Path:
        """Extract raw logs from container.

        Args:
            container_name: Name of the Docker container
            output_dir: Directory to store extracted logs
            session_id: Optional session/thread ID to filter logs (Codex uses thread_id)

        Returns:
            Path to the extracted logs directory
        """

    @abstractmethod
    def parse_tool_calls(self, log_dir: Path) -> List[ToolCallRecord]:
        """Parse tool calls from logs.

        Args:
            log_dir: Directory containing extracted logs

        Returns:
            List of tool call records, including subagent calls
        """

    @abstractmethod
    def parse_stdout_stats(self, stdout_file: Path) -> Dict:
        """Parse agent stdout file for accumulated statistics.

        Args:
            stdout_file: Path to agent_stdout.txt file

        Returns:
            Dictionary with total_cost_usd, total_turns, modelUsage, session_count
        """

    def parse_tool_results(
        self,
        log_dir: Path,
        tool_calls: List[ToolCallRecord],
    ) -> None:
        """Update tool calls with result information.

        Parses result records from logs and updates the corresponding
        tool call records with success status and output size.

        Modifies tool_calls in place.

        This is an optional method - agents that don't have separate
        result records can skip implementing this.

        Args:
            log_dir: Directory containing extracted logs
            tool_calls: List of tool call records to update
        """
        # Default implementation: no-op
        # Override in subclasses if the agent has separate result records
        pass

    def get_milestone_times(self, container_name: str, work_dir: str = "/testbed") -> Dict[str, Dict]:
        """Get milestone time boundaries from git tags.

        Args:
            container_name: Docker container name
            work_dir: Working directory in container

        Returns:
            Dictionary mapping milestone IDs to start/end times
        """
        try:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "git",
                    "-C",
                    work_dir,
                    "for-each-ref",
                    "--sort=creatordate",
                    "--format=%(refname:short) %(creatordate:iso8601)",
                    "refs/tags/agent-impl-*",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                logger.warning(f"Failed to get git tags: {result.stderr}")
                return {}

            milestone_times = {}
            previous_end_time = None

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.split(" ", 1)
                if len(parts) != 2:
                    continue

                tag_name, timestamp_str = parts
                # Parse tag name: agent-impl-M002 -> M002
                if tag_name.startswith("agent-impl-"):
                    milestone_id = tag_name.replace("agent-impl-", "")
                else:
                    continue

                try:
                    # Parse ISO 8601 timestamp and convert to naive datetime
                    end_time = datetime.fromisoformat(timestamp_str.strip())
                    # Remove timezone info for consistent comparison
                    if end_time.tzinfo is not None:
                        end_time = end_time.replace(tzinfo=None)
                except ValueError as e:
                    logger.warning(f"Failed to parse timestamp '{timestamp_str}': {e}")
                    continue

                milestone_times[milestone_id] = {
                    "start_time": previous_end_time,  # Will be None for first milestone
                    "end_time": end_time,
                }
                previous_end_time = end_time

            return milestone_times

        except subprocess.TimeoutExpired:
            logger.warning("Timeout getting git tags")
            return {}
        except Exception as e:
            logger.warning(f"Error getting milestone times: {e}")
            return {}

    # Default gap threshold: 30 minutes. Gaps larger than this indicate a new session.
    SESSION_GAP_THRESHOLD_MS = 30 * 60 * 1000

    @staticmethod
    def load_session_times_from_history(session_history_path: Path) -> List[SessionInfo]:
        """Load precise session times from session_history.jsonl.

        Parses agent_exec_start/agent_exec_end event pairs to get exact
        session boundaries as recorded by agent_runner.py.

        Args:
            session_history_path: Path to session_history.jsonl

        Returns:
            List of SessionInfo with precise start/end times. Empty if file
            doesn't exist or has no exec events (old data).
        """
        import json as _json

        if not session_history_path.exists():
            return []

        try:
            events = []
            with open(session_history_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue

            # Pair agent_exec_start with agent_exec_end
            sessions = []
            pending_start = None
            for ev in events:
                event_type = ev.get("event")
                if event_type == "agent_exec_start":
                    pending_start = ev
                elif event_type == "agent_exec_end" and pending_start:
                    start_ts = pending_start.get("ts")
                    end_ts = ev.get("ts")
                    if start_ts and end_ts:
                        start_time = datetime.fromisoformat(start_ts)
                        end_time = datetime.fromisoformat(end_ts)
                        # Remove timezone for consistent comparison
                        if start_time.tzinfo is not None:
                            start_time = start_time.replace(tzinfo=None)
                        if end_time.tzinfo is not None:
                            end_time = end_time.replace(tzinfo=None)
                        dur = max(0, int((end_time - start_time).total_seconds() * 1000))

                        sessions.append(
                            SessionInfo(
                                session_index=len(sessions),
                                start_time=start_time,
                                end_time=end_time,
                                duration_ms=dur,
                                tool_call_count=0,  # Will be enriched later if needed
                            )
                        )
                    pending_start = None

            return sessions
        except Exception as e:
            logger.warning(f"Failed to load session history from {session_history_path}: {e}")
            return []

    @staticmethod
    def detect_sessions_from_tool_calls(
        tool_calls: List[ToolCallRecord],
        gap_threshold_ms: int = SESSION_GAP_THRESHOLD_MS,
    ) -> List[SessionInfo]:
        """Detect active sessions by finding gaps in tool call timestamps.

        Fallback method when session_history.jsonl doesn't have exec events (old data).
        Groups consecutive tool calls into sessions; a new session starts when the
        gap between consecutive tool calls exceeds gap_threshold_ms.

        Args:
            tool_calls: List of tool call records
            gap_threshold_ms: Minimum gap in ms to start a new session (default: 30 min)

        Returns:
            List of SessionInfo objects, ordered by time.
        """
        timed_calls = sorted(
            [tc for tc in tool_calls if tc.timestamp],
            key=lambda tc: tc.timestamp,
        )
        if not timed_calls:
            return []

        sessions: List[SessionInfo] = []
        session_start = timed_calls[0].timestamp
        session_count = 1
        prev_ts = timed_calls[0].timestamp

        for tc in timed_calls[1:]:
            gap_ms = int((tc.timestamp - prev_ts).total_seconds() * 1000)
            if gap_ms > gap_threshold_ms:
                # Finalize current session
                sessions.append(
                    SessionInfo(
                        session_index=len(sessions),
                        start_time=session_start,
                        end_time=prev_ts,
                        duration_ms=int((prev_ts - session_start).total_seconds() * 1000),
                        tool_call_count=session_count,
                    )
                )
                session_start = tc.timestamp
                session_count = 1
            else:
                session_count += 1
            prev_ts = tc.timestamp

        # Finalize last session
        sessions.append(
            SessionInfo(
                session_index=len(sessions),
                start_time=session_start,
                end_time=prev_ts,
                duration_ms=int((prev_ts - session_start).total_seconds() * 1000),
                tool_call_count=session_count,
            )
        )
        return sessions

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
        """Compute complete trial statistics.

        Args:
            trial_name: Name of the trial
            model: Model identifier
            tool_calls: List of parsed tool calls
            stdout_stats: Statistics from agent stdout
            milestone_times: Optional milestone time boundaries
            reasoning_effort: Optional reasoning effort level (for Codex)
            session_history_path: Optional path to session_history.jsonl for precise session timing

        Returns:
            Complete TrialStats object
        """
        milestone_times = milestone_times or {}

        # Assign milestones to tool calls based on timestamps
        if milestone_times:
            self._assign_milestones_to_tool_calls(tool_calls, milestone_times)

        # Compute overall statistics
        total_subagent_calls = sum(1 for tc in tool_calls if tc.is_subagent)

        # Compute tool call breakdown
        tool_call_breakdown = defaultdict(int)
        for tc in tool_calls:
            tool_call_breakdown[tc.name] += 1

        # Determine start/end times
        start_time = None
        end_time = None
        if tool_calls:
            timestamps = [tc.timestamp for tc in tool_calls if tc.timestamp]
            if timestamps:
                start_time = min(timestamps)
                end_time = max(timestamps)

        # Override with milestone times if available
        if milestone_times:
            all_starts = [m["start_time"] for m in milestone_times.values() if m.get("start_time")]
            all_ends = [m["end_time"] for m in milestone_times.values() if m.get("end_time")]
            if all_starts:
                start_time = min(all_starts)
            if all_ends:
                end_time = max(all_ends)

        # Wall-clock duration (end - start, including idle gaps)
        wall_clock_ms = 0
        if start_time and end_time:
            wall_clock_ms = int((end_time - start_time).total_seconds() * 1000)

        # Detect sessions for active duration calculation
        # Priority: session_history.jsonl (precise) > tool call gap detection (fallback)
        sessions: List[SessionInfo] = []
        if session_history_path:
            sessions = self.load_session_times_from_history(session_history_path)
        if not sessions:
            sessions = self.detect_sessions_from_tool_calls(tool_calls)

        # Active duration = sum of session durations (excluding idle gaps)
        duration_ms = sum(s.duration_ms for s in sessions) if sessions else wall_clock_ms

        # Compute per-milestone statistics
        milestone_stats = self._compute_milestone_stats(milestone_times, tool_calls, stdout_stats)

        # Get model usage and add reasoning_effort for models that support it
        model_usage = stdout_stats.get("modelUsage", {})
        if reasoning_effort:
            model_usage = self._add_reasoning_effort_to_model_usage(model_usage, reasoning_effort)

        return TrialStats(
            trial_name=trial_name,
            agent_framework=self.FRAMEWORK_NAME,
            model=model,
            start_time=start_time,
            end_time=end_time,
            duration_ms=duration_ms,
            wall_clock_ms=wall_clock_ms,
            total_cost_usd=stdout_stats.get("total_cost_usd", 0.0),
            total_turns=stdout_stats.get("total_turns", 0),
            total_tool_calls=len(tool_calls),
            total_subagent_calls=total_subagent_calls,
            session_count=stdout_stats.get("session_count", 0),
            sessions=sessions,
            reasoning_effort=reasoning_effort,
            model_usage=model_usage,
            tool_call_breakdown=dict(tool_call_breakdown),
            milestone_stats=milestone_stats,
            all_tool_calls=tool_calls,
        )

    def _assign_milestones_to_tool_calls(
        self,
        tool_calls: List[ToolCallRecord],
        milestone_times: Dict[str, Dict],
    ) -> None:
        """Assign milestone IDs to tool calls based on timestamps.

        Modifies tool calls in place.

        Args:
            tool_calls: List of tool call records
            milestone_times: Milestone time boundaries
        """
        # Sort milestones by end time
        sorted_milestones = sorted(
            [(mid, times) for mid, times in milestone_times.items() if times.get("end_time")],
            key=lambda x: x[1]["end_time"],
        )

        for tc in tool_calls:
            if not tc.timestamp:
                continue

            for mid, times in sorted_milestones:
                end_time = times["end_time"]
                start_time = times.get("start_time")

                # Tool call is in this milestone if:
                # - timestamp <= end_time AND
                # - (no start_time OR timestamp > start_time)
                if tc.timestamp <= end_time:
                    if start_time is None or tc.timestamp > start_time:
                        tc.milestone_id = mid
                        break

    def _compute_milestone_stats(
        self,
        milestone_times: Dict[str, Dict],
        tool_calls: List[ToolCallRecord],
        stdout_stats: Dict,
    ) -> Dict[str, MilestoneStats]:
        """Compute per-milestone statistics.

        Args:
            milestone_times: Milestone time boundaries {mid: {start_time, end_time}}
            tool_calls: List of tool call records
            stdout_stats: Statistics from agent stdout

        Returns:
            Dictionary mapping milestone IDs to MilestoneStats objects
        """
        if not milestone_times:
            return {}

        milestone_stats = {}
        total_milestones = len(milestone_times)
        total_turns = stdout_stats.get("total_turns", 0)
        total_cost = stdout_stats.get("total_cost_usd", 0.0)
        model_usage_data = stdout_stats.get("modelUsage", {})

        for mid, times in milestone_times.items():
            ms_tool_calls = [tc for tc in tool_calls if tc.milestone_id == mid]
            ms_breakdown = defaultdict(int)
            for tc in ms_tool_calls:
                ms_breakdown[tc.name] += 1

            # Use git tag times, fallback to tool call times if start_time is None
            ms_start_time = times.get("start_time")
            ms_end_time = times.get("end_time")

            # For first milestone, start_time is None - use earliest tool call time
            if ms_start_time is None and ms_tool_calls:
                ms_timestamps = [tc.timestamp for tc in ms_tool_calls if tc.timestamp]
                if ms_timestamps:
                    ms_start_time = min(ms_timestamps)

            # Wall-clock duration for this milestone
            ms_wall_clock = 0
            if ms_start_time and ms_end_time:
                ms_wall_clock = int((ms_end_time - ms_start_time).total_seconds() * 1000)

            # Active duration: detect sessions within this milestone's tool calls
            ms_sessions = self.detect_sessions_from_tool_calls(ms_tool_calls)
            ms_duration = sum(s.duration_ms for s in ms_sessions) if ms_sessions else ms_wall_clock

            # For single milestone, use total stats; for multiple, estimate proportionally
            if total_milestones == 1:
                ms_turns = total_turns
                ms_cost = total_cost
                # Extract token usage from modelUsage
                ms_token_usage = {}
                for model_name, usage in model_usage_data.items():
                    # Handle different key naming conventions across frameworks:
                    # - Claude: cacheReadInputTokens, cacheCreationInputTokens
                    # - Codex: cachedInputTokens
                    # - Gemini: cachedContentTokenCount
                    cache_read = usage.get(
                        "cacheReadInputTokens",
                        usage.get("cacheReadTokens", usage.get("cachedInputTokens", 0)),
                    )
                    ms_token_usage = {
                        "inputTokens": usage.get("inputTokens", 0),
                        "outputTokens": usage.get("outputTokens", 0),
                        "cacheReadInputTokens": cache_read,
                        "cacheCreationInputTokens": usage.get("cacheCreationInputTokens", 0),
                    }
                    break  # Use first model's usage
            else:
                # For multiple milestones, estimate based on tool call proportion
                proportion = len(ms_tool_calls) / len(tool_calls) if tool_calls else 0
                ms_turns = int(total_turns * proportion)
                ms_cost = total_cost * proportion
                ms_token_usage = {}  # Cannot accurately split without per-milestone tracking

            milestone_stats[mid] = MilestoneStats(
                milestone_id=mid,
                start_time=ms_start_time,
                end_time=ms_end_time,
                duration_ms=ms_duration,
                wall_clock_ms=ms_wall_clock,
                turns=ms_turns,
                cost_usd=ms_cost,
                subagent_calls=sum(1 for tc in ms_tool_calls if tc.is_subagent),
                token_usage=ms_token_usage,
                total_tool_calls=len(ms_tool_calls),
                tool_call_breakdown=dict(ms_breakdown),
            )

        return milestone_stats

    def _add_reasoning_effort_to_model_usage(
        self,
        model_usage: Dict[str, Dict],
        reasoning_effort: str,
    ) -> Dict[str, Dict]:
        """Add reasoning_effort to model usage for models that support it.

        Only adds reasoning_effort for models starting with "gpt-5".

        Args:
            model_usage: Original model usage dictionary
            reasoning_effort: Reasoning effort level

        Returns:
            Updated model usage dictionary
        """
        updated = {}
        for model_name, usage in model_usage.items():
            usage_copy = dict(usage)
            # Only add reasoning_effort for gpt-5* models
            if model_name.lower().startswith("gpt-5"):
                usage_copy["reasoning_effort"] = reasoning_effort
            updated[model_name] = usage_copy
        return updated


# Registry of available parsers
_PARSER_REGISTRY: Dict[str, Type[AgentLogParser]] = {}


def register_parser(framework: str):
    """Decorator to register a parser class.

    Args:
        framework: Framework name for registration

    Returns:
        Class decorator
    """

    def decorator(cls: Type[AgentLogParser]) -> Type[AgentLogParser]:
        _PARSER_REGISTRY[framework] = cls
        return cls

    return decorator


def get_parser(framework: str) -> AgentLogParser:
    """Factory function to get a parser for a specific framework.

    Args:
        framework: Framework name (e.g., "claude_code")

    Returns:
        Parser instance for the framework

    Raises:
        ValueError: If framework is not supported
    """
    # Import implementations to trigger registration
    from harness.e2e.log_parser import claude_code  # noqa: F401
    from harness.e2e.log_parser import codex  # noqa: F401
    from harness.e2e.log_parser import gemini  # noqa: F401  # registers as "gemini-cli"
    from harness.e2e.log_parser import openhands  # noqa: F401

    if framework not in _PARSER_REGISTRY:
        available = ", ".join(_PARSER_REGISTRY.keys()) or "none"
        raise ValueError(f"Unknown framework: {framework}. Available: {available}")

    return _PARSER_REGISTRY[framework]()
