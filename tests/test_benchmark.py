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


def test_workload_oracle_blocks_false_clearance(tmp_path: Path):
    suite = {
        "name": "workload-clearance-unit",
        "cases": [
            {
                "id": "hidden-regression",
                "payload": {
                    "baseline": {
                        "queries": [
                            {
                                "fingerprint": "hidden",
                                "calls": 30,
                                "mean_ms": 10.0,
                            },
                            {
                                "fingerprint": "stable",
                                "calls": 70,
                                "mean_ms": 10.0,
                            },
                        ]
                    },
                    "candidate": {
                        "queries": [
                            {
                                "fingerprint": "stable",
                                "calls": 70,
                                "mean_ms": 10.0,
                            }
                        ]
                    },
                    "coverage": {
                        "workload_volume_pct": 100,
                        "bind_value_diversity_pct": 100,
                        "write_workload_covered": True,
                        "peak_concurrency_covered": True,
                        "background_jobs_covered": True,
                        "replay_failure_pct": 0,
                        "environment_match_pct": 100,
                    },
                    "environment_diffs": [],
                    "experiments": [],
                    "thresholds": {
                        "regression_ratio": 1.25,
                        "min_delta_ms": 5,
                    },
                },
                "oracle": {
                    "workload": {
                        "expected_regressions": {},
                        "expected_stable": ["stable"],
                        "hidden_regressions": ["hidden"],
                        "max_observed_workload_overlap_pct": 70.0,
                        "max_evidence_strength": "MEDIUM",
                        "must_not_clear": True,
                    }
                },
            }
        ],
    }
    path = tmp_path / "workload.json"
    path.write_text(json.dumps(suite), encoding="utf-8")

    report = run_blind_benchmark(path)

    assert strict_pass(report), report
    assert report["metrics"]["false_clearance_events"] == 0
    assert report["cases"][0]["observed_workload_overlap_pct"] == 70.0
