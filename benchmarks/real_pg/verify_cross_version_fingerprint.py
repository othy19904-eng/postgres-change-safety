from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg

from pgchangesafety.snapshot import (
    build_assessment_payload,
    capture_live,
    derive_window_snapshot,
)


def prepare(dsn: str) -> None:
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
                "DROP TABLE IF EXISTS fingerprint_probe"
            )
            cur.execute(
                """
                CREATE TABLE fingerprint_probe (
                    id bigint PRIMARY KEY,
                    customer_id bigint NOT NULL,
                    payload text NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO fingerprint_probe
                    (id, customer_id, payload)
                SELECT
                    g,
                    (g % 1000) + 1,
                    md5(g::text)
                FROM generate_series(1, 25000) AS g
                """
            )
            cur.execute(
                "CREATE INDEX fingerprint_probe_customer_idx "
                "ON fingerprint_probe(customer_id)"
            )
            cur.execute("ANALYZE fingerprint_probe")
            cur.execute("SELECT pg_stat_statements_reset()")
            cur.fetchone()


def exercise(dsn: str) -> None:
    with psycopg.connect(
        dsn,
        autocommit=True,
    ) as conn:
        with conn.cursor() as cur:
            for value in (1, 7, 123, 999):
                cur.execute(
                    """
                    SELECT payload
                    FROM fingerprint_probe
                    WHERE id = %s
                    """,
                    (value,),
                )
                cur.fetchone()

            for customer in (5, 55, 505):
                cur.execute(
                    """
                    SELECT count(*)
                    FROM fingerprint_probe
                    WHERE customer_id = %s
                    """,
                    (customer,),
                )
                cur.fetchone()

            for low in (100, 1000):
                cur.execute(
                    """
                    SELECT sum(length(payload))
                    FROM fingerprint_probe
                    WHERE id BETWEEN %s AND %s
                    """,
                    (low, low + 99),
                )
                cur.fetchone()


def window_for(
    dsn: str,
    *,
    label: str,
    key: str,
) -> dict:
    start = capture_live(
        dsn,
        label=f"{label}-start",
        fingerprint_key=key,
    )
    exercise(dsn)
    end = capture_live(
        dsn,
        label=f"{label}-end",
        fingerprint_key=key,
    )
    return derive_window_snapshot(
        start,
        end,
        label=label,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg14", required=True)
    parser.add_argument("--pg17", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    prepare(args.pg14)
    prepare(args.pg17)

    key = "cross-version-ci-key"
    pg14 = window_for(
        args.pg14,
        label="pg14",
        key=key,
    )
    pg17 = window_for(
        args.pg17,
        label="pg17",
        key=key,
    )

    for snapshot in (pg14, pg17):
        if not snapshot["measurement_window_valid"]:
            raise RuntimeError(
                "measurement window was not valid: "
                + repr(snapshot["window_unknowns"])
            )
        if (
            snapshot["fingerprint_scheme"]
            != "hmac-sha256-normalized-sql-v1"
        ):
            raise RuntimeError(
                "stable normalized-SQL fingerprinting was not used"
            )
        if (
            snapshot.get("fingerprint_cross_version_stable")
            is not True
        ):
            raise RuntimeError(
                "snapshot was not marked cross-version stable"
            )
        if any(
            "query" in row
            for row in snapshot["queries"]
        ):
            raise RuntimeError(
                "query text was persisted despite privacy default"
            )

    fp14 = {
        row["fingerprint"]: row["calls"]
        for row in pg14["queries"]
    }
    fp17 = {
        row["fingerprint"]: row["calls"]
        for row in pg17["queries"]
    }

    if set(fp14) != set(fp17):
        raise RuntimeError(
            "same workload did not produce the same fingerprint "
            "set across PostgreSQL 14 and 17: "
            f"pg14={sorted(fp14)} pg17={sorted(fp17)}"
        )

    if len(fp14) != 3:
        raise RuntimeError(
            "expected exactly three workload query fingerprints, "
            f"got {len(fp14)}"
        )

    if fp14 != fp17:
        raise RuntimeError(
            "same workload produced different call counts across "
            f"versions: pg14={fp14} pg17={fp17}"
        )

    payload = build_assessment_payload(
        pg14,
        pg17,
    )
    unknowns = payload["coverage"].get(
        "additional_unknowns",
        [],
    )
    if any(
        "Cross-version fingerprint stability" in item
        for item in unknowns
    ):
        raise RuntimeError(
            "stable fingerprints were incorrectly marked unknown"
        )

    overlap = payload["coverage"].get(
        "workload_volume_pct"
    )
    if overlap != 100.0:
        raise RuntimeError(
            f"expected 100% cross-version overlap, got {overlap}"
        )

    report = {
        "pg14_version": pg14["postgres_version"],
        "pg17_version": pg17["postgres_version"],
        "fingerprint_scheme": (
            pg14["fingerprint_scheme"]
        ),
        "fingerprints": len(fp14),
        "pg14_total_calls": (
            pg14["window_total_calls"]
        ),
        "pg17_total_calls": (
            pg17["window_total_calls"]
        ),
        "observed_workload_overlap_pct": overlap,
        "query_text_stored": False,
        "cross_version_unknown": False,
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
