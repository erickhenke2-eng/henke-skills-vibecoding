"""test_adversarial_round4.py — regressões provadas fisicamente na rodada 4.

Cada teste REPROVA no código pré-fix e PASSA no pós-fix. As PoCs físicas
completas vivem em %TEMP%\\henke-audit-r4 (etapa 2 da auditoria). Cobre:

  H4  — `.pyw` (source que o Windows importa) fora de SCAN_EXTS.
  H1  — `iter_tree max_depth=64` deixava diretório profundo sair verde.
  H10 — teste pulado (skip) creditado como "passou".
  H12 — regex do Jest casava antes do unittest e inflava a contagem.
  H18 — `git_state` colapsava para NOGIT ao decodificar diff com locale cp1252.
"""
from __future__ import annotations

import locale
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


class H4PywIsScanned(unittest.TestCase):
    def test_pyw_payload_is_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "evil.pyw"
            p.write_text("def danger(x):\n    return eval(x)\n", encoding="utf-8")
            scanned, findings, _oversize = hazard_scan.scan_file(p)
            self.assertEqual(scanned, 1, "um .pyw deve ser escaneado")
            self.assertTrue(findings, "payload eval() em .pyw deve gerar finding")


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
            self.assertEqual(
                res.exit_code, 2,
                "árvore truncada por max_depth não pode ser PASS")
            self.assertTrue(
                any("depth" in r for r in res.reasons),
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


class H12UnittestBeatsJestRegex(unittest.TestCase):
    def test_printed_jest_line_does_not_inflate(self):
        out = ("Tests: 500 passed\n.\n" + "-" * 70 +
               "\nRan 1 test in 0.001s\n\nOK\n")
        self.assertEqual(
            parse_test_runner_output(out, ""), (1, 1),
            "linha Jest impressa não pode inflar uma run unittest")


class H18GitStateSurvivesNonCp1252Bytes(unittest.TestCase):
    def test_distinct_dirty_states_do_not_collide(self):
        from henke.gate_cache import compute_cache_key

        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            env = dict(os.environ)

            def g(*args):
                return subprocess.run(
                    ["git", *args], cwd=str(repo),
                    capture_output=True, text=True, env=env)

            if g("init").returncode != 0:
                self.skipTest("git indisponível")
            g("config", "user.email", "a@b.c")
            g("config", "user.name", "t")
            (repo / "keep.py").write_text("x = 1\n", encoding="utf-8")
            acc = repo / "acento.py"
            acc.write_text("# base\ny = 1\n", encoding="utf-8")
            g("add", "-A")
            g("commit", "-m", "base")

            acc.write_text("# comentario \u00c1 estado-1\ny = 2\n",
                           encoding="utf-8")
            orig = locale.getpreferredencoding
            locale.getpreferredencoding = lambda *a, **k: "cp1252"
            try:
                # guarda: só isola o bug quando o patch realmente força cp1252
                probe = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false", "diff", "HEAD"],
                    cwd=str(repo), capture_output=True, text=True, env=env)
                cp1252_active = probe.stdout is None or "\ufffd" in (probe.stdout or "")
                if not cp1252_active:
                    self.skipTest(
                        "locale cp1252 não forçável neste runtime "
                        "(H18 coberto pela PoC física em %TEMP%)")
                script = _spec("hazard").script_path
                args = [str(repo / "keep.py")]  # Merkle fixo: só git_state varia
                k1 = compute_cache_key("hazard", script, args, repo)
                acc.write_text("# comentario \u00c1 estado-2 DIFERENTE\ny = 3\n",
                               encoding="utf-8")
                k2 = compute_cache_key("hazard", script, args, repo)
            finally:
                locale.getpreferredencoding = orig
            self.assertNotEqual(
                k1, k2,
                "estados sujos distintos colidiram na chave — "
                "git_state degradou para NOGIT (H18)")


if __name__ == "__main__":
    unittest.main()
