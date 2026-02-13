"""Snapshot creation utilities."""

from typing import List, Optional, Set

# Root build/config files to include in snapshots.
# These files define workspace/module dependencies and version numbers.
# Including them ensures agent's code is self-consistent (e.g., crate versions
# match root Cargo.toml). SRS constrains agents not to add new external
# dependencies, so base image's pre-installed deps remain sufficient.
ROOT_BUILD_FILES: List[str] = [
    # Rust
    "Cargo.toml",
    "Cargo.lock",
]


def get_snapshot_paths(
    repo_src_dirs: List[str],
    existing_root_files: Optional[Set[str]] = None,
    existing_src_dirs: Optional[Set[str]] = None,
) -> List[str]:
    """Get all paths to include in snapshot.

    Includes source directories and root build files. Root build files ensure
    version consistency between agent's crate configs and workspace config.

    Args:
        repo_src_dirs: Source directories (e.g., ["src/", "crates/"])
        existing_root_files: Optional set of root build files that exist.
            If provided, only files in this set are included.
            If None, all root build files are included (legacy behavior).
        existing_src_dirs: Optional set of source directories that exist.
            If provided, only directories in this set are included.
            If None, all source directories are included (legacy behavior).

    Returns:
        List of paths for git archive command
    """
    # Filter source directories if existence check was performed
    if existing_src_dirs is not None:
        paths = [d for d in repo_src_dirs if d in existing_src_dirs]
    else:
        paths = list(repo_src_dirs)

    if existing_root_files is not None:
        # Only include root build files that exist
        for f in ROOT_BUILD_FILES:
            if f in existing_root_files:
                paths.append(f)
    else:
        # Legacy behavior: include all root build files
        paths.extend(ROOT_BUILD_FILES)

    return paths
