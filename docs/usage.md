# Usage

## Quick Start: Run All Repos

The simplest way to run EvoClaw across all repositories:

```bash
# 1. Configure
cp trial_config.example.yaml trial_config.yaml
# Edit: set data_root, trial_name, agent, model

# 2. Set API credentials
export UNIFIED_API_KEY="sk-..."
export UNIFIED_BASE_URL="https://..."    # optional, for proxy or custom endpoints

# 3. Run all repos in parallel
python scripts/run_all.py --config trial_config.yaml

# 4. Monitor progress (in another terminal)
./scripts/monitor.sh my_experiment
```

> **Resource note:** Running all 7 repos in parallel spawns up to **35 Docker containers** simultaneously (1 agent container + up to 4 concurrent evaluation containers per repo). Make sure your machine has sufficient CPU, memory, and disk space before running at full parallelism. Use `max_parallel` in the trial config to limit concurrency if needed.

### run_all.py Options

```bash
# Override config options from CLI
python scripts/run_all.py --config trial_config.yaml --max-parallel 2
python scripts/run_all.py --config trial_config.yaml --repos navidrome dubbo
```

### Trial Config

The config only requires two fields: `data_root` and `trial_name`. Everything else has sensible defaults. See [`trial_config.example.yaml`](../trial_config.example.yaml) for the full template with comments.

---

## Run a Single Repo

For running one repository at a time with full control:

```bash
python -m harness.e2e.run_e2e \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --image navidrome_navidrome_v0.57.0_v0.58.0/base:latest \
  --srs-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0/srs \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --agent claude-code \
  --model claude-sonnet-4-5-20250929 \
  --timeout 18000
```

### CLI Arguments

| Argument | Description |
|----------|-------------|
| `--repo-name` | Repository identifier (e.g., `navidrome_navidrome_v0.57.0_v0.58.0`) |
| `--image` | Base Docker image for the agent container |
| `--srs-root` | Path to SRS directory (contains `{milestone_id}/SRS.md` files) |
| `--workspace-root` | Path to workspace with metadata, DAG, and test data |
| `--agent` | Agent framework: `claude-code`, `codex`, `gemini-cli`, `openhands` |
| `--model` | Model identifier (e.g., `claude-sonnet-4-5-20250929`) |
| `--timeout` | Max agent runtime in seconds |
| `--reasoning-effort` | Reasoning level: `low`, `medium`, `high`, `xhigh`, `max` |
| `--prompt-version` | Prompt template version (`v1`, `v2`) |
| `--trial-name` | Custom trial name prefix (auto-increments with `_001` suffix) |

### Environment Variables

All agents use a unified API interface:

| Variable | Description |
|----------|-------------|
| `UNIFIED_API_KEY` | API key (required). Mapped to agent-specific env vars internally. |
| `UNIFIED_BASE_URL` | Base URL (optional). For proxy or custom endpoints. |

The framework maps these to each agent's native env vars:

| Agent | API Key | Base URL |
|-------|---------|----------|
| Claude Code | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` |
| Codex | `CODEX_API_KEY` | `OPENAI_BASE_URL` |
| Gemini CLI | `GEMINI_API_KEY` | `GOOGLE_GEMINI_BASE_URL` |
| OpenHands | `LLM_API_KEY` | `LLM_BASE_URL` |

## Resume a Trial

If a repo's trial is interrupted (e.g., killed, timeout, API error), you can resume it individually. Each repo runs in an independent container, so resuming one does not affect others.

```bash
python -m harness.e2e.run_e2e --resume-trial /path/to/EvoClaw-data/repo_name/e2e_trial/my_experiment_001
```

This restores the DAG state, pending evaluations, and agent session from the existing container.

> **Tip:** Use `./scripts/monitor.sh` to watch trial progress. If a repo's milestones appear stuck for a long time (usually due to agent framework memory or network issues), kill that repo's `run_e2e` process and resume it with the command above. EvoClaw will automatically continue from the latest checkpoint. This workflow pairs well with AI coding agents like [Claude Code](https://docs.anthropic.com/en/docs/claude-code), which can help you identify stuck trials, kill the right processes, and run the resume commands smoothly.

## Run a Single Milestone

For testing or debugging, run one milestone in isolation:

```bash
python -m harness.e2e.run_milestone \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --srs-path /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0/srs/milestone_001/SRS.md \
  --agent claude-code \
  --model claude-sonnet-4-5-20250929
```

## Collect Results

### Single Repo

```bash
python -m harness.e2e.collect_results \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --trials my_experiment_001 \
  --trial-type e2e
```

### Multi-Repo (via monitor.sh)

```bash
# Uses auto-generated config from run_all.py
./scripts/monitor.sh my_experiment

# Only show specific repos
./scripts/monitor.sh my_experiment --repos navidrome dubbo
```

### Re-evaluate Snapshots

Re-run evaluation on a previously captured source snapshot:

```bash
python -m harness.e2e.evaluator \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --patch-file /path/to/trial/evaluation/milestone_001/source_snapshot.tar \
  --baseline-classification /path/to/test_results/milestone_001/milestone_001_classification.json \
  --output /path/to/output/evaluation_result.json
```

## Configuration

The `e2e_config.yaml` (at `harness/e2e/e2e_config.yaml`) controls evaluation behavior:

```yaml
dag_unlock:
  early_unblock: true          # Unlock milestones immediately on submission
  ignore_weak_dependencies: true
  strict_threshold:
    fail_to_pass: 1.0          # 100% of fail_to_pass tests must pass
    pass_to_pass: 1.0          # No regressions allowed
    none_to_pass: 1.0

retry_and_timing:
  debounce_seconds: 120        # Wait for tag hash to stabilize
  max_retries: 2               # Re-evaluate if tag changes
  max_no_progress_attempts: 3  # Max recovery attempts without progress
```

See the full [e2e_config.yaml](../harness/e2e/e2e_config.yaml) for all available options.

## Trial Output Structure

Each trial produces a structured output directory:

```
{workspace_root}/e2e_trial/{trial_name}/
├── trial_metadata.json        # Run configuration
├── orchestrator.log           # Detailed orchestration log
├── agent_stats.json           # Agent statistics (cost, tokens, turns)
├── log/
│   ├── agent_prompt.txt       # Initial prompt sent to agent
│   ├── agent_stdout.txt       # Agent stdout
│   └── agent_stderr.txt       # Agent stderr
└── evaluation/
    ├── summary.json           # Aggregated results across all milestones
    └── {milestone_id}/
        ├── source_snapshot.tar
        └── evaluation_result.json
```
