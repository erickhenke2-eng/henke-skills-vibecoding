"""Banca rodada 5 (GPT Sol): provas físicas — passed adulterável no
ledger/exec-log e nomes que não materializam no Windows no --staged."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from henke import audit_ledger


def _entry(prev: str, passed: int, entry_hash: str) -> dict:
    return {
        "cmd": "python tests.py", "exit": 0, "timed_out": False,
        "testes_descobertos": 1, "testes_passados": passed,
        "evidence_reason": "unittest",
        "stdout_sha256": "a" * 64,
        "prev_hash": prev, "entry_hash": entry_hash,
    }


def _pipe(prev: str, rec: dict) -> str:
    return hashlib.sha256(
        f"{prev}|{rec['cmd']}|{rec['exit']}|{rec['testes_descobertos']}|"
        f"{rec['testes_passados']}|{rec['stdout_sha256']}|"
        f"{rec['evidence_reason']}".encode()).hexdigest()


class TestPassedTamper(unittest.TestCase):
    """P1: `passed` estava fora do payload encadeado — editar testes_passados
    no log ou no ledger passava com cadeia íntegra (audit RC 1→0)."""

    def _chain_errors(self, recs: list[dict]) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            hd = Path(td) / ".henke"
            hd.mkdir()
            log_p = hd / "exec_log.jsonl"
            log_p.write_text("\n".join(json.dumps(r) for r in recs) + "\n",
                             encoding="utf-8")
            errors, _, _ = audit_ledger._verify_log_chain_core(
                log_p.read_text(encoding="utf-8").splitlines(), log_p)
            return errors

    def test_tampered_passed_breaks_chain(self):
        prev = "0" * 64
        rec = _entry(prev, 0, "")
        rec["entry_hash"] = _pipe(prev, rec)
        clean_errs = self._chain_errors([rec])
        self.assertFalse(any("entry_hash adulterado" in e for e in clean_errs),
                         clean_errs)

        rec_bad = dict(rec, testes_passados=1)  # adultera SEM rehashear
        rec_bad["entry_hash"] = rec["entry_hash"]
        self.assertTrue(
            any("entry_hash adulterado" in e
                for e in self._chain_errors([rec_bad])),
            "edição de passed sobreviveu com entry_hash válido")

        rec_bad2 = dict(rec, evidence_reason="forged_ok")
        rec_bad2["entry_hash"] = rec["entry_hash"]
        self.assertTrue(
            any("entry_hash adulterado" in e
                for e in self._chain_errors([rec_bad2])),
            "edição de evidence_reason sobreviveu com entry_hash válido")

        # Registro v1 (hash legado de 5 campos COM campos de decisão —
        # assinatura de log pré-fix): rejeitado com diagnóstico próprio,
        # não com 'adulterado' genérico.
        rec_v1 = dict(rec)
        rec_v1["entry_hash"] = hashlib.sha256(
            f"{prev}|{rec['cmd']}|{rec['exit']}|{rec['testes_descobertos']}|"
            f"{rec['stdout_sha256']}".encode()).hexdigest()
        errs_v1 = self._chain_errors([rec_v1])
        self.assertTrue(any("cadeia v1" in e for e in errs_v1), errs_v1)

    def test_ledger_edit_passed_fails_crosscheck(self):
        from unittest.mock import patch
        from henke import rs_custody

        prev = "0" * 64
        rec = _entry(prev, 0, "")
        rec["cmd"] = "python test_suite.py"  # runner reconhecido (is_test_cmd)
        rec["entry_hash"] = _pipe(prev, rec)
        with tempfile.TemporaryDirectory() as td, \
                tempfile.TemporaryDirectory() as prof:
            repo = Path(td)
            hd = repo / ".henke"
            hd.mkdir()
            log_p = hd / "exec_log.jsonl"
            log_p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            # Âncora de ponta real — sem ela a auditoria falha por motivo
            # errado (âncora ausente), não pela adulteração.
            with patch.dict(os.environ, {"APPDATA": prof}):
                rs_custody.record_exec_tip(repo, rec["entry_hash"])
                entry = {"cmd": "python test_suite.py", "exit": 0,
                         "testes_descobertos": 1, "testes_passados": 1,
                         "stdout_sha256": "a" * 64}
                ok, errors, _ = audit_ledger.audit_ledger_content(
                    [entry], exec_log_path=log_p, require_exec_log=True)
                self.assertFalse(ok)
                self.assertTrue(
                    any("sem registro correspondente íntegro" in e
                        for e in errors), errors)
                # Variante da revisão: OMITIR 'passed' no ledger é
                # adulteração equivalente — não pode casar com log que
                # registrou o placar.
                entry_omit = dict(entry)
                del entry_omit["testes_passados"]
                ok2, errors2, _ = audit_ledger.audit_ledger_content(
                    [entry_omit], exec_log_path=log_p, require_exec_log=True)
                self.assertFalse(ok2)
                self.assertTrue(
                    any("sem registro correspondente íntegro" in e
                        for e in errors2), errors2)


@unittest.skipUnless(os.name == "nt", "ADS/device names são semântica NTFS")
class TestStagedNonMaterializableNames(unittest.TestCase):
    """`:` em nome vira ADS no NTFS — o blob nunca materializa e nunca é
    escaneado (forjável no Linux, onde `:` é legal). Device names idem."""

    def _git(self, repo: Path, *args: str, input=None) -> str:
        r = subprocess.run(["git", "-C", str(repo), *args], input=input,
                           capture_output=True, text=True, encoding="utf-8")
        if r.returncode:
            raise RuntimeError(r.stderr)
        return r.stdout.strip()

    def _stage_collision(self, names: list[str]) -> int:
        from henke.cli_check import _cmd_check
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self._git(repo, "init", "-q")
            self._git(repo, "config", "core.protectNTFS", "false")
            blobs = {}
            for name in names:
                blob = self._git(repo, "hash-object", "-w", "--stdin",
                                 input="x = 1\n")
                blobs[name] = blob
            for name, sha in blobs.items():
                self._git(repo, "-c", "core.protectNTFS=false",
                          "update-index", "--add", "--cacheinfo",
                          f"100644,{sha},{name}")
            return _cmd_check(repo, staged=True, as_json=True)

    def test_colon_ads_fails_closed(self):
        rc = self._stage_collision(["evil:pkg.py", "evil_pkg.py"])
        self.assertEqual(rc, 2, "nome com `:` (ADS) passou como staged válido")

    def test_device_name_fails_closed(self):
        # Revisão adversarial: `nul `/`con `/`aux .txt` evadiam a checagem
        # por split(".")[0] cru; CONIN$/CONOUT$ e superscripts COM¹-³ eram
        # ausentes do set — todos materializam devices no Windows.
        for name in ("aux", "nul ", "con ", "aux .txt",
                     "CONOUT$", "COM¹", "sub/LPT2/x.py"):
            with self.subTest(name=name):
                rc = self._stage_collision([name])
                self.assertEqual(rc, 2,
                                 f"nome de dispositivo {name!r} passou staged")


class TestSupervisorDeadline(unittest.TestCase):
    """PoC GPT Sol (proof_pipe_wait): wait(0.2) bloqueou 21s quando um
    descendente segurou o pipe de saída. O deadline deve cobrir a operação
    inteira, não só o proc.wait."""

    @unittest.skipUnless(os.name == "nt", "Job Object é específico do Windows")
    def test_wait_honors_deadline_when_descendant_holds_pipe(self):
        import time
        from henke.process_supervisor import ChildSupervisor

        sup = ChildSupervisor()
        try:
            child = sup.start(
                [sys.executable, "-c",
                 "import subprocess,sys; "
                 "subprocess.Popen([sys.executable,'-c','import time;"
                 "time.sleep(60)'], stdout=sys.stdout, stderr=sys.stderr); "
                 "sys.stdout.flush()"],
                cwd=".")
            t0 = time.monotonic()
            res = sup.wait(child, timeout_sec=0.5)
            elapsed = time.monotonic() - t0
        finally:
            sup.close()
        self.assertLess(elapsed, 8.0,
                        f"wait() esticou {elapsed:.1f}s além do deadline")
        self.assertTrue(res.timed_out,
                        f"deadline estourado mas timed_out={res.timed_out} "
                        f"(rc={res.returncode}, reason={res.termination_reason})")


class TestIterTreeJunctionSwap(unittest.TestCase):
    """PoC GPT Sol (proof_iter_tree_junction): um diretório enfileirado como
    real trocado por junction antes do scandir fazia iter_tree emitir e
    hashear arquivo fora da raiz."""

    @unittest.skipUnless(os.name == "nt", "junction é específico do Windows")
    def test_swapped_junction_dir_is_not_traversed(self):
        import subprocess
        from henke.winpath import iter_tree

        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            outside = Path(td) / "outside"
            (repo / "queued").mkdir(parents=True)
            outside.mkdir()
            (outside / "external.txt").write_text("outside\n")

            it = iter_tree(repo)
            first = next(it)
            self.assertEqual((first.relative_path, first.kind),
                             ("queued", "directory"))
            # Troca o diretório por junction exatamente na janela TOCTOU:
            # o stat de descoberta já passou, o pop da fila ainda não
            # aconteceu (o yield suspendeu o gerador entre os dois).
            (repo / "queued").rmdir()
            subprocess.run(  # hazard-ok: SHELL-INJECTION — mklink /J é a única via de junction no Windows; argv fixo, sem shell=True
                ["cmd", "/c", "mklink", "/J",
                 str(repo / "queued"), str(outside)],
                check=True, capture_output=True)

            rest = list(it)
            kinds = {(e.relative_path, e.kind, e.reason) for e in rest}
            self.assertFalse(
                any(e.kind == "file" and "external" in e.relative_path
                    for e in rest),
                f"arquivo externo emitido como conteúdo do repo: {kinds}")
            self.assertTrue(
                any(e.relative_path == "queued"
                    and e.kind in ("skipped", "error") for e in rest),
                f"troca por junction não reportada: {kinds}")


if __name__ == "__main__":
    unittest.main()
