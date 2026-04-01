"""
Split test code from source code in milestone patches.

This module provides tools to:
1. Analyze milestone patches for test/src/mixed hunks
2. Use Claude agent to apply test changes and create new start tags
3. Regenerate clean patches:
   - milestone_patches/{m}.patch (src-only)
   - milestone_patches/start_diff_patches/{m}.patch (test-only)

Note: Currently only supports Rust projects.
"""

from .main import fix_baseline
from .analyzer import analyze_baseline

__all__ = ["fix_baseline", "analyze_baseline"]
