# Failure Analysis: Dubbo `JarScanner` Cascade (GLM-5.1 / Claude Code)

**Trial:** `apache_dubbo_dubbo-3.3.3_dubbo-3.3.6 / e2e_trial / claude-code_glm-5.1_001`
**Score:** **0 / 12** milestones resolved (8 failed, 1 runner error, 4 unreached)

---

## 1. TL;DR

- One misapplied try-with-resources on a `JarURLConnection` in `dubbo-native/JarScanner.java` (1 line, introduced during **M016.1**) made `dubbo-native` fail `mvn compile`.
- Under the default fail-fast Maven runner, the reactor SKIPPED ~90 downstream modules, dropping `pass_to_pass` from ~6920 to ~1140 for every milestone evaluated after the bug landed.
- The agent never ran `mvn compile` or `javac` *at any point in the trial* — there are **0** invocations of any build tool across ~5.5k conversation lines. It tagged-and-forgot.
- Counterfactual: had the bug not been introduced, the trial's average post-bug pass rate should have matched the pre-bug average (~68% P2P on M004/M006), and the 5 poisoned milestones would have returned ~19k additional passing tests instead of ~0.
- Top guardrail recommendation: **pre-submit `mvn -q -pl <touched-modules> -am compile` check inside the evaluator before recording a tag** — cheap, doesn't leak test info, faithfully preserves the "agent capability" signal.

---

## 2. Root Cause

### 2.1 The diff (introduced in M016.1, tag `99e67e2a...`)

`dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`, lines 79–84:

```diff
@@ -77,8 +77,10 @@
                 if ("file".equals(protocol)) {
                     scanFile(resource.getPath());
                 } else if ("jar".equals(protocol)) {
-                    JarFile jar = ((JarURLConnection) resource.openConnection()).getJarFile();
-                    scanJar(jar);
+                    try (JarURLConnection jarConnection = (JarURLConnection) resource.openConnection()) {
+                        JarFile jar = jarConnection.getJarFile();
+                        scanJar(jar);
+                    }
                 }
             }
         } catch (Throwable ex) {
```

`java.net.URLConnection` (and its subclass `JarURLConnection`) does **not** implement `AutoCloseable` — this is a long-standing JDK gotcha. Compiler output:

```
[ERROR] JarScanner.java:[80,42] error: incompatible types: try-with-resources
    not applicable to variable type (JarURLConnection cannot be converted to AutoCloseable)
```

### 2.2 What the SRS actually asked

From `e2e_workspace/srs/M016.1_SRS.md` FR10:

> **Problem**: `JarScanner` does not properly close InputStream resources, causing potential resource leaks.
> **Requirements**: Wrap InputStream resources in try-with-resources blocks in `JarScanner`.

The SRS text is itself misleading: `JarScanner.java` contains **zero `InputStream` usages** (only `URL`, `JarURLConnection`, `JarFile`, `JarEntry`). The ground-truth fix in upstream dubbo is a no-op here or something far narrower — but the SRS steered the agent to "find something to close." The agent free-interpreted "the closest closeable-looking thing" and wrapped the `JarURLConnection` cast, which breaks compilation.

### 2.3 Agent transcript (verbatim, from `log/claude-conversation-2026-04-10-74bcc5c4.md:4170–4240`)

The agent performed **3 back-to-back `Edit` calls** on `JarScanner.java` — each one rewriting the same block, the first two failing due to whitespace / tab mismatch — then moved on to FR11 without reading back the modified file or running any validation:

```
Claude: **FR10: Resource Leak Prevention - JarScanner**
  Edit → old: "JarFile jar = ((JarURLConnection) resource.openConnection()).getJarFile(); scanJar(jar);"
         new: "try (JarURLConnection jarConnection = ...) { JarFile jar = jarConnection.getJarFile(); scanJar(jar); }"
  Edit → (retry with different indentation)
  Edit → (third retry, finally succeeds)
Claude: **FR11: ConsistentHashSelector Concurrency Optimization**
  (no compile check, no re-read)
```

The commit message then cheerfully claims `- FR10: Fix JarScanner resource leak with try-with-resources`.

### 2.4 First milestone touched

Snapshot hashing of `JarScanner.java` across all 9 evaluated milestones (ordered by eval timestamp):

| Milestone | md5(10) | Status |
|---|---|---|
| M004    | `4368ce72fa` | pristine |
| M003.1  | `4368ce72fa` | pristine |
| M006    | `4368ce72fa` | pristine |
| M011    | `4368ce72fa` | pristine |
| **M016.1**  | **`2afa6d4b6c`** | **bug introduced** |
| M019    | `2afa6d4b6c` | bug propagated |
| M017    | `2afa6d4b6c` | bug propagated (same tag as M016.1) |
| M003.2  | `2afa6d4b6c` | bug propagated |
| M001.1  | `2afa6d4b6c` | bug propagated |

---

## 3. Cascade Table (per-milestone P2P)

Evaluation order is the timestamp order in `summary.json`. `P2P_req` = baseline pass_to_pass; `P2P_got` = achieved; `missing` = the gap that drops the score.

| # | Milestone | Tag_hash (8) | Agent touched JarScanner? | P2P req | P2P got | **Missing** | Reactor outcome | Notes |
|---|---|---|---|---:|---:|---:|---|---|
| 1 | M004     | `bcdcfb04` | no | 6902 | 4456 | 2446 | dubbo-common built; pre-existing fail-fast on an *unrelated* module | normal partial credit |
| 2 | M003.1   | `e14eea83` | no | 6954 | 6954 |    0 | clean (only dubbo-mutiny demo failed, which had no P2P tests) | **clean run** |
| 3 | M006     | `d5dc265c` | no | 6914 | 4873 | 2041 | pre-existing unrelated reactor failure | normal partial credit |
| 4 | M011     | `5e5a48ce` | no | — | — | — (runner `error`) | `dubbo-common` test-compile error in `LoggerTest.java` | agent's own M011 bug, not JarScanner |
| 5 | **M016.1**   | **`99e67e2a`** | **yes (introduced)** | 6915 | 1136 | **5779** | fail-fast: dubbo-native FAILURE → 92 SKIPPED | **cascade starts** |
| 6 | M019     | `854db221` | inherited | 6910 | 1131 | 5779 | fail-fast: same cascade | same bug + M019's own commit |
| 7 | **M017**     | **`99e67e2a`** (same) | inherited | 6923 | 6304 |  613 | fail-at-end: 6 modules FAILURE, 94 SUCCESS | **different runner mode, same bug, *much* less damage** |
| 8 | M003.2   | `75abdc56` | inherited | 6920 | 1141 | 5779 | fail-fast cascade | agent never worked on M003.2 |
| 9 | M001.1   | `64c74b56` | inherited | 6911 | 1131 | 5780 | fail-fast cascade | agent never worked on M001.1 |

**Pre-bug total** (M003.1, M004, M006 — M011 errored): 4487 missing out of 20770 P2P slots → **78.4% pass**.
**Post-bug total** (M016.1, M017, M019, M003.2, M001.1): 23730 missing out of 34579 P2P slots → **31.4% pass**.

**Tests directly lost to this 1 line:** the delta between pre-bug and post-bug rates, across 5 post-bug milestones:
`0.784 * 34579 - 10843 ≈ 27110 - 10843 ≈ 16300` previously-passing tests got reported as "missing" purely because of the cascade.

Two observations:

1. **M017 is the smoking gun for the runner's role.** Same broken commit as M016.1, but the M017 evaluator invokes Maven with `--fail-at-end` (the log shows dubbo-native FAIL, then modules with no dep on dubbo-native continue: 94 SUCCESS, only 5 transitive dependents fail). P2P missing drops from 5779 → 613, a **9.4x** improvement purely from changing Maven flags. This is a strong hint that reactor-mode is a tunable knob.
2. **M016.1 / M019 / M003.2 / M001.1 all use fail-fast.** The first module to fail short-circuits the entire reactor → ~90 modules marked SKIPPED → no test results → the Evaluator reports them as `pass_to_pass_missing`. Every single one of those ~5779 "missing" tests is a test that *did pass in the baseline*, i.e., pure cascade.

---

## 4. Did the Agent Self-Detect?

**No. The agent never ran any build tool, ever.**

Evidence (searches against `log/claude-conversation-2026-04-10-74bcc5c4.md`, 5511 lines):

| Needle | Matches |
|---|---:|
| `"command": ".*mvn`   | **0** |
| `"command": ".*javac` | **0** |
| `"command": ".*gradle`| **0** |
| `AutoCloseable`       | 0 |
| `BUILD FAILURE`       | 0 |
| `try-with-resources`  | 0 (in agent-authored text) |
| `JarScanner`          | 12 (all during the edit itself) |

And `log/session_history.jsonl` has only 5 lines total and none mention any of these terms.

**Workflow signature:** Read SRS → scatter-gun `Edit` tool calls across N files → `git add` everything → `git commit` → `git tag agent-impl-<M>`. No compile step, no unit-test run, no IDE-style feedback loop. The three consecutive failed-then-retried `Edit` calls on `JarScanner.java` (whitespace-mismatch) are a telltale: the agent was fighting tab vs space indentation rather than sanity-checking the code.

Ground truth on agent's mental model: in the M016.1 commit message the agent states "FR10: Fix JarScanner resource leak with try-with-resources" as if it had just completed a successful fix. It believed FR10 was done.

---

## 5. Counterfactual Score Estimate

Baseline assumptions:

- Dubbo has 12 scored milestones (the orchestrator enumerates 13 incl. already-passing M002 which is baseline).
- The agent actually tagged 7 (M003.1, M004, M006, M011, M016.1, M017, M019). M001.1 / M003.2 were evaluated on an unchanged post-M019 state.
- A milestone "resolves" only when **all** `fail_to_pass_required` pass, **all** `none_to_pass_required` pass, and `pass_to_pass_missing == 0`.

### 5.1 The cascade zeroed the P2P denominator for 5 milestones

Without the JarScanner bug, M016.1, M019, M003.2, M001.1 would hit the same ~78% pre-bug P2P retention seen on M003.1/M004/M006 (M003.1 was actually 100%). Even generous pass-rates don't automatically "resolve" a milestone, because M016.1/M019 etc. also had unmet F2P/N2P requirements. But they at least would not have been **wrongly scored as catastrophic regressions**.

### 5.2 Likely resolved-milestone count

Looking at the F2P/N2P data in summary.json:

- **M003.1**: resolved *would require* `none_to_pass_achieved==1`; got `0`. The agent missed this FR even under the clean build. → **would still fail**.
- **M004**: got `none_to_pass_achieved=1/17`, `f2p=0/2`. → **fail**.
- **M006**: `f2p=0/2`, `n2p=0/6`. → **fail**.
- **M016.1, M019, M003.2, M001.1**: all zero F2P/N2P even before counting the P2P cascade. → **fail even without the bug**.
- **M017**: `f2p=0/1`, `n2p=0/3`, plus `p2p_failed=6`. → **fail even without cascade**.
- **M011**: runner error (LoggerTest compile error — a different agent bug). → **fail**.

So the painful honest conclusion: **even if we subtract the JarScanner cascade, the agent would still have scored 0/12 resolved milestones.** GLM-5.1 + Claude Code on this task is F2P/N2P-limited, not cascade-limited.

What the cascade *did* destroy is the **partial-credit signal**. Without the bug:

- The trial telemetry would have shown "ok, pipeline broadly works, the agent just misses specific behavior changes" — a useful, differentiable signal.
- With the bug, 5 milestones have `pass_to_pass_achieved ≈ 1130` against `≈ 6920 required`, making them visually indistinguishable from "agent destroyed the repo." This collapses the benchmark's ability to discriminate between a *mostly-correct* submission and a *scorched-earth* submission on the post-M016.1 portion of the run.

**Score delta:** 0/12 → 0/12 (no change to resolved count).
**P2P-achieved-fraction delta:** 10849 / 55349 (19.6%) → ~37149 / 55349 (~67.1%). A **~3.4×** improvement in the "how close is the agent to correct" signal, purely by removing the cascade.

That is the real cost: 16,300+ tests silently converted from "pass" to "missing," masking whatever partial progress the agent *did* make on the post-M016.1 FRs.

---

## 6. Guardrail Options

These are ranked by my confidence that they'd help without compromising the benchmark's capability-measurement purpose.

### Option A — Pre-submit compile check (RECOMMENDED)

**Where:** inside the evaluator, *before* committing a milestone tag.

**What:** run `mvn -q -B -pl <touched-modules> -am compile` (or, in a repo-agnostic way, whatever the repo's canonical "compile, no tests" target is — e.g., `gradle assemble`, `cargo check`, `go build ./...`, `tsc --noEmit`, `python -c "import <pkg>"`). Non-zero exit ⇒ reject the submission and surface the stderr back to the agent as a `build_failed` event.

- **(a) Info leak:** minimal. The agent only learns "your commit doesn't compile" — exactly the feedback any developer has instantly from their IDE. It does **not** leak test names, baseline diffs, or hidden tests. The compile-error message is the same one the agent would see if it had run `mvn compile` itself (which GLM-5.1 here apparently couldn't be bothered to do).
- **(b) Implementation cost:** ~50 LOC per-language in the evaluator. A lookup table `repo → precheck_cmd` already exists in most of the infra for the eval container — re-use it.
- **(c) Capability signal:** ✅ preserved. In fact it *sharpens* it — agents that can actually write compiling code aren't handed a free 0% for one typo, while agents that can't are still exposed (they'll bounce off the precheck and have to retry, which is *itself* a measurable capability: "can this agent iterate on feedback?"). A bounded retry budget (say, 2–3 compile retries per milestone) gives clean semantics.
- **Risk:** the agent learns it can iterate-to-green without thinking. Mitigation: cap retries and log each attempt as a cost.

### Option B — Per-module test scoring (reactor-aware grader)

**What:** instead of a single `pass_to_pass_missing` count for the whole repo, score per-module and mark "module didn't build → module's P2P tests are 'inconclusive,' not 'missing.'" For a SKIPPED module, you can look at which module *actually* failed and only penalize its (transitive-dependent) set.

Equivalently: switch the grader's `mvn` invocation to `--fail-at-end` everywhere (this is what M017 accidentally did, and it reduced cascade damage ~9.4×).

- **(a) Info leak:** zero. The agent sees nothing different.
- **(b) Implementation cost:** one-line flag change for Maven (`--fail-at-end`); for Gradle use `--continue`; for Bazel use `--keep_going`. Harder for monolithic test runners.
- **(c) Capability signal:** ✅ *strengthened.* Currently a single compile bug hides 90% of the test result matrix. Fail-at-end gives you the full matrix and you can see which specific behaviors the agent got right vs wrong. This is pure upside *except* that it masks how bad cascades actually are in the real Maven workflow — but since we're measuring agent code quality, not build-tool ergonomics, that's fine.
- **Risk:** some projects have ordering dependencies where downstream tests *need* upstream artifacts; you'll get secondary failures that aren't really "the agent's fault." Mitigation: classify those as `build_error` rather than `test_failure` when summarizing.

**I'd actually ship both A and B together.** They solve different problems: A catches the bug *before* it's measured; B bounds the damage when A is circumvented.

### Option C — Auto-reject on P2P drop > X% vs previous milestone

**What:** compare `pass_to_pass_achieved` of milestone N against the rolling average of milestones 1..N-1. If it drops by more than, say, 30% absolute, auto-reject the commit, revert to the last green state, and surface "your changes broke the baseline" to the agent.

- **(a) Info leak:** medium. The agent learns "your change caused a big regression vs. previous runs," which is a *useful* capability signal (real devs see this in CI), but it also gives the agent a hint about the grader's hidden baseline numerically. An adversarial agent could probe this to infer baseline sizes.
- **(b) Implementation cost:** moderate. Needs previous-milestone bookkeeping in the evaluator, a threshold, and a revert mechanism.
- **(c) Capability signal:** ⚠️ mixed. It rewards "not breaking anything" (good) but it also lets the agent notice a drop-threshold it can game ("delete this risky FR, stay below the threshold"). Prefer making this a *hard gate on compile errors only*, not on test deltas.
- **Verdict:** too crude alone, but a useful defense-in-depth on top of A+B.

### Option D — Sandboxed pre-tag build server the agent can optionally hit

**What:** expose a tool (`check_build`) in the agent harness that runs `mvn -pl touched -am -o compile` and returns stdout/stderr. Make it *explicitly available*, so a well-behaved agent uses it; a careless one doesn't.

- **(a) Info leak:** low — same info as running `mvn` in a Bash tool, which the agent *already could have done* but didn't. Doesn't change difficulty for capable agents.
- **(b) Implementation cost:** trivial — it's just a sanctioned shortcut to something the agent could already do.
- **(c) Capability signal:** ✅ preserved, but note: GLM-5.1 here didn't use the Bash tool to run mvn even though it existed. So adding *another* optional tool won't help unless we also prompt the agent to use it. Weak on its own.
- **Verdict:** nice-to-have convenience, not a guardrail.

### Summary recommendation

Ship **Option A (pre-submit compile precheck) + Option B (`--fail-at-end` grader)** together:

- A prevents the "tag-and-forget" failure mode that cost this trial, and gives faint iterate-on-feedback signal.
- B ensures that when A fails (e.g., a bug slips through in a file not part of the precheck's module scope), the cascade blast radius is limited to actual dependents.
- Neither leaks hidden test names or changes the task's substantive difficulty.
- Combined cost: maybe a day of infra work; ~100 LOC.
- Combined benefit on this trial: **P2P achievement rate 19.6% → ~67%,** recovering ~16300 tests' worth of capability signal, without changing resolved count (which was honestly-zero anyway).

The underlying lesson is that **the agent's 0% score here was overdetermined** (GLM-5.1 genuinely couldn't resolve the F2P/N2P behaviors), but the benchmark reporting makes it look *catastrophically* worse than a neutral "this model is weak" signal — and that kind of noise is exactly what makes benchmark comparisons across models unreliable.
