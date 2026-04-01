"""Patch file parsing utilities."""

import re
from typing import List, Dict, Tuple

from .models import Hunk


def parse_patch_hunks(patch_content: str) -> Dict[str, List[Hunk]]:
    """Parse patch into {file: [hunks]} structure."""
    result = {}
    current_file = None
    current_hunk = None
    hunk_lines = []

    for line in patch_content.split("\n"):
        if line.startswith("diff --git"):
            # Save previous hunk
            if current_hunk and current_file:
                current_hunk.content = "\n".join(hunk_lines)
                if current_file not in result:
                    result[current_file] = []
                result[current_file].append(current_hunk)

            # Start new file
            match = re.search(r"b/(.+)$", line)
            current_file = match.group(1) if match else None
            current_hunk = None
            hunk_lines = []

        elif line.startswith("@@") and current_file:
            # Save previous hunk
            if current_hunk:
                current_hunk.content = "\n".join(hunk_lines)
                if current_file not in result:
                    result[current_file] = []
                result[current_file].append(current_hunk)

            # Parse new hunk header
            match = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                current_hunk = Hunk(
                    file=current_file,
                    old_start=int(match.group(1)),
                    old_count=int(match.group(2) or 1),
                    new_start=int(match.group(3)),
                    new_count=int(match.group(4) or 1),
                    content="",
                )
                hunk_lines = [line]

        elif current_hunk:
            hunk_lines.append(line)

    # Save last hunk
    if current_hunk and current_file:
        current_hunk.content = "\n".join(hunk_lines)
        if current_file not in result:
            result[current_file] = []
        result[current_file].append(current_hunk)

    return result


def get_actual_modified_lines(hunk: Hunk) -> Tuple[List[int], List[int]]:
    """
    Parse hunk content to get actual modified line numbers.

    Returns (old_deleted_lines, new_added_lines):
    - old_deleted_lines: line numbers of '-' lines in old file
    - new_added_lines: line numbers of '+' lines in new file

    Context lines (space prefix) are ignored.
    """
    old_deleted = []
    new_added = []

    old_line = hunk.old_start
    new_line = hunk.new_start

    for line in hunk.content.split("\n"):
        if line.startswith("@@"):
            continue  # skip hunk header
        elif line.startswith("-"):
            old_deleted.append(old_line)
            old_line += 1
        elif line.startswith("+"):
            new_added.append(new_line)
            new_line += 1
        elif line.startswith(" ") or line == "":
            # Context line or empty line
            old_line += 1
            new_line += 1

    return old_deleted, new_added


def get_modified_lines_with_content(hunk: Hunk) -> Tuple[List[Tuple[int, str]], List[Tuple[int, str]]]:
    """
    Parse hunk content to get actual modified line numbers WITH their content.

    Returns (old_deleted_lines, new_added_lines):
    - old_deleted_lines: list of (line_number, content) tuples for '-' lines
    - new_added_lines: list of (line_number, content) tuples for '+' lines

    Context lines (space prefix) are ignored.
    """
    old_deleted = []
    new_added = []

    old_line = hunk.old_start
    new_line = hunk.new_start

    for line in hunk.content.split("\n"):
        if line.startswith("@@"):
            continue  # skip hunk header
        elif line.startswith("-"):
            # Remove the '-' prefix to get actual content
            content = line[1:] if len(line) > 1 else ""
            old_deleted.append((old_line, content))
            old_line += 1
        elif line.startswith("+"):
            # Remove the '+' prefix to get actual content
            content = line[1:] if len(line) > 1 else ""
            new_added.append((new_line, content))
            new_line += 1
        elif line.startswith(" ") or line == "":
            # Context line or empty line
            old_line += 1
            new_line += 1

    return old_deleted, new_added


def get_insertion_point(hunk: Hunk) -> int:
    """
    For pure addition hunks (no deletions), get the insertion point in old file.

    Returns the old line number after which the new code is inserted.
    This is the last context line BEFORE the first '+' line.
    """
    old_line = hunk.old_start
    last_context_before_add = hunk.old_start - 1  # before first line
    found_addition = False

    for line in hunk.content.split("\n"):
        if line.startswith("@@"):
            continue
        elif line.startswith("-"):
            old_line += 1
        elif line.startswith("+"):
            found_addition = True
            # Don't increment old_line for additions
        elif line.startswith(" "):
            # Only count context lines BEFORE the first addition
            if not found_addition:
                last_context_before_add = old_line
            old_line += 1
        # Ignore empty strings (artifacts from split)

    return last_context_before_add
