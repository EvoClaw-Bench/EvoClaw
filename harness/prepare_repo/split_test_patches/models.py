"""Data models for patch analysis."""

from dataclasses import dataclass


@dataclass
class Hunk:
    """Represents a single hunk in a patch file."""

    file: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    content: str  # The actual diff content
