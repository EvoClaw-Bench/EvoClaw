# Setup

## Prerequisites

- Python >= 3.10
- Docker
- Model API access via environment variables: `UNIFIED_API_KEY` and `UNIFIED_BASE_URL`

## Installation

```bash
git clone https://github.com/Hydrapse/EvoClaw.git
cd EvoClaw
uv sync
```

## Workspace Data

Workspace data (metadata, SRS documents, test classifications) is hosted on HuggingFace:

```bash
git lfs install
git clone https://huggingface.co/datasets/hyd2apse/EvoClaw-data
```

The dataset contains one directory per repository:

```
EvoClaw-data/
├── navidrome_navidrome_v0.57.0_v0.58.0/
├── apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/
├── BurntSushi_ripgrep_14.1.1_15.0.0/
├── zeromicro_go-zero_v1.6.0_v1.9.3/
├── nushell_nushell_0.106.0_0.108.0/
├── element-hq_element-web_v1.11.95_v1.11.97/
└── scikit-learn_scikit-learn_1.5.2_1.6.0/
```

Each repository workspace directory contains:

```
<repo_name>/
├── metadata.json                      # Repo metadata (src_dirs, test_dirs, patterns)
├── dependencies.csv                   # Milestone dependency DAG
├── milestones.csv                     # Milestone catalog
├── selected_milestone_ids.txt         # (optional) Subset of milestones to evaluate
├── additional_dependencies.csv        # (optional) Extra DAG edges
├── non-graded_milestone_ids.txt       # (optional) Milestones excluded from scoring
├── srs/{milestone_id}/SRS.md          # Requirements specification per milestone
└── test_results/{milestone_id}/       # Baseline test classifications
    └── {milestone_id}_classification.json
```

The "Milestones" column in the main README counts graded milestones only. Some repositories include additional non-graded milestones (listed in `non-graded_milestone_ids.txt`) that the agent must still implement as part of the DAG but are excluded from scoring.

## Docker Images

Pre-built Docker images are hosted on DockerHub under the `hyd2apse` organization. There are two types of images per repository:

- **Base image** -- the agent runs inside this container (passed via `--image`)
- **Milestone images** -- used by the evaluator to run tests for each milestone

### Quick Setup (Recommended)

A helper script automates pulling and retagging:

```bash
# Pull and retag all images for a specific repo
./scripts/pull_images.sh --repo navidrome

# Pull and retag all repos
./scripts/pull_images.sh

# Dry run (see what would be pulled)
./scripts/pull_images.sh --repo navidrome --dry-run
```

### Manual Setup

The evaluator expects milestone images named as `{repo_name}/{milestone_id}:latest`. Since DockerHub does not support multi-level repository names, images are published with a flat naming scheme and must be **retagged locally** after pulling.

**Image naming mapping:**

| DockerHub name | Local name (after retag) |
|---|---|
| `hyd2apse/<repo>:base` | `<repo_full>/base:latest` |
| `hyd2apse/<repo>:<milestone_id>` | `<repo_full>/<milestone_id>:latest` |

**Repository name mapping:**

| Short name | Full repo name |
|------------|---------------|
| `navidrome` | `navidrome_navidrome_v0.57.0_v0.58.0` |
| `dubbo` | `apache_dubbo_dubbo-3.3.3_dubbo-3.3.6` |
| `ripgrep` | `burntsushi_ripgrep_14.1.1_15.0.0` |
| `go-zero` | `zeromicro_go-zero_v1.6.0_v1.9.3` |
| `nushell` | `nushell_nushell_0.106.0_0.108.0` |
| `element-web` | `element-hq_element-web_v1.11.95_v1.11.97` |
| `scikit-learn` | `scikit-learn_scikit-learn_1.5.2_1.6.0` |

**Example: Pull and retag navidrome images manually**

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
