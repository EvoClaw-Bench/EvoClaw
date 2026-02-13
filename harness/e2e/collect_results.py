#!/usr/bin/env python3
"""
Compare milestone results across multiple trials and select the best result for each milestone.

Usage:
    # For mstone trials (default):
    python scripts/compare_milestone_trials.py \
        --workspace-root DATA/harness_workspace/navidrome_navidrome_v0.57.0_v0.58.0/baseline_004_v4 \
        --trials complete_run_001 complete_run_002

    # For e2e trials:
    python scripts/compare_milestone_trials.py \
        --workspace-root DATA/harness_workspace/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/baseline_rerun_stage4_002_fix2_v2 \
        --trials complete_run_001 complete_run_002 \
        --trial-type e2e  # or mstone

Output:
    ASCII table with best results for each milestone
"""

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Set, Tuple, Optional


def load_non_graded_milestones(workspace_root: Path) -> Set[str]:
    """Load non-graded milestone IDs from file."""
    non_graded_file = workspace_root / "non-graded_milestone_ids.txt"
    if not non_graded_file.exists():
        return set()
    try:
        with open(non_graded_file) as f:
            return {line.strip() for line in f if line.strip()}
    except Exception as e:
        print(f"Warning: Failed to load {non_graded_file}: {e}", file=sys.stderr)
        return set()


def load_milestones_from_csv(workspace_root: Path) -> Optional[Set[str]]:
    """Load milestone IDs from milestones.csv file.

    Returns None if file doesn't exist or cannot be parsed.
    """
    import csv

    csv_file = workspace_root / "milestones.csv"
    if not csv_file.exists():
        return None
    try:
        with open(csv_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return {row["id"].strip() for row in reader if row.get("id", "").strip()}
    except Exception as e:
        print(f"Warning: Failed to load {csv_file}: {e}", file=sys.stderr)
        return None


def load_selected_milestones(workspace_root: Path) -> Tuple[Optional[Set[str]], Optional[str]]:
    """Load selected milestone IDs from file.

    First tries selected_milestone_ids.txt, then falls back to milestones.csv.
    Returns tuple of (milestone_set, source) where source is:
        - "selected_milestone_ids.txt" if loaded from that file
        - "milestones.csv" if loaded from CSV
        - None if no file found (meaning show all milestones)
    """
    selected_file = workspace_root / "selected_milestone_ids.txt"
    if selected_file.exists():
        try:
            with open(selected_file) as f:
                return {line.strip() for line in f if line.strip()}, "selected_milestone_ids.txt"
        except Exception as e:
            print(f"Warning: Failed to load {selected_file}: {e}", file=sys.stderr)

    # Fall back to milestones.csv
    csv_milestones = load_milestones_from_csv(workspace_root)
    if csv_milestones is not None:
        return csv_milestones, "milestones.csv"

    return None, None


def display_width(s: str) -> int:
    """Calculate the display width of a string, accounting for emoji and wide characters."""
    width = 0
    for char in s:
        # Emoji and some symbols take 2 display columns
        if unicodedata.east_asian_width(char) in ("F", "W"):
            width += 2
        elif ord(char) >= 0x1F300:  # Emoji range
            width += 2
        else:
            width += 1
    return width


def pad_to_width(s: str, target_width: int) -> str:
    """Pad a string to reach target display width."""
    current_width = display_width(s)
    padding = target_width - current_width
    if padding > 0:
        return s + " " * padding
    return s


def load_agent_stats(milestone_dir: Path) -> Dict:
    """Load agent_stats.json from milestone directory.

    Returns dict with cost, turns, duration, agent_framework, model or empty dict if not available.

    Duration handling:
    - New format (has wall_clock_ms): duration_ms is already active duration (session-aware)
    - Old format (no wall_clock_ms): re-compute active duration from all_tool_calls via gap detection
    """
    stats_path = milestone_dir / "agent_stats.json"
    if not stats_path.exists():
        return {}
    try:
        with open(stats_path) as f:
            stats = json.load(f)
            summary = stats.get("summary", {})
            duration_ms = summary.get("duration_ms")

            # If wall_clock_ms is present, this is new format - duration_ms is already correct
            # If wall_clock_ms is absent, this is old format - re-compute from tool calls
            if "wall_clock_ms" not in summary and duration_ms and duration_ms > 0:
                duration_ms = _recompute_active_duration(stats) or duration_ms

            return {
                "cost": summary.get("total_cost_usd"),
                "turns": summary.get("total_turns"),
                "duration": duration_ms if duration_ms and duration_ms > 0 else None,
                "agent_framework": stats.get("agent_framework"),
                "model": stats.get("model"),
            }
    except Exception:
        return {}


def _recompute_active_duration(stats: Dict) -> Optional[int]:
    """Re-compute active duration from all_tool_calls using gap detection.

    Used for old-format agent_stats.json that doesn't have session-aware duration.
    """
    from datetime import datetime as _dt

    GAP_THRESHOLD_MS = 30 * 60 * 1000  # 30 minutes

    tool_calls = stats.get("all_tool_calls", [])
    if not tool_calls:
        return None

    # Extract and sort timestamps
    timestamps = []
    for tc in tool_calls:
        ts_str = tc.get("timestamp")
        if ts_str:
            try:
                timestamps.append(_dt.fromisoformat(ts_str.rstrip("Z")))
            except (ValueError, TypeError):
                continue

    if len(timestamps) < 2:
        return None

    timestamps.sort()

    # Sum gaps between consecutive tool calls, capping at threshold
    active_ms = 0
    for i in range(1, len(timestamps)):
        gap = int((timestamps[i] - timestamps[i - 1]).total_seconds() * 1000)
        if gap <= GAP_THRESHOLD_MS:
            active_ms += gap

    return active_ms if active_ms > 0 else None


def load_agent_cost(milestone_dir: Path) -> Optional[float]:
    """Load cost from agent_stats.json in milestone directory.

    Returns cost in USD or None if not available.
    """
    stats = load_agent_stats(milestone_dir)
    return stats.get("cost")


def load_agent_duration_from_log(milestone_dir: Path) -> Optional[int]:
    """Load agent execution duration from milestone_runner.log.

    Parses the time difference between Phase 3 (Running agent) and Phase 4.
    Returns duration in milliseconds or None if not available.
    """
    import re
    from datetime import datetime

    # Check both possible locations for log file
    log_path = milestone_dir / "log" / "milestone_runner.log"
    if not log_path.exists():
        log_path = milestone_dir / "milestone_runner.log"
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            content = f.read()

        # Pattern: 2026-01-27 05:59:50,707 [INFO] ... Phase 3: Running agent
        # Use findall + last match to handle retries correctly
        phase3_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Phase 3: Running agent"
        phase4_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Phase 4:"

        phase3_matches = re.findall(phase3_pattern, content)
        phase4_matches = re.findall(phase4_pattern, content)

        phase3_match = phase3_matches[-1] if phase3_matches else None
        phase4_match = phase4_matches[-1] if phase4_matches else None

        if not phase3_match or not phase4_match:
            return None

        time_format = "%Y-%m-%d %H:%M:%S,%f"
        phase3_time = datetime.strptime(phase3_match, time_format)
        phase4_time = datetime.strptime(phase4_match, time_format)

        duration_ms = int((phase4_time - phase3_time).total_seconds() * 1000)
        return duration_ms if duration_ms > 0 else None
    except Exception:
        return None


def format_duration(duration_ms: Optional[int]) -> str:
    """Format duration in milliseconds as minutes with 2 decimal places."""
    if duration_ms is None:
        return "-"
    minutes = duration_ms / 1000 / 60
    return f"{minutes:.2f} min"


def load_e2e_trial_cost(workspace_root: Path, trial: str) -> Optional[float]:
    """Load total cost from e2e trial's agent_stats.json.

    Returns total cost in USD or None if not available.
    """
    stats_path = workspace_root / "e2e_trial" / trial / "agent_stats.json"
    if not stats_path.exists():
        return None
    try:
        with open(stats_path) as f:
            stats = json.load(f)
            return stats.get("summary", {}).get("total_cost_usd")
    except Exception:
        return None


def load_e2e_trial_turns(workspace_root: Path, trial: str) -> Optional[int]:
    """Load total turns from e2e trial's agent_stats.json.

    Returns total turns or None if not available.
    """
    stats_path = workspace_root / "e2e_trial" / trial / "agent_stats.json"
    if not stats_path.exists():
        return None
    try:
        with open(stats_path) as f:
            stats = json.load(f)
            return stats.get("summary", {}).get("total_turns")
    except Exception:
        return None


def load_e2e_trial_duration(workspace_root: Path, trial: str) -> Optional[int]:
    """Load e2e trial duration from agent_stats.json.

    Uses the sum of all session durations (duration_ms) which represents
    actual agent working time, excluding gaps between sessions (e.g. resume delays).
    Falls back to orchestrator.log wall-clock time if agent_stats.json is unavailable.
    Returns duration in milliseconds or None if not available.
    """
    # Primary: read from agent_stats.json
    stats_path = workspace_root / "e2e_trial" / trial / "agent_stats.json"
    if stats_path.exists():
        try:
            with open(stats_path) as f:
                stats = json.load(f)
            duration_ms = stats.get("summary", {}).get("duration_ms")
            if duration_ms and duration_ms > 0:
                return duration_ms
        except Exception:
            pass

    # Fallback: parse orchestrator.log wall-clock time
    import re
    from datetime import datetime

    log_path = workspace_root / "e2e_trial" / trial / "orchestrator.log"
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            content = f.read()

        time_format = "%Y-%m-%d %H:%M:%S,%f"

        start_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*Agent started \(first run\)"
        start_match = re.search(start_pattern, content)

        end_pattern = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*E2E Trial (?:COMPLETED|INCOMPLETE)"
        end_matches = re.findall(end_pattern, content)

        if not start_match or not end_matches:
            return None

        start_time = datetime.strptime(start_match.group(1), time_format)
        end_time = datetime.strptime(end_matches[-1], time_format)

        duration_ms = int((end_time - start_time).total_seconds() * 1000)
        return duration_ms if duration_ms > 0 else None
    except Exception:
        return None


def load_evaluation_result(result_path: Path, prefer_filtered: bool = True) -> Tuple[Optional[Dict], Optional[str]]:
    """Load evaluation result JSON file.

    Args:
        result_path: Path to evaluation_result.json
        prefer_filtered: If True, try to load evaluation_result_filtered.json first

    Returns:
        Tuple of (result_dict, result_type) where result_type is 'filtered', 'unfiltered', or None
    """
    if prefer_filtered:
        # Try filtered version first
        filtered_path = result_path.parent / "evaluation_result_filtered.json"
        if filtered_path.exists():
            try:
                with open(filtered_path) as f:
                    return json.load(f), "filtered"
            except Exception as e:
                print(f"Warning: Failed to load {filtered_path}: {e}", file=sys.stderr)

    # Fall back to regular evaluation_result.json
    if not result_path.exists():
        return None, None
    try:
        with open(result_path) as f:
            return json.load(f), "unfiltered"
    except Exception as e:
        print(f"Warning: Failed to load {result_path}: {e}", file=sys.stderr)
        return None, None


def check_compilation_failure(result: Dict) -> bool:
    """Check if the result indicates a compilation failure."""
    if not result:
        return False

    # Check for compilation failure in patch status
    patch_status = result.get("patch_status", {})
    compilation_success = patch_status.get("compilation_success")

    # If compilation_success is explicitly False
    if compilation_success is False:
        return True

    # Check test summary for signs of compilation failure
    test_summary = result.get("test_summary", {})
    total = test_summary.get("total", 0)

    # If no tests ran at all, it might be compilation failure
    if total == 0:
        return True

    return False


def is_resolved(result: Dict) -> bool:
    """Check if a result is resolved/passed, handling both mstone and e2e formats."""
    if not result:
        return False
    # mstone format uses "resolved"
    if "resolved" in result:
        return result.get("resolved", False)
    # e2e format uses "eval_status"
    if "eval_status" in result:
        return result.get("eval_status") == "passed"
    return False


def score_result(result: Dict) -> Tuple[int, int, int, int]:
    """
    Score a result for comparison. Higher is better.
    Returns: (resolved, f2p_achieved, n2p_achieved, p2p_achieved)
    """
    if not result:
        return (-1, -1, -1, -1)

    # Check for compilation failure
    if check_compilation_failure(result):
        return (-2, -2, -2, -2)

    resolved = 1 if is_resolved(result) else 0
    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    p2p_achieved = ts.get("pass_to_pass_achieved", 0)

    return (resolved, f2p_achieved, n2p_achieved, p2p_achieved)


def format_ratio(achieved: int, required: int) -> str:
    """Format a ratio with check mark if complete (and not 0/0)."""
    if required == 0:
        return "-"
    elif achieved == required:
        return f"✅ {achieved}/{required}"
    elif achieved == 0:
        return f"{achieved}/{required}"
    else:
        return f"⚠️ {achieved}/{required}"


def format_p2p(result: Dict) -> str:
    """Format P2P with check mark if perfect."""
    # For compilation failures, show "-"
    if check_compilation_failure(result):
        return "-"

    ts = result.get("test_summary", {})
    achieved = ts.get("pass_to_pass_achieved", 0)
    required = ts.get("pass_to_pass_required", 0)
    failed = ts.get("pass_to_pass_failed", 0)
    missing = ts.get("pass_to_pass_missing", 0)

    if achieved == required and failed == 0 and missing == 0 and required > 0:
        return f"✅ {achieved}/{required}"
    else:
        return f"{achieved}/{required}"


def get_status(result: Dict) -> str:
    """Get milestone status."""
    if not result:
        return "❌ 未运行"

    # Check for e2e not_run status first (before compilation check)
    if result.get("eval_status") == "not_run":
        return "⏳ 未运行"

    # Check for synthetic results (agent timeout/killed, no evaluation produced)
    if result.get("_synthetic"):
        failure_reason = result.get("_failure_reason", "unknown")
        if failure_reason == "compilation_failure":
            return "❌ 编译失败"
        elif failure_reason == "no_result":
            return "❌ 运行失败"
        return "❌ 未知错误"

    if check_compilation_failure(result):
        return "❌ 编译失败"

    # Check for e2e error status
    if result.get("eval_status") == "error":
        return "❌ ERROR"

    if is_resolved(result):
        return "✅ RESOLVED"

    return "❌"


def get_failure_note(result: Dict, milestone_id: str = "") -> str:
    """Generate a brief note explaining the failure reason."""
    if not result:
        return "未运行"

    # Check for e2e not_run status first
    if result.get("eval_status") == "not_run":
        return "未运行"

    # Check for synthetic results
    if result.get("_synthetic"):
        failure_reason = result.get("_failure_reason", "unknown")
        if failure_reason == "compilation_failure":
            return "编译失败(无测试报告)"
        elif failure_reason == "no_result":
            return "运行失败"
        return "未知错误"

    if check_compilation_failure(result):
        return "编译失败"

    # Check for e2e error status
    if result.get("eval_status") == "error":
        error_msg = result.get("error", "评估错误")
        return error_msg[:25] if len(error_msg) > 25 else error_msg

    if is_resolved(result):
        return "-"

    # Fallback to auto-generated notes
    ts = result.get("test_summary", {})
    f2p_a = ts.get("fail_to_pass_achieved", 0)
    f2p_r = ts.get("fail_to_pass_required", 0)
    n2p_a = ts.get("none_to_pass_achieved", 0)
    n2p_r = ts.get("none_to_pass_required", 0)
    p2p_failed = ts.get("pass_to_pass_failed", 0)
    p2p_missing = ts.get("pass_to_pass_missing", 0)

    issues = []

    # Check F2P
    if f2p_r > 0 and f2p_a < f2p_r:
        issues.append(f"F2P-{f2p_r - f2p_a}")

    # Check N2P
    if n2p_r > 0:
        if n2p_a == 0:
            issues.append(f"N2P未完成")
        elif n2p_a < n2p_r:
            issues.append(f"N2P-{n2p_r - n2p_a}")

    # Check P2P
    if p2p_failed > 0:
        issues.append(f"{p2p_failed}回归")
    if p2p_missing > 0:
        issues.append(f"{p2p_missing}缺失")

    if not issues:
        return "其他原因"

    return ", ".join(issues)


def is_milestone_dir(item: Path) -> bool:
    """Check if a directory is a milestone directory.

    A milestone directory is identified by:
    1. Having an evaluation_result.json file, OR
    2. Having a log/ subdirectory (indicates milestone run was attempted), OR
    3. Name matches common milestone patterns (milestone_*, M###, etc.)
    """
    if not item.is_dir():
        return False

    # Check for evaluation result file
    if (item / "evaluation_result.json").exists():
        return True

    # Check for log directory (indicates milestone was run)
    if (item / "log").is_dir():
        return True

    # Check for common milestone naming patterns
    name = item.name
    # Pattern: milestone_XXX, M###, M###.#, etc.
    if name.startswith("milestone_"):
        return True
    if (
        name.startswith("M")
        and len(name) > 1
        and (name[1:].replace(".", "").replace("-", "").isdigit() or name[1:2].isdigit())
    ):
        return True

    return False


def sort_milestone_key(name: str) -> Tuple[str, int, int]:
    """Generate a sort key for milestone names to ensure proper ordering.

    Handles formats like: M001, M001.1, M001.2, M002, milestone_001, etc.
    """
    import re

    # Try to extract numeric parts for proper sorting
    # Pattern for M###.# format
    match = re.match(r"^M(\d+)(?:\.(\d+))?$", name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        return ("M", major, minor)

    # Pattern for milestone_### format
    match = re.match(r"^milestone_(\d+)(?:_sub-(\d+))?", name)
    if match:
        major = int(match.group(1))
        minor = int(match.group(2)) if match.group(2) else 0
        return ("milestone", major, minor)

    # Fallback: alphabetical sorting
    return (name, 0, 0)


def load_e2e_execution_order(workspace_root: Path, trial: str) -> Optional[List[str]]:
    """Load milestone execution order from e2e trial's summary.json by timestamp.

    Returns a list of milestone IDs sorted by execution time, or None if not available.
    """
    from datetime import datetime

    summary_path = workspace_root / "e2e_trial" / trial / "evaluation" / "summary.json"
    if not summary_path.exists():
        return None

    try:
        with open(summary_path) as f:
            summary = json.load(f)

        results = summary.get("results", {})
        if not results:
            return None

        # Parse timestamps and sort
        milestone_times = []
        for milestone_id, data in results.items():
            timestamp_str = data.get("timestamp")
            if timestamp_str:
                try:
                    # Format: "Tue Jan 27 07:24:26 2026"
                    timestamp = datetime.strptime(timestamp_str, "%a %b %d %H:%M:%S %Y")
                    milestone_times.append((milestone_id, timestamp))
                except ValueError:
                    # If parsing fails, use a very early time
                    milestone_times.append((milestone_id, datetime.min))
            else:
                milestone_times.append((milestone_id, datetime.min))

        # Sort by timestamp
        milestone_times.sort(key=lambda x: x[1])
        return [m[0] for m in milestone_times]

    except Exception as e:
        print(f"Warning: Failed to load execution order from {summary_path}: {e}", file=sys.stderr)
        return None


def make_custom_sort_key(custom_order: List[str]):
    """Create a sort key function based on custom order.

    Milestones in custom_order are sorted by their index.
    Milestones not in custom_order are sorted after, using default sorting.
    """

    def sort_key(name: str):
        if name in custom_order:
            return (0, custom_order.index(name), 0, 0)
        else:
            # Put non-listed milestones at the end, sorted by default
            default_key = sort_milestone_key(name)
            return (1, 0, default_key[1], default_key[2])

    return sort_key


def find_milestones(workspace_root: Path, trials: List[str]) -> List[str]:
    """Find all milestones across the given trials (mstone format)."""
    milestones = set()

    for trial in trials:
        trial_dir = workspace_root / "mstone_trial" / trial
        if trial_dir.exists():
            for item in trial_dir.iterdir():
                if is_milestone_dir(item):
                    milestones.add(item.name)

    return sorted(milestones, key=sort_milestone_key)


def find_milestones_e2e(workspace_root: Path, trials: List[str]) -> List[str]:
    """Find all milestones across the given e2e trials.

    Checks both summary.json and individual evaluation directories.
    """
    milestones = set()

    for trial in trials:
        eval_dir = workspace_root / "e2e_trial" / trial / "evaluation"

        # Check summary.json
        summary_path = eval_dir / "summary.json"
        if summary_path.exists():
            try:
                with open(summary_path) as f:
                    summary = json.load(f)
                    results = summary.get("results", {})
                    milestones.update(results.keys())
            except Exception as e:
                print(f"Warning: Failed to load {summary_path}: {e}", file=sys.stderr)

        # Also check for milestone directories with evaluation_result.json
        if eval_dir.exists():
            for item in eval_dir.iterdir():
                if item.is_dir() and item.name.startswith("M"):
                    if (item / "evaluation_result.json").exists():
                        milestones.add(item.name)

    return sorted(milestones, key=sort_milestone_key)


def load_e2e_results(
    workspace_root: Path, trial: str, prefer_filtered: bool = True
) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Load all milestone results from an e2e trial.

    First loads from summary.json, then checks individual evaluation_result.json
    files to supplement missing results and correct eval_status based on the
    authoritative 'resolved' field.

    Returns:
        Tuple of (results, result_type_counts) where result_type_counts
        tracks how many results were loaded from 'filtered' vs 'unfiltered' files.
    """
    results = {}
    result_type_counts = {"filtered": 0, "unfiltered": 0}
    eval_dir = workspace_root / "e2e_trial" / trial / "evaluation"

    # First, load from summary.json
    summary_path = eval_dir / "summary.json"
    if summary_path.exists():
        try:
            with open(summary_path) as f:
                summary = json.load(f)
                results = summary.get("results", {})
        except Exception as e:
            print(f"Warning: Failed to load {summary_path}: {e}", file=sys.stderr)

    # Then, check ALL evaluation_result files to supplement or correct results
    # The evaluation_result.json 'resolved' field is the authoritative source
    if eval_dir.exists():
        for item in eval_dir.iterdir():
            if is_milestone_dir(item):
                milestone_id = item.name
                result_file = item / "evaluation_result.json"

                # Try to load the result (filtered or unfiltered based on preference)
                eval_result, result_type = load_evaluation_result(result_file, prefer_filtered)

                if eval_result:
                    resolved = eval_result.get("resolved", False)
                    correct_status = "passed" if resolved else "failed"

                    if milestone_id not in results:
                        # Add new result from evaluation_result.json
                        results[milestone_id] = {
                            "eval_status": correct_status,
                            "test_summary": eval_result.get("test_summary", {}),
                            "_from_eval_result": True,
                        }
                        if result_type:
                            result_type_counts[result_type] += 1
                    else:
                        # Correct eval_status if it doesn't match resolved field
                        if results[milestone_id].get("eval_status") != correct_status:
                            results[milestone_id]["eval_status"] = correct_status
                            results[milestone_id]["_corrected"] = True
                        # Replace test_summary with filtered data when available
                        if result_type == "filtered":
                            results[milestone_id]["test_summary"] = eval_result.get("test_summary", {})
                            result_type_counts["filtered"] += 1

    return results, result_type_counts


def check_log_for_failure(log_path: Path) -> Optional[str]:
    """Check milestone runner log for failure reason.

    Returns a failure reason string if found, None otherwise.
    """
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            content = f.read()

        # Check for common failure patterns
        if "RuntimeError: No valid test report files generated" in content:
            return "compilation_failure"
        if "[ERROR]" in content and ("compilation" in content.lower() or "compile" in content.lower()):
            return "compilation_failure"
        if "BUILD FAILURE" in content:
            return "compilation_failure"

        return "unknown_failure"
    except Exception:
        return None


def compare_trials(
    workspace_root: Path, trials: List[str], prefer_filtered: bool = True
) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Compare trials and return best result for each milestone.

    Returns:
        Tuple of (best_results, result_type_counts) where result_type_counts
        tracks how many results were loaded from 'filtered' vs 'unfiltered' files.
    """
    milestones = find_milestones(workspace_root, trials)

    best_results = {}
    result_type_counts = {"filtered": 0, "unfiltered": 0, "synthetic": 0}

    for milestone in milestones:
        best_result = None
        best_trial = None
        best_score = (-999, -999, -999, -999)
        best_result_type = None
        has_any_attempt = False

        for trial in trials:
            trial_dir = workspace_root / "mstone_trial" / trial / milestone
            result_path = trial_dir / "evaluation" / "evaluation_result.json"
            result, result_type = load_evaluation_result(result_path, prefer_filtered)

            # Check if this trial was attempted (has log directory)
            log_path = trial_dir / "log" / "milestone_runner.log"
            if log_path.exists() or (trial_dir / "log").is_dir():
                has_any_attempt = True

            if result is None:
                # Check log for failure reason
                failure_reason = check_log_for_failure(log_path)
                if failure_reason == "compilation_failure":
                    # Create synthetic result for compilation failure
                    synthetic_result = {
                        "milestone_id": milestone,
                        "resolved": False,
                        "patch_status": {"compilation_success": False},
                        "test_summary": {"total": 0},
                        "_synthetic": True,
                        "_failure_reason": "compilation_failure",
                    }
                    score = score_result(synthetic_result)
                    if score > best_score:
                        best_score = score
                        best_result = synthetic_result
                        best_trial = trial
                        best_result_type = "synthetic"
                continue

            score = score_result(result)

            if score > best_score:
                best_score = score
                best_result = result
                best_trial = trial
                best_result_type = result_type

        # Include milestone if we have a result OR if there was an attempt
        if best_result:
            # Load cost, duration, and turns from the best trial
            cost = None
            duration = None
            turns = None
            agent_framework = None
            model = None
            if best_trial:
                trial_dir = workspace_root / "mstone_trial" / best_trial / milestone
                agent_stats = load_agent_stats(trial_dir)
                cost = agent_stats.get("cost")
                turns = agent_stats.get("turns")
                agent_framework = agent_stats.get("agent_framework")
                model = agent_stats.get("model")
                duration = agent_stats.get("duration") or load_agent_duration_from_log(trial_dir)
            best_results[milestone] = {
                "result": best_result,
                "trial": best_trial,
                "score": best_score,
                "cost": cost,
                "duration": duration,
                "turns": turns,
                "agent_framework": agent_framework,
                "model": model,
            }
            if best_result_type:
                result_type_counts[best_result_type] += 1
        elif has_any_attempt:
            # Milestone was attempted but no result - mark as failed
            # Try to get cost, duration, and turns from any trial that attempted this milestone
            cost = None
            duration = None
            turns = None
            agent_framework = None
            model = None
            for trial in trials:
                trial_dir = workspace_root / "mstone_trial" / trial / milestone
                agent_stats = load_agent_stats(trial_dir)
                cost = agent_stats.get("cost")
                turns = agent_stats.get("turns")
                agent_framework = agent_stats.get("agent_framework")
                model = agent_stats.get("model")
                duration = agent_stats.get("duration") or load_agent_duration_from_log(trial_dir)
                if cost is not None or duration is not None:
                    break
            best_results[milestone] = {
                "result": {
                    "milestone_id": milestone,
                    "resolved": False,
                    "test_summary": {"total": 0},
                    "_synthetic": True,
                    "_failure_reason": "no_result",
                },
                "trial": None,
                "score": (-999, -999, -999, -999),
                "cost": cost,
                "duration": duration,
                "turns": turns,
                "agent_framework": agent_framework,
                "model": model,
            }
            result_type_counts["synthetic"] += 1

    return best_results, result_type_counts


def compare_trials_e2e(
    workspace_root: Path,
    trials: List[str],
    prefer_filtered: bool = True,
    selected_milestones: Optional[Set[str]] = None,
) -> Tuple[Dict[str, Dict], Dict[str, int]]:
    """Compare e2e trials and return best result for each milestone.

    Args:
        workspace_root: Path to workspace root
        trials: List of trial names to compare
        prefer_filtered: Whether to prefer filtered results
        selected_milestones: Optional set of selected milestone IDs to include
            even if they have no results (will show as "未运行")

    Returns:
        Tuple of (best_results, result_type_counts) where result_type_counts
        tracks how many results were loaded from 'filtered' vs 'unfiltered' files.
    """
    milestones = find_milestones_e2e(workspace_root, trials)

    # Also include selected milestones even if they have no results
    if selected_milestones:
        milestones = sorted(set(milestones) | selected_milestones, key=sort_milestone_key)

    # Load all results from all trials
    trial_results: Dict[str, Dict[str, Dict]] = {}
    total_type_counts = {"filtered": 0, "unfiltered": 0, "synthetic": 0}
    for trial in trials:
        results, type_counts = load_e2e_results(workspace_root, trial, prefer_filtered)
        trial_results[trial] = results
        for k, v in type_counts.items():
            if k in total_type_counts:
                total_type_counts[k] += v

    best_results = {}

    for milestone in milestones:
        best_result = None
        best_trial = None
        best_score = (-999, -999, -999, -999)

        for trial in trials:
            result = trial_results.get(trial, {}).get(milestone)

            if result is None:
                continue

            score = score_result(result)

            if score > best_score:
                best_score = score
                best_result = result
                best_trial = trial

        if best_result:
            # Load cost from agent_stats.json for e2e trials
            cost = None
            if best_trial:
                eval_dir = workspace_root / "e2e_trial" / best_trial / "evaluation" / milestone
                cost = load_agent_cost(eval_dir)
            best_results[milestone] = {
                "result": best_result,
                "trial": best_trial,
                "score": best_score,
                "cost": cost,
            }
        elif selected_milestones and milestone in selected_milestones:
            # Milestone is in selected list but has no results - add placeholder
            best_results[milestone] = {
                "result": {
                    "milestone_id": milestone,
                    "resolved": False,
                    "eval_status": "not_run",
                    "test_summary": {},
                    "_synthetic": True,
                    "_failure_reason": "no_result",
                },
                "trial": None,
                "score": (-999, -999, -999, -999),
                "cost": None,
            }
            total_type_counts["synthetic"] += 1

    return best_results, total_type_counts


def format_cost(cost: Optional[float]) -> str:
    """Format cost in USD."""
    if cost is None:
        return "-"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def calculate_score(result: Dict) -> Optional[float]:
    """Calculate milestone score (Algorithm V1).

    Formula: (F2P_achieved + N2P_achieved) / (F2P_required + N2P_required) * (P2P_achieved / P2P_required)

    Uses ratio-based P2P penalty instead of absolute numbers, which is fairer for
    projects with large test suites.

    Returns None if the result is invalid, 0.0 if compilation failure.
    """
    if not result:
        return None

    if check_compilation_failure(result):
        return 0.0

    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    f2p_required = ts.get("fail_to_pass_required", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    n2p_required = ts.get("none_to_pass_required", 0)
    p2p_achieved = ts.get("pass_to_pass_achieved", 0)
    p2p_required = ts.get("pass_to_pass_required", 0)

    # Calculate the first part: (F2P + N2P) ratio
    total_required = f2p_required + n2p_required
    if total_required == 0:
        # If no F2P or N2P required, treat as 100% achieved
        first_part = 1.0
    else:
        first_part = (f2p_achieved + n2p_achieved) / total_required

    # Calculate the second part: P2P ratio penalty
    # Uses ratio instead of absolute numbers for fairness across different project sizes
    if p2p_required > 0:
        second_part = p2p_achieved / p2p_required
    else:
        second_part = 1.0

    return first_part * second_part


def calculate_score_v2(result: Dict) -> Optional[float]:
    """Calculate milestone score (Algorithm V2).

    Formula: (F2P_achieved + N2P_achieved) / (F2P_required + N2P_required) * max(0, 1 - P2P_missed / min(1000, P2P_required))

    This algorithm:
    - Uses F2P + N2P for the main score (same as V1)
    - Caps P2P penalty denominator at 1000 to avoid over-penalizing large test suites
    - P2P_missed = P2P_required - P2P_achieved

    Returns None if the result is invalid, 0.0 if compilation failure.
    """
    if not result:
        return None

    if check_compilation_failure(result):
        return 0.0

    ts = result.get("test_summary", {})

    f2p_achieved = ts.get("fail_to_pass_achieved", 0)
    f2p_required = ts.get("fail_to_pass_required", 0)
    n2p_achieved = ts.get("none_to_pass_achieved", 0)
    n2p_required = ts.get("none_to_pass_required", 0)
    p2p_achieved = ts.get("pass_to_pass_achieved", 0)
    p2p_required = ts.get("pass_to_pass_required", 0)

    # Calculate the first part: (F2P + N2P) ratio
    total_required = f2p_required + n2p_required
    if total_required == 0:
        # If no F2P or N2P required, treat as 100% achieved
        first_part = 1.0
    else:
        first_part = (f2p_achieved + n2p_achieved) / total_required

    # Calculate the second part: P2P penalty with capped denominator
    # P2P_missed = P2P_required - P2P_achieved
    p2p_missed = p2p_required - p2p_achieved
    if p2p_required > 0:
        capped_p2p = min(1000, p2p_required)
        second_part = max(0.0, 1.0 - p2p_missed / capped_p2p)
    else:
        second_part = 1.0

    return first_part * second_part


def format_score(score: Optional[float]) -> str:
    """Format score as percentage for display."""
    if score is None:
        return "-"
    return f"{score * 100:.2f}%"


def print_comparison_table(
    best_results: Dict[str, Dict],
    non_graded_milestones: Set[str] = None,
    show_cost_column: bool = True,
    total_cost: Optional[float] = None,
    show_time_column: bool = False,
    custom_sort_key=None,
    trial_names: List[str] = None,
    workspace_root: Path = None,
    trial_type: str = None,
    total_duration: Optional[int] = None,
    total_turns: Optional[int] = None,
):
    """Print comparison table.

    Args:
        best_results: Dictionary of milestone results
        non_graded_milestones: Set of milestone IDs that are not graded
        show_cost_column: Whether to show per-milestone Cost column (default True)
        total_cost: Total cost to display in summary (if None, sum from per-milestone costs)
        show_time_column: Whether to show per-milestone Time column (default False, only for mstone)
        custom_sort_key: Optional custom sort key function for milestone ordering
        trial_names: List of trial names being compared
        workspace_root: Path to workspace root for extracting repo info
        trial_type: Type of trial ('mstone' or 'e2e')
        total_duration: Total duration in ms for e2e trials (from orchestrator.log)
        total_turns: Total turns for e2e trials (from agent_stats.json)
    """
    if non_graded_milestones is None:
        non_graded_milestones = set()

    # Sort milestones by custom order if provided, otherwise by name
    sort_key = custom_sort_key if custom_sort_key else sort_milestone_key
    sorted_milestones = sorted(best_results.keys(), key=sort_key)

    # Pre-calculate all notes, scores and stats
    notes = {}
    scores = {}
    scores_v2 = {}
    resolved_count = 0
    graded_count = 0  # Count of milestones that are graded (not in non_graded_milestones)
    sum_cost = 0.0
    sum_duration = 0  # Total duration in milliseconds
    sum_turns = 0  # Total turns
    sum_score = 0.0
    sum_score_v2 = 0.0
    score_count = 0  # Count of milestones with valid scores
    agent_framework = None
    model = None

    for milestone in sorted_milestones:
        data = best_results[milestone]
        result = data["result"]
        cost = data.get("cost")
        duration = data.get("duration")
        turns = data.get("turns")
        notes[milestone] = get_failure_note(result, milestone)
        scores[milestone] = calculate_score(result)
        scores_v2[milestone] = calculate_score_v2(result)

        # Extract agent_framework and model from first available
        if agent_framework is None and data.get("agent_framework"):
            agent_framework = data.get("agent_framework")
        if model is None and data.get("model"):
            model = data.get("model")

        # Only count graded milestones for pass rate and score
        if milestone not in non_graded_milestones:
            graded_count += 1
            if is_resolved(result):
                resolved_count += 1
            # Sum scores for graded milestones only
            if scores[milestone] is not None:
                sum_score += scores[milestone]
            if scores_v2[milestone] is not None:
                sum_score_v2 += scores_v2[milestone]
                score_count += 1

        # Cost includes all milestones
        if cost is not None:
            sum_cost += cost

        # Duration includes all milestones
        if duration is not None:
            sum_duration += duration

        # Turns includes all milestones
        if turns is not None:
            sum_turns += turns

    # Use provided total_cost or sum from per-milestone costs
    display_cost = total_cost if total_cost is not None else sum_cost

    # Print trial info at top
    if workspace_root:
        # Extract repo range from workspace root path (last 2 components before workspace_root)
        repo_range = "/".join(workspace_root.parts[-2:]) if len(workspace_root.parts) >= 2 else str(workspace_root)
        print(f"📁 Repo: {repo_range}")
    if trial_names:
        trial_str = (
            ", ".join(trial_names)
            if len(trial_names) <= 3
            else f"{', '.join(trial_names[:3])}... ({len(trial_names)} trials)"
        )
        print(f"🏃 Trial: {trial_str}")
    if trial_type:
        print(f"📊 Type: {trial_type}")

    # Print agent info
    if agent_framework or model:
        agent_info = f"🤖 Agent: {agent_framework or 'unknown'} | Model: {model or 'unknown'}"
        print(agent_info)
    print()

    # Print summary at top (pass rate based on graded milestones only)
    non_graded_count = len(sorted_milestones) - graded_count
    pass_rate = resolved_count * 100 / graded_count if graded_count > 0 else 0.0

    # Use total_duration/total_turns if provided (e2e), otherwise use summed values (mstone)
    display_duration = total_duration if total_duration is not None else sum_duration
    display_turns = total_turns if total_turns is not None else sum_turns

    # Show duration and turns in summary (for both mstone and e2e when available)
    time_suffix = (
        f" | Duration: {format_duration(display_duration)}" if display_duration and display_duration > 0 else ""
    )
    turns_suffix = f" | Turns: {display_turns}" if display_turns and display_turns > 0 else ""
    avg_score = sum_score / graded_count if graded_count > 0 else 0.0
    avg_score_v2 = sum_score_v2 / graded_count if graded_count > 0 else 0.0

    if non_graded_count > 0:
        summary_text = f"Score-1000: {avg_score_v2 * 100:.2f}% | Score-full: {avg_score * 100:.2f}% | Resolve: {pass_rate:.2f}% ({resolved_count}/{graded_count}, 排除{non_graded_count}个不计分) | Cost: ${display_cost:.2f}{time_suffix}{turns_suffix}"
    else:
        summary_text = f"Score-1000: {avg_score_v2 * 100:.2f}% | Score-full: {avg_score * 100:.2f}% | Resolve: {pass_rate:.2f}% ({resolved_count}/{graded_count}) | Cost: ${display_cost:.2f}{time_suffix}{turns_suffix}"

    # Print summary with prominent border
    summary_width = display_width(summary_text) + 4
    print("┏" + "━" * summary_width + "┓")
    print("┃  " + summary_text + "  ┃")
    print("┗" + "━" * summary_width + "┛")
    print()

    # Calculate dynamic column widths
    milestone_width = max(len("Milestone"), max(display_width(m) for m in sorted_milestones)) + 2

    # Fixed widths for other columns
    f2p_width = 10
    n2p_width = 11
    p2p_width = 14
    status_width = 15
    score_1000_width = 12  # "Score-1000"
    score_full_width = 12  # "Score-full"
    cost_width = 10
    time_width = 10
    turns_width = 6
    # Dynamic note width based on content
    note_width = max(len("备注") + 2, max(display_width(n) for n in notes.values()) + 2)

    # Build table border strings
    def make_border(left: str, mid: str, right: str) -> str:
        parts = [
            f"{left}{'─' * (milestone_width + 2)}{mid}{'─' * (f2p_width + 2)}{mid}",
            f"{'─' * (n2p_width + 2)}{mid}{'─' * (p2p_width + 2)}{mid}",
            f"{'─' * (status_width + 2)}{mid}{'─' * (score_1000_width + 2)}{mid}{'─' * (score_full_width + 2)}{mid}",
        ]
        if show_cost_column:
            parts.append(f"{'─' * (cost_width + 2)}{mid}")
        if show_time_column:
            parts.append(f"{'─' * (time_width + 2)}{mid}{'─' * (turns_width + 2)}{mid}")
        parts.append(f"{'─' * (note_width + 2)}{right}")
        return "".join(parts)

    top_border = make_border("┌", "┬", "┐")
    mid_border = make_border("├", "┼", "┤")
    bot_border = make_border("└", "┴", "┘")

    # Print header
    print(top_border)
    header_milestone = pad_to_width("Milestone", milestone_width)
    header_f2p = pad_to_width("F2P", f2p_width)
    header_n2p = pad_to_width("N2P", n2p_width)
    header_p2p = pad_to_width("P2P", p2p_width)
    header_status = pad_to_width("状态", status_width)
    header_score_1000 = pad_to_width("Score-1000", score_1000_width)
    header_score_full = pad_to_width("Score-full", score_full_width)
    header_cost = pad_to_width("Cost", cost_width)
    header_time = pad_to_width("Duration", time_width)
    header_turns = pad_to_width("Turns", turns_width)
    header_note = pad_to_width("备注", note_width)

    # Build header row based on which columns are shown
    header_parts = [
        f"│ {header_milestone} │ {header_f2p} │ {header_n2p} │ {header_p2p} │ {header_status} │ {header_score_1000} │ {header_score_full} │"
    ]
    if show_cost_column:
        header_parts.append(f" {header_cost} │")
    if show_time_column:
        header_parts.append(f" {header_time} │ {header_turns} │")
    header_parts.append(f" {header_note} │")
    print("".join(header_parts))
    print(mid_border)

    for i, milestone in enumerate(sorted_milestones):
        data = best_results[milestone]
        result = data["result"]
        cost = data.get("cost")
        duration = data.get("duration")
        turns = data.get("turns")
        ts = result.get("test_summary", {})

        # Format columns
        f2p_a = ts.get("fail_to_pass_achieved", 0)
        f2p_r = ts.get("fail_to_pass_required", 0)
        n2p_a = ts.get("none_to_pass_achieved", 0)
        n2p_r = ts.get("none_to_pass_required", 0)

        f2p_str = format_ratio(f2p_a, f2p_r)
        n2p_str = format_ratio(n2p_a, n2p_r)
        p2p_str = format_p2p(result)
        # Use non-graded status if milestone is in non-graded list
        if milestone in non_graded_milestones:
            status = "🚫 不计分"
        else:
            status = get_status(result)
        note = notes[milestone]  # Use pre-calculated note
        score_full_str = format_score(scores[milestone])  # Use pre-calculated score (full)
        score_1000_str = format_score(scores_v2[milestone])  # Use pre-calculated score (1000)
        cost_str = format_cost(cost)
        time_str = format_duration(duration)
        turns_str = str(turns) if turns is not None else "-"

        # Pad each column to correct display width
        milestone_col = pad_to_width(milestone, milestone_width)
        f2p_col = pad_to_width(f2p_str, f2p_width)
        n2p_col = pad_to_width(n2p_str, n2p_width)
        p2p_col = pad_to_width(p2p_str, p2p_width)
        status_col = pad_to_width(status, status_width)
        score_1000_col = pad_to_width(score_1000_str, score_1000_width)
        score_full_col = pad_to_width(score_full_str, score_full_width)
        cost_col = pad_to_width(cost_str, cost_width)
        time_col = pad_to_width(time_str, time_width)
        turns_col = pad_to_width(turns_str, turns_width)
        note_col = pad_to_width(note, note_width)

        # Build data row based on which columns are shown
        row_parts = [
            f"│ {milestone_col} │ {f2p_col} │ {n2p_col} │ {p2p_col} │ {status_col} │ {score_1000_col} │ {score_full_col} │"
        ]
        if show_cost_column:
            row_parts.append(f" {cost_col} │")
        if show_time_column:
            row_parts.append(f" {time_col} │ {turns_col} │")
        row_parts.append(f" {note_col} │")
        print("".join(row_parts))

        # Print separator or bottom border
        if i < len(sorted_milestones) - 1:
            print(mid_border)
        else:
            print(bot_border)


def main():
    parser = argparse.ArgumentParser(description="Compare milestone results across multiple trials")
    parser.add_argument(
        "--workspace-root",
        required=True,
        type=Path,
        help="Path to workspace root (e.g., DATA/harness_workspace/...)",
    )
    parser.add_argument(
        "--trials",
        nargs="+",
        required=True,
        help="List of trial names to compare (e.g., complete_run_001 complete_run_002)",
    )
    parser.add_argument(
        "--trial-type",
        choices=["mstone", "e2e"],
        default="mstone",
        help="Type of trials to compare: 'mstone' (default) or 'e2e'",
    )
    parser.add_argument(
        "--non-filter",
        action="store_true",
        help="Use unfiltered results (evaluation_result.json) instead of filtered results",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Show all milestones, not just those in selected_milestone_ids.txt",
    )
    parser.add_argument(
        "--sort-by-e2e",
        type=str,
        metavar="TRIAL",
        help="Sort milestones by e2e execution order from specified trial (e.g., _claude-code_sonnet-4.5-run_001_001)",
    )
    parser.add_argument(
        "--sort-order",
        type=str,
        help="Custom sort order: comma-separated milestone IDs (e.g., 'M06,M11,M01') or path to file with one ID per line",
    )

    args = parser.parse_args()

    if not args.workspace_root.exists():
        print(f"Error: Workspace root does not exist: {args.workspace_root}", file=sys.stderr)
        sys.exit(1)

    # Determine whether to prefer filtered results (default is True, --non-filter sets to False)
    prefer_filtered = not args.non_filter

    # Load selected milestones early (needed for e2e to include unrun milestones)
    selected_milestones, milestones_source = load_selected_milestones(args.workspace_root)

    # Choose comparison function based on trial type
    if args.trial_type == "e2e":
        # For e2e, pass selected_milestones unless --show-all is specified
        # This ensures all selected milestones are shown even if not yet evaluated
        include_selected = selected_milestones if not args.show_all else None
        best_results, result_type_counts = compare_trials_e2e(
            args.workspace_root, args.trials, prefer_filtered, include_selected
        )
    else:
        best_results, result_type_counts = compare_trials(args.workspace_root, args.trials, prefer_filtered)

    if not best_results:
        print("No results found for any milestone.", file=sys.stderr)
        sys.exit(1)

    # Filter results by selected milestones (unless --show-all is specified)
    # Note: selected_milestones was already loaded earlier
    total_milestones = len(best_results)
    not_selected_count = 0

    if selected_milestones is not None:
        # Count how many milestones are not in the selected list
        not_selected_count = sum(1 for k in best_results.keys() if k not in selected_milestones)

        if not args.show_all:
            # Filter to only show selected milestones
            # For e2e, we already included selected milestones in compare_trials_e2e
            # For mstone, we need to filter here
            best_results = {k: v for k, v in best_results.items() if k in selected_milestones}

    if not best_results:
        print("No results found for any milestone after filtering.", file=sys.stderr)
        sys.exit(1)

    # Print result source information
    print()
    if prefer_filtered:
        print("📋 结果来源: 优先使用 evaluation_result_filtered.json (过滤后结果)")
    else:
        print("📋 结果来源: 使用 evaluation_result.json (原始结果)")

    filtered_count = result_type_counts.get("filtered", 0)
    unfiltered_count = result_type_counts.get("unfiltered", 0)
    synthetic_count = result_type_counts.get("synthetic", 0)

    if filtered_count > 0 or unfiltered_count > 0:
        source_info = []
        if filtered_count > 0:
            source_info.append(f"过滤后: {filtered_count}")
        if unfiltered_count > 0:
            source_info.append(f"原始: {unfiltered_count}")
        if synthetic_count > 0:
            source_info.append(f"合成: {synthetic_count}")
        print(f"   加载统计: {', '.join(source_info)}")

    # Print milestone filtering info
    if selected_milestones is not None:
        if args.show_all:
            print(f"📌 显示范围: 全部 milestone ({total_milestones} 个，含 {not_selected_count} 个未选中)")
        else:
            print(
                f"📌 显示范围: 仅 {milestones_source} 中的 milestone ({len(best_results)} 个，隐藏 {not_selected_count} 个)"
            )
    else:
        print(f"📌 显示范围: 全部 milestone (未找到 selected_milestone_ids.txt 或 milestones.csv)")
    print()

    # Load non-graded milestones
    non_graded_milestones = load_non_graded_milestones(args.workspace_root)

    # Determine custom sort order
    custom_sort_key = None
    custom_order = None

    # For e2e trials, default to using the first trial's execution order
    # For mstone trials, check if there's a matching e2e trial with the same name
    sort_by_e2e_trial = args.sort_by_e2e
    if not args.sort_by_e2e and not args.sort_order:
        if args.trial_type == "e2e":
            # Use the first trial as default for e2e sorting
            sort_by_e2e_trial = args.trials[0]
        else:
            # For mstone, check if there's a matching e2e trial
            for trial in args.trials:
                e2e_trial_dir = args.workspace_root / "e2e_trial" / trial
                if e2e_trial_dir.exists():
                    sort_by_e2e_trial = trial
                    break

    if sort_by_e2e_trial:
        # Load execution order from e2e trial
        custom_order = load_e2e_execution_order(args.workspace_root, sort_by_e2e_trial)
        if custom_order:
            print(f"📊 排序方式: 按 e2e trial '{sort_by_e2e_trial}' 执行顺序")
            print(f"   执行顺序: {', '.join(custom_order)}")
            custom_sort_key = make_custom_sort_key(custom_order)
        else:
            print(f"Warning: Could not load execution order from e2e trial '{sort_by_e2e_trial}'", file=sys.stderr)
    elif args.sort_order:
        # Check if it's a file path or comma-separated list
        sort_order_path = Path(args.sort_order)
        if sort_order_path.exists():
            # Load from file
            try:
                with open(sort_order_path) as f:
                    custom_order = [line.strip() for line in f if line.strip()]
                print(f"📊 排序方式: 按文件 '{args.sort_order}' 中的顺序")
            except Exception as e:
                print(f"Warning: Failed to load sort order from {args.sort_order}: {e}", file=sys.stderr)
        else:
            # Parse as comma-separated list
            custom_order = [m.strip() for m in args.sort_order.split(",") if m.strip()]
            print(f"📊 排序方式: 按指定顺序")

        if custom_order:
            print(f"   排序顺序: {', '.join(custom_order)}")
            custom_sort_key = make_custom_sort_key(custom_order)

    print()

    # For e2e trials, load total cost, duration, turns from trial-level stats
    if args.trial_type == "e2e":
        # Load total cost, duration, turns from the first trial (or sum if multiple trials)
        total_cost = 0.0
        total_duration = 0
        total_turns = 0
        for trial in args.trials:
            trial_cost = load_e2e_trial_cost(args.workspace_root, trial)
            if trial_cost is not None:
                total_cost += trial_cost
            trial_duration = load_e2e_trial_duration(args.workspace_root, trial)
            if trial_duration is not None:
                total_duration += trial_duration
            trial_turns = load_e2e_trial_turns(args.workspace_root, trial)
            if trial_turns is not None:
                total_turns += trial_turns
        print_comparison_table(
            best_results,
            non_graded_milestones,
            show_cost_column=False,
            total_cost=total_cost,
            show_time_column=False,
            custom_sort_key=custom_sort_key,
            trial_names=args.trials,
            workspace_root=args.workspace_root,
            trial_type=args.trial_type,
            total_duration=total_duration if total_duration > 0 else None,
            total_turns=total_turns if total_turns > 0 else None,
        )
    else:
        # For mstone trials, show time column
        print_comparison_table(
            best_results,
            non_graded_milestones,
            show_cost_column=True,
            show_time_column=True,
            custom_sort_key=custom_sort_key,
            trial_names=args.trials,
            workspace_root=args.workspace_root,
            trial_type=args.trial_type,
        )


if __name__ == "__main__":
    main()
