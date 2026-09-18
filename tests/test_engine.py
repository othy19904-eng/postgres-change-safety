from pgchangesafety.engine import assess


def base_payload():
    return {
        "baseline": {"queries": [{"fingerprint": "q1", "calls": 1000, "p95_ms": 10.0}]},
        "candidate": {"queries": [{"fingerprint": "q1", "calls": 1000, "p95_ms": 20.0}]},
        "coverage": {
            "workload_volume_pct": 98,
            "bind_value_diversity_pct": 90,
            "write_workload_covered": True,
            "peak_concurrency_covered": True,
            "background_jobs_covered": True,
            "replay_failure_pct": 1,
            "environment_match_pct": 98,
        },
        "thresholds": {"regression_ratio": 1.25, "min_delta_ms": 5},
    }


def test_probable_cause_isolated():
    payload = base_payload()
    payload["experiments"] = [
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 19.8,
            "restored_ms": 10.2,
        }
    ]
    result = assess(payload)
    assert len(result.regressions) == 1
    assert result.regressions[0].cause == "postgres.version"
    assert result.regressions[0].cause_status == "PROBABLE_CAUSE"
    assert result.evidence_strength == "HIGH"


def test_unknown_when_causality_not_isolated():
    payload = base_payload()
    payload["experiments"] = []
    result = assess(payload)
    assert result.regressions[0].cause == "UNKNOWN"
    assert result.regressions[0].cause_status == "UNKNOWN"


def test_critical_unknown_caps_strength():
    payload = base_payload()
    payload["experiments"] = [
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 20.0,
            "restored_ms": 10.0,
        }
    ]
    payload["coverage"]["peak_concurrency_covered"] = False
    result = assess(payload)
    assert "Peak concurrency is not covered" in result.known_unknowns
    assert result.evidence_strength != "HIGH"
