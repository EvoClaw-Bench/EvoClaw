# Advanced Usage

Deeper dives — single-repo / single-milestone debugging, result collection,
config tuning, output layout, and the `flock` ownership model.

For day-to-day "launch a trial and monitor it" flows, see
[`running-trials.md`](./running-trials.md).

---

## Single-Repo `run_e2e`

`run_all.py` is just a thin launcher around `run_e2e`. For one-off debugging
you can call `run_e2e` directly. Same `flock` + `--force` semantics apply.

```bash
# Fresh start (or wipe-and-restart with --force)
python -m harness.e2e.run_e2e \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --image navidrome_navidrome_v0.57.0_v0.58.0/base:latest \
  --srs-root /path/to/EvoClaw-data/navidrome_.../srs \
  --workspace-root /path/to/EvoClaw-data/navidrome_... \
  --agent claude-code --model claude-sonnet-4-6 \
  --timeout 18000 --trial-name my_experiment_001 \
  --force

# Resume an existing trial directly (container must exist)
python -m harness.e2e.run_e2e \
  --resume-trial /path/to/EvoClaw-data/.../e2e_trial/my_experiment_001
```

### CLI Reference

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
| `--trial-name` | Trial name. Ending in `_NNN` is used as-is; bare names auto-increment. |
| `--force` | Wipe trial dir + remove container + take over the per-trial lock (SIGTERM stale owner, then SIGKILL after 10s) |
| `--remove-container` | Remove container after trial completes (default: keep running) |
| `--skip-testbed-copy` | Skip copying `/testbed` from container after trial |
| `--resume-trial PATH` | Resume from existing trial directory (container must exist) |
| `--no-resume-session` | In resume mode, start a new agent session instead of resuming the previous one |

To clean up all containers from a specific trial:
```bash
docker rm -f $(docker ps -q --filter "name=my_experiment_001")
```

---

## Run a Single Milestone

For testing or debugging a single milestone in isolation:

```bash
python -m harness.e2e.run_milestone \
  --repo-name navidrome_navidrome_v0.57.0_v0.58.0 \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --srs-path /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0/srs/milestone_001/SRS.md \
  --agent claude-code \
  --model claude-sonnet-4-6
```

---

## Collect Results

### Single repo

```bash
python -m harness.e2e.collect_results \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --trials my_experiment_001 \
  --trial-type e2e
```

### Re-evaluate a snapshot

Re-run evaluation against a previously captured `source_snapshot.tar`:

```bash
python -m harness.e2e.evaluator \
  --workspace-root /path/to/EvoClaw-data/navidrome_navidrome_v0.57.0_v0.58.0 \
  --milestone-id milestone_001 \
  --patch-file /path/to/trial/evaluation/milestone_001/source_snapshot.tar \
  --baseline-classification /path/to/test_results/milestone_001/milestone_001_classification.json \
  --output /path/to/output/evaluation_result.json
```

---

## E2E Config (`e2e_config.yaml`)

The `e2e_config.yaml` (at `harness/e2e/e2e_config.yaml`) controls evaluation
behavior. The default is the EvoClaw Benchmark configuration:

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

See the full [`e2e_config.yaml`](../harness/e2e/e2e_config.yaml) for all
available options.

---

## Claude Code + Custom Endpoints (`api_router`)

> ⚠️ **Warning — `api_router` is not recommended for long trials on
> non-Anthropic / reasoning models.**
>
> The router path has known stability issues for models like
> Moonshot kimi that reach 200K+ context in long benchmark runs:
>
> - **No real compaction**: Claude Code's built-in 80%-threshold
>   auto-compact only fires for models it recognizes (claude-\*). For
>   third-party models it sends `clear_tool_uses` / `clear_thinking`
>   pruning edits instead — which delete blocks but don't summarize.
>   Context grows until it exceeds the upstream's body limit → HTTP 413.
> - **Fidelity loss** through Anthropic ↔ OpenAI translation: prompt
>   cache breakdown (ephemeral_5m / ephemeral_1h) flattens to OpenAI's
>   single `cached_tokens`; `compact_20260112` edits are silently
>   dropped (router can't invoke an LLM for summarization).
> - **Reasoning-model incompatibility**: some upstreams (notably
>   OpenRouter → Moonshot) reject `thinking` / `reasoning_effort` /
>   `context_management` body fields with HTTP 400. The router has
>   adaptive retry for these, but it's whack-a-mole territory.
>
> **Prefer OpenHands for non-Anthropic models.** OpenHands drives its own
> LLM client via LiteLLM's native OpenAI `/v1/chat/completions` path —
> no translation, no missing compaction, and the framework has built-in
> `LLMSummarizingCondenser` that actually calls the LLM to summarize
> when context grows. See `trial_configs/openhands_kimi-k2.6.yaml` and
> `openhands_glm-5.yaml` for reference configs.
>
> The rest of this section documents what `api_router` does for the
> cases where it's still needed (ad-hoc claude-code experiments against
> OpenAI-only endpoints), not as a recommended path.

Claude Code only speaks the Anthropic Messages API (`POST /v1/messages`).
When `UNIFIED_BASE_URL` points at a third-party endpoint, whether you need
`api_router: true` depends on two orthogonal things:

1. **Does the endpoint accept Anthropic request shape at all?** (URL and
   top-level schema.)
2. **Does the endpoint accept the `context_management` body field Claude
   Code sends on long sessions?**

Both must be yes to leave the router off.

> Applies to `agent: claude-code` only. Codex / Gemini / OpenHands each
> drive their own native LLM client and ignore this flag.

### Decision

| Upstream / model | Anthropic shape? | `context_management`? | `api_router` |
|---|---|---|---|
| `api.anthropic.com` + claude-* | yes | yes (native) | **off** |
| `llm-proxy.eval.all-hands.dev` + claude-* (Anthropic-backed) | yes | yes (passthrough to Anthropic) | **off** |
| `llm-proxy.eval.all-hands.dev` + OpenRouter-backed model (e.g. `openrouter/moonshotai/kimi-k2.6`) | yes | **no — LiteLLM returns HTTP 400** on `UnsupportedParamsError` | **on** |
| Z.AI Anthropic endpoint (`open.bigmodel.cn/api/anthropic`) + glm-* | yes | provider-dependent; typically no | **on** (unless verified otherwise) |
| Pure OpenAI-only endpoints / self-hosted vLLM/SGLang/TGI | no | n/a | **on** |
| OpenRouter native `/api/v1/chat/completions` | no | n/a | **on** |

Concrete trial configs: `claude-code_opus-4.7-1m.yaml` runs router-off
(Anthropic-native and compatible end-to-end); `claude-code_kimi-k2.6.yaml`
and `claude-code_glm-5_router.yaml` run router-on.

### Why `context_management` is the subtle trap

Claude Code auto-sends a top-level `context_management` field in the
request body once context grows large (tool-history trimming, thinking-
block eviction). The Anthropic spec for it is:

```json
{
  "context_management": {
    "edits": [
      {"type": "clear_tool_uses_20250919",
       "trigger": {"type": "input_tokens", "value": 120000},
       "keep": {"type": "tool_uses", "value": 5}}
    ]
  }
}
```

LiteLLM forwarders (like all-hands) pass this through to the backing
provider. If the provider is Anthropic-native, they honor it and echo
`context_management.applied_edits` back in the response. If the provider
is anything else — OpenRouter, direct Moonshot, fireworks, etc. — LiteLLM
raises `UnsupportedParamsError` with **HTTP 400**, not a silent drop.
This means a trial that looks fine for hundreds of short turns suddenly
starts 400-ing the moment compaction triggers.

The router's
[`context_editor.py`](../vendor/claude-code-router-py/context_editor.py)
solves this by popping the field off the body **before** forwarding to
upstream and executing two of the three edit strategies locally (pure
message manipulation, no extra LLM call):

- `clear_thinking_20251015` — drop old thinking blocks, keep recent N
- `clear_tool_uses_20250919` — drop old tool_use/tool_result pairs once
  the input-tokens trigger fires
- `compact_20260112` — **skipped** (summarizing requires a real LLM call,
  not yet implemented). No compaction happens for this edit type, but the
  request no longer fails.

Router also auto-injects a default `clear_tool_uses` if
`context_management` is present without it, as a safety net against
context overflow.

### Verifying an endpoint

Two probes — a cheap one and a definitive one.

```bash
# 1. Basic shape: does /v1/messages work?
curl -sS -X POST "$UNIFIED_BASE_URL/v1/messages" \
  -H "x-api-key: $UNIFIED_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"<your-model>","max_tokens":16,
       "messages":[{"role":"user","content":"hi"}]}'
```

200 + Anthropic shape → shape is fine. 404 / 400 "unsupported endpoint" →
router **on**.

```bash
# 2. Definitive: does context_management survive?
curl -sS -X POST "$UNIFIED_BASE_URL/v1/messages" \
  -H "x-api-key: $UNIFIED_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"<your-model>","max_tokens":16,
       "context_management":{"edits":[
         {"type":"clear_tool_uses_20250919",
          "trigger":{"type":"input_tokens","value":1000},
          "keep":{"type":"tool_uses","value":2}}]},
       "messages":[{"role":"user","content":"hi"}]}'
```

- 200 + `context_management: {applied_edits: [...]}` in response → native,
  router **off**.
- 400 `UnsupportedParamsError` → router **on**. (LiteLLM's error text
  includes the hint `"To drop these, set litellm.drop_params=True"` —
  we can't change the proxy config, so router is the answer.)

### What the router does end-to-end

`api_router: true` deploys the vendored
[`claude-code-router-py`](../vendor/claude-code-router-py) as a daemon on
`127.0.0.1:8181` inside the agent container. `ContainerSetup` then
rewrites `ANTHROPIC_BASE_URL=http://localhost:8181` and stashes the real
upstream in `API_PROXY_UPSTREAM`. The daemon:

1. Pops `context_management` and applies edits locally
   ([context_editor.py](../vendor/claude-code-router-py/context_editor.py))
2. Converts Anthropic Messages → OpenAI `/v1/chat/completions`
3. Forwards to `API_PROXY_UPSTREAM`
4. Translates the response back

Model name passes through unchanged
(`"Router": {"default": "upstream,/model"}`). Logs at
`/tmp/api_router.log` inside the container — check there first on wedge:

```bash
docker exec "${REPO}-${TRIAL}" tail -50 /tmp/api_router.log
```

### Tradeoffs (why prefer off when truly unnecessary)

- **Fidelity loss**: Anthropic-specific fields collapse through the OpenAI
  round-trip. Prompt cache breakdown (`ephemeral_5m` / `ephemeral_1h`
  components of `cache_creation_input_tokens`) flattens to OpenAI's single
  `cached_tokens`; thinking-block `signature` may drop. Cost-calc
  precision and trace quality degrade.
- **Compaction semantics change**: `compact_20260112` is silently dropped
  (no summarization). For very long sessions you may end up with less
  aggressive context management than native Anthropic would do.
- **Extra hop + new failure surface**: every request goes through
  localhost. Router startup / config errors are silent until the first
  agent turn.

Upside: non-Anthropic-compatible upstreams (including LiteLLM routes that
hand off to OpenRouter/moonshot/fireworks/etc.) are only reachable via the
router.

### Model string hint

`model:` in `trial_config.yaml` is forwarded verbatim as `--model` to
`claude` and then to the upstream. The string must match what the upstream
registers. Example gotcha: the all-hands LiteLLM proxy requires
`openrouter/moonshotai/kimi-k2.6` — the bare `kimi-k2.6` yields
`"No healthy deployments"`. Use `curl $BASE/v1/models | jq '.data[].id'`
to list acceptable IDs. `harness/e2e/pricing.py` auto-strips
`litellm_proxy/`, `openrouter/`, `openrouter/moonshotai/`, `gemini/`
prefixes, so prefixed IDs still resolve to the canonical price entry.

---

## Trial Output Structure

Every trial produces:

```
{data_root}/{repo_name}/e2e_trial/{trial_name}/
├── trial_metadata.json        # Run configuration
├── orchestrator.log           # Detailed orchestration log
├── agent_stats.json           # Agent statistics (cost, tokens, turns)
├── log/
│   ├── agent_prompt.txt       # Initial prompt sent to agent
│   ├── agent_stdout.txt       # Agent stdout
│   └── agent_stderr.txt       # Agent stderr
└── evaluation/
    ├── summary.json           # Aggregated results across milestones
    └── {milestone_id}/
        ├── source_snapshot.tar
        └── evaluation_result.json
```

Locks live alongside trial dirs but in a sibling hidden subdir:

```
{data_root}/{repo_name}/e2e_trial/.locks/
├── {trial_name}.lock          # fcntl.flock target (size 0)
└── {trial_name}.info          # JSON sidecar with owner pid/started_at/cmdline/host
```

---

## Lock Internals

Each `(workspace_root, trial_name)` pair has an exclusive `fcntl.flock` on
`<workspace>/e2e_trial/.locks/<trial_name>.lock`. Implemented in
[`harness/e2e/trial_lock.py`](../harness/e2e/trial_lock.py).

Properties:

- **Race-free acquire**: `fcntl.flock(fd, LOCK_EX | LOCK_NB)` is atomic.
- **No stale-cleanup needed**: kernel releases the flock the instant the
  holder's fd closes — process exit (any reason: clean, SIGKILL, OOM, segfault)
  releases automatically. No `atexit` hook required.
- **Lock file outside trial dir**: `--force` rmtrees `trial_root` without
  racing the lock.
- **Diagnostic sidecar**: `<trial_name>.info` is a JSON dump of
  `pid / started_at / cmdline / host`, written atomically (tempfile + rename)
  after acquire. Used to format the "owned by …" refusal message.

`--force` flow:

1. Try `LOCK_EX | LOCK_NB` → `BlockingIOError`.
2. Read sidecar → extract `pid`.
3. `SIGTERM`, poll up to 10s for graceful exit (lets the watcher reap docker
   subprocesses for in-flight evaluations).
4. If still alive: `SIGKILL`.
5. Retry `LOCK_NB` (kernel may need a tick after the dead process is reaped).
6. Write fresh sidecar with our own info.

Without `--force`, a busy lock prints the sidecar contents to stderr and
exits 1. No silent overwrite, no race.

---

## Resume Constraint

Container mounts (verified via `docker inspect`):

```
~/.claude/.credentials.json  → /tmp/host-claude-credentials/.credentials.json
~/.local/share/claude        → /tmp/host-claude-share
<trial>/e2e_workspace        → /e2e_workspace
```

`/testbed` (the repo work tree) lives **only** in the container's writable
layer. Consequence: `docker rm` on a trial container destroys all
in-container git history, uncommitted code, and the agent's session cache.

What survives on the host across `docker rm`:

- `evaluation/{milestone_id}/source_snapshot.tar` — per-submitted milestone
- `orchestrator.log`, `agent_stats.json`, `agent_*.txt`
- DAG state, trial metadata

Implications:

- `--resume-trial` exits with a container-not-found error if the container is
  gone (`harness/e2e/resume.py:280-282`); use `--force` (full restart from the
  initial commit) instead.
- For a soft "continue from where we were", keep the container around — even
  stopped is fine (`verify_container_for_resume` auto-starts stopped
  containers).
