from __future__ import annotations

from typing import Any

from .engine import assess
from .plan import attach_plan_samples
from .snapshot import build_assessment_payload


_QUERY = "pgss:demo-checkout-by-customer"


def _index_plan() -> list[dict[str, Any]]:
    return [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Relation Name": "orders",
                "Index Name": "orders_customer_idx",
            }
        }
    ]


def _seq_plan() -> list[dict[str, Any]]:
    return [
        {
            "Plan": {
                "Node Type": "Seq Scan",
                "Relation Name": "orders",
            }
        }
    ]


def build_demo_assessment() -> dict[str, Any]:
    """Build a self-contained synthetic demonstration.

    This intentionally uses no database connection. It demonstrates the
    reporting contract only: regression detection, causal evidence,
    plan-variant shift, workload share, and explicit coverage.
    """
    baseline = {
        "label": "demo-baseline",
        "postgres_version": "17.11",
        "queries": [
            {
                "fingerprint": _QUERY,
                "calls": 1000,
                "mean_ms": 10.0,
            },
            {
                "fingerprint": "pgss:demo-stable-query",
                "calls": 3000,
                "mean_ms": 4.0,
            },
        ],
    }
    candidate = {
        "label": "demo-candidate",
        "postgres_version": "17.11",
        "queries": [
            {
                "fingerprint": _QUERY,
                "calls": 1000,
                "mean_ms": 28.0,
            },
            {
                "fingerprint": "pgss:demo-stable-query",
                "calls": 3000,
                "mean_ms": 4.2,
            },
        ],
    }

    baseline = attach_plan_samples(
        baseline,
        {
            "samples": [
                {
                    "query_fingerprint": _QUERY,
                    "plan": _index_plan(),
                    "sample_count": 5,
                    "parameter_bucket": "narrow",
                },
                {
                    "query_fingerprint": _QUERY,
                    "plan": _index_plan(),
                    "sample_count": 5,
                    "parameter_bucket": "wide",
                },
            ]
        },
        key="demo-key",
    )
    candidate = attach_plan_samples(
        candidate,
        {
            "samples": [
                {
                    "query_fingerprint": _QUERY,
                    "plan": _index_plan(),
                    "sample_count": 2,
                    "parameter_bucket": "narrow",
                },
                {
                    "query_fingerprint": _QUERY,
                    "plan": _seq_plan(),
                    "sample_count": 8,
                    "parameter_bucket": "wide",
                },
            ]
        },
        key="demo-key",
    )

    coverage = {
        "workload_volume_pct": 100,
        "bind_value_diversity_pct": 100,
        "write_workload_covered": True,
        "peak_concurrency_covered": True,
        "background_jobs_covered": True,
        "replay_failure_pct": 0,
        "environment_match_pct": 100,
    }
    experiments = [
        {
            "fingerprint": _QUERY,
            "factor": "schema.orders_customer_idx",
            "controlled": True,
            "changed_ms": 28.0,
            "restored_ms": 10.2,
        },
        {
            "fingerprint": _QUERY,
            "factor": "schema.orders_customer_idx",
            "controlled": True,
            "changed_ms": 27.5,
            "restored_ms": 10.1,
        },
    ]

    payload = build_assessment_payload(
        baseline,
        candidate,
        coverage=coverage,
        experiments=experiments,
        thresholds={
            "regression_ratio": 1.25,
            "min_delta_ms": 5.0,
        },
    )
    return assess(payload).to_dict()
