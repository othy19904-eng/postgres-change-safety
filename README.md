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
