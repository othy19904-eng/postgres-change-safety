from pathlib import Path

from pgchangesafety.engine import assess
from pgchangesafety.snapshot import build_assessment_payload, import_pgss_csv


def test_import_pgss_csv(tmp_path: Path):
    csv_file = tmp_path / "pgss.csv"
    csv_file.write_text(
        "queryid,calls,total_exec_time,mean_exec_time,rows\n"
        "101,10,150,15,20\n",
        encoding="utf-8",
    )
    snapshot = import_pgss_csv(csv_file, label="pg14")
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


def test_explicit_coverage_can_raise_strength_when_cause_isolated():
    baseline = {
        "queries": [
            {"fingerprint": "queryid:1", "calls": 100, "mean_ms": 10},
        ]
    }
    candidate = {
        "queries": [
            {"fingerprint": "queryid:1", "calls": 100, "mean_ms": 20},
        ]
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
        }
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
