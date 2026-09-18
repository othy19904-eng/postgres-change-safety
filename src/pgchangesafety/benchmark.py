from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import assess


_STRENGTH_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _actual_for(
    result: dict[str, Any],
    fingerprint: str,
) -> dict[str, Any] | None:
    for regression in result.get("regressions", []):
        if str(regression.get("fingerprint")) == fingerprint:
            return regression
    return None


def _ratio(correct: int, total: int) -> float:
    return 1.0 if total == 0 else round(correct / total, 4)


def _workload_case_result(
    case_id: str,
    assessment: dict[str, Any],
    oracle: dict[str, Any],
) -> dict[str, Any]:
    spec = oracle["workload"]
    actual_by_fp = {
        str(item["fingerprint"]): item
        for item in assessment.get("regressions", [])
    }

    expected = {
        str(fingerprint): str(cause)
        for fingerprint, cause in spec.get(
            "expected_regressions",
            {},
        ).items()
    }
    expected_stable = {
        str(item)
        for item in spec.get("expected_stable", [])
    }
    hidden = {
        str(item)
        for item in spec.get("hidden_regressions", [])
    }

    actual_fps = set(actual_by_fp)
    expected_fps = set(expected)
    missing = sorted(expected_fps - actual_fps)
    unexpected = sorted(actual_fps - expected_fps)
    stable_false_positives = sorted(actual_fps & expected_stable)

    cause_mismatches: list[dict[str, str]] = []
    for fingerprint, expected_cause in expected.items():
        actual = actual_by_fp.get(fingerprint)
        if actual is None:
            continue
        actual_cause = str(actual.get("cause", "UNKNOWN"))
        if actual_cause != expected_cause:
            cause_mismatches.append(
                {
                    "fingerprint": fingerprint,
                    "expected": expected_cause,
                    "actual": actual_cause,
                }
            )

    overlap = assessment.get("observed_workload_overlap_pct")
    overlap_ok = True
    if spec.get("min_observed_workload_overlap_pct") is not None:
        overlap_ok = (
            overlap is not None
            and float(overlap)
            >= float(spec["min_observed_workload_overlap_pct"])
        )
    if spec.get("max_observed_workload_overlap_pct") is not None:
        overlap_ok = (
            overlap_ok
            and overlap is not None
            and float(overlap)
            <= float(spec["max_observed_workload_overlap_pct"])
        )

    strength = str(assessment.get("evidence_strength", "LOW"))
    strength_ok = True
    max_strength = spec.get("max_evidence_strength")
    if max_strength is not None:
        strength_ok = (
            _STRENGTH_RANK.get(strength, -1)
            <= _STRENGTH_RANK[str(max_strength)]
        )

    # The engine intentionally does not emit GO/NO-GO. For the benchmark,
    # "clearance-like" means the dangerous combination of no detected
    # regression plus HIGH evidence and no explicit uncertainty.
    clearance_like = (
        not actual_by_fp
        and strength == "HIGH"
        and not assessment.get("known_unknowns")
        and not assessment.get("unresolved_confounders")
    )
    false_clearance = bool(
        spec.get("must_not_clear", False)
        and clearance_like
    )

    passed = (
        not missing
        and not unexpected
        and not stable_false_positives
        and not cause_mismatches
        and overlap_ok
        and strength_ok
        and not false_clearance
    )

    return {
        "id": case_id,
        "kind": "workload",
        "passed": passed,
        "expected_regressions": expected,
        "actual_regressions": {
            fingerprint: str(item.get("cause", "UNKNOWN"))
            for fingerprint, item in actual_by_fp.items()
        },
        "missing_regressions": missing,
        "unexpected_regressions": unexpected,
        "stable_false_positives": stable_false_positives,
        "cause_mismatches": cause_mismatches,
        "hidden_regressions": sorted(hidden),
        "observed_workload_overlap_pct": overlap,
        "evidence_strength": strength,
        "known_unknowns": list(
            assessment.get("known_unknowns", [])
        ),
        "false_clearance": false_clearance,
    }


def run_blind_benchmark(path: Path) -> dict[str, Any]:
    suite = json.loads(path.read_text(encoding="utf-8"))
    cases = suite.get("cases", [])
    results: list[dict[str, Any]] = []

    detection_total = 0
    detection_correct = 0
    provable_total = 0
    provable_correct = 0
    abstention_total = 0
    abstention_correct = 0
    false_causal_attributions = 0

    workload_cases = 0
    workload_passed = 0
    workload_expected_regressions = 0
    workload_detected_regressions = 0
    workload_false_positives = 0
    false_clearance_events = 0

    for case in cases:
        case_id = str(case["id"])
        payload = case["payload"]
        oracle = case["oracle"]

        # The engine sees payload only. The oracle is read only after
        # assessment, so planted truth remains outside the engine input.
        assessment = assess(payload).to_dict()

        if "workload" in oracle:
            item = _workload_case_result(
                case_id,
                assessment,
                oracle,
            )
            results.append(item)

            workload_cases += 1
            workload_passed += int(item["passed"])
            workload_expected_regressions += len(
                item["expected_regressions"]
            )
            workload_detected_regressions += (
                len(item["expected_regressions"])
                - len(item["missing_regressions"])
            )
            workload_false_positives += len(
                item["unexpected_regressions"]
            )
            false_clearance_events += int(
                item["false_clearance"]
            )
            continue

        fingerprint = str(oracle["fingerprint"])
        expected_regression = bool(oracle["regression"])
        expected_cause = str(
            oracle.get("cause", "UNKNOWN")
        )

        actual = _actual_for(assessment, fingerprint)
        actual_regression = actual is not None
        actual_cause = (
            str(actual.get("cause", "UNKNOWN"))
            if actual is not None
            else "NONE"
        )

        detection_total += 1
        detection_ok = (
            actual_regression == expected_regression
        )
        detection_correct += int(detection_ok)

        attribution_ok: bool | None = None
        if expected_regression:
            if expected_cause == "UNKNOWN":
                abstention_total += 1
                attribution_ok = (
                    actual_regression
                    and actual_cause == "UNKNOWN"
                )
                abstention_correct += int(
                    bool(attribution_ok)
                )
                if (
                    actual_regression
                    and actual_cause
                    not in {"UNKNOWN", "NONE"}
                ):
                    false_causal_attributions += 1
            else:
                provable_total += 1
                attribution_ok = (
                    actual_regression
                    and actual_cause == expected_cause
                )
                provable_correct += int(
                    bool(attribution_ok)
                )

        case_passed = detection_ok and (
            attribution_ok is None or attribution_ok
        )

        results.append(
            {
                "id": case_id,
                "kind": "query",
                "fingerprint": fingerprint,
                "expected_regression": expected_regression,
                "actual_regression": actual_regression,
                "expected_cause": (
                    expected_cause
                    if expected_regression
                    else "N/A"
                ),
                "actual_cause": actual_cause,
                "passed": bool(case_passed),
            }
        )

    metrics = {
        "cases": len(cases),
        "passed": sum(
            1 for item in results if item["passed"]
        ),
        "detection_accuracy": _ratio(
            detection_correct,
            detection_total,
        ),
        "provable_cause_accuracy": _ratio(
            provable_correct,
            provable_total,
        ),
        "unknown_abstention_accuracy": _ratio(
            abstention_correct,
            abstention_total,
        ),
        "false_causal_attributions": (
            false_causal_attributions
        ),
        "workload_cases": workload_cases,
        "workload_passed": workload_passed,
        "workload_regression_recall": _ratio(
            workload_detected_regressions,
            workload_expected_regressions,
        ),
        "workload_false_positives": (
            workload_false_positives
        ),
        "false_clearance_events": (
            false_clearance_events
        ),
    }

    return {
        "suite": suite.get("name", path.name),
        "metrics": metrics,
        "cases": results,
    }


def strict_pass(report: dict[str, Any]) -> bool:
    metrics = report["metrics"]
    return (
        metrics["passed"] == metrics["cases"]
        and metrics["detection_accuracy"] == 1.0
        and metrics["provable_cause_accuracy"] == 1.0
        and metrics["unknown_abstention_accuracy"] == 1.0
        and metrics["false_causal_attributions"] == 0
        and metrics["workload_passed"]
        == metrics["workload_cases"]
        and metrics["workload_regression_recall"] == 1.0
        and metrics["workload_false_positives"] == 0
        and metrics["false_clearance_events"] == 0
    )


def render_benchmark(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        f"Blind Regression Benchmark: {report['suite']}",
        "=" * 54,
        (
            f"Cases passed: "
            f"{metrics['passed']}/{metrics['cases']}"
        ),
        (
            "Regression detection accuracy: "
            f"{metrics['detection_accuracy'] * 100:.1f}%"
        ),
        (
            "Provable-cause accuracy: "
            f"{metrics['provable_cause_accuracy'] * 100:.1f}%"
        ),
        (
            "UNKNOWN abstention accuracy: "
            f"{metrics['unknown_abstention_accuracy'] * 100:.1f}%"
        ),
        (
            "False causal attributions: "
            f"{metrics['false_causal_attributions']}"
        ),
        (
            "Workload cases passed: "
            f"{metrics['workload_passed']}/"
            f"{metrics['workload_cases']}"
        ),
        (
            "Workload regression recall: "
            f"{metrics['workload_regression_recall'] * 100:.1f}%"
        ),
        (
            "Workload false positives: "
            f"{metrics['workload_false_positives']}"
        ),
        (
            "False-clearance events: "
            f"{metrics['false_clearance_events']}"
        ),
        "",
        "Cases:",
    ]

    for item in report["cases"]:
        mark = "PASS" if item["passed"] else "FAIL"
        if item.get("kind") == "workload":
            lines.append(
                f"- {mark} {item['id']}: "
                f"expected={len(item['expected_regressions'])} "
                f"actual={len(item['actual_regressions'])}; "
                f"overlap="
                f"{item['observed_workload_overlap_pct']}%; "
                f"evidence={item['evidence_strength']}; "
                f"false_clearance="
                f"{item['false_clearance']}"
            )
            if item["missing_regressions"]:
                lines.append(
                    "  missing regressions: "
                    + ", ".join(
                        item["missing_regressions"]
                    )
                )
            if item["unexpected_regressions"]:
                lines.append(
                    "  unexpected regressions: "
                    + ", ".join(
                        item["unexpected_regressions"]
                    )
                )
            if item["cause_mismatches"]:
                for mismatch in item[
                    "cause_mismatches"
                ]:
                    lines.append(
                        "  cause mismatch: "
                        f"{mismatch['fingerprint']} "
                        f"expected={mismatch['expected']} "
                        f"actual={mismatch['actual']}"
                    )
            continue

        lines.append(
            f"- {mark} {item['id']}: "
            f"regression expected="
            f"{item['expected_regression']} "
            f"actual={item['actual_regression']}; "
            f"cause expected={item['expected_cause']} "
            f"actual={item['actual_cause']}"
        )

    return "\n".join(lines)
