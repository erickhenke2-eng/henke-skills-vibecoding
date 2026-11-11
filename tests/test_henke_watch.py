"""Testes para henke/henke_watch.py (Wave 7)."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from henke.gate_parallel import GateResult
from henke.henke_watch import WatchConfig, WatchController, _snapshot


def _result(name="g", code=0, passed=True, stdout="ok"):
    return GateResult(name=name, exit_code=code, stdout=stdout, stderr="",
                      passed=passed, cached=False, timed_out=False,
                      duration_ms=1, output_truncated=False,
                      output_error=False)


class TestWatchSnapshot(unittest.TestCase):
    def test_snapshot_detects_change(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            f = repo / "a.py"
            f.write_text("X=1\n", encoding="utf-8")
            s1 = _snapshot(repo)
            self.assertIsNotNone(s1)
            f.write_text("X=2222222\n", encoding="utf-8")
            s2 = _snapshot(repo)
            self.assertIsNotNone(s2)
            self.assertNotEqual(s1, s2)

    def test_gate_specs_include_three_universal(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "tests").mkdir()
            ctl = WatchController(WatchConfig(repo_root=repo))
            names = [s.name for s in ctl._gate_specs()]
            self.assertIn("hazard_scan", names)
            self.assertIn("assertless_scan", names)
            self.assertIn("a11y_scan", names)

    def test_quiet_suppresses_only_identical_green(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            ctl = WatchController(WatchConfig(repo_root=repo, quiet=True))
            ok = [_result()]
            buf = io.StringIO()
            with redirect_stdout(buf):
                ctl._print_result(ok, "h1")
                ctl._print_result(ok, "h1")
            self.assertEqual(buf.getvalue().count("verde"), 1)
            with redirect_stdout(buf):
                ctl._print_result([_result(code=1, passed=False)], "")
            self.assertIn("FAIL", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
