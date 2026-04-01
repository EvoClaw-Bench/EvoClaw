"""Verification behavior classifier for agent Bash commands.

Classifies bash commands into verification categories:
- BUILD: compilation/build commands
- TEST: test execution commands
- LINT: formatting/linting checks
- NONE: everything else

Works cross-language (Java, Go, Python, Rust, JS/TS) and cross-agent
(claude-code, codex, gemini-cli, openhands).

This is the canonical location for verification rules. The analysis script
(analysis/mstone_toolcall/verification_classifier.py) re-exports from here.
"""

import re
from enum import Enum
from typing import Tuple


class VerificationType(Enum):
    BUILD = "build"  # L1: compilation/build
    TEST = "test"  # L2: test execution
    LINT = "lint"  # formatting/linting checks
    NOT_VERIFICATION = "none"  # git ops, file ops, exploration, etc.


# ─── Patterns ───────────────────────────────────────────────────────
# Each pattern is (compiled_regex, VerificationType, rule_name)
# Order matters: first match wins.

_VERIFICATION_RULES: list[tuple[re.Pattern, VerificationType, str]] = []


def _add_rule(pattern: str, vtype: VerificationType, name: str, flags: int = re.IGNORECASE):
    _VERIFICATION_RULES.append((re.compile(pattern, flags), vtype, name))


# ── BUILD / COMPILE ─────────────────────────────────────────────────

# Java / Maven
_add_rule(r"(?:mvn|\.\/mvnw)\s+.*\b(?:compile|package)\b", VerificationType.BUILD, "maven-compile")
_add_rule(r"(?:mvn|\.\/mvnw)\s+.*\binstall\b", VerificationType.BUILD, "maven-install")
# Maven verify/validate (build lifecycle phases)
_add_rule(r"(?:mvn|\.\/mvnw)\s+.*\b(?:verify|validate)\b", VerificationType.BUILD, "maven-verify")

# Java / Gradle
_add_rule(r"(?:gradle|\.\/gradlew)\s+.*\b(?:compile|build|assemble)\b", VerificationType.BUILD, "gradle-build")

# Go
_add_rule(r"\bgo\s+build\b", VerificationType.BUILD, "go-build")
_add_rule(r"\bgo\s+install\b", VerificationType.BUILD, "go-install")
# go vet is a static analysis/verification tool
_add_rule(r"\bgo\s+vet\b", VerificationType.BUILD, "go-vet")
# go run as build verification:
#   - `go run main.go` or `go run .` — running the project
#   - `go run -exec "true"` — compile-only check (skips execution)
#   - `go run -tags=X main.go` — build with tags
#   - `go run github.com/onsi/ginkgo/...` — Ginkgo test runner (handled as TEST below)
# Exclude: `go run /tmp/debug.go` style ad-hoc scripts (too ambiguous)
_add_rule(
    r"\bgo\s+run\s+(?:-[a-z]+=\S+\s+)*(?:main\.go|\.(?:/\.\.\.)?\s*$|\.\s)", VerificationType.BUILD, "go-run-main"
)
_add_rule(r"\bgo\s+run\s+.*-exec\s+", VerificationType.BUILD, "go-run-exec")

# Rust
_add_rule(r"\bcargo\s+(?:build|check)\b", VerificationType.BUILD, "cargo-build")

# Python
_add_rule(r"\bpip\s+install\s+-e\s+\.", VerificationType.BUILD, "pip-editable-install")
_add_rule(r"\bpython[23]?\s+-m\s+(?:compileall|py_compile)\b", VerificationType.BUILD, "python-compile")
_add_rule(r"\bpython[23]?\s+setup\.py\s+(?:build|develop|install)\b", VerificationType.BUILD, "python-setup-build")
_add_rule(r"\bcython\b", VerificationType.BUILD, "cython-build")
# meson / cmake
_add_rule(r"\bmeson\s+compile\b", VerificationType.BUILD, "meson-compile")
_add_rule(r"\bcmake\s+--build\b", VerificationType.BUILD, "cmake-build")
# make must appear at beginning of (sub)command, not inside source code
_add_rule(r"(?:^|\n)\s*make\b(?!\s*file)(?!\s*dir)", VerificationType.BUILD, "make-build")

# JS/TS — `yarn build`, `yarn run build`, `yarn build:types`, `yarn build:compile`, etc.
_add_rule(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?build(?::\w+)?\b", VerificationType.BUILD, "npm-build")
_add_rule(r"\btsc\b(?!\s+--version)", VerificationType.BUILD, "tsc-build")
_add_rule(r"\b(?:webpack|vite|esbuild|rollup)\b", VerificationType.BUILD, "js-bundler-build")

# Rust: direct rustc compilation
_add_rule(r"\brustc\b", VerificationType.BUILD, "rustc-build")


# ── TEST EXECUTION ──────────────────────────────────────────────────

# Java / Maven
_add_rule(r"(?:mvn|\.\/mvnw)\s+.*\btest\b", VerificationType.TEST, "maven-test")
_add_rule(r"(?:mvn|\.\/mvnw)\s+.*-Dtest=", VerificationType.TEST, "maven-test-targeted")

# Java / Gradle
_add_rule(r"(?:gradle|\.\/gradlew)\s+.*\btest\b", VerificationType.TEST, "gradle-test")

# Go
_add_rule(r"\bgo\s+test\b", VerificationType.TEST, "go-test")
# Ginkgo test runner (invoked via `go run github.com/onsi/ginkgo/v2/ginkgo`)
_add_rule(r"\bginkgo\b", VerificationType.TEST, "ginkgo-test")

# Rust
_add_rule(r"\bcargo\s+test\b", VerificationType.TEST, "cargo-test")
# Running built test binary directly
_add_rule(r"\./target/(?:debug|release)/(?:deps/)?[a-zA-Z_]+-[0-9a-f]+", VerificationType.TEST, "cargo-test-binary")

# Python
_add_rule(r"\bpytest\b", VerificationType.TEST, "pytest")
_add_rule(r"\bpython[23]?\s+-m\s+pytest\b", VerificationType.TEST, "python-m-pytest")
_add_rule(r"\bpython[23]?\s+-m\s+unittest\b", VerificationType.TEST, "python-unittest")
_add_rule(r"\btox\b", VerificationType.TEST, "tox")
_add_rule(r"\bnox\b", VerificationType.TEST, "nox")

# JS/TS
_add_rule(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b", VerificationType.TEST, "npm-test")
_add_rule(r"\bjest\b", VerificationType.TEST, "jest")
_add_rule(r"\bvitest\b", VerificationType.TEST, "vitest")
_add_rule(r"\bmocha\b", VerificationType.TEST, "mocha")
_add_rule(r"\bcypress\s+run\b", VerificationType.TEST, "cypress")
_add_rule(r"\bplaywright\s+test\b", VerificationType.TEST, "playwright")

# Generic: running built binary for verification
# (e.g., ./target/debug/rg, ./target/debug/nu)
_add_rule(r"\./target/(?:debug|release)/[a-zA-Z_]+(?:\s|$)", VerificationType.TEST, "run-built-binary")


# ── LINT / FORMAT ───────────────────────────────────────────────────

# Java
_add_rule(r"\bspotless:(?:check|apply)\b", VerificationType.LINT, "spotless")
_add_rule(r"\bcheckstyle\b", VerificationType.LINT, "checkstyle")

# Go
_add_rule(r"\bgolangci-lint\b", VerificationType.LINT, "golangci-lint")
_add_rule(r"\bgofmt\s+-w\b", VerificationType.LINT, "gofmt-write")
_add_rule(r"\bgo\s+fmt\b", VerificationType.LINT, "go-fmt")
_add_rule(r"\bgoimports\b", VerificationType.LINT, "goimports")

# Rust
_add_rule(r"\bcargo\s+clippy\b", VerificationType.LINT, "cargo-clippy")
_add_rule(r"\bcargo\s+fmt\b", VerificationType.LINT, "cargo-fmt")

# Python
_add_rule(r"\bflake8\b", VerificationType.LINT, "flake8")
_add_rule(r"\bruff\s+(?:check|format)\b", VerificationType.LINT, "ruff")
_add_rule(r"\bblack\b(?!\s+--version)", VerificationType.LINT, "black")
_add_rule(r"\bmypy\b", VerificationType.LINT, "mypy")
_add_rule(r"\bpylint\b", VerificationType.LINT, "pylint")
_add_rule(r"\bisort\b", VerificationType.LINT, "isort")

# JS/TS
_add_rule(r"\beslint\b", VerificationType.LINT, "eslint")
_add_rule(r"\bprettier\b", VerificationType.LINT, "prettier")
# yarn lint, yarn lint:types, yarn lint:types:src, npm run lint, etc.
_add_rule(r"\b(?:npm|yarn|pnpm)\s+(?:run\s+)?lint(?::\w+)*\b", VerificationType.LINT, "npm-lint")


# ─── Preprocessing ─────────────────────────────────────────────────


def _strip_heredocs(cmd: str) -> str:
    """Remove heredoc content (<<'EOF'...EOF, <<EOF...EOF, etc.).

    Heredoc bodies contain arbitrary source code that can trigger false
    positives (e.g., Go's make() matching the make build rule).
    We keep the command before the heredoc marker but strip the body.
    """
    # Match <<'MARKER' or <<MARKER or <<"MARKER" through to the closing MARKER
    return re.sub(
        r"<<-?\s*['\"]?(\w+)['\"]?\s*\n.*?\n\s*\1\b",
        "<<HEREDOC_STRIPPED",
        cmd,
        flags=re.DOTALL,
    )


def _preprocess_command(cmd: str) -> list[str]:
    """Split compound commands and strip noise prefixes."""
    # Strip heredoc bodies first to avoid false positives from embedded code
    cmd = _strip_heredocs(cmd)

    # Strip common preambles (Codex style)
    cmd = re.sub(r"^set\s+-euo\s+pipefail\s*[\n;]", "", cmd.strip())
    cmd = re.sub(r"^cd\s+/testbed\s*(?:&&|\n)\s*", "", cmd.strip())

    # Split on && or ; or || (but not inside quotes)
    # Simple split — good enough for our purpose
    parts = re.split(r"\s*(?:&&|\|\||;)\s*", cmd)
    return [p.strip() for p in parts if p.strip()]


# ─── Classifier ─────────────────────────────────────────────────────


def classify_command(cmd: str) -> Tuple[str, str]:
    """Classify a bash command string into a verification type.

    For compound commands (&&, ||, ;), if ANY sub-command is verification,
    the whole command is classified as the highest-priority verification type
    (TEST > BUILD > LINT).

    Returns:
        Tuple of (vtype, matched_rule) where vtype is one of
        "build", "test", "lint", "none" and matched_rule is the
        rule name that matched (or "none").
    """
    subcmds = _preprocess_command(cmd)
    best = VerificationType.NOT_VERIFICATION
    best_rule = "none"
    priority = {
        VerificationType.NOT_VERIFICATION: 0,
        VerificationType.LINT: 1,
        VerificationType.BUILD: 2,
        VerificationType.TEST: 3,
    }

    for sub in subcmds:
        for pattern, vtype, rule_name in _VERIFICATION_RULES:
            if pattern.search(sub):
                if priority[vtype] > priority[best]:
                    best = vtype
                    best_rule = rule_name
                break

    return best.value, best_rule
