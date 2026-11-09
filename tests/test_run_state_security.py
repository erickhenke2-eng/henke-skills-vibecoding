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

from scripts.run_state import derive_run_outcome, verify_run_file


def _evidence(tmp: Path, name: str, content: str = "ok\n") -> tuple[str, str]:
    ev = tmp / name
    ev.write_text(content, encoding="utf-8")
    return str(ev), hashlib.sha256(ev.read_bytes()).hexdigest()


def _triad(tmp: Path, gate: str, exit_code: int = 0) -> dict:
    ev, sha = _evidence(tmp, f"{gate}.out")
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
    p = tmp / "run.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class ForgedRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)

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
