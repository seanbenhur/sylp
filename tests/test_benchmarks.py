from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from benchmarks.score import _read_records, render_markdown, summarize

ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"


class BenchmarkPreparationTests(unittest.TestCase):
    def test_durability_scenarios_have_fixed_inputs_and_verifiers(self):
        manifest = json.loads((BENCHMARKS / "cases.json").read_text(encoding="utf-8"))
        scenarios = manifest["durability_scenarios"]
        self.assertEqual(
            {item["id"] for item in scenarios},
            {"retry-policy-clarification", "inventory-manual-compaction", "long-shell-recovery"},
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario["id"]):
                self.assertTrue((BENCHMARKS / scenario["project"]).is_dir())
                self.assertTrue((BENCHMARKS / scenario["verifier"]).is_file())
                for key in ("prompt", "followup", "setup_prompt", "task_prompt"):
                    if scenario.get(key):
                        self.assertTrue((BENCHMARKS / scenario[key]).is_file())

    def test_prepare_command_prepares_all_durability_scenarios(self):
        manifest = json.loads((BENCHMARKS / "cases.json").read_text(encoding="utf-8"))
        for scenario in manifest["durability_scenarios"]:
            with self.subTest(scenario=scenario["id"]), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, str(BENCHMARKS / "prepare.py"), scenario["id"], directory],
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"case={scenario['id']}", result.stdout)
                self.assertIn("baseline_commit=", result.stdout)
                self.assertIn("verifier_sha256=", result.stdout)
                self.assertTrue((Path(directory) / "repo" / ".git").is_dir())
                for key in ("prompt", "followup", "setup_prompt", "task_prompt"):
                    if scenario.get(key):
                        self.assertIn(f"{key}_sha256=", result.stdout)

    def test_long_shell_verifier_rejects_replayed_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / "build"
            build.mkdir()
            (build / "invocations.log").write_text("started pid=1\nstarted pid=2\n", encoding="utf-8")
            records = [
                {"id": f"item-{phase:03d}", "checksum": f"sha256:fixture-{phase:03d}"}
                for phase in range(1, 9)
            ]
            (build / "index.json").write_text(json.dumps(records), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(BENCHMARKS / "fixtures/long-shell/verify.py"), str(root)],
                text=True,
                capture_output=True,
                timeout=10,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("side effect was replayed", result.stderr)

    def test_scorecard_keeps_per_task_failures_and_omits_unmeasured_metrics(self):
        records = [
            {"task": "cache", "arm": "everrun", "run_number": 1, "acceptance_pass": True,
             "regression_pass": None, "recovery_pass": False, "wall_seconds": 12.0,
             "human_interventions": 2, "input_tokens": 100, "output_tokens": 20,
             "billed_cost_usd": None, "billing_path": "subscription"},
            {"task": "cache", "arm": "everrun", "run_number": 2, "acceptance_pass": False,
             "regression_pass": None, "recovery_pass": True, "wall_seconds": 14.0,
             "human_interventions": 0, "input_tokens": None, "output_tokens": None,
             "billed_cost_usd": None, "billing_path": "subscription"},
            {"task": "cache", "arm": "codex", "run_number": 1, "acceptance_pass": True,
             "regression_pass": True, "recovery_pass": None, "wall_seconds": 10.0,
             "human_interventions": 1},
        ]

        summary = summarize(records)
        markdown = render_markdown(summary)

        everrun = next(item for item in summary["task_results"] if item["arm"] == "everrun")
        self.assertEqual(everrun["metrics"]["acceptance_pass"], {"passed": 1, "measured": 2})
        self.assertEqual(everrun["metrics"]["regression_pass"], {"passed": 0, "measured": 0})
        self.assertIn("cache / everrun run 1: recovery_pass", markdown)
        self.assertIn("Acceptance | Regression | Recovery", markdown)
        self.assertIn("1/2 | n/a | 1/2", markdown)
        self.assertEqual(everrun["usage"]["input_tokens"], {"total": 100, "reported_runs": 1, "runs": 2})
        self.assertIn("100 (1/2 runs)", markdown)
        self.assertIn("0 (0/2 runs)", markdown)

    def test_scorecard_rejects_duplicate_and_incomplete_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.jsonl"
            record = {"task": "cache", "arm": "everrun", "run_number": 1, "acceptance_pass": True,
                      "wall_seconds": 1, "human_interventions": 0}
            path.write_text(json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate run record"):
                _read_records([path])
            path.write_text('{"task":"cache"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required fields"):
                _read_records([path])

    def test_scorecard_cli_writes_machine_readable_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records = root / "runs.jsonl"
            output = root / "summary.json"
            records.write_text(json.dumps({
                "task": "smoke", "arm": "everrun", "run_number": 1,
                "acceptance_pass": True, "wall_seconds": 1.5, "human_interventions": 0,
                "input_tokens": 10, "output_tokens": 5, "billed_cost_usd": 0.01,
                "billing_path": "api",
            }) + "\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(BENCHMARKS / "score.py"), str(records), "--json-out", str(output)],
                text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("smoke", result.stdout)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["runs"], 1)


if __name__ == "__main__":
    unittest.main()
