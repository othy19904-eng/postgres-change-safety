# PostgreSQL Change Safety v0.10.0

This release is the first public package intended for external trial beyond the original v0.1 prototype.

## What changed since v0.1

- Real `pg_stat_statements` start/end measurement windows instead of relying on unrelated cumulative snapshots.
- Stable normalized-SQL fingerprints that can match the same workload shape across PostgreSQL 14 and 17 without persisting raw SQL by default.
- Causal isolation with repeated controlled evidence, unresolved-confounder blocking, and explicit `UNKNOWN`.
- Workload-overlap checks that prevent optimistic metadata from hiding missing query coverage.
- Structural plan fingerprints, plan-variant distributions, dominant-plan changes, and parameter-sensitivity coverage.
- Real PostgreSQL CI benchmarks across PostgreSQL 14 and 17, including repeated trials and false-clearance tests.
- A one-command synthetic demo: `pgchangesafe demo`.

## Try it

```bash
python -m pip install "postgres-change-safety @ git+https://github.com/othy19904-eng/postgres-change-safety.git@v0.10.0"
pgchangesafe demo
```

For live PostgreSQL capture:

```bash
python -m pip install "postgres-change-safety[postgres] @ git+https://github.com/othy19904-eng/postgres-change-safety.git@v0.10.0"
```

See the README for the start → workload → end → window → compare flow.

## Important limits

PostgreSQL Change Safety is experimental. It reports evidence and unknowns; it is not a production certification system and does not make a GO/NO-GO decision for you. Real customer workloads, additional concurrency/locking evidence, and broader production validation remain future work.
