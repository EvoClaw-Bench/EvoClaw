"""
Test code region detection for Rust files.

Uses ast-grep for accurate parsing to detect:
- #[cfg(test)] blocks (mod, fn, impl, struct, enum, trait, const, static, type, use)
- #[cfg(test)] mod tests; (declarative/external modules)
- #[cfg_attr(test, ...)] items
- #![cfg(test)] file-level test-only modules
- #[test], #[bench] and other test framework attributes
- Doc tests (code blocks in /// or //! comments)
- Test-related macros (proptest!, macro_rules! with "test" in name)
"""

import re
import sys
import json
import subprocess
from pathlib import Path
from typing import List, Dict, Tuple, Optional


# ============================================================
# Helper functions for finding block boundaries
# ============================================================


def _find_module_end_with_brace_counting(lines: List[str], start_idx: int) -> Optional[int]:
    """Find the end of a module by counting braces, starting from start_idx.

    WARNING: This is a fallback method. It does NOT handle:
    - Braces inside strings: "{ fake }"
    - Braces inside comments: // { comment }
    - Character literals: '{'

    Prefer using ast-grep based methods which use proper parsing.
    """
    brace_count = 0
    found_open = False
    for k in range(start_idx, len(lines)):
        for char in lines[k]:
            if char == "{":
                brace_count += 1
                found_open = True
            elif char == "}":
                brace_count -= 1
        if found_open and brace_count == 0:
            return k + 1  # 1-indexed
    return None


def _get_item_ranges_from_ast_grep(
    file_path: str, pattern: str, filter_fn: Optional[callable] = None
) -> List[Tuple[int, int, str]]:
    """Use ast-grep to get precise item ranges.

    ast-grep properly handles strings, comments, and other edge cases
    that simple brace counting cannot handle.

    Args:
        file_path: Path to the Rust file
        pattern: ast-grep pattern (e.g., "mod $NAME { $$$ }")
        filter_fn: Optional function to filter matches (receives match dict)

    Returns:
        List of (start_line, end_line, matched_text) tuples (1-indexed)
    """
    try:
        result = subprocess.run(
            ["ast-grep", "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        matches = json.loads(result.stdout)
        ranges = []

        for match in matches:
            if filter_fn and not filter_fn(match):
                continue

            start_line = match["range"]["start"]["line"] + 1  # 0-indexed to 1-indexed
            end_line = match["range"]["end"]["line"] + 1
            text = match.get("text", "")

            ranges.append((start_line, end_line, text))

        return ranges

    except Exception:
        return []


def _get_block_items_with_precise_ranges(file_path: str) -> Dict[str, List[Tuple[int, int]]]:
    """Get precise ranges for block items (mod, impl, fn, trait, struct, enum).

    Uses ast-grep for accurate parsing that handles strings/comments correctly.

    Returns:
        Dict mapping item type to list of (start_line, end_line) tuples (1-indexed)
    """
    items = {
        "mod": [],
        "impl": [],
        "fn": [],
        "trait": [],
        "struct": [],
        "enum": [],
    }

    # Pattern for each item type
    # Note: fn pattern uses $$$RET to match optional return type (e.g., "-> Type")
    patterns = {
        "mod": "mod $NAME { $$$ }",
        "impl": "impl $TYPE { $$$ }",
        "fn": "fn $NAME($$$) $$$RET { $$$ }",
        "trait": "trait $NAME { $$$ }",
        "struct": "struct $NAME { $$$ }",
        "enum": "enum $NAME { $$$ }",
    }

    for item_type, pattern in patterns.items():
        ranges = _get_item_ranges_from_ast_grep(file_path, pattern)
        items[item_type] = [(start, end) for start, end, _ in ranges]

    return items


def _find_item_end_from_ranges(
    item_line: int, item_ranges: List[Tuple[int, int]], tolerance: int = 20
) -> Optional[int]:
    """Find the end line for an item given its start line using pre-computed ranges.

    Args:
        item_line: The line number where the item starts (1-indexed)
        item_ranges: List of (start_line, end_line) tuples from ast-grep
        tolerance: How many lines ahead to search for the item

    Returns:
        The end line number, or None if not found
    """
    for start, end in item_ranges:
        # Allow the item to start at item_line or a few lines after
        # (to account for attributes)
        if item_line <= start <= item_line + tolerance:
            return end
    return None


def _find_block_end(
    item_line: int,
    item_type: str,
    block_ranges: Dict[str, List[Tuple[int, int]]],
    lines: List[str],
    fallback: bool = True,
) -> Optional[int]:
    """Find the end of a block item using ast-grep ranges with fallback.

    Args:
        item_line: The 0-indexed line where the item starts
        item_type: Type of item (mod, impl, fn, trait, struct, enum)
        block_ranges: Pre-computed ranges from _get_block_items_with_precise_ranges
        lines: File lines for fallback brace counting
        fallback: Whether to fall back to brace counting if ast-grep fails

    Returns:
        1-indexed end line number, or None if not found
    """
    # Convert to 1-indexed for range lookup
    item_line_1idx = item_line + 1

    if item_type in block_ranges:
        end_line = _find_item_end_from_ranges(item_line_1idx, block_ranges[item_type])
        if end_line:
            return end_line

    # Fallback to brace counting (less accurate but works when ast-grep fails)
    if fallback:
        return _find_module_end_with_brace_counting(lines, item_line)

    return None


# ============================================================
# Helper functions for parsing Rust syntax
# ============================================================


def _strip_visibility(line: str) -> str:
    """Remove visibility modifiers from a line.

    Handles: pub, pub(crate), pub(super), pub(self), pub(in path)
    """
    stripped = line.strip()
    # Match pub, pub(crate), pub(super), pub(self), pub(in ...)
    vis_pattern = r"^pub\s*(\([^)]*\))?\s*"
    return re.sub(vis_pattern, "", stripped)


def _strip_leading_attrs_from_line(line: str) -> Tuple[str, bool]:
    """Strip leading attributes from a single line.

    Returns (remainder, complete). If complete is False, an attribute starts
    on this line but does not close on the same line.
    """
    i = 0
    n = len(line)
    while True:
        while i < n and line[i].isspace():
            i += 1
        if line.startswith("#[", i) or line.startswith("#![", i):
            close = line.find("]", i + 2)
            if close == -1:
                return "", False
            i = close + 1
            continue
        break
    return line[i:].lstrip(), True


def _strip_leading_attrs(line: str) -> str:
    """Return line content after stripping any leading same-line attributes."""
    remainder, complete = _strip_leading_attrs_from_line(line)
    if not complete:
        return ""
    return remainder


def _skip_to_item(lines: List[str], start_idx: int, max_lines: int = 20) -> int:
    """Skip empty lines and attribute lines to find the actual item.

    Returns the index of the first non-attribute, non-empty line.
    """
    j = start_idx
    in_attr_block = False
    while j < len(lines) and j < start_idx + max_lines:
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            j += 1
            continue
        if in_attr_block:
            if "]" in line:
                # Attribute block ended; check for inline item after closing bracket
                remainder, complete = _strip_leading_attrs_from_line(line.split("]", 1)[1])
                remainder = remainder.lstrip()
                if complete and remainder and not remainder.startswith(("//", "/*")):
                    return j
                in_attr_block = False
            j += 1
            continue
        if stripped.startswith("#[") or stripped.startswith("#!["):
            remainder, complete = _strip_leading_attrs_from_line(line)
            remainder = remainder.lstrip()
            if complete and remainder and not remainder.startswith(("//", "/*")):
                return j
            if not complete:
                in_attr_block = True
            j += 1
            continue
        if stripped.startswith("///") or stripped.startswith("//!"):
            j += 1
            continue
        # Found the item
        break
    return j


def _is_fn_line(line: str) -> bool:
    """Check if line starts a function definition."""
    stripped = _strip_leading_attrs(line)
    if not stripped:
        return False
    stripped = _strip_visibility(stripped)
    return (
        stripped.startswith("fn ")
        or stripped.startswith("async fn ")
        or stripped.startswith("const fn ")
        or stripped.startswith("unsafe fn ")
        or stripped.startswith("extern fn ")
        or stripped.startswith("async unsafe fn ")
    )


def _find_single_statement_end(lines: List[str], start_idx: int) -> int:
    """Find end of a single statement (use, const, static, type alias).

    Handles multi-line statements by looking for the semicolon.
    """
    for k in range(start_idx, min(start_idx + 50, len(lines))):
        if ";" in lines[k]:
            return k + 1  # 1-indexed
    return start_idx + 1  # Fallback to single line


def _find_first_attr_line(lines: List[str], attr_line: int) -> int:
    """Find the first attribute line by looking backwards.

    When we find #[test], there may be other attributes above it like
    #[ignore], #[should_panic], etc. This function finds the first one.

    Returns 0-indexed line number.
    """
    first = attr_line
    k = attr_line - 1
    while k >= 0:
        stripped = lines[k].strip()
        if not stripped:
            # Empty line - continue looking
            k -= 1
            continue
        if stripped.startswith("#["):
            # Another attribute - include it
            first = k
            k -= 1
        else:
            # Non-attribute, non-empty line - stop
            break
    return first


def _extract_parenthesized_content(text: str, open_idx: int) -> Optional[str]:
    """Extract content inside matching parentheses starting at open_idx."""
    depth = 0
    in_string = None
    i = open_idx
    start = None
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
            if depth == 1:
                start = i + 1
        elif ch == ")":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start:i]
        i += 1
    return None


def _split_top_level_args(text: str) -> List[str]:
    """Split a comma-separated argument list at top level (no nested parens)."""
    args = []
    depth = 0
    in_string = None
    start = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth > 0:
                depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
        i += 1
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _tokenize_cfg_expr(text: str) -> List[Tuple[str, str]]:
    """Tokenize a cfg expression for a minimal parser."""
    tokens = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(text) and (text[i].isalnum() or text[i] == "_"):
                i += 1
            tokens.append(("IDENT", text[start:i]))
            continue
        if ch in ("(", ")", ",", "="):
            tokens.append((ch, ch))
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == quote:
                    i += 1
                    break
                i += 1
            tokens.append(("STRING", ""))
            continue
        i += 1
    return tokens


def _parse_cfg_meta(tokens: List[Tuple[str, str]], pos: int) -> Tuple[Optional[Tuple], int]:
    """Parse a cfg meta item from tokens."""
    if pos >= len(tokens) or tokens[pos][0] != "IDENT":
        return None, pos
    name = tokens[pos][1]
    pos += 1
    if pos < len(tokens) and tokens[pos][0] == "=":
        pos += 1
        if pos < len(tokens) and tokens[pos][0] in ("IDENT", "STRING"):
            pos += 1
        return ("name_value", name), pos
    if pos < len(tokens) and tokens[pos][0] == "(":
        pos += 1
        args = []
        while pos < len(tokens) and tokens[pos][0] != ")":
            if tokens[pos][0] == ",":
                pos += 1
                continue
            node, pos = _parse_cfg_meta(tokens, pos)
            if node is not None:
                args.append(node)
            else:
                pos += 1
        if pos < len(tokens) and tokens[pos][0] == ")":
            pos += 1
        return ("list", name, args), pos
    return ("word", name), pos


def _cfg_meta_contains_test(node: Tuple, negated: bool = False) -> bool:
    """Return True if cfg meta contains a positive test word."""
    if not node:
        return False
    if node[0] == "word":
        return node[1] == "test" and not negated
    if node[0] == "name_value":
        return False
    if node[0] == "list":
        name = node[1]
        args = node[2]
        if name == "not":
            return _cfg_meta_contains_test(args[0], not negated) if args else False
        if name in ("any", "all"):
            return any(_cfg_meta_contains_test(arg, negated) for arg in args)
        return False
    return False


def _cfg_expr_has_test(expr: str) -> bool:
    """Check if a cfg expression includes positive 'test'."""
    tokens = _tokenize_cfg_expr(expr)
    node, _ = _parse_cfg_meta(tokens, 0)
    if node is None:
        return False
    return _cfg_meta_contains_test(node, False)


def _extract_cfg_condition(attr_text: str, attr_name: str) -> Optional[str]:
    """Extract cfg or cfg_attr condition expression from attribute text."""
    m = re.search(r"\b" + re.escape(attr_name) + r"\s*\(", attr_text)
    if not m:
        return None
    content = _extract_parenthesized_content(attr_text, m.end() - 1)
    if content is None:
        return None
    if attr_name == "cfg_attr":
        parts = _split_top_level_args(content)
        if not parts:
            return None
        return parts[0]
    return content


def _is_cfg_test_attr(attr_text: str, attr_name: str) -> bool:
    """Return True if cfg/cfg_attr attribute condition includes positive test."""
    cond = _extract_cfg_condition(attr_text, attr_name)
    if not cond:
        return False
    return _cfg_expr_has_test(cond)


def _has_item_after_column(line: str, col: int) -> bool:
    """Return True if there's an item start after col on the line."""
    if col < 0 or col >= len(line):
        return False
    remainder = line[col:].lstrip()
    if not remainder or remainder.startswith("//") or remainder.startswith("/*"):
        return False
    remainder, complete = _strip_leading_attrs_from_line(remainder)
    if not complete:
        return False
    remainder = remainder.lstrip()
    if not remainder:
        return False
    no_vis = _strip_visibility(remainder)
    return (
        no_vis.startswith("mod ")
        or no_vis.startswith("use ")
        or no_vis.startswith("impl ")
        or no_vis.startswith("const ")
        or no_vis.startswith("static ")
        or no_vis.startswith("type ")
        or no_vis.startswith("trait ")
        or no_vis.startswith("struct ")
        or no_vis.startswith("enum ")
        or _is_fn_line(remainder)
    )


# ============================================================
# Doc test detection
# ============================================================


def _find_doc_test_ranges(lines: List[str]) -> List[Tuple[int, int, str]]:
    """Find doc test code blocks in documentation comments.

    Doc tests are code examples in /// or //! comments that get executed by `cargo test --doc`.
    They are marked by triple backticks: ```rust or just ```

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    ranges = []
    i = 0
    in_doc_block = False
    block_start = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Check for doc comment lines
        is_doc_comment = stripped.startswith("///") or stripped.startswith("//!")

        if is_doc_comment:
            # Extract content after the doc comment marker (/// or //! are both 3 chars)
            content = stripped[3:].strip()

            # Check for code block markers
            if content.startswith("```"):
                if not in_doc_block:
                    # Start of doc test block
                    in_doc_block = True
                    block_start = i
                else:
                    # End of doc test block
                    in_doc_block = False
                    ranges.append((block_start + 1, i + 1, "doc test"))

        elif in_doc_block:
            # Non-doc-comment line while in a doc block means the block ended
            # (shouldn't normally happen in valid Rust)
            in_doc_block = False

        i += 1

    return ranges


# ============================================================
# Macro test detection
# ============================================================


def _find_macro_test_ranges(file_path: str) -> List[Tuple[int, int, str]]:
    """Find test-related macro invocations.

    Detects:
    - Common test macro invocations (e.g., test!, test_case!, proptest!)

    NOTE: We intentionally do NOT detect macro_rules! definitions even if their
    name contains "test". A macro_rules! definition is just a definition, not
    actual test code. The #[test] functions inside the macro body only become
    real tests when the macro is invoked. Detecting macro_rules! definitions
    would incorrectly remove them, breaking code that depends on those macros.

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    ranges = []

    # Common test macro invocations
    test_macro_patterns = [
        "proptest! { $$$ }",
        "test! { $$$ }",
        "test_case! { $$$ }",
        "quickcheck! { $$$ }",
    ]

    for pattern in test_macro_patterns:
        try:
            result = subprocess.run(
                ["ast-grep", "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                matches = json.loads(result.stdout)
                for match in matches:
                    start_line = match["range"]["start"]["line"] + 1
                    end_line = match["range"]["end"]["line"] + 1
                    macro_name = pattern.split("!")[0]
                    ranges.append((start_line, end_line, f"{macro_name}! macro"))
        except Exception:
            pass

    return ranges


# ============================================================
# Root-level detection using declaration_list
# ============================================================


def _find_declaration_list_ranges(file_path: str) -> List[Tuple[int, int]]:
    """
    Find all declaration_list ranges in a Rust file using ast-grep.

    declaration_list is the content body of mod, impl, and trait blocks.
    Any item inside a declaration_list is NOT at file root level.

    This provides a unified way to detect nesting - instead of checking
    for each block type separately (mod, impl, trait), we just check if
    something is inside any declaration_list.

    Args:
        file_path: Path to the Rust file

    Returns:
        List of (start_line, end_line) tuples (1-indexed, inclusive)
    """
    import tempfile
    import os

    try:
        # Create a YAML rule file for finding declaration_list nodes
        rule_content = """id: find-declaration-list
language: rust
rule:
  kind: declaration_list
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(rule_content)
            rule_path = f.name

        try:
            result = subprocess.run(
                ["ast-grep", "scan", "-r", rule_path, "--json", file_path],
                capture_output=True,
                text=True,
                timeout=10,
            )

            ranges = []
            if result.returncode == 0 and result.stdout.strip():
                try:
                    matches = json.loads(result.stdout)
                    for match in matches:
                        start_line = match["range"]["start"]["line"] + 1  # Convert to 1-indexed
                        end_line = match["range"]["end"]["line"] + 1
                        ranges.append((start_line, end_line))
                except json.JSONDecodeError:
                    pass

            return ranges
        finally:
            os.unlink(rule_path)

    except Exception:
        return []


def _is_inside_any_block(line_number: int, block_ranges: List[Tuple[int, int]]) -> bool:
    """
    Check if a line is inside any block (declaration_list).

    Args:
        line_number: 1-indexed line number
        block_ranges: List of (start, end) tuples for declaration_list blocks

    Returns:
        True if the line is inside any block (not at file root level)
    """
    for start, end in block_ranges:
        if start <= line_number <= end:
            return True
    return False


# ============================================================
# Range merging
# ============================================================


def _merge_overlapping_ranges(ranges: List[Tuple[int, int, str]]) -> List[Tuple[int, int, str]]:
    """Merge overlapping ranges, keeping the largest."""
    if not ranges:
        return []

    # Sort by start line
    sorted_ranges = sorted(ranges, key=lambda x: (x[0], -x[1]))

    merged = [sorted_ranges[0]]
    for start, end, reason in sorted_ranges[1:]:
        last_start, last_end, last_reason = merged[-1]
        if start <= last_end:
            # Overlapping - keep the larger range
            if end > last_end:
                merged[-1] = (last_start, end, last_reason)
        else:
            merged.append((start, end, reason))

    return merged


# ============================================================
# Main detection function
# ============================================================


def find_test_code_ranges(
    file_path: str, include_doc_tests: bool = False, only_root_level: bool = False
) -> List[Tuple[int, int, str]]:
    """
    Find all test code regions in a Rust file using ast-grep.

    Detects:
    - #[cfg(test)] and #[cfg(all/any(test, ...))] blocks
    - #[cfg(test)] mod/use/fn/impl/const/static/type statements
    - #[cfg(test)] mod tests; (declarative/external module)
    - #[cfg_attr(test, ...)] items and #![cfg(test)] file-level modules
    - #[test], #[bench] functions
    - Async test frameworks: tokio, async_std, actix_rt, smol, etc.
    - Other test frameworks: rstest, quickcheck, test_case, wasm_bindgen_test
    - Doc tests (code blocks in /// or //! comments) - optional, disabled by default
    - Test-related macros (proptest!, test!, macro_rules! with "test" in name)

    Args:
        file_path: Path to the Rust source file
        include_doc_tests: Whether to include doc tests (code blocks in /// comments).
            Default False because doc tests are typically part of source documentation
            and their changes are coupled with API changes, so they should be treated
            as src code for test/src separation purposes.
        only_root_level: If True, only return test regions at file root level.
            Test regions nested inside mod/impl/trait blocks are excluded.
            This prevents extraction of nested tests that would lose context
            when moved to file end (causing E0428, E0061 errors).

    Returns list of (start_line, end_line, reason) tuples (1-indexed).
    """
    if not Path(file_path).exists() or not file_path.endswith(".rs"):
        return []

    try:
        with open(file_path, "r") as f:
            lines = f.readlines()

        ranges = []

        # Pre-compute block ranges using ast-grep for precise end detection
        # This avoids the pitfalls of simple brace counting (strings, comments, etc.)
        block_ranges = _get_block_items_with_precise_ranges(file_path)

        def _item_start_after_attr(match: Dict) -> int:
            end_line = match["range"]["end"]["line"]
            end_col = match["range"]["end"].get("column", 0)
            if end_line < len(lines) and _has_item_after_column(lines[end_line], end_col):
                return _skip_to_item(lines, end_line)
            return _skip_to_item(lines, end_line + 1)

        # ============================================================
        # Pattern Group 1: #[cfg(...test...)] - various item types
        # ============================================================
        # Use ast-grep to find #[cfg($$$)] and filter for test-related
        # Note: $$$ matches multiple tokens, needed for cfg(all(test, ...))
        #
        # IMPORTANT: Only #[cfg(test)] makes an item test-only.
        # #[cfg_attr(test, X)] does NOT make the item test-only - it just adds
        # attribute X during tests. The item itself exists in all builds.
        # Example: #[cfg_attr(test, derive(EnumIter))] pub enum Type { ... }
        #   - The enum exists in release builds (without EnumIter)
        #   - The enum exists in test builds (with EnumIter)
        #   - Removing this would break the build!
        for attr_name in ("cfg",):  # Removed "cfg_attr" - it doesn't make items test-only
            result = subprocess.run(
                ["ast-grep", "run", "--pattern", f"#[{attr_name}($$$)]", "--lang", "rust", "--json", file_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                matches = json.loads(result.stdout)
                for match in matches:
                    matched_text = match.get("text", "")
                    if not _is_cfg_test_attr(matched_text, attr_name):
                        continue

                    cfg_line = match["range"]["start"]["line"]  # 0-indexed
                    j = _item_start_after_attr(match)
                    if j >= len(lines):
                        continue

                    next_line = _strip_leading_attrs(lines[j]).strip()
                    if not next_line:
                        continue
                    next_line_no_vis = _strip_visibility(next_line)
                    start_line = cfg_line + 1  # 1-indexed

                    # Determine item type and find end using precise ast-grep ranges
                    if next_line_no_vis.startswith("mod "):
                        # Check for declarative module: mod tests; (semicolon, no braces)
                        # This means the test code is in a separate file (tests.rs or tests/mod.rs)
                        if ";" in next_line and "{" not in next_line:
                            end_line = _find_single_statement_end(lines, j)
                            ranges.append((start_line, end_line, f"#[{attr_name}(test)] mod (external)"))
                        else:
                            end_line = _find_block_end(j, "mod", block_ranges, lines)
                            if end_line:
                                ranges.append((start_line, end_line, f"#[{attr_name}(test)] mod"))

                    elif next_line_no_vis.startswith("use "):
                        end_line = _find_single_statement_end(lines, j)
                        ranges.append((start_line, end_line, f"#[{attr_name}(test)] use"))

                    elif _is_fn_line(lines[j]):
                        end_line = _find_block_end(j, "fn", block_ranges, lines)
                        if end_line:
                            ranges.append((start_line, end_line, f"#[{attr_name}(test)] fn"))

                    elif next_line_no_vis.startswith("impl "):
                        end_line = _find_block_end(j, "impl", block_ranges, lines)
                        if end_line:
                            ranges.append((start_line, end_line, f"#[{attr_name}(test)] impl"))

                    elif next_line_no_vis.startswith("const "):
                        end_line = _find_single_statement_end(lines, j)
                        ranges.append((start_line, end_line, f"#[{attr_name}(test)] const"))

                    elif next_line_no_vis.startswith("static "):
                        end_line = _find_single_statement_end(lines, j)
                        ranges.append((start_line, end_line, f"#[{attr_name}(test)] static"))

                    elif next_line_no_vis.startswith("type "):
                        end_line = _find_single_statement_end(lines, j)
                        ranges.append((start_line, end_line, f"#[{attr_name}(test)] type"))

                    elif next_line_no_vis.startswith("trait "):
                        end_line = _find_block_end(j, "trait", block_ranges, lines)
                        if end_line:
                            ranges.append((start_line, end_line, f"#[{attr_name}(test)] trait"))

                    elif next_line_no_vis.startswith("struct "):
                        # Struct can be single-line or multi-line
                        if "{" in next_line:
                            end_line = _find_block_end(j, "struct", block_ranges, lines)
                        else:
                            end_line = _find_single_statement_end(lines, j)
                        if end_line:
                            ranges.append((start_line, end_line, f"#[{attr_name}(test)] struct"))

                    elif next_line_no_vis.startswith("enum "):
                        end_line = _find_block_end(j, "enum", block_ranges, lines)
                        if end_line:
                            ranges.append((start_line, end_line, f"#[{attr_name}(test)] enum"))

        # Inner attribute: #![cfg(test)] marks entire file as test-only
        result = subprocess.run(
            ["ast-grep", "run", "--pattern", "#![cfg($$$)]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                matched_text = match.get("text", "")
                if _is_cfg_test_attr(matched_text, "cfg"):
                    ranges.append((1, len(lines), "#![cfg(test)] file"))

        # ============================================================
        # Pattern Group 2: Test function attributes
        # ============================================================
        test_fn_attrs = [
            # Standard test attributes
            ("test", False),
            ("bench", False),
            # Async runtime test attributes
            ("tokio::test", True),
            ("async_std::test", True),
            ("actix_rt::test", True),
            ("smol_potat::test", True),
            ("futures_test::test", True),
            # Other test frameworks
            ("rstest", True),
            ("quickcheck", True),
            ("wasm_bindgen_test", True),
        ]

        for attr_name, allow_args in test_fn_attrs:
            patterns = [f"#[{attr_name}]"]
            if allow_args:
                patterns.append(f"#[{attr_name}($$$)]")

            for pattern in patterns:
                result = subprocess.run(
                    ["ast-grep", "run", "--pattern", pattern, "--lang", "rust", "--json", file_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout.strip():
                    matches = json.loads(result.stdout)
                    for match in matches:
                        attr_line = match["range"]["start"]["line"]  # 0-indexed

                        # Skip to the function definition
                        j = _item_start_after_attr(match)

                        if j >= len(lines):
                            continue

                        # Verify this is a function
                        if _is_fn_line(lines[j]):
                            # Find first attribute (there may be #[ignore] etc. above #[test])
                            first_attr = _find_first_attr_line(lines, attr_line)
                            start_line = first_attr + 1  # 1-indexed
                            end_line = _find_block_end(j, "fn", block_ranges, lines)
                            if end_line:
                                ranges.append((start_line, end_line, f"#[{attr_name}] fn"))

        # ============================================================
        # Pattern Group 3: Parameterized test attributes (with arguments)
        # ============================================================
        # #[test_case(...)] - need to match with arguments
        result = subprocess.run(
            ["ast-grep", "run", "--pattern", "#[test_case($$$)]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            # Group by function (multiple test_case on same fn)
            processed_fns = set()
            for match in matches:
                attr_line = match["range"]["start"]["line"]

                # Skip to function
                j = _item_start_after_attr(match)
                if j >= len(lines) or j in processed_fns:
                    continue

                if _is_fn_line(lines[j]):
                    processed_fns.add(j)
                    # Find the first test_case attribute for this function
                    first_attr = attr_line
                    k = attr_line - 1
                    while k >= 0:
                        stripped = lines[k].strip()
                        if stripped.startswith("#[test_case") or stripped.startswith("#[case"):
                            first_attr = k
                            k -= 1
                        elif stripped.startswith("#[") or not stripped:
                            k -= 1
                        else:
                            break

                    start_line = first_attr + 1
                    end_line = _find_block_end(j, "fn", block_ranges, lines)
                    if end_line:
                        ranges.append((start_line, end_line, "#[test_case] fn"))

        # #[rstest] #[case(...)] combinations
        result = subprocess.run(
            ["ast-grep", "run", "--pattern", "#[case($$$)]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            processed_fns = set()
            for match in matches:
                attr_line = match["range"]["start"]["line"]
                j = _item_start_after_attr(match)
                if j >= len(lines) or j in processed_fns:
                    continue

                if _is_fn_line(lines[j]):
                    processed_fns.add(j)
                    # Find first attribute
                    first_attr = attr_line
                    k = attr_line - 1
                    while k >= 0:
                        stripped = lines[k].strip()
                        if stripped.startswith("#["):
                            first_attr = k
                            k -= 1
                        elif not stripped:
                            k -= 1
                        else:
                            break

                    start_line = first_attr + 1
                    end_line = _find_block_end(j, "fn", block_ranges, lines)
                    if end_line:
                        ranges.append((start_line, end_line, "#[rstest/case] fn"))

        # #[fixture] for rstest
        result = subprocess.run(
            ["ast-grep", "run", "--pattern", "#[fixture]", "--lang", "rust", "--json", file_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            matches = json.loads(result.stdout)
            for match in matches:
                attr_line = match["range"]["start"]["line"]
                j = _item_start_after_attr(match)
                if j >= len(lines):
                    continue

                if _is_fn_line(lines[j]):
                    start_line = attr_line + 1
                    end_line = _find_block_end(j, "fn", block_ranges, lines)
                    if end_line:
                        ranges.append((start_line, end_line, "#[fixture] fn"))

        # ============================================================
        # Pattern Group 4: Doc tests (code blocks in documentation)
        # ============================================================
        # Only include doc tests if explicitly requested.
        # Doc tests are part of source documentation and their changes are
        # typically coupled with API changes, so by default we treat them as src.
        if include_doc_tests:
            doc_test_ranges = _find_doc_test_ranges(lines)
            ranges.extend(doc_test_ranges)

        # ============================================================
        # Pattern Group 5: Test-related macros
        # ============================================================
        macro_test_ranges = _find_macro_test_ranges(file_path)
        ranges.extend(macro_test_ranges)

        # Merge overlapping ranges
        ranges = _merge_overlapping_ranges(ranges)

        # Filter to only root-level test regions if requested
        if only_root_level:
            declaration_list_ranges = _find_declaration_list_ranges(file_path)
            ranges = [
                (start, end, reason)
                for start, end, reason in ranges
                if not _is_inside_any_block(start, declaration_list_ranges)
            ]

        return ranges

    except Exception as e:
        # Log error for debugging but return empty list
        print(f"Warning: find_test_code_ranges failed for {file_path}: {e}", file=sys.stderr)
        return []


def find_test_module_ranges(file_path: str, include_doc_tests: bool = False) -> List[Tuple[int, int]]:
    """
    Find all test code regions in a Rust file.

    Args:
        file_path: Path to the Rust source file
        include_doc_tests: Whether to include doc tests. Default False.

    Returns list of (start_line, end_line) tuples (1-indexed).
    """
    ranges = find_test_code_ranges(file_path, include_doc_tests=include_doc_tests)
    return [(start, end) for start, end, _ in ranges]


def find_test_ranges_from_content(
    content: str, file_path: str, include_doc_tests: bool = False, only_root_level: bool = False
) -> List[Tuple[int, int]]:
    """Find test code ranges from file content string.

    Uses ast-grep for accurate parsing by writing content to a temp file.
    Falls back to simple text parsing if ast-grep is unavailable.

    Args:
        content: File content as a string
        file_path: Original file path (used to determine file type)
        include_doc_tests: Whether to include doc tests. Default False.
        only_root_level: If True, only return test regions at file root level.
            Test regions nested inside mod/impl/trait blocks are excluded.
    """
    if not file_path.endswith(".rs"):
        return []

    import tempfile
    import os

    # Try ast-grep approach first (more accurate)
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".rs", delete=False, encoding="utf-8") as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            ranges = find_test_code_ranges(
                tmp_path, include_doc_tests=include_doc_tests, only_root_level=only_root_level
            )
            return [(start, end) for start, end, _ in ranges]
        finally:
            os.unlink(tmp_path)

    except Exception:
        # Fallback to simple text parsing
        pass

    # Fallback: simple text parsing for #[cfg(test)] mod blocks
    lines = content.split("\n")
    ranges = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#![cfg") and _is_cfg_test_attr(line, "cfg"):
            ranges.append((1, len(lines)))
            return ranges

    i = 0
    while i < len(lines):
        raw_line = lines[i]
        stripped = raw_line.strip()
        if stripped.startswith("#[cfg") or stripped.startswith("#[cfg_attr"):
            attr_name = "cfg_attr" if stripped.startswith("#[cfg_attr") else "cfg"
            if not _is_cfg_test_attr(raw_line, attr_name):
                i += 1
                continue

            remainder, complete = _strip_leading_attrs_from_line(raw_line)
            remainder = remainder.lstrip()
            if complete and remainder and not remainder.startswith(("//", "/*")):
                j = i
            else:
                j = _skip_to_item(lines, i + 1)

            if j < len(lines):
                next_line = _strip_leading_attrs(lines[j])
                next_line = _strip_visibility(next_line)
                if next_line.startswith("mod "):
                    start_line = i + 1  # 1-indexed
                    end_line = _find_module_end_with_brace_counting(lines, j)
                    if end_line:
                        ranges.append((start_line, end_line))
                        i = end_line - 1
        i += 1

    return ranges
