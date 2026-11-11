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
        f"{rec['testes_passados']}|{rec['stdout_sha256']}".encode()).hexdigest()


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

    def test_ledger_edit_passed_fails_crosscheck(self):
        prev = "0" * 64
        rec = _entry(prev, 0, "")
        rec["entry_hash"] = _pipe(prev, rec)
        with tempfile.TemporaryDirectory() as td:
            hd = Path(td) / ".henke"
            hd.mkdir()
            log_p = hd / "exec_log.jsonl"
            log_p.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            # ledger declara passed=1 mas o log gravou 0 — divergência
            entry = {"cmd": "python tests.py", "exit": 0,
                     "testes_descobertos": 1, "testes_passados": 1,
                     "stdout_sha256": "a" * 64}
            ok, errors, _ = audit_ledger.audit_ledger_content(
                [entry], exec_log_path=log_p, require_exec_log=True)
            self.assertFalse(ok)
            self.assertTrue(any("UNVERIFIED_EXECUTION_EVIDENCE" in e
                                for e in errors), errors)


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
        rc = self._stage_collision(["aux"])
        self.assertEqual(rc, 2, "nome de dispositivo passou como staged válido")


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

            # Troca o diretório real por junction ANTES do iter_tree pop —
            # mesmo efeito do swap entre stat e scandir.
            (repo / "queued").rmdir()
            subprocess.run(  # hazard-ok: SHELL-INJECTION — mklink /J é a única via de junction no Windows; argv fixo, sem shell=True
                ["cmd", "/c", "mklink", "/J",
                 str(repo / "queued"), str(outside)],
                check=True, capture_output=True)

            emitted = list(iter_tree(repo))
            kinds = {(e.relative_path, e.kind, e.reason) for e in emitted}
            self.assertFalse(
                any(e.relative_path == "queued/external.txt"
                    and e.kind == "file" for e in emitted),
                f"arquivo externo emitido como conteúdo do repo: {kinds}")
            self.assertTrue(
                any(e.relative_path == "queued"
                    and e.kind in ("skipped", "error") for e in emitted),
                f"troca por junction não reportada: {kinds}")


if __name__ == "__main__":
    unittest.main()
