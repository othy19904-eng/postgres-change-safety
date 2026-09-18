# PostgreSQL Change Safety

[![CI](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml/badge.svg)](https://github.com/othy19904-eng/postgres-change-safety/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/othy19904-eng/postgres-change-safety)](https://github.com/othy19904-eng/postgres-change-safety/releases/latest)

**Experimental / RETEST.** Not a certification system and not an automatic production go/no-go authority.

PostgreSQL Change Safety is a small evidence engine for a narrow problem that ordinary before/after benchmarks often leave unresolved:

1. **What probably caused this PostgreSQL regression?**
2. **How much of the production decision did we actually test — and what is still unknown?**

It is aimed at engineers evaluating **major PostgreSQL upgrades, configuration changes, extension changes, or migration tests** who already have baseline/candidate evidence but need a more conservative decision layer.

## Try it in 60 seconds

Install directly from the tagged release:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install "git+https://github.com/othy19904-eng/postgres-change-safety.git@v0.1.0"
curl -L -o major-upgrade.json https://raw.githubusercontent.com/othy19904-eng/postgres-change-safety/v0.1.0/examples/major-upgrade.json
pgchangesafe assess major-upgrade.json
```

Or download the public release asset:

**[Download PostgreSQL Change Safety v0.1.0](https://github.com/othy19904-eng/postgres-change-safety/releases/download/v0.1.0/postgres-change-safety-v0.1.zip)**

Expected shape of the result:

```text
PostgreSQL Change Safety Assessment
====================================
Evidence strength: MEDIUM
Decision coverage: 88.5/100
Causal resolution rate: 100.0%

Regressions detected: 1
- checkout_by_customer: 18.0ms -> 34.0ms (...), cause=config.random_page_cost

Known unknowns:
- Peak concurrency is not covered
```

## What is different here?

The first public MVP intentionally does **not** clone databases, capture traffic, or replace replay/benchmarking tools.

It consumes comparison evidence from existing workflows and adds two layers:

### 1. Causal Isolation Engine

A slowdown is not automatically assigned a cause.

Controlled experiments must reproduce the regression and restoring a factor must move the result back toward baseline. If evidence is weak or competing explanations remain, attribution becomes:

`UNKNOWN`

### 2. Decision Coverage / Unknowns

Coverage is measured at the **change-decision level**, not only at the query/planner level.

The MVP scores:

- observed workload volume exercised
- bind-value diversity
- write workload coverage
- peak concurrency coverage
- background jobs
- replay failures
- environment similarity

Missing coverage is surfaced explicitly and caps evidence strength.

## Why this exists

A PostgreSQL upgrade can complete successfully while a real workload still regresses.

Existing tools can already provide clones, replay, plans, benchmarks, and query diffs. This project explores a narrower layer above those primitives:

```
baseline/candidate evidence
        ↓
regression detection
        ↓
causal isolation
        ↓
decision coverage + known unknowns
        ↓
evidence strength
```

## Input model

The CLI accepts one JSON file with four evidence groups:

- `baseline.queries`: query fingerprints with `calls` and `p95_ms` (or `mean_ms`)
- `candidate.queries`: the same fingerprints after the change
- `coverage`: decision-level coverage claims
- `experiments`: controlled factor-isolation experiments

See [`examples/major-upgrade.json`](examples/major-upgrade.json).

## Who should try v0.1?

Please try it if you are currently doing any of these:

- PostgreSQL 14 → 15/16/17/18 upgrade testing
- managed PostgreSQL major-version upgrade assessment
- parameter-group/configuration change validation
- extension upgrade testing
- before/after workload replay where the hard part is deciding whether the evidence is sufficient

If the current JSON input is too artificial for your workflow, that is useful feedback too.

## Feedback we want

Open an issue with one of these:

- **It found a regression correctly**
- **It attributed the wrong cause**
- **It should have returned UNKNOWN**
- **A critical coverage dimension is missing**
- **I cannot feed my existing PostgreSQL evidence into it**

Do not include production SQL, credentials, customer data, or sensitive logs in public issues.

## Important behavior

The engine is deliberately conservative:

- A slowdown is not automatically assigned a cause.
- Competing explanations keep attribution at `UNKNOWN`.
- Missing write/concurrency/environment coverage prevents a `HIGH` evidence rating.
- The tool reports evidence; the operator owns the deployment decision.

## Traction gate

This repository is being tested GitHub-first before any marketplace or paid product work.

**Gate: 100 real installs/downloads/runs before marketplace work.** Stars are not counted as adoption.

We track release downloads and repository clone/run signals separately because neither alone proves 100 unique users.

## Non-goals for v0.1

- production certification
- database cloning
- traffic capture/replay
- automatic schema migration
- automatic production deployment
- pretending unknown evidence is safe

## License

MIT
