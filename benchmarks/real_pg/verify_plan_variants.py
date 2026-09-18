from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg

from pgchangesafety.engine import assess
from pgchangesafety.plan import attach_plan_samples
from pgchangesafety.snapshot import (
    build_assessment_payload,
    canonicalize_sql,
)


QUERY_SMALL = """
SELECT count(payload)
FROM plan_probe
WHERE k BETWEEN 1000 AND 1100
"""

QUERY_LARGE = """
SELECT count(payload)
FROM plan_probe
WHERE k BETWEEN 1000 AND 100000
"""

QUERY_FP = "pgss:plan-variant-probe"


def connect(dsn: str):
    return psycopg.connect(dsn, autocommit=True)


def setup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS plan_probe")
        cur.execute(
            """
            CREATE TABLE plan_probe (
                k bigint NOT NULL,
                payload text NOT NULL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO plan_probe (k, payload)
            SELECT
                g,
                md5(g::text) || md5((g * 31)::text)
            FROM generate_series(1, 200000) AS g
            """
        )
        cur.execute(
            "CREATE INDEX plan_probe_k_idx "
            "ON plan_probe (k)"
        )
        cur.execute("ANALYZE plan_probe")
        cur.execute("SET jit = off")
        cur.execute("SET max_parallel_workers_per_gather = 0")


def set_index_plan(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = off")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_indexscan = on")
        cur.execute("SET enable_indexonlyscan = on")


def set_seq_plan(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SET enable_seqscan = on")
        cur.execute("SET enable_bitmapscan = off")
        cur.execute("SET enable_indexscan = off")
        cur.execute("SET enable_indexonlyscan = off")


def explain(conn, sql: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "EXPLAIN (FORMAT JSON) " + sql
        )
        return cur.fetchone()[0]


def plan_nodes(plan: list[dict]) -> list[str]:
    names: list[str] = []

    def visit(node: dict) -> None:
        names.append(str(node.get("Node Type", "")))
        for child in node.get("Plans", []) or []:
            visit(child)

    visit(plan[0]["Plan"])
    return names


def snapshot(mean_ms: float = 10.0) -> dict:
    return {
        "queries": [
            {
                "fingerprint": QUERY_FP,
                "calls": 100,
                "mean_ms": mean_ms,
            }
        ]
    }


def coverage() -> dict:
    return {
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 100,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if canonicalize_sql(QUERY_SMALL) != canonicalize_sql(QUERY_LARGE):
        raise RuntimeError(
            "parameter-bucket SQL shapes did not normalize identically"
        )

    with connect(args.dsn) as conn:
        setup(conn)

        set_index_plan(conn)
        baseline_small = explain(conn, QUERY_SMALL)
        baseline_large = explain(conn, QUERY_LARGE)

        set_index_plan(conn)
        candidate_small = explain(conn, QUERY_SMALL)

        set_seq_plan(conn)
        candidate_large = explain(conn, QUERY_LARGE)

    for label, plan in (
        ("baseline small", baseline_small),
        ("baseline large", baseline_large),
        ("candidate small", candidate_small),
    ):
        nodes = plan_nodes(plan)
        if not any("Index" in node for node in nodes):
            raise RuntimeError(
                f"{label} plan was not index-based: {nodes}"
            )

    candidate_nodes = plan_nodes(candidate_large)
    if "Seq Scan" not in candidate_nodes:
        raise RuntimeError(
            "candidate large plan was not sequential: "
            f"{candidate_nodes}"
        )

    baseline = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": baseline_small,
                    "sample_count": 5,
                    "parameter_bucket": "narrow",
                },
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": baseline_large,
                    "sample_count": 5,
                    "parameter_bucket": "wide",
                },
            ]
        },
        key="plan-ci-key",
    )
    candidate = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": candidate_small,
                    "sample_count": 2,
                    "parameter_bucket": "narrow",
                },
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": candidate_large,
                    "sample_count": 8,
                    "parameter_bucket": "wide",
                },
            ]
        },
        key="plan-ci-key",
    )

    payload = build_assessment_payload(
        baseline,
        candidate,
        coverage=coverage(),
    )
    result = assess(payload).to_dict()

    if len(result["plan_variant_diffs"]) != 1:
        raise RuntimeError(
            "expected one plan-variant comparison"
        )
    diff = result["plan_variant_diffs"][0]

    if diff["status"] != "VARIANT_SHIFT":
        raise RuntimeError(
            f"expected VARIANT_SHIFT, got {diff}"
        )
    if diff["dominant_plan_changed"] is not True:
        raise RuntimeError(
            "dominant plan did not change"
        )
    if diff["candidate_variant_count"] < 2:
        raise RuntimeError(
            "candidate did not expose multiple plan variants"
        )
    if (
        diff["parameter_sensitivity_status"]
        != "COVERED"
    ):
        raise RuntimeError(
            "parameter sensitivity should be covered"
        )
    if diff["distribution_shift_pct"] < 70.0:
        raise RuntimeError(
            "plan distribution shift was unexpectedly weak"
        )

    # Now deliberately remove bucket labels. Plan structure still changes,
    # but parameter sensitivity must become UNKNOWN and evidence must be capped.
    unbucketed_baseline = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": baseline_small,
                    "sample_count": 5,
                },
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": baseline_large,
                    "sample_count": 5,
                },
            ]
        },
        key="plan-ci-key",
    )
    unbucketed_candidate = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": candidate_small,
                    "sample_count": 2,
                },
                {
                    "query_fingerprint": QUERY_FP,
                    "plan": candidate_large,
                    "sample_count": 8,
                },
            ]
        },
        key="plan-ci-key",
    )
    unknown_payload = build_assessment_payload(
        unbucketed_baseline,
        unbucketed_candidate,
        coverage=coverage(),
    )
    unknown_result = assess(
        unknown_payload
    ).to_dict()

    if unknown_result["evidence_strength"] == "HIGH":
        raise RuntimeError(
            "missing parameter buckets incorrectly allowed HIGH evidence"
        )
    if not any(
        "Parameter sensitivity is unresolved" in item
        for item in unknown_result["known_unknowns"]
    ):
        raise RuntimeError(
            "missing parameter buckets were not surfaced as UNKNOWN"
        )

    report = {
        "baseline_variant_count": (
            diff["baseline_variant_count"]
        ),
        "candidate_variant_count": (
            diff["candidate_variant_count"]
        ),
        "dominant_plan_changed": (
            diff["dominant_plan_changed"]
        ),
        "distribution_shift_pct": (
            diff["distribution_shift_pct"]
        ),
        "parameter_sensitivity_status": (
            diff["parameter_sensitivity_status"]
        ),
        "unbucketed_evidence_strength": (
            unknown_result["evidence_strength"]
        ),
        "raw_plan_persisted": False,
    }

    args.output.write_text(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
