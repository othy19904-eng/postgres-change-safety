from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any, Callable

import psycopg


HASH_FP = "workload:hash-join"
INDEX_FP = "workload:index-lookup"
POINT_FP = "workload:stable-point-read"
AGG_FP = "workload:stable-aggregate"
WRITE_FP = "workload:stable-write"

CALLS = {
    HASH_FP: 3000,
    INDEX_FP: 2500,
    POINT_FP: 1500,
    AGG_FP: 1500,
    WRITE_FP: 1500,
}

HASH_QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
SELECT count(*)
FROM bench_left AS l
JOIN bench_right AS r
  ON r.k = l.k
"""

INDEX_QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
SELECT count(payload)
FROM bench_lookup
WHERE k BETWEEN 100000 AND 109999
"""

POINT_QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
SELECT sum(length(payload))
FROM bench_stable
WHERE k BETWEEN 10000 AND 14999
"""

AGG_QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
SELECT sum((k % 97) + length(payload))
FROM bench_stable
"""

WRITE_QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
UPDATE bench_write
SET counter = counter + 1
WHERE id BETWEEN 1 AND 250
"""


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def configure_session(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SET jit = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")
        # Keep merge joins disabled on both sides. The planted planner factor
        # is enable_hashjoin, so the fallback becomes a clear nested loop.
        cur.execute("SET enable_mergejoin = off")


def setup(conn) -> None:
    with conn.cursor() as cur:
        for table in (
            "bench_left",
            "bench_right",
            "bench_lookup",
            "bench_stable",
            "bench_write",
        ):
            cur.execute(f"DROP TABLE IF EXISTS {table}")

        cur.execute("""
            CREATE TABLE bench_left (
                k bigint NOT NULL,
                payload text NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE bench_right (
                k bigint NOT NULL,
                payload text NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE bench_lookup (
                k bigint NOT NULL,
                payload text NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE bench_stable (
                k bigint PRIMARY KEY,
                payload text NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE bench_write (
                id bigint PRIMARY KEY,
                counter bigint NOT NULL
            )
        """)

        cur.execute("""
            INSERT INTO bench_left (k, payload)
            SELECT
                g,
                md5(g::text) || md5((g * 17)::text)
            FROM generate_series(1, 4000) AS g
        """)
        cur.execute("""
            INSERT INTO bench_right (k, payload)
            SELECT
                g,
                md5((g * 97)::text) || md5((g * 193)::text)
            FROM generate_series(1, 4000) AS g
        """)
        cur.execute("""
            INSERT INTO bench_lookup (k, payload)
            SELECT
                g,
                md5((g * 31)::text) || md5((g * 73)::text)
            FROM generate_series(1, 300000) AS g
        """)
        cur.execute("""
            INSERT INTO bench_stable (k, payload)
            SELECT
                g,
                md5((g * 43)::text) || md5((g * 89)::text)
            FROM generate_series(1, 120000) AS g
        """)
        cur.execute("""
            INSERT INTO bench_write (id, counter)
            SELECT g, 0
            FROM generate_series(1, 5000) AS g
        """)

        cur.execute(
            "CREATE INDEX bench_lookup_k_idx "
            "ON bench_lookup (k)"
        )

        for table in (
            "bench_left",
            "bench_right",
            "bench_lookup",
            "bench_stable",
            "bench_write",
        ):
            cur.execute(f"ANALYZE {table}")


def set_hashjoin(conn, enabled: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SET enable_hashjoin = "
            + ("on" if enabled else "off")
        )


def ensure_lookup_index(conn, present: bool) -> None:
    with conn.cursor() as cur:
        if present:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS "
                "bench_lookup_k_idx ON bench_lookup (k)"
            )
        else:
            cur.execute(
                "DROP INDEX IF EXISTS bench_lookup_k_idx"
            )


def server_version(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        return str(cur.fetchone()[0])


def _walk_nodes(plan: dict[str, Any]) -> list[str]:
    nodes = [str(plan.get("Node Type", ""))]
    for child in plan.get("Plans", []) or []:
        nodes.extend(_walk_nodes(child))
    return nodes


def execution(
    conn,
    query: str,
    *,
    rollback: bool = False,
) -> tuple[float, list[str]]:
    with conn.cursor() as cur:
        if rollback:
            cur.execute("BEGIN")
        try:
            cur.execute(query)
            raw = cur.fetchone()[0]
            report = raw[0] if isinstance(raw, list) else raw
            result = (
                float(report["Execution Time"]),
                _walk_nodes(report["Plan"]),
            )
        finally:
            if rollback:
                cur.execute("ROLLBACK")
    return result


def median_ms(
    conn,
    query: str,
    *,
    rounds: int = 5,
    warmups: int = 1,
    rollback: bool = False,
) -> float:
    for _ in range(warmups):
        execution(conn, query, rollback=rollback)
    values = [
        execution(conn, query, rollback=rollback)[0]
        for _ in range(rounds)
    ]
    return round(statistics.median(values), 3)


def assert_plan_contains(
    conn,
    query: str,
    expected: set[str],
    label: str,
) -> None:
    _, nodes = execution(conn, query)
    if not expected.intersection(nodes):
        raise RuntimeError(
            f"{label} expected one of {sorted(expected)}, "
            f"got plan nodes={nodes}"
        )


def assert_effect(
    *,
    baseline_ms: float,
    candidate_ms: float,
    ratio: float,
    min_delta_ms: float,
    label: str,
) -> None:
    if (
        candidate_ms < baseline_ms * ratio
        or candidate_ms - baseline_ms < min_delta_ms
    ):
        raise RuntimeError(
            f"{label} did not clear threshold: "
            f"baseline={baseline_ms:.3f}ms "
            f"candidate={candidate_ms:.3f}ms "
            f"ratio="
            f"{candidate_ms / max(baseline_ms, 1e-9):.3f}x"
        )


def assert_not_regression(
    *,
    baseline_ms: float,
    candidate_ms: float,
    ratio: float,
    min_delta_ms: float,
    label: str,
) -> None:
    is_regression = (
        candidate_ms >= baseline_ms * ratio
        and candidate_ms - baseline_ms >= min_delta_ms
    )
    if is_regression:
        raise RuntimeError(
            f"{label} unexpectedly crossed regression threshold: "
            f"baseline={baseline_ms:.3f}ms "
            f"candidate={candidate_ms:.3f}ms"
        )


def measure_all(conn) -> dict[str, float]:
    return {
        HASH_FP: median_ms(conn, HASH_QUERY),
        INDEX_FP: median_ms(conn, INDEX_QUERY),
        POINT_FP: median_ms(conn, POINT_QUERY),
        AGG_FP: median_ms(conn, AGG_QUERY),
        WRITE_FP: median_ms(
            conn,
            WRITE_QUERY,
            rollback=True,
        ),
    }


def snapshot(
    version: str,
    timings: dict[str, float],
    fingerprints: list[str] | None = None,
) -> dict[str, Any]:
    selected = fingerprints or list(CALLS)
    return {
        "postgres_version": version,
        "queries": [
            {
                "fingerprint": fingerprint,
                "calls": CALLS[fingerprint],
                "mean_ms": timings[fingerprint],
            }
            for fingerprint in selected
        ],
    }


def scenario_coverage() -> dict[str, Any]:
    # These fields are scenario inputs for testing the decision-coverage
    # logic. Real timings come from PostgreSQL; concurrency/bind/background
    # dimensions are not measured by this harness.
    return {
        "workload_volume_pct": 100,
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 95,
    }


def repeated_experiment(
    conn,
    *,
    fingerprint: str,
    factor: str,
    query: str,
    apply_changed: Callable[[], None],
    apply_restored: Callable[[], None],
    rollback: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _ in range(2):
        apply_changed()
        changed = median_ms(
            conn,
            query,
            rounds=3,
            warmups=0,
            rollback=rollback,
        )
        apply_restored()
        restored = median_ms(
            conn,
            query,
            rounds=3,
            warmups=0,
            rollback=rollback,
        )
        rows.append(
            {
                "fingerprint": fingerprint,
                "factor": factor,
                "controlled": True,
                "changed_ms": changed,
                "restored_ms": restored,
            }
        )
    return rows


def build_experiments(conn) -> list[dict[str, Any]]:
    experiments: list[dict[str, Any]] = []

    # Positive isolation for the hash-join regression.
    ensure_lookup_index(conn, False)
    experiments.extend(
        repeated_experiment(
            conn,
            fingerprint=HASH_FP,
            factor="config.enable_hashjoin",
            query=HASH_QUERY,
            apply_changed=lambda: set_hashjoin(conn, False),
            apply_restored=lambda: set_hashjoin(conn, True),
        )
    )

    # Negative control: lookup-index presence should not explain HASH_QUERY.
    set_hashjoin(conn, True)
    experiments.extend(
        repeated_experiment(
            conn,
            fingerprint=HASH_FP,
            factor="schema.lookup_k_index",
            query=HASH_QUERY,
            apply_changed=lambda: ensure_lookup_index(conn, False),
            apply_restored=lambda: ensure_lookup_index(conn, True),
        )
    )

    # Positive isolation for the lookup-index regression.
    set_hashjoin(conn, False)
    experiments.extend(
        repeated_experiment(
            conn,
            fingerprint=INDEX_FP,
            factor="schema.lookup_k_index",
            query=INDEX_QUERY,
            apply_changed=lambda: ensure_lookup_index(conn, False),
            apply_restored=lambda: ensure_lookup_index(conn, True),
        )
    )

    # Negative control: hash-join availability should not explain INDEX_QUERY.
    ensure_lookup_index(conn, True)
    experiments.extend(
        repeated_experiment(
            conn,
            fingerprint=INDEX_FP,
            factor="config.enable_hashjoin",
            query=INDEX_QUERY,
            apply_changed=lambda: set_hashjoin(conn, False),
            apply_restored=lambda: set_hashjoin(conn, True),
        )
    )

    return experiments


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg17", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trial", default="local")
    args = parser.parse_args()

    threshold_ratio = 1.50
    min_delta_ms = 5.0

    with connect(args.pg17) as conn:
        configure_session(conn)
        setup(conn)
        version = server_version(conn)

        # Complete baseline.
        set_hashjoin(conn, True)
        ensure_lookup_index(conn, True)
        assert_plan_contains(
            conn,
            HASH_QUERY,
            {"Hash Join"},
            "workload hash baseline",
        )
        assert_plan_contains(
            conn,
            INDEX_QUERY,
            {
                "Index Scan",
                "Index Only Scan",
                "Bitmap Heap Scan",
            },
            "workload index baseline",
        )
        baseline = measure_all(conn)

        # Complete candidate with two independent planted regressions.
        set_hashjoin(conn, False)
        ensure_lookup_index(conn, False)
        assert_plan_contains(
            conn,
            HASH_QUERY,
            {"Nested Loop"},
            "workload hash candidate",
        )
        assert_plan_contains(
            conn,
            INDEX_QUERY,
            {"Seq Scan"},
            "workload index candidate",
        )
        candidate = measure_all(conn)

        assert_effect(
            baseline_ms=baseline[HASH_FP],
            candidate_ms=candidate[HASH_FP],
            ratio=threshold_ratio,
            min_delta_ms=min_delta_ms,
            label="workload hash-join regression",
        )
        assert_effect(
            baseline_ms=baseline[INDEX_FP],
            candidate_ms=candidate[INDEX_FP],
            ratio=threshold_ratio,
            min_delta_ms=min_delta_ms,
            label="workload index-removal regression",
        )

        for fingerprint in (
            POINT_FP,
            AGG_FP,
            WRITE_FP,
        ):
            assert_not_regression(
                baseline_ms=baseline[fingerprint],
                candidate_ms=candidate[fingerprint],
                ratio=threshold_ratio,
                min_delta_ms=min_delta_ms,
                label=f"stable workload query {fingerprint}",
            )

        experiments = build_experiments(conn)

        complete_case = {
            "id": "real-multi-query-complete-workload",
            "payload": {
                "baseline": snapshot(version, baseline),
                "candidate": snapshot(version, candidate),
                "coverage": scenario_coverage(),
                "environment_diffs": [
                    {
                        "factor": "config.enable_hashjoin",
                        "kind": "setting",
                        "baseline": "on",
                        "candidate": "off",
                    },
                    {
                        "factor": "schema.lookup_k_index",
                        "kind": "schema",
                        "baseline": "present",
                        "candidate": "absent",
                    },
                ],
                "experiments": experiments,
                "thresholds": {
                    "regression_ratio": threshold_ratio,
                    "min_delta_ms": min_delta_ms,
                },
            },
            "oracle": {
                "workload": {
                    "expected_regressions": {
                        HASH_FP: "config.enable_hashjoin",
                        INDEX_FP: "schema.lookup_k_index",
                    },
                    "expected_stable": [
                        POINT_FP,
                        AGG_FP,
                        WRITE_FP,
                    ],
                    "min_observed_workload_overlap_pct": 99.9,
                    "must_not_clear": True,
                }
            },
        }

        # Adversarial partial replay. HASH_QUERY is known by the oracle to
        # regress under enable_hashjoin=off, but it is omitted from the
        # candidate snapshot. Coverage metadata deliberately overclaims 100%.
        # The engine must cap that claim to the 70% observed fingerprint
        # overlap and therefore avoid a clean HIGH-evidence result.
        set_hashjoin(conn, False)
        ensure_lookup_index(conn, True)
        partial_fps = [
            INDEX_FP,
            POINT_FP,
            AGG_FP,
            WRITE_FP,
        ]
        partial_timings = {
            INDEX_FP: median_ms(conn, INDEX_QUERY),
            POINT_FP: median_ms(conn, POINT_QUERY),
            AGG_FP: median_ms(conn, AGG_QUERY),
            WRITE_FP: median_ms(
                conn,
                WRITE_QUERY,
                rollback=True,
            ),
        }

        for fingerprint in partial_fps:
            assert_not_regression(
                baseline_ms=baseline[fingerprint],
                candidate_ms=partial_timings[fingerprint],
                ratio=threshold_ratio,
                min_delta_ms=min_delta_ms,
                label=f"partial replay stable query {fingerprint}",
            )

        partial_case = {
            "id": "real-hidden-regression-partial-workload",
            "payload": {
                "baseline": snapshot(version, baseline),
                "candidate": snapshot(
                    version,
                    partial_timings,
                    partial_fps,
                ),
                "coverage": scenario_coverage(),
                "environment_diffs": [
                    {
                        "factor": "config.enable_hashjoin",
                        "kind": "setting",
                        "baseline": "on",
                        "candidate": "off",
                    }
                ],
                "experiments": [],
                "thresholds": {
                    "regression_ratio": threshold_ratio,
                    "min_delta_ms": min_delta_ms,
                },
            },
            "oracle": {
                "workload": {
                    "expected_regressions": {},
                    "expected_stable": partial_fps,
                    "hidden_regressions": [HASH_FP],
                    "max_observed_workload_overlap_pct": 70.1,
                    "max_evidence_strength": "MEDIUM",
                    "must_not_clear": True,
                }
            },
        }

        suite = {
            "name": (
                "real-postgresql-workload-safety-v0.7-"
                f"trial-{args.trial}"
            ),
            "metadata": {
                "trial": args.trial,
                "postgres_version": version,
                "query_count": len(CALLS),
                "baseline_ms": baseline,
                "candidate_ms": candidate,
                "hidden_case_expected_overlap_pct": 70.0,
            },
            "cases": [
                complete_case,
                partial_case,
            ],
        }

        args.output.write_text(
            json.dumps(
                suite,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        print(
            json.dumps(
                suite["metadata"],
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
