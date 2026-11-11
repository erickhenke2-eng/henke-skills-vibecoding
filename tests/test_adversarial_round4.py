"""test_adversarial_round4.py — regressões provadas fisicamente na rodada 4.

Cada teste REPROVA no código pré-fix e PASSA no pós-fix. PoCs físicas em
%TEMP%\\henke-audit-r4.

  H4  — `.pyw` (source que o Windows importa) fora de SCAN_EXTS.
  F1  — `.pyw` escaneado com o perfil C-like ERRADO (regressão do H4): sem
        _PY_SHELL_RE/_py_ast_findings e sem mascarar `#`/`//`, deixando
        `os.system` passar e `# resolve(` suprimir PATH-TRAVERSAL.
  H1  — `iter_tree max_depth=64` deixava diretório profundo sair verde.
  H10 — teste pulado (skip) creditado como "passou".
  F2  — "expected failures" creditadas como passou (irmão do H10);
        "unexpected successes" contam como falha.
  H12 — regex do Jest casava antes do unittest e inflava a contagem.
  H18 — `git_state` colapsava para NOGIT ao decodificar diff com locale cp1252.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from henke.metricas import hazard_scan
from henke.metricas.scan_manifest import run_incremental_scan, scanner_specs
from henke.cli_exec_proc import parse_test_runner_output
from henke.exec_contract import evaluate_execution


def _spec(scanner_id):
    for s in scanner_specs():
        if s.scanner_id == scanner_id:
            return s
    raise AssertionError(f"scanner desconhecido: {scanner_id}")


_PYW_PAYLOAD = (
    "import os\n"
    "\n"
    "\n"
    "def handler(req):\n"
    "    os.system(req.args.get('cmd'))     # SHELL-INJECTION (perfil Python)\n"
    "    user_path = req.args.get('f')      # fonte externa\n"
    "    open(user_path, 'w')               # PATH-TRAVERSAL (sink dinamico)\n"
    "    # resolve(user_path)               # decoy: so vale se vivo (C-like)\n"
)


class H4PywIsScanned(unittest.TestCase):
    def test_pyw_payload_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "evil.pyw"
            p.write_text("def danger(x):\n    return eval(x)\n", encoding="utf-8")
            scanned, findings, _ov = hazard_scan.scan_file(p)
            self.assertEqual(scanned, 1, "um .pyw deve ser escaneado")
            self.assertTrue(findings, "payload eval() em .pyw deve gerar finding")


class F1PywScannedAsPython(unittest.TestCase):
    def test_pyw_uses_python_profile(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "evil.pyw"
            p.write_text(_PYW_PAYLOAD, encoding="utf-8")
            scanned, findings, _ov = hazard_scan.scan_file(p)
            rules = {r for r, _loc, _sc, _msg in findings}
            self.assertEqual(scanned, 1)
            # perfil Python: os.system detectado (C-like não tem _PY_SHELL_RE)
            self.assertIn("SHELL-INJECTION", rules,
                          ".pyw deve ser escaneado como Python (os.system)")
            # decoy `# resolve(` só suprime se o comentário sobreviver (C-like);
            # no perfil Python o `#` é mascarado e o PATH-TRAVERSAL dispara
            self.assertIn("PATH-TRAVERSAL", rules,
                          "comentario `# resolve(` nao pode suprimir em .pyw")

    def test_pyw_hazard_ok_is_honored(self):
        # prova que polyglot reconhece `#` em .pyw (a supressão deixou de ser
        # cega). O marcador é montado em runtime para o literal NÃO aparecer no
        # fonte — senão o dogfooding do próprio repo o trata como contrabando
        # de supressão dentro de string (canário M52).
        ok = "hazard-" + "ok"
        src = ("import os\n\n\ndef f(c):\n"
               "    os.system(c)  # " + ok + ": SHELL-INJECTION — suprimido "
               "de proposito neste teste de regressao\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ok.pyw"
            p.write_text(src, encoding="utf-8")
            _sc, findings, _ov = hazard_scan.scan_file(p)
            rules = {r for r, _loc, _sc_, _m in findings}
            self.assertNotIn("SHELL-INJECTION", rules,
                             "supressão `#` em .pyw deve valer (polyglot pyw)")


class H1MaxDepthIsNotGreen(unittest.TestCase):
    def test_truncated_tree_is_not_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "top.py").write_text("x = 1\n", encoding="utf-8")
            deep = root
            for _ in range(66):  # além do max_depth=64 do iter_tree
                deep = deep / "a"
            deep.mkdir(parents=True)
            (deep / "evil.py").write_text(
                "import os\nos.system(x)\n", encoding="utf-8")
            res = run_incremental_scan(root, _spec("hazard"), full=True)
            self.assertEqual(res.exit_code, 2,
                             "árvore truncada por max_depth não pode ser PASS")
            self.assertTrue(any("depth" in r for r in res.reasons),
                            f"esperava motivo de truncamento; reasons={res.reasons}")


class H10SkipIsNotPassed(unittest.TestCase):
    def test_all_skipped_credits_zero(self):
        out = ("sssss\n" + "-" * 70 +
               "\nRan 5 tests in 0.001s\n\nOK (skipped=5)\n")
        self.assertEqual(parse_test_runner_output(out, ""), (0, 0))
        v = evaluate_execution(
            "python -m unittest discover -s tests", 0, out, "")
        self.assertFalse(v.credit_eligible)
        self.assertEqual(v.discovered, 0)

    def test_partial_skip_counts_only_real(self):
        out = "Ran 10 tests in 0.1s\n\nOK (skipped=3)\n"
        self.assertEqual(parse_test_runner_output(out, ""), (7, 7))


class F2ExpectedFailuresAreNotCredit(unittest.TestCase):
    def test_all_expected_failures_credit_zero(self):
        out = "Ran 5 tests in 0.001s\n\nOK (expected failures=5)\n"
        self.assertEqual(parse_test_runner_output(out, ""), (0, 0))
        v = evaluate_execution(
            "python -m unittest discover -s tests", 0, out, "")
        self.assertFalse(v.credit_eligible)
        self.assertEqual(v.discovered, 0)

    def test_unexpected_success_counts_as_failure(self):
        # 4 rodados: 2 xfail (fora dos descobertos) + 2 unexpected success
        # (contam como falha) -> discovered=2, passed=0
        out = ("Ran 4 tests in 0.1s\n\nFAILED "
               "(expected failures=2, unexpected successes=2)\n")
        self.assertEqual(parse_test_runner_output(out, ""), (2, 0))


class H12UnittestBeatsJestRegex(unittest.TestCase):
    def test_printed_jest_line_does_not_inflate(self):
        out = ("Tests: 500 passed\n.\n" + "-" * 70 +
               "\nRan 1 test in 0.001s\n\nOK\n")
        self.assertEqual(parse_test_runner_output(out, ""), (1, 1),
                         "linha Jest impressa não pode inflar uma run unittest")


class H18GitStateSurvivesNonUtf8Locale(unittest.TestCase):
    """Dois estados sujos distintos devem gerar chaves distintas.

    Sob um console não-UTF-8 (cp1252) o código antigo estourava
    UnicodeDecodeError ao decodificar a saída do git (byte 0x81 de "Á"),
    engolia a exceção e colapsava git_state para NOGIT -> as duas chaves
    colidiam. O fix decodifica UTF-8 e as mantém distintas. Sob UTF-8 o bug
    não se manifesta (ambas distintas); a regressão é provada rodando a
    suíte sob cp1252 (documentado na PoC física). As asserts rodam SEMPRE —
    não há guarda de locale nem probe estrito (que matavam o teste antes).
    """

    def test_distinct_dirty_states_do_not_collide(self):
        from henke.gate_cache import compute_cache_key

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            env = dict(os.environ)

            def g(*args):
                # errors="replace": o helper do teste nunca estoura decode
                return subprocess.run(
                    ["git", *args], cwd=str(repo), capture_output=True,
                    text=True, errors="replace", env=env)

            if g("init").returncode != 0:
                self.skipTest("git indisponível")
            g("config", "user.email", "a@b.c")
            g("config", "user.name", "t")
            (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
            acc = repo / "acento.py"
            acc.write_text("# base\ny = 1\n", encoding="utf-8")
            g("add", "-A")
            g("commit", "-m", "base")

            script = _spec("hazard").script_path
            args = [str(repo / "keep.py")]  # Merkle fixo: só git_state varia

            acc.write_text("# comentario \u00c1 estado-1\ny = 2\n",
                           encoding="utf-8")
            k1 = compute_cache_key("hazard", script, args, repo)
            acc.write_text("# comentario \u00c1 estado-2 DIFERENTE\ny = 3\n",
                           encoding="utf-8")
            k2 = compute_cache_key("hazard", script, args, repo)

            self.assertNotEqual(
                k1, k2,
                "estados sujos distintos colidiram na chave — git_state "
                "degradou para NOGIT (H18; reproduz sob console cp1252)")


if __name__ == "__main__":
    unittest.main()
