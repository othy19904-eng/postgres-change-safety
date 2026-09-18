from pgchangesafety.engine import assess
from pgchangesafety.plan import (
    attach_plan_samples,
    canonicalize_plan,
    compare_plan_variants,
    plan_fingerprint,
)
from pgchangesafety.snapshot import build_assessment_payload


def index_plan(cost: float = 1.0, actual_ms: float = 0.1):
    return [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "orders",
                "Index Name": "orders_customer_idx",
                "Startup Cost": cost,
                "Total Cost": cost + 10,
                "Plan Rows": 10,
                "Actual Total Time": actual_ms,
                "Actual Rows": 9,
            }
        }
    ]


def seq_plan():
    return [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "orders",
                "Startup Cost": 0.0,
                "Total Cost": 1000.0,
                "Plan Rows": 5000,
            }
        }
    ]


def snapshot(mean_ms: float = 10.0):
    return {
        "queries": [
            {
                "fingerprint": "pgss:q1",
                "calls": 100,
                "mean_ms": mean_ms,
            }
        ]
    }


def test_plan_fingerprint_ignores_runtime_and_cost_noise():
    first = index_plan(cost=1.0, actual_ms=0.1)
    second = index_plan(cost=999.0, actual_ms=500.0)

    assert canonicalize_plan(first) == canonicalize_plan(second)
    assert plan_fingerprint(first) == plan_fingerprint(second)


def test_plan_fingerprint_changes_when_structure_changes():
    assert plan_fingerprint(index_plan()) != plan_fingerprint(seq_plan())


def test_attach_plan_samples_aggregates_variants_without_raw_plans():
    payload = {
        "samples": [
            {
                "query_fingerprint": "pgss:q1",
                "plan": index_plan(),
                "sample_count": 3,
                "parameter_bucket": "small",
            },
            {
                "query_fingerprint": "pgss:q1",
                "plan": seq_plan(),
                "sample_count": 1,
                "parameter_bucket": "large",
            },
        ]
    }

    enriched = attach_plan_samples(
        snapshot(),
        payload,
        key="test-key",
    )

    query = enriched["queries"][0]
    evidence = query["plan_evidence"]
    assert evidence["total_samples"] == 4
    assert evidence["variant_count"] == 2
    assert evidence["parameter_bucket_count"] == 2
    assert evidence["parameter_bucket_sample_pct"] == 100.0
    assert enriched["plan_fingerprint_scheme"].startswith("hmac-")
    assert "Plan" not in str(enriched)


def test_compare_plan_variants_detects_dominant_switch():
    baseline = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": index_plan(),
                    "sample_count": 8,
                    "parameter_bucket": "small",
                },
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": index_plan(),
                    "sample_count": 2,
                    "parameter_bucket": "large",
                },
            ]
        },
        key="same-key",
    )
    candidate = attach_plan_samples(
        snapshot(mean_ms=20.0),
        {
            "samples": [
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": index_plan(),
                    "sample_count": 2,
                    "parameter_bucket": "small",
                },
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": seq_plan(),
                    "sample_count": 8,
                    "parameter_bucket": "large",
                },
            ]
        },
        key="same-key",
    )

    diffs, unknowns = compare_plan_variants(
        baseline,
        candidate,
    )

    assert unknowns == []
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff["status"] == "VARIANT_SHIFT"
    assert diff["dominant_plan_changed"] is True
    assert diff["candidate_variant_count"] == 2
    assert diff["parameter_sensitivity_status"] == "COVERED"
    assert diff["distribution_shift_pct"] >= 80.0


def test_unbucketed_plan_evidence_is_explicit_unknown():
    baseline = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": index_plan(),
                    "sample_count": 3,
                }
            ]
        },
    )
    candidate = attach_plan_samples(
        snapshot(),
        {
            "samples": [
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": seq_plan(),
                    "sample_count": 3,
                }
            ]
        },
    )

    diffs, unknowns = compare_plan_variants(
        baseline,
        candidate,
    )

    assert diffs[0]["parameter_sensitivity_status"] == "UNKNOWN"
    assert any(
        "Parameter sensitivity is unresolved" in item
        for item in unknowns
    )


def test_assessment_surfaces_plan_shift_on_regression():
    baseline = attach_plan_samples(
        snapshot(mean_ms=10.0),
        {
            "samples": [
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": index_plan(),
                    "sample_count": 4,
                    "parameter_bucket": "small",
                },
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": index_plan(),
                    "sample_count": 4,
                    "parameter_bucket": "large",
                },
            ]
        },
        key="same",
    )
    candidate = attach_plan_samples(
        snapshot(mean_ms=25.0),
        {
            "samples": [
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": seq_plan(),
                    "sample_count": 4,
                    "parameter_bucket": "small",
                },
                {
                    "query_fingerprint": "pgss:q1",
                    "plan": seq_plan(),
                    "sample_count": 4,
                    "parameter_bucket": "large",
                },
            ]
        },
        key="same",
    )

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

    assert len(result.regressions) == 1
    regression = result.regressions[0]
    assert regression.plan_variant_status == "VARIANT_SHIFT"
    assert regression.dominant_plan_changed is True
    assert regression.parameter_sensitivity_status == "COVERED"
    assert result.plan_variant_shift_rate == 1.0
