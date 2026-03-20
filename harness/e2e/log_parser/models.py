"""Data models for agent log parsing."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import json


@dataclass
class ToolCallRecord:
    """Record of a single tool call made by the agent."""

    id: str  # Tool call ID
    name: str  # Tool name (Bash, Read, Edit, etc.)
    timestamp: datetime  # Call timestamp
    success: bool  # Whether the call succeeded
    input_size: int  # Input size in bytes
    output_size: int  # Output size in bytes
    milestone_id: Optional[str] = None  # Associated milestone
    is_subagent: bool = False  # Whether from subagent
    cost_usd: float = 0.0  # Allocated cost for this tool call
    token_usage: Dict[str, int] = field(default_factory=dict)  # Allocated token usage for this tool call
    _bash_command: Optional[str] = None  # Internal: raw command for Bash tool calls (not serialized)
    behavior_detail: Optional[str] = None  # Fine-grained behavior category (14 categories)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        d = {
            "id": self.id,
            "name": self.name,
            "timestamp": self.timestamp.isoformat() + "Z" if self.timestamp else None,
            "success": self.success,
            "input_size": self.input_size,
            "output_size": self.output_size,
            "milestone_id": self.milestone_id,
            "is_subagent": self.is_subagent,
            "cost_usd": self.cost_usd,
            "token_usage": self.token_usage,
        }
        if self.behavior_detail is not None:
            d["behavior_detail"] = self.behavior_detail
        return d


@dataclass
class SessionInfo:
    """Timing information for a detected active session."""

    session_index: int  # 0-based session index
    start_time: Optional[datetime] = None  # Session start (agent_exec_start or first tool call)
    end_time: Optional[datetime] = None  # Session end (agent_exec_end or last tool call)
    duration_ms: int = 0  # Active duration (end - start)
    tool_call_count: int = 0  # Number of tool calls in this session
    session_id: Optional[str] = None  # UUID from session_history.jsonl

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        d = {
            "session_index": self.session_index,
            "start_time": self.start_time.isoformat() + "Z" if self.start_time else None,
            "end_time": self.end_time.isoformat() + "Z" if self.end_time else None,
            "duration_ms": self.duration_ms,
        }
        if self.tool_call_count:
            d["tool_call_count"] = self.tool_call_count
        if self.session_id is not None:
            d["session_id"] = self.session_id
        return d


@dataclass
class NativeUsageUnit:
    """Framework-native finest-grained usage/cost unit (message/turn)."""

    id: str
    source_type: str  # e.g., "message", "turn"
    timestamp: Optional[datetime]
    model: str
    milestone_id: Optional[str] = None
    token_usage: Dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    is_subagent: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "timestamp": self.timestamp.isoformat() + "Z" if self.timestamp else None,
            "model": self.model,
            "milestone_id": self.milestone_id,
            "token_usage": self.token_usage,
            "cost_usd": self.cost_usd,
            "is_subagent": self.is_subagent,
        }


@dataclass
class MilestoneStats:
    """Statistics for a single milestone."""

    milestone_id: str
    start_time: datetime  # From git tag (previous milestone end_time)
    end_time: datetime  # From git tag
    duration_ms: int  # Active duration (sum of sessions, excluding idle gaps)
    wall_clock_ms: int = 0  # Wall-clock duration (end - start, including gaps)
    turns: int = 0
    cost_usd: float = 0.0
    subagent_calls: int = 0
    token_usage: Dict[str, int] = field(default_factory=dict)  # inputTokens, outputTokens, etc.
    total_tool_calls: int = 0
    tool_call_breakdown: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        return {
            "milestone_id": self.milestone_id,
            "start_time": self.start_time.isoformat() + "Z" if self.start_time else None,
            "end_time": self.end_time.isoformat() + "Z" if self.end_time else None,
            "duration_ms": self.duration_ms,
            "wall_clock_ms": self.wall_clock_ms,
            "turns": self.turns,
            "cost_usd": self.cost_usd,
            "subagent_calls": self.subagent_calls,
            "token_usage": self.token_usage,
            "total_tool_calls": self.total_tool_calls,
            "tool_call_breakdown": self.tool_call_breakdown,
        }


@dataclass
class TrialStats:
    """Complete statistics for a trial run."""

    trial_name: str
    agent_framework: str
    model: str

    # Summary
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_ms: int = 0  # Active duration (sum of sessions, excluding idle gaps)
    wall_clock_ms: int = 0  # Wall-clock duration (end - start, including gaps)
    total_cost_usd: float = 0.0
    total_turns: int = 0
    total_tool_calls: int = 0
    total_subagent_calls: int = 0
    session_count: int = 0  # Execution attempts (used for active-time segmentation)
    unique_session_count: int = 0  # Unique logical session IDs
    sessions: List[SessionInfo] = field(default_factory=list)  # Detected active sessions

    # Agent configuration
    reasoning_effort: Optional[str] = None  # For Codex: "low", "medium", "high", "xhigh"

    # Aggregations
    model_usage: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    tool_call_breakdown: Dict[str, int] = field(default_factory=dict)
    milestone_stats: Dict[str, MilestoneStats] = field(default_factory=dict)
    native_usage_units: List[NativeUsageUnit] = field(default_factory=list)
    all_tool_calls: List[ToolCallRecord] = field(default_factory=list)
    verification_events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        result = {
            "trial_name": self.trial_name,
            "agent_framework": self.agent_framework,
            "model": self.model,
        }

        # Add reasoning_effort right after model if set (only for GPT models)
        if self.reasoning_effort and self.model.lower().startswith("gpt"):
            result["reasoning_effort"] = self.reasoning_effort

        result.update(
            {
                "summary": {
                    "start_time": self.start_time.isoformat() + "Z" if self.start_time else None,
                    "end_time": self.end_time.isoformat() + "Z" if self.end_time else None,
                    "duration_ms": self.duration_ms,
                    "wall_clock_ms": self.wall_clock_ms,
                    "total_cost_usd": self.total_cost_usd,
                    "total_turns": self.total_turns,
                    "total_tool_calls": self.total_tool_calls,
                    "total_subagent_calls": self.total_subagent_calls,
                    "session_count": self.session_count,
                    "unique_session_count": self.unique_session_count,
                    "sessions": [s.to_dict() for s in self.sessions],
                },
                "modelUsage": self.model_usage,
                "tool_call_breakdown": self.tool_call_breakdown,
                "milestone_stats": {
                    mid: (ms.to_dict() if hasattr(ms, "to_dict") else ms) for mid, ms in self.milestone_stats.items()
                },
                "usage_units": [(u.to_dict() if hasattr(u, "to_dict") else u) for u in self.native_usage_units],
                "all_tool_calls": [(tc.to_dict() if hasattr(tc, "to_dict") else tc) for tc in self.all_tool_calls],
                "verification_events": self.verification_events,
            }
        )

        return result

    def to_json(self, path: Path) -> None:
        """Write statistics to JSON file.

        Args:
            path: Output file path
        """

        def json_serializer(obj):
            """Custom JSON serializer for objects not serializable by default."""
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False, default=json_serializer)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrialStats":
        """Create TrialStats from dictionary.

        Args:
            data: Dictionary representation

        Returns:
            TrialStats instance
        """
        summary = data.get("summary", {})

        # Parse datetime strings
        start_time = None
        end_time = None
        if summary.get("start_time"):
            start_time = datetime.fromisoformat(summary["start_time"].rstrip("Z"))
        if summary.get("end_time"):
            end_time = datetime.fromisoformat(summary["end_time"].rstrip("Z"))

        # Parse milestone stats
        milestone_stats = {}
        for mid, ms_data in data.get("milestone_stats", {}).items():
            ms_start = None
            ms_end = None
            if ms_data.get("start_time"):
                ms_start = datetime.fromisoformat(ms_data["start_time"].rstrip("Z"))
            if ms_data.get("end_time"):
                ms_end = datetime.fromisoformat(ms_data["end_time"].rstrip("Z"))

            milestone_stats[mid] = MilestoneStats(
                milestone_id=ms_data["milestone_id"],
                start_time=ms_start,
                end_time=ms_end,
                duration_ms=ms_data.get("duration_ms", 0),
                wall_clock_ms=ms_data.get("wall_clock_ms", 0),
                turns=ms_data.get("turns", 0),
                cost_usd=ms_data.get("cost_usd", 0.0),
                subagent_calls=ms_data.get("subagent_calls", 0),
                token_usage=ms_data.get("token_usage", {}),
                total_tool_calls=ms_data.get("total_tool_calls", 0),
                tool_call_breakdown=ms_data.get("tool_call_breakdown", {}),
            )

        # Parse tool calls
        all_tool_calls = []
        for tc_data in data.get("all_tool_calls", []):
            tc_timestamp = None
            if tc_data.get("timestamp"):
                tc_timestamp = datetime.fromisoformat(tc_data["timestamp"].rstrip("Z"))

            all_tool_calls.append(
                ToolCallRecord(
                    id=tc_data["id"],
                    name=tc_data["name"],
                    timestamp=tc_timestamp,
                    success=tc_data.get("success", True),
                    input_size=tc_data.get("input_size", 0),
                    output_size=tc_data.get("output_size", 0),
                    milestone_id=tc_data.get("milestone_id"),
                    is_subagent=tc_data.get("is_subagent", False),
                    cost_usd=tc_data.get("cost_usd", 0.0),
                    token_usage=tc_data.get("token_usage", {}),
                    behavior_detail=tc_data.get("behavior_detail"),
                )
            )

        # Parse native usage units
        native_usage_units = []
        native_units_data = data.get("usage_units")
        if not isinstance(native_units_data, list):
            native_units_data = data.get("framework_native_usage_units")
        if not isinstance(native_units_data, list):
            native_units_data = data.get("native_usage_units", [])

        for u_data in native_units_data:
            u_ts = None
            if isinstance(u_data, dict) and u_data.get("timestamp"):
                u_ts = datetime.fromisoformat(u_data["timestamp"].rstrip("Z"))
            if not isinstance(u_data, dict):
                continue
            native_usage_units.append(
                NativeUsageUnit(
                    id=u_data.get("id", ""),
                    source_type=u_data.get("source_type", "turn"),
                    timestamp=u_ts,
                    model=u_data.get("model", "unknown"),
                    milestone_id=u_data.get("milestone_id"),
                    token_usage=u_data.get("token_usage", {}),
                    cost_usd=u_data.get("cost_usd", 0.0),
                    is_subagent=u_data.get("is_subagent", False),
                )
            )

        # Parse sessions
        sessions = []
        for s_data in summary.get("sessions", []):
            s_start = None
            s_end = None
            if s_data.get("start_time"):
                s_start = datetime.fromisoformat(s_data["start_time"].rstrip("Z"))
            if s_data.get("end_time"):
                s_end = datetime.fromisoformat(s_data["end_time"].rstrip("Z"))
            sessions.append(
                SessionInfo(
                    session_index=s_data.get("session_index", 0),
                    start_time=s_start,
                    end_time=s_end,
                    duration_ms=s_data.get("duration_ms", 0),
                    tool_call_count=s_data.get("tool_call_count", 0),
                    session_id=s_data.get("session_id"),
                )
            )

        return cls(
            trial_name=data.get("trial_name", ""),
            agent_framework=data.get("agent_framework", ""),
            model=data.get("model", ""),
            start_time=start_time,
            end_time=end_time,
            duration_ms=summary.get("duration_ms", 0),
            wall_clock_ms=summary.get("wall_clock_ms", 0),
            total_cost_usd=summary.get("total_cost_usd", 0.0),
            total_turns=summary.get("total_turns", 0),
            total_tool_calls=summary.get("total_tool_calls", 0),
            total_subagent_calls=summary.get("total_subagent_calls", 0),
            session_count=summary.get("session_count", 0),
            unique_session_count=summary.get("unique_session_count", summary.get("session_count", 0)),
            sessions=sessions,
            reasoning_effort=data.get("reasoning_effort"),
            model_usage=data.get("modelUsage", {}),
            tool_call_breakdown=data.get("tool_call_breakdown", {}),
            milestone_stats=milestone_stats,
            native_usage_units=native_usage_units,
            all_tool_calls=all_tool_calls,
            verification_events=data.get("verification_events", []),
        )

    @classmethod
    def from_json(cls, path: Path) -> "TrialStats":
        """Load TrialStats from JSON file.

        Args:
            path: JSON file path

        Returns:
            TrialStats instance
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
