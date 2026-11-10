#!/usr/bin/env python3
"""Regressão P0: `check` JAMAIS retorna 0 quando um gate falha.

Caso-hostil do audit: repo contendo os.system → hazard_scan FAIL →
exit agregado obrigatoriamente != 0. Também prova o contraponto
(repo limpo → 0 ou N/A honesto) e o caminho JSON.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.henke_cli import main as henke_cli_main  # noqa: E402


def _run_check(repo: Path, *extra: str) -> tuple[int, str]:
    buf, ebuf = StringIO(), StringIO()
    with redirect_stdout(buf), redirect_stderr(ebuf):
        rc = henke_cli_main(["check", "--repo", str(repo), "--no-cache",
                             *extra])
    return rc, buf.getvalue() + ebuf.getvalue()


class TestCheckAggregation(unittest.TestCase):
    def test_hostile_repo_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="hostile_check_") as td:
            repo = Path(td)
            (repo / "evil.py").write_text('import os\nos.system("id")\n')
            rc, out = _run_check(repo)
            self.assertNotEqual(rc, 0,
                                f"check aprovou repo com os.system:\n{out}")
            self.assertIn("FAIL", out)

    def test_hostile_repo_json_not_passed(self):
        with tempfile.TemporaryDirectory(prefix="hostile_check_") as td:
            repo = Path(td)
            (repo / "evil.py").write_text('import os\nos.system("id")\n')
            buf = StringIO()
            with redirect_stdout(buf), redirect_stderr(StringIO()):
                rc = henke_cli_main(["check", "--repo", str(repo),
                                     "--no-cache", "--json"])
            self.assertNotEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertFalse(data["passed"])
            hz = [g for g in data["gates"] if g["gate"] == "hazard_scan"]
            self.assertTrue(hz and not hz[0]["passed"])

    def test_clean_repo_passes_or_na(self):
        with tempfile.TemporaryDirectory(prefix="clean_check_") as td:
            repo = Path(td)
            (repo / "app.py").write_text("x = 1\n")
            rc, out = _run_check(repo)
            self.assertEqual(rc, 0, f"check reprovou repo limpo:\n{out}")


if __name__ == "__main__":
    unittest.main()
