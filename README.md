# PostgreSQL Change Safety

[![CI](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml/badge.svg)](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml)

**Experimental / RETEST.** This tool reports evidence and unknowns. It is not a production certification authority.

PostgreSQL Change Safety is being developed around two questions that ordinary before/after benchmarks often leave unresolved:

1. **What probably caused this regression?**
2. **How much of the production decision did we actually test — and what is still unknown?**

## v0.3: causal isolation with confounder blocking

v0.3 makes causal attribution deliberately harder.

A regression is no longer enough, and one apparently successful experiment is no longer enough. The engine now:

- detects PostgreSQL version and captured configuration differences,
- requires repeated controlled evidence before reporting `PROBABLE_CAUSE`,
- checks whether other changed factors were actually tested,
- reports unresolved confounders explicitly,
- falls back to `UNKNOWN` when competing explanations remain.

The intended path is:

```
real baseline/candidate evidence
          ↓
regression detection
          ↓
environment/config diff
          ↓
repeated controlled experiments
          ↓
unresolved confounder check
          ↓
PROBABLE_CAUSE or UNKNOWN
          ↓
decision coverage + known unknowns
```

## v0.4: blind planted-regression benchmark

v0.4 adds an evaluation layer that is separate from the engine.

Each benchmark case has two parts:

- `payload`: the only evidence the engine receives.
- `oracle`: the hidden expected regression/cause used only after the engine has produced its answer.

Run it with:

```bash
pgchangesafe benchmark benchmarks/blind_cases.json --strict
```

The first suite measures four things:

- regression detection accuracy,
- correct attribution when a cause is actually provable,
- correct abstention as `UNKNOWN` when evidence is insufficient or competing,
- false causal attribution count.

The suite currently includes planted cases for clean version regressions, clean config regressions, unresolved config confounders, competing explanations, single-trial evidence, unstable repeated evidence, regressions with no causal evidence, unrelated experiments, negative controls, and non-regressions with misleading causal-looking trials.

This is still a **synthetic blind benchmark**, not proof that the engine is production-safe on real PostgreSQL workloads. Its purpose is to catch logic errors and overconfident attribution before moving to a real database-backed benchmark.

## v0.5: real PostgreSQL blind benchmark

v0.5 adds a database-backed benchmark in CI using real **PostgreSQL 14 and PostgreSQL 17** service containers.

The benchmark creates deterministic join tables on both servers and measures server-side execution time with `EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF)`. It then generates a blind suite dynamically.

The first real suite contains three cases:

1. **Real config regression** — PostgreSQL 17 with hash joins enabled versus disabled on a large equality join with no supporting indexes. Repeated controlled restore/reapply trials must support `config.enable_hashjoin` before attribution.
2. **Version + config confounder** — PostgreSQL 14/hash join enabled versus PostgreSQL 17/hash join disabled. The config factor is tested, but the version factor is not independently isolated, so the expected causal result is `UNKNOWN`.
3. **Stable control** — the same PostgreSQL 17 configuration measured twice; the engine must not invent a regression.

CI fails if the planted slowdown is too weak, if regression detection is wrong, if a provable config cause is missed, or if the engine makes a causal claim in the version-confounded case.

This is materially stronger than the synthetic suite because the timings come from real PostgreSQL execution. The first attempted planted factor (`work_mem`) was rejected after CI showed that the low-memory run was actually faster on that workload; the benchmark was changed rather than forcing the expected result. It is still not proof of production safety: the workload is deterministic and intentionally small, and we do **not** yet claim a real version-only PostgreSQL regression.

## v0.6: multi-mechanism stability gate

v0.6 makes the real PostgreSQL benchmark harder in two ways.

First, it tests **two distinct planted regression mechanisms** instead of one:

1. **Planner-method regression** — disable hash joins for a large equality join.
2. **Schema regression** — remove an index used by a selective lookup.

Both plants are verified twice: the benchmark checks that the expected PostgreSQL plan shape actually changed, and it checks that the measured slowdown clears a minimum effect threshold before the oracle is allowed to expect a regression.

Second, GitHub Actions now runs the entire real benchmark in **three independent matrix trials**, each with fresh PostgreSQL 14 and PostgreSQL 17 service containers. Each trial must pass all four real cases:

- hash-join regression → correct cause,
- index-removal regression → correct cause,
- version + config confounder → `UNKNOWN`,
- stable negative control → no invented regression.

That means a pull request must currently survive **12 real database-backed case evaluations across 3 fresh CI trials**, in addition to the synthetic blind suite and unit tests.

The CI workflow also avoids duplicate branch-push runs: feature branches are tested through pull requests, while direct push testing is reserved for `main`.

This is a stability gate, not a production-safety claim. The workloads are still controlled and intentionally small; real customer traces and broader failure mechanisms remain future validation work.

## v0.7: workload-level safety gate

v0.7 moves the benchmark from isolated query cases to a **multi-query workload** and adds an explicit defense against false clearance.

The PostgreSQL-backed workload contains five fingerprints:

- a hash-join query with a planted planner regression,
- an indexed lookup with a planted schema/index regression,
- a stable point read,
- a stable aggregate,
- a stable write measured with rollback.

The complete workload changes two factors at once (`enable_hashjoin` and lookup-index presence). Each regressed query gets both a positive causal experiment and a negative control for the unrelated factor. The engine must therefore identify the correct cause while leaving the stable workload untouched.

A second adversarial case deliberately **omits the known-regressed hash-join fingerprint from the candidate snapshot** while supplying optimistic metadata that claims 100% workload coverage. v0.7 now derives fingerprint overlap from the actual baseline/candidate snapshots and caps declared workload coverage by that observed overlap. In the benchmark, the missing 30% of baseline call volume forces the observed overlap to 70%, creates an explicit coverage unknown, and prevents a clean HIGH-evidence result.

The benchmark evaluator still does not turn the product into a GO/NO-GO authority. Instead it defines a test-only **false-clearance** event as the dangerous combination of:

```
no detected regression
+ HIGH evidence
+ no known unknowns
+ no unresolved confounders
```

When the hidden-regression oracle says the case must not clear, CI fails if that combination appears.

The new workload suite runs in **three fresh PostgreSQL 17 CI trials** in addition to the existing PostgreSQL 14/17 regression suite and Python unit tests.

The timing measurements are real PostgreSQL execution. Some non-timing coverage fields in the benchmark are intentionally scenario inputs used to test decision-coverage logic; they are not claims that the harness itself reproduced production concurrency, bind distributions, or background jobs.

## Use real pg_stat_statements evidence

Export comparable baseline and candidate windows:

```sql
SELECT
  queryid,
  calls,
  total_exec_time,
  mean_exec_time,
  rows
FROM pg_stat_statements
WHERE calls > 0;
```

Then:

```bash
pgchangesafe import-pgss baseline.csv --output baseline.json --label pg14 --postgres-version 14.20
pgchangesafe import-pgss candidate.csv --output candidate.json --label pg17 --postgres-version 17.6
pgchangesafe compare baseline.json candidate.json
```

The comparison derives observed workload overlap from shared query fingerprints. Anything that cannot be proven from the snapshots remains explicit **UNKNOWN**.

## Capture directly from PostgreSQL

Install the optional connector:

```bash
pip install -e ".[postgres]"
```

Use an environment variable so credentials do not need to be written into shell history:

```bash
export PGCHANGE_DSN='postgresql://user:password@host/dbname'
pgchangesafe capture --output baseline.json --label pg14
```

Repeat against the candidate test environment:

```bash
export PGCHANGE_DSN='postgresql://user:password@candidate-host/dbname'
pgchangesafe capture --output candidate.json --label pg17
pgchangesafe compare baseline.json candidate.json
```

Live capture also records a small set of PostgreSQL settings so the comparison can expose configuration drift. Query text is **not captured by default**.

## Decision-level coverage

A pg_stat_statements comparison cannot prove that writes, peak concurrency, background jobs, replay success, or bind-value diversity were exercised.

Supply only what you actually know:

```json
{
  "bind_value_diversity_pct": 82,
  "write_workload_covered": true,
  "peak_concurrency_covered": false,
  "background_jobs_covered": true,
  "replay_failure_pct": 1.2,
  "environment_match_pct": 96
}
```

Then:

```bash
pgchangesafe compare baseline.json candidate.json \
  --coverage examples/decision-coverage.json
```

## Repeated controlled experiments

Causal attribution is separate from slowdown detection.

v0.3 requires repeated controlled evidence. Two independent trials are the normal minimum; alternatively a trial can declare `"replicates": 3` when it represents at least three controlled repetitions.

Example:

```json
{
  "experiments": [
    {
      "fingerprint": "queryid:101",
      "factor": "postgres.version",
      "controlled": true,
      "changed_ms": 20.1,
      "restored_ms": 10.2
    },
    {
      "fingerprint": "queryid:101",
      "factor": "postgres.version",
      "controlled": true,
      "changed_ms": 19.8,
      "restored_ms": 9.9
    }
  ]
}
```

Run:

```bash
pgchangesafe compare baseline.json candidate.json \
  --coverage examples/decision-coverage.json \
  --experiments examples/controlled-experiments.json
```

If the version changed **and** `random_page_cost` also changed, testing only the PostgreSQL version is not sufficient. The report will keep the cause at `UNKNOWN` and list `config.random_page_cost` as an unresolved confounder until that factor is tested strongly enough.

## Example report shape

```text
Environment / configuration diffs:
- postgres.version: 14.20 -> 17.6
- config.random_page_cost: 4 -> 1.1

Regressions detected: 1
- queryid:101: 10.0ms -> 20.0ms (2.0x), cause=UNKNOWN, trials=2
  unresolved confounders: config.random_page_cost

Unresolved confounders:
- config.random_page_cost
```

The system should prefer an explicit `UNKNOWN` over a false causal story.

## Measurement-window warning

`pg_stat_statements` is cumulative. Baseline and candidate snapshots are most useful when they represent comparable windows. For serious testing, reset stats or use equivalent observation windows before collecting both sides.

## Privacy

- Query text is excluded from live captures by default.
- Do not post production SQL, credentials, customer data, or sensitive logs in public issues.
- Prefer test/staging replicas for upgrade experiments.

## What this project intentionally does not build

Existing PostgreSQL tools already handle cloning, workload replay, plan inspection, and benchmarking. This project does not rebuild them.

Its intended layer is evidence reliability above those primitives.

## Development gate

We will not push this to a marketplace merely because the repository exists.

Before marketplace work, the project needs:

- a usable real PostgreSQL workflow,
- successful blind regression tests,
- evidence that strangers actually run it,
- then the 100 real install/download/run traction gate.

Stars are not counted as adoption.

## License

MIT
