"""Banca rodada 3: provas físicas de falso-verde em cobertura, staged,
pipeline concorrente e scan a11y incremental."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from henke import gate_pipeline as gp
from henke.metricas import coverage_ledger as cl
from henke.metricas.scan_manifest import run_incremental_scan, scanner_specs


class TestResultPolarity(unittest.TestCase):
    """_RES_NEG com `\\b` final nunca casava stems maiores que o token —
    'passed 2 failed', 'reprovado', '❌' eram aceitos como cobertura."""

    def test_negative_verdicts_rejected(self):
        for res in ("2 failed", "passed 2 failed", "ok ❌", "passou ✗",
                    "ok — 1 reprovado", "violated", "falha parcial",
                    "error: none found"):
            with self.subTest(res=res):
                self.assertTrue(cl._RES_NEG.search(res.lower()),
                                f"falha não detectada em {res!r}")

    def test_positive_verdicts_accepted(self):
        for res in ("2 passed", "ok", "verified", "conforme", "sim"):
            with self.subTest(res=res):
                self.assertTrue(cl._RES_POS.search(res.lower()))
                self.assertFalse(cl._RES_NEG.search(res.lower()),
                                 f"veredito limpo marcado como falha: {res!r}")

    def test_nothing_still_negative(self):
        self.assertFalse(cl._RES_POS.search("nothing"))
        self.assertFalse(cl._RES_NEG.search("nothing"))

    def test_verify_ledger_rejects_covered_with_failed_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "alvo.py").write_text("x = 1\n", encoding="utf-8")
            out = root / "ledger.json"
            ledger = cl.init_ledger(root, out)
            data = json.loads(out.read_text(encoding="utf-8"))
            for u in data["units"]:
                u["status"] = "covered"
                u["checks"] = [{
                    "agent_id": "poc",
                    "invariant": "unidade auditada",
                    "result": "passed 2 failed",
                    "method": "source",
                    "assumptions": [],
                }]
            out.write_text(json.dumps(data), encoding="utf-8")
            ok, errors, _ = cl.verify_ledger(out, min_pct=0)
            self.assertFalse(ok)
            self.assertTrue(any(e.startswith("FAILED-RESULT") for e in errors),
                            f"result com falha aceito como cobertura: {errors}")


class TestStagedCaseCollision(unittest.TestCase):
    """Índice com A.py+a.py colapsa para 1 arquivo no NTFS — o blob
    malicioso nunca era materializado nem escaneado (falso-verde)."""

    def _git(self, repo: Path, *args: str, input=None) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], input=input,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    def test_case_collision_fails_closed(self):
        from henke.cli_check import _cmd_check
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._git(repo, "init", "-q")
            evil = self._git(repo, "hash-object", "-w", "--stdin",
                             input="import pickle\nvalue = pickle.loads(payload)\n")
            clean = self._git(repo, "hash-object", "-w", "--stdin",
                              input="value = 1\n")
            self._git(repo, "update-index", "--add", "--cacheinfo",
                      f"100644,{evil},A.py")
            self._git(repo, "update-index", "--add", "--cacheinfo",
                      f"100644,{clean},a.py")
            rc = _cmd_check(repo, staged=True, as_json=True)
            self.assertEqual(rc, 2,
                             "índice com colisão de case passou como staged válido")


class TestPipelinePerRunArtifact(unittest.TestCase):
    """Dois runs concorrentes dividiam `ratchet-current.tsv`: o portão de A
    podia avaliar evidência escrita por B e passar verde."""

    def test_gate_reads_own_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            src = repo / "a.py"
            base = repo / "baseline.tsv"
            src.write_text("def f(x):\n    if x:\n        return 1\n    return 0\n")
            base.write_text("a.py\t2\t1\t0\n")
            real_run = gp._run
            ready = threading.Event()
            resume = threading.Event()
            results: dict[str, int] = {}
            gate_argv: dict[str, list] = {}

            def run(argv, cwd=None, timeout=300):
                if str(gp.GATES["ratchet"]) in argv:
                    gate_argv[threading.current_thread().name] = list(argv)
                    if threading.current_thread().name == "first":
                        ready.set()
                        self.assertTrue(resume.wait(30), "schedule timeout")
                return real_run(argv, cwd, timeout)

            def first():
                results["first"] = gp.main(
                    ["ratchet", "--repo", str(repo), "--baseline", str(base)])

            with patch.object(gp, "_run", side_effect=run):
                t = threading.Thread(target=first, name="first", daemon=True)
                t.start()
                self.assertTrue(ready.wait(30), "gate do first não lançou")
                src.write_text("def f(x):\n    return 0\n")
                results["second"] = gp.main(
                    ["ratchet", "--repo", str(repo), "--baseline", str(base)])
                resume.set()
                t.join(30)
            self.assertFalse(t.is_alive())

            self.assertEqual(results.get("second"), 0)
            self.assertNotEqual(results.get("first"), 0,
                                "run com regressão passou lendo evidência alheia")
            self.assertNotEqual(gate_argv["first"][-1], gate_argv["MainThread"][-1],
                                "runs concorrentes devem usar artefatos distintos")


class TestA11yIncrementalStrict(unittest.TestCase):
    """Incremental decodificava com errors='replace' — bytes inválidos viravam
    mojibake escaneado, enquanto o scan direto falha fechado (ilegível)."""

    def test_invalid_utf8_is_infra_error(self):
        spec = next(s for s in scanner_specs() if s.scanner_id == "a11y")
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "page.html").write_bytes(
                b"<html><body>\xff\xfe\xfb</body></html>")
            r = run_incremental_scan(repo, spec)
            self.assertEqual(r.exit_code, 2,
                             "bytes inválidos varridos como mojibake")
            self.assertTrue(any("UnicodeDecodeError" in reason
                                for reason in r.reasons), r.reasons)

    def test_valid_utf8_still_scans(self):
        spec = next(s for s in scanner_specs() if s.scanner_id == "a11y")
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "page.html").write_text(
                "<html><body><p>ok</p></body></html>", encoding="utf-8")
            r = run_incremental_scan(repo, spec)
            self.assertNotEqual(r.exit_code, 2, r.reasons)


if __name__ == "__main__":
    unittest.main()
