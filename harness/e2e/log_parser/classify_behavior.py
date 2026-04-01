"""Fine-grained behavior classifier for agent shell commands.

Classifies every shell command into one of 14 behavior categories:

  Exploration-leaning:
    file_read, search, git_read, env_probe, remote_read, task_poll

  Exploitation-leaning:
    build, test, format_lint, file_write, file_ops, git_write, dep_manage

  Ambiguous:
    script_exec

This classifier is independent from verification.py (which handles the
BUILD/TEST/LINT/NONE 4-category verification taxonomy).

Works cross-agent: claude-code (Bash), codex (exec_command/shell_command),
gemini-cli (run_shell_command), openhands (terminal).
"""

import re
from typing import List, Tuple

# ─── Priority ordering ─────────────────────────────────────────────
# For compound commands, highest-priority category wins.
# Higher number = higher priority (exploitation > exploration).
CATEGORY_PRIORITY = {
    "task_poll": 0,
    "env_probe": 1,
    "file_read": 2,
    "search": 3,
    "git_read": 4,
    "remote_read": 5,
    "script_exec": 6,
    "dep_manage": 7,
    "file_ops": 8,
    "file_write": 9,
    "git_write": 10,
    "format_lint": 11,
    "build": 12,
    "test": 13,
}

# ─── Rules ──────────────────────────────────────────────────────────
# Each rule: (compiled_regex, category, rule_name)
# Order matters within a category group, but categories are checked
# in the order listed below.  First match wins for each sub-command.

_BEHAVIOR_RULES: List[Tuple[re.Pattern, str, str]] = []


def _add(pattern: str, category: str, name: str, flags: int = re.IGNORECASE) -> None:
    _BEHAVIOR_RULES.append((re.compile(pattern, flags), category, name))


# ── task_poll ──────────────────────────────────────────────────────
_add(r"\bTASK_QUEUE\b", "task_poll", "task-queue-read")
_add(r"\bsleep\b.*\bcat\b.*QUEUE", "task_poll", "sleep-cat-queue")
_add(r"^\s*sleep\s+\d+\s*$", "task_poll", "bare-sleep")

# ── dep_manage (early): must match before curl/wget are seen ───────
_add(r"\bapt-get\b", "dep_manage", "apt-get")

# ── remote_read ────────────────────────────────────────────────────
_add(r"\bcurl\b", "remote_read", "curl")
_add(r"\bwget\b", "remote_read", "wget")

# ── env_probe (early): which/command -v must match before tool names ──
_add(r"\bwhich\b", "env_probe", "which")
_add(r"\bcommand\s+-v\b", "env_probe", "command-v")
_add(r"\btype\s+\w", "env_probe", "type-cmd")

# ── test ───────────────────────────────────────────────────────────
_add(r"\bcargo\s+test\b", "test", "cargo-test")
_add(r"\bgo\s+test\b", "test", "go-test")
_add(r"\bginkgo\b", "test", "ginkgo-test")
_add(r"\bpytest\b", "test", "pytest")
_add(r"\bpython[23]?\s+-m\s+pytest\b", "test", "python-m-pytest")
_add(r"\bpython[23]?\s+-m\s+unittest\b", "test", "python-unittest")
_add(r"\btox\b", "test", "tox")
_add(r"\bnox\b", "test", "nox")
_add(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b", "test", "npm-test")
_add(r"\bjest\b", "test", "jest")
_add(r"\bvitest\b", "test", "vitest")
_add(r"\bmocha\b", "test", "mocha")
_add(r"\bcypress\s+run\b", "test", "cypress")
_add(r"\bplaywright\s+test\b", "test", "playwright")
_add(r"(?:mvn|\.\/mvnw)\s+.*\btest\b", "test", "maven-test")
_add(r"(?:mvn|\.\/mvnw)\s+.*-Dtest=", "test", "maven-test-targeted")
_add(r"(?:gradle|\.\/gradlew)\s+.*\btest\b", "test", "gradle-test")
_add(r"\bmake\s+test\b", "test", "make-test")
_add(r"\bctest\b", "test", "ctest")
_add(r"\bsurefire\b", "test", "surefire")
# Running compiled test binaries (Rust)
_add(r"\./target/(?:debug|release)/(?:deps/)?[a-zA-Z_]+-[0-9a-f]+", "test", "cargo-test-binary")
_add(r"\./target/(?:debug|release)/[a-zA-Z_]+(?:\s|$)", "test", "run-built-binary")
# npx react-scripts test
_add(r"\bnpx\s+react-scripts\s+test\b", "test", "react-scripts-test")
# python -c inline test scripts with assert
_add(r"\bpython[23]?\s+-c\s+.*\bassert\b", "test", "python-c-assert")
# python -c with test markers (# FR, # M0)
_add(r'\bpython[23]?\s+-c\s+"[\s\n]*#\s*(FR|M\d+)', "test", "python-c-test-marker")
# python -c with Test in comment
_add(r'\bpython[23]?\s+-c\s+"[\s\n]*#.*\bTest\b', "test", "python-c-test-comment")

# ── build ──────────────────────────────────────────────────────────
_add(r"\bcargo\s+(?:build|check)\b", "build", "cargo-build")
_add(r"\bgo\s+build\b", "build", "go-build")
_add(r"\bgo\s+install\b", "build", "go-install")
_add(r"(?:mvn|\.\/mvnw)\s+.*\b(?:compile|package|install)\b", "build", "maven-compile")
_add(r"(?:mvn|\.\/mvnw)\s+.*\b(?:verify|validate)\b", "build", "maven-verify")
_add(r"(?:gradle|\.\/gradlew)\s+.*\b(?:compile|build|assemble)\b", "build", "gradle-build")
_add(r"\bmeson\s+(?:compile|setup)\b", "build", "meson-build")
_add(r"\bninja\b", "build", "ninja-build")
_add(r"\bcython\b", "build", "cython-build")
_add(r"\brustc\b(?!\s+--version)", "build", "rustc-build")
_add(r"\bcmake\s+--build\b", "build", "cmake-build")
_add(r"\bcmake\b", "build", "cmake")
_add(r"(?:^|\n)\s*make\b(?!\s*file)(?!\s*dir)(?!\s+test)", "build", "make-build")
_add(r"\bgcc\b", "build", "gcc-build")
_add(r"\bg\+\+\b", "build", "gxx-build")
_add(r"\bjavac\b", "build", "javac-build")
_add(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?build(?::\w+)?\b", "build", "npm-build")
_add(r"\btsc\b(?!\s+--version)", "build", "tsc-build")
_add(r"\b(?:webpack|vite|esbuild|rollup)\b", "build", "js-bundler-build")
_add(r"\bpip\s+install\s+-e\s+\.", "build", "pip-editable-install")
_add(r"\bpython[23]?\s+-m\s+(?:compileall|py_compile)\b", "build", "python-compile")
_add(r"\bpython[23]?\s+setup\.py\s+(?:build|develop|install)\b", "build", "python-setup-build")
_add(r"\bgo\s+generate\b", "build", "go-generate")
_add(r"\bnpx\s+react-scripts\s+build\b", "build", "react-scripts-build")

# ── format_lint ────────────────────────────────────────────────────
_add(r"\bgofmt\b", "format_lint", "gofmt")
_add(r"\bgo\s+vet\b", "format_lint", "go-vet")
_add(r"\bgo\s+fmt\b", "format_lint", "go-fmt")
_add(r"\bgoimports\b", "format_lint", "goimports")
_add(r"\bcargo\s+clippy\b", "format_lint", "cargo-clippy")
_add(r"\bcargo\s+fmt\b", "format_lint", "cargo-fmt")
_add(r"\bspotless:(?:check|apply)\b", "format_lint", "spotless")
_add(r"\bcheckstyle\b", "format_lint", "checkstyle")
_add(r"\bgolangci-lint\b", "format_lint", "golangci-lint")
_add(r"\beslint\b", "format_lint", "eslint")
_add(r"\bprettier\b", "format_lint", "prettier")
_add(r"\bblack\b(?!\s+--version)", "format_lint", "black")
_add(r"\bruff\s+(?:check|format)\b", "format_lint", "ruff")
_add(r"\bflake8\b", "format_lint", "flake8")
_add(r"\bmypy\b", "format_lint", "mypy")
_add(r"\bpylint\b", "format_lint", "pylint")
_add(r"\bisort\b", "format_lint", "isort")
_add(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?lint(?::\w+)*\b", "format_lint", "npm-lint")

# ── dep_manage ─────────────────────────────────────────────────────
_add(r"\bpip\s+install\b(?!\s+-e)", "dep_manage", "pip-install")
_add(r"\bgo\s+(?:get|mod\s+tidy|mod\s+download)\b", "dep_manage", "go-mod")
_add(r"\bnpm\s+install\b", "dep_manage", "npm-install")
_add(r"\byarn\s+(?:add|install)\b", "dep_manage", "yarn-install")
_add(r"\bpnpm\s+(?:add|install)\b", "dep_manage", "pnpm-install")
_add(r"\brustup\b", "dep_manage", "rustup")
_add(r"\bcargo\s+add\b", "dep_manage", "cargo-add")
_add(r"\bconda\s+install\b", "dep_manage", "conda-install")
_add(r"\buv\s+(?:pip|add)\b", "dep_manage", "uv-install")

# ── git_write ──────────────────────────────────────────────────────
_add(r"\bgit\s+add\b", "git_write", "git-add")
_add(r"\bgit\s+commit\b", "git_write", "git-commit")
_add(r"\bgit\s+tag\s+(?!-l\b|--list\b)(?!-v\b)", "git_write", "git-tag-create")
_add(r"\bgit\s+tag\s+-d\b", "git_write", "git-tag-delete")
_add(r"\bgit\s+restore\b", "git_write", "git-restore")
_add(r"\bgit\s+checkout\s+--\b", "git_write", "git-checkout-file")
_add(r"\bgit\s+cherry-pick\b", "git_write", "git-cherry-pick")
_add(r"\bgit\s+apply\b", "git_write", "git-apply")
_add(r"\bgit\s+clone\b", "git_write", "git-clone")
_add(r"\bgit\s+fetch\b", "git_write", "git-fetch")
_add(r"\bgit\s+push\b", "git_write", "git-push")
_add(r"\bgit\s+merge\b", "git_write", "git-merge")
_add(r"\bgit\s+rebase\b", "git_write", "git-rebase")
_add(r"\bgit\s+reset\b", "git_write", "git-reset")
_add(r"\bgit\s+stash\b", "git_write", "git-stash")
_add(r"\bgit\s+rm\b", "git_write", "git-rm")
_add(r"\bgit\s+mv\b", "git_write", "git-mv")
_add(r"\bgit\s+init\b", "git_write", "git-init")
_add(r"\bgit\s+am\b", "git_write", "git-am")
_add(r"\bgit\s+config\b", "git_write", "git-config")
_add(r"\bgit\s+remote\b", "git_write", "git-remote")
_add(r"\bgit\s+branch\b", "git_write", "git-branch")

# ── file_write ─────────────────────────────────────────────────────
_add(r"\bsed\s+-i\b", "file_write", "sed-inplace")
_add(r"\bperl\s+-[ip]", "file_write", "perl-inplace")
_add(r"(?<![&\d])>\s*(?!&)\S", "file_write", "redirect-write")
_add(r">>\s*\S", "file_write", "redirect-append")
_add(r"\bcat\s*<<", "file_write", "heredoc-write")
_add(r"\btee\b", "file_write", "tee-write")
_add(r"\bpython[23]?\s+.*write_text\b", "file_write", "python-write-text")
_add(r'\bpython[23]?\s+-c\s+.*open\(.*,\s*["\']w', "file_write", "python-open-w")
_add(r"\bnode\s+-e\s+.*writeFileSync", "file_write", "node-writeFileSync")
_add(r"\bpatch\b", "file_write", "patch-apply")

# ── file_ops ───────────────────────────────────────────────────────
_add(r"\bmkdir\b", "file_ops", "mkdir")
_add(r"(?:^|&&|\|\||;)\s*cp\s", "file_ops", "cp")
_add(r"\bdo\s+cp\s", "file_ops", "cp-in-loop")
_add(r"\bmv\s", "file_ops", "mv")
_add(r"(?:^|&&|\|\||;)\s*rm\s", "file_ops", "rm")
_add(r"\bdo\s+rm\s", "file_ops", "rm-in-loop")
_add(r"\btouch\b", "file_ops", "touch")
_add(r"\bchmod\b", "file_ops", "chmod")
_add(r"\bchown\b", "file_ops", "chown")
_add(r"\bln\s", "file_ops", "ln")
_add(r"\bunzip\b", "file_ops", "unzip")
_add(r"\btar\b", "file_ops", "tar")

# ── search ─────────────────────────────────────────────────────────
_add(r"\brg\b", "search", "rg")
_add(r"\bgrep\b", "search", "grep")
_add(r"\bfind\s", "search", "find")
_add(r"\bls\b", "search", "ls")
_add(r"\btree\b", "search", "tree")
_add(r"\bgit\s+grep\b", "search", "git-grep")
_add(r"\bgit\s+ls-tree\b", "search", "git-ls-tree")
_add(r"\bwc\s", "search", "wc")
_add(r"\bdu\s", "search", "du")
_add(r"\bfile\s", "search", "file-cmd")
_add(r"\blocate\b", "search", "locate")

# ── git_read ───────────────────────────────────────────────────────
_add(r"\bgit\s+status\b", "git_read", "git-status")
_add(r"\bgit\s+diff\b", "git_read", "git-diff")
_add(r"\bgit\s+log\b", "git_read", "git-log")
_add(r"\bgit\s+show\b", "git_read", "git-show")
_add(r"\bgit\s+tag\s+(-l|--list)\b", "git_read", "git-tag-list")
_add(r"\bgit\s+tag\s*$", "git_read", "git-tag-bare")
_add(r"\bgit\s+rev-parse\b", "git_read", "git-rev-parse")
_add(r"\bgit\s+describe\b", "git_read", "git-describe")
_add(r"\bgit\s+blame\b", "git_read", "git-blame")
_add(r"\bgit\s+rev-list\b", "git_read", "git-rev-list")
_add(r"\bgit\s+ls-files\b", "git_read", "git-ls-files")
_add(r"\bgit\s+shortlog\b", "git_read", "git-shortlog")
_add(r"\bgit\s+show-ref\b", "git_read", "git-show-ref")

# ── file_read ──────────────────────────────────────────────────────
_add(r"\bsed\s+-n\b", "file_read", "sed-n")
_add(r"\bcat\s", "file_read", "cat")
_add(r"\bhead\b", "file_read", "head")
_add(r"\btail\b", "file_read", "tail")
_add(r"\bnl\b", "file_read", "nl")
_add(r"\bawk\b", "file_read", "awk")
_add(r"\bjavap\b", "file_read", "javap")
_add(r"\bjar\s+tf\b", "file_read", "jar-tf")
_add(r"\bstrings\b", "file_read", "strings")
_add(r"\bod\b", "file_read", "od")
_add(r"\bxxd\b", "file_read", "xxd")
_add(r"\bless\b", "file_read", "less")
_add(r"\bmore\b", "file_read", "more")
_add(r"\bbat\b", "file_read", "bat")
_add(r"\bpython[23]?\s+.*inspect\b", "file_read", "python-inspect")

# ── env_probe ──────────────────────────────────────────────────────
_add(r"\bpwd\b", "env_probe", "pwd")
_add(r"\bgo\s+version\b", "env_probe", "go-version")
_add(r"\bcargo\s+--version\b", "env_probe", "cargo-version")
_add(r"\brustc\s+--version\b", "env_probe", "rustc-version")
_add(r"\bjava\s+(?:-|--)version\b", "env_probe", "java-version")
_add(r"\bpython[23]?\s+(?:--version|-V)\b", "env_probe", "python-version")
_add(r"\bnode\s+--version\b", "env_probe", "node-version")
_add(r"\bnpm\s+--version\b", "env_probe", "npm-version")
_add(r"\bgo\s+env\b", "env_probe", "go-env")
_add(r"\bgo\s+list\b", "env_probe", "go-list")
_add(r"\bgo\s+doc\b", "env_probe", "go-doc")
_add(r"\bcargo\s+info\b", "env_probe", "cargo-info")
_add(r"\bcargo\s+search\b", "env_probe", "cargo-search")
_add(r"\buname\b", "env_probe", "uname")
_add(r"\bprintenv\b", "env_probe", "printenv")
_add(r"\becho\s+\$", "env_probe", "echo-var")
_add(r"\bdf\b", "env_probe", "df")
_add(r"\bfree\b", "env_probe", "free")
_add(r"\bhostname\b", "env_probe", "hostname")
_add(r"\bwhoami\b", "env_probe", "whoami")
_add(r"\bid\b", "env_probe", "id")
_add(r"\bpip\s+(?:list|show|index)\b", "env_probe", "pip-info")
_add(r"\bpip\s+--version\b", "env_probe", "pip-version")
# python -c for version/import checks
_add(r'\bpython[23]?\s+-c\s+"import\s+\w+;\s*print\(.*__version__', "env_probe", "python-c-version")
_add(r'\bpython[23]?\s+-c\s+"import\s+\w+.*;\s*print\(.*__file__', "env_probe", "python-c-file")
_add(r'\bpython[23]?\s+-c\s+"from\s+.*import\s+.*;\s*print\([\'"]OK', "env_probe", "python-c-import-ok")
_add(r'\bpython[23]?\s+-c\s+"import\s+\w+;\s*print\(', "env_probe", "python-c-import-print")
_add(r'\bpython[23]?\s+-c\s+"from\s+\w+', "env_probe", "python-c-from-import")
_add(r'\bpython[23]?\s+-c\s+["\']', "env_probe", "python-c-general")
_add(r"^\s*cd\s", "env_probe", "cd")
_add(r"^\s*export\s", "env_probe", "export")

# ── script_exec (must be last — catch-all for running scripts) ────
_add(r"\bpython[23]?\s+\S+\.py\b", "script_exec", "python-script")
_add(r"\bbash\s+\S+\.sh\b", "script_exec", "bash-script")
_add(r"\bsh\s+\S+\.sh\b", "script_exec", "sh-script")
_add(r"\bzsh\s+\S+\.sh\b", "script_exec", "zsh-script")
_add(r"\bgo\s+run\s+\S+\.go\b", "script_exec", "go-run-file")
_add(r"\bnode\s+\S+\.(?:js|ts|mjs)\b", "script_exec", "node-script")
_add(r"\./[a-zA-Z_][a-zA-Z0-9_.-]*(?:\s|$)", "script_exec", "exec-script")
_add(r"\bcargo\s+run\b", "script_exec", "cargo-run")


# ─── Preprocessing ─────────────────────────────────────────────────


def _strip_heredocs(cmd: str) -> str:
    """Remove heredoc content to avoid false positives from embedded code."""
    return re.sub(
        r"<<-?\s*['\"]?(\w+)['\"]?\s*\n.*?\n\s*\1\b",
        "<<HEREDOC_STRIPPED",
        cmd,
        flags=re.DOTALL,
    )


def _preprocess_command(cmd: str) -> List[str]:
    """Split compound commands and strip noise prefixes."""
    cmd = _strip_heredocs(cmd)
    # Strip common preambles (Codex style)
    cmd = re.sub(r"^set\s+-euo\s+pipefail\s*[\n;]", "", cmd.strip())
    cmd = re.sub(r"^cd\s+/testbed\s*(?:&&|\n)\s*", "", cmd.strip())
    # Split on && or ; or || (simple split)
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", cmd)
    return [p.strip() for p in parts if p.strip()]


# ─── Classifier ────────────────────────────────────────────────────


def _classify_single(cmd: str) -> Tuple[str, str]:
    """Classify a single (non-compound) command. Returns (category, rule_name)."""
    for pattern, category, rule_name in _BEHAVIOR_RULES:
        if pattern.search(cmd):
            return category, rule_name
    return "env_probe", "unclassified-fallback"


def classify_shell_command(cmd: str) -> Tuple[str, str]:
    """Classify a shell command string into a behavior_detail category.

    For compound commands (&&, ||, ;), the highest-priority category wins
    (exploitation > exploration).

    Args:
        cmd: Raw shell command string

    Returns:
        Tuple of (behavior_detail, matched_rule) where behavior_detail
        is one of the 14 categories and matched_rule is the rule name.
    """
    subcmds = _preprocess_command(cmd)
    if not subcmds:
        return "env_probe", "empty-command"

    best_category = None
    best_rule = None
    best_priority = -1

    for sub in subcmds:
        category, rule = _classify_single(sub)
        priority = CATEGORY_PRIORITY.get(category, 0)
        if priority > best_priority:
            best_priority = priority
            best_category = category
            best_rule = rule

    return best_category, best_rule
