# PostgreSQL Change Safety

[![CI](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml/badge.svg)](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml)

**Experimental / RETEST.** This tool reports evidence and unknowns. It is not a production certification authority.

PostgreSQL Change Safety is being developed around two questions that ordinary before/after benchmarks often leave unresolved:

1. **What probably caused this regression?**
2. **How much of the production decision did we actually test — and what is still unknown?**

## v0.2 workflow

v0.2 removes the biggest usability problem in the first prototype: you no longer need to hand-write the baseline and candidate query JSON.

You can use either exported `pg_stat_statements` CSV files or capture snapshots directly from PostgreSQL.

### Option A — import existing pg_stat_statements CSV

Export these columns from both environments/windows:

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

Save the results as CSV with headers, then:

```bash
pgchangesafe import-pgss baseline.csv --output baseline.json --label pg14
pgchangesafe import-pgss candidate.csv --output candidate.json --label pg17

pgchangesafe compare baseline.json candidate.json
```

The comparison automatically derives observed workload overlap from shared query fingerprints. Anything it cannot know from `pg_stat_statements` — bind-value diversity, peak concurrency, write coverage, replay failures — remains explicitly **UNKNOWN** instead of being assumed safe.

### Option B — capture from a live PostgreSQL test environment

Install the optional PostgreSQL connector:

```bash
pip install -e ".[postgres]"
```

Set the DSN through an environment variable so credentials do not need to be put into shell history:

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

Query text is **not captured by default**. The snapshot uses `queryid` fingerprints. Use `--include-query-text` only when you explicitly want query text stored locally.

## Add decision-level coverage

A raw `pg_stat_statements` comparison cannot prove that writes, peak concurrency, background jobs, replay success, or parameter diversity were exercised.

Supply what you actually know:

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

The automatically derived workload-volume overlap is kept unless you explicitly provide a value.

## Causal isolation

Regression detection and causal attribution are separate.

A query getting slower does **not** prove that the PostgreSQL version caused it. Controlled experiments can be supplied independently:

```json
[
  {
    "fingerprint": "queryid:123456789",
    "factor": "config.random_page_cost",
    "controlled": true,
    "changed_ms": 33.0,
    "restored_ms": 18.5
  }
]
```

If a factor reproduces the candidate slowdown and restoring it moves the result back toward baseline, the engine may report `PROBABLE_CAUSE`. If evidence is weak or competing explanations remain, it reports `UNKNOWN`.

## What v0.2 intentionally does not build

Existing PostgreSQL tools already handle cloning, workload replay, plan inspection, and benchmarking. This project does not rebuild them.

Its intended layer is:

```
real baseline/candidate evidence
          ↓
regression detection
          ↓
causal isolation
          ↓
decision coverage + known unknowns
          ↓
evidence strength
```

## Measurement-window warning

`pg_stat_statements` is cumulative. Baseline and candidate snapshots are most useful when they represent comparable windows. For serious testing, reset stats or use equivalent observation windows before collecting both sides.

Do not interpret this MVP as a certification system.

## Privacy

- Query text is excluded from live captures by default.
- Do not post production SQL, credentials, customer data, or sensitive logs in public issues.
- Prefer test/staging replicas for upgrade experiments.

## Development gate

We will not push this to a marketplace merely because the repository exists.

Before marketplace work, the project needs:
- a usable real PostgreSQL workflow,
- successful blind regression tests,
- evidence that strangers actually run it,
- and then the 100 real install/download/run traction gate.

Stars are not counted as adoption.

## License

MIT
