from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .benchmark import render_benchmark, run_blind_benchmark, strict_pass
from .engine import assess
from .snapshot import (
    build_assessment_payload,
    capture_live,
    import_pgss_csv,
)


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _render_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("PostgreSQL Change Safety Assessment")
    lines.append("=" * 36)
    lines.append(
        f"Evidence strength: {result['evidence_strength']} "
        f"({result['evidence_score']}/100)"
    )
    lines.append(f"Decision coverage: {result['decision_coverage_score']}/100")
    overlap = result.get("observed_workload_overlap_pct")
    lines.append(
        "Observed workload overlap: "
        + ("unknown" if overlap is None else f"{overlap}%")
    )
    lines.append(
        f"Causal resolution rate: {result['causal_resolution_rate'] * 100:.1f}%"
    )
    lines.append("")

    diffs = result.get("environment_diffs", [])
    lines.append("Environment / configuration diffs:")
    if diffs:
        for item in diffs:
            lines.append(
                f"- {item['factor']}: {item.get('baseline')} -> {item.get('candidate')}"
            )
    else:
        lines.append("- none observed in captured fields")

    lines.append("")
    regs = result["regressions"]
    if not regs:
        lines.append("Regressions detected: none above configured threshold")
    else:
        lines.append(f"Regressions detected: {len(regs)}")
        for r in regs:
            cause = r["cause"] if r["cause_status"] != "UNKNOWN" else "UNKNOWN"
            lines.append(
                f"- {r['fingerprint']}: {r['baseline_ms']}ms -> "
                f"{r['candidate_ms']}ms ({r['ratio']}x), "
                f"workload={r['workload_share_pct']}%, severity={r['severity']}, "
                f"cause={cause}, confidence={r['cause_confidence']}, "
                f"trials={r['supporting_trials']}"
            )
            if r.get("unresolved_confounders"):
                lines.append(
                    "  unresolved confounders: "
                    + ", ".join(r["unresolved_confounders"])
                )

    lines.append("")
    lines.append("Known unknowns:")
    if result["known_unknowns"]:
        for item in result["known_unknowns"]:
            lines.append(f"- {item}")
    else:
        lines.append("- none declared by the supplied coverage data")

    lines.append("")
    lines.append("Unresolved confounders:")
    if result.get("unresolved_confounders"):
        for item in result["unresolved_confounders"]:
            lines.append(f"- {item}")
    else:
        lines.append("- none among captured environment diffs")

    return "\n".join(lines)


def _emit(result: dict[str, Any], fmt: str, output: Path | None) -> None:
    if fmt == "json":
        rendered = json.dumps(result, indent=2, sort_keys=True)
    else:
        rendered = _render_text(result)

    if output is None:
        print(rendered)
    else:
        output.write_text(rendered + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pgchangesafe",
        description=(
            "Assess PostgreSQL change regressions, causal evidence, "
            "and decision coverage."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    assess_cmd = sub.add_parser(
        "assess",
        help="Assess a complete JSON comparison payload",
    )
    assess_cmd.add_argument("input", type=Path)
    assess_cmd.add_argument("--format", choices=["text", "json"], default="text")
    assess_cmd.add_argument("--output", type=Path)

    import_cmd = sub.add_parser(
        "import-pgss",
        help="Convert pg_stat_statements CSV into a normalized snapshot",
    )
    import_cmd.add_argument("csv", type=Path)
    import_cmd.add_argument("--output", type=Path, required=True)
    import_cmd.add_argument("--label", default="snapshot")
    import_cmd.add_argument("--postgres-version")

    capture_cmd = sub.add_parser(
        "capture",
        help="Capture a live pg_stat_statements snapshot (optional postgres extra)",
    )
    capture_cmd.add_argument(
        "--dsn",
        default=os.getenv("PGCHANGE_DSN"),
        help="PostgreSQL DSN. Prefer PGCHANGE_DSN to avoid shell-history leakage.",
    )
    capture_cmd.add_argument("--output", type=Path, required=True)
    capture_cmd.add_argument("--label", default="snapshot")
    capture_cmd.add_argument(
        "--include-query-text",
        action="store_true",
        help="Include normalized query text. Off by default for privacy.",
    )

    compare_cmd = sub.add_parser(
        "compare",
        help="Compare two normalized snapshots and produce a conservative assessment",
    )
    compare_cmd.add_argument("baseline", type=Path)
    compare_cmd.add_argument("candidate", type=Path)
    compare_cmd.add_argument("--coverage", type=Path)
    compare_cmd.add_argument("--experiments", type=Path)
    compare_cmd.add_argument("--thresholds", type=Path)
    compare_cmd.add_argument("--format", choices=["text", "json"], default="text")
    compare_cmd.add_argument("--output", type=Path)

    benchmark_cmd = sub.add_parser(
        "benchmark",
        help="Run a blind planted-regression benchmark suite",
    )
    benchmark_cmd.add_argument(
        "suite",
        type=Path,
        nargs="?",
        default=Path("benchmarks/blind_cases.json"),
    )
    benchmark_cmd.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless every blind case passes with zero false causal attributions or false-clearance events.",
    )
    benchmark_cmd.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "assess":
        payload = _load_json(args.input)
        _emit(assess(payload).to_dict(), args.format, args.output)
        return

    if args.command == "import-pgss":
        snapshot = import_pgss_csv(
            args.csv,
            label=args.label,
            postgres_version=args.postgres_version,
        )
        _write_json(args.output, snapshot)
        print(f"Wrote {len(snapshot['queries'])} query fingerprints to {args.output}")
        return

    if args.command == "capture":
        if not args.dsn:
            parser.error("capture requires --dsn or PGCHANGE_DSN")
        snapshot = capture_live(
            args.dsn,
            label=args.label,
            include_query_text=args.include_query_text,
        )
        _write_json(args.output, snapshot)
        print(f"Wrote {len(snapshot['queries'])} query fingerprints to {args.output}")
        return

    if args.command == "compare":
        baseline = _load_json(args.baseline)
        candidate = _load_json(args.candidate)
        payload = build_assessment_payload(
            baseline,
            candidate,
            coverage=_load_json(args.coverage),
            experiments=_load_json(args.experiments),
            thresholds=_load_json(args.thresholds),
        )
        _emit(assess(payload).to_dict(), args.format, args.output)
        return

    if args.command == "benchmark":
        report = run_blind_benchmark(args.suite)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(render_benchmark(report))
        if args.strict and not strict_pass(report):
            raise SystemExit(1)
        return


if __name__ == "__main__":
    main()
