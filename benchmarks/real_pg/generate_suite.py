from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

import psycopg


HASH_FINGERPRINT = "real:large-equality-join"
INDEX_FINGERPRINT = "real:selective-index-lookup"

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
WHERE k BETWEEN 123456 AND 123555
"""


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def configure_session(conn) -> None:
    # Reduce runner noise. These settings are identical on both sides and are
    # not the planted factors under test.
    with conn.cursor() as cur:
        cur.execute("SET jit = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")


def setup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_left")
        cur.execute("DROP TABLE IF EXISTS bench_right")
        cur.execute("DROP TABLE IF EXISTS bench_lookup")

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
            INSERT INTO bench_left (k, payload)
            SELECT
                g,
                md5(g::text) || md5((g * 17)::text)
            FROM generate_series(1, 250000) AS g
        """)
        cur.execute("""
            INSERT INTO bench_right (k, payload)
            SELECT
                g,
                md5((g * 97)::text) || md5((g * 193)::text)
            FROM generate_series(1, 250000) AS g
        """)
        cur.execute("""
            INSERT INTO bench_lookup (k, payload)
            SELECT
                g,
                md5((g * 31)::text) || md5((g * 73)::text)
            FROM generate_series(1, 300000) AS g
        """)
        cur.execute("CREATE INDEX bench_lookup_k_idx ON bench_lookup (k)")

        cur.execute("ANALYZE bench_left")
        cur.execute("ANALYZE bench_right")
        cur.execute("ANALYZE bench_lookup")


def set_hashjoin(conn, enabled: bool) -> None:
    value = "on" if enabled else "off"
    with conn.cursor() as cur:
        cur.execute(f"SET enable_hashjoin = {value}")


def ensure_lookup_index(conn, present: bool) -> None:
    with conn.cursor() as cur:
        if present:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS bench_lookup_k_idx "
                "ON bench_lookup (k)"
            )
        else:
            cur.execute("DROP INDEX IF EXISTS bench_lookup_k_idx")


def server_version(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        return str(cur.fetchone()[0])


def _walk_nodes(plan: dict[str, Any]) -> list[str]:
    names = [str(plan.get("Node Type", ""))]
    for child in plan.get("Plans", []) or []:
        names.extend(_walk_nodes(child))
    return names


def execution(conn, query: str) -> tuple[float, list[str]]:
    with conn.cursor() as cur:
        cur.execute(query)
        raw = cur.fetchone()[0]
        report = raw[0] if isinstance(raw, list) else raw
        return (
            float(report["Execution Time"]),
            _walk_nodes(report["Plan"]),
        )


def median_ms(
    conn,
    query: str,
    *,
    rounds: int = 5,
    warmups: int = 1,
) -> float:
    for _ in range(warmups):
        execution(conn, query)
    values = [execution(conn, query)[0] for _ in range(rounds)]
    return round(statistics.median(values), 3)


def assert_plan_contains(conn, query: str, expected: set[str], label: str) -> None:
    _, nodes = execution(conn, query)
    if not expected.intersection(nodes):
        raise RuntimeError(
            f"{label} did not use an expected plan node. "
            f"expected one of {sorted(expected)}, got {nodes}"
        )


def assert_plan_excludes(conn, query: str, forbidden: set[str], label: str) -> None:
    _, nodes = execution(conn, query)
    found = forbidden.intersection(nodes)
    if found:
        raise RuntimeError(
            f"{label} unexpectedly used forbidden nodes {sorted(found)}; "
            f"plan nodes={nodes}"
        )


def coverage(environment_match: float) -> dict:
    return {
        "workload_volume_pct": 100,
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": environment_match,
    }


def snapshot(
    version: str,
    fingerprint: str,
    mean_ms: float,
    calls: int = 100,
) -> dict:
    return {
        "postgres_version": version,
        "queries": [
            {
                "fingerprint": fingerprint,
                "calls": calls,
                "mean_ms": mean_ms,
            }
        ],
    }


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
        or (candidate_ms - baseline_ms) < min_delta_ms
    ):
        raise RuntimeError(
            f"{label} did not clear the benchmark threshold: "
            f"baseline={baseline_ms:.3f}ms "
            f"candidate={candidate_ms:.3f}ms "
            f"ratio={candidate_ms / max(baseline_ms, 1e-9):.3f}x"
        )


def repeated_hashjoin_experiments(conn) -> list[dict]:
    experiments = []
    for _ in range(2):
        set_hashjoin(conn, False)
        changed = median_ms(conn, HASH_QUERY, rounds=3, warmups=0)
        set_hashjoin(conn, True)
        restored = median_ms(conn, HASH_QUERY, rounds=3, warmups=0)
        experiments.append(
            {
                "fingerprint": HASH_FINGERPRINT,
                "factor": "config.enable_hashjoin",
                "controlled": True,
                "changed_ms": changed,
                "restored_ms": restored,
            }
        )

    changed_med = statistics.median(x["changed_ms"] for x in experiments)
    restored_med = statistics.median(x["restored_ms"] for x in experiments)
    assert_effect(
        baseline_ms=restored_med,
        candidate_ms=changed_med,
        ratio=1.20,
        min_delta_ms=5,
        label="Repeated enable_hashjoin plant",
    )
    return experiments


def repeated_index_experiments(conn) -> list[dict]:
    experiments = []
    for _ in range(2):
        ensure_lookup_index(conn, False)
        changed = median_ms(conn, INDEX_QUERY, rounds=3, warmups=0)

        ensure_lookup_index(conn, True)
        restored = median_ms(conn, INDEX_QUERY, rounds=3, warmups=0)

        experiments.append(
            {
                "fingerprint": INDEX_FINGERPRINT,
                "factor": "schema.lookup_k_index",
                "controlled": True,
                "changed_ms": changed,
                "restored_ms": restored,
            }
        )

    changed_med = statistics.median(x["changed_ms"] for x in experiments)
    restored_med = statistics.median(x["restored_ms"] for x in experiments)
    assert_effect(
        baseline_ms=restored_med,
        candidate_ms=changed_med,
        ratio=1.50,
        min_delta_ms=3,
        label="Repeated lookup-index removal plant",
    )
    return experiments


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg14", required=True)
    parser.add_argument("--pg17", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trial", default="local")
    args = parser.parse_args()

    with connect(args.pg14) as pg14, connect(args.pg17) as pg17:
        configure_session(pg14)
        configure_session(pg17)
        setup(pg14)
        setup(pg17)

        v14 = server_version(pg14)
        v17 = server_version(pg17)

        # Case 1: planner-method regression on one PostgreSQL version.
        set_hashjoin(pg17, True)
        assert_plan_contains(
            pg17,
            HASH_QUERY,
            {"Hash Join"},
            "hashjoin baseline",
        )
        baseline17_hash = median_ms(pg17, HASH_QUERY)

        set_hashjoin(pg17, False)
        assert_plan_excludes(
            pg17,
            HASH_QUERY,
            {"Hash Join"},
            "hashjoin candidate",
        )
        candidate17_hash = median_ms(pg17, HASH_QUERY)
        assert_effect(
            baseline_ms=baseline17_hash,
            candidate_ms=candidate17_hash,
            ratio=1.25,
            min_delta_ms=5,
            label="Real enable_hashjoin regression",
        )

        hash_experiments = repeated_hashjoin_experiments(pg17)

        hash_case = {
            "id": "real-enable-hashjoin-regression",
            "payload": {
                "baseline": snapshot(
                    v17, HASH_FINGERPRINT, baseline17_hash
                ),
                "candidate": snapshot(
                    v17, HASH_FINGERPRINT, candidate17_hash
                ),
                "coverage": coverage(95),
                "environment_diffs": [
                    {
                        "factor": "config.enable_hashjoin",
                        "kind": "setting",
                        "baseline": "on",
                        "candidate": "off",
                    }
                ],
                "experiments": hash_experiments,
                "thresholds": {
                    "regression_ratio": 1.25,
                    "min_delta_ms": 5,
                },
            },
            "oracle": {
                "fingerprint": HASH_FINGERPRINT,
                "regression": True,
                "cause": "config.enable_hashjoin",
            },
        }

        # Case 2: schema/index regression, a distinct failure mechanism.
        ensure_lookup_index(pg17, True)
        assert_plan_contains(
            pg17,
            INDEX_QUERY,
            {"Index Scan", "Index Only Scan", "Bitmap Heap Scan"},
            "index baseline",
        )
        baseline17_index = median_ms(pg17, INDEX_QUERY)

        ensure_lookup_index(pg17, False)
        assert_plan_contains(
            pg17,
            INDEX_QUERY,
            {"Seq Scan"},
            "index candidate",
        )
        candidate17_index = median_ms(pg17, INDEX_QUERY)
        assert_effect(
            baseline_ms=baseline17_index,
            candidate_ms=candidate17_index,
            ratio=1.50,
            min_delta_ms=3,
            label="Real lookup-index removal regression",
        )

        index_experiments = repeated_index_experiments(pg17)

        index_case = {
            "id": "real-lookup-index-removal-regression",
            "payload": {
                "baseline": snapshot(
                    v17, INDEX_FINGERPRINT, baseline17_index
                ),
                "candidate": snapshot(
                    v17, INDEX_FINGERPRINT, candidate17_index
                ),
                "coverage": coverage(95),
                "environment_diffs": [
                    {
                        "factor": "schema.lookup_k_index",
                        "kind": "schema",
                        "baseline": "present",
                        "candidate": "absent",
                    }
                ],
                "experiments": index_experiments,
                "thresholds": {
                    "regression_ratio": 1.50,
                    "min_delta_ms": 3,
                },
            },
            "oracle": {
                "fingerprint": INDEX_FINGERPRINT,
                "regression": True,
                "cause": "schema.lookup_k_index",
            },
        }

        # Case 3: version and config move together. Hash-join availability is
        # isolated, but the version itself is not, so attribution must abstain.
        set_hashjoin(pg14, True)
        baseline14_hash = median_ms(pg14, HASH_QUERY)

        set_hashjoin(pg17, False)
        candidate17_confounded = median_ms(pg17, HASH_QUERY)
        assert_effect(
            baseline_ms=baseline14_hash,
            candidate_ms=candidate17_confounded,
            ratio=1.25,
            min_delta_ms=5,
            label="Version+config confounded regression",
        )

        confounded_case = {
            "id": "real-version-plus-config-confounder",
            "payload": {
                "baseline": snapshot(
                    v14, HASH_FINGERPRINT, baseline14_hash
                ),
                "candidate": snapshot(
                    v17, HASH_FINGERPRINT, candidate17_confounded
                ),
                "coverage": coverage(85),
                "environment_diffs": [
                    {
                        "factor": "postgres.version",
                        "kind": "postgres_version",
                        "baseline": v14,
                        "candidate": v17,
                    },
                    {
                        "factor": "config.enable_hashjoin",
                        "kind": "setting",
                        "baseline": "on",
                        "candidate": "off",
                    },
                ],
                "experiments": hash_experiments,
                "thresholds": {
                    "regression_ratio": 1.25,
                    "min_delta_ms": 5,
                },
            },
            "oracle": {
                "fingerprint": HASH_FINGERPRINT,
                "regression": True,
                "cause": "UNKNOWN",
            },
        }

        # Case 4: negative control.
        set_hashjoin(pg17, True)
        stable_a = median_ms(pg17, HASH_QUERY)
        stable_b = median_ms(pg17, HASH_QUERY)

        no_regression_case = {
            "id": "real-stable-control",
            "payload": {
                "baseline": snapshot(v17, HASH_FINGERPRINT, stable_a),
                "candidate": snapshot(v17, HASH_FINGERPRINT, stable_b),
                "coverage": coverage(100),
                "environment_diffs": [],
                "experiments": [],
                "thresholds": {
                    "regression_ratio": 1.35,
                    "min_delta_ms": 10,
                },
            },
            "oracle": {
                "fingerprint": HASH_FINGERPRINT,
                "regression": False,
            },
        }

        suite = {
            "name": (
                "real-postgresql-planted-regressions-v2-"
                f"trial-{args.trial}"
            ),
            "metadata": {
                "trial": args.trial,
                "pg14_version": v14,
                "pg17_version": v17,
                "hashjoin": {
                    "baseline_ms": baseline17_hash,
                    "candidate_ms": candidate17_hash,
                    "effect_ratio": round(
                        candidate17_hash / baseline17_hash, 3
                    ),
                },
                "index_removal": {
                    "baseline_ms": baseline17_index,
                    "candidate_ms": candidate17_index,
                    "effect_ratio": round(
                        candidate17_index / baseline17_index, 3
                    ),
                },
                "version_confounded": {
                    "baseline_ms": baseline14_hash,
                    "candidate_ms": candidate17_confounded,
                    "effect_ratio": round(
                        candidate17_confounded / baseline14_hash, 3
                    ),
                },
                "stable_control": {
                    "a_ms": stable_a,
                    "b_ms": stable_b,
                    "ratio": round(
                        max(stable_a, stable_b)
                        / max(min(stable_a, stable_b), 1e-9),
                        3,
                    ),
                },
            },
            "cases": [
                hash_case,
                index_case,
                confounded_case,
                no_regression_case,
            ],
        }

        args.output.write_text(
            json.dumps(suite, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(suite["metadata"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
