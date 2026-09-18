from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .engine import assess


def _render_text(result: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("PostgreSQL Change Safety Assessment")
    lines.append("=" * 36)
    lines.append(f"Evidence strength: {result['evidence_strength']} ({result['evidence_score']}/100)")
    lines.append(f"Decision coverage: {result['decision_coverage_score']}/100")
    lines.append(f"Causal resolution rate: {result['causal_resolution_rate'] * 100:.1f}%")
    lines.append("")

    regs = result["regressions"]
    if not regs:
        lines.append("Regressions detected: none above configured threshold")
    else:
        lines.append(f"Regressions detected: {len(regs)}")
        for r in regs:
            cause = r["cause"] if r["cause_status"] != "UNKNOWN" else "UNKNOWN"
            lines.append(
                f"- {r['fingerprint']}: {r['baseline_ms']}ms -> {r['candidate_ms']}ms "
                f"({r['ratio']}x), severity={r['severity']}, cause={cause}, "
                f"confidence={r['cause_confidence']}"
            )

    lines.append("")
    lines.append("Known unknowns:")
    if result["known_unknowns"]:
        for item in result["known_unknowns"]:
            lines.append(f"- {item}")
    else:
        lines.append("- none declared by the supplied coverage data")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pgchangesafe",
        description="Assess PostgreSQL change regressions, causal evidence, and decision coverage.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    assess_cmd = sub.add_parser("assess", help="Assess a JSON comparison payload")
    assess_cmd.add_argument("input", type=Path)
    assess_cmd.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()

    if args.command == "assess":
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        result = assess(payload).to_dict()
        if args.format == "json":
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(_render_text(result))


if __name__ == "__main__":
    main()
