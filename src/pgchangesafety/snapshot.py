from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


_REQUIRED = {"queryid", "calls"}
_SETTINGS = [
    "max_connections",
    "shared_buffers",
    "work_mem",
    "maintenance_work_mem",
    "random_page_cost",
    "effective_cache_size",
    "jit",
    "max_parallel_workers_per_gather",
]


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    return float(value)


def _normalize_pgss_row(row: dict[str, Any]) -> dict[str, Any]:
    queryid = str(row.get("queryid", "")).strip()
    if not queryid:
        raise ValueError("pg_stat_statements row is missing queryid")

    calls = _to_float(row.get("calls"))

    if row.get("mean_exec_time") not in (None, ""):
        mean_ms = _to_float(row.get("mean_exec_time"))
    else:
        total_ms = _to_float(row.get("total_exec_time"))
        mean_ms = total_ms / calls if calls > 0 else 0.0

    normalized: dict[str, Any] = {
        "fingerprint": f"queryid:{queryid}",
        "calls": calls,
        "mean_ms": mean_ms,
        "rows": _to_float(row.get("rows")),
    }

    if row.get("query"):
        normalized["query"] = str(row["query"])

    return normalized


def import_pgss_csv(
    path: Path,
    *,
    label: str = "snapshot",
    postgres_version: str | None = None,
) -> dict[str, Any]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = _REQUIRED - fields
        if missing:
            raise ValueError(
                "pg_stat_statements CSV is missing required column(s): "
                + ", ".join(sorted(missing))
            )
        rows = [_normalize_pgss_row(row) for row in reader]

    return {
        "label": label,
        "postgres_version": postgres_version,
        "source": "pg_stat_statements_csv",
        "queries": rows,
    }


def capture_live(
    dsn: str,
    *,
    label: str = "snapshot",
    include_query_text: bool = False,
) -> dict[str, Any]:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Live capture requires the optional postgres dependency. "
            'Install with: pip install -e ".[postgres]"'
        ) from exc

    query_column = ", query" if include_query_text else ""
    sql = f"""
        SELECT
          queryid::text AS queryid,
          calls,
          total_exec_time,
          mean_exec_time,
          rows
          {query_column}
        FROM pg_stat_statements
        WHERE calls > 0
    """

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            version = str(cur.fetchone()[0])

            cur.execute(sql)
            names = [desc.name for desc in cur.description]
            rows = [
                _normalize_pgss_row(dict(zip(names, values)))
                for values in cur.fetchall()
            ]

            cur.execute(
                "SELECT name, setting FROM pg_settings WHERE name = ANY(%s)",
                (_SETTINGS,),
            )
            settings = {str(name): str(setting) for name, setting in cur.fetchall()}

    return {
        "label": label,
        "postgres_version": version,
        "source": "pg_stat_statements_live",
        "settings": settings,
        "queries": rows,
    }


def _query_map(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(q["fingerprint"]): q for q in snapshot.get("queries", [])}


def _derive_workload_overlap(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> float | None:
    before = _query_map(baseline)
    after = _query_map(candidate)
    total_calls = sum(max(_to_float(q.get("calls")), 0.0) for q in before.values())
    if total_calls <= 0:
        return None

    shared_calls = sum(
        max(_to_float(q.get("calls")), 0.0)
        for fingerprint, q in before.items()
        if fingerprint in after
    )
    return round(shared_calls / total_calls * 100.0, 2)


def _derive_environment_match(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> float | None:
    before = baseline.get("settings") or {}
    after = candidate.get("settings") or {}
    common = sorted(set(before) & set(after))
    if not common:
        return None

    same = sum(1 for key in common if str(before[key]) == str(after[key]))
    return round(same / len(common) * 100.0, 2)


def derive_environment_diffs(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []

    before_version = baseline.get("postgres_version")
    after_version = candidate.get("postgres_version")
    if (
        before_version not in (None, "")
        and after_version not in (None, "")
        and str(before_version) != str(after_version)
    ):
        diffs.append(
            {
                "factor": "postgres.version",
                "kind": "postgres_version",
                "baseline": str(before_version),
                "candidate": str(after_version),
            }
        )

    before_settings = baseline.get("settings") or {}
    after_settings = candidate.get("settings") or {}
    for key in sorted(set(before_settings) | set(after_settings)):
        before = before_settings.get(key)
        after = after_settings.get(key)
        if str(before) == str(after):
            continue
        diffs.append(
            {
                "factor": f"config.{key}",
                "kind": "setting",
                "baseline": before,
                "candidate": after,
            }
        )

    return diffs


def build_assessment_payload(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    coverage: dict[str, Any] | None = None,
    experiments: dict[str, Any] | list[dict[str, Any]] | None = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    derived_coverage: dict[str, Any] = {
        "workload_volume_pct": _derive_workload_overlap(baseline, candidate),
        "bind_value_diversity_pct": None,
        "write_workload_covered": None,
        "peak_concurrency_covered": None,
        "background_jobs_covered": None,
        "replay_failure_pct": None,
        "environment_match_pct": _derive_environment_match(baseline, candidate),
    }

    if coverage:
        derived_coverage.update(coverage)

    if isinstance(experiments, dict):
        experiment_rows = experiments.get("experiments", [])
    else:
        experiment_rows = experiments or []

    return {
        "baseline": baseline,
        "candidate": candidate,
        "coverage": derived_coverage,
        "environment_diffs": derive_environment_diffs(baseline, candidate),
        "experiments": experiment_rows,
        "thresholds": thresholds or {},
    }
