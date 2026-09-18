from pgchangesafety.engine import assess


def base_payload():
    return {
        "baseline": {
            "queries": [
                {"fingerprint": "q1", "calls": 1000, "p95_ms": 10.0}
            ]
        },
        "candidate": {
            "queries": [
                {"fingerprint": "q1", "calls": 1000, "p95_ms": 20.0}
            ]
        },
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


def repeated_version_trials():
    return [
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 19.8,
            "restored_ms": 10.2,
        },
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 20.2,
            "restored_ms": 9.9,
        },
    ]


def test_probable_cause_requires_repeated_evidence():
    payload = base_payload()
    payload["experiments"] = repeated_version_trials()

    result = assess(payload)

    assert len(result.regressions) == 1
    assert result.regressions[0].cause == "postgres.version"
    assert result.regressions[0].cause_status == "PROBABLE_CAUSE"
    assert result.regressions[0].supporting_trials == 2
    assert result.evidence_strength == "HIGH"


def test_single_trial_stays_unknown():
    payload = base_payload()
    payload["experiments"] = repeated_version_trials()[:1]

    result = assess(payload)

    assert result.regressions[0].cause == "UNKNOWN"
    assert result.regressions[0].cause_status == "UNKNOWN"


def test_unknown_when_causality_not_isolated():
    payload = base_payload()
    payload["experiments"] = []

    result = assess(payload)

    assert result.regressions[0].cause == "UNKNOWN"
    assert result.regressions[0].cause_status == "UNKNOWN"


def test_unresolved_environment_confounder_blocks_probable_cause():
    payload = base_payload()
    payload["environment_diffs"] = [
        {
            "factor": "postgres.version",
            "kind": "postgres_version",
            "baseline": "14",
            "candidate": "17",
        },
        {
            "factor": "config.random_page_cost",
            "kind": "setting",
            "baseline": "4",
            "candidate": "1.1",
        },
    ]
    payload["experiments"] = repeated_version_trials()

    result = assess(payload)

    regression = result.regressions[0]
    assert regression.cause == "UNKNOWN"
    assert "config.random_page_cost" in regression.unresolved_confounders
    assert result.evidence_strength != "HIGH"


def test_tested_negative_confounder_allows_probable_cause():
    payload = base_payload()
    payload["environment_diffs"] = [
        {
            "factor": "postgres.version",
            "kind": "postgres_version",
            "baseline": "14",
            "candidate": "17",
        },
        {
            "factor": "config.random_page_cost",
            "kind": "setting",
            "baseline": "4",
            "candidate": "1.1",
        },
    ]
    payload["experiments"] = repeated_version_trials() + [
        {
            "fingerprint": "q1",
            "factor": "config.random_page_cost",
            "controlled": True,
            "changed_ms": 11.0,
            "restored_ms": 10.4,
        },
        {
            "fingerprint": "q1",
            "factor": "config.random_page_cost",
            "controlled": True,
            "changed_ms": 10.8,
            "restored_ms": 10.1,
        },
    ]

    result = assess(payload)

    regression = result.regressions[0]
    assert regression.cause == "postgres.version"
    assert regression.cause_status == "PROBABLE_CAUSE"
    assert regression.unresolved_confounders == []


def test_critical_unknown_caps_strength():
    payload = base_payload()
    payload["experiments"] = repeated_version_trials()
    payload["coverage"]["peak_concurrency_covered"] = False

    result = assess(payload)

    assert "Peak concurrency is not covered" in result.known_unknowns
    assert result.evidence_strength != "HIGH"


def test_observed_overlap_caps_optimistic_declared_coverage():
    payload = base_payload()
    payload["baseline"]["queries"] = [
        {"fingerprint": "hidden", "calls": 30, "p95_ms": 10.0},
        {"fingerprint": "stable", "calls": 70, "p95_ms": 10.0},
    ]
    payload["candidate"]["queries"] = [
        {"fingerprint": "stable", "calls": 70, "p95_ms": 10.0},
    ]
    payload["coverage"]["workload_volume_pct"] = 100
    payload["coverage"]["bind_value_diversity_pct"] = 100
    payload["coverage"]["environment_match_pct"] = 100

    result = assess(payload)

    assert result.observed_workload_overlap_pct == 70.0
    assert result.evidence_strength != "HIGH"
    assert any(
        "70.0% of observed workload volume" in item
        for item in result.known_unknowns
    )


def test_causal_support_tolerates_runtime_scale_drift():
    payload = base_payload()
    payload["baseline"]["queries"][0]["p95_ms"] = 10.0
    payload["candidate"]["queries"][0]["p95_ms"] = 200.0
    payload["environment_diffs"] = [
        {
            "factor": "postgres.version",
            "kind": "postgres_version",
            "baseline": "14",
            "candidate": "17",
        }
    ]
    payload["experiments"] = [
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 120.0,
            "restored_ms": 10.5,
        },
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 130.0,
            "restored_ms": 10.2,
        },
    ]

    result = assess(payload)

    assert result.regressions[0].cause == "postgres.version"
    assert result.regressions[0].cause_status == "PROBABLE_CAUSE"


def test_small_effect_cannot_explain_large_regression():
    payload = base_payload()
    payload["baseline"]["queries"][0]["p95_ms"] = 10.0
    payload["candidate"]["queries"][0]["p95_ms"] = 200.0
    payload["environment_diffs"] = [
        {
            "factor": "postgres.version",
            "kind": "postgres_version",
            "baseline": "14",
            "candidate": "17",
        }
    ]
    payload["experiments"] = [
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 15.0,
            "restored_ms": 10.0,
        },
        {
            "fingerprint": "q1",
            "factor": "postgres.version",
            "controlled": True,
            "changed_ms": 15.2,
            "restored_ms": 10.1,
        },
    ]

    result = assess(payload)

    assert result.regressions[0].cause == "UNKNOWN"
    assert result.regressions[0].cause_status == "UNKNOWN"


def test_replicated_cause_survives_large_runner_scale_jitter():
    payload = base_payload()
    payload["baseline"]["queries"][0]["p95_ms"] = 10.0
    payload["candidate"]["queries"][0]["p95_ms"] = 1000.0
    payload["environment_diffs"] = [
        {
            "factor": "config.enable_hashjoin",
            "kind": "setting",
            "baseline": "on",
            "candidate": "off",
        }
    ]
    payload["experiments"] = [
        {
            "fingerprint": "q1",
            "factor": "config.enable_hashjoin",
            "controlled": True,
            "changed_ms": 900.0,
            "restored_ms": 10.0,
        },
        {
            "fingerprint": "q1",
            "factor": "config.enable_hashjoin",
            "controlled": True,
            "changed_ms": 200.0,
            "restored_ms": 20.0,
        },
    ]

    result = assess(payload)

    regression = result.regressions[0]
    assert regression.cause == "config.enable_hashjoin"
    assert regression.cause_status == "PROBABLE_CAUSE"
    assert regression.supporting_trials == 2


def test_one_strong_and_one_weak_trial_do_not_form_probable_cause():
    payload = base_payload()
    payload["baseline"]["queries"][0]["p95_ms"] = 10.0
    payload["candidate"]["queries"][0]["p95_ms"] = 1000.0
    payload["environment_diffs"] = [
        {
            "factor": "config.enable_hashjoin",
            "kind": "setting",
            "baseline": "on",
            "candidate": "off",
        }
    ]
    payload["experiments"] = [
        {
            "fingerprint": "q1",
            "factor": "config.enable_hashjoin",
            "controlled": True,
            "changed_ms": 900.0,
            "restored_ms": 10.0,
        },
        {
            "fingerprint": "q1",
            "factor": "config.enable_hashjoin",
            "controlled": True,
            "changed_ms": 15.0,
            "restored_ms": 10.0,
        },
    ]

    result = assess(payload)

    assert result.regressions[0].cause == "UNKNOWN"
