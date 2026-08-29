# Mini-Agent adapted open-source benchmark suite

This suite contains nine self-contained tasks adapted from Terminal-Bench,
SWE-bench Lite, and τ³-bench. It is designed to measure whether Mini-Agent
can produce a correct artifact, repair a real regression, or complete a
policy-constrained tool workflow. It is not an official implementation of any
upstream leaderboard and its scores must not be compared directly with those
leaderboards.

## Run

List tasks without calling a model:

```powershell
python -m benchmarks.run --list
```

Run one task or the complete suite with a configured OpenAI-compatible model:

```powershell
python -m benchmarks.run --task swe-requests-2317
python -m benchmarks.run --all --output report.json
```

Repeat each task to observe model variability. Every attempt receives a fresh
workspace and fresh MCP state; the report aggregates attempts equally by task:

```powershell
python -m benchmarks.run --all --repeat 3
```

`--repeat N` requires `N >= 1` and defaults to one attempt. Repeating a task
multiplies model/API usage and cost; use it only when you have accepted that
cost and want a variability estimate. The JSON report keeps every attempt and
computes the overall score as the equal-weight mean of per-task pass rates.

The benchmark runner reads model credentials only from the configured
`~/mini_agent/config.toml` (or `--config PATH`). It does not download source
repositories, install task dependencies, call external web services, or use
Docker while evaluating a task.

The formal registry contains three Terminal-Bench tasks, three SWE-bench Lite
regression tasks, and three τ³-bench tool workflows. All nine are LLM-planner
tasks; `rule` remains a CLI parser option for harness compatibility but has no
registered formal tasks.

## Scoring

Every task has one deterministic, subprocess-backed verifier. A task receives
`1.0` only when the complete verifier passes and `0.0` otherwise. Verifiers
check semantics and final state rather than requiring a particular tool name or
answer wording. The test suite also checks the important benchmark invariant:
the untouched fixture fails and a test-only oracle result passes.

Reports contain flat attempt records under `runs` and equal-weight per-task
aggregates under `tasks`. Source metadata includes the upstream benchmark,
task ID, pinned revision, URL, license, and the exact local adaptation.

These are Mini-Agent adaptations with rewritten fixtures and a single-call
τ³ interaction model. They are useful for deterministic regression tracking,
not substitutes for the upstream harnesses, and their scores cannot be
compared with official Terminal-Bench, SWE-bench, or τ³-bench leaderboards.

## Sources and licensing

See [THIRD_PARTY_BENCHMARKS.md](THIRD_PARTY_BENCHMARKS.md) for provenance and
notices. Only the smallest code/data fixtures needed to reproduce each issue
or workflow are vendored; no gold patch is included in an agent workspace.
