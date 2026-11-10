#!/usr/bin/env python3
"""P0-02/P1-02: contrato tipado de evidência de execução (exec_contract)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from scripts.exec_contract import evaluate_execution  # noqa: E402


class TestNotTestCommand(unittest.TestCase):
    def test_echo_cannot_mint(self):
        """`echo "10 passed"` jamais vira evidência (DS-1)."""
        v = evaluate_execution('echo "10 passed"', 0, "10 passed\n", "")
        self.assertFalse(v.is_test)
        self.assertEqual(v.discovered, 0)
        self.assertEqual(v.passed, 0)
        self.assertFalse(v.credit_eligible)
        self.assertEqual(v.reason, "not_test_command")

    def test_cat_readme_cannot_mint(self):
        v = evaluate_execution("cat results.txt", 0,
                               "tests: 50, failures: 0\n", "")
        self.assertFalse(v.credit_eligible)
        self.assertEqual(v.discovered, 0)


class TestRunnerVerdicts(unittest.TestCase):
    def test_clean_unittest_run(self):
        v = evaluate_execution(
            "python -m unittest discover -s tests", 0,
            ".....\n----------------------------------------------------------------------\n"
            "Ran 5 tests in 0.01s\n\nOK\n", "")
        self.assertTrue(v.is_test)
        self.assertEqual(v.discovered, 5)
        self.assertEqual(v.passed, 5)
        self.assertTrue(v.credit_eligible)
        self.assertEqual(v.evidence_source, "stdout_parse")

    def test_zero_tests_never_green(self):
        v = evaluate_execution("python -m unittest discover -s tests", 0,
                               "Ran 0 tests in 0.00s\n\nOK\n", "")
        self.assertTrue(v.is_test)
        self.assertFalse(v.credit_eligible)
        self.assertEqual(v.reason, "zero_tests_discovered")

    def test_rc_mismatch_zeroed(self):
        """w7: exit≠0 com 'todos passaram' = forja — zera e nega crédito."""
        v = evaluate_execution(
            "python -m pytest -q", 1,
            "10 passed in 2.0s\n", "")
        self.assertTrue(v.is_test)
        self.assertEqual(v.passed, 0)
        self.assertFalse(v.credit_eligible)
        self.assertEqual(v.reason, "rc_mismatch_zeroed")

    def test_partial_failures_recorded(self):
        v = evaluate_execution(
            "python -m pytest -q", 1,
            "7 passed, 3 failed in 2.0s\n", "")
        self.assertTrue(v.is_test)
        self.assertEqual(v.discovered, 10)
        self.assertEqual(v.passed, 7)
        self.assertTrue(v.credit_eligible)

    def test_impossible_scoreboard(self):
        """passed>discovered é parse adulterado — nega crédito."""
        v = evaluate_execution(
            "python -m unittest discover -s tests", 0,
            "Ran 3 tests\n\nOK\n", "")
        if v.discovered and v.passed > v.discovered:
            self.fail("parser produziu placar impossível")
        self.assertFalse(v.passed > v.discovered)

    def test_verdict_immutable(self):
        v = evaluate_execution("python -m pytest -q", 0, "3 passed\n", "")
        with self.assertRaises(Exception):
            v.passed = 99  # type: ignore[misc]


class TestCliAutoConsistency(unittest.TestCase):
    def test_same_contract_module_for_both(self):
        """henke_cli e henke_auto resolvem o MESMO evaluate_execution."""
        import scripts.exec_contract as ec
        self.assertTrue(callable(ec.evaluate_execution))
        # henke_auto chama via _load_module('exec_contract') → scripts.exec_contract
        import importlib
        mod = importlib.import_module("scripts.exec_contract")
        self.assertIs(mod.evaluate_execution, ec.evaluate_execution)


if __name__ == "__main__":
    unittest.main()
