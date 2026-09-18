from __future__ import annotations

import copy
import hashlib
import hmac
import json
from collections import defaultdict
from typing import Any


_PLAN_KEYS = (
    "Node Type",
    "Join Type",
    "Strategy",
    "Operation",
    "Relation Name",
    "Schema",
    "Index Name",
    "Scan Direction",
    "Parent Relationship",
    "Partial Mode",
    "Parallel Aware",
    "Async Capable",
    "Subplan Name",
    "CTE Name",
    "Function Name",
)


def _extract_plan(document: Any) -> dict[str, Any]:
    if isinstance(document, list):
        if not document:
            raise ValueError("EXPLAIN JSON document is empty")
        return _extract_plan(document[0])

    if not isinstance(document, dict):
        raise ValueError("EXPLAIN plan must be a JSON object or list")

    if "Plan" in document:
        plan = document["Plan"]
        if not isinstance(plan, dict):
            raise ValueError("EXPLAIN Plan field must be an object")
        return plan

    if "Node Type" in document:
        return document

    raise ValueError(
        "Could not find a PostgreSQL plan tree in EXPLAIN JSON"
    )


def canonicalize_plan(document: Any) -> dict[str, Any]:
    """Keep structural plan identity and discard runtime/cost noise."""
    plan = _extract_plan(document)

    def visit(node: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in _PLAN_KEYS:
            if key in node:
                result[key] = node[key]

        children = node.get("Plans")
        if isinstance(children, list):
            result["Plans"] = [
                visit(child)
                for child in children
                if isinstance(child, dict)
            ]

        return result

    return visit(plan)


def plan_fingerprint(
    document: Any,
    *,
    key: str | None = None,
) -> str:
    canonical = canonicalize_plan(document)
    material = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    if key:
        digest = hmac.new(
            key.encode("utf-8"),
            material,
            hashlib.sha256,
        ).hexdigest()
    else:
        digest = hashlib.sha256(material).hexdigest()

    return f"plan:{digest[:32]}"


def _plan_scheme(key: str | None) -> dict[str, str]:
    if key:
        return {
            "plan_fingerprint_scheme": "hmac-sha256-plan-structure-v1",
            "plan_fingerprint_key_id": hashlib.sha256(
                key.encode("utf-8")
            ).hexdigest()[:12],
        }
    return {
        "plan_fingerprint_scheme": "sha256-plan-structure-v1",
        "plan_fingerprint_key_id": "unkeyed",
    }


def attach_plan_samples(
    snapshot: dict[str, Any],
    samples_payload: dict[str, Any],
    *,
    key: str | None = None,
) -> dict[str, Any]:
    """Attach aggregated plan evidence without persisting raw plans."""
    result = copy.deepcopy(snapshot)
    query_map = {
        str(query["fingerprint"]): query
        for query in result.get("queries", [])
    }

    aggregate: dict[
        str,
        dict[str, dict[str, Any]],
    ] = defaultdict(dict)
    missing_queries: set[str] = set()

    samples = samples_payload.get("samples", [])
    if not isinstance(samples, list):
        raise ValueError("plans payload must contain a samples list")

    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("each plan sample must be an object")

        query_fingerprint = str(
            sample.get("query_fingerprint", "")
        ).strip()
        if not query_fingerprint:
            raise ValueError(
                "plan sample is missing query_fingerprint"
            )

        if query_fingerprint not in query_map:
            missing_queries.add(query_fingerprint)
            continue

        count = int(sample.get("sample_count", 1))
        if count <= 0:
            raise ValueError(
                "plan sample_count must be greater than zero"
            )

        fingerprint = plan_fingerprint(
            sample.get("plan"),
            key=key,
        )
        bucket = sample.get("parameter_bucket")
        bucket_name = (
            str(bucket).strip()
            if bucket is not None
            else ""
        )

        variant = aggregate[query_fingerprint].setdefault(
            fingerprint,
            {
                "sample_count": 0,
                "parameter_buckets": defaultdict(int),
            },
        )
        variant["sample_count"] += count
        if bucket_name:
            variant["parameter_buckets"][bucket_name] += count

    queries_with_evidence = 0
    for query_fingerprint, variants in aggregate.items():
        query = query_map[query_fingerprint]
        total_samples = sum(
            int(item["sample_count"])
            for item in variants.values()
        )
        if total_samples <= 0:
            continue

        rows: list[dict[str, Any]] = []
        bucketed_samples = 0
        all_buckets: set[str] = set()

        for fingerprint, item in variants.items():
            buckets = dict(item["parameter_buckets"])
            bucketed = sum(buckets.values())
            bucketed_samples += bucketed
            all_buckets.update(buckets)

            rows.append(
                {
                    "plan_fingerprint": fingerprint,
                    "sample_count": int(item["sample_count"]),
                    "share_pct": round(
                        int(item["sample_count"])
                        / total_samples
                        * 100.0,
                        2,
                    ),
                    "parameter_buckets": [
                        {
                            "bucket": bucket_name,
                            "sample_count": bucket_count,
                        }
                        for bucket_name, bucket_count in sorted(
                            buckets.items()
                        )
                    ],
                }
            )

        rows.sort(
            key=lambda item: (
                -int(item["sample_count"]),
                str(item["plan_fingerprint"]),
            )
        )
        dominant = rows[0]

        query["plan_variants"] = rows
        query["plan_evidence"] = {
            "total_samples": total_samples,
            "variant_count": len(rows),
            "dominant_plan_fingerprint": dominant[
                "plan_fingerprint"
            ],
            "dominant_share_pct": dominant["share_pct"],
            "parameter_bucket_count": len(all_buckets),
            "parameter_bucket_sample_pct": round(
                bucketed_samples
                / total_samples
                * 100.0,
                2,
            ),
        }
        queries_with_evidence += 1

    result.update(_plan_scheme(key))
    result["plan_evidence_attached"] = True
    result["plan_queries_with_evidence"] = queries_with_evidence
    result["plan_samples_unmatched_queries"] = sorted(
        missing_queries
    )
    return result


def _variant_shares(
    query: dict[str, Any],
) -> dict[str, float]:
    return {
        str(item["plan_fingerprint"]): float(
            item.get("share_pct", 0.0)
        )
        for item in query.get("plan_variants", [])
    }


def compare_plan_variants(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    before = {
        str(query["fingerprint"]): query
        for query in baseline.get("queries", [])
    }
    after = {
        str(query["fingerprint"]): query
        for query in candidate.get("queries", [])
    }

    diffs: list[dict[str, Any]] = []
    unknowns: list[str] = []

    if not (
        baseline.get("plan_evidence_attached")
        or candidate.get("plan_evidence_attached")
    ):
        return diffs, unknowns

    left_scheme = baseline.get("plan_fingerprint_scheme")
    right_scheme = candidate.get("plan_fingerprint_scheme")
    left_key = baseline.get("plan_fingerprint_key_id")
    right_key = candidate.get("plan_fingerprint_key_id")
    if (
        left_scheme
        and right_scheme
        and str(left_scheme) != str(right_scheme)
    ):
        raise ValueError(
            "Snapshots use different plan fingerprint schemes"
        )
    if (
        left_key
        and right_key
        and str(left_key) != str(right_key)
    ):
        raise ValueError(
            "Snapshots use different plan fingerprint keys"
        )

    for fingerprint in sorted(set(before) & set(after)):
        left = before[fingerprint]
        right = after[fingerprint]
        left_ev = left.get("plan_evidence")
        right_ev = right.get("plan_evidence")

        if left_ev is None and right_ev is None:
            continue
        if left_ev is None or right_ev is None:
            unknowns.append(
                "Plan evidence is missing on one side for "
                f"{fingerprint}"
            )
            diffs.append(
                {
                    "fingerprint": fingerprint,
                    "status": "UNKNOWN",
                    "baseline_variant_count": (
                        int(left_ev["variant_count"])
                        if left_ev
                        else 0
                    ),
                    "candidate_variant_count": (
                        int(right_ev["variant_count"])
                        if right_ev
                        else 0
                    ),
                    "new_plan_variants": [],
                    "disappeared_plan_variants": [],
                    "dominant_plan_changed": None,
                    "distribution_shift_pct": None,
                    "parameter_sensitivity_status": "UNKNOWN",
                }
            )
            continue

        left_shares = _variant_shares(left)
        right_shares = _variant_shares(right)
        left_set = set(left_shares)
        right_set = set(right_shares)

        union = left_set | right_set
        distribution_shift = 0.5 * sum(
            abs(
                left_shares.get(plan, 0.0)
                - right_shares.get(plan, 0.0)
            )
            for plan in union
        )

        dominant_changed = (
            str(left_ev.get("dominant_plan_fingerprint"))
            != str(right_ev.get("dominant_plan_fingerprint"))
        )

        new_variants = sorted(right_set - left_set)
        disappeared = sorted(left_set - right_set)

        left_bucket_count = int(
            left_ev.get("parameter_bucket_count", 0)
        )
        right_bucket_count = int(
            right_ev.get("parameter_bucket_count", 0)
        )
        left_bucket_pct = float(
            left_ev.get("parameter_bucket_sample_pct", 0.0)
        )
        right_bucket_pct = float(
            right_ev.get("parameter_bucket_sample_pct", 0.0)
        )

        parameter_covered = (
            left_bucket_count >= 2
            and right_bucket_count >= 2
            and left_bucket_pct >= 80.0
            and right_bucket_pct >= 80.0
        )
        parameter_status = (
            "COVERED"
            if parameter_covered
            else "UNKNOWN"
        )

        if not parameter_covered:
            unknowns.append(
                "Parameter sensitivity is unresolved for "
                f"{fingerprint}: plan samples need at least "
                "two parameter buckets covering 80% on both sides"
            )

        shifted = (
            bool(new_variants)
            or bool(disappeared)
            or dominant_changed
            or distribution_shift >= 25.0
        )
        status = (
            "VARIANT_SHIFT"
            if shifted
            else "STABLE"
        )

        diffs.append(
            {
                "fingerprint": fingerprint,
                "status": status,
                "baseline_variant_count": int(
                    left_ev.get("variant_count", 0)
                ),
                "candidate_variant_count": int(
                    right_ev.get("variant_count", 0)
                ),
                "new_plan_variants": new_variants,
                "disappeared_plan_variants": disappeared,
                "baseline_dominant_plan": left_ev.get(
                    "dominant_plan_fingerprint"
                ),
                "candidate_dominant_plan": right_ev.get(
                    "dominant_plan_fingerprint"
                ),
                "dominant_plan_changed": dominant_changed,
                "distribution_shift_pct": round(
                    distribution_shift,
                    2,
                ),
                "parameter_sensitivity_status": parameter_status,
                "baseline_parameter_bucket_count": left_bucket_count,
                "candidate_parameter_bucket_count": right_bucket_count,
                "baseline_parameter_bucket_sample_pct": left_bucket_pct,
                "candidate_parameter_bucket_sample_pct": right_bucket_pct,
            }
        )

    for snapshot, side in (
        (baseline, "baseline"),
        (candidate, "candidate"),
    ):
        unmatched = snapshot.get(
            "plan_samples_unmatched_queries",
            [],
        )
        if unmatched:
            unknowns.append(
                f"{side} plan samples referenced "
                f"{len(unmatched)} query fingerprint(s) "
                "that were absent from the snapshot"
            )

    return diffs, unknowns
