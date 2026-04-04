#!/usr/bin/env bash
#
# Monitor EvoClaw trial progress across all repos.
#
# Usage:
#   ./scripts/monitor.sh <trial_name>                          # monitor a trial
#   ./scripts/monitor.sh <trial_name> --repos navidrome dubbo  # monitor specific repos
#   ./scripts/monitor.sh <trial_name> --data-root /path/to/data
#
# This script auto-generates a config from trial_config.yaml (or --data-root)
# and runs collect_results.py --multi-repo.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$PROJECT_ROOT/.evoclaw"

# ─────────────────────────────────────────────
# Parse arguments
# ─────────────────────────────────────────────
TRIAL_NAMES=()
DATA_ROOT=""
REPOS=()
EXTRA_ARGS=()
DETAIL_REPO=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-root)  DATA_ROOT="$2"; shift 2 ;;
        --repos)      shift; while [[ $# -gt 0 ]] && [[ "$1" != --* ]]; do REPOS+=("$1"); shift; done ;;
        --detail)
            # --detail with optional repo argument
            if [[ $# -ge 2 ]] && [[ "$2" != --* ]]; then
                DETAIL_REPO="$2"; shift 2
            else
                DETAIL_REPO="__ALL__"; shift
            fi
            ;;
        --full)       EXTRA_ARGS+=("--full"); shift ;;
        --help|-h)
            echo "Usage: $0 [trial_name ...] [OPTIONS]"
            echo ""
            echo "  trial_name          One or more trial names to monitor"
            echo ""
            echo "Display modes:"
            echo "  (default)           Compact overview — progress, score, status (80 cols)"
            echo "  --detail REPO       Per-milestone breakdown for a repo (substring match)"
            echo "  --full              Full wide table with all columns"
            echo ""
            echo "Filters:"
            echo "  --data-root PATH    Path to EvoClaw-data (default: from trial_config.yaml)"
            echo "  --repos REPO ...    Only show these repos"
            echo "  -- ...              Extra args passed to collect_results.py"
            exit 0
            ;;
        --)           shift; EXTRA_ARGS=("$@"); break ;;
        --*)          EXTRA_ARGS+=("$1"); shift ;;
        *)            TRIAL_NAMES+=("$1"); shift ;;
    esac
done

# ─────────────────────────────────────────────
# Resolve data_root (needed before trial auto-detect)
# ─────────────────────────────────────────────
EVOCLAW_CONFIG="$PROJECT_ROOT/trial_config.yaml"
if [[ -z "$DATA_ROOT" ]]; then
    # Try reading from trial_config.yaml
    if [[ -f "$EVOCLAW_CONFIG" ]]; then
        DATA_ROOT=$(python3 -c "
import yaml
with open('$EVOCLAW_CONFIG') as f:
    cfg = yaml.safe_load(f)
print(cfg.get('data_root', ''))
" 2>/dev/null)
    fi
    if [[ -z "$DATA_ROOT" ]]; then
        echo "Error: --data-root not specified and no data_root found in trial_config.yaml"
        exit 1
    fi
fi

# Resolve to absolute path
DATA_ROOT="$(cd "$DATA_ROOT" 2>/dev/null && pwd)" || { echo "Error: data_root not found: $DATA_ROOT"; exit 1; }

# ─────────────────────────────────────────────
# Auto-detect trial name if not provided
# ─────────────────────────────────────────────
if [[ ${#TRIAL_NAMES[@]} -eq 0 ]]; then
    # Auto-detect: find all trial directories across repos
    FOUND_TRIALS=()
    for repo_dir in "$DATA_ROOT"/*/; do
        [[ ! -f "$repo_dir/metadata.json" ]] && continue
        trial_base="$repo_dir/e2e_trial"
        [[ ! -d "$trial_base" ]] && continue
        for trial_dir in "$trial_base"/*/; do
            [[ ! -d "$trial_dir" ]] && continue
            t=$(basename "$trial_dir")
            # Deduplicate
            local_found=false
            for existing in "${FOUND_TRIALS[@]:-}"; do
                [[ "$existing" == "$t" ]] && local_found=true && break
            done
            $local_found || FOUND_TRIALS+=("$t")
        done
    done

    if [[ ${#FOUND_TRIALS[@]} -eq 0 ]]; then
        echo "No trials found in $DATA_ROOT"
        echo ""
        echo "Usage: $0 [trial_name ...] [OPTIONS]"
        exit 1
    elif [[ ${#FOUND_TRIALS[@]} -eq 1 ]]; then
        TRIAL_NAMES=("${FOUND_TRIALS[0]}")
    else
        echo "Multiple trials found. Please specify one or more:"
        echo ""
        for t in "${FOUND_TRIALS[@]}"; do
            echo "  $0 $t"
        done
        echo ""
        echo "  $0 ${FOUND_TRIALS[*]}    # show all"
        exit 1
    fi
fi

# ─────────────────────────────────────────────
# Auto-generate config
# ─────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"

# Discover repos (directories with metadata.json)
REPO_ENTRIES=""
for repo_dir in "$DATA_ROOT"/*/; do
    [[ ! -f "$repo_dir/metadata.json" ]] && continue
    repo_name=$(basename "$repo_dir")

    # If --repos specified, filter
    if [[ ${#REPOS[@]} -gt 0 ]]; then
        matched=false
        for r in "${REPOS[@]}"; do
            if [[ "$repo_name" == *"$r"* ]]; then
                matched=true
                break
            fi
        done
        $matched || continue
    fi

    REPO_ENTRIES+="    \"$repo_name\": {\"path\": \"$repo_name\"},
"
done

# Ensure each trial name has _NNN suffix (matching run_all.py convention)
RESOLVED_TRIALS=()
for t in "${TRIAL_NAMES[@]}"; do
    if ! [[ "$t" =~ _[0-9]{3}$ ]]; then
        t="${t}_001"
    fi
    RESOLVED_TRIALS+=("$t")
done

# Build config file name from all trial names
CONFIG_LABEL=$(IFS=_; echo "${RESOLVED_TRIALS[*]}")
CONFIG_FILE="$CONFIG_DIR/${CONFIG_LABEL}_collect.py"

# Build Python list of trial names
TRIAL_LIST_PY=""
for t in "${RESOLVED_TRIALS[@]}"; do
    TRIAL_LIST_PY+="\"$t\", "
done

cat > "$CONFIG_FILE" << PYEOF
# Auto-generated by monitor.sh
DATA_ROOT = "$DATA_ROOT"

WORKSPACE_MAPPING = {
$REPO_ENTRIES}

E2E_TRIAL_NAMES = [${TRIAL_LIST_PY}]
PYEOF

# ─────────────────────────────────────────────
# Run collect_results
# ─────────────────────────────────────────────
cd "$PROJECT_ROOT"

COLLECT_ARGS=(
    python3 -m harness.e2e.collect_results
    --multi-repo
    --config "$CONFIG_FILE"
    --trial-type e2e
)

# Pass --detail if specified
if [[ -n "$DETAIL_REPO" ]]; then
    if [[ "$DETAIL_REPO" == "__ALL__" ]]; then
        COLLECT_ARGS+=("--detail" "")
    else
        COLLECT_ARGS+=("--detail" "$DETAIL_REPO")
    fi
fi

# --repos filtering is already applied in the generated WORKSPACE_MAPPING,
# so we don't pass --config-repos (which requires exact key match).

# Append extra args
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    COLLECT_ARGS+=("${EXTRA_ARGS[@]}")
fi

exec "${COLLECT_ARGS[@]}"
