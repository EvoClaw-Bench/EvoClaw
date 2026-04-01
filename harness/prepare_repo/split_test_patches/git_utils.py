"""Git utilities for accessing file content at specific refs."""

import subprocess
from pathlib import Path
from typing import Optional


def get_file_at_git_ref(testbed: Path, file_path: str, ref: str) -> Optional[str]:
    """Get file content at a specific git ref.

    Args:
        testbed: Path to the git repository
        file_path: Relative path to the file within the repo
        ref: Git ref (tag, branch, commit hash)

    Returns:
        File content as string, or None if not found
    """
    try:
        result = subprocess.run(
            ["git", "show", f"{ref}:{file_path}"], capture_output=True, text=True, cwd=testbed, timeout=10
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception:
        return None
