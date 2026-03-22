# Comprehensive Analysis Report: Extending EvoClaw Beyond Coding Tasks

## Part 1: Current Coupling Analysis

### 1.1 Task Definition (SRS Documents)

**Coupling Level: LOW**

Task definitions are plain Markdown files (`{milestone_id}_SRS.md`) loaded from an `srs_root` directory and copied into the container at `/e2e_workspace/srs/`. The system treats them as opaque blobs -- the orchestrator never parses their content. The only structural requirement is the naming convention `{milestone_id}_SRS.md`.

Code reference: `_copy_srs_for_tasks()` in `harness/e2e/orchestrator.py` simply copies files by milestone ID. The prompt template at `harness/e2e/prompt/v2.md` tells the agent to "Read the SRS file" but imposes no schema on SRS content.

**Implication**: SRS files could describe any task (SQL migrations, Terraform plans, documentation) without framework changes. This is the most domain-agnostic component.

### 1.2 Execution Environment (Docker)

**Coupling Level: MEDIUM**

The `ContainerSetup` class (`harness/e2e/container_setup.py`) provisions Docker containers with a `fakeroot` user, git configuration, and network lockdown. The tight coupling points are:

- **Network whitelist** (lines 21-83): Hard-coded domains for package registries (npm, pip, crates.io, Maven, Go proxy). Non-coding scenarios would need different whitelists (e.g., Terraform registry, Ansible Galaxy, database endpoints).
- **Toolchain directory permissions** (lines 302-328): Hard-codes paths like `/usr/local/cargo`, `/usr/local/go`, `/root/.npm` -- all programming-language toolchains.
- **`/testbed` working directory**: Convention assumed by many components. Conceptually this is just "the workspace" and is not inherently code-specific, but the name carries connotations.

The Docker abstraction itself (container lifecycle, volume mounts, exec) is domain-agnostic.

### 1.3 Checkpoint Signaling (Git Tags)

**Coupling Level: MEDIUM-HIGH**

This is one of the tighter coupling points. The entire checkpoint mechanism depends on git:

- Agents signal completion via `git tag agent-impl-{milestone_id}` (prompt v2.md, line 49-52).
- The watcher loop in `E2ETrialRunner._run_watcher_loop()` polls `git tag -l` inside the container.
- Source snapshots are extracted via `git archive --format=tar {agent_tag}`.
- Tag hash debouncing prevents premature evaluation.

Git is deeply embedded as both the checkpoint mechanism and the artifact extraction mechanism. Non-coding scenarios that don't naturally produce git-trackable artifacts (e.g., database state, trained ML models, running infrastructure) would need an alternative signaling mechanism.

### 1.4 Artifact Snapshot Extraction

**Coupling Level: HIGH**

Snapshot extraction is tightly coupled to source-code conventions:

- `SrcFileFilter` filters files based on `src_dirs`, `test_dirs`, `exclude_patterns`, `generated_patterns` (`harness/utils/src_filter.py`).
- `ROOT_BUILD_FILES` in `harness/utils/snapshot.py` lists `Cargo.toml` and `Cargo.lock` -- Rust-specific.
- The tar archive is filtered to remove test files but keep generated code like `.pb.go` files.
- `_filter_tar_archive()` assumes the snapshot contains source files organized in directory hierarchies.

This entire subsystem assumes the deliverable is a set of source files in a directory tree. Scenarios producing different artifact types (SQL files, Terraform state, Jupyter notebooks) might still fit, but scenarios producing non-file artifacts (running services, database schemas, trained models) would not.

### 1.5 Evaluation (Test Runners, Classifiers)

**Coupling Level: HIGH**

This is the most tightly coupled component:

- `PatchEvaluator` (`harness/e2e/evaluator.py`) applies a tar/diff patch to a Docker container and runs tests.
- Test execution uses framework-specific commands: pytest, go_test, maven, cargo, jest, mocha (`harness/test_runner/core/test_executor.py`).
- The evaluator does `_checkout_to_tag()` for git tags like `milestone-{id}-start` and `milestone-{id}-end`.
- Compilation checks use `build_command` from repo config.
- Rust-specific logic replaces inline test regions with ground-truth tests (`process_rust_files_in_container`).

The entire evaluation pipeline assumes: (1) artifacts are source code patches, (2) quality is measured by running a test suite against the patched code, and (3) test frameworks produce parseable reports.

### 1.6 Result Classification Scheme

**Coupling Level: MEDIUM**

The `TestClassifier` (`harness/test_runner/core/classifier.py`) classifies results into a 3x4 matrix of state transitions (`pass_to_pass`, `fail_to_pass`, `none_to_pass`, etc.). This scheme is actually quite general -- it measures "did expected improvements happen without regressions." The concepts of:

- **fail_to_pass**: Required fixes were achieved
- **pass_to_pass**: Nothing was broken (no regressions)
- **none_to_pass**: New capabilities were added

...are transferable to many domains (e.g., "previously failing SQL query now returns correct results", "existing API endpoints still work"). The coupling is in the assumption that you can enumerate discrete, named test cases with binary pass/fail outcomes.

### 1.7 Agent Frameworks

**Coupling Level: LOW**

The `AgentFramework` ABC (`harness/e2e/agents/base.py`) is cleanly abstracted with a registry pattern. The interface is generic:

- `get_container_mounts()`: Docker volume mounts
- `get_container_init_script()`: Setup script
- `build_run_command()`: Command to start the agent
- `build_resume_command()`: Command to resume

The framework delegates to concrete implementations (Claude Code, Codex, Gemini, OpenHands) via the strategy pattern. Adding new agent types requires no changes to the core -- just register a new implementation.

---

## Part 2: Compatible Scenarios (Deep Dive)

### Scenario 1: Data Engineering (SQL Pipelines, ETL, dbt)

**Feasibility: HIGH**

**Mapping to EvoClaw abstractions:**
- **Milestones**: Each milestone is a dbt model, SQL transformation, or ETL stage to implement/refine.
- **SRS**: Describes expected transformations, schema changes, data quality requirements.
- **Checkpoint**: Agent writes SQL/dbt files and commits via git tag. This maps naturally since dbt projects are git-managed.
- **Evaluation**: Instead of running test suites, run `dbt test` or custom SQL assertions against a test database seeded in the eval container.

**What works out of the box:**
- DAG management (dbt's own DAG concept maps directly to EvoClaw milestones).
- Docker-based execution (dbt runs in containers).
- Git-based checkpointing (dbt projects are source files).
- The classification scheme (`fail_to_pass` = failing dbt test now passes).

**What needs adaptation:**
- Eval containers need a database service (PostgreSQL/DuckDB) -- either embedded or sidecar.
- `test_executor.py` needs a `dbt_test` framework option that runs `dbt test --select {model}` and parses dbt's JSON output.
- Network whitelist needs database endpoints.

**Concrete milestone example:**
- Milestone M001: "Create staging model `stg_orders` that deduplicates raw orders and casts timestamps."
- Eval: `dbt test` asserts uniqueness, not-null constraints, row count expectations.

### Scenario 2: Infrastructure as Code (Terraform, Kubernetes, Ansible)

**Feasibility: HIGH**

**Mapping:**
- **Milestones**: Terraform modules, K8s manifests, Ansible playbooks to create/modify.
- **Checkpoint**: Git tag on `.tf`, `.yaml`, or playbook files.
- **Evaluation**: `terraform validate`, `terraform plan` (dry-run), `kubeval`, `ansible-lint`, or Open Policy Agent (OPA) policy checks.

**What works out of the box:**
- File-based artifacts fit the snapshot extraction model.
- DAG dependencies map naturally (networking before compute, namespaces before deployments).
- Git checkpoint signaling works.

**What needs adaptation:**
- Evaluation cannot run `terraform apply` in a test container (no real cloud provider). Must rely on `terraform plan` + policy-as-code validation.
- Need custom test runners for `terraform validate`, `tflint`, OPA `conftest`, `kubeval`.
- Classification: "fail_to_pass" = validation error now passes; "pass_to_pass" = existing resources still valid.
- Network whitelist: Terraform registry (`registry.terraform.io`).

**Concrete milestone example:**
- M001: "Define VPC module with public/private subnets."
- Eval: `terraform validate` passes, `terraform plan` shows expected resource count, OPA policy checks pass.

### Scenario 3: DevOps / CI-CD Pipeline Construction

**Feasibility: MEDIUM-HIGH**

**Mapping:**
- **Milestones**: Build stages, deployment pipelines, monitoring configs.
- **Checkpoint**: Git tag on `.github/workflows/`, `Jenkinsfile`, `.gitlab-ci.yml`.
- **Evaluation**: YAML lint, action-validator, dry-run tools, schema validation.

**What works:**
- CI/CD configs are files in git repos -- natural fit.
- DAG: build-before-deploy, test-before-release.

**What needs adaptation:**
- No real CI system to execute pipelines. Must use static validation: `actionlint` (GitHub Actions), schema validators, custom assertion scripts.
- Test executor needs CI-validation framework support.

**Concrete milestone:**
- M001: "Create GitHub Actions workflow for running unit tests on PRs."
- Eval: `actionlint` passes, YAML validates against schema, required steps present.

### Scenario 4: Data Science / ML Workflows

**Feasibility: MEDIUM**

**Mapping:**
- **Milestones**: Feature engineering, model training, evaluation pipeline, experiment tracking.
- **Checkpoint**: Python scripts, Jupyter notebooks, pipeline configs.
- **Evaluation**: Script execution producing metrics; metric thresholds as "tests."

**What works:**
- Python scripts are source files -- snapshot extraction works.
- DAG: data prep before training, training before evaluation.

**What needs adaptation:**
- Jupyter notebooks need special handling (`.ipynb` files are JSON; execution via `nbconvert` or `papermill`).
- "Tests" become metric assertions (accuracy > 0.85, loss < 0.3) -- need a custom test framework adapter.
- Training may require GPU or significant compute -- Docker eval containers need resource constraints.
- Model artifacts (`.pt`, `.pkl`) don't fit the source-file-only snapshot model.
- Reproducibility requires fixed random seeds, data versioning.

**Concrete milestone:**
- M001: "Implement data preprocessing pipeline that handles missing values and produces feature matrix."
- Eval: Run preprocessing script; assert output shape, no NaN values, column types match spec.

### Scenario 5: Technical Writing / Documentation (with Validation)

**Feasibility: MEDIUM-HIGH**

**Mapping:**
- **Milestones**: Sections of documentation, API references, tutorials.
- **Checkpoint**: Git tag on Markdown/RST/AsciiDoc files.
- **Evaluation**: Linting (markdownlint, vale), link checking, structure validation, possibly LLM-based quality scoring.

**What works:**
- Documentation is files in git -- perfect fit for snapshot extraction.
- Simple DAG (intro before advanced topics, API reference before tutorials).

**What needs adaptation:**
- Test runner needs documentation linter integration (`vale`, `markdownlint`, link checkers).
- Classification: "fail_to_pass" = lint errors fixed; "pass_to_pass" = existing docs still valid.
- Quality assessment may need LLM-as-judge scoring (non-deterministic).

**Concrete milestone:**
- M001: "Write API authentication section covering OAuth2 flow with code examples."
- Eval: markdownlint passes, all code examples are syntactically valid, required sections present.

### Scenario 6: System Administration / Configuration Management

**Feasibility: MEDIUM-HIGH**

**Mapping:**
- **Milestones**: Server configurations, security hardening, service setup.
- **Checkpoint**: Config files (nginx, systemd, sshd_config) in git.
- **Evaluation**: Config syntax validation, testinfra, Serverspec, InSpec.

**What works:**
- Config files are text files -- snapshot extraction works.
- DAG: base OS before services, services before hardening.

**What needs adaptation:**
- Eval containers need the target services installed to validate configs (e.g., `nginx -t`).
- InSpec/testinfra integration for infrastructure testing.
- Some validations require running services (e.g., testing nginx config requires nginx binary).

**Concrete milestone:**
- M001: "Configure nginx reverse proxy with SSL termination and rate limiting."
- Eval: `nginx -t` passes, InSpec tests verify SSL settings, rate limit directives present.

### Scenario 7: Database Schema Evolution / Migration

**Feasibility: HIGH**

**Mapping:**
- **Milestones**: Schema migrations (create tables, add indexes, data migrations).
- **Checkpoint**: Migration files (SQL, Alembic, Flyway) committed via git tag.
- **Evaluation**: Apply migrations to test database, run schema assertions, data integrity checks.

**What works:**
- Migration files are source files in git.
- DAG maps directly to migration ordering.

**What needs adaptation:**
- Eval containers need an embedded database (SQLite, PostgreSQL via Docker-in-Docker or sidecar).
- Custom test runner: apply migration, then run SQL assertions.
- Need "before" database state seeded in the eval container.

**Concrete milestone:**
- M001: "Create migration adding `user_preferences` table with foreign key to `users`."
- Eval: Apply migration, assert table exists, foreign key constraint holds, rollback works.

### Scenario 8: API Design and Implementation

**Feasibility: HIGH**

**Mapping:**
- **Milestones**: API endpoints, request/response schemas, middleware.
- **Checkpoint**: OpenAPI specs + implementation code via git tag.
- **Evaluation**: Schema validation, contract testing, possibly running the API and hitting endpoints.

**What works:**
- This is largely a coding scenario -- already supported.
- API specs (OpenAPI YAML) are files.

**What needs adaptation:**
- Contract testing tools (Schemathesis, Dredd) as test runner options.
- May need to start the API server in the eval container and run integration tests.

**Concrete milestone:**
- M001: "Implement `POST /users` endpoint with validation and 201 response."
- Eval: Existing test suite + Schemathesis fuzzing against OpenAPI spec.

### Scenario 9: Security Auditing / Penetration Testing Workflows

**Feasibility: MEDIUM-HIGH**

**Mapping:**
- **Milestones**: Audit reports, vulnerability fixes, security configurations.
- **Checkpoint**: Remediation code or config changes committed via git tag.
- **Evaluation**: Security scanning tools (Bandit, Semgrep, OWASP ZAP in passive mode).

**What works:**
- Remediation is code/config changes -- fits the file-based model.
- DAG: discovery before remediation, remediation before verification.

**What needs adaptation:**
- Need security scanner integration in test runner.
- "Tests" are scanner findings (vulnerability count must decrease).
- Classification: "fail_to_pass" = vulnerability now remediated.

**Concrete milestone:**
- M001: "Fix SQL injection vulnerability in user login endpoint."
- Eval: Semgrep/Bandit no longer flags the pattern; existing tests still pass.

### Scenario 10: Research Reproducibility (Scientific Computing)

**Feasibility: MEDIUM**

**Mapping:**
- **Milestones**: Data processing, analysis scripts, figure generation, statistical tests.
- **Checkpoint**: Scripts and notebooks committed via git tag.
- **Evaluation**: Script execution produces expected outputs; numerical tolerance checks.

**What works:**
- Scripts are source files.
- DAG: data prep before analysis, analysis before visualization.

**What needs adaptation:**
- Output comparison with numerical tolerance (not binary pass/fail).
- Large datasets may not fit in eval containers.
- Non-deterministic algorithms need special handling.
- Custom test runner: execute script, compare output files.

**Concrete milestone:**
- M001: "Implement normalization pipeline that produces `normalized_data.csv` matching reference within 1e-6 tolerance."
- Eval: Run script, compare output to reference file with tolerance.

---

## Part 3: Abstraction Recommendations

### 3.1 Interfaces That Need Generalization

**A. Evaluation Interface (highest priority)**

The current `PatchEvaluator` class is monolithic and hard-codes the "apply patch, run tests, compare results" flow. This should be abstracted into an `EvaluationStrategy` interface:

```python
class EvaluationStrategy(ABC):
    @abstractmethod
    def prepare_environment(self, container_name: str, snapshot_path: Path) -> bool:
        """Apply artifact to eval environment. Returns success."""

    @abstractmethod
    def run_evaluation(self, container_name: str, output_dir: Path) -> Dict[str, Any]:
        """Run evaluation. Returns standardized results."""

    @abstractmethod
    def classify_results(self, baseline: Dict, results: Dict) -> Dict[str, List[str]]:
        """Classify results against baseline. Returns classification."""
```

**B. Checkpoint/Signal Interface**

Abstract the "detect completion" mechanism away from git tags:

```python
class CheckpointDetector(ABC):
    @abstractmethod
    def poll_for_submissions(self, container_name: str) -> List[Submission]:
        """Check for new submissions. Returns list of detected submissions."""

    @abstractmethod
    def extract_artifact(self, submission: Submission, output_path: Path) -> bool:
        """Extract deliverable artifact from submission."""
```

The current git-tag implementation would become `GitTagCheckpointDetector`. Alternatives could include file-sentinel detection (write a `.done` file), API-based signaling, or database state snapshots.

**C. Network Whitelist Configuration**

Move `WHITELISTED_DOMAINS` from hard-coded list to a per-scenario config file:

```yaml
# scenario_config.yaml
network:
  whitelisted_domains:
    - registry.terraform.io
    - hashicorp.com
  blocked_domains:
    - github.com
```

**D. Test Runner Plugin Interface**

The current `get_default_test_cmd()` function with its hard-coded framework commands should become a plugin:

```python
class TestRunnerPlugin(ABC):
    @abstractmethod
    def get_test_command(self, workers: int, timeout: int, output_file: str) -> str:
        """Return the test command for this framework."""

    @abstractmethod
    def parse_report(self, report_path: Path) -> Dict[str, Any]:
        """Parse raw report into standardized format."""
```

### 3.2 Proposed Plugin/Adapter Architecture

```
EvoClaw Core
  │
  ├── orchestrator.py (domain-agnostic DAG + lifecycle management)
  │
  ├── plugins/
  │     ├── checkpoints/
  │     │     ├── git_tag.py          (current behavior)
  │     │     ├── file_sentinel.py    (for non-git scenarios)
  │     │     └── db_snapshot.py      (for database scenarios)
  │     │
  │     ├── evaluators/
  │     │     ├── test_suite.py       (current behavior: run tests, classify)
  │     │     ├── validation.py       (static validation: lint, schema check)
  │     │     ├── script_output.py    (run script, compare output)
  │     │     └── metric_threshold.py (ML metrics above threshold)
  │     │
  │     ├── artifact_extractors/
  │     │     ├── git_archive.py      (current behavior)
  │     │     ├── file_copy.py        (copy specific files)
  │     │     └── db_dump.py          (database state export)
  │     │
  │     └── network_profiles/
  │           ├── coding.yaml         (current whitelist)
  │           ├── iac.yaml            (terraform, ansible registries)
  │           └── data_eng.yaml       (database endpoints, dbt)
  │
  └── scenarios/
        ├── coding/                   (current default)
        ├── data_engineering/
        ├── infrastructure_as_code/
        └── database_migration/
```

A scenario would be defined by a config file selecting which plugins to use:

```yaml
# scenario: data_engineering
checkpoint: git_tag
artifact_extractor: git_archive
evaluator: test_suite
test_runner: dbt_test
network_profile: data_eng
container_image: dbt-project:latest
workspace_dir: /dbt_project
```

### 3.3 Minimum Refactoring for Top 3 Scenarios

The top 3 most feasible non-coding scenarios are:

1. **Data Engineering (dbt)** -- Feasibility: HIGH
2. **Infrastructure as Code (Terraform/K8s)** -- Feasibility: HIGH
3. **Database Schema Migration** -- Feasibility: HIGH

**Minimum refactoring required:**

**Phase 1: Configuration extraction (estimated 2-3 days)**
- Extract `WHITELISTED_DOMAINS` to a YAML config file loaded at runtime. Currently hard-coded in `container_setup.py` lines 21-83.
- Make `ROOT_BUILD_FILES` in `snapshot.py` configurable (or empty for non-Rust/Go projects).
- Make `/testbed` workspace path configurable in `ContainerSetup.__init__()` (already a parameter `workdir`, but other components still hard-code `/testbed`).

**Phase 2: Test runner generalization (estimated 3-5 days)**
- Add `dbt_test` framework to `get_default_test_cmd()` in `test_executor.py`: `dbt test --profiles-dir /profiles --target test --output json > {output_file}`
- Add `terraform_validate` framework: `terraform validate -json > {output_file}`
- Add `sql_migration` framework: custom script that runs migrations and executes assertion queries.
- Create parsers in `report_parser.py` for each new framework's output format.
- Add corresponding entries to `OUTCOME_MAPPINGS` in `classifier.py`.

**Phase 3: Evaluation generalization (estimated 3-5 days)**
- Extract the "apply patch + run tests" flow from `PatchEvaluator.evaluate()` into a method that can be overridden.
- The Rust-specific test region replacement code (`_apply_tar_to_container` in evaluator.py) should be behind a framework-specific flag, not executed by default.
- Make `_check_compilation()` optional and configurable per scenario (already partially done via `build_command`).

**Total estimated effort: 8-13 person-days** for a working prototype supporting dbt, Terraform validation, and SQL migration scenarios.

### 3.4 Suggested Priority Order for Implementation

1. **Configuration extraction** -- Prerequisite for everything else. Low risk, high leverage.
2. **dbt/Data Engineering** -- Highest market demand, closest to current architecture (file-based, has built-in test framework, git-managed).
3. **Terraform/IaC validation** -- Large user base, rich validation tooling, no execution infrastructure needed.
4. **Database migration** -- Natural DAG structure, but requires embedded database in eval containers.
5. **Documentation** -- Growing demand for AI writing evaluation, but quality metrics are subjective.
6. **DevOps/CI-CD** -- Static validation only; limited depth.
7. **ML workflows** -- High complexity (GPU, large data, non-determinism).
8. **System administration** -- Niche use case.
9. **Security auditing** -- Specialized tooling.
10. **Research reproducibility** -- Academic niche, high variability.

---

## Part 4: Competitive Landscape

### 4.1 Existing Tools for Continuous Task Evaluation

**For coding agents:**
- **SWE-bench** (Princeton): Single-issue, isolated fixes. No dependency DAG, no continuous context. EvoClaw's primary differentiation.
- **DevBench**: Multi-step software development, but focuses on project creation from scratch, not evolution.
- **AgentBench** (Tsinghua): Multi-domain agent benchmark, but tasks are independent, not sequenced.
- **CORE-Bench**: Computational reproducibility benchmark -- closer to scenario 10 above, but single-task.
- **ML-Bench**: ML pipeline tasks, but isolated.

**For non-coding AI agent tasks:**
- **GAIA** (Meta): General AI assistant benchmark with tool use. Tasks are independent.
- **WebArena / OSWorld / Computer Use benchmarks**: Focus on UI interaction, not persistent project evolution.
- **Tau-bench**: Customer service tasks with tools. Domain-specific, not generalizable.
- **HumanEval / MBPP**: Code-only, single-function, no context.

**For IaC/DevOps specifically:**
- No known continuous evaluation harness exists. Most IaC testing is done via CI pipelines (Terratest, kitchen-terraform) but these are not agent evaluation frameworks.

**For data engineering:**
- **dbt's built-in testing** exists but is not an agent evaluation framework.
- No known continuous task evaluation tool for data pipeline agents.

### 4.2 Market Gap

The market gap is substantial:

1. **No continuous, multi-step evaluation exists for non-coding AI agent tasks.** Every existing benchmark evaluates tasks independently. EvoClaw's DAG-based sequential evaluation is unique.

2. **No "bring your own domain" evaluation harness exists.** Existing benchmarks are monolithic -- you evaluate on their data or not at all. EvoClaw's architecture already supports custom data, and extending this to custom domains would be a first-mover advantage.

3. **Agent evaluation in DevOps/IaC/Data Engineering is entirely unserved.** As AI agents increasingly target these domains (Terraform copilots, dbt assistants, Kubernetes operators), there is no standardized way to evaluate them on multi-step tasks.

4. **Enterprise demand for "continuous AI agent QA"** is growing. Companies deploying AI agents need to verify they can handle sequential, dependent tasks without regressing -- exactly what EvoClaw measures.

### 4.3 Differentiation Strategy

EvoClaw's core differentiators for a multi-domain positioning:

1. **Continuous context**: The only framework that evaluates an agent's ability to maintain coherent context across a sequence of dependent tasks. This matters more in non-coding domains where tasks build on each other (migration 2 depends on migration 1's schema).

2. **Dependency DAG**: Real-world tasks have ordering constraints. EvoClaw models this natively. No competitor does.

3. **Regression detection via classification**: The `fail_to_pass` / `pass_to_pass` / `none_to_pass` taxonomy generalizes beyond code to any domain with enumerable success criteria. "Did you fix what was broken? Did you break what was working? Did you add what was needed?"

4. **Agent-agnostic harness**: The `AgentFramework` abstraction already supports four different agents. This portability extends naturally to non-coding agents.

5. **Docker-based isolation**: Reproducible evaluation across domains. Each scenario can define its own eval container image.

**Recommended positioning**: "EvoClaw: A continuous task evaluation harness for AI agents" -- dropping the word "coding" from the tagline and making it the default (but not only) scenario. This positions EvoClaw as the evaluation infrastructure layer that any AI-agent-product team can build on, across domains.

---

## Summary

| Component | Coupling to Coding | Refactoring Effort |
|-----------|:------------------:|:------------------:|
| Task definition (SRS) | Low | None needed |
| Agent framework abstraction | Low | None needed |
| DAG management | None | None needed |
| Docker execution environment | Medium | Config extraction (~2 days) |
| Checkpoint signaling (git tags) | Medium-High | Interface abstraction (~3 days) |
| Artifact extraction | High | Plugin system (~3 days) |
| Evaluation/test running | High | Framework plugins (~5 days) |
| Result classification | Medium | Outcome mapping extension (~1 day) |

The core DAG engine, task queue management, agent lifecycle orchestration, resume/retry logic, and result classification scheme are all domain-agnostic today. The coding-specific coupling is concentrated in three areas: snapshot extraction, evaluation, and network configuration. These are addressable with **8-13 person-days** of focused refactoring for the first three target scenarios.
