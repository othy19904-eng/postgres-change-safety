import json
from pathlib import Path

from pgchangesafety.benchmark import run_blind_benchmark, strict_pass


ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmarks" / "blind_cases.json"


def test_oracle_is_separate_from_engine_payload():
    raw = json.loads(SUITE.read_text(encoding="utf-8"))
    for case in raw["cases"]:
        assert "oracle" in case
        assert "oracle" not in case["payload"]


def test_blind_benchmark_strict_gate():
    report = run_blind_benchmark(SUITE)
    assert strict_pass(report), report
    assert report["metrics"]["false_causal_attributions"] == 0
