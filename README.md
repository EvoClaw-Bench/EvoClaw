<p align="center">
  <img src="assets/banner.png" width="720" alt="EvoClaw Banner" />
</p>

<p align="center">
  <b>A Continuous Task Evaluation Playground for AI Harness</b>
</p>

<p align="center">
  <a href="https://evo-claw.com"><img src="https://img.shields.io/badge/Website-evo--claw.com-blue.svg" alt="Website" /></a>
  <a href="https://arxiv.org/abs/2603.13428"><img src="https://img.shields.io/badge/arXiv-2603.13428-b31b1b.svg" alt="arXiv" /></a>
  <a href="https://huggingface.co/datasets/hyd2apse/EvoClaw-data"><img src="https://img.shields.io/badge/%F0%9F%A4%97-Dataset-orange.svg" alt="HuggingFace Dataset" /></a>
  <a href="https://hub.docker.com/u/hyd2apse"><img src="https://img.shields.io/badge/Docker-hyd2apse-2496ED?logo=docker&logoColor=white" alt="DockerHub" /></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue.svg" alt="Python 3.10+" /></a>
</p>

---

> [!NOTE]
> 🆕 **GPT-5.4** (xhigh) achieves **2nd place** among all models at **33.71%** score. See the [leaderboard](https://evo-claw.com).

Most existing benchmarks evaluate agents on **isolated, one-shot tasks**. But real-world workflows are not a bag of independent missions, they are continuous processes where tasks build on each other, dependencies interleave, and context accumulates over a long session.

<p align="center">
  <img src="assets/evoclaw_concept.png" width="560" alt="Independent Coding Task vs. Continuous Software Evolution" />
</p>

**EvoClaw** is a general-purpose evaluation harness for **continuous tasks**. It drops an AI agent into a working environment and challenges it to complete an ordered sequence of milestones. As the agent works, EvoClaw silently extracts checkpoints, evaluates each milestone, and asynchronously unlocks downstream tasks, enabling fine-grained, per-milestone analysis without interrupting the agent's session. 

<p align="center">
  <img src="assets/evoclaw_illustration.png" width="820" alt="How EvoClaw works: Continuous Task Evaluation with DAG, Agent Loop, and Test-Based Grading" />
</p>

Currently focused on **software evolution**, EvoClaw's architecture is designed to extend to other domains.

## ✨ Key Features

- **Test Your Model**: Out of the box, EvoClaw ships with the [EvoClaw Benchmark](https://arxiv.org/abs/2603.13428) (long-horizon software evolution itineraries from 7 real-world repos) and 4 pre-configured agent frameworks ([Claude Code](https://docs.anthropic.com/en/docs/claude-code), [Codex](https://openai.com/index/codex/), [Gemini CLI](https://github.com/google-gemini/gemini-cli), [OpenHands](https://github.com/All-Hands-AI/OpenHands)). Provide a model API key and start evaluating.

- **Bring Your Own Agent**: The agent layer is decoupled from the evaluation engine (see below). Plug in your own agent by implementing a lightweight adapter. EvoClaw also provides a per-milestone analysis framework for detailed performance breakdowns.

- **Bring Your Own Data**: Supply your own task descriptions, test environments (Docker), test list for scoring, and task dependencies. EvoClaw handles orchestration, checkpoint-based evaluation, and reporting, enabling continuous task evaluation beyond coding.

## 👋 Overview

<p align="center">
  <img src="assets/evoclaw_arch.png" width="820" alt="EvoClaw Architecture: Orchestrator, Agent Container, DAG Manager, Evaluation Cycle, and Analytics" />
</p>

Each evaluation trial works as follows:

1. An agent is dropped into a persistent Docker container with a workspace at a given starting state.
2. It receives a sequence of **task specifications** describing tasks to achieve.
3. Tasks are ordered by a **dependency DAG**---downstream tasks unlock as upstream ones are completed.
4. The agent signals completion by creating git tags (e.g., `agent-impl-milestone_001`).
5. A **watcher thread** silently detects tags, extracts artifact snapshots, and runs pre-defined automated validation in a separate, one-time task evaluation container.
6. Results, logs, and outcomes are automatically collected and analyzed per task.

## 🔧 Setup

**0. Prerequisites**

- Python >= 3.10
- Docker
- Model API access via environment variables: `UNIFIED_API_KEY` and `UNIFIED_BASE_URL`

**1. Installation**

```bash
git clone https://github.com/Hydrapse/EvoClaw.git
cd EvoClaw
uv sync
```

**2. Data & Docker Images**

Workspace data is hosted on [HuggingFace](https://huggingface.co/datasets/hyd2apse/EvoClaw-data). Docker images are hosted on [DockerHub](https://hub.docker.com/u/hyd2apse).

```bash
# Download workspace data
git lfs install
git clone https://huggingface.co/datasets/hyd2apse/EvoClaw-data

# Pull all repos at once
./scripts/pull_images.sh
```

> See [docs/setup.md](docs/setup.md) for the full data layout, Docker image naming conventions, and manual retag instructions.

## 🚀 Usage

**1. Configure** — copy the template and edit:

```bash
cp trial_config.example.yaml trial_config.yaml
```

```yaml
# modify trial_config.yaml 
data_root: /path/to/EvoClaw-data       # where you cloned the HuggingFace dataset
trial_name: my_experiment              # name for this evaluation run
agent: claude-code                     # agent: claude-code | codex | gemini-cli | openhands
model: claude-opus-4-6                 # model identifier
timeout: 18000                         # optional: max agent runtime per repo (seconds)
# reasoning_effort: high               # optional: low | medium | high | xhigh | max 
# repos: [navidrome, ripgrep]          # optional: run only these repos (default: all)
# max_parallel: 3                      # optional: limit parallel repos (default: all)
```

**2. Run** — evaluate across all repos:

```bash
export UNIFIED_API_KEY=sk-...
export UNIFIED_BASE_URL=https://...   # optional, for proxy or custom endpoints
python scripts/run_all.py --config trial_config.yaml
```

**3. Monitor** — check progress in another terminal:

```bash
./scripts/monitor.sh                              # auto-detects trial, compact view
./scripts/monitor.sh my_experiment --detail        # per-milestone breakdown
./scripts/monitor.sh my_experiment --full          # full table with all columns
```

> **Tip:** If a repo's milestones appear stuck (usually due to agent framework memory or network issues), kill that repo's `run_e2e` process and resume with `python -m harness.e2e.run_e2e --resume-trial /path/to/trial_dir`. EvoClaw will continue from the latest checkpoint. Use `--model` to override the model on resume, or `--force` to start a completely fresh trial.

> See [docs/usage.md](docs/usage.md) for single-repo runs, resume, re-evaluation, result collection, and all CLI arguments.

## 🔍 Troubleshooting

Below are common issues you may encounter when running evaluations, along with solutions.

**1. Network access blocked inside containers**

Agent containers enforce an iptables-based outbound whitelist — only domains needed for API access and package management are allowed (e.g., `api.anthropic.com`, `registry.npmjs.org`, `pypi.org`). Code hosting sites (GitHub, GitLab, etc.) are explicitly blocked to prevent data leakage. If your setup routes API requests through a custom proxy, make sure the proxy domain is included in `WHITELISTED_DOMAINS` in `harness/e2e/container_setup.py`.

**2. Agent exits prematurely before completing all milestones**

Due to LLM capability limitations or agent framework issues, agents may exit without completing all milestones. For example, out of memory, hitting API errors, or getting stuck in implementation loops without submitting. EvoClaw handles this with a built-in resume mechanism that recovers the agent session and continues from where it left off:

```bash
python -m harness.e2e.run_e2e --resume-trial /path/to/trial_dir
```

When possible, the agent resumes within the same session context, preserving its memory of all previous work. If the session becomes unrecoverable (e.g., corrupted state or context overflow after many turns), EvoClaw automatically falls back to a new session. In either case, all prior code changes and git state (commits, tags) are preserved in the container, so the agent can continue from the current codebase state.

> **Evaluation protocol**: The reported results in the EvoClaw benchmark follow a protocol where trials are resumed until all milestones are submitted and evaluated, unless three consecutive resumes yield no new submissions. We encourage reproducibility studies to follow the same setting.

## 🤝 Contributing

We welcome contributions! Whether it's adding support for new agents, new task domains, new datasets, bug fixes, or documentation improvements.

## ✍️ Citation

Welcome to cite our paper if you find EvoClaw useful!

```bibtex
@misc{deng2026evoclawevaluatingaiagents,
      title={EvoClaw: Evaluating AI Agents on Continuous Software Evolution},
      author={Gangda Deng and Zhaoling Chen and Zhongming Yu and Haoyang Fan and Yuhong Liu and Yuxin Yang and Dhruv Parikh and Rajgopal Kannan and Le Cong and Mengdi Wang and Qian Zhang and Viktor Prasanna and Xiangru Tang and Xingyao Wang},
      year={2026},
      eprint={2603.13428},
      archivePrefix={arXiv},
      primaryClass={cs.SE},
      url={https://arxiv.org/abs/2603.13428},
}
```

## 📄 License

This project is licensed under the [MIT](LICENSE) License.
