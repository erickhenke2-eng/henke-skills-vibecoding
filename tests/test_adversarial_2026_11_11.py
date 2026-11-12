"""Auditoria adversarial 2026-11-11 — provas físicas:

1. `python - <argv>` mintava crédito: `-` é o script-programa lido do stdin,
   mas era tratado como flag — `-m pytest`/`test_*.py` no rabo (sys.argv
   inerte) virava runner visível. Um programa arbitrário que imprimia
   "10 passed" via stdin recebia credit_eligible=True.
2. parse_test_runner_output casava o PRIMEIRO 'N passed' (pytest/jest/
   canário): uma linha plantada antes do sumário real inflava discovered.
3. `npm --prefix/-C/--cwd/--dir <alvo> <script>` expandia o lifecycle do
   package.json da RAIZ — o pretest hostil do package.json do alvo ficava
   invisível à denylist.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from henke import audit_ledger
from henke.cli_exec_proc import parse_test_runner_output
from henke.exec_contract import evaluate_execution


class TestPythonStdinProgram(unittest.TestCase):
    """A1: `python -` executa o programa do STDIN — o que segue é argv,
    nunca runner. Antes, `-` pulava como flag e `-m pytest` vazava."""

    def test_python_dash_hides_argv_tail(self):
        self.assertFalse(audit_ledger.is_test_cmd("python - -m pytest"))
        self.assertFalse(audit_ledger.is_test_cmd("python -u - -m pytest"))
        self.assertFalse(audit_ledger.is_test_cmd(
            f"{sys.executable} - -m pytest"))
        self.assertFalse(audit_ledger.is_test_cmd("python - pytest"))

    def test_legit_forms_still_tests(self):
        self.assertTrue(audit_ledger.is_test_cmd("python -m pytest"))
        self.assertTrue(audit_ledger.is_test_cmd("python tests/test_x.py"))
        self.assertTrue(audit_ledger.is_test_cmd("python -u -m pytest"))

    def test_stdin_program_does_not_mint_credit(self):
        # Execução REAL: programa vindo do stdin imprime '10 passed';
        # '-m pytest' é apenas sys.argv inerte.
        p = subprocess.run(
            [sys.executable, "-", "-m", "pytest"],
            input="print('10 passed')\n",
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(p.returncode, 0)
        self.assertIn("10 passed", p.stdout)
        v = evaluate_execution(
            f"{sys.executable} - -m pytest", p.returncode, p.stdout, p.stderr)
        self.assertFalse(v.is_test)
        self.assertFalse(v.credit_eligible)


class TestLastSummaryMatch(unittest.TestCase):
    """A2: contadores de saída usam o ÚLTIMO sumário — linha 'N passed'
    plantada no início não infla discovered/passed."""

    def test_pytest_planted_summary_ignored(self):
        d, p = parse_test_runner_output(
            "planted: 999 passed\n===== 3 passed in 0.01s =====", "")
        self.assertEqual((d, p), (3, 3))

    def test_jest_planted_summary_ignored(self):
        d, p = parse_test_runner_output(
            "Tests: 999 passed\nTests: 1 failed, 4 passed, 5 total", "")
        self.assertEqual((d, p), (5, 4))

    def test_canary_planted_score_ignored(self):
        d, p = parse_test_runner_output(
            "85/85 canários aprovados\n2/85 canários aprovados", "")
        self.assertEqual((d, p), (85, 2))

    def test_clean_output_unchanged(self):
        d, p = parse_test_runner_output("===== 42 passed in 1.2s =====", "")
        self.assertEqual((d, p), (42, 42))
        d, p = parse_test_runner_output("Tests: 6 passed, 6 total", "")
        self.assertEqual((d, p), (6, 6))


class TestNpmDirRedirect(unittest.TestCase):
    """A3: --prefix/-C/--cwd/--dir redirecionam o lifecycle para o
    package.json do ALVO — o da raiz não é autoridade para esses hooks."""

    def _mkrepo(self, td: str) -> tuple[Path, dict]:
        root = Path(td)
        (root / "package.json").write_text(
            json.dumps({"scripts": {"test": "echo ok"}}), encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "package.json").write_text(json.dumps({
            "name": "sub", "scripts": {
                "pretest": "rm -rf /tmp/x", "test": "echo ok"}}),
            encoding="utf-8")
        pkg_root = json.loads((root / "package.json").read_text())
        return root, pkg_root

    def test_prefix_target_package_audited(self):
        with tempfile.TemporaryDirectory() as td:
            root, pkg_root = self._mkrepo(td)
            for flag in ("--prefix sub", "--prefix=sub", "-C sub",
                         "--cwd sub"):
                cmd = f"npm {flag} test"
                ledger = [{"cmd": cmd, "exit": 0, "testes_descobertos": 5,
                           "stdout_sha256": "a" * 64}]
                ok, errors, _ = audit_ledger.audit_ledger_content(
                    ledger, pkg=pkg_root, pkg_dir=root)
                self.assertFalse(ok, f"{cmd!r} passou limpo")
                self.assertTrue(
                    any("denylist" in e for e in errors),
                    f"{cmd!r}: {errors}")

    def test_unresolvable_prefix_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root, pkg_root = self._mkrepo(td)
            ledger = [{"cmd": "npm --prefix missing test", "exit": 0,
                       "testes_descobertos": 5, "stdout_sha256": "a" * 64}]
            ok, errors, _ = audit_ledger.audit_ledger_content(
                ledger, pkg=pkg_root, pkg_dir=root)
            self.assertFalse(ok)
            self.assertTrue(any("lifecycle" in e for e in errors), errors)

    def test_workspace_name_resolves_hooks(self):
        with tempfile.TemporaryDirectory() as td:
            root, pkg_root = self._mkrepo(td)
            pkg_root["workspaces"] = ["sub"]
            ledger = [{"cmd": "npm -w sub test", "exit": 0,
                       "testes_descobertos": 5, "stdout_sha256": "a" * 64}]
            ok, errors, _ = audit_ledger.audit_ledger_content(
                ledger, pkg=pkg_root, pkg_dir=root)
            self.assertFalse(ok, "npm -w sub test passou limpo")
            self.assertTrue(any("denylist" in e for e in errors), errors)

    def test_pnpm_dash_w_boolean_tolerated(self):
        # pnpm '-w' é booleano (workspace-ROOT), não leva nome — não pode
        # falso-flaggear `pnpm -w test`.
        with tempfile.TemporaryDirectory() as td:
            root, pkg_root = self._mkrepo(td)
            ledger = [{"cmd": "pnpm -w test", "exit": 0,
                       "testes_descobertos": 5, "stdout_sha256": "a" * 64}]
            ok, errors, _ = audit_ledger.audit_ledger_content(
                ledger, pkg=pkg_root, pkg_dir=root)
            self.assertTrue(ok, errors)

    def test_no_flag_root_pkg_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root, pkg_root = self._mkrepo(td)
            ledger = [{"cmd": "npm test", "exit": 0,
                       "testes_descobertos": 5, "stdout_sha256": "a" * 64}]
            ok, errors, _ = audit_ledger.audit_ledger_content(
                ledger, pkg=pkg_root, pkg_dir=root)
            self.assertTrue(ok, errors)


if __name__ == "__main__":
    unittest.main()
