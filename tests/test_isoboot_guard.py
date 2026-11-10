"""Guarda de isolamento (scripts/_isoboot.py) — cobertura adversarial.

Critério de aceite da auditoria (item 1): nenhum `check`/`exec` em repo
hostil retorna 0 — cobre sentinel de ambiente, sitecustomize, usercustomize,
.pth, sys.flags forjado, sys.modules['sys'] trocado e interpretador
absoluto. O bloco canônico ISO-BOOT é exigido verbatim em TODOS os
entrypoints (`if __name__ == "__main__"`).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts import _isoboot  # noqa: E402

ENTRYPOINT_DIRS = (SUITE_ROOT / "scripts", SUITE_ROOT / "scripts" / "metricas")


def _entrypoint_files() -> list[Path]:
    files = []
    for d in ENTRYPOINT_DIRS:
        for p in sorted(d.glob("*.py")):
            if '__name__ == "__main__"' in p.read_text(encoding="utf-8", errors="replace"):
                files.append(p)
    return files


class TestCanonicalGuardSync(unittest.TestCase):
    """Todo entrypoint carrega o bloco canônico verbatim — divergência ou
    guarda ausente = regressão (P1-01: isolamento em TODOS os entrypoints)."""

    def test_every_entrypoint_has_canonical_guard(self):
        missing, divergent = [], []
        for p in _entrypoint_files():
            text = p.read_text(encoding="utf-8")
            if "ISO-BOOT" not in text:
                missing.append(p.name)
            elif _isoboot.CANONICAL_GUARD not in text:
                divergent.append(p.name)
        self.assertEqual([], missing, "entrypoints sem guarda ISO-BOOT")
        self.assertEqual([], divergent, "guardas divergentes do canônico")

    def test_entrypoint_inventory_nonempty(self):
        files = _entrypoint_files()
        self.assertGreaterEqual(len(files), 20)
        names = {p.name for p in files}
        for need in ("henke_cli.py", "henke_auto.py", "henke_gates.py",
                     "henke_router.py", "henke_driver_agent.py", "run_state.py",
                     "audit_ledger.py", "hazard_scan.py", "assertless_scan.py"):
            self.assertIn(need, names)


class TestIsolatedRuntimeCheck(unittest.TestCase):
    """A prova é estrutural: structseq C autenticado + identidade sys≡os.sys.
    Falsificações de Python puro (namespace, tuple-subclass, módulo fake)
    NÃO passam — heaptype delata qualquer classe."""

    def test_real_flags_are_authentic(self):
        self.assertTrue(_isoboot._authentic_flags(sys.flags))

    def test_namespace_flags_rejected(self):
        import types
        fake = types.SimpleNamespace(isolated=1, ignore_environment=1,
                                     no_user_site=1)
        self.assertFalse(_isoboot._authentic_flags(fake))
        self.assertFalse(_isoboot._authentic_flags(None))
        self.assertFalse(_isoboot._authentic_flags(1))

    def test_tuple_subclass_flags_rejected(self):
        class flags(tuple):  # mesmo nome/module spoofing tentável
            isolated = 1
            ignore_environment = 1
            no_user_site = 1
        flags.__module__ = "sys"
        self.assertFalse(_isoboot._authentic_flags(flags()))

    def test_other_structseq_rejected(self):
        self.assertFalse(_isoboot._authentic_flags(sys.version_info))

    def test_plain_runtime_not_isolated(self):
        # O processo de teste não nasce com -Es/-I: tem que detectar e respawnar.
        if not (sys.flags.ignore_environment and sys.flags.no_user_site):
            self.assertFalse(_isoboot.isolated_runtime())

    def test_es_child_isolated(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from scripts import _isoboot; "
            "print('ISO' if _isoboot.isolated_runtime() else 'PLAIN')"
        ) % str(SUITE_ROOT).replace("\\", "\\\\")
        res = subprocess.run([sys.executable, "-Es", "-c", code],
                             capture_output=True, text=True, timeout=60)
        self.assertIn("ISO", res.stdout, res.stderr)

    def test_I_child_isolated(self):
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "from scripts import _isoboot; "
            "print('ISO' if _isoboot.isolated_runtime() else 'PLAIN')"
        ) % str(SUITE_ROOT).replace("\\", "\\\\")
        res = subprocess.run([sys.executable, "-I", "-c", code],
                             capture_output=True, text=True, timeout=60)
        self.assertIn("ISO", res.stdout, res.stderr)


class TestAdversarialRespawn(unittest.TestCase):
    """e2e: invocação hostil de `henke_cli check` em repo com os.system.
    Todas as variantes DEVEM respawnar para -Es e reprovar (exit != 0)."""

    def _hostile_repo(self, td: Path) -> Path:
        repo = td / "repo"
        repo.mkdir()
        (repo / "app.py").write_text(
            "import os\nos.system('echo pwned')\n", encoding="utf-8")
        return repo

    def _evil_dir(self, td: Path, sitecustomize_body: str = "") -> Path:
        evil = td / "evil"
        evil.mkdir(exist_ok=True)
        (evil / "sitecustomize.py").write_text(
            sitecustomize_body
            or (
                "import scripts.metricas.scan_manifest as _m\n"
                "class _R:\n"
                "    exit_code = 0\n    full_scan = True\n    scanned_files = ()\n"
                "    reused_files = ()\n    invalidated_files = ()\n    reasons = ()\n"
                "_m.run_incremental_scan = lambda *a, **k: _R()\n"
            ),
            encoding="utf-8")
        return evil

    def _run_check(self, repo: Path, extra_env: dict | None = None,
                   argv0: list[str] | None = None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update(extra_env or {})
        cmd = argv0 or [sys.executable,
                        str(SUITE_ROOT / "scripts" / "henke_cli.py"),
                        "check", "--repo", str(repo), "--no-cache"]
        return subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=240)

    def test_forged_flags_namespace_sitecustomize(self):
        """sys.flags = SimpleNamespace(isolated=1,...) não pode pular o
        respawn — structseq autenticado (heaptype delata o falso)."""
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            repo = self._hostile_repo(td)
            evil = self._evil_dir(td, (
                "import sys, types\n"
                "sys.flags = types.SimpleNamespace("
                "isolated=1, ignore_environment=1, no_user_site=1, "
                "safe_path=1, hash_randomization=1)\n"
            ))
            env = {"PYTHONPATH": str(evil) + os.pathsep + str(SUITE_ROOT)}
            res = self._run_check(repo, env)
            self.assertNotEqual(
                res.returncode, 0,
                "sys.flags forjado não pode pular o respawn: "
                f"{res.stdout[-300:]} {res.stderr[-200:]}")

    def test_forged_flags_tuple_subclass_sitecustomize(self):
        """class flags(tuple) com __module__='sys' ainda é heaptype — detectado."""
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            repo = self._hostile_repo(td)
            evil = self._evil_dir(td, (
                "import sys\n"
                "class flags(tuple):\n"
                "    isolated=1\n    ignore_environment=1\n    no_user_site=1\n"
                "flags.__module__='sys'\n"
                "sys.flags = flags()\n"
            ))
            env = {"PYTHONPATH": str(evil) + os.pathsep + str(SUITE_ROOT)}
            res = self._run_check(repo, env)
            self.assertNotEqual(
                res.returncode, 0,
                "flags tuple-subclass não pode pular o respawn: "
                f"{res.stdout[-300:]} {res.stderr[-200:]}")

    def test_sys_module_swap_sitecustomize(self):
        """sys.modules['sys'] trocado (fake com flags REAIS isolated=0 capturadas):
        identidade sys≡os.sys pode até bater — mas flags reais dizem
        não-isolado → respawn."""
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            repo = self._hostile_repo(td)
            evil = self._evil_dir(td, (
                "import sys, types\n"
                "_real = sys.flags\n"
                "_fake = types.ModuleType('sys')\n"
                "for _k in dir(sys):\n"
                "    try: setattr(_fake, _k, getattr(sys, _k))\n"
                "    except Exception: pass\n"
                "_fake.flags = _real\n"
                "sys.modules['sys'] = _fake\n"
            ))
            env = {"PYTHONPATH": str(evil) + os.pathsep + str(SUITE_ROOT)}
            res = self._run_check(repo, env)
            self.assertNotEqual(
                res.returncode, 0,
                "sys.modules['sys'] trocado não pode fabricar PASS: "
                f"{res.stdout[-300:]} {res.stderr[-200:]}")

    def test_usercustomize_via_pythonuserbase(self):
        """usercustomize.py no user site (PYTHONUSERBASE) roda no startup sem
        -s e patcha o scanner — o respawn -Es elimina o canal."""
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            repo = self._hostile_repo(td)
            ubase = td / "ubase"
            usite = ubase / ("Python%s%s" % sys.version_info[:2]) / "site-packages"
            usite.mkdir(parents=True)
            (usite / "usercustomize.py").write_text(
                "import scripts.metricas.scan_manifest as _m\n"
                "class _R:\n"
                "    exit_code = 0\n    full_scan = True\n    scanned_files = ()\n"
                "    reused_files = ()\n    invalidated_files = ()\n    reasons = ()\n"
                "_m.run_incremental_scan = lambda *a, **k: _R()\n",
                encoding="utf-8")
            env = {
                "PYTHONUSERBASE": str(ubase),
                "PYTHONPATH": str(SUITE_ROOT),
            }
            res = self._run_check(repo, env)
            self.assertNotEqual(
                res.returncode, 0,
                "usercustomize não pode fabricar PASS: "
                f"{res.stdout[-300:]} {res.stderr[-200:]}")

    def test_pth_in_user_site(self):
        """.pth executável no user site é canal de auto-exec — morto por -s."""
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            repo = self._hostile_repo(td)
            ubase = td / "ubase"
            usite = ubase / ("Python%s%s" % sys.version_info[:2]) / "site-packages"
            usite.mkdir(parents=True)
            (usite / "evil.pth").write_text(
                "import os; os.environ.setdefault('ZZ_PTH_RAN','1')\n",
                encoding="utf-8")
            # Prova que o .pth roda sem -s e não roda com -Es, sem depender
            # do check completo (mais rápido): consulta a env marker.
            env = os.environ.copy()
            env["PYTHONUSERBASE"] = str(ubase)
            env["PYTHONPATH"] = str(SUITE_ROOT)
            plain = subprocess.run(
                [sys.executable, "-c", "import os; print(os.environ.get('ZZ_PTH_RAN'))"],
                capture_output=True, text=True, env=env, timeout=60)
            iso = subprocess.run(
                [sys.executable, "-Es", "-c", "import os; print(os.environ.get('ZZ_PTH_RAN'))"],
                capture_output=True, text=True, env=env, timeout=60)
            self.assertIn("1", plain.stdout, "setup do PoC: .pth não executou")
            self.assertIn("None", iso.stdout,
                          ".pth do user site executou sob -Es: " + iso.stdout)

    def test_shell_wrapper_absolute_interpreter(self):
        """Invocação via shell + interpretador ABSOLUTO + env sentinel:
        ainda respawna (nenhum caminho ambiental influencia a prova)."""
        with tempfile.TemporaryDirectory() as tds:
            td = Path(tds)
            repo = self._hostile_repo(td)
            evil = self._evil_dir(td)
            env = {
                "PYTHONPATH": str(evil) + os.pathsep + str(SUITE_ROOT),
                "HENKE_ISOLATED": "1",
            }
            script = str(SUITE_ROOT / "scripts" / "henke_cli.py")
            if os.name == "nt":
                argv0 = ["cmd", "/c", sys.executable, script, "check",
                         "--repo", str(repo), "--no-cache"]
            else:
                argv0 = ["sh", "-c",
                         f"'{sys.executable}' '{script}' check --repo '{repo}' --no-cache"]
            res = self._run_check(repo, env, argv0=argv0)
            self.assertNotEqual(
                res.returncode, 0,
                "wrapper de shell não pode fabricar PASS: "
                f"{res.stdout[-300:]} {res.stderr[-200:]}")


if __name__ == "__main__":
    unittest.main()
