"""Testes para scripts/test_impact.py (Wave 7)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import subprocess

from scripts.test_impact import (
    GitDiscoveryError, ImpactSelection, build_unittest_command,
    discover_changed_files, select_tests,
)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestSelectTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)
        for rel in ("scripts/prod.py", "tests/test_prod.py", "tests/test_other.py"):
            _write(self.repo, rel, "X = 1\n")
        self.direct = {
            "scripts/prod.py": frozenset(),
            "tests/test_prod.py": frozenset({"scripts/prod.py"}),
            "tests/test_other.py": frozenset(),
        }

    def _select(self, changed, deleted=frozenset(), unresolved=None):
        with patch("scripts.test_impact._discover",
                   return_value=(tuple(changed), frozenset(deleted))):
            return select_tests(self.repo, "BASE", self.direct, unresolved=unresolved)

    def test_no_changes(self):
        sel = self._select([])
        self.assertEqual(sel.mode, "selected")
        self.assertEqual(sel.test_files, ())
        self.assertIn("no_changes", sel.reasons)

    def test_changed_test_selects_itself(self):
        sel = self._select(["tests/test_prod.py"])
        self.assertEqual(sel.mode, "selected")
        self.assertEqual(sel.test_files, ("tests/test_prod.py",))

    def test_changed_prod_selects_importer(self):
        sel = self._select(["scripts/prod.py"])
        self.assertEqual(sel.mode, "selected")
        self.assertIn("tests/test_prod.py", sel.test_files)

    def test_unknown_prod_forces_full(self):
        sel = self._select(["scripts/unknown_new.py"])
        self.assertEqual(sel.mode, "full")
        self.assertTrue(any(r.startswith("unknown_or_unreachable") for r in sel.reasons))

    def test_non_python_forces_full(self):
        sel = self._select(["requirements.txt"])
        self.assertEqual(sel.mode, "full")

    def test_deletion_forces_full(self):
        sel = self._select(["tests/test_prod.py"], deleted={"tests/test_prod.py"})
        self.assertEqual(sel.mode, "full")

    def test_conftest_forces_full(self):
        sel = self._select(["tests/conftest.py"])
        self.assertEqual(sel.mode, "full")

    def test_unresolved_forces_full(self):
        sel = self._select(["tests/test_prod.py"],
                           unresolved={"x.py": ("dynamic_import",)})
        self.assertEqual(sel.mode, "full")
        self.assertIn("x.py", sel.unresolved_files)

    def test_dynamic_exec_test_forces_full(self):
        """subprocess com sys.executable em teste → incerteza → suíte full."""
        _write(self.repo, "tests/test_dyn.py",
               "import subprocess, sys\n"
               "def test_x():\n"
               "    subprocess.run([sys.executable, '-c', 'pass'])\n")
        sel = self._select(["tests/test_prod.py"])
        self.assertEqual(sel.mode, "full")
        self.assertTrue(any(r.startswith("dynamic_exec:") for r in sel.reasons),
                        f"reasons={sel.reasons}")

    def test_dynamic_loaders_force_full(self):
        """spec_from_file_location, runpy, exec e Popen(sys.executable) → full."""
        cases = [
            "import importlib.util\n"
            "importlib.util.spec_from_file_location('m', 'x.py')\n",
            "import runpy\nrunpy.run_module('mod')\n",
            "import runpy\nrunpy.run_path('x.py')\n",
            "exec(compile('x=1', '<s>', 'exec'))\n",
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, 'm.py'])\n",
            "import sys\nfrom subprocess import run\n"
            "run(sys.executable + ' -V', shell=True)\n",
        ]
        for body in cases:
            _write(self.repo, "tests/test_dyn.py", body)
            with self.subTest(snippet=body.strip().splitlines()[-1][:48]):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")

    def test_static_subprocess_without_interpreter_stays_selected(self):
        """subprocess sem sys.executable não dispara o fallback."""
        _write(self.repo, "tests/test_dyn.py",
               "import subprocess\n"
               "def test_x():\n"
               "    subprocess.run(['git', 'status'])\n")
        sel = self._select(["tests/test_prod.py"])
        self.assertEqual(sel.mode, "selected")
        self.assertEqual(sel.test_files, ("tests/test_prod.py",))

    def test_dynamic_exec_scan_survives_missing_tests_dir(self):
        """Repo sem tests/ → scan dinâmico retorna lista vazia, nunca None."""
        import shutil
        from scripts.test_impact import _dynamic_exec_test_files
        shutil.rmtree(self.repo / "tests")
        self.assertEqual(_dynamic_exec_test_files(self.repo), [])

    def test_unparseable_test_file_flags_dynamic(self):
        """Arquivo de teste que o AST não parseia → incerteza → full."""
        _write(self.repo, "tests/test_broken.py", "def broken(:\n")
        sel = self._select(["tests/test_prod.py"])
        self.assertEqual(sel.mode, "full")
        self.assertTrue(any(r.startswith("dynamic_exec:") for r in sel.reasons),
                        f"reasons={sel.reasons}")

    def test_dynamic_exec_predicates_return_exact_booleans(self):
        """Os predicados retornam bool exato — nunca None disfarçado."""
        import ast as _ast
        from scripts.test_impact import (
            _has_sys_executable, _test_uses_dynamic_exec,
        )
        _write(self.repo, "tests/test_static.py", "X = 1\n")
        self.assertIs(
            _test_uses_dynamic_exec(self.repo / "tests" / "test_static.py"),
            False)
        node = _ast.parse("x = sys.argv\n")
        self.assertIs(_has_sys_executable(node), False)

    def test_build_command_selected(self):
        sel = ImpactSelection("selected", ("a.py",), ("tests/test_prod.py",), (), ())
        cmd = build_unittest_command(sel, ROOT)
        self.assertIn("tests.test_prod", cmd)
        sel_full = ImpactSelection("full", (), (), (), ())
        cmd_full = build_unittest_command(sel_full, ROOT)
        self.assertIn("discover", cmd_full)


class TestDiscoverRealGit(unittest.TestCase):
    """Cobertura real do parser Git (sem mocks) — mata mutantes de _discover/_git."""

    def _repo(self, td: str) -> Path:
        repo = Path(td)
        for cmd in (["git", "init"], ["git", "config", "user.email", "t@t"],
                    ["git", "config", "user.name", "t"]):
            subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
        _write(repo, "a.py", "X=1\n")
        _write(repo, "b.py", "Y=1\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo,
                       capture_output=True, check=True)
        return repo

    def test_discover_modified_and_untracked(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            _write(repo, "a.py", "X=2\n")
            _write(repo, "c.py", "Z=1\n")
            changed = discover_changed_files(repo, "HEAD")
            self.assertIn("a.py", changed)
            self.assertIn("c.py", changed)
            self.assertNotIn("b.py", changed)

    def test_discover_deletion_reported_as_changed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            (repo / "a.py").unlink()
            changed = discover_changed_files(repo, "HEAD")
            self.assertIn("a.py", changed)

    def test_discover_no_changes(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(td)
            self.assertEqual(discover_changed_files(repo, "HEAD"), ())

    def test_git_failure_raises(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(GitDiscoveryError):
                discover_changed_files(Path(td), "HEAD")


if __name__ == "__main__":
    unittest.main()
