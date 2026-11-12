"""Auditoria adversarial 2026-11-11 — provas físicas:

1. `python - <argv>` mintava crédito: `-` é o script-programa lido do stdin,
   mas era tratado como flag — `-m pytest`/`test_*.py` no rabo (sys.argv
   inerte) virava runner visível. Um programa arbitrário que imprimia
   "10 passed" via stdin recebia credit_eligible=True.
"""
from __future__ import annotations

import subprocess
import sys
import unittest

from henke import audit_ledger
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


if __name__ == "__main__":
    unittest.main()
