# split_test_patches

Split test code from source code in milestone patches.

> **Note**: Currently only supports **Rust** projects.

## Overview

This module separates test code from source code in milestone patches to ensure:

- **milestone_patches/{m}.patch** - Contains ONLY source code changes
- **milestone_patches/start_diff_patches/{m}.patch** - Contains ONLY test code changes

### How it works

```
Before splitting:
  milestone-{m}-start ─────────────────► milestone-{m}-end
                       (mixed: src+test)

After splitting:
  milestone-{m}-start-old ──► milestone-{m}-start ──► milestone-{m}-end
                          │                       │
                     (test only)              (src only)
                          │                       │
                          ▼                       ▼
               start_diff_patches/         milestone_patches/
                   {m}.patch                   {m}.patch
```

| Patch Location | Content | Git Range |
|----------------|---------|-----------|
| `milestone_patches/{m}.patch` | Source code only | start → end |
| `milestone_patches/start_diff_patches/{m}.patch` | Test code only | start-old → start |

## Quick Start

```bash
python -m harness.prepare_repo.split_test_patches.main <original_baseline_dir>
```

### Processing Steps

The command executes the following steps:

**Step 1: Analyze**
- Scan all patches in `milestone_patches/`
- Use ast-grep to identify test hunks, source hunks, and mixed hunks
- Report which milestones need fixing (have test or mixed hunks)

**Step 2: Create new directory**
- Copy `<original_baseline_dir>` → `<original_baseline_dir>_v2`
- Rename `milestone_patches/` → `milestone_patches_original/` (backup)
- Create empty `milestone_patches/` and `milestone_patches/start_diff_patches/`

**Step 3: Fix each problematic milestone**

For each milestone that contains test or mixed hunks:

1. Create `milestone-{m}-start-old` tag to preserve original start point
2. Checkout to `milestone-{m}-start-old`
3. Generate a prompt with test hunks for Claude agent
4. Run Claude agent to commit ONLY test code changes
5. Update `milestone-{m}-start` tag to point to new HEAD (with test code applied)
6. Generate split patches:
   - `milestone_patches/{m}.patch` (start → end, source only)
   - `milestone_patches/start_diff_patches/{m}.patch` (start-old → start, test only)

**Step 4: Copy unchanged milestones**
- For milestones that were already source-only, copy patches directly without modification

**Step 5: Verify and retry**
- Verify all patches are correctly separated (no test hunks in src patch, no src hunks in test patch)
- Auto-retry failed milestones up to `--max-retries` times with enhanced prompts

**Step 6: Report**
- Summary of fixed/skipped/failed milestones
- List any errors encountered

## CLI Options

```bash
python -m harness.prepare_repo.split_test_patches.main <baseline_dir> [options]
```

| Option | Description |
|--------|-------------|
| `--dry-run` | Analyze only, do not create directory or make any changes |
| `--force` | Delete existing target directory and recreate |
| `--suffix SUFFIX` | Suffix for new directory (default: `_v2`), target dir must not exist unless `--force` |
| `--max-retries N` | Max retry attempts for failed milestones (default: 3) |
| `--milestone ID` | Filter to specific milestone (requires `--dry-run` or `--retry-only`) |
| `--retry-only` | Retry on existing directory (requires `--milestone`) |
| `--skip-agent` | Create directory, tags, and prompts, but skip Claude agent execution (for testing setup) |
| `-v, --verbose` | Enable verbose logging |

## Examples

### Analyze without making changes

Use `--dry-run` to see which milestones need fixing:

```bash
python -m harness.prepare_repo.split_test_patches.main \
    DATA/harness_workspace/BurntSushi_ripgrep/baseline_004 \
    --dry-run
```

### Fix all milestones

Run the full fixing process:

```bash
python -m harness.prepare_repo.split_test_patches.main \
    DATA/harness_workspace/BurntSushi_ripgrep/baseline_004
```

### Force overwrite existing directory

If `baseline_004_v2` already exists, use `--force` to delete and recreate:

```bash
python -m harness.prepare_repo.split_test_patches.main \
    DATA/harness_workspace/BurntSushi_ripgrep/baseline_004 \
    --force
```

### Retry a specific failed milestone

If a milestone failed during the initial run, retry it on the existing `_v2` directory:

```bash
python -m harness.prepare_repo.split_test_patches.main \
    DATA/harness_workspace/BurntSushi_ripgrep/baseline_004_v2 \
    --milestone milestone_seed_abc123_1 \
    --retry-only
```

### Verify separation results

Check that all patches are correctly separated:

```bash
python -m harness.prepare_repo.split_test_patches.verify_test_separation \
    check DATA/harness_workspace/BurntSushi_ripgrep/baseline_004_v2
```

## Output Structure

After running, the new baseline directory will have:

```
<original_baseline_dir>_v2/
├── milestone_patches/
│   ├── {m1}.patch                      # Source-only patch
│   ├── {m2}.patch                      # Source-only patch
│   ├── ...
│   ├── start_diff_patches/             # Test-only patches
│   │   ├── {m1}.patch
│   │   ├── {m2}.patch
│   │   └── ...
│   └── fix_logs/                       # Claude agent execution logs
│       ├── {m}_agent.log               # Structured log (cmd, prompt, stdout, stderr)
│       ├── {m}_conversation.md         # Full conversation trace
│       └── ...
├── milestone_patches_original/         # Backup of original mixed patches
├── testbed/                            # Git repository with updated tags
│   └── .git/
│       └── refs/tags/
│           ├── milestone-{m}-start-old  # Original start (new tag)
│           ├── milestone-{m}-start      # Updated start (with test code)
│           └── milestone-{m}-end        # End (unchanged)
└── ...
```

## How Test Code is Detected

For Rust projects, test code is identified by:

| Pattern | Description |
|---------|-------------|
| `tests/` directory | Any file under `tests/` directory |
| `#[cfg(test)]` | Modules or items with test configuration |
| `#[test]` | Test functions |
| `#[bench]` | Benchmark functions |
| `#[tokio::test]` | Async test functions (tokio) |
| `#[async_std::test]` | Async test functions (async-std) |

The detection uses `ast-grep` for accurate Rust AST analysis, including proper handling of nested modules and brace matching.

## Limitations

**Currently only supports Rust projects.**

To add support for other languages (Python, Go, Java, etc.), modify:

| File | Changes Needed |
|------|----------------|
| `prompts.py` | Language-specific prompts explaining what test code looks like |
| `verify_test_separation.py` | Language-specific test detection logic (e.g., `test_*.py`, `*_test.go`) |

## Dependencies

| Dependency | Purpose |
|------------|---------|
| `ast-grep` | Rust AST analysis for accurate test region detection |
| `claude` CLI | Running Claude agent to apply test code changes |

## Module API

```python
from harness.prepare_repo.split_test_patches import fix_baseline, analyze_baseline

# Analyze baseline for issues
analysis = analyze_baseline("/path/to/baseline")
print(f"Problematic milestones: {analysis.problematic_milestones}")
print(f"Clean milestones: {analysis.clean_milestones}")

# Fix and create new baseline
results = fix_baseline(
    baseline_dir="/path/to/baseline",
    suffix="_v2",
    max_retries=3,
    force=False
)
print(f"Fixed: {results['fixed']}")
print(f"Errors: {results['errors']}")
```

## Directory Structure

```
split_test_patches/
├── __init__.py                  # Module exports (fix_baseline, analyze_baseline)
├── main.py                      # CLI entry point and orchestration
├── analyzer.py                  # Analyze patches for test/src/mixed hunks
├── verify_test_separation.py    # Verify separation correctness (ast-grep based)
├── patch_generator.py           # Generate split patches and manage git tags
├── prompts.py                   # Claude agent prompt templates
└── agent_runner.py              # Run Claude agent for test code application
```
