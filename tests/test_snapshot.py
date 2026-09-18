from pathlib import Path

from pgchangesafety.engine import assess
from pgchangesafety.snapshot import (
    build_assessment_payload,
    canonicalize_sql,
    derive_environment_diffs,
    derive_window_snapshot,
    import_pgss_csv,
)


def test_import_pgss_csv(tmp_path: Path):
    csv_file = tmp_path / "pgss.csv"
    csv_file.write_text(
        "queryid,calls,total_exec_time,mean_exec_time,rows\n"
        "101,10,150,15,20\n",
        encoding="utf-8",
    )
    snapshot = import_pgss_csv(
        csv_file,
        label="pg14",
        raw_queryid=True,
    )

    assert snapshot["label"] == "pg14"
    assert snapshot["queries"][0]["fingerprint"] == "queryid:101"
    assert snapshot["queries"][0]["mean_ms"] == 15.0


def test_build_payload_derives_overlap_but_keeps_other_unknowns():
    baseline = {
        "queries": [
            {"fingerprint": "queryid:1", "calls": 80, "mean_ms": 10},
            {"fingerprint": "queryid:2", "calls": 20, "mean_ms": 5},
        ]
    }
    candidate = {
        "queries": [
            {"fingerprint": "queryid:1", "calls": 75, "mean_ms": 20},
        ]
    }

    payload = build_assessment_payload(baseline, candidate)

    assert payload["coverage"]["workload_volume_pct"] == 80.0
    assert payload["coverage"]["peak_concurrency_covered"] is None

    result = assess(payload)
    assert len(result.regressions) == 1
    assert result.regressions[0].fingerprint == "queryid:1"
    assert result.regressions[0].cause == "UNKNOWN"
    assert result.evidence_strength == "LOW"
    assert "Peak-concurrency coverage is unknown" in result.known_unknowns


def test_environment_diffs_capture_version_and_settings():
    baseline = {
        "postgres_version": "14.20",
        "settings": {
            "random_page_cost": "4",
            "work_mem": "4096",
        },
        "queries": [],
    }
    candidate = {
        "postgres_version": "17.6",
        "settings": {
            "random_page_cost": "1.1",
            "work_mem": "4096",
        },
        "queries": [],
    }

    diffs = derive_environment_diffs(baseline, candidate)
    factors = {item["factor"] for item in diffs}

    assert "postgres.version" in factors
    assert "config.random_page_cost" in factors
    assert "config.work_mem" not in factors


def test_explicit_coverage_can_raise_strength_when_cause_isolated():
    baseline = {
        "postgres_version": "14",
        "fingerprint_cross_version_stable": True,
        "queries": [
            {"fingerprint": "queryid:1", "calls": 100, "mean_ms": 10},
        ],
    }
    candidate = {
        "postgres_version": "17",
        "fingerprint_cross_version_stable": True,
        "queries": [
            {"fingerprint": "queryid:1", "calls": 100, "mean_ms": 20},
        ],
    }
    coverage = {
        "bind_value_diversity_pct": 95,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 100,
    }
    experiments = [
        {
            "fingerprint": "queryid:1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 20,
            "restored_ms": 10,
        },
        {
            "fingerprint": "queryid:1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 20.2,
            "restored_ms": 10.1,
        },
    ]

    payload = build_assessment_payload(
        baseline,
        candidate,
        coverage=coverage,
        experiments=experiments,
    )
    result = assess(payload)

    assert result.regressions[0].cause == "postgres.version"
    assert result.evidence_strength == "HIGH"



def test_import_pgss_is_pseudonymous_by_default(tmp_path: Path):
    csv_file = tmp_path / "pgss.csv"
    csv_file.write_text(
        "queryid,calls,total_exec_time,mean_exec_time,rows\n"
        "101,10,150,15,20\n",
        encoding="utf-8",
    )

    snapshot = import_pgss_csv(
        csv_file,
        fingerprint_key="same-secret",
    )

    fingerprint = snapshot["queries"][0]["fingerprint"]
    assert fingerprint.startswith("pgss:")
    assert "101" not in fingerprint
    assert snapshot["fingerprint_scheme"] == "hmac-sha256-queryid-v1"
    assert snapshot["fingerprint_key_id"] != "none"


def test_window_derives_only_counter_deltas():
    start = {
        "captured_at": "2026-09-18T08:00:00+00:00",
        "postgres_version": "17.6",
        "source": "pg_stat_statements_live",
        "stats_reset": "2026-09-18T07:00:00+00:00",
        "capture_self_tracking_disabled": True,
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "settings": {"work_mem": "4096"},
        "queries": [
            {
                "fingerprint": "pgss:a",
                "calls": 100,
                "total_exec_time": 1000,
                "mean_ms": 10,
                "rows": 500,
            }
        ],
    }
    end = {
        "captured_at": "2026-09-18T08:05:00+00:00",
        "postgres_version": "17.6",
        "source": "pg_stat_statements_live",
        "stats_reset": "2026-09-18T07:00:00+00:00",
        "capture_self_tracking_disabled": True,
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "settings": {"work_mem": "4096"},
        "queries": [
            {
                "fingerprint": "pgss:a",
                "calls": 140,
                "total_exec_time": 1800,
                "mean_ms": 12.857,
                "rows": 700,
            },
            {
                "fingerprint": "pgss:b",
                "calls": 10,
                "total_exec_time": 500,
                "mean_ms": 50,
                "rows": 10,
            },
        ],
    }

    window = derive_window_snapshot(
        start,
        end,
        label="baseline-window",
    )

    rows = {
        row["fingerprint"]: row
        for row in window["queries"]
    }
    assert window["measurement_window_valid"] is True
    assert window["window_total_calls"] == 50.0
    assert rows["pgss:a"]["calls"] == 40.0
    assert rows["pgss:a"]["mean_ms"] == 20.0
    assert rows["pgss:b"]["calls"] == 10.0
    assert rows["pgss:b"]["mean_ms"] == 50.0


def test_window_rejects_pgss_reset():
    start = {
        "stats_reset": "2026-09-18T07:00:00+00:00",
        "capture_self_tracking_disabled": True,
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "queries": [],
    }
    end = {
        "stats_reset": "2026-09-18T08:00:00+00:00",
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "queries": [],
    }

    try:
        derive_window_snapshot(start, end)
    except ValueError as exc:
        assert "reset during" in str(exc)
    else:
        raise AssertionError("expected reset to invalidate the window")


def test_different_fingerprint_keys_cannot_be_compared():
    baseline = {
        "fingerprint_scheme": "hmac-sha256-queryid-v1",
        "fingerprint_key_id": "key-a",
        "queries": [],
    }
    candidate = {
        "fingerprint_scheme": "hmac-sha256-queryid-v1",
        "fingerprint_key_id": "key-b",
        "queries": [],
    }

    try:
        build_assessment_payload(
            baseline,
            candidate,
        )
    except ValueError as exc:
        assert "different fingerprint keys" in str(exc)
    else:
        raise AssertionError("expected incompatible pseudonym keys")


def test_cumulative_pgss_snapshots_cannot_reach_high_evidence():
    baseline = {
        "source": "pg_stat_statements_live",
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "queries": [
            {
                "fingerprint": "pgss:a",
                "calls": 100,
                "mean_ms": 10,
            }
        ],
    }
    candidate = {
        "source": "pg_stat_statements_live",
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "queries": [
            {
                "fingerprint": "pgss:a",
                "calls": 100,
                "mean_ms": 20,
            }
        ],
    }
    coverage = {
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 100,
    }

    payload = build_assessment_payload(
        baseline,
        candidate,
        coverage=coverage,
    )
    result = assess(payload)

    assert result.evidence_strength != "HIGH"
    assert any(
        "measurement windows are not verified" in item
        for item in result.known_unknowns
    )



def test_window_marks_capture_self_tracking_as_unknown():
    start = {
        "captured_at": "2026-09-18T08:00:00+00:00",
        "stats_reset": "2026-09-18T07:00:00+00:00",
        "capture_self_tracking_disabled": False,
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "queries": [],
    }
    end = {
        "captured_at": "2026-09-18T08:05:00+00:00",
        "stats_reset": "2026-09-18T07:00:00+00:00",
        "capture_self_tracking_disabled": False,
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "queries": [],
    }

    window = derive_window_snapshot(start, end)

    assert window["measurement_window_valid"] is False
    assert any(
        "Capture-session queries may be present" in item
        for item in window["window_unknowns"]
    )



def test_sql_canonicalization_ignores_literals_comments_and_spacing():
    first = """
        SELECT /* checkout */ total
        FROM orders
        WHERE customer_id = 123
          AND status = 'paid'
    """
    second = """
        select total from orders
        where customer_id=$1 and status=$2 -- same shape
    """

    assert canonicalize_sql(first) == canonicalize_sql(second)


def test_sql_canonicalization_preserves_quoted_identifier_case():
    upper = 'SELECT "CustomerID" FROM orders WHERE id = 1'
    lower = 'SELECT "customerid" FROM orders WHERE id = 2'

    assert canonicalize_sql(upper) != canonicalize_sql(lower)


def test_csv_with_query_uses_cross_version_stable_fingerprint(tmp_path: Path):
    csv_file = tmp_path / "pgss.csv"
    csv_file.write_text(
        "queryid,calls,total_exec_time,mean_exec_time,rows,query\n"
        '101,10,150,15,20,"SELECT total FROM orders WHERE id = 7"\n',
        encoding="utf-8",
    )

    snapshot = import_pgss_csv(
        csv_file,
        fingerprint_key="same-secret",
    )

    assert (
        snapshot["fingerprint_scheme"]
        == "hmac-sha256-normalized-sql-v1"
    )
    assert snapshot["fingerprint_cross_version_stable"] is True
    assert "query" not in snapshot["queries"][0]


def test_same_sql_shape_matches_across_different_queryids(tmp_path: Path):
    pg14 = tmp_path / "pg14.csv"
    pg17 = tmp_path / "pg17.csv"

    pg14.write_text(
        "queryid,calls,total_exec_time,mean_exec_time,rows,query\n"
        '111,10,100,10,10,"SELECT total FROM orders WHERE id = 7"\n',
        encoding="utf-8",
    )
    pg17.write_text(
        "queryid,calls,total_exec_time,mean_exec_time,rows,query\n"
        '999999,10,120,12,10,"select total from orders where id=$1"\n',
        encoding="utf-8",
    )

    baseline = import_pgss_csv(
        pg14,
        postgres_version="14.20",
        fingerprint_key="cross-version-key",
    )
    candidate = import_pgss_csv(
        pg17,
        postgres_version="17.6",
        fingerprint_key="cross-version-key",
    )

    assert (
        baseline["queries"][0]["fingerprint"]
        == candidate["queries"][0]["fingerprint"]
    )
    payload = build_assessment_payload(
        baseline,
        candidate,
    )
    assert not any(
        "Cross-version fingerprint stability" in item
        for item in payload["coverage"]["additional_unknowns"]
    )


def test_queryid_based_cross_version_comparison_is_explicit_unknown():
    baseline = {
        "postgres_version": "14.20",
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "fingerprint_cross_version_stable": False,
        "queries": [
            {
                "fingerprint": "pgss:same-looking-id-hash",
                "calls": 100,
                "mean_ms": 10,
            }
        ],
    }
    candidate = {
        "postgres_version": "17.6",
        "fingerprint_scheme": "sha256-queryid-v1",
        "fingerprint_key_id": "unkeyed",
        "fingerprint_cross_version_stable": False,
        "queries": [
            {
                "fingerprint": "pgss:same-looking-id-hash",
                "calls": 100,
                "mean_ms": 20,
            }
        ],
    }
    coverage = {
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 100,
    }

    payload = build_assessment_payload(
        baseline,
        candidate,
        coverage=coverage,
    )
    result = assess(payload)

    assert result.evidence_strength != "HIGH"
    assert any(
        "Cross-version fingerprint stability" in item
        for item in result.known_unknowns
    )
