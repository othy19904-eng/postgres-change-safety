from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import psycopg


FINGERPRINT = "real:large-equality-join"

QUERY = """
EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS OFF)
SELECT count(*)
FROM bench_left AS l
JOIN bench_right AS r
  ON r.k = l.k
"""


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def setup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS bench_left")
        cur.execute("DROP TABLE IF EXISTS bench_right")
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
        cur.execute("ANALYZE bench_left")
        cur.execute("ANALYZE bench_right")


def set_hashjoin(conn, enabled: bool) -> None:
    with conn.cursor() as cur:
        cur.execute("SET enable_hashjoin = %s", ("on" if enabled else "off",))


def server_version(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
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
                "fingerprint": FINGERPRINT,
                "calls": calls,
                "mean_ms": mean_ms,
            }
        ],
    }


def repeated_hashjoin_experiments(conn) -> list[dict]:
    experiments = []
    for _ in range(2):
        set_hashjoin(conn, False)
        changed = median_ms(conn, rounds=3, warmups=0)
        set_hashjoin(conn, True)
        restored = median_ms(conn, rounds=3, warmups=0)
        experiments.append(
            {
                "fingerprint": FINGERPRINT,
                "factor": "config.enable_hashjoin",
                "controlled": True,
                "changed_ms": changed,
                "restored_ms": restored,
            }
        )

    changed_med = statistics.median(x["changed_ms"] for x in experiments)
    restored_med = statistics.median(x["restored_ms"] for x in experiments)
    if changed_med <= restored_med * 1.20:
        raise RuntimeError(
            "Planted enable_hashjoin regression was too weak in repeated trials: "
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

        # Case 1: same PostgreSQL version. Only hash join availability changes.
        set_hashjoin(pg17, True)
        baseline17 = median_ms(pg17)

        set_hashjoin(pg17, False)
        candidate17 = median_ms(pg17)

        if candidate17 < baseline17 * 1.25 or (candidate17 - baseline17) < 5:
            raise RuntimeError(
                "Real PostgreSQL enable_hashjoin regression did not clear the "
                "engine threshold: "
                f"baseline={baseline17:.3f}ms candidate={candidate17:.3f}ms"
            )

        experiments = repeated_hashjoin_experiments(pg17)

        config_case = {
            "id": "real-enable-hashjoin-regression",
            "payload": {
                "baseline": snapshot(v17, baseline17),
                "candidate": snapshot(v17, candidate17),
                "coverage": coverage(95),
                "environment_diffs": [
                    {
                        "factor": "config.enable_hashjoin",
                        "kind": "setting",
                        "baseline": "on",
                        "candidate": "off",
                    }
                ],
                "experiments": experiments,
                "thresholds": {"regression_ratio": 1.25, "min_delta_ms": 5},
            },
            "oracle": {
                "fingerprint": FINGERPRINT,
                "regression": True,
                "cause": "config.enable_hashjoin",
            },
        }

        # Case 2: both version and config change. We isolate hashjoin only.
        # Because the version is not independently tested, the correct causal
        # output is UNKNOWN even if the slowdown is real.
        set_hashjoin(pg14, True)
        baseline14 = median_ms(pg14)

        set_hashjoin(pg17, False)
        candidate17_for_version = median_ms(pg17)

        if (
            candidate17_for_version < baseline14 * 1.25
            or (candidate17_for_version - baseline14) < 5
        ):
            raise RuntimeError(
                "Version+config real case did not produce a measurable regression: "
                f"pg14={baseline14:.3f}ms "
                f"pg17-hashjoin-off={candidate17_for_version:.3f}ms"
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
                        "factor": "config.enable_hashjoin",
                        "kind": "setting",
                        "baseline": "on",
                        "candidate": "off",
                    },
                ],
                "experiments": experiments,
                "thresholds": {"regression_ratio": 1.25, "min_delta_ms": 5},
            },
            "oracle": {
                "fingerprint": FINGERPRINT,
                "regression": True,
                "cause": "UNKNOWN",
            },
        }

        # Case 3: same server and same config measured twice.
        set_hashjoin(pg17, True)
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
                "fingerprint": FINGERPRINT,
                "regression": False,
            },
        }

        suite = {
            "name": "real-postgresql-planted-regressions-v1",
            "metadata": {
                "pg14_version": v14,
                "pg17_version": v17,
                "baseline17_hashjoin_on_ms": baseline17,
                "candidate17_hashjoin_off_ms": candidate17,
                "baseline14_hashjoin_on_ms": baseline14,
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
