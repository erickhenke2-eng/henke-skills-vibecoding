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

    def test_static_subprocess_without_interpreter_forces_full(self):
        """subprocess em tests/ é incerteza MESMO sem sys.executable literal.

        Inversão exigida pela auditoria: `subprocess.run(['git','status'])`
        não prova que o teste não executa código fora do grafo — dentro de
        tests/ qualquer import/uso de subprocess força a suíte completa.
        """
        _write(self.repo, "tests/test_dyn.py",
               "import subprocess\n"
               "def test_x():\n"
               "    subprocess.run(['git', 'status'])\n")
        sel = self._select(["tests/test_prod.py"])
        self.assertEqual(sel.mode, "full")
        self.assertTrue(any(r.startswith("dynamic_exec:") for r in sel.reasons),
                        f"reasons={sel.reasons}")

    def test_loose_name_dynamic_loader_forces_full(self):
        """PoC A: loader por nome solto (ast.Name) é detectado.

        `from importlib.util import spec_from_file_location` chamado pelo
        nome importado — sem o qualificador importlib.util — também força
        a suíte completa.
        """
        for body in (
            "from importlib.util import spec_from_file_location\n"
            "spec_from_file_location('m', 'x.py')\n",
            "from runpy import run_module\n"
            "run_module('mod')\n",
            "from importlib import import_module\n"
            "import_module('pkg.mod')\n",
            "spec_from_file_location('m', 'x.py')  # nome solto sem import\n",
        ):
            _write(self.repo, "tests/test_dyn.py", body)
            with self.subTest(snippet=body.strip().splitlines()[-1][:48]):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")

    def test_sys_executable_alias_forces_full(self):
        """PoC B: `PY = sys.executable; run([PY, ...])` é detectado."""
        for body in (
            "import sys\nfrom subprocess import run\n"
            "PY = sys.executable\nrun([PY, '-V'])\n",
            "import sys\nPY = sys.executable\n"
            "Popen([PY, 'm.py'])  # spawn sem import explícito de subprocess\n",
        ):
            _write(self.repo, "tests/test_dyn.py", body)
            with self.subTest(snippet=body.strip().splitlines()[-1][:48]):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")

    def test_helper_py_under_tests_scanned_for_dynamic_exec(self):
        """PoC C: helpers sob tests/ (não test_*.py) também são varridos."""
        for rel in ("tests/helpers.py", "tests/e2e/e2e_common.py"):
            _write(self.repo, rel,
                   "import subprocess\n"
                   "def go():\n"
                   "    subprocess.call(['echo', 'hi'])\n")
            with self.subTest(helper=rel):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")
                self.assertTrue(
                    any(r == f"dynamic_exec:{rel}" for r in sel.reasons),
                    f"reasons={sel.reasons}")
            (self.repo / rel).unlink()

    def test_dynamic_module_import_alone_forces_full(self):
        """PoC D: import pelado de módulos de execução já é incerteza."""
        for body in (
            "import subprocess\nX = 1\n",
            "import subprocess as sp\nX = 1\n",
            "import runpy\nX = 1\n",
            "import multiprocessing\nX = 1\n",
            "import os\nos.system('echo hi')\n",
            "import os as o\no.system('echo hi')\n",
            "from os import system\nsystem('echo hi')\n",
            "from os import system\nX = 1\n",
            "import os\nos.execv('/bin/x', [])\n",
            "import importlib\nX = 1\n",
            "import importlib.util\nX = 1\n",
            "from importlib import import_module\nX = 1\n",
            "import asyncio\nX = 1\n",
            "import asyncio.subprocess\nX = 1\n",
            "from asyncio import create_subprocess_exec\nX = 1\n",
            # Bare-name sem import delator (from os import * / sombra local):
            # o chamador, não o import, é a prova conservadora.
            "system('echo hi')\n",
            "execv('/bin/x', [])\n",
        ):
            _write(self.repo, "tests/test_dyn.py", body)
            with self.subTest(snippet=body.strip().splitlines()[0][:48]):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")

    def test_dynamic_exec_in_prod_closure_forces_full(self):
        """PoC R3-A: produção alcançada pelo grafo com execução dinâmica.

        Um loader de plugins/runner em produção invisível ao grafo torna a
        seleção não-probante — o fechamento, não só tests/, é varrido.
        """
        cases = [
            "import importlib\ngetattr(importlib, 'import_module')('x')\n",
            "run = exec\nrun('x=1')\n",
            "from importlib.machinery import SourceFileLoader\n"
            "SourceFileLoader('m', 'x.py')\n",
            "import asyncio\nasyncio.create_subprocess_exec('x')\n",
            "import importlib\nimportlib.import_module('x')\n",
            "loader.exec_module(mod)\n",
            "imp.load_module('m')\n",
            "util.spec_from_file_location('m', 'x.py')\n",
        ]
        for body in cases:
            _write(self.repo, "scripts/prod.py", body)
            with self.subTest(snippet=body.strip().splitlines()[0][:48]):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")
                self.assertTrue(
                    any(r == "dynamic_exec:scripts/prod.py"
                        for r in sel.reasons),
                    f"reasons={sel.reasons}")
        _write(self.repo, "scripts/prod.py", "X = 1\n")

    def test_reference_only_dynamic_names_force_full(self):
        """PoC R3-B: referências em carga sem chamada direta flagam.

        `run = exec` (ast.Name Load), `handler = obj.exec_module`
        (ast.Attribute Load) e getattr com literal de string escapavam do
        filtro que só inspecionava ast.Call.
        """
        for body in (
            "run = exec\nX = 1\n",
            "handler = eval\nX = 1\n",
            "loader = __import__\nX = 1\n",
            "fn = mod.exec_module\nX = 1\n",
            "fn = pkg.load_module\nX = 1\n",
            "Loader = machinery.SourceFileLoader\nX = 1\n",
            "factory = util.spec_from_loader\nX = 1\n",
            "spawn = asyncio.create_subprocess_shell\nX = 1\n",
            "getattr(obj, 'exec_module')\n",
            "m = getattr(mod, 'import_module')('x')\n",
        ):
            _write(self.repo, "tests/test_dyn.py", body)
            with self.subTest(snippet=body.strip().splitlines()[0][:48]):
                sel = self._select(["tests/test_prod.py"])
                self.assertEqual(sel.mode, "full")

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
