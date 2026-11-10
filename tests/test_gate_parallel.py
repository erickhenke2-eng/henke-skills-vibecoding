"""Testes para scripts/gate_parallel.py (Wave 7)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.gate_parallel import (
    GateSpec, aggregate_exit_code, run_gates_parallel,
)


class TestGateParallel(unittest.TestCase):
    def test_order_timeout_and_aggregate(self):
        specs = [
            GateSpec("ok1", (sys.executable, "-c", "print('a')"), ROOT,
                     timeout_sec=10.0),
            GateSpec("fail", (sys.executable, "-c", "import sys; sys.exit(1)"),
                     ROOT, timeout_sec=10.0),
            GateSpec("tout", (sys.executable, "-c", "import time; time.sleep(5)"),
                     ROOT, timeout_sec=0.5),
            GateSpec("ok2", (sys.executable, "-c", "print('b')"), ROOT,
                     timeout_sec=10.0),
        ]
        results = run_gates_parallel(specs, max_parallel=4)
        self.assertEqual([r.name for r in results],
                         ["ok1", "fail", "tout", "ok2"])
        self.assertTrue(results[0].passed)
        self.assertEqual(results[1].exit_code, 1)
        self.assertEqual(results[2].exit_code, 124)
        self.assertTrue(results[2].timed_out)
        self.assertEqual(aggregate_exit_code(results), 1)

    def test_aggregate_error_two(self):
        specs = [
            GateSpec("err", (sys.executable, "-c", "import sys; sys.exit(2)"),
                     ROOT, timeout_sec=10.0),
        ]
        results = run_gates_parallel(specs, max_parallel=1)
        self.assertEqual(aggregate_exit_code(results), 2)


if __name__ == "__main__":
    unittest.main()
