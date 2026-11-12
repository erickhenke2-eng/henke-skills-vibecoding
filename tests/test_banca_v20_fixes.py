"""Regressões da banca v20 — cada teste prova que um furo confirmado está fechado.

F1 assertless skip-de-coleção · F2 coverage_ledger forjável · F2b audit_ledger
runner forjado · F3 supressão invisível no check · F4 check ≠ suite · F5 skipped
silencioso no manifesto · F6 derive assimétrico · K1 SSRF sources · K2 SQLi split.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MET = REPO / "henke" / "metricas"
sys.path.insert(0, str(REPO / "henke"))
sys.path.insert(0, str(MET))

from henke.audit_ledger import is_test_cmd  # noqa: E402
from henke.rs_verify import derive_run_outcome  # noqa: E402
from henke.metricas import assertless_scan, hazard_scan  # noqa: E402


def _run(script: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(script), *args],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


class TestF1CollectionSkips(unittest.TestCase):
    """F1: pytestmark/pytest.skip(allow_module_level)/parametrize([]) viram
    COLLECTION-SKIP em vez de 'N testes escaneados, 0 achados'."""

    def _scan(self, src: str):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "test_x.py"
            p.write_text(src, encoding="utf-8")
            return assertless_scan.scan_test_file(p, set())

    def test_pytestmark_skipif_true(self):
        n, finds = self._scan(
            "import pytest\npytestmark = pytest.mark.skipif(True, reason='x')\n"
            "def test_a():\n    assert 1\n")
        self.assertEqual(n, 1)
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_pytest_skip_allow_module_level(self):
        n, finds = self._scan(
            "import pytest\npytest.skip('mod', allow_module_level=True)\n"
            "def test_b():\n    assert 1\n")
        self.assertEqual(n, 1)
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_empty_parametrize(self):
        n, finds = self._scan(
            "import pytest\n@pytest.mark.parametrize('x', [])\n"
            "def test_c(x):\n    assert x\n")
        self.assertEqual(n, 1)
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_controle_sem_skip_de_colecao(self):
        n, finds = self._scan(
            "def compute():\n    return 41\n"
            "def test_ok():\n    r = compute()\n    assert r == 41\n")
        self.assertEqual(n, 1)
        self.assertEqual(finds, [])

    # --- banca-v20r2 (re-auditoria SWE-2): bypasses adjacentes do F1 ---

    def test_skipif_const_de_modulo(self):
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\nCOND = True\n"
            "pytestmark = pytest.mark.skipif(COND)\n"
            "def test_d():\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_pytestmark_via_variavel(self):
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\n"
            "MARKS = [pytest.mark.skipif(True)]\npytestmark = MARKS\n"
            "def test_e():\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_pytest_skip_posicional(self):
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\n"
            "pytest.skip('m', True)\n"
            "def test_f():\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_pytestmark_escopo_classe(self):
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\n"
            "class T:\n    pytestmark = pytest.mark.skip(reason='x')\n"
            "    def test_n(self):\n        r = compute()\n        assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_parametrize_kwargs_e_nomeado(self):
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\n"
            "@pytest.mark.parametrize(argnames='x', argvalues=[])\n"
            "def test_q(x):\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\nARGS = []\n"
            "@pytest.mark.parametrize('x', ARGS)\n"
            "def test_r(x):\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))

    def test_globals_e_annassign_pytestmark(self):
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\n"
            "globals()['pytestmark'] = pytest.mark.skip(reason='x')\n"
            "def test_s():\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))
        n, finds = self._scan(
            "def compute():\n    return 5\nimport pytest\n"
            "pytestmark: list = [pytest.mark.skip(reason='x')]\n"
            "def test_t():\n    r = compute()\n    assert r == 5\n")
        self.assertTrue(any(f[0] == "COLLECTION-SKIP" for f in finds))


class TestF2CoverageForgery(unittest.TestCase):
    """F2: ledger 100% 'covered' por auto-atestação reprova sem opt-in."""

    def test_forged_ledger_blocked_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "p"
            (proj / "src").mkdir(parents=True)
            (proj / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
            led = Path(td) / "l.json"
            self.assertEqual(_run(MET / "coverage_ledger.py", "init",
                                  "--root", str(proj), "--out", str(led))[0], 0)
            d = json.loads(led.read_text(encoding="utf-8"))
            for u in d["units"]:
                u["status"] = "covered"
                u["checks"] = [{"agent_id": "forged", "method": "source",
                                "invariant": "x", "result": "pass"}]
            led.write_text(json.dumps(d), encoding="utf-8")
            code, out = _run(MET / "coverage_ledger.py", "verify", str(led))
            self.assertEqual(code, 1, out)
            self.assertIn("MISSING-CRITIC", out)
            code, out = _run(MET / "coverage_ledger.py", "verify", str(led),
                             "--allow-self-attested")
            self.assertEqual(code, 0, out)

    def test_self_critic_nao_e_independente(self):
        # banca-v20r2 (V2): {"agent":"self"} igual ao autor dos checks mintava
        # a "independência" — forma JSON não é vínculo dos dois agentes.
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "p"
            (proj / "src").mkdir(parents=True)
            (proj / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
            led = Path(td) / "l.json"
            self.assertEqual(_run(MET / "coverage_ledger.py", "init",
                                  "--root", str(proj), "--out", str(led))[0], 0)
            d = json.loads(led.read_text(encoding="utf-8"))
            for u in d["units"]:
                u["status"] = "covered"
                u["checks"] = [{"agent_id": "self", "method": "source",
                                "invariant": "x", "result": "pass"}]
            d["critic_passes"] = [{"agent": "self", "round": 1,
                                   "scope": "all units"}]
            led.write_text(json.dumps(d), encoding="utf-8")
            code, out = _run(MET / "coverage_ledger.py", "verify", str(led))
            self.assertEqual(code, 1, out)
            self.assertIn("CRITIC-NOT-INDEPENDENT", out)


class TestF2bForgedRunner(unittest.TestCase):
    """F2b: 'python tests/<qualquer>.py' não minta evidência de teste."""

    def test_forged_runner_rejected(self):
        self.assertFalse(is_test_cmd("python tests/forged_runner.py"))
        self.assertFalse(is_test_cmd("python tests/helper.py"))

    def test_substring_nao_minta(self):
        # banca-v20r2: 'test' como substring (conTEST/laTEST/atTEST) mintava.
        for cmd in ("python tests/contest_runner.py", "python tests/latest.py",
                    "python tests/attest_x.py", "python tests/detest.py"):
            self.assertFalse(is_test_cmd(cmd), cmd)

    def test_real_test_files_and_frameworks_ok(self):
        self.assertTrue(is_test_cmd("python tests/test_foo.py"))
        self.assertTrue(is_test_cmd("python -m pytest tests/"))
        self.assertTrue(is_test_cmd("pytest"))


class TestF6DeriveAsymmetry(unittest.TestCase):
    """F6: exit 3 só satisfaz portão obrigatório se n/a-capable."""

    _TRI = {"command": "c", "evidence_file": "e", "evidence_sha256": "s"}

    def test_assertless_na_blocked_com_mandatory(self):
        gates = {"hazard_scan": {**self._TRI, "exit_code": 0},
                 "assertless_scan": {**self._TRI, "exit_code": 3}}
        data = {"mode": "EXECUTAR", "gates": gates,
                "mandatory_gates": ["hazard_scan", "assertless_scan"]}
        outcome, reasons = derive_run_outcome(data)
        self.assertEqual(outcome, "blocked")
        self.assertTrue(any("assertless" in r for r in reasons))

    def test_a11y_na_ok_com_mandatory(self):
        gates = {"hazard_scan": {**self._TRI, "exit_code": 0},
                 "a11y_scan": {**self._TRI, "exit_code": 3}}
        data = {"mode": "EXECUTAR", "gates": gates,
                "mandatory_gates": ["hazard_scan", "a11y_scan"]}
        outcome, _ = derive_run_outcome(data)
        self.assertEqual(outcome, "verified")


class TestKiroRegexGaps(unittest.TestCase):
    """K1/K2: SSRF reconhece request.args/GET; SQLi pega split-literal."""

    def test_ssrf_flask_django_sources(self):
        self.assertRegex('request.args["url"]', hazard_scan._SSRF_SOURCE_RE)
        self.assertRegex('request.GET.get("u")', hazard_scan._SSRF_SOURCE_RE)

    def test_ssrf_fontes_multiframework(self):
        # banca-v20r2 (V8/N1): POST/get_json/query_params/view_args/META/
        # match_info evadiam; decoys silenciavam a função inteira.
        for src in ('request.POST["u"]', 'request.get_json()["url"]',
                    'request.query_params["u"]', 'request.match_info["u"]',
                    'request.view_args["u"]', 'request.META["HTTP_X"]'):
            self.assertRegex(src, hazard_scan._SSRF_SOURCE_RE, src)
        for decoy in ('allowedHosts = None', 'x = h.hostname',
                      't = ipaddress.ip_address'):
            self.assertIsNone(hazard_scan._SSRF_SAFE_RE.search(decoy), decoy)
        for real in ('if u.hostname in allowedHosts:',
                     'if parsed.hostname == "x":',
                     'ipaddress.ip_address(h)'):
            self.assertRegex(real, hazard_scan._SSRF_SAFE_RE, real)

    def test_sqli_split_literal(self):
        self.assertRegex(
            'execute("SELECT * FROM users" + " WHERE id = " + uid)',  # hazard-ok: SQL-CONCAT — PoC de regex em fixture, nunca executado
            hazard_scan._SQL_CONCAT_RE)
        # controle: query segura não dispara
        self.assertIsNone(
            hazard_scan._SQL_CONCAT_RE.search(
                'execute("SELECT * FROM users WHERE id = %s", (uid,))'))

    def test_sqli_formas_adjacentes(self):
        # banca-v20r2 (V9): literal parentizado, N literais, CRUD partido,
        # join — todos evadiam a regex literal.
        evade = [
            'execute(("SELECT * FROM users WHERE id = ") + uid)',  # hazard-ok: SQL-CONCAT — PoC de regex em fixture, nunca executado
            'execute("SELECT * FROM u" + " WHERE a=" + " AND b=" + uid)',  # hazard-ok: SQL-CONCAT — PoC de regex em fixture, nunca executado
            'execute("SELECT" + " * FROM users" + " WHERE id=" + uid)',  # hazard-ok: SQL-CONCAT — PoC de regex em fixture, nunca executado
            'execute("".join(["SELECT * FROM users WHERE id = ", uid]))',  # hazard-ok: SQL-CONCAT — PoC de regex em fixture, nunca executado
        ]
        for s in evade:
            merged = hazard_scan._merge_string_lits(s)
            hit = hazard_scan._SQL_CONCAT_RE.search(merged) or \
                hazard_scan._SQL_JOIN_RE.search(merged)
            self.assertIsNotNone(hit, s)
        # var-first: nome atrelado a literal SQL reutilizado em concat
        src = 'q = "SELECT * FROM users"\nexecute(q + " WHERE id=" + uid)'  # hazard-ok: SQL-CONCAT — PoC de regex em fixture, nunca executado
        names = {m.group(1) for m in __import__("re").finditer(
            r"\b(\w+)\s*=\s*['\"][^'\"]*\b(?:" + hazard_scan._SQL_KW + r")\b",
            hazard_scan._merge_string_lits(src))}
        self.assertIn("q", names)


class TestF5SkippedDeclared(unittest.TestCase):
    """F5: entry skipped (reparse/outros) no manifesto vira reason visível."""

    def test_skipped_reparse_vira_reason(self):
        # Prova indireta do código: o ramo 'skipped' agora emite reason antes de
        # descartar — verifica a string presente no source do manifesto.
        src = (MET / "scan_manifest.py").read_text(encoding="utf-8")
        self.assertIn('skipped:{entry.reason}', src)


if __name__ == "__main__":
    unittest.main()
