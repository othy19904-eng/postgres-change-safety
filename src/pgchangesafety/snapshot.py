from __future__ import annotations

import csv
import hashlib
import hmac
from datetime import datetime, timezone
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonicalize_sql(sql: str) -> str:
    """Return a conservative, literal-insensitive SQL token stream.

    This is intentionally not a full SQL parser. Its job is narrower:
    make the same PostgreSQL statement stable across whitespace,
    comments, bind placeholders, and literal values before hashing.
    Quoted identifiers are preserved because their case can be
    semantically significant.
    """
    tokens: list[str] = []
    i = 0
    n = len(sql)

    while i < n:
        ch = sql[i]

        if ch.isspace():
            i += 1
            continue

        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue

        if sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue

        if ch == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append("?")
            continue

        if ch == "$":
            if i + 1 < n and sql[i + 1].isdigit():
                i += 2
                while i < n and sql[i].isdigit():
                    i += 1
                tokens.append("?")
                continue

            tag_end = i + 1
            while (
                tag_end < n
                and (
                    sql[tag_end].isalnum()
                    or sql[tag_end] == "_"
                )
            ):
                tag_end += 1
            if tag_end < n and sql[tag_end] == "$":
                tag = sql[i : tag_end + 1]
                end = sql.find(tag, tag_end + 1)
                if end >= 0:
                    i = end + len(tag)
                    tokens.append("?")
                    continue

        if ch == '"':
            start = i
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            tokens.append(sql[start:i])
            continue

        if ch.isdigit() or (
            ch == "."
            and i + 1 < n
            and sql[i + 1].isdigit()
        ):
            i += 1
            while i < n and (
                sql[i].isalnum()
                or sql[i] in ".+-_"
            ):
                i += 1
            tokens.append("?")
            continue

        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (
                sql[i].isalnum()
                or sql[i] in "_$"
            ):
                i += 1
            tokens.append(sql[start:i].lower())
            continue

        if ch in "~!@#%^&*+-=|<>/?:":
            start = i
            i += 1
            while i < n and sql[i] in "~!@#%^&*+-=|<>/?:":
                i += 1
            tokens.append(sql[start:i])
            continue

        tokens.append(ch)
        i += 1

    return " ".join(tokens)


def _fingerprint_metadata(
    key: str | None,
    *,
    raw_queryid: bool,
    stable_sql: bool,
) -> dict[str, Any]:
    if raw_queryid:
        return {
            "fingerprint_scheme": "raw-queryid-v1",
            "fingerprint_key_id": "none",
            "fingerprint_basis": "queryid",
            "fingerprint_cross_version_stable": False,
        }

    basis = "normalized-sql" if stable_sql else "queryid"
    if key:
        scheme = f"hmac-sha256-{basis}-v1"
        key_id = hashlib.sha256(
            key.encode("utf-8")
        ).hexdigest()[:12]
    else:
        scheme = f"sha256-{basis}-v1"
        key_id = "unkeyed"

    return {
        "fingerprint_scheme": scheme,
        "fingerprint_key_id": key_id,
        "fingerprint_basis": (
            "normalized_sql"
            if stable_sql
            else "queryid"
        ),
        "fingerprint_cross_version_stable": stable_sql,
    }


def _fingerprint(
    queryid: str,
    *,
    query: str | None,
    key: str | None,
    raw_queryid: bool,
    stable_sql: bool,
) -> str:
    if raw_queryid:
        return f"queryid:{queryid}"

    if stable_sql:
        if not query:
            raise ValueError(
                "Stable SQL fingerprinting requires query text "
                "during capture/import"
            )
        material = canonicalize_sql(query)
        if not material:
            raise ValueError(
                "Cannot fingerprint an empty normalized SQL statement"
            )
    else:
        material = queryid

    data = material.encode("utf-8")
    if key:
        digest = hmac.new(
            key.encode("utf-8"),
            data,
            hashlib.sha256,
        ).hexdigest()
    else:
        digest = hashlib.sha256(data).hexdigest()

    return f"pgss:{digest[:32]}"


def _normalize_pgss_row(
    row: dict[str, Any],
    *,
    fingerprint_key: str | None = None,
    raw_queryid: bool = False,
    stable_sql: bool = False,
    store_query_text: bool = True,
) -> dict[str, Any]:
    queryid = str(row.get("queryid", "")).strip()
    if not queryid:
        raise ValueError(
            "pg_stat_statements row is missing queryid"
        )

    calls = _to_float(row.get("calls"))
    total_ms = _to_float(row.get("total_exec_time"))

    if row.get("mean_exec_time") not in (None, ""):
        mean_ms = _to_float(row.get("mean_exec_time"))
    else:
        mean_ms = total_ms / calls if calls > 0 else 0.0

    normalized: dict[str, Any] = {
        "fingerprint": _fingerprint(
            queryid,
            query=(
                str(row.get("query"))
                if row.get("query")
                else None
            ),
            key=fingerprint_key,
            raw_queryid=raw_queryid,
            stable_sql=stable_sql,
        ),
        "calls": calls,
        "total_exec_time": total_ms,
        "mean_ms": mean_ms,
        "rows": _to_float(row.get("rows")),
    }

    if store_query_text and row.get("query"):
        normalized["query"] = str(row["query"])

    return normalized


def _assert_fingerprint_compatibility(
    left: dict[str, Any],
    right: dict[str, Any],
) -> None:
    left_scheme = left.get("fingerprint_scheme")
    right_scheme = right.get("fingerprint_scheme")
    left_key = left.get("fingerprint_key_id")
    right_key = right.get("fingerprint_key_id")

    if (
        left_scheme
        and right_scheme
        and str(left_scheme) != str(right_scheme)
    ):
        raise ValueError(
            "Snapshots use different fingerprint schemes: "
            f"{left_scheme} vs {right_scheme}"
        )

    if (
        left_key
        and right_key
        and str(left_key) != str(right_key)
    ):
        raise ValueError(
            "Snapshots were pseudonymized with different "
            "fingerprint keys"
        )


def import_pgss_csv(
    path: Path,
    *,
    label: str = "snapshot",
    postgres_version: str | None = None,
    fingerprint_key: str | None = None,
    raw_queryid: bool = False,
    include_query_text: bool = False,
) -> dict[str, Any]:
    with path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = _REQUIRED - fields
        if missing:
            raise ValueError(
                "pg_stat_statements CSV is missing "
                "required column(s): "
                + ", ".join(sorted(missing))
            )
        stable_sql = (
            not raw_queryid
            and "query" in fields
        )
        rows = [
            _normalize_pgss_row(
                row,
                fingerprint_key=fingerprint_key,
                raw_queryid=raw_queryid,
                stable_sql=stable_sql,
                store_query_text=include_query_text,
            )
            for row in reader
        ]

    return {
        "label": label,
        "captured_at": _utc_now(),
        "postgres_version": postgres_version,
        "source": "pg_stat_statements_csv",
        **_fingerprint_metadata(
            fingerprint_key,
            raw_queryid=raw_queryid,
            stable_sql=stable_sql,
        ),
        "queries": rows,
    }


def capture_live(
    dsn: str,
    *,
    label: str = "snapshot",
    include_query_text: bool = False,
    fingerprint_key: str | None = None,
    raw_queryid: bool = False,
) -> dict[str, Any]:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Live capture requires the optional postgres "
            "dependency. Install with: "
            'pip install -e ".[postgres]"'
        ) from exc

    stable_sql = not raw_queryid
    query_column = (
        ", query"
        if stable_sql or include_query_text
        else ""
    )
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

    startup_tracking_disabled = False
    try:
        conn = psycopg.connect(
            dsn,
            autocommit=True,
            options="-c pg_stat_statements.track=none",
        )
        startup_tracking_disabled = True
    except Exception:
        # Some managed/limited roles cannot set the extension GUC at
        # connection startup. Fall back so capture still works, but the
        # resulting window remains explicitly uncertain: a later SET can
        # itself be counted before tracking is disabled.
        conn = psycopg.connect(
            dsn,
            autocommit=True,
        )

    with conn:
        with conn.cursor() as cur:
            self_tracking_disabled = startup_tracking_disabled
            if not startup_tracking_disabled:
                try:
                    cur.execute(
                        "SET pg_stat_statements.track = 'none'"
                    )
                except Exception:
                    pass

            cur.execute("SHOW server_version")
            version = str(cur.fetchone()[0])

            cur.execute(sql)
            names = [
                desc.name
                for desc in cur.description
            ]
            rows = [
                _normalize_pgss_row(
                    dict(zip(names, values)),
                    fingerprint_key=fingerprint_key,
                    raw_queryid=raw_queryid,
                    stable_sql=stable_sql,
                    store_query_text=include_query_text,
                )
                for values in cur.fetchall()
            ]

            cur.execute(
                "SELECT name, setting "
                "FROM pg_settings "
                "WHERE name = ANY(%s)",
                (_SETTINGS,),
            )
            settings = {
                str(name): str(setting)
                for name, setting in cur.fetchall()
            }

            stats_reset: str | None = None
            try:
                cur.execute(
                    "SELECT stats_reset "
                    "FROM pg_stat_statements_info"
                )
                value = cur.fetchone()[0]
                stats_reset = (
                    value.isoformat()
                    if hasattr(value, "isoformat")
                    else str(value)
                )
            except Exception:
                # Older/limited environments may not expose the info view.
                # The missing reset marker is retained as an explicit unknown
                # when a measurement window is derived.
                stats_reset = None

    return {
        "label": label,
        "captured_at": _utc_now(),
        "postgres_version": version,
        "source": "pg_stat_statements_live",
        "stats_reset": stats_reset,
        "capture_self_tracking_disabled": (
            self_tracking_disabled
        ),
        **_fingerprint_metadata(
            fingerprint_key,
            raw_queryid=raw_queryid,
            stable_sql=stable_sql,
        ),
        "settings": settings,
        "queries": rows,
    }


def _query_map(
    snapshot: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        str(q["fingerprint"]): q
        for q in snapshot.get("queries", [])
    }


def derive_window_snapshot(
    start: dict[str, Any],
    end: dict[str, Any],
    *,
    label: str = "window",
) -> dict[str, Any]:
    _assert_fingerprint_compatibility(
        start,
        end,
    )

    start_reset = start.get("stats_reset")
    end_reset = end.get("stats_reset")
    if (
        start_reset is not None
        and end_reset is not None
        and str(start_reset) != str(end_reset)
    ):
        raise ValueError(
            "pg_stat_statements was reset during the "
            "measurement window"
        )

    before = _query_map(start)
    after = _query_map(end)
    queries: list[dict[str, Any]] = []
    unknowns: list[str] = []

    if start_reset is None or end_reset is None:
        unknowns.append(
            "pg_stat_statements reset marker was unavailable "
            "for this measurement window"
        )

    self_tracking_clean = (
        start.get("capture_self_tracking_disabled") is True
        and end.get("capture_self_tracking_disabled") is True
    )
    if not self_tracking_clean:
        unknowns.append(
            "Capture-session queries may be present because "
            "self-tracking could not be disabled"
        )

    disappeared = sorted(
        set(before) - set(after)
    )
    if disappeared:
        unknowns.append(
            f"{len(disappeared)} baseline fingerprint(s) "
            "disappeared before the window ended"
        )

    counter_regressions = 0
    for fingerprint, end_row in after.items():
        start_row = before.get(fingerprint, {})

        end_calls = _to_float(
            end_row.get("calls")
        )
        start_calls = _to_float(
            start_row.get("calls")
        )
        end_total = _to_float(
            end_row.get("total_exec_time")
        )
        start_total = _to_float(
            start_row.get("total_exec_time")
        )
        end_rows = _to_float(
            end_row.get("rows")
        )
        start_rows = _to_float(
            start_row.get("rows")
        )

        delta_calls = end_calls - start_calls
        delta_total = end_total - start_total
        delta_rows = end_rows - start_rows

        if delta_calls < 0 or delta_total < 0:
            counter_regressions += 1
            continue
        if delta_calls <= 0:
            continue

        query: dict[str, Any] = {
            "fingerprint": fingerprint,
            "calls": round(delta_calls, 6),
            "total_exec_time": round(
                delta_total,
                6,
            ),
            "mean_ms": round(
                delta_total / delta_calls,
                6,
            ),
            "rows": round(
                max(delta_rows, 0.0),
                6,
            ),
        }

        # Query text remains opt-in. If both captures included it, retain the
        # end text for local diagnostics; otherwise no SQL text is created.
        if end_row.get("query"):
            query["query"] = end_row["query"]

        queries.append(query)

    if counter_regressions:
        unknowns.append(
            f"{counter_regressions} fingerprint counter(s) "
            "moved backwards and were excluded"
        )

    total_calls = sum(
        _to_float(q.get("calls"))
        for q in queries
    )
    total_exec_ms = sum(
        _to_float(q.get("total_exec_time"))
        for q in queries
    )

    valid = (
        not disappeared
        and counter_regressions == 0
        and start_reset is not None
        and end_reset is not None
        and self_tracking_clean
    )

    return {
        "label": label,
        "captured_at": end.get(
            "captured_at",
            _utc_now(),
        ),
        "window_start": start.get("captured_at"),
        "window_end": end.get("captured_at"),
        "postgres_version": end.get(
            "postgres_version"
        ),
        "source": "pg_stat_statements_window",
        "stats_reset": end_reset,
        "fingerprint_scheme": end.get(
            "fingerprint_scheme"
        ),
        "fingerprint_key_id": end.get(
            "fingerprint_key_id"
        ),
        "fingerprint_basis": end.get(
            "fingerprint_basis"
        ),
        "fingerprint_cross_version_stable": end.get(
            "fingerprint_cross_version_stable"
        ),
        "settings": end.get("settings") or {},
        "measurement_window_valid": valid,
        "window_unknowns": unknowns,
        "window_total_calls": round(
            total_calls,
            6,
        ),
        "window_total_exec_ms": round(
            total_exec_ms,
            6,
        ),
        "queries": queries,
    }


def _derive_workload_overlap(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> float | None:
    _assert_fingerprint_compatibility(
        baseline,
        candidate,
    )

    before = _query_map(baseline)
    after = _query_map(candidate)
    total_calls = sum(
        max(
            _to_float(q.get("calls")),
            0.0,
        )
        for q in before.values()
    )
    if total_calls <= 0:
        return None

    shared_calls = sum(
        max(
            _to_float(q.get("calls")),
            0.0,
        )
        for fingerprint, q in before.items()
        if fingerprint in after
    )
    return round(
        shared_calls / total_calls * 100.0,
        2,
    )


def _derive_environment_match(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> float | None:
    before = baseline.get("settings") or {}
    after = candidate.get("settings") or {}
    common = sorted(
        set(before) & set(after)
    )
    if not common:
        return None

    same = sum(
        1
        for key in common
        if str(before[key]) == str(after[key])
    )
    return round(
        same / len(common) * 100.0,
        2,
    )


def derive_environment_diffs(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []

    before_version = baseline.get(
        "postgres_version"
    )
    after_version = candidate.get(
        "postgres_version"
    )
    if (
        before_version not in (None, "")
        and after_version not in (None, "")
        and str(before_version)
        != str(after_version)
    ):
        diffs.append(
            {
                "factor": "postgres.version",
                "kind": "postgres_version",
                "baseline": str(
                    before_version
                ),
                "candidate": str(
                    after_version
                ),
            }
        )

    before_settings = (
        baseline.get("settings") or {}
    )
    after_settings = (
        candidate.get("settings") or {}
    )
    for key in sorted(
        set(before_settings)
        | set(after_settings)
    ):
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


def _measurement_window_valid(
    snapshot: dict[str, Any],
) -> bool | None:
    if "measurement_window_valid" in snapshot:
        return bool(
            snapshot[
                "measurement_window_valid"
            ]
        )
    source = str(
        snapshot.get("source", "")
    )
    if source.startswith(
        "pg_stat_statements_"
    ):
        return False
    return None


def build_assessment_payload(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    coverage: dict[str, Any] | None = None,
    experiments: (
        dict[str, Any]
        | list[dict[str, Any]]
        | None
    ) = None,
    thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _assert_fingerprint_compatibility(
        baseline,
        candidate,
    )

    baseline_window = _measurement_window_valid(
        baseline
    )
    candidate_window = _measurement_window_valid(
        candidate
    )

    if (
        baseline_window is None
        and candidate_window is None
    ):
        comparable_window: bool | None = None
    else:
        comparable_window = (
            baseline_window is True
            and candidate_window is True
        )

    additional_unknowns = list(
        baseline.get("window_unknowns") or []
    ) + list(
        candidate.get("window_unknowns") or []
    )

    baseline_version = baseline.get("postgres_version")
    candidate_version = candidate.get("postgres_version")
    versions_differ = (
        baseline_version not in (None, "")
        and candidate_version not in (None, "")
        and str(baseline_version) != str(candidate_version)
    )
    if versions_differ:
        stable_across_versions = (
            baseline.get(
                "fingerprint_cross_version_stable"
            ) is True
            and candidate.get(
                "fingerprint_cross_version_stable"
            ) is True
        )
        if not stable_across_versions:
            additional_unknowns.append(
                "Cross-version fingerprint stability is not "
                "verified for this PostgreSQL comparison"
            )

    derived_coverage: dict[str, Any] = {
        "workload_volume_pct": (
            _derive_workload_overlap(
                baseline,
                candidate,
            )
        ),
        "bind_value_diversity_pct": None,
        "write_workload_covered": None,
        "peak_concurrency_covered": None,
        "background_jobs_covered": None,
        "replay_failure_pct": None,
        "environment_match_pct": (
            _derive_environment_match(
                baseline,
                candidate,
            )
        ),
        "additional_unknowns": (
            additional_unknowns
        ),
    }
    if comparable_window is not None:
        derived_coverage[
            "measurement_window_valid"
        ] = comparable_window

    if coverage:
        derived_coverage.update(coverage)

    if isinstance(experiments, dict):
        experiment_rows = experiments.get(
            "experiments",
            [],
        )
    else:
        experiment_rows = (
            experiments or []
        )

    return {
        "baseline": baseline,
        "candidate": candidate,
        "coverage": derived_coverage,
        "environment_diffs": (
            derive_environment_diffs(
                baseline,
                candidate,
            )
        ),
        "experiments": experiment_rows,
        "thresholds": thresholds or {},
    }
