"""Agent log parser package for extracting and analyzing agent execution logs.

This package provides a plugin-style architecture for parsing logs from various
agent frameworks. Currently supports:
- Claude Code (claude-code)
- OpenAI Codex (codex)

Usage:
    from harness.e2e.log_parser import get_parser, TrialStats

    parser = get_parser("claude-code")  # or "codex"
    tool_calls = parser.parse_tool_calls(log_dir)
    stats = parser.compute_trial_stats(...)
"""

from harness.e2e.log_parser.models import ToolCallRecord, MilestoneStats, SessionInfo, TrialStats
from harness.e2e.log_parser.base import AgentLogParser, get_parser

__all__ = [
    "ToolCallRecord",
    "MilestoneStats",
    "SessionInfo",
    "TrialStats",
    "AgentLogParser",
    "get_parser",
]
