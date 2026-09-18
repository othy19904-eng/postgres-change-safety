from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import mean
from typing import Any


@dataclass
class Regression:
    fingerprint: str
    baseline_ms: float
    candidate_ms: float
    ratio: float
    calls: float
    workload_share_pct: float
    severity: str
    cause: str
    cause_confidence: float
    cause_status: str
    supporting_trials: int
    unresolved_confounders: list[str]


@dataclass
class Assessment:
    regressions: list[Regression]
    decision_coverage_score: float
    known_unknowns: list[str]
    environment_diffs: list[dict[str, Any]]
    unresolved_confounders: list[str]
    evidence_strength: str
    evidence_score: float
    causal_resolution_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "regressions": [asdict(r) for r in self.regressions],
            "decision_coverage_score": self.decision_coverage_score,
            "known_unknowns": self.known_unknowns,
            "environment_diffs": self.environment_diffs,
            "unresolved_confounders": self.unresolved_confounders,
            "evidence_strength": self.evidence_strength,
            "evidence_score": self.evidence_score,
            "causal_resolution_rate": self.causal_resolution_rate,
        }


def _query_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(q["fingerprint"]): q for q in snapshot.get("queries", [])}


def _severity(ratio: float, workload_share_pct: float) -> str:
    if ratio >= 2.0 and workload_share_pct >= 1.0:
        return "CRITICAL"
    if ratio >= 1.5 and workload_share_pct >= 3.0:
        return "CRITICAL"
    if ratio >= 2.0 or workload_share_pct >= 10.0:
        return "HIGH"
    if ratio >= 1.5 or workload_share_pct >= 3.0:
        return "MEDIUM"
    return "LOW"


def _trial_support(
    baseline_ms: float,
    candidate_ms: float,
    exp: dict[str, Any],
) -> float | None:
    if not exp.get("controlled", False):
        return None

    changed = float(exp.get("changed_ms", 0.0))
    restored = float(exp.get("restored_ms", 0.0))
    if baseline_ms <= 0 or candidate_ms <= 0 or changed <= 0 or restored <= 0:
        return None

    reproduce_error = abs(changed - candidate_ms) / max(candidate_ms, 1e-9)
    restore_error = abs(restored - baseline_ms) / max(baseline_ms, 1e-9)
    slowdown = changed / baseline_ms

    support = 1.0 - (
        0.55 * min(reproduce_error, 1.0)
        + 0.45 * min(restore_error, 1.0)
    )
    if slowdown < 1.15:
        support *= 0.35

    return max(0.0, min(support, 1.0))


def _group_experiments(
    fingerprint: str,
    baseline_ms: float,
    candidate_ms: float,
    experiments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for exp in experiments:
        if str(exp.get("fingerprint")) != fingerprint:
            continue

        support = _trial_support(baseline_ms, candidate_ms, exp)
        if support is None:
            continue

        factor = str(exp.get("factor", "unknown"))
        replicates = max(int(exp.get("replicates", 1)), 1)
        bucket = grouped.setdefault(
            factor,
            {"supports": [], "effective_trials": 0},
        )
        bucket["supports"].append(support)
        bucket["effective_trials"] += replicates

    for bucket in grouped.values():
        supports = bucket["supports"]
        bucket["score"] = mean(supports)
        bucket["spread"] = max(supports) - min(supports) if len(supports) > 1 else 0.0
        bucket["repeated"] = (
            len(supports) >= 2 or int(bucket["effective_trials"]) >= 3
        )

    return grouped


def _causal_attribution(
    fingerprint: str,
    baseline_ms: float,
    candidate_ms: float,
    experiments: list[dict[str, Any]],
    environment_diffs: list[dict[str, Any]],
) -> tuple[str, float, str, int, list[str]]:
    grouped = _group_experiments(
        fingerprint,
        baseline_ms,
        candidate_ms,
        experiments,
    )

    changed_factors = {
        str(item.get("factor"))
        for item in environment_diffs
        if item.get("factor")
    }

    reliably_tested = {
        factor
        for factor, data in grouped.items()
        if data.get("repeated", False)
    }
    unresolved = sorted(changed_factors - reliably_tested)

    if not grouped:
        return "UNKNOWN", 0.0, "UNKNOWN", 0, unresolved

    ranked = sorted(
        grouped.items(),
        key=lambda item: float(item[1].get("score", 0.0)),
        reverse=True,
    )
    top_factor, top_data = ranked[0]
    top_score = float(top_data.get("score", 0.0))
    second_score = (
        float(ranked[1][1].get("score", 0.0))
        if len(ranked) > 1
        else 0.0
    )
    trials = int(top_data.get("effective_trials", 0))
    repeated = bool(top_data.get("repeated", False))
    stable = float(top_data.get("spread", 1.0)) <= 0.18

    unresolved_other = [factor for factor in unresolved if factor != top_factor]

    if (
        repeated
        and stable
        and top_score >= 0.80
        and (top_score - second_score) >= 0.12
        and not unresolved_other
    ):
        return (
            top_factor,
            round(top_score, 3),
            "PROBABLE_CAUSE",
            trials,
            [],
        )

    return (
        "UNKNOWN",
        round(top_score, 3),
        "UNKNOWN",
        trials,
        unresolved_other or unresolved,
    )


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _truth(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower in {"true", "yes", "1"}:
            return True
        if lower in {"false", "no", "0"}:
            return False
    return bool(value)


def _coverage(coverage: dict[str, Any]) -> tuple[float, list[str], bool]:
    workload = _number(coverage.get("workload_volume_pct"))
    bind = _number(coverage.get("bind_value_diversity_pct"))
    env = _number(coverage.get("environment_match_pct"))
    replay_failure = _number(coverage.get("replay_failure_pct"))
    replay_success = None if replay_failure is None else max(0.0, 100.0 - replay_failure)

    writes = _truth(coverage.get("write_workload_covered"))
    concurrency = _truth(coverage.get("peak_concurrency_covered"))
    jobs = _truth(coverage.get("background_jobs_covered"))

    score = (
        (workload or 0.0) * 0.34
        + (bind or 0.0) * 0.18
        + (env or 0.0) * 0.16
        + (replay_success or 0.0) * 0.14
        + (100.0 if writes is True else 0.0) * 0.07
        + (100.0 if concurrency is True else 0.0) * 0.07
        + (100.0 if jobs is True else 0.0) * 0.04
    )

    unknowns: list[str] = []

    if workload is None:
        unknowns.append("Observed workload-volume coverage is unknown")
    elif workload < 90:
        unknowns.append(f"Only {workload:.1f}% of observed workload volume was exercised")

    if bind is None:
        unknowns.append("Bind-value diversity coverage is unknown")
    elif bind < 70:
        unknowns.append(f"Bind-value diversity coverage is {bind:.1f}%")

    if writes is None:
        unknowns.append("Write-workload coverage is unknown")
    elif writes is False:
        unknowns.append("Write workload is not covered")

    if concurrency is None:
        unknowns.append("Peak-concurrency coverage is unknown")
    elif concurrency is False:
        unknowns.append("Peak concurrency is not covered")

    if jobs is None:
        unknowns.append("Background-job coverage is unknown")
    elif jobs is False:
        unknowns.append("Background jobs are not covered")

    if replay_success is None:
        unknowns.append("Replay success/failure coverage is unknown")
    elif replay_success < 97:
        unknowns.append(f"Replay success is only {replay_success:.1f}%")

    if env is None:
        unknowns.append("Environment similarity is unknown")
    elif env < 90:
        unknowns.append(f"Environment match is only {env:.1f}%")

    critical_unknown = (
        writes is not True
        or concurrency is not True
        or workload is None
        or workload < 80
        or env is None
        or env < 80
    )
    return round(score, 1), unknowns, critical_unknown


def assess(payload: dict[str, Any]) -> Assessment:
    baseline = _query_map(payload.get("baseline", {}))
    candidate = _query_map(payload.get("candidate", {}))
    experiments = payload.get("experiments", [])
    environment_diffs = payload.get("environment_diffs", [])
    thresholds = payload.get("thresholds", {})
    ratio_threshold = float(thresholds.get("regression_ratio", 1.25))
    min_delta_ms = float(thresholds.get("min_delta_ms", 5.0))

    total_calls = sum(max(float(q.get("calls", 0.0)), 0.0) for q in baseline.values())
    regressions: list[Regression] = []

    for fingerprint, before in baseline.items():
        after = candidate.get(fingerprint)
        if not after:
            continue

        b = float(before.get("p95_ms", before.get("mean_ms", 0.0)))
        c = float(after.get("p95_ms", after.get("mean_ms", 0.0)))
        if b <= 0:
            continue

        ratio = c / b
        if ratio < ratio_threshold or (c - b) < min_delta_ms:
            continue

        calls = max(float(before.get("calls", 0.0)), 0.0)
        workload_share = (calls / total_calls * 100.0) if total_calls > 0 else 0.0

        cause, confidence, status, trials, unresolved = _causal_attribution(
            fingerprint,
            b,
            c,
            experiments,
            environment_diffs,
        )

        regressions.append(
            Regression(
                fingerprint=fingerprint,
                baseline_ms=round(b, 3),
                candidate_ms=round(c, 3),
                ratio=round(ratio, 3),
                calls=calls,
                workload_share_pct=round(workload_share, 2),
                severity=_severity(ratio, workload_share),
                cause=cause,
                cause_confidence=confidence,
                cause_status=status,
                supporting_trials=trials,
                unresolved_confounders=unresolved,
            )
        )

    coverage_score, unknowns, critical_unknown = _coverage(payload.get("coverage", {}))

    unresolved_global = sorted(
        {
            confounder
            for regression in regressions
            for confounder in regression.unresolved_confounders
        }
    )

    if regressions:
        resolved = sum(1 for r in regressions if r.cause_status == "PROBABLE_CAUSE")
        causal_rate = resolved / len(regressions)
    else:
        causal_rate = 1.0

    evidence_score = coverage_score * 0.72 + (causal_rate * 100.0) * 0.28

    if critical_unknown or unresolved_global:
        evidence_score = min(evidence_score, 69.0)
    if unknowns:
        evidence_score = min(evidence_score, 84.0)

    if evidence_score >= 85:
        strength = "HIGH"
    elif evidence_score >= 65:
        strength = "MEDIUM"
    else:
        strength = "LOW"

    severity_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    regressions.sort(
        key=lambda r: (
            severity_rank.get(r.severity, 0),
            r.workload_share_pct,
            r.ratio,
        ),
        reverse=True,
    )

    return Assessment(
        regressions=regressions,
        decision_coverage_score=coverage_score,
        known_unknowns=unknowns,
        environment_diffs=environment_diffs,
        unresolved_confounders=unresolved_global,
        evidence_strength=strength,
        evidence_score=round(evidence_score, 1),
        causal_resolution_rate=round(causal_rate, 3),
    )
