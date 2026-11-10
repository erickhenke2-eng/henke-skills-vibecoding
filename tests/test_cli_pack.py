"""Testes do pacote CLI v4.0+: run/report/status/setup/auto/goldfish/exec --timeout.

Cobre o roundtrip `run init` + `record-gate` + `verify`, as 5 seções do `report`,
a propagação de `--timeout` no `exec` (flag > PROJECT.json > 180s), o HARD STOP
honesto do `goldfish` sem `--driver`, os stubs idempotentes do `setup` e a
garantia de que `auto --dry-run` não escreve nada no repositório.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("henke_cli", ROOT / "scripts" / "henke_cli.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


def run_cli(args):
    """Invoca henke_cli.main in-process capturando stdout/stderr (exit code retornado)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def tree_snapshot(root: Path) -> dict:
    """Mapa {relpath: bytes} de todos os arquivos do diretório (para detectar escritas)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


class CliPackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)

    # ------------------------------------------------------------------
    def test_run_init_record_gate_verify_roundtrip(self):
        """run init -> verify (blocked=1) -> record-gate (atestação) -> verify segue 1."""
        code, out, err = run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "t1"])
        self.assertEqual(code, 0, err)
        run_file = self.repo / ".henke" / "runs" / "t1.json"
        self.assertTrue(run_file.is_file())

        # Sem portões registrados, a derivação mecânica resulta 'blocked' -> exit 1
        code, _, _ = run_cli(["run", "verify", str(run_file)])
        self.assertEqual(code, 1)

        ev = self.repo / "evidence.out"
        ev.write_text("saída real do portão\n", encoding="utf-8")
        code, out, err = run_cli([
            "run", "record-gate", str(run_file),
            "--gate", "hazard_scan", "--exit-code", "0", "--summary", "ok",
        ])
        self.assertEqual(code, 0, err)

        # F-01: a superfície CLI só emite atestação — tríade física é domínio
        # da pipeline; verify não libera por record-gate manual.
        code, out, err = run_cli(["run", "verify", str(run_file)])
        self.assertEqual(code, 1, out + err)

    # ------------------------------------------------------------------
    def test_report_produz_as_5_secoes(self):
        """`henke report <id>` renderiza as 5 seções fixas em markdown PT-BR."""
        run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "rep1"])
        run_file = self.repo / ".henke" / "runs" / "rep1.json"
        run_cli(["run", "record-gate", str(run_file), "--gate", "hazard_scan",
                 "--exit-code", "0", "--summary", "limpo"])

        code, out, err = run_cli(["report", "rep1", "--repo", str(self.repo)])
        self.assertEqual(code, 0, err)
        for sec in (
            "## O que foi pedido",
            "## O que mudou",
            "## O que foi verificado",
            "## O que NÃO foi verificado",
            "## Próximo passo",
        ):
            self.assertIn(sec, out)
        # Campos ausentes devem ser declarados, nunca inventados
        self.assertIn("não registrado", out)

        # Run inexistente -> exit 2
        code, _, err = run_cli(["report", "nao-existe", "--repo", str(self.repo)])
        self.assertEqual(code, 2)

    # ------------------------------------------------------------------
    def test_manual_record_gate_nao_conta_como_portao_fisico(self):
        """Audit do Kimi: record-gate SEM --command/--evidence-file → manual (blocked)."""
        run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "m1"])
        run_file = self.repo / ".henke" / "runs" / "m1.json"

        code, out, _ = run_cli([
            "run", "record-gate", str(run_file),
            "--gate", "hazard_scan", "--exit-code", "0", "--summary", "ok sem prova",
        ])
        self.assertEqual(code, 0)
        self.assertIn("atestação manual", out)

        data = json.loads(run_file.read_text(encoding="utf-8"))
        self.assertTrue(data["gates"]["hazard_scan"]["manual"])
        self.assertNotIn("evidence_sha256", data["gates"]["hazard_scan"])

        code, out, err = run_cli(["run", "verify", str(run_file)])
        self.assertEqual(code, 1, "atestação manual NUNCA pode liberar verify")

    # ------------------------------------------------------------------
    def test_record_gate_com_evidencia_e_atestacao_mesmo_com_triade(self):
        """F-01+M2: --command/--evidence-file são RECUSADOS no CLI — atestação
        manual não carrega tríade (antes envenenava a run para sempre:
        EVIDENCE-UNMANIFESTED sem caminho oficial de reparo)."""
        run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "e1"])
        run_file = self.repo / ".henke" / "runs" / "e1.json"
        ev = self.repo / "ev.out"
        ev.write_text("saída real\n", encoding="utf-8")

        code, _, err = run_cli([
            "run", "record-gate", str(run_file),
            "--gate", "hazard_scan", "--exit-code", "0",
            "--command", "python hazard_scan.py",
            "--evidence-file", str(ev),
        ])
        self.assertEqual(code, 2)
        self.assertIn("evidência física", err)
        data = json.loads(run_file.read_text(encoding="utf-8"))
        self.assertNotIn("hazard_scan", data.get("gates") or {})

        # Sem tríade, a atestação manual segue funcionando.
        code, _, _ = run_cli([
            "run", "record-gate", str(run_file),
            "--gate", "hazard_scan", "--exit-code", "0",
        ])
        self.assertEqual(code, 0)
        data = json.loads(run_file.read_text(encoding="utf-8"))
        entry = data["gates"]["hazard_scan"]
        self.assertTrue(entry.get("manual"), "CLI nunca emite tríade física")
        self.assertNotIn("evidence_sha256", entry)

        code, _, _ = run_cli(["run", "verify", str(run_file)])
        self.assertEqual(code, 1, "atestação não libera verify")

    # ------------------------------------------------------------------
    def test_exec_timeout_flag_e_project_json(self):
        """--timeout propaga; sem flag, honra timeout_sec de .henke/PROJECT.json; fallback 180."""
        captured = {}

        class _FakeProc:
            returncode = 0
            pid = 1

            def communicate(self, timeout=None):
                captured["timeout"] = timeout
                return b"", b""

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        with mock.patch.object(subprocess, "Popen", fake_popen):
            code, _, err = run_cli([
                "exec", "--repo", str(self.repo), "--timeout", "7", "--", "echo", "hi",
            ])
            self.assertEqual(code, 0, err)
            self.assertEqual(captured.get("timeout"), 7.0)

            # Fallback para .henke/PROJECT.json
            (self.repo / ".henke").mkdir(exist_ok=True)
            (self.repo / ".henke" / "PROJECT.json").write_text(
                json.dumps({"timeout_sec": 3}), encoding="utf-8"
            )
            code, _, err = run_cli(["exec", "--repo", str(self.repo), "--", "echo", "hi"])
            self.assertEqual(code, 0, err)
            self.assertEqual(captured.get("timeout"), 3.0)

            # Sem flag e sem campo -> fallback 180
            (self.repo / ".henke" / "PROJECT.json").write_text("{}", encoding="utf-8")
            code, _, err = run_cli(["exec", "--repo", str(self.repo), "--", "echo", "hi"])
            self.assertEqual(code, 0, err)
            self.assertEqual(captured.get("timeout"), 180.0)

    # ------------------------------------------------------------------
    def test_goldfish_sem_driver_exit2_e_unavailable(self):
        """goldfish sem --driver: HARD STOP -> exit 2 + review_status=goldfish_unavailable."""
        run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "gf1"])
        run_file = self.repo / ".henke" / "runs" / "gf1.json"

        code, out, err = run_cli(["goldfish", "--repo", str(self.repo), "--run", str(run_file)])
        self.assertEqual(code, 2)
        data = json.loads(run_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("review_status"), "goldfish_unavailable")
        self.assertIn("driver", (out + err).lower())

    def test_goldfish_com_driver_gera_envelope(self):
        """goldfish --driver <cmd>: grava envelope spec+diff SEM transcript -> exit 0."""
        run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "gf2"])
        spec = self.repo / "spec.md"
        spec.write_text("# Spec\n\n## 4. Escopo Declarado\n- x.py\n", encoding="utf-8")

        code, out, err = run_cli([
            "goldfish", "--repo", str(self.repo), "--driver", "echo revisor",
            "--spec", "spec.md", "--run", "gf2",
        ])
        self.assertEqual(code, 0, err)
        env_file = self.repo / ".henke" / "runs" / "gf2.goldfish.json"
        self.assertTrue(env_file.is_file())
        env = json.loads(env_file.read_text(encoding="utf-8"))
        self.assertEqual(env.get("driver"), "echo revisor")
        self.assertIn("Spec", env.get("spec") or "")
        self.assertNotIn("transcript desta sessão", json.dumps(env.get("diff") or ""))

    # ------------------------------------------------------------------
    def test_setup_cria_stubs_sem_sobrescrever(self):
        """setup: cria PROJECT.json (se henke_detect presente) + POLICY.json exato; idempotente."""
        has_detect = importlib.util.find_spec("scripts.henke_detect") is not None
        code, out, err = run_cli(["setup", str(self.repo)])
        if not has_detect:
            # Módulo de outro worker ausente -> mensagem honesta + exit 2, sem escrita parcial
            self.assertEqual(code, 2)
            self.assertIn("henke_detect", out + err)
            return

        self.assertEqual(code, 0, err)
        henke_dir = self.repo / ".henke"
        self.assertTrue((henke_dir / "PROJECT.json").is_file())
        policy_path = henke_dir / "POLICY.json"
        self.assertTrue(policy_path.is_file())
        self.assertEqual(
            json.loads(policy_path.read_text(encoding="utf-8")),
            {
                "policy": "balanced",
                "always_ask": ["publish", "destructive", "metaconfig"],
                "auto_approve": ["non-publish"],
                "allowed_cmds": [],
            },
        )
        self.assertTrue((henke_dir / "DIARIO.md").is_file())
        self.assertTrue((henke_dir / "DECISOES.md").is_file())
        self.assertTrue((henke_dir / "CONFIG.json").is_file())

        # Idempotência: arquivo existente NUNCA é sobrescrito
        policy_path.write_text('{"custom": 1}', encoding="utf-8")
        code, out, err = run_cli(["setup", str(self.repo)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(policy_path.read_text(encoding="utf-8")), {"custom": 1})
        self.assertIn("Preservados", out)

    # ------------------------------------------------------------------
    def test_auto_dry_run_nao_escreve_nada(self):
        """auto --dry-run: não cria nenhum arquivo no repo (presente ou ausente o módulo)."""
        before = tree_snapshot(self.repo)
        code, out, err = run_cli([
            "auto", "--dry-run", "--repo", str(self.repo), "refatora", "o", "login",
        ])
        # 0 se henke_auto estiver instalado e honrar dry-run; 2 (honesto) se ausente
        self.assertIn(code, (0, 2), out + err)
        self.assertEqual(before, tree_snapshot(self.repo))

    # ------------------------------------------------------------------
    def test_status_mostra_outcome_derivado(self):
        """status/run list: exibe outcome DERIVADO mesmo com 'verified' adulterado no arquivo."""
        run_cli(["run", "init", "--repo", str(self.repo), "--run-id", "s1"])
        run_file = self.repo / ".henke" / "runs" / "s1.json"
        data = json.loads(run_file.read_text(encoding="utf-8"))
        data["outcome"] = "verified"  # auto-atestado fraudulento
        run_file.write_text(json.dumps(data), encoding="utf-8")

        code, out, err = run_cli(["status", "--repo", str(self.repo)])
        self.assertEqual(code, 0, err)
        self.assertIn("s1", out)
        self.assertIn("blocked", out)  # derivado (sem portões), não o 'verified' declarado

        code, out, err = run_cli(["run", "list", "--repo", str(self.repo)])
        self.assertEqual(code, 0, err)
        self.assertIn("s1", out)


if __name__ == "__main__":
    unittest.main()
