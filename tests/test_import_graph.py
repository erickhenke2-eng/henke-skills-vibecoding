"""Testes para scripts/import_graph.py (Wave 7)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.import_graph import build_import_graph


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestImportGraph(unittest.TestCase):
    def test_direct_and_reverse_closure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "pkg/mod_a.py", "import pkg.mod_b\n")
            _write(root, "pkg/mod_b.py", "X = 1\n")
            _write(root, "tests/test_a.py", "import pkg.mod_a\n")
            g = build_import_graph(root)
            self.assertIn("pkg/mod_a.py", g.direct)
            self.assertIn("pkg/mod_b.py", g.direct["pkg/mod_a.py"])
            self.assertIn("pkg/mod_a.py", g.direct["tests/test_a.py"])
            clo = g.reverse_closure(["pkg/mod_b.py"])
            self.assertIn("pkg/mod_a.py", clo)
            self.assertIn("tests/test_a.py", clo)

    def test_dynamic_import_marks_unresolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root, "dyn.py", "import importlib\nimportlib.import_module('x')\n")
            g = build_import_graph(root)
            self.assertIn("dyn.py", g.unresolved)


if __name__ == "__main__":
    unittest.main()
