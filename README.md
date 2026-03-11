# EvoClaw

**End-to-End Evaluation Harness for AI Coding Agents on Real-World Software Evolution**

EvoClaw evaluates how well AI coding agents (Claude Code, Codex, Gemini CLI, OpenHands) can implement a sequence of real-world software milestones within a single session. Unlike single-task benchmarks, EvoClaw tests agents on **multi-milestone, dependency-ordered development tasks** extracted from actual open-source repository evolution.

## Overview

Each evaluation trial works as follows:

1. An agent is dropped into a Docker container with a codebase at a historical version
2. The agent receives a sequence of **Software Requirements Specifications (SRS)** describing milestones to implement
3. Milestones are ordered by a **dependency DAG** — downstream milestones unlock as upstream ones are completed
4. The agent signals completion by creating git tags (e.g., `agent-impl-M001`)
5. A **watcher thread** detects tags, extracts source snapshots, and runs the project's test suite against baseline classifications
6. Results track `fail_to_pass`, `pass_to_pass`, and `none_to_pass` test outcomes per milestone

## Supported Repositories

| Repository | Language | Milestones | Version Range |
|-----------|----------|------------|---------------|
| [navidrome](https://github.com/navidrome/navidrome) | Go | 9 | v0.57.0 → v0.58.0 |
| [dubbo](https://github.com/apache/dubbo) | Java | 13 | 3.3.3 → 3.3.6 |
| [ripgrep](https://github.com/BurntSushi/ripgrep) | Rust | 13 | 14.1.1 → 15.0.0 |
| [go-zero](https://github.com/zeromicro/go-zero) | Go | 23 | v1.6.0 → v1.9.3 |
| [nushell](https://github.com/nushell/nushell) | Rust | 13 | 0.106.0 → 0.108.0 |
| [element-web](https://github.com/element-hq/element-web) | TypeScript | 18 | v1.11.95 → v1.11.97 |
| [scikit-learn](https://github.com/scikit-learn/scikit-learn) | Python | 12 | 1.5.2 → 1.6.0 |

## Prerequisites

- Python >= 3.10
- Docker
- An API key for the agent you want to evaluate (e.g., `ANTHROPIC_API_KEY` for Claude Code)
- The agent CLI installed on the host (e.g., `claude` for Claude Code)

## Installation

```bash
git clone https://github.com/Hydrapse/EvoClaw.git
cd EvoClaw
pip install -e .
```

## Data Setup

Workspace data (metadata, SRS documents, test classifications) is hosted on HuggingFace:

```bash
# Install Git LFS if not already installed
git lfs install

# Clone the dataset
git clone https://huggingface.co/datasets/hyd2apse/EvoClaw-data

# The dataset contains one directory per repository:
# EvoClaw-data/
# ├── navidrome_navidrome_v0.57.0_v0.58.0/
# ├── apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/
# ├── BurntSushi_ripgrep_14.1.1_15.0.0/
# ├── zeromicro_go-zero_v1.6.0_v1.9.3/
# ├── nushell_nushell_0.106.0_0.108.0/
# ├── element-hq_element-web_v1.11.95_v1.11.97/
# └── scikit-learn_scikit-learn_1.5.2_1.6.0/
```

Each repository workspace directory contains:

```
<repo_name>/
├── metadata.json                      # Repo metadata (src_dirs, test_dirs, patterns)
├── dependencies.csv                   # Milestone dependency DAG
├── milestones.csv                     # Milestone catalog
├── selected_milestone_ids.txt         # (optional) Subset of milestones to evaluate
├── additional_dependencies.csv        # (optional) Extra DAG edges
├── srs/{milestone_id}/SRS.md          # Requirements specification per milestone
└── test_results/{milestone_id}/       # Baseline test classifications
    └── {milestone_id}_classification.json
```

### Docker Images

Pre-built Docker images are hosted on DockerHub under the `hyd2apse` organization. There are two types of images per repository:

- **Base image** — the agent runs inside this container (passed via `--image`)
- **Milestone images** — used by the evaluator to run tests for each milestone

The evaluator expects milestone images named as `{repo_name}/{milestone_id}:latest`. Since DockerHub does not support multi-level repository names, images are published with a flat naming scheme and must be **retagged locally** after pulling.

**Image naming mapping:**

| DockerHub name | Local name (after retag) |
|---|---|
| `hyd2apse/<repo>:base` | `<repo_full>/base:latest` |
| `hyd2apse/<repo>:<milestone_id>` | `<repo_full>/<milestone_id>:latest` |

**Available repositories:**

| Short name | Full repo name |
|------------|---------------|
| `navidrome` | `navidrome_navidrome_v0.57.0_v0.58.0` |
| `dubbo` | `apache_dubbo_dubbo-3.3.3_dubbo-3.3.6` |
| `ripgrep` | `burntsushi_ripgrep_14.1.1_15.0.0` |
| `go-zero` | `zeromicro_go-zero_v1.6.0_v1.9.3` |
| `nushell` | `nushell_nushell_0.106.0_0.108.0` |
| `element-web` | `element-hq_element-web_v1.11.95_v1.11.97` |
| `scikit-learn` | `scikit-learn_scikit-learn_1.5.2_1.6.0` |

**Example: Pull and retag navidrome images**

```bash
REPO=navidrome
REPO_FULL=navidrome_navidrome_v0.57.0_v0.58.0

# Pull and retag base image
docker pull hyd2apse/${REPO}:base
docker tag hyd2apse/${REPO}:base ${REPO_FULL}/base:latest

# Pull and retag all milestone images
for MID in milestone_001 milestone_002 milestone_003_sub-01 milestone_003_sub-02 \
           milestone_003_sub-03 milestone_003_sub-04 milestone_004 milestone_006 milestone_007; do
    docker pull hyd2apse/${REPO}:${MID}
    docker tag hyd2apse/${REPO}:${MID} ${REPO_FULL}/${MID}:latest
done
```

A helper script is provided to automate this for all repositories:

```bash
# Pull and retag all images for a specific repo
./scripts/pull_images.sh --repo navidrome

# Pull and retag all repos
./scripts/pull_images.sh
```

## Quick Start

### Run an E2E Trial (Full Session)

```bash
export UNIFIED_BASE_URL="https://llm-proxy.example.com/v1" && \
  export UNIFIED_API_KEY="sk-..." && \
  python -m harness.e2e.run_e2e \
    --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
    --image navidrome_navidrome_v0.57.0_v0.58.0/base:latest \
    --srs-root /path/to/workspace/srs \
    --workspace-root /path/to/workspace \
    --agent claude-code \
    --model claude-sonnet-4-5-20250929 \
    --timeout 18000
```

**Key arguments:**

| Argument | Description |
|----------|-------------|
| `--repo-name` | Repository identifier (e.g., `navidrome_navidrome_v0.57.0_v0.58.0`) |
| `--image` | Base Docker image for the agent container |
| `--srs-root` | Path to SRS directory (contains `{milestone_id}/SRS.md` files) |
| `--workspace-root` | Path to workspace with metadata, DAG, and test data |
| `--agent` | Agent framework: `claude-code`, `codex`, `gemini-cli`, `openhands` |
| `--model` | Model identifier |
| `--timeout` | Max agent runtime in seconds |
| `--prompt-version` | Prompt template version (`v1`, `v2`) |
| `--trial-name` | Custom trial name prefix (auto-increments) |

### Resume a Trial

```bash
python -m harness.e2e.run_e2e --resume-trial /path/to/trial_dir
```

Resumes from the existing container and restores DAG state, pending evaluations, and agent session.

### Run a Single Milestone

```bash
python -m harness.e2e.run_milestone \
    --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
    --workspace-root /path/to/workspace \
    --milestone-id milestone_001 \
    --srs-path /path/to/workspace/srs/milestone_001/SRS.md \
    --agent claude-code \
    --model claude-sonnet-4-5-20250929
```

### Re-evaluate Snapshots

```bash
python -m harness.e2e.evaluator \
    --workspace-root /path/to/workspace \
    --milestone-id M001 \
    --patch-file /path/to/trial/evaluation/M001/source_snapshot.tar \
    --baseline-classification /path/to/workspace/test_results/M001/M001_classification.json \
    --output /path/to/output/evaluation_result.json
```

### Collect Results

```bash
python -m harness.e2e.collect_results \
    --workspace-root /path/to/workspace \
    --trials trial_001 trial_002 \
    --trial-type e2e
```

## Architecture

```
Agent Container (Docker)
├── /testbed/          ← Source code at start version
├── Agent CLI          ← Claude Code / Codex / Gemini / OpenHands
└── git tags           ← Agent creates tags to signal milestone completion

Host (Orchestrator)
├── Watcher Thread     ← Monitors git tags via `docker exec git`
├── Debounce           ← Waits for tag hash to stabilize
├── Evaluator          ← Extracts snapshot, runs tests in milestone container
└── DAG Manager        ← Unlocks downstream milestones, sends recovery prompts
```

### Evaluation Flow

1. Agent creates git tag `agent-impl-{milestone_id}` in the container
2. Watcher detects the tag, waits for debounce period
3. Source snapshot is extracted as a tar archive
4. Tests run in a separate milestone-specific Docker container
5. Results are compared against baseline test classifications:
   - **fail_to_pass**: Tests that should now pass (the core requirement)
   - **pass_to_pass**: Tests that must not regress
   - **none_to_pass**: New tests that should pass
6. DAG state is updated; dependent milestones are unlocked
7. A recovery prompt is sent to the agent with updated task queue

## Configuration

The `e2e_config.yaml` controls evaluation behavior:

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

## Trial Output

Each trial produces:

```
e2e_trial/{trial_name}/
├── trial_metadata.json        # Run configuration
├── orchestrator.log           # Detailed orchestration log
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

## License

TBD
