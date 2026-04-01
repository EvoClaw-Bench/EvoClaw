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
  --model claude-sonnet-4-6 \
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
| `--model` | Model identifier (e.g., `claude-sonnet-4-6`) |
| `--timeout` | Max agent runtime in seconds |
| `--reasoning-effort` | Reasoning level: `low`, `medium`, `high`, `xhigh`, `max` |
| `--prompt-version` | Prompt template version (`v1`, `v2`) |
| `--trial-name` | Custom trial name prefix (auto-increments with `_001` suffix) |
| `--force` | Force remove existing container before starting a fresh trial |
| `--remove-container` | Remove container after trial completes (default: keep running) |
| `--skip-testbed-copy` | Skip copying `/testbed` from container after trial |

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

To start a fresh trial while an existing one occupies the container, use `--force` to remove the old container:

```bash
python -m harness.e2e.run_e2e \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --image navidrome_navidrome_v0.57.0_v0.58.0/base:latest \
  --srs-root /path/to/EvoClaw-data/navidrome_.../srs \
  --workspace-root /path/to/EvoClaw-data/navidrome_... \
  --agent claude-code --model claude-sonnet-4-6 \
  --timeout 18000 --trial-name my_experiment_001 \
  --force
```

Resume options:

| Flag | Description |
|------|-------------|
| `--resume-trial PATH` | Resume from existing trial directory (container must exist) |
| `--no-resume-session` | In resume mode, start a new agent session instead of resuming the previous one |
| `--force` | Force remove existing container before starting a fresh trial |

> **Tip:** Use `./scripts/monitor.sh` to watch trial progress. If a repo's milestones appear stuck for a long time (usually due to agent framework memory or network issues), kill that repo's `run_e2e` process and resume it with the command above. EvoClaw will automatically continue from the latest checkpoint.

## Run a Single Milestone

For testing or debugging, run one milestone in isolation:

```bash
python -m harness.e2e.run_milestone \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --srs-path /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0/srs/milestone_001/SRS.md \
  --agent claude-code \
  --model claude-sonnet-4-6
```

## Monitor Progress

`monitor.sh` provides three display modes for tracking experiment progress:

### Compact Overview (default)

Shows all repos at a glance with progress, score, and running status. Fits in 80 columns.

```bash
./scripts/monitor.sh                           # auto-detects trial name
./scripts/monitor.sh my_experiment_001         # explicit trial name
./scripts/monitor.sh my_experiment_001 --repos navidrome dubbo   # filter repos
```

Example output:
```
🏃 my_experiment_001 | 2 running, 5 pending
  claude-code | claude-sonnet-4-6 | effort=high

  Repo              Total Subm Eval   Score        Resolve  Status
  ──────────────────────────────────────────────────────────────────────────
  ripgrep              13    6    5   19.1%      9% (1/11)  ● running
  navidrome             9    2    2   11.1%     11% (1/9)   ● running
  dubbo                12    0    0      --      0% (0/12)  ○ pending
  ...
```

- **Total**: total milestones (including non-graded)
- **Subm**: milestones submitted by the agent (tagged)
- **Eval**: milestones evaluated (scored)

### Per-Milestone Detail

Drill into a specific repo's milestones with F2P, N2P, P2P, Precision, and Recall:

```bash
./scripts/monitor.sh --detail ripgrep          # specific repo (substring match)
./scripts/monitor.sh --detail                  # all repos that have started
```

### Full Table

Original wide table with all columns (Cost, Time, Turns, OutTok). Best for wide terminals or piping to a file:

```bash
./scripts/monitor.sh --full
./scripts/monitor.sh --full > results.txt
```

## Collect Results

### Single Repo

```bash
python -m harness.e2e.collect_results \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --trials my_experiment_001 \
  --trial-type e2e
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
