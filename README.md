# PostgreSQL Change Safety

[![CI](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml/badge.svg)](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml)
[![Release](https://img.shields.io/badge/release-v0.10.0-blue)](https://github.com/othy19904-eng/postgres-change-safety/releases/tag/v0.10.0)

**Detect PostgreSQL change regressions, show the evidence behind the suspected cause, and make missing coverage explicit.**

PostgreSQL Change Safety is an experimental evidence layer for major upgrades and other PostgreSQL changes. It combines comparable `pg_stat_statements` windows, stable cross-version SQL fingerprints, controlled causal experiments, plan-variant evidence, and explicit unknowns.

It is **not** a production certification authority and does not make a GO/NO-GO decision for you.

## 60-second demo — no PostgreSQL required

Install the tagged release directly from GitHub:

```bash
python -m pip install "postgres-change-safety @ git+https://github.com/othy19904-eng/postgres-change-safety.git@v0.10.0"
pgchangesafe demo
```

The demo is synthetic and clearly labeled as such. It shows the reporting contract in one command: a regression, its workload share, repeated causal evidence, a dominant plan change, plan-distribution shift, and parameter-sensitivity coverage.

Expected shape:

```text
Synthetic demonstration — not production evidence.

PostgreSQL Change Safety Assessment
Evidence strength: HIGH
Regressions detected: 1
... cause=schema.orders_customer_idx ...
... plan=VARIANT_SHIFT ...

Plan variant analysis:
... dominant_changed=True ...
... parameter_sensitivity=COVERED
```

## 5-minute real PostgreSQL path

Prerequisite: `pg_stat_statements` must be available on the PostgreSQL environments you want to compare.

Install the PostgreSQL connector:

```bash
python -m pip install "postgres-change-safety[postgres] @ git+https://github.com/othy19904-eng/postgres-change-safety.git@v0.10.0"
```

Use the same private fingerprint key on baseline and candidate so the same SQL shape can be matched without persisting raw SQL:

```bash
export PGCHANGE_FINGERPRINT_KEY='use-a-private-random-secret'
export PGCHANGE_DSN='postgresql://user:password@baseline-host/dbname'

pgchangesafe capture --output baseline-start.json --label baseline-start
# Run the representative baseline workload window.
pgchangesafe capture --output baseline-end.json --label baseline-end

pgchangesafe window baseline-start.json baseline-end.json \
  --output baseline-window.json --label baseline
```

Repeat on the candidate environment:

```bash
export PGCHANGE_DSN='postgresql://user:password@candidate-host/dbname'

pgchangesafe capture --output candidate-start.json --label candidate-start
# Run the comparable candidate workload window.
pgchangesafe capture --output candidate-end.json --label candidate-end

pgchangesafe window candidate-start.json candidate-end.json \
  --output candidate-window.json --label candidate

pgchangesafe compare baseline-window.json candidate-window.json
```

Query text is read transiently to derive the stable normalized-SQL fingerprint but is **not persisted by default**.

## What the report is trying to answer

- Which important query fingerprints regressed, by how much, and what workload share do they represent?
- Did PostgreSQL version, configuration, schema, or another tested factor actually reproduce the slowdown?
- Did the dominant execution plan or plan-variant distribution change?
- Was parameter diversity represented strongly enough to trust the plan evidence?
- How much observed workload overlapped between baseline and candidate?
- Which confounders and coverage gaps are still unresolved?

When the evidence is insufficient, the intended output is `UNKNOWN`, not a causal story invented from correlation.

## Current validation

The repository CI currently exercises Python 3.10/3.12/3.13 plus real PostgreSQL 14 and 17 containers. It includes blind planted regressions, repeated-run stability checks, multi-query workload coverage attacks, real `pg_stat_statements` measurement windows, cross-version normalized-SQL fingerprint matching, and real plan-variant tests.

These controlled tests are evidence that the implementation behaves as designed under the covered scenarios. They are not proof that every production workload or upgrade is safe.

## Development history

### v0.3: causal isolation with confounder blocking

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

### v0.4: blind planted-regression benchmark

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

### v0.5: real PostgreSQL blind benchmark

v0.5 adds a database-backed benchmark in CI using real **PostgreSQL 14 and PostgreSQL 17** service containers.

The benchmark creates deterministic join tables on both servers and measures server-side execution time with `EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF)`. It then generates a blind suite dynamically.

The first real suite contains three cases:

1. **Real config regression** — PostgreSQL 17 with hash joins enabled versus disabled on a large equality join with no supporting indexes. Repeated controlled restore/reapply trials must support `config.enable_hashjoin` before attribution.
2. **Version + config confounder** — PostgreSQL 14/hash join enabled versus PostgreSQL 17/hash join disabled. The config factor is tested, but the version factor is not independently isolated, so the expected causal result is `UNKNOWN`.
3. **Stable control** — the same PostgreSQL 17 configuration measured twice; the engine must not invent a regression.

CI fails if the planted slowdown is too weak, if regression detection is wrong, if a provable config cause is missed, or if the engine makes a causal claim in the version-confounded case.

This is materially stronger than the synthetic suite because the timings come from real PostgreSQL execution. The first attempted planted factor (`work_mem`) was rejected after CI showed that the low-memory run was actually faster on that workload; the benchmark was changed rather than forcing the expected result. It is still not proof of production safety: the workload is deterministic and intentionally small, and we do **not** yet claim a real version-only PostgreSQL regression.

### v0.6: multi-mechanism stability gate

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

### v0.7: workload-level safety gate

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

### v0.8: real pg_stat_statements measurement windows

v0.8 makes the real-workload path stricter. A raw `pg_stat_statements` snapshot is cumulative, so two captures can look comparable even when they represent very different observation periods. v0.8 therefore adds explicit **measurement windows**:

```
capture start
    ↓
run the real workload window
    ↓
capture end
    ↓
subtract pg_stat_statements counters
    ↓
window snapshot
    ↓
compare baseline window vs candidate window
```

The window contains only counter deltas observed between the two captures. It records total calls and execution time for that interval and rejects a window if `pg_stat_statements` was reset during measurement.

The capture path is also privacy-first:

- query text is still excluded by default,
- raw `queryid` is no longer the default fingerprint,
- fingerprints are pseudonymous SHA-256 values,
- set `PGCHANGE_FINGERPRINT_KEY` to use HMAC-SHA256 with the same secret on both environments,
- `--raw-queryid` exists only as an explicit compatibility/debug opt-in.

The capture session now tries to start the PostgreSQL connection with `pg_stat_statements.track = 'none'` already applied, so even the command that disables tracking cannot contaminate the workload window. If PostgreSQL permissions prevent startup-level disabling, the capture falls back but marks the window uncertain instead of silently treating it as clean.

A cumulative `pg_stat_statements_live` or CSV snapshot can still be inspected, but v0.8 will not let that alone become HIGH evidence. HIGH evidence requires comparable verified windows or an explicit higher-quality measurement source.

CI now includes a real PostgreSQL 17 instance started with `shared_preload_libraries=pg_stat_statements`. The test creates the extension, captures a real start/end window, runs SQL, derives delta counters, verifies pseudonymous fingerprints, verifies that SQL text was not stored, and checks that a valid window can participate in the evidence layer.

This is still not production certification. A valid measurement window proves that the sampled counters are temporally comparable; it does not prove peak concurrency, bind diversity, writes, background jobs, or complete workload coverage.

### v0.9: stable cross-version workload fingerprints

v0.9 removes a major ambiguity in PostgreSQL major-upgrade comparisons. PostgreSQL `queryid` is useful inside one server/version, but it is not treated here as a guaranteed stable identity across major versions.

Live capture now fingerprints each `pg_stat_statements` row from a locally normalized SQL shape instead of from `queryid`:

```
pg_stat_statements query text
        ↓
local canonicalization
  - comments removed
  - literals/bind values replaced
  - whitespace normalized
  - quoted identifiers preserved
        ↓
SHA-256 / HMAC-SHA256
        ↓
pseudonymous fingerprint
        ↓
raw SQL discarded unless explicitly requested
```

With the same `PGCHANGE_FINGERPRINT_KEY` on both sides, the same SQL shape can therefore be matched between PostgreSQL 14 and PostgreSQL 17 without persisting raw SQL in the snapshot.

Important limits:

- the normalizer is deliberately conservative, not a full SQL parser,
- query text is read transiently by the local capture process because it is needed to derive the stable fingerprint,
- `--include-query-text` is still opt-in,
- `--raw-queryid` disables cross-version-stable matching,
- CSV imports only get cross-version-stable fingerprints when the CSV contains a `query` column,
- a 100% fingerprint match does not prove that bind distributions, peak concurrency, writes, background jobs, or all production traffic were represented.

CI now starts real PostgreSQL 14 and PostgreSQL 17 instances with `pg_stat_statements`, runs the same three-query workload on both, derives measurement windows, and requires the resulting fingerprint sets and call counts to match exactly with 100% observed workload overlap. The test also verifies that query text was not persisted.

If a 14→17 comparison uses legacy `queryid`-based fingerprints, the system now records an explicit cross-version fingerprint unknown and prevents that comparison from becoming clean HIGH evidence.

### v0.10: plan variants and parameter sensitivity

v0.10 adds a second identity layer above the stable SQL fingerprint:

```
same SQL fingerprint
        ↓
structural EXPLAIN plan fingerprints
        ↓
variant distribution
        ↓
parameter-bucket coverage
        ↓
STABLE / VARIANT_SHIFT / UNKNOWN
```

A plan fingerprint keeps structural fields such as node type, join type, relation/index identity, strategy, and child-plan shape while discarding volatile cost, row-estimate, timing, and buffer values. Raw EXPLAIN JSON is used locally to calculate the fingerprint and is **not persisted** in the enriched snapshot.

Plan evidence is attached explicitly:

```bash
pgchangesafe attach-plans baseline-window.json baseline-plans.json \
  --output baseline-with-plans.json

pgchangesafe attach-plans candidate-window.json candidate-plans.json \
  --output candidate-with-plans.json

pgchangesafe compare baseline-with-plans.json candidate-with-plans.json
```

The plan-sample file is intentionally separate from `pg_stat_statements` because PostgreSQL does not store execution plans there. A sample looks like:

```json
{
  "samples": [
    {
      "query_fingerprint": "pgss:...",
      "plan": [{"Plan": {"Node Type": "Index Scan"}}],
      "sample_count": 5,
      "parameter_bucket": "narrow"
    }
  ]
}
```

The `parameter_bucket` field should identify a non-sensitive class of bind/input values, not the raw value itself. For plan sensitivity to count as covered, both baseline and candidate currently require at least two buckets covering at least 80% of their plan samples. Otherwise the system reports parameter sensitivity as `UNKNOWN` and caps evidence instead of assuming that one observed plan represents every bind pattern.

v0.10 reports:

- baseline and candidate plan-variant counts,
- newly appearing and disappearing plan fingerprints,
- dominant-plan switches,
- plan-distribution shift percentage,
- parameter-sensitivity coverage,
- per-regression plan status in the normal assessment.

CI includes a real PostgreSQL 17 controlled test. It uses the same normalized SQL shape across two parameter buckets, plants an index-plan → sequential-plan variant shift, verifies a dominant-plan change and a large distribution shift, then repeats the assessment without bucket labels and requires evidence to fall from clean HIGH confidence.

The CI plant is deliberately controlled; it proves the plan-variant accounting and UNKNOWN behavior, not that all real PostgreSQL parameter-sensitive plans have been modeled.

## Use real pg_stat_statements evidence

Export comparable baseline and candidate windows:

```sql
SELECT
  queryid,
  calls,
  total_exec_time,
  mean_exec_time,
  rows,
  query
FROM pg_stat_statements
WHERE calls > 0;
```

Then:

```bash
pgchangesafe import-pgss baseline.csv --output baseline.json --label pg14 --postgres-version 14.20
pgchangesafe import-pgss candidate.csv --output candidate.json --label pg17 --postgres-version 17.6
pgchangesafe compare baseline.json candidate.json
```

For cross-version CSV matching, include the `query` column so v0.9 can derive normalized-SQL fingerprints. The imported JSON still omits query text by default, but the CSV itself contains SQL and must be handled as sensitive data. If the CSV has no `query` column, the importer falls back to `queryid` fingerprints and a major-version comparison remains an explicit fingerprint-stability **UNKNOWN**.

The comparison derives observed workload overlap from shared query fingerprints. Anything that cannot be proven from the snapshots remains explicit **UNKNOWN**.

## Capture directly from PostgreSQL

Install the optional connector:

```bash
pip install -e ".[postgres]"
```

Use environment variables so credentials and the optional fingerprint secret do not need to be written into shell history:

```bash
export PGCHANGE_DSN='postgresql://user:password@host/dbname'
export PGCHANGE_FINGERPRINT_KEY='same-secret-for-both-sides'
```

Capture the start of the baseline observation window:

```bash
pgchangesafe capture --output baseline-start.json --label baseline-start
```

Run the real baseline workload for the period you want to measure, then capture the end and derive the delta-only window:

```bash
pgchangesafe capture --output baseline-end.json --label baseline-end
pgchangesafe window baseline-start.json baseline-end.json \
  --output baseline-window.json --label baseline
```

Repeat the same process against the candidate environment using the **same** `PGCHANGE_FINGERPRINT_KEY`:

```bash
export PGCHANGE_DSN='postgresql://user:password@candidate-host/dbname'

pgchangesafe capture --output candidate-start.json --label candidate-start
# run the comparable candidate workload window
pgchangesafe capture --output candidate-end.json --label candidate-end

pgchangesafe window candidate-start.json candidate-end.json \
  --output candidate-window.json --label candidate

pgchangesafe compare baseline-window.json candidate-window.json
```

Live capture also records a small set of PostgreSQL settings so comparison can expose configuration drift. Query text is read transiently for normalization but is **not persisted by default**. Use the same `PGCHANGE_FINGERPRINT_KEY` on baseline and candidate so the HMAC fingerprints are comparable; snapshots produced with different schemes/keys are rejected.

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

`pg_stat_statements` is cumulative. Prefer the v0.8 `capture → window` flow so comparison uses counter deltas from explicit start/end boundaries. If a reset occurs inside a window, the command rejects that window. If reset metadata or capture self-tracking cleanliness cannot be verified, the uncertainty is reported and evidence is capped rather than silently treated as complete.

## Privacy

- Query text is read transiently by v0.9 live capture to derive a stable normalized-SQL fingerprint, but it is excluded from persisted snapshots by default.
- Prefer setting `PGCHANGE_FINGERPRINT_KEY` so fingerprints use HMAC rather than an unkeyed hash.
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
