from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import psycopg


QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
SELECT array_length(array_agg(id ORDER BY payload), 1)
FROM bench_sort
"""


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def setup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_sort")
        cur.execute("""
            CREATE TABLE bench_sort (
                id bigint PRIMARY KEY,
                payload text NOT NULL
            )
        """)
        cur.execute("""
            INSERT INTO bench_sort (id, payload)
            SELECT
                g,
                md5(g::text) || md5((g * 17)::text) ||
                md5((g * 97)::text) || md5((g * 193)::text)
            FROM generate_series(1, 180000) AS g
        """)
        cur.execute("ANALYZE bench_sort")


def set_work_mem(conn, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"SET work_mem = '{value}'")


def server_version(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        return str(cur.fetchone()[0])


def work_mem(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW work_mem")
        return str(cur.fetchone()[0])


def execution_ms(conn) -> float:
    with conn.cursor() as cur:
        cur.execute(QUERY)
        raw = cur.fetchone()[0]
        plan = raw[0] if isinstance(raw, list) else raw
        return float(plan["Execution Time"])


def median_ms(conn, *, rounds: int = 5, warmups: int = 1) -> float:
    for _ in range(warmups):
        execution_ms(conn)
    values = [execution_ms(conn) for _ in range(rounds)]
    return round(statistics.median(values), 3)


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


def snapshot(version: str, mean_ms: float, calls: int = 100) -> dict:
    return {
        "postgres_version": version,
        "queries": [
            {
                "fingerprint": "real:ordered-array-aggregate",
                "calls": calls,
                "mean_ms": mean_ms,
            }
        ],
    }


def repeated_work_mem_experiments(conn, candidate_ms: float, baseline_ms: float) -> list[dict]:
    experiments = []
    for _ in range(2):
        set_work_mem(conn, "64kB")
        changed = median_ms(conn, rounds=3, warmups=0)
        set_work_mem(conn, "128MB")
        restored = median_ms(conn, rounds=3, warmups=0)
        experiments.append(
            {
                "fingerprint": "real:ordered-array-aggregate",
                "factor": "config.work_mem",
                "controlled": True,
                "changed_ms": changed,
                "restored_ms": restored,
            }
        )

    # Sanity check: the experiments themselves should broadly reproduce
    # the measured candidate/baseline relationship.
    changed_med = statistics.median(x["changed_ms"] for x in experiments)
    restored_med = statistics.median(x["restored_ms"] for x in experiments)
    if changed_med <= restored_med * 1.20:
        raise RuntimeError(
            "Planted work_mem regression was too weak in repeated trials: "
            f"changed={changed_med:.3f}ms restored={restored_med:.3f}ms"
        )

    return experiments


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg14", required=True)
    parser.add_argument("--pg17", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    with connect(args.pg14) as pg14, connect(args.pg17) as pg17:
        setup(pg14)
        setup(pg17)

        v14 = server_version(pg14)
        v17 = server_version(pg17)

        # Real causal case: same PostgreSQL version, one planted config change.
        set_work_mem(pg17, "128MB")
        baseline17 = median_ms(pg17)

        set_work_mem(pg17, "64kB")
        candidate17 = median_ms(pg17)

        if candidate17 < baseline17 * 1.25 or (candidate17 - baseline17) < 5:
            raise RuntimeError(
                "Real PostgreSQL work_mem regression did not clear the engine threshold: "
                f"baseline={baseline17:.3f}ms candidate={candidate17:.3f}ms"
            )

        experiments = repeated_work_mem_experiments(
            pg17,
            candidate_ms=candidate17,
            baseline_ms=baseline17,
        )

        config_case = {
            "id": "real-work-mem-regression",
            "payload": {
                "baseline": snapshot(v17, baseline17),
                "candidate": snapshot(v17, candidate17),
                "coverage": coverage(100),
                "environment_diffs": [
                    {
                        "factor": "config.work_mem",
                        "kind": "setting",
                        "baseline": "128MB",
                        "candidate": "64kB",
                    }
                ],
                "experiments": experiments,
                "thresholds": {"regression_ratio": 1.25, "min_delta_ms": 5},
            },
            "oracle": {
                "fingerprint": "real:ordered-array-aggregate",
                "regression": True,
                "cause": "config.work_mem",
            },
        }

        # Real confounder case: version and work_mem both changed.
        # We only isolate work_mem, so version remains unresolved by design.
        set_work_mem(pg14, "128MB")
        baseline14 = median_ms(pg14)

        set_work_mem(pg17, "64kB")
        candidate17_for_version = median_ms(pg17)

        if (
            candidate17_for_version < baseline14 * 1.25
            or (candidate17_for_version - baseline14) < 5
        ):
            raise RuntimeError(
                "Version+config real case did not produce a measurable regression: "
                f"pg14={baseline14:.3f}ms pg17-low-work-mem={candidate17_for_version:.3f}ms"
            )

        confounded_case = {
            "id": "real-version-plus-config-confounder",
            "payload": {
                "baseline": snapshot(v14, baseline14),
                "candidate": snapshot(v17, candidate17_for_version),
                "coverage": coverage(85),
                "environment_diffs": [
                    {
                        "factor": "postgres.version",
                        "kind": "postgres_version",
                        "baseline": v14,
                        "candidate": v17,
                    },
                    {
                        "factor": "config.work_mem",
                        "kind": "setting",
                        "baseline": "128MB",
                        "candidate": "64kB",
                    },
                ],
                "experiments": experiments,
                "thresholds": {"regression_ratio": 1.25, "min_delta_ms": 5},
            },
            "oracle": {
                "fingerprint": "real:ordered-array-aggregate",
                "regression": True,
                "cause": "UNKNOWN",
            },
        }

        # Real negative case: same server/config measured twice.
        set_work_mem(pg17, "128MB")
        stable_a = median_ms(pg17)
        stable_b = median_ms(pg17)

        no_regression_case = {
            "id": "real-stable-control",
            "payload": {
                "baseline": snapshot(v17, stable_a),
                "candidate": snapshot(v17, stable_b),
                "coverage": coverage(100),
                "environment_diffs": [],
                "experiments": [],
                "thresholds": {"regression_ratio": 1.35, "min_delta_ms": 10},
            },
            "oracle": {
                "fingerprint": "real:ordered-array-aggregate",
                "regression": False,
            },
        }

        suite = {
            "name": "real-postgresql-planted-regressions-v1",
            "metadata": {
                "pg14_version": v14,
                "pg17_version": v17,
                "baseline17_ms": baseline17,
                "candidate17_low_work_mem_ms": candidate17,
                "baseline14_ms": baseline14,
                "candidate17_confounded_ms": candidate17_for_version,
                "stable_control_a_ms": stable_a,
                "stable_control_b_ms": stable_b,
            },
            "cases": [
                config_case,
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
