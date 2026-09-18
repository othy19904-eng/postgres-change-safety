from pgchangesafety.demo import build_demo_assessment


def test_demo_shows_high_evidence_causal_plan_shift():
    result = build_demo_assessment()

    assert result["evidence_strength"] == "HIGH"
    assert len(result["regressions"]) == 1

    regression = result["regressions"][0]
    assert regression["cause"] == "schema.orders_customer_idx"
    assert regression["cause_status"] == "PROBABLE_CAUSE"
    assert regression["plan_variant_status"] == "VARIANT_SHIFT"
    assert regression["dominant_plan_changed"] is True
    assert regression["parameter_sensitivity_status"] == "COVERED"
    assert regression["workload_share_pct"] == 25.0

    assert result["known_unknowns"] == []
    assert result["unresolved_confounders"] == []
