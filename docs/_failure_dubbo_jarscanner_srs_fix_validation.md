# SRS Fix Validation: Dubbo M016.1 FR10 (JarScanner Resource Leak)

**Question:** Would the proposed FR10 SRS rewording have prevented GLM-5.1's cascade-
failure bug on `dubbo-plugin/dubbo-native/.../JarScanner.java`?

**Trial population sampled:** 4 Dubbo e2e trials under
`/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/`.

---

## 1. TL;DR

- **Yes, the rewording would have prevented the specific GLM-5.1 mistake** (wrapping
  `JarURLConnection` in try-with-resources) because the new SRS explicitly names
  `JarFile` as the target and explicitly forbids wrapping `JarURLConnection`.
- **1 of 4 trial models actually reproduced the cascade bug** (GLM-5.1 / claude-code);
  2 trials "did nothing" (no edit to the file under the old SRS), and 1 trial
  (codex gpt-5.4) independently wrote the correct `JarFile` fix without any guidance.
  So the old SRS is *noisy but not uniformly poisonous*: it's a ~25% bug-injection
  rate in this tiny sample, with a 25% correct-fix rate and 50% no-op rate.
- **Residual risk:** the proposed "~4 lines" and near-verbatim code snippet make this
  SRS borderline solution-leak. If the same prescription-level detail were generalised
  across every FR in the benchmark, it would erode the benchmark's capability signal
  for harder milestones. For this specific FR, which is a tiny mechanical nit
  undeserving of a full milestone anyway, the leak is acceptable — but it argues for
  either (a) dropping FR10 from the milestone altogether, or (b) shipping the rewording
  and pairing it with the compile-precheck guardrail from the cascade report.
- **Recommendation:** **Ship with one tweak** (remove the verbatim code snippet,
  keep the "`JarFile`/not `JarURLConnection`" warning), **or better yet drop FR10
  from M016.1 entirely** — it contributes zero behavioural change and only exists
  to create a failure surface.

---

## 2. Bug Premise Verification

### 2.1 Buggy SRS FR10 text (exact, lines 152–158)

File: `/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/claude-code_glm-5.1_001_backup_pre_force_2026-04-10/e2e_workspace/srs/M016.1_SRS.md`

```markdown
### FR10: Resource Leak Prevention

**Problem**: `JarScanner` does not properly close InputStream resources,
causing potential resource leaks.

**Requirements**:
- Wrap InputStream resources in try-with-resources blocks in `JarScanner`
```

The wording is exactly as described in the original bug analysis.

### 2.2 `JarScanner.java` (pristine) has zero InputStreams

`grep -c InputStream` on the pristine JarScanner (md5 `4368ce72fa…`) returns **0**.
The imports are:

```java
import java.io.File;
import java.net.JarURLConnection;
import java.net.URL;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Enumeration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.jar.JarEntry;
import java.util.jar.JarFile;
```

No `java.io.InputStream`, no `java.io.Reader`, no `InputStreamReader`, nothing.
The SRS is asking the agent to "wrap" something that doesn't exist in the file.

### 2.3 The only Closeable target in `scanURL()`

```java
} else if ("jar".equals(protocol)) {
    JarFile jar = ((JarURLConnection) resource.openConnection()).getJarFile();
    scanJar(jar);
}
```

- `java.net.URLConnection` / `java.net.JarURLConnection`: **not** `AutoCloseable`
  (long-standing JDK gotcha — URL connections are keep-alive–pool managed, no
  `close()` method).
- `java.util.jar.JarFile` (extends `ZipFile` implements `Closeable`, Java 7+):
  **is** `AutoCloseable`. This is the only legal try-with-resources target.
- `Enumeration<URL> resources`, `Enumeration<JarEntry> entry`, `URL resource`,
  `JarEntry jarEntry`: none are `AutoCloseable`.

So the premise holds: the SRS asks for something that can't be done, and the only
shape-matching thing in the file is the JarFile returned by `getJarFile()`.

### 2.4 Pristine source-of-truth

Pristine content retrieved via `git show HEAD:…JarScanner.java` inside
`openhands_glm-5_001/testbed` (HEAD = `6dd59dfaa Implement M025`; file was never
touched by any openhands commit). md5 matches `4368ce72fa…`, which is the same hash
observed in the cascade analysis for pre-M016.1 snapshots (M004/M003.1/M006/M011).

---

## 3. Cross-Model Behaviour Table

Four trials were examined. `claude-code_glm-5_router_001/testbed` exists as an empty
directory (trial never materialised), so it is excluded from the sample.

| # | Trial                                         | M016.1 commit? | Touched JarScanner? | Action                                                        | Outcome                        |
|---|-----------------------------------------------|----------------|---------------------|---------------------------------------------------------------|--------------------------------|
| 1 | `openhands_glm-5_001`                         | yes (`b4d49f806`) | **no** (pristine)   | interpreted FR10 as N/A; left file untouched                  | compiles                       |
| 2 | `codex_gpt-5.4_001`                           | yes (`da58e9135`) | **yes**             | wrapped **`JarFile`** in try-w/r + added `setUseCaches(false)` | **compiles, correct**          |
| 3 | `claude-code_glm-5_001` (not 5.1)             | **no** (trial never reached M016.1) | **no** (pristine)   | N/A — milestone skipped                                       | N/A                            |
| 4 | `claude-code_glm-5.1_001_backup_pre_force_…`  | yes (`99e67e2a`) | **yes**             | wrapped **`JarURLConnection`** in try-w/r                      | **compile error** → cascade failure of ~5779 P2P tests |

### 3.1 Distribution

- "Did nothing / file untouched": **2 / 4** (trials 1 and 3)
- "Correct `JarFile` wrap": **1 / 4** (trial 2, codex)
- "Wrong `JarURLConnection` wrap": **1 / 4** (trial 4, GLM-5.1 — the cascade bug)
- "Other (wrapped something else)": **0 / 4**

### 3.2 Key observations about the "correct" and "broken" diffs

Codex gpt-5.4 wrote:
```java
JarURLConnection connection = (JarURLConnection) resource.openConnection();
connection.setUseCaches(false);
try (JarFile jar = connection.getJarFile()) {
    scanJar(jar);
}
```

GLM-5.1 wrote:
```java
try (JarURLConnection jarConnection = (JarURLConnection) resource.openConnection()) {
    JarFile jar = jarConnection.getJarFile();
    scanJar(jar);
}
```

Both models read the same misleading SRS. The difference is entirely **whether the
model independently knew that `JarURLConnection` is not `AutoCloseable`**. Codex
did; GLM-5.1 did not. The SRS does nothing to disambiguate between these two
behaviours, which is why it's a dice roll even with capable models.

### 3.3 Why "did nothing" is a legitimate interpretation

Openhands agents searched the file, found no `InputStream`, and appear to have
treated FR10 as vacuous. Under the old SRS that is arguably the *most sensible*
response — there's nothing to wrap. Any model that is willing to freelance "find
the closest closeable shape" is gambling; GLM-5.1 lost that gamble.

---

## 4. Walk-Through of the Proposed Rewording

The proposed text (for reference):

```markdown
### FR10: Resource Leak Prevention

**Problem**: `JarScanner.scanURL()` opens a `JarFile` (via
`JarURLConnection.getJarFile()`) but never closes it, leaking file handles when
scanning many JARs.

**Requirements**:
- File: `dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`
- Wrap the `JarFile` returned by `JarURLConnection.getJarFile()` in a
  try-with-resources block so it is closed after `scanJar()` returns.
  `JarFile` implements `Closeable` (Java 7+).
- Do NOT wrap `JarURLConnection` itself; `JarURLConnection`/`URLConnection`
  are not `AutoCloseable`.
- The change is approximately 4 lines: turn
    `JarFile jar = ((JarURLConnection) resource.openConnection()).getJarFile(); scanJar(jar);`
  into
    `try (JarFile jar = ((JarURLConnection) resource.openConnection()).getJarFile()) { scanJar(jar); }`
```

Per-line failure-mode accounting:

| Line                                                    | Failure mode it addresses                              |
|---------------------------------------------------------|--------------------------------------------------------|
| "opens a `JarFile` … never closes it"                   | Removes the fictional "InputStream" target that caused openhands to no-op and GLM-5.1 to freelance. |
| Exact file path with package directory                   | Removes any ambiguity about *which* JarScanner (there's only one, but explicit is cheap). |
| "Wrap the `JarFile` returned by `JarURLConnection.getJarFile()`" | Names the exact variable/expression to wrap, directly killing the GLM-5.1 failure path. |
| "`JarFile` implements `Closeable` (Java 7+)"             | Preempts "but is that even closeable?" doubt that drives freelancing. |
| **"Do NOT wrap `JarURLConnection` itself; … not `AutoCloseable`"** | **Directly names and prohibits GLM-5.1's exact mistake.** This is the load-bearing sentence of the rewording. |
| "approximately 4 lines" hint                             | Tells the model not to over-engineer (e.g., refactor `scanJar` signature). |
| Verbatim before/after snippet                            | Removes all remaining doubt about syntax. (Also leaks essentially the full answer — see §5.) |

### 4.1 Counterfactual for each observed model

- **openhands (did nothing)**: The new SRS now names a concrete, findable target
  (`JarFile`) that *exists* in the file. An agent that grep'd for `InputStream`
  and found zero matches will, under the new SRS, grep for `JarFile` and find
  two matches, the first of which is exactly the line to edit. → **would
  produce the correct fix**.
- **codex gpt-5.4 (correct)**: The new SRS does not contradict what codex
  already did; if anything the "Do NOT wrap `JarURLConnection`" line rules out
  codex's optional `setUseCaches(false)` tweak, but that tweak is a no-op for
  correctness. → **still correct, possibly slightly leaner**.
- **claude-code glm-5_001 (no M016.1)**: Trial didn't reach the milestone. N/A.
- **claude-code glm-5.1 (broken)**: The sentence "Do NOT wrap `JarURLConnection`
  itself; `JarURLConnection`/`URLConnection` are not `AutoCloseable`" is a
  direct refutation of what GLM-5.1 actually did. Combined with the verbatim
  4-line snippet, there is essentially no way to produce the
  `try (JarURLConnection jarConnection = …)` form without actively ignoring the
  SRS. → **would produce the correct fix**.

Expected distribution under the new SRS: 4 correct / 0 broken / 0 no-op (the
2 "did nothing" models now have an actionable, file-existing target).

---

## 5. Residual Risks

### 5.1 The verbatim snippet is close to a solution leak

The SRS now contains both the before-diff and the after-diff code, byte-for-byte.
For *this* FR the answer is one line of Java, so "telling the model the answer"
is approximately the same as "telling the model what to do." But if this level
of prescription became a pattern across the benchmark, it would erase capability
signal on harder milestones where the SRS' job is to specify *behaviour*, not
*edits*. Mitigation: keep the "don't wrap `JarURLConnection`" warning (which
carries knowledge) and drop the before/after code block (which carries the
answer).

### 5.2 The "approximately 4 lines" hint over-constrains

A well-behaved model might expand `scanJar` to take a `JarFile` via a factory,
add an `IOException` rethrow in `scanURL`, or refactor the whole block. The
"~4 lines" hint is fine *here* but is a template that, if copied to other FRs,
discourages legitimate refactors. Recommend downgrading to "one-line change,
wrap the existing assignment in a try-with-resources."

### 5.3 Models could still misinterpret — but the likely alternatives are benign

Possible residual mis-wraps:

| Candidate wrap                            | `AutoCloseable`? | Compiles? | Semantically OK? |
|-------------------------------------------|------------------|-----------|------------------|
| `JarFile jar` ✅ (intended)                | yes              | yes       | yes              |
| `Enumeration<URL> resources`              | no               | **no**    | n/a              |
| `Enumeration<JarEntry> entry` (in `scanJar`) | no           | **no**    | n/a              |
| `URLConnection` / `JarURLConnection`      | no               | **no**    | n/a              |
| `InputStream` (doesn't exist)             | n/a              | **no**    | n/a              |

Of all the closeable-shaped things in the file, **only `JarFile` compiles**.
A model that is determined to ignore the explicit "wrap `JarFile`" instruction
and instead wraps anything else will get a compile error similar to the current
cascade — so **the rewording doesn't eliminate the *class* of bug, only the
specific instance**. This is why the rewording is necessary-but-not-sufficient
and must be paired with the compile-precheck guardrail from the cascade report
(Option A in `_failure_dubbo_jarscanner_cascade.md §6`).

### 5.4 Subtlety of `getJarFile()` and JAR caching

`JarURLConnection.getJarFile()` returns the cached `JarFile` held by the URL
connection's protocol handler. By default, Java's jar: URL stream handler caches
open `JarFile` objects via `JarFileFactory`, and calling `close()` on a cached
`JarFile` will **mark it closed in the cache, so subsequent `getJarFile()` for
the same URL will throw `IllegalStateException: zip file closed`**. This is a
real concern for `dubbo-native` at runtime if the AOT generation tool walks the
same jar URL twice.

Mitigations already in use by codex gpt-5.4:
```java
connection.setUseCaches(false);  // opt out of the cache
try (JarFile jar = connection.getJarFile()) { … }
```
`setUseCaches(false)` forces `getJarFile()` to return a fresh, non-cached
`JarFile` that is safe to `close()`.

**The proposed SRS does not mention `setUseCaches(false)`.** This means the
rewording delivers a *compile-clean but runtime-fragile* fix — AOT processing
code that scans the same jar twice could crash with `zip file closed`. Whether
this matters in practice depends on whether `JarScanner` is only instantiated
once per run (it is — see the constructor `scanURL(PACKAGE_NAME_PREFIX)`, and
the class appears to be used at most once per AOT run) and whether subsequent
`ClassLoader.getResources()` calls on the same URL within the same JVM happen
elsewhere.

Bottom line: the fix is *benign* for the benchmark (no behavioural regression
expected in dubbo's test suite) but is *not* a clean upstream-quality resource
hygiene fix. If we want to mirror the codex solution, the SRS should add one
sentence: *"Call `setUseCaches(false)` on the `JarURLConnection` before
`getJarFile()` to avoid sharing the `JarFile` with the JVM's jar URL cache."*

### 5.5 The FR is cosmetic — it doesn't affect any test

Critically, the M016.1 milestone has no fail-to-pass or pass-to-pass test that
exercises JarScanner's file-handle lifetime. No test exists in the dubbo repo
that scans JARs, measures fd count, and asserts. So whatever FR10 does or
doesn't do, it **cannot positively contribute to the milestone score** — it
can only *subtract* by breaking compilation. This is the deepest residual risk:
**FR10 has negative expected value for the benchmark** regardless of wording.

---

## 6. Recommendation

### 6.1 Tier the options

**(a) Drop FR10 from M016.1 entirely. — STRONGLY PREFERRED.**
- The requirement is cosmetic (no test covers it), purely a compile-break risk,
  and contributes nothing to the F2P/N2P/P2P signal.
- Removing it fully eliminates the cascade-failure surface and doesn't change
  the benchmark's capability-measurement goal.
- Zero implementation cost (one markdown deletion).

**(b) Ship the proposed rewording with tweaks. — ACCEPTABLE FALLBACK.**
- Keep: explicit "wrap `JarFile`"; explicit "Do NOT wrap `JarURLConnection`".
- Drop: verbatim before/after code block (solution leak).
- Add: one sentence about `setUseCaches(false)` for runtime correctness
  parity with the codex fix.
- Tweaked version:

  ```markdown
  ### FR10: Resource Leak Prevention

  **Problem**: `JarScanner.scanURL()` opens a `JarFile` via
  `JarURLConnection.getJarFile()` but never closes it, leaking file handles when
  scanning many JARs.

  **Requirements**:
  - File:
    `dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`
  - In the `"jar".equals(protocol)` branch of `scanURL()`, close the `JarFile`
    returned by `JarURLConnection.getJarFile()` after `scanJar()` returns
    (e.g. via try-with-resources). `JarFile` implements `Closeable` (Java 7+).
  - Do **not** wrap `JarURLConnection` itself: `JarURLConnection` / `URLConnection`
    are not `AutoCloseable` and the code will fail to compile.
  - To avoid sharing the `JarFile` with the JVM's jar URL cache (which can cause
    `IllegalStateException: zip file closed` on subsequent scans),
    call `setUseCaches(false)` on the `JarURLConnection` before `getJarFile()`.
  ```

**(c) Ship the proposed rewording as-is. — NOT RECOMMENDED.**
- Fixes the specific GLM-5.1 mistake but leaks the full answer verbatim and
  omits the runtime-correctness hint. Tolerable but suboptimal.

**(d) Reject the rewording / keep old SRS. — NOT ACCEPTABLE.**
- Old SRS has ~25% bug-injection rate (1 of 4 trials), with the bug blast
  radius being ~5779 missing P2P tests. The benchmark noise floor cannot absorb
  that.

### 6.2 Orthogonal guardrail (repeat from cascade report)

Whatever we do to the SRS, **also ship Option A from
`_failure_dubbo_jarscanner_cascade.md`**: pre-submit `mvn -q -pl <touched> -am
compile` in the evaluator. The rewording alone cannot defend against
pathological freelancing — an adversarial model can still wrap
`Enumeration<URL>` and break the build. The compile check catches *any*
compile-break bug, not just this one, and turns this whole class of cascade
from "catastrophic benchmark poison" into "bounded per-milestone penalty with
feedback the agent can fix."

### 6.3 Combined recommendation

1. **Drop FR10 from M016.1 SRS** (tier-a).
2. If FR10 must stay for milestone-diff reasons, ship the tweaked rewording
   from §6.1(b).
3. Independently ship the compile-precheck guardrail (Option A from cascade
   report) so that future SRS typos of this shape can't produce another
   5779-test cascade regardless of wording quality.

---

## Appendix: File / path inventory

- Buggy SRS: `/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/claude-code_glm-5.1_001_backup_pre_force_2026-04-10/e2e_workspace/srs/M016.1_SRS.md`
- Pristine JarScanner (md5 `4368ce72fa…`):
  `/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/openhands_glm-5_001/testbed/dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`
- Broken JarScanner (md5 `2afa6d4b6c…`):
  `/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/claude-code_glm-5.1_001_backup_pre_force_2026-04-10/testbed/dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`
- Correct codex JarScanner (md5 `27a2208633…`):
  `/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/codex_gpt-5.4_001/testbed/dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`
- claude-code glm-5_001 JarScanner (pristine, M016.1 never attempted):
  `/data2/gangda/EvoClaw-data/apache_dubbo_dubbo-3.3.3_dubbo-3.3.6/e2e_trial/claude-code_glm-5_001/testbed/dubbo-plugin/dubbo-native/src/main/java/org/apache/dubbo/aot/generate/JarScanner.java`
- claude-code glm-5_router_001 testbed: **empty** (trial never materialised);
  excluded from cross-model comparison.
- Original cascade analysis:
  `/home/gangda/workspace/EvoClaw-Bench/EvoClaw/docs/_failure_dubbo_jarscanner_cascade.md`
