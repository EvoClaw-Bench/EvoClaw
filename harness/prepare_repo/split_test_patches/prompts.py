#!/usr/bin/env python3
"""
Prompt templates for Claude agent to apply test changes.
"""

APPLY_TEST_HUNKS_PROMPT = """
You are a code modification expert working on a Rust codebase. Your task is to apply ONLY test-related code changes from a patch, leaving source code unchanged.

## Current State
- Repository path: {testbed_path}
- Current git tag: {start_old_tag}
- Target: Apply test hunks to create new start point

## What is Test Code in Rust?
Test code includes:
1. **Test directory files**: Any file in `tests/` directory
2. **Test modules**: Code inside `#[cfg(test)]` modules
3. **Test functions**: Functions annotated with `#[test]`, `#[bench]`, `#[tokio::test]`, `#[async_std::test]`, etc.
4. **Test helper code**: Code inside `mod tests {{ ... }}` blocks

## Test Hunks to Apply

The following hunks have been identified as test-related. Apply these changes:

{test_hunks_section}

## Mixed Hunks (Need Intelligent Splitting)

The following hunks contain BOTH test and source code. You need to extract and apply ONLY the test portions:

{mixed_hunks_section}

## Instructions

1. **Read each file** mentioned in the hunks above
2. **For pure test hunks**: Apply the entire hunk content to the file
3. **For mixed hunks**:
   - Identify which lines are test code (inside #[cfg(test)], #[test] functions, tests/ directory)
   - Apply ONLY those test-related lines
   - Do NOT apply source code changes
4. **After all changes**: Run these commands:
   ```bash
   cd {testbed_path}
   git add -A
   git commit -m "Add test code for {milestone}"
   ```

## Important Rules

- ONLY modify test-related code
- Preserve exact whitespace and formatting from the patch
- If a file doesn't exist, it might be a new test file - create it with the test content
- Do NOT modify any source code (non-test code)
- If unsure whether something is test code, err on the side of NOT applying it

## Original Patch Reference

For context, here is the full original patch content:

```diff
{patch_content}
```

Now, apply the test changes. Start by reading the files that need to be modified.
"""


def format_hunk_section(hunks: list) -> str:
    """Format a list of hunks for prompt display."""
    if not hunks:
        return "(none)"

    lines = []
    for i, hunk in enumerate(hunks, 1):
        lines.append(f"### Hunk {i}: {hunk['file']}")
        lines.append(f"- Lines: {hunk.get('old_lines', 'N/A')} -> {hunk.get('new_lines', 'N/A')}")
        if hunk.get("reason"):
            lines.append(f"- Reason: {hunk['reason']}")
        if hunk.get("test_lines") and hunk.get("src_lines"):
            lines.append(f"- Contains: {hunk['test_lines']} test lines, {hunk['src_lines']} src lines")
        lines.append(f"```diff\n{hunk.get('content', '')}\n```")
        lines.append("")

    return "\n".join(lines)


def generate_apply_test_prompt(
    testbed_path: str, milestone: str, start_old_tag: str, test_hunks: list, mixed_hunks: list, patch_content: str
) -> str:
    """
    Generate the prompt for Claude to apply test hunks.

    Args:
        testbed_path: Path to the testbed git repository
        milestone: Milestone ID
        start_old_tag: The old start tag name
        test_hunks: List of pure test hunk details
        mixed_hunks: List of mixed hunk details
        patch_content: Full original patch content

    Returns:
        Formatted prompt string
    """
    return APPLY_TEST_HUNKS_PROMPT.format(
        testbed_path=testbed_path,
        start_old_tag=start_old_tag,
        milestone=milestone,
        test_hunks_section=format_hunk_section(test_hunks),
        mixed_hunks_section=format_hunk_section(mixed_hunks),
        patch_content=patch_content,
    )


# Retry prompt when verification fails
RETRY_TEST_HUNKS_PROMPT = """
You are a code modification expert working on a Rust codebase. Your previous attempt to separate test code from source code FAILED verification. You need to fix the issues.

## Previous Attempt Failed

**Milestone**: {milestone}
**Attempt**: {attempt_number}

### Verification Results:

{verification_results}

## Current State
- Repository path: {testbed_path}
- Current git tag: {start_old_tag} (reset to original state)
- Target: Apply ONLY test hunks to create new start point

## Your Task

You need to ensure:
1. **milestone_patches/start_diff_patches** (changes from start-old to start) contains ONLY test code
2. **milestone_patches** (changes from start to end) contains ONLY source code

This means you must apply all test-related changes NOW (to the start point), so they won't appear in the milestone_patches later.

## Test Hunks to Apply

{test_hunks_section}

## Mixed Hunks (Need Careful Splitting)

{mixed_hunks_section}

## Instructions

1. **Explore the codebase**: Use `git log`, `git show`, read files to understand the current state and what changes need to be made
2. **Handle file renames**: If a file was renamed in the original patch, find the corresponding file in the current state
3. **Apply test changes**: Add/modify test code in the appropriate locations
4. **Commit**: Run `git add -A && git commit -m "Add test code for {milestone}"`

Now, explore the codebase and apply the test changes.
"""


def generate_retry_prompt(
    testbed_path: str,
    milestone: str,
    start_old_tag: str,
    test_hunks: list,
    mixed_hunks: list,
    patch_content: str,
    attempt_number: int,
    failure_details: dict,
) -> str:
    """
    Generate retry prompt with failure details from previous attempt.

    Args:
        testbed_path: Path to the testbed git repository
        milestone: Milestone ID
        start_old_tag: The old start tag name
        test_hunks: List of pure test hunk details
        mixed_hunks: List of mixed hunk details
        patch_content: Full original patch content
        attempt_number: Current attempt number (2, 3, ...)
        failure_details: Dict with verification failure info

    Returns:
        Formatted retry prompt string
    """
    # Format verification results for both patches
    result_lines = []

    # milestone_patches results
    result_lines.append("**milestone_patches** (start → end, should contain ONLY source code):")
    if failure_details.get("src_patch_issues"):
        result_lines.append("  ❌ FAILED - contains test code that should have been applied earlier:")
        for issue in failure_details["src_patch_issues"]:
            result_lines.append(f"    - {issue}")
    else:
        result_lines.append("  ✅ PASSED")

    result_lines.append("")

    # milestone_patches/start_diff_patches results
    result_lines.append("**milestone_patches/start_diff_patches** (start-old → start, should contain ONLY test code):")
    if failure_details.get("test_patch_issues"):
        result_lines.append("  ❌ FAILED - contains non-test code:")
        for issue in failure_details["test_patch_issues"]:
            result_lines.append(f"    - {issue}")
    else:
        result_lines.append("  ✅ PASSED")

    verification_results = "\n".join(result_lines)

    return RETRY_TEST_HUNKS_PROMPT.format(
        testbed_path=testbed_path,
        start_old_tag=start_old_tag,
        milestone=milestone,
        attempt_number=attempt_number,
        verification_results=verification_results,
        test_hunks_section=format_hunk_section(test_hunks),
        mixed_hunks_section=format_hunk_section(mixed_hunks),
    )


# Verification prompt to check if test changes were applied correctly
VERIFY_TEST_CHANGES_PROMPT = """
You are verifying that test code changes were correctly applied to a Rust codebase.

## Context
- Repository: {testbed_path}
- Milestone: {milestone}
- Expected test changes were applied

## Verification Steps

1. Run `git status` to see staged/unstaged changes
2. Run `git diff --cached` to see what will be committed
3. Verify that:
   - All changes are in test-related locations (tests/, #[cfg(test)] blocks, #[test] functions)
   - No source code was modified
   - Changes match the expected test hunks

## Expected Test Locations
{expected_test_files}

Report any issues found.
"""


def generate_verify_prompt(testbed_path: str, milestone: str, expected_test_files: list) -> str:
    """Generate verification prompt."""
    files_section = "\n".join(f"- {f}" for f in expected_test_files) if expected_test_files else "(none specified)"

    return VERIFY_TEST_CHANGES_PROMPT.format(
        testbed_path=testbed_path, milestone=milestone, expected_test_files=files_section
    )
