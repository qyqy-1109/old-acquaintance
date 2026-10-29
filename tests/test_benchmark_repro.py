"""Benchmark results must be reproducible and never silently lose cases."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
PYTHON = sys.executable


class BenchmarkReproTests(unittest.TestCase):
    def test_persona_identification_order_is_stable_across_processes(self):
        program = """
import sys
sys.path.insert(0, "benchmark")
import run_persona
run_persona._ask_json = lambda service, prompt: ({"pick": "1"}, "")
for index in range(12):
    case = {"id": f"PERS{index:02d}", "probe": "测试", "quote": "她", "expect": "独立"}
    result = run_persona._identify(None, case, "她的回答", ["对照甲", "对照乙"])
    print(result["answer_pos"])
"""
        outputs = []
        for seed in ("1", "2", "3"):
            env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
            result = subprocess.run([PYTHON, "-c", program], cwd=ROOT, env=env,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[1], outputs[2])

    def test_rescore_rejects_report_case_missing_from_current_definitions(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "old-report.json"
            out = Path(directory) / "rescored.json"
            report.write_text(json.dumps({"cases": [
                {"id": "REMOVED_CASE", "answer": "旧回答"}]}), encoding="utf-8")
            result = subprocess.run(
                [PYTHON, "benchmark/run_bench.py", "--rescore", str(report),
                 "--out", str(out)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
                env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("REMOVED_CASE", result.stdout + result.stderr)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
