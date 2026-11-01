#!/usr/bin/env python3
"""
Testes dos portões métricos assimilados (spec: docs/designs/portoes-metricos-ub.md).
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

MET = repo_root / "scripts" / "metricas"
SIGNALS = repo_root / "scripts" / "repo_signals.py"

from evals.canaries_metricas import run_all_metric_canaries
from scripts.repo_signals import complexity_py
from scripts.henke_router import route_request
import ast as _ast


def run(script, *args, input_text=None, cwd=None):
    res = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        capture_output=True, text=True, input=input_text, cwd=cwd,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


class TestMetricGates(unittest.TestCase):

    def test_canary_battery_passes(self):
        passed, total, reports = run_all_metric_canaries()
        self.assertEqual(passed, total, f"Canários falharam: {reports}")

    def test_crap_formula(self):
        # CRAP(10, 50) = 100 * 0.125 + 10 = 22.5 → breach no threshold 6
        code, _ = run(MET / "crap_score.py", "--threshold", "6", input_text="f\t10\t50\n")
        self.assertEqual(code, 1)

    def test_crap_malformed_is_not_verdict(self):
        code, _ = run(MET / "crap_score.py", input_text="nan\tabc\n")
        self.assertEqual(code, 2)

    def test_tornado_requires_declared_order(self):
        code, _ = run(MET / "tornado_scan.py", input_text="c1\t2\nc2\t3\n")
        self.assertEqual(code, 2)

    def test_fence_fails_closed_unmapped_endpoint(self):
        with tempfile.TemporaryDirectory() as td:
            t = Path(td)
            fence = t / "f.json"
            fence.write_text(json.dumps({"layers": ["a", "b"], "modules": {"x": "a", "y": "b"}}))
            edges = t / "e.json"
            edges.write_text(json.dumps([{"from": "x/1.py", "to": "z/fora.py"}]))
            code, _ = run(MET / "fence_check.py", fence, edges)
            self.assertIn(code, (1, 2))  # reprova ou fail-closed, nunca verde

    def test_assertless_mock_only_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_mock.py"
            f.write_text(
                "import unittest.mock as m\n"
                "def test_x():\n"
                "    d = m.Mock()\n"
                "    d.foo()\n"
                "    d.foo.assert_called_once()\n", encoding="utf-8")
            code, out = run(MET / "assertless_scan.py", f)
            self.assertEqual(code, 1)
            self.assertIn("MOCK-ONLY", out)

    def test_complexity_metric_counts_decisions(self):
        src = "def f(x):\n    if x:\n        return 1\n    return 0\n"
        fn = _ast.parse(src).body[0]
        self.assertEqual(complexity_py(fn), 2)

    def test_metrics_marks_missing_coverage_as_zero(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.py"
            p.write_text("def f():\n    return 1\n", encoding="utf-8")
            code, out = run(SIGNALS, "metrics", "--root", td)
            self.assertEqual(code, 0)
            self.assertIn("\t0\n", out + "\n") or self.assertIn("\t0", out)

    def test_edges_ignores_external_imports(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "a.py").write_text("import os\nimport sys\n", encoding="utf-8")
            code, out = run(SIGNALS, "edges", "--root", td)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out), [])

    def test_router_selects_carpinteiro_and_respects_zero_match(self):
        res_exec = route_request("faz uma mudança cirúrgica e alinha o contrato front e back")
        self.assertEqual(res_exec["mode"], "EXECUTAR")
        self.assertEqual(res_exec["archetype"], "CARPINTEIRO")
        names = [s["name"] for s in res_exec["skills"]]
        self.assertTrue("henke-code-cirurgico" in names or "henke-code-ponte" in names)
        res_zero = route_request("quantos planetas existem no sistema solar?")
        self.assertEqual(res_zero["confidence"], "none")
        self.assertEqual(res_zero["skills"], [])

    def test_wave2_spec_contract_closure_passes(self):
        spec_w2 = repo_root / "docs" / "designs" / "wave2-cloudflare-nvidia-tob.md"
        code, out = run(MET / "contract_closure.py", "--spec", spec_w2, "--repo", repo_root)
        self.assertEqual(code, 0, out)
        spec_w25 = repo_root / "docs" / "designs" / "wave25-pentest-multicloud-53skills.md"
        code25, out25 = run(MET / "contract_closure.py", "--spec", spec_w25, "--repo", repo_root)
        self.assertEqual(code25, 0, out25)
        res_pt = route_request("faz um pentest e red team local nas rotas")
        self.assertEqual(res_pt["skills"][0]["name"], "henke-audit-pentest")
        self.assertEqual(res_pt["unrouted_catalog_skills"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
