#!/usr/bin/env python3
"""Command-line interface for test/src separation verification."""

import json
import sys
from pathlib import Path

from .analyzer import analyze_patch_test_hunks
from .verifier import verify_milestone


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Verify test/src separation in milestone patches (using ast-grep)")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Verify command (default behavior)
    verify_parser = subparsers.add_parser("verify", help="Verify test/src separation between two directories")
    verify_parser.add_argument("old_dir", help="Old baseline directory (with original patches)")
    verify_parser.add_argument("new_dir", help="New baseline directory (with updated patches)")
    verify_parser.add_argument("--milestone", help="Verify specific milestone only")
    verify_parser.add_argument("--json", action="store_true", help="Output as JSON")
    verify_parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed test hunk info")

    # Analyze command
    analyze_parser = subparsers.add_parser("analyze", help="Analyze test hunks in a single directory")
    analyze_parser.add_argument("base_dir", help="Baseline directory to analyze")
    analyze_parser.add_argument("--milestone", help="Analyze specific milestone only")
    analyze_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # Check command - verify milestone_patches has only src, milestone_patches/start_diff_patches has only test
    check_parser = subparsers.add_parser(
        "check",
        help="Check that milestone_patches has only src changes and milestone_patches/start_diff_patches has only test changes",
    )
    check_parser.add_argument("base_dir", help="Baseline directory to check")
    check_parser.add_argument("--milestone", help="Check specific milestone only")
    check_parser.add_argument("--json", action="store_true", help="Output as JSON")

    args = parser.parse_args()

    # Default to verify if no command specified (backward compatibility)
    if args.command is None:
        # Check if old positional args style
        if len(sys.argv) >= 3 and not sys.argv[1].startswith("-"):
            # Legacy mode: old_dir new_dir
            args.command = "verify"
            args.old_dir = sys.argv[1]
            args.new_dir = sys.argv[2]
            args.milestone = None
            args.json = "--json" in sys.argv
            args.verbose = "-v" in sys.argv or "--verbose" in sys.argv
        else:
            parser.print_help()
            return 1

    if args.command == "analyze":
        return run_analyze(args)
    elif args.command == "check":
        return run_check(args)
    else:
        return run_verify(args)


def run_check(args):
    """Run check command - verify milestone_patches has only src, milestone_patches/start_diff_patches has only test."""
    base_dir = Path(args.base_dir)
    src_patches_dir = base_dir / "milestone_patches"
    test_patches_dir = base_dir / "milestone_patches" / "start_diff_patches"

    if not src_patches_dir.exists():
        print(f"Error: {src_patches_dir} does not exist")
        return 1
    if not test_patches_dir.exists():
        print(f"Error: {test_patches_dir} does not exist")
        return 1

    # Get milestones
    if args.milestone:
        milestones = [args.milestone]
    else:
        src_milestones = set(f.stem for f in src_patches_dir.glob("*.patch"))
        test_milestones = set(f.stem for f in test_patches_dir.glob("*.patch"))
        milestones = sorted(src_milestones | test_milestones)

    results = {}
    all_pass = True

    for m in milestones:
        result = {"milestone": m, "src_patch": {}, "test_patch": {}}

        # Check milestone_patches (should have 0 test hunks)
        src_patch_path = src_patches_dir / f"{m}.patch"
        if src_patch_path.exists():
            src_result = analyze_patch_test_hunks(str(base_dir), m, patches_subdir="milestone_patches")
            if "error" not in src_result:
                test_count = src_result["summary"].get("test_hunks", 0)
                mixed_count = src_result["summary"].get("mixed_hunks", 0)
                result["src_patch"] = {
                    "test_hunks": test_count,
                    "src_hunks": src_result["summary"].get("src_hunks", 0),
                    "mixed_hunks": mixed_count,
                    "pass": test_count == 0 and mixed_count == 0,
                    "violations": src_result.get("test_hunks", []) + src_result.get("mixed_hunks", []),
                }
                if not result["src_patch"]["pass"]:
                    all_pass = False
            else:
                result["src_patch"] = {"error": src_result["error"]}
        else:
            result["src_patch"] = {"skip": "not found"}

        # Check milestone_patches/start_diff_patches (should have 0 src hunks)
        # Use start-old tag for test region detection since diff is from old start to current start
        test_patch_path = test_patches_dir / f"{m}.patch"
        if test_patch_path.exists():
            old_start_tag = f"milestone-{m}-start-old"
            test_result = analyze_patch_test_hunks(
                str(base_dir), m, patches_subdir="milestone_patches/start_diff_patches", ref_tag=old_start_tag
            )
            if "error" not in test_result:
                src_count = test_result["summary"].get("src_hunks", 0)
                mixed_count = test_result["summary"].get("mixed_hunks", 0)
                result["test_patch"] = {
                    "test_hunks": test_result["summary"].get("test_hunks", 0),
                    "src_hunks": src_count,
                    "mixed_hunks": mixed_count,
                    "pass": src_count == 0 and mixed_count == 0,
                    "violations": test_result.get("src_hunks", []) + test_result.get("mixed_hunks", []),
                }
                if not result["test_patch"]["pass"]:
                    all_pass = False
            else:
                result["test_patch"] = {"error": test_result["error"]}
        else:
            result["test_patch"] = {"skip": "not found"}

        results[m] = result

    if args.json:
        print(json.dumps(results, indent=2))
        return 0 if all_pass else 1

    # Print results
    print("Check test/src separation")
    print(f"Directory: {base_dir}")
    print("=" * 70)

    for m, r in results.items():
        src = r["src_patch"]
        test = r["test_patch"]

        # Determine overall status
        src_ok = src.get("pass", True) or "skip" in src or "error" in src
        test_ok = test.get("pass", True) or "skip" in test or "error" in test
        status = "✓ PASS" if (src_ok and test_ok) else "✗ FAIL"

        print(f"\n{m}: {status}")

        # milestone_patches check
        if "skip" in src:
            print(f"  [milestone_patches] skipped (not found)")
        elif "error" in src:
            print(f"  [milestone_patches] error: {src['error']}")
        else:
            if src["pass"]:
                print(f"  [milestone_patches] ✓ {src['src_hunks']} src, 0 test hunks")
            else:
                print(
                    f"  [milestone_patches] ✗ {src['src_hunks']} src, {src['test_hunks']} test, {src['mixed_hunks']} mixed hunks"
                )
                for v in src.get("violations", [])[:3]:
                    print(f"      ✗ {v['file']} lines {v['new_lines']}")

        # milestone_patches/start_diff_patches check
        if "skip" in test:
            print(f"  [start_diff_patches] skipped (not found)")
        elif "error" in test:
            print(f"  [start_diff_patches] error: {test['error']}")
        else:
            if test["pass"]:
                print(f"  [start_diff_patches] ✓ {test['test_hunks']} test, 0 src hunks")
            else:
                print(f"  [start_diff_patches] ✗ {test['test_hunks']} test, {test['src_hunks']} src hunks")
                for v in test.get("violations", [])[:3]:
                    print(f"      ✗ {v['file']} lines {v['new_lines']}")

    print(f"\n{'=' * 70}")
    print(f"Overall: {'ALL PASS ✓' if all_pass else 'SOME FAILURES ✗'}")
    return 0 if all_pass else 1


def run_analyze(args):
    """Run analyze command."""
    base_dir = args.base_dir
    patches_dir = Path(base_dir) / "milestone_patches"

    if args.milestone:
        milestones = [args.milestone]
    else:
        milestones = sorted(f.stem for f in patches_dir.glob("*.patch"))

    results = {}
    for m in milestones:
        results[m] = analyze_patch_test_hunks(base_dir, m)

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"Analyze test hunks in patches (using ast-grep)")
    print(f"Directory: {base_dir}")
    print("=" * 70)

    for m, r in results.items():
        if "error" in r:
            print(f"\n{m}: Error - {r['error']}")
            continue

        summary = r["summary"]
        parts = [f"{summary['test_hunks']} test", f"{summary['src_hunks']} src"]
        if summary.get("mixed_hunks", 0) > 0:
            parts.append(f"{summary['mixed_hunks']} mixed")
        print(f"\n{m}: {', '.join(parts)} hunks")

        if r["test_hunks"]:
            print("  Test hunks:")
            for h in r["test_hunks"]:
                print(f"    ✓ {h['file']} lines {h['new_lines']} ({h['reason']})")

        if r.get("mixed_hunks"):
            print("  Mixed hunks (src+test):")
            for h in r["mixed_hunks"]:
                print(f"    ⚠ {h['file']} lines {h['new_lines']} ({h['reason']})")

    print(f"\n{'=' * 70}")
    total_test = sum(r.get("summary", {}).get("test_hunks", 0) for r in results.values())
    total_src = sum(r.get("summary", {}).get("src_hunks", 0) for r in results.values())
    total_mixed = sum(r.get("summary", {}).get("mixed_hunks", 0) for r in results.values())
    parts = [f"{total_test} test", f"{total_src} src"]
    if total_mixed > 0:
        parts.append(f"{total_mixed} mixed")
    print(f"Total: {', '.join(parts)} hunks")

    return 0


def run_verify(args):
    """Run verify command."""
    old_dir = args.old_dir
    new_dir = args.new_dir

    old_patches = Path(old_dir) / "milestone_patches"
    new_patches = Path(new_dir) / "milestone_patches"

    if args.milestone:
        milestones = [args.milestone]
    else:
        milestones = sorted(
            set(f.stem for f in old_patches.glob("*.patch")) & set(f.stem for f in new_patches.glob("*.patch"))
        )

    results = {}
    for m in milestones:
        results[m] = verify_milestone(args.old_dir, args.new_dir, m)

    if args.json:
        print(json.dumps(results, indent=2))
        return

    print("Verify test/src separation (using ast-grep)")
    print("=" * 70)

    all_pass = True
    for m, r in results.items():
        success = r.get("overall_success", False)
        status = "✓ PASS" if success else "✗ FAIL"
        if not success:
            all_pass = False

        print(f"\n{m}: {status}")

        if "error" in r:
            print(f"  Error: {r['error']}")
            continue

        # File level
        fl = r["file_level"]
        print(f"  [File Level] Removed: {len(fl['removed_test_files'])} test files", end="")
        if fl["removed_src_files"]:
            print(f", ⚠ {len(fl['removed_src_files'])} src files")
            for f in fl["removed_src_files"]:
                print(f"      ✗ {f}")
        else:
            print()

        # Hunk level
        hl = r["hunk_level"]
        print(f"  [Hunk Level] Removed: {len(hl['removed_test_hunks'])} test hunks", end="")
        if hl["removed_src_hunks"]:
            print(f", ⚠ {len(hl['removed_src_hunks'])} src hunks")
            for h in hl["removed_src_hunks"][:3]:  # Show first 3
                print(f"      ✗ {h['file']} lines {h['new_lines']}")
            if len(hl["removed_src_hunks"]) > 3:
                print(f"      ... and {len(hl['removed_src_hunks']) - 3} more")
        else:
            print()

        if args.verbose and hl["removed_test_hunks"]:
            print(f"    Test hunks removed:")
            for h in hl["removed_test_hunks"][:5]:
                print(f"      ✓ {h['file']} lines {h['new_lines']} - {h['test_reason']}")

        # New patch
        np = r["new_patch"]
        print(f"  [New Patch] {len(np['src_changes'])} src changes", end="")

        test_violations = np["test_path_changes"] + np["test_module_changes"]
        if test_violations:
            print(f", ⚠ {len(test_violations)} test changes")
            for h in test_violations[:3]:
                print(f"      ✗ {h['file']} lines {h['lines']}")
            if len(test_violations) > 3:
                print(f"      ... and {len(test_violations) - 3} more")
        else:
            print()

    print(f"\n{'=' * 70}")
    print(f"Overall: {'ALL PASS ✓' if all_pass else 'SOME FAILURES ✗'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
