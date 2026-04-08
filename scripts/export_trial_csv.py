#!/usr/bin/env python3
"""Export an EvoClaw e2e trial to CSV rows matching EvoClaw-website/data/e2e_trial.csv format.

Usage:
    python scripts/export_trial_csv.py --trial codex_gpt-5.4_001
    python scripts/export_trial_csv.py --trial codex_gpt-5.4_001 --append /path/to/e2e_trial.csv
"""

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.e2e.collect_results import (
    calculate_score,
    calculate_score_reliable,
    calculate_precision_recall,
    check_compilation_failure,
    is_resolved,
    load_e2e_results,
    load_non_graded_milestones,
    load_selected_milestones,
)
from harness.e2e.config import map_tool_breakdown

# Repo dir name -> CSV workspace short name
WORKSPACE_NAMES = {
    "apache_dubbo_dubbo-3.3.3_dubbo-3.3.6": "dubbo",
    "BurntSushi_ripgrep_14.1.1_15.0.0": "ripgrep",
    "element-hq_element-web_v1.11.95_v1.11.97": "element-web",
    "navidrome_navidrome_v0.57.0_v0.58.0": "navidrome",
    "nushell_nushell_0.106.0_0.108.0": "nushell",
    "scikit-learn_scikit-learn_1.5.2_1.6.0": "scikit-learn",
    "zeromicro_go-zero_v1.6.0_v1.9.3": "go-zero",
}

CSV_COLUMNS = [
    "workspace", "trial_name", "agent_name", "model",
    "total_milestones", "total_turns", "total_cost_usd",
    "total_duration_ms", "total_wall_clock_ms",
    "total_tool_calls", "total_subagent_calls", "unique_session_count",
    "total_input_tokens", "total_output_tokens", "total_cache_read_tokens",
    "resolved", "resolve_rate", "failed", "error", "not_run",
    "tool_read", "tool_edit", "tool_write", "tool_shell",
    "tool_search", "tool_plan", "tool_subagent", "tool_other",
    "mean_score_full", "mean_score_reliable",
    "mean_score_precision", "mean_score_recall", "sum_score_full",
]


def eval_milestones(workspace_root: Path, trial_name: str) -> dict:
    """Evaluate milestones using the same logic as collect_results / monitor.

    Uses load_e2e_results for retry-merge, load_selected_milestones and
    load_non_graded_milestones for milestone filtering, and
    calculate_score_reliable (PR-F1) as the primary score — matching monitor.
    """
    loaded_results, _ = load_e2e_results(workspace_root, trial_name)

    selected, _ = load_selected_milestones(workspace_root)
    non_graded = load_non_graded_milestones(workspace_root)
    if non_graded:
        selected = selected - non_graded

    resolved = 0
    failed = 0
    error = 0
    not_run = 0
    sum_score_full = 0.0
    sum_score_reliable = 0.0
    sum_precision = 0.0
    sum_recall = 0.0

    for mid in sorted(selected):
        result = loaded_results.get(mid)

        if result is None:
            not_run += 1
            continue

        if result.get("eval_status") == "error":
            error += 1
        elif is_resolved(result):
            resolved += 1
        else:
            failed += 1

        s_full = calculate_score(result)
        s_rel = calculate_score_reliable(result)
        prec, rec = calculate_precision_recall(result)

        if s_full is not None:
            sum_score_full += s_full
        if s_rel is not None:
            sum_score_reliable += s_rel
        if prec is not None:
            sum_precision += prec
        if rec is not None:
            sum_recall += rec

    total = len(selected)
    return {
        "total_milestones": total,
        "resolved": resolved,
        "failed": failed,
        "error": error,
        "not_run": not_run,
        "resolve_rate": resolved / total if total > 0 else 0.0,
        "mean_score_full": sum_score_full / total if total > 0 else 0.0,
        "mean_score_reliable": sum_score_reliable / total if total > 0 else 0.0,
        "mean_score_precision": sum_precision / total if total > 0 else 0.0,
        "mean_score_recall": sum_recall / total if total > 0 else 0.0,
        "sum_score_full": sum_score_full,
    }


def export_trial(data_root: Path, trial_name: str) -> List[Dict]:
    """Export CSV rows for a trial across all repos."""
    rows = []

    for repo_dir_name, ws_name in sorted(WORKSPACE_NAMES.items()):
        workspace_root = data_root / repo_dir_name
        trial_dir = workspace_root / "e2e_trial" / trial_name
        if not trial_dir.exists():
            continue

        stats_path = trial_dir / "agent_stats.json"
        if not stats_path.exists():
            continue

        stats = json.load(open(stats_path))
        summary = stats.get("summary", {})
        agent_name = stats.get("agent_framework", "")
        model = stats.get("model", "")

        # Token usage (output includes reasoning tokens)
        model_usage = stats.get("modelUsage", {})
        total_input = sum(m.get("inputTokens", 0) for m in model_usage.values())
        total_output = sum(
            m.get("outputTokens", 0)
            + m.get("thoughtsTokens", 0)
            + m.get("reasoningOutputTokens", 0)
            + m.get("reasoningTokens", 0)
            for m in model_usage.values()
        )
        total_cached = sum(
            m.get("cachedInputTokens", 0) + m.get("cacheReadInputTokens", 0)
            for m in model_usage.values()
        )

        # Tool breakdown (unified mapping)
        raw_breakdown = stats.get("tool_call_breakdown", {})
        unified = map_tool_breakdown(agent_name, raw_breakdown)

        # Evaluation scores
        scores = eval_milestones(workspace_root, trial_name)

        row = {
            "workspace": ws_name,
            "trial_name": trial_name,
            "agent_name": agent_name,
            "model": model,
            "total_milestones": scores["total_milestones"],
            "total_turns": summary.get("total_turns", 0),
            "total_cost_usd": summary.get("total_cost_usd", 0),
            "total_duration_ms": summary.get("duration_ms", 0),
            "total_wall_clock_ms": summary.get("wall_clock_ms", 0),
            "total_tool_calls": summary.get("total_tool_calls", 0),
            "total_subagent_calls": summary.get("total_subagent_calls", 0),
            "unique_session_count": summary.get("unique_session_count", 0),
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_cache_read_tokens": total_cached,
            "resolved": scores["resolved"],
            "resolve_rate": scores["resolve_rate"],
            "failed": scores["failed"],
            "error": scores["error"],
            "not_run": scores["not_run"],
            "tool_read": unified.get("read", 0),
            "tool_edit": unified.get("edit", 0),
            "tool_write": unified.get("write", 0),
            "tool_shell": unified.get("shell", 0),
            "tool_search": unified.get("search", 0),
            "tool_plan": unified.get("plan", 0),
            "tool_subagent": unified.get("subagent", 0),
            "tool_other": unified.get("other", 0),
            "mean_score_full": scores["mean_score_full"],
            "mean_score_reliable": scores["mean_score_reliable"],
            "mean_score_precision": scores["mean_score_precision"],
            "mean_score_recall": scores["mean_score_recall"],
            "sum_score_full": scores["sum_score_full"],
        }
        rows.append(row)

    return rows


def main():
    parser = argparse.ArgumentParser(description="Export trial to e2e_trial.csv format")
    parser.add_argument("--trial", required=True, help="Trial name (e.g. codex_gpt-5.4_001)")
    parser.add_argument("--data-root", type=Path, default=Path("/data2/gangda/EvoClaw-data"))
    parser.add_argument("--append", type=Path, default=None,
                        help="Append to existing CSV file instead of printing")
    args = parser.parse_args()

    rows = export_trial(args.data_root, args.trial)
    if not rows:
        print(f"No data found for trial {args.trial}", file=sys.stderr)
        sys.exit(1)

    if args.append:
        with open(args.append, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            writer.writerows(rows)
        print(f"Appended {len(rows)} rows to {args.append}")
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
        print(buf.getvalue(), end="")


if __name__ == "__main__":
    main()
