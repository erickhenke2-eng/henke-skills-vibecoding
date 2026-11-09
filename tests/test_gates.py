#!/usr/bin/env python3
"""
Testes do executor mecânico de portões (scripts/henke_gates.py — proposta M3).

Cobre: vereditos por path (exit/sha/stderr_tail), ordem preservada sem parar no
primeiro vermelho, portão desconhecido -> exit 2 sem exceção, despacho por nome
via GATE_MAP (patch.dict), --from-route com JSON fake, gravação em run real via
run_state.init_run e abort (FileNotFoundError / exit 2) quando a run não existe.
"""
from pathlib import Path
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import scripts.henke_gates as henke_gates
from scripts.henke_gates import MAX_REWORK, main as gates_main, run_gates
from scripts.run_state import derive_run_outcome, init_run, record_gate

GATE_OK_SRC = (
    "import sys\n"
    "print('portao-fake-ok')\n"
    "sys.exit(0)\n"
)
GATE_FAIL_SRC = (
    "import sys\n"
    "print('portao-fake-fail')\n"
    "print('detalhe do erro', file=sys.stderr)\n"
    "sys.exit(1)\n"
)
GATE_NA_SRC = (
    "import sys\n"
    "print('portao-fake-na')\n"
    "sys.exit(3)\n"
)

VERDICT_KEYS = {"gate", "cmd", "exit", "na", "duration_ms", "stdout_sha256", "stderr_tail"}


def sha(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


class GatesRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # resolve(): run_gates/run_state normalizam internamente (8.3 no Windows)
        self.repo = Path(self.temp.name).resolve()
        self.ok_script = self.repo / "fake_ok.py"
        self.ok_script.write_text(GATE_OK_SRC, encoding="utf-8")
        self.fail_script = self.repo / "fake_fail.py"
        self.fail_script.write_text(GATE_FAIL_SRC, encoding="utf-8")

    # ---------- núcleo: vereditos ----------

    def test_verdicts_exit_sha_e_stderr_tail(self):
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script), str(self.fail_script)], record=False)
        self.assertEqual(res["ran"], 2)
        self.assertFalse(res["all_green"])
        v_ok, v_fail = res["verdicts"]
        self.assertEqual(v_ok["gate"], "fake_ok")
        self.assertEqual(v_ok["exit"], 0)
        self.assertEqual(v_ok["stdout_sha256"], sha("portao-fake-ok\n"))
        self.assertEqual(v_ok["stderr_tail"], "")
        self.assertEqual(v_fail["gate"], "fake_fail")
        self.assertEqual(v_fail["exit"], 1)
        self.assertEqual(v_fail["stdout_sha256"], sha("portao-fake-fail\n"))
        self.assertIn("detalhe do erro", v_fail["stderr_tail"])
        for v in res["verdicts"]:
            self.assertEqual(set(v), VERDICT_KEYS)
            self.assertIsInstance(v["duration_ms"], int)
            self.assertIn(str(self.repo), v["cmd"])  # path não mapeado -> arg <repo>

    def test_ordem_preservada_e_nao_para_no_vermelho(self):
        res = run_gates(
            str(self.repo),
            [str(self.fail_script), str(self.ok_script), str(self.fail_script)],
            record=False,
            allow_external_gates=True,
        )
        self.assertEqual([v["exit"] for v in res["verdicts"]], [1, 0, 1])
        self.assertEqual(res["ran"], 3)
        self.assertFalse(res["all_green"])

    def test_todos_verdes(self):
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script)], record=False)
        self.assertTrue(res["all_green"])

    def test_portao_desconhecido_exit2_sem_excecao(self):
        res = run_gates(str(self.repo), allow_external_gates=True, gates=["nao-existe", "scripts/tambem_nao.py"], record=False)
        self.assertEqual([v["exit"] for v in res["verdicts"]], [2, 2])
        self.assertFalse(res["all_green"])
        self.assertEqual(res["verdicts"][0]["gate"], "nao-existe")
        self.assertEqual(res["verdicts"][1]["gate"], "tambem_nao")
        self.assertIn("desconhecido", res["verdicts"][0]["stderr_tail"])
        self.assertEqual(res["verdicts"][0]["cmd"], "")

    def test_despacho_por_nome_via_gate_map(self):
        # patch.dict injeta nome fake na tabela real (restaurada ao sair do with)
        with mock.patch.dict(henke_gates.GATE_MAP, {"fakeok": self.ok_script}):
            res = run_gates(str(self.repo), allow_external_gates=True, gates=["fakeok"], record=False)
        v = res["verdicts"][0]
        self.assertEqual(v["exit"], 0)
        self.assertEqual(v["gate"], "fake_ok")
        self.assertIn("fake_ok.py", v["cmd"])

    def test_path_mapeado_usa_args_default_do_gate(self):
        # 'assertless' é mapeado e tem default <repo>/tests quando o dir existe
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_x.py").write_text(
            "def test_x():\n    assert True\n", encoding="utf-8"
        )
        res = run_gates(str(self.repo), allow_external_gates=True, gates=["scripts/metricas/assertless_scan.py"], record=False)
        v = res["verdicts"][0]
        self.assertEqual(v["gate"], "assertless_scan")
        self.assertIn(str(self.repo / "tests"), v["cmd"])

    def test_max_rework_contrato(self):
        self.assertEqual(henke_gates.MAX_REWORK, 2)
        self.assertEqual(MAX_REWORK, 2)

    # ---------- veredito n/a (exit 3): rodou, reportou "nada a verificar" ----------

    def test_na_nao_reprova_quando_ha_verde_real(self):
        na_script = self.repo / "fake_na.py"
        na_script.write_text(GATE_NA_SRC, encoding="utf-8")
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script), str(na_script)], record=False)
        self.assertTrue(res["all_green"])
        self.assertEqual([v["exit"] for v in res["verdicts"]], [0, 3])
        self.assertFalse(res["verdicts"][0]["na"])
        self.assertTrue(res["verdicts"][1]["na"])

    def test_na_sozinho_nunca_e_verde(self):
        na_script = self.repo / "fake_na.py"
        na_script.write_text(GATE_NA_SRC, encoding="utf-8")
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(na_script), str(na_script)], record=False)
        self.assertEqual(res["ran"], 2)
        self.assertTrue(all(v["na"] for v in res["verdicts"]))
        self.assertFalse(res["all_green"])

    # ---------- run_state: exit 3 na derivação mecânica ----------

    def _run_with_gates(self, gate_exits: dict, mandatory: list | None = None):
        run_path = init_run(self.repo, "run-na-test", mode="EXECUTAR",
                            mandatory_gates=mandatory)
        for gate, ec in gate_exits.items():
            ev = self.repo / f"ev_{gate}.out"
            ev.write_text(f"evidencia {gate} exit {ec}", encoding="utf-8")
            record_gate(run_path, gate, ec, f"{gate} → exit {ec}",
                        command=f"python {gate}.py", evidence_file=ev)
        return json.loads(run_path.read_text(encoding="utf-8"))

    def test_derive_na_gate_nao_reprova_mas_exige_verde(self):
        data = self._run_with_gates({"hazard_scan": 0, "a11y_scan": 3, "mutant_hunt": 3})
        outcome, reasons = derive_run_outcome(data)
        self.assertEqual(outcome, "verified", reasons)

    def test_derive_todos_na_e_blocked_antitautologia(self):
        data = self._run_with_gates({"a11y_scan": 3, "mutant_hunt": 3})
        outcome, reasons = derive_run_outcome(data)
        self.assertEqual(outcome, "blocked")
        self.assertTrue(any("ALL-GATES-N/A" in r for r in reasons), reasons)

    def test_derive_mandatory_satisfeito_por_veredito_na(self):
        data = self._run_with_gates(
            {"hazard_scan": 0, "a11y_scan": 3},
            mandatory=["scripts/metricas/hazard_scan.py", "scripts/metricas/a11y_scan.py"],
        )
        outcome, reasons = derive_run_outcome(data)
        self.assertEqual(outcome, "verified", reasons)

    def test_derive_mandatory_ausente_mesmo_com_na_presente(self):
        data = self._run_with_gates(
            {"hazard_scan": 0, "a11y_scan": 3},
            mandatory=["scripts/metricas/hazard_scan.py", "scripts/metricas/ratchet.py"],
        )
        outcome, reasons = derive_run_outcome(data)
        self.assertEqual(outcome, "blocked")
        self.assertTrue(any("ratchet" in r for r in reasons), reasons)

    def test_shape_do_resultado(self):
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[], record=False)
        self.assertEqual(set(res), {"verdicts", "all_green", "ran", "skipped"})
        self.assertEqual(res["ran"], 0)
        self.assertFalse(res["all_green"])  # ran=0 é NÃO-VEREDITO, não verde

    # ---------- from-route ----------

    def test_from_route_lendo_gates_required(self):
        route = self.repo / "route.json"
        route.write_text(json.dumps({
            "mode": "EXECUTAR",
            "archetype": "CARPINTEIRO",
            "gates_required": [str(self.ok_script), str(self.fail_script)],
        }), encoding="utf-8")
        gates = henke_gates._load_route_gates(route)
        self.assertEqual(len(gates), 2)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gates_main(["--repo", str(self.repo), "--from-route", str(route), "--json"])
        self.assertEqual(rc, 1)
        payload = json.loads(buf.getvalue())
        # Route é plantável (vive no repo) — paths fora da suíte são recusados
        # com exit 2 (banca v8 F-04); só --gates explícito aceita externos.
        self.assertEqual([v["exit"] for v in payload["verdicts"]], [2, 2])
        self.assertFalse(payload["all_green"])

    def test_from_route_invalido_exit2(self):
        ruim = self.repo / "ruim.json"
        ruim.write_text('{"mode": "EXECUTAR"}', encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = gates_main(["--repo", str(self.repo), "--from-route", str(ruim)])
        self.assertEqual(rc, 2)

    # ---------- record em run_state ----------

    def test_record_grava_vereditos_na_run(self):
        init_run(self.repo, "t-run", mode="ANALISAR", archetype="SHERLOCK")
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script)], run_id="t-run", record=True)
        self.assertTrue(res["all_green"])
        data = json.loads((self.repo / ".henke" / "runs" / "t-run.json").read_text(encoding="utf-8"))
        self.assertIn("fake_ok", data["gates"])
        self.assertEqual(data["gates"]["fake_ok"]["exit_code"], 0)
        self.assertEqual(data["outcome"], "verified")
        # portão vermelho também é gravado e derruba a run
        res2 = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.fail_script)], run_id="t-run", record=True)
        self.assertFalse(res2["all_green"])
        data2 = json.loads((self.repo / ".henke" / "runs" / "t-run.json").read_text(encoding="utf-8"))
        self.assertEqual(data2["gates"]["fake_fail"]["exit_code"], 1)
        self.assertEqual(data2["outcome"], "failed")

    def test_record_aborta_quando_run_inexistente(self):
        with self.assertRaises(FileNotFoundError):
            run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script)], run_id="ghost", record=True)
        # CLI converte em exit 2
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = gates_main([
                "--repo", str(self.repo), "--gates", str(self.ok_script), "--run", "ghost",
            ])
        self.assertEqual(rc, 2)
        self.assertIn("run", buf.getvalue().lower())

    def test_sem_run_id_nao_grava(self):
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script)], record=True)  # run_id=None
        self.assertTrue(res["all_green"])
        self.assertFalse((self.repo / ".henke" / "runs").exists())

    # ---------- CLI ----------

    def test_cli_exit_codes(self):
        for argv, esperado in (
            (["--repo", str(self.repo), "--gates", f"{self.ok_script},{self.fail_script}"], 1),
            (["--repo", str(self.repo), "--gates", str(self.ok_script)], 0),
            (["--repo", str(self.repo), "--gates", "inexistente"], 1),   # exit 2 do portão reprova -> CLI 1
            (["--repo", str(self.repo)], 2),                              # sem --gates/--from-route
            (["--repo", str(self.repo), "--gates", " , "], 2),            # lista vazia
        ):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = gates_main(list(argv))
            self.assertEqual(rc, esperado, f"argv={argv} -> rc={rc}, esperado={esperado}")

    def test_cli_resumo_ptbr(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = gates_main(["--repo", str(self.repo), "--gates", str(self.ok_script)])
        self.assertEqual(rc, 0)
        saida = buf.getvalue()
        self.assertIn("[PASS] fake_ok", saida)
        self.assertIn("RESULTADO: VERDE", saida)


class GatesRunnerKimiAuditTests(unittest.TestCase):
    """Regressões do audit do Kimi: skip!=verde, ran=0!=verde, evidência física."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name).resolve()
        self.ok_script = self.repo / "fake_ok.py"
        self.ok_script.write_text(GATE_OK_SRC, encoding="utf-8")

    def test_all_green_false_em_zero_verdicts(self):
        res = run_gates(str(self.repo), allow_external_gates=True, gates=["nao-existe"], record=False)
        # gate desconhecido → verdict exit 2 — mas zero verde também reprova com ran>0
        self.assertFalse(res["all_green"])
        res_empty = run_gates(str(self.repo), allow_external_gates=True, gates=[], record=False)
        self.assertFalse(res_empty["all_green"], "ran=0 deve ser não-veredito")
        self.assertEqual(res_empty["ran"], 0)

    def test_skipped_gate_nao_conta_como_verde(self):
        # 'ratchet' é portão conhecido sem args autoexecutáveis → skipped (não verdict)
        res = run_gates(str(self.repo), allow_external_gates=True, gates=["ratchet"], record=False)
        self.assertEqual(res["ran"], 0)
        self.assertEqual(len(res["skipped"]), 1)
        self.assertEqual(res["skipped"][0]["gate"], "ratchet")
        self.assertFalse(res["all_green"], "portão pulado NUNCA pode dar all_green")

    def test_evidence_file_criado_e_sha_registrado(self):
        init_run(self.repo, "ev-run", mode="ANALISAR", archetype="SHERLOCK")
        res = run_gates(str(self.repo), allow_external_gates=True, gates=[str(self.ok_script)], run_id="ev-run", record=True)
        ev_dir = self.repo / ".henke" / "evidence" / "ev-run"
        evs = list(ev_dir.glob("*.out"))
        self.assertTrue(evs, "saída física do portão deve ser persistida")
        run = json.loads((self.repo / ".henke" / "runs" / "ev-run.json").read_text(encoding="utf-8"))
        entry = run["gates"]["fake_ok"]
        self.assertEqual(entry["command"], res["verdicts"][0]["cmd"])
        self.assertEqual(entry["evidence_file"], str(evs[0]))
        self.assertEqual(
            entry["evidence_sha256"], hashlib.sha256(evs[0].read_bytes()).hexdigest()
        )
        self.assertNotIn("manual", entry, "portão com evidência não é atestação manual")


if __name__ == "__main__":
    unittest.main(verbosity=2)
