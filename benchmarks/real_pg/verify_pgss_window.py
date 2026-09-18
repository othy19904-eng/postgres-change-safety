from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg

from pgchangesafety.engine import assess
from pgchangesafety.snapshot import (
    build_assessment_payload,
    capture_live,
    derive_window_snapshot,
)


def setup_and_run(dsn: str) -> None:
    with psycopg.connect(
        dsn,
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE EXTENSION IF NOT EXISTS "
                "pg_stat_statements"
            )
            cur.execute(
                "DROP TABLE IF EXISTS pgss_window_probe"
            )
            cur.execute(
                """
                CREATE TABLE pgss_window_probe (
                    id bigint PRIMARY KEY,
                    payload text NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO pgss_window_probe
                    (id, payload)
                SELECT
                    g,
                    md5(g::text)
                FROM generate_series(1, 50000) AS g
                """
            )
            cur.execute(
                "ANALYZE pgss_window_probe"
            )


def exercise_workload(dsn: str) -> None:
    with psycopg.connect(
        dsn,
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            for low in (
                100,
                1000,
                5000,
                10000,
                20000,
            ):
                cur.execute(
                    """
                    SELECT sum(length(payload))
                    FROM pgss_window_probe
                    WHERE id BETWEEN %s AND %s
                    """,
                    (low, low + 999),
                )
                cur.fetchone()

            for _ in range(3):
                cur.execute(
                    """
                    SELECT count(*)
                    FROM pgss_window_probe
                    WHERE payload LIKE 'a%'
                    """
                )
                cur.fetchone()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    setup_and_run(args.dsn)

    key = "ci-window-key"
    start = capture_live(
        args.dsn,
        label="window-start",
        fingerprint_key=key,
    )
    exercise_workload(args.dsn)
    end = capture_live(
        args.dsn,
        label="window-end",
        fingerprint_key=key,
    )

    window = derive_window_snapshot(
        start,
        end,
        label="real-pgss-window",
    )

    if not window["measurement_window_valid"]:
        raise RuntimeError(
            "real pg_stat_statements window was not valid: "
            + repr(window["window_unknowns"])
        )
    if window["window_total_calls"] < 8:
        raise RuntimeError(
            "expected at least eight observed workload calls"
        )
    if not window["queries"]:
        raise RuntimeError(
            "no pg_stat_statements window queries captured"
        )
    if any(
        not str(row["fingerprint"]).startswith("pgss:")
        for row in window["queries"]
    ):
        raise RuntimeError(
            "non-pseudonymous fingerprint escaped capture"
        )
    if any(
        "query" in row
        for row in window["queries"]
    ):
        raise RuntimeError(
            "query text was captured despite privacy default"
        )

    # A verified window can participate in the coverage layer. These
    # scenario fields are supplied only to prove that window integrity no
    # longer blocks evidence; they are not claims about this CI workload.
    coverage = {
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 100,
    }
    payload = build_assessment_payload(
        window,
        window,
        coverage=coverage,
    )
    result = assess(payload).to_dict()

    if result["evidence_strength"] != "HIGH":
        raise RuntimeError(
            "verified window unexpectedly blocked HIGH evidence: "
            + repr(result["known_unknowns"])
        )

    report = {
        "window": window,
        "assessment": result,
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
            {
                "measurement_window_valid": (
                    window["measurement_window_valid"]
                ),
                "window_total_calls": (
                    window["window_total_calls"]
                ),
                "fingerprints": len(
                    window["queries"]
                ),
                "query_text_stored": any(
                    "query" in row
                    for row in window["queries"]
                ),
                "evidence_strength": (
                    result["evidence_strength"]
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
