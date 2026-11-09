#!/usr/bin/env python3
"""Regressões de segurança do run_state — achados da banca adversarial v5.

Cada teste trava um falso-verde específico que os revisores encontraram:
- JSON escrito à mão com exit_code:0 NÃO verifica (tríade de evidência exigida);
- stages/dispatch/tests bookkeeping não satisfazem exigência de portão físico;
- adulterar o arquivo de evidência invalida a verificação retroativamente;
- todos os portões em exit 3 (n/a) → blocked, nunca verified;
- driver echo/stdin tem cap 'unverified' que record-gate posterior não apaga;
- mandatory_gates malformado bloqueia em vez de relaxar.
"""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.run_state import (
    _append_evidence_manifest, _evidence_manifest_path, derive_run_outcome,
    verify_run_file, write_anchor_sidecar,
)


def _evidence(tmp: Path, name: str, content: str = "ok\n") -> tuple[str, str]:
    # Evidência real vive sob .henke/evidence/ — o verify confina o
    # evidence_file a esse diretório (anti-forja banca v6).
    ev_dir = tmp / ".henke" / "evidence" / "r"
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev = ev_dir / name
    ev.write_text(content, encoding="utf-8")
    return str(ev), hashlib.sha256(ev.read_bytes()).hexdigest()


def _triad(tmp: Path, gate: str, exit_code: int = 0) -> dict:
    ev, sha = _evidence(tmp, f"{gate}.out")
    # Tríade só verifica com manifesto local (custódia v6): simula o que
    # record_gate escreve fora do repo — incluindo o veredito (banca v9 C1).
    # run_path alimenta a tag de repo do manifesto (banca v10 A3).
    _append_evidence_manifest("r", gate, ev, sha, f"gate {gate}", exit_code, False,
                              run_path=tmp / ".henke" / "runs" / "r.json")
    return {
        "exit_code": exit_code,
        "command": f"gate {gate}",
        "evidence_file": ev,
        "evidence_sha256": sha,
    }


def _run(tmp: Path, gates: dict, **extra) -> Path:
    data = {"run_id": "r", "mode": "EXECUTAR", "archetype": "CARPINTEIRO",
            "outcome": "verified", "gates": gates}
    data.update(extra)
    runs = tmp / ".henke" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    p = runs / "r.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    # Custódia como init_run faria: sidecar de âncoras fora do repo (banca v8
    # F-06 — verify falha fechado sem ele).
    write_anchor_sidecar("r", None, None, None, repo_root=tmp, fields=data)
    return p


class ForgedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        # O manifesto de evidência vive no config dir chaveado por run_id —
        # compartilhado ENTRE testes: entradas gravadas por fixtures anteriores
        # viram órfãs (EVIDENCE-ORPHANED) nas runs seguintes.
        _evidence_manifest_path("r").unlink(missing_ok=True)
        _evidence_manifest_path(
            "r", self.t / ".henke" / "runs" / "r.json").unlink(missing_ok=True)

    def test_bare_exit_zero_json_does_not_verify(self):
        """exit_code:0 escrito à mão é atestação implícita — não conta."""
        p = _run(self.t, {"hazard_scan": {"exit_code": 0}})
        ok, reasons = verify_run_file(p)
        self.assertFalse(ok)
        self.assertTrue(any("NO-GATES-EXECUTED" in r or "CONFLICT" in r for r in reasons))

    def test_bookkeeping_entries_are_not_physical_gates(self):
        """stage_*/dispatch sem tríade não satisfazem exigência de execução."""
        p = _run(self.t, {
            "stage_route": {"exit_code": 0, "summary": "x"},
            "dispatch": {"exit_code": 0, "summary": "x"},
            "tests": {"exit_code": 0, "summary": "3/3"},
        })
        ok, reasons = verify_run_file(p)
        self.assertFalse(ok)
        self.assertTrue(any("NO-GATES-EXECUTED" in r for r in reasons))

    def test_real_triad_verifies(self):
        p = _run(self.t, {"hazard_scan": _triad(self.t, "hazard_scan", 0)})
        ok, reasons = verify_run_file(p)
        self.assertTrue(ok, reasons)

    def test_evidence_tampering_invalidates(self):
        g = _triad(self.t, "hazard_scan", 0)
        p = _run(self.t, {"hazard_scan": g})
        Path(g["evidence_file"]).write_text("adulterado\n", encoding="utf-8")
        ok, reasons = verify_run_file(p)
        self.assertFalse(ok)
        self.assertTrue(any("EVIDENCE" in r for r in reasons), reasons)

    def test_evidence_missing_invalidates(self):
        g = _triad(self.t, "hazard_scan", 0)
        p = _run(self.t, {"hazard_scan": g})
        Path(g["evidence_file"]).unlink()
        ok, reasons = verify_run_file(p)
        self.assertFalse(ok)
        self.assertTrue(any("EVIDENCE" in r for r in reasons), reasons)

    def test_all_na_gates_block(self):
        gates = {
            "a11y_scan": _triad(self.t, "a11y_scan", 3),
            "mutant_hunt": _triad(self.t, "mutant_hunt", 3),
        }
        p = _run(self.t, gates)
        derived, reasons = derive_run_outcome(json.loads(p.read_text()))
        self.assertEqual(derived, "blocked")
        self.assertTrue(any("ALL-GATES-N/A" in r for r in reasons))

    def test_one_green_plus_na_verifies(self):
        gates = {
            "hazard_scan": _triad(self.t, "hazard_scan", 0),
            "a11y_scan": _triad(self.t, "a11y_scan", 3),
        }
        p = _run(self.t, gates)
        ok, reasons = verify_run_file(p)
        self.assertTrue(ok, reasons)

    def test_echo_driver_cap_survives_later_gates(self):
        """record-gate posterior não apaga o cap unverified do driver simulado."""
        data = {
            "run_id": "r", "mode": "EXECUTAR", "driver": "echo",
            "gates": {"hazard_scan": _triad(self.t, "hazard_scan", 0)},
        }
        derived, reasons = derive_run_outcome(data)
        self.assertEqual(derived, "unverified")
        self.assertTrue(any("SIMULATED-DRIVER" in r for r in reasons))

    def test_echo_driver_with_failed_gate_still_fails(self):
        """Falha explícita vence o cap — 'failed' é pior que 'unverified'."""
        data = {
            "run_id": "r", "mode": "EXECUTAR", "driver": "echo",
            "gates": {"hazard_scan": _triad(self.t, "hazard_scan", 1)},
        }
        derived, reasons = derive_run_outcome(data)
        self.assertEqual(derived, "failed")

    def test_manual_failure_counts(self):
        """Atestação manual com exit != 0 reprova mesmo sem tríade."""
        data = {
            "run_id": "r", "mode": "EXECUTAR",
            "gates": {
                "hazard_scan": _triad(self.t, "hazard_scan", 0),
                "anotacao_humana": {"exit_code": 1, "manual": True},
            },
        }
        derived, _ = derive_run_outcome(data)
        self.assertEqual(derived, "failed")

    def test_manual_na_ok(self):
        """Atestação manual exit 3 (n/a) não reprova."""
        data = {
            "run_id": "r", "mode": "EXECUTAR",
            "gates": {
                "hazard_scan": _triad(self.t, "hazard_scan", 0),
                "anotacao": {"exit_code": 3, "manual": True},
            },
        }
        derived, _ = derive_run_outcome(data)
        self.assertEqual(derived, "verified")

    def test_malformed_mandatory_blocks(self):
        """mandatory_gates malformado endurece — nunca relaxa."""
        data = {
            "run_id": "r", "mode": "EXECUTAR",
            "mandatory_gates": "hazard_scan",  # string, não lista
            "gates": {"hazard_scan": _triad(self.t, "hazard_scan", 0)},
        }
        derived, reasons = derive_run_outcome(data)
        self.assertEqual(derived, "blocked")
        self.assertTrue(any("MANDATORY" in r for r in reasons))

    def test_evidence_outside_scope_rejected(self):
        """evidence_file apontando para arquivo do repo (README) não é prova
        de execução — confinamento a .henke/evidence/ (banca v6)."""
        repo_file = self.t / "README.md"
        repo_file.write_text("estável\n", encoding="utf-8")
        sha = hashlib.sha256(repo_file.read_bytes()).hexdigest()
        # Manifesto forjado apontando para o mesmo arquivo fora do confinamento —
        # passa a checagem de manifesto e testa o confinamento isoladamente.
        _append_evidence_manifest("r", "hazard_scan", str(repo_file), sha, "x", 0, False,
                                  run_path=self.t / ".henke" / "runs" / "r.json")
        p = _run(self.t, {"hazard_scan": {
            "exit_code": 0, "command": "x",
            "evidence_file": str(repo_file), "evidence_sha256": sha,
        }})
        ok, reasons = verify_run_file(p)
        self.assertFalse(ok)
        self.assertTrue(any("OUTSIDE-SCOPE" in r for r in reasons), reasons)

    def test_evidence_traversal_rejected(self):
        """evidence_file com .. saindo do repo é rejeitado."""
        outside = self.t.parent / "fora.txt"
        outside.write_text("x\n", encoding="utf-8")
        sha = hashlib.sha256(outside.read_bytes()).hexdigest()
        _append_evidence_manifest("r", "hazard_scan", str(outside), sha, "x", 0, False,
                                  run_path=self.t / ".henke" / "runs" / "r.json")
        p = _run(self.t, {"hazard_scan": {
            "exit_code": 0, "command": "x",
            "evidence_file": str(outside), "evidence_sha256": sha,
        }})
        ok, reasons = verify_run_file(p)
        self.assertFalse(ok)
        self.assertTrue(any("OUTSIDE-SCOPE" in r or "EVIDENCE" in r for r in reasons), reasons)

    def test_mandatory_gate_stem_matching(self):
        """'hazard' casa 'scripts/metricas/hazard_scan.py'? Não — casa pelo stem
        do nome registrado do portão (ambos os lados normalizados)."""
        data = {
            "run_id": "r", "mode": "EXECUTAR",
            "mandatory_gates": ["hazard_scan"],
            "gates": {"scripts/metricas/hazard_scan.py": _triad(self.t, "hs", 0)},
        }
        derived, reasons = derive_run_outcome(data)
        self.assertEqual(derived, "verified", reasons)


if __name__ == "__main__":
    unittest.main()
