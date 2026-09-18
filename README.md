# PostgreSQL Change Safety

**Status: experimental / RETEST.** This project is not a certification system and does not make production go/no-go decisions for you.

PostgreSQL Change Safety is a small evidence engine for answering two questions that ordinary before/after benchmarking often leaves open:

1. **What probably caused a regression?**
2. **How much of the production decision did we actually test, and what is still unknown?**

The first public MVP intentionally does **not** clone databases, capture traffic, or replace replay tools. It consumes comparison evidence from existing test/replay workflows and adds two layers:

- **Causal Isolation Engine** — evaluates controlled experiments and reports a probable cause only when the evidence reproduces the regression and restoration removes it. Otherwise it says `UNKNOWN`.
- **Decision Coverage / Unknowns** — scores workload volume, bind-value diversity, writes, peak concurrency, background jobs, replay success, and environment similarity. Missing coverage is shown explicitly and caps evidence strength.

## Why this exists

A PostgreSQL upgrade can complete successfully while a real workload still regresses. Existing tools can provide clones, replay, plans, benchmarks, and query diffs. This project explores a narrower layer: **decision reliability** above those primitives.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
pgchangesafe assess examples/major-upgrade.json
```

Example output:

```text
PostgreSQL Change Safety Assessment
====================================
Evidence strength: MEDIUM (.../100)
Decision coverage: .../100
Causal resolution rate: 100.0%

Regressions detected: 1
- checkout_by_customer: 18.0ms -> 34.0ms (...x), severity=..., cause=config.random_page_cost, confidence=...

Known unknowns:
- Peak concurrency is not covered
```

## Input model

The MVP accepts one JSON file with four evidence groups:

- `baseline.queries`: query fingerprints with `calls` and `p95_ms` (or `mean_ms`)
- `candidate.queries`: the same fingerprints after the change
- `coverage`: decision-level coverage claims
- `experiments`: controlled factor-isolation experiments

See [`examples/major-upgrade.json`](examples/major-upgrade.json).

## Important behavior

The engine is deliberately conservative:

- A slowdown is not automatically assigned a cause.
- Competing explanations keep attribution at `UNKNOWN`.
- Missing write/concurrency/environment coverage prevents a `HIGH` evidence rating.
- The tool reports evidence; the operator owns the deployment decision.

## Traction gate

This repository is being tested GitHub-first before any marketplace or paid product work.

**Gate: 100 real installs/downloads/runs before marketplace work.** Stars are not counted as adoption.

After that gate, the next question is whether users repeatedly need the same engine across upgrades, configuration changes, extension changes, and migrations.

## Non-goals for v0.1

- production certification
- database cloning
- traffic capture/replay
- automatic schema migration
- automatic production deployment
- pretending unknown evidence is safe

## License

MIT
