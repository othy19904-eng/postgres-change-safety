from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import assess


def _actual_for(result: dict[str, Any], fingerprint: str) -> dict[str, Any] | None:
    for regression in result.get("regressions", []):
        if str(regression.get("fingerprint")) == fingerprint:
            return regression
    return None


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

    for case in cases:
        case_id = str(case["id"])
        payload = case["payload"]
        oracle = case["oracle"]

        # The engine sees payload only. The oracle is read only after assessment.
        assessment = assess(payload).to_dict()

        fingerprint = str(oracle["fingerprint"])
        expected_regression = bool(oracle["regression"])
        expected_cause = str(oracle.get("cause", "UNKNOWN"))

        actual = _actual_for(assessment, fingerprint)
        actual_regression = actual is not None
        actual_cause = (
            str(actual.get("cause", "UNKNOWN")) if actual is not None else "NONE"
        )

        detection_total += 1
        detection_ok = actual_regression == expected_regression
        detection_correct += int(detection_ok)

        attribution_ok: bool | None = None
        if expected_regression:
            if expected_cause == "UNKNOWN":
                abstention_total += 1
                attribution_ok = actual_regression and actual_cause == "UNKNOWN"
                abstention_correct += int(bool(attribution_ok))
                if actual_regression and actual_cause not in {"UNKNOWN", "NONE"}:
                    false_causal_attributions += 1
            else:
                provable_total += 1
                attribution_ok = (
                    actual_regression and actual_cause == expected_cause
                )
                provable_correct += int(bool(attribution_ok))

        case_passed = detection_ok and (
            attribution_ok is None or attribution_ok
        )

        results.append(
            {
                "id": case_id,
                "fingerprint": fingerprint,
                "expected_regression": expected_regression,
                "actual_regression": actual_regression,
                "expected_cause": expected_cause if expected_regression else "N/A",
                "actual_cause": actual_cause,
                "passed": bool(case_passed),
            }
        )

    def ratio(correct: int, total: int) -> float:
        return 1.0 if total == 0 else round(correct / total, 4)

    metrics = {
        "cases": len(cases),
        "passed": sum(1 for item in results if item["passed"]),
        "detection_accuracy": ratio(detection_correct, detection_total),
        "provable_cause_accuracy": ratio(provable_correct, provable_total),
        "unknown_abstention_accuracy": ratio(abstention_correct, abstention_total),
        "false_causal_attributions": false_causal_attributions,
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
    )


def render_benchmark(report: dict[str, Any]) -> str:
    metrics = report["metrics"]
    lines = [
        f"Blind Regression Benchmark: {report['suite']}",
        "=" * 54,
        f"Cases passed: {metrics['passed']}/{metrics['cases']}",
        f"Regression detection accuracy: {metrics['detection_accuracy'] * 100:.1f}%",
        f"Provable-cause accuracy: {metrics['provable_cause_accuracy'] * 100:.1f}%",
        f"UNKNOWN abstention accuracy: {metrics['unknown_abstention_accuracy'] * 100:.1f}%",
        f"False causal attributions: {metrics['false_causal_attributions']}",
        "",
        "Cases:",
    ]

    for item in report["cases"]:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(
            f"- {mark} {item['id']}: "
            f"regression expected={item['expected_regression']} "
            f"actual={item['actual_regression']}; "
            f"cause expected={item['expected_cause']} "
            f"actual={item['actual_cause']}"
        )

    return "\n".join(lines)
