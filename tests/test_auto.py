#!/usr/bin/env python3
"""Testes da máquina de uma frase (scripts/henke_auto.py) — propostas M1+M2+M8.

Cobre: --dry-run sem escrita, envelope com SKILL.md verbatim + sha256 (M2),
trava de autorização PUBLISH/destrutivo sem exceção para --yes/yolo (M8),
fail-closed de driver ausente/inválido, política strict vs balanced e
mapeamento de outcome derivado para exit codes.

Fixtures em tempfile (sem .git — run_state não exige git; os portões e o
`henke exec` internos são emulados via seams `_invoke_gates`/`_invoke_test_cmd`
para o teste ficar determinístico e rápido).
"""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts import henke_auto
from scripts.run_state import approve_run, record_gate
import subprocess

SUITE = Path(henke_auto.SUITE_ROOT)

PROMPT_EXEC = "faz uma mudança cirúrgica no parser"      # EXECUTAR, escopo comum
PROMPT_PUBLISH = "faz deploy pra produção agora"          # requires_human_auth


def _ns(**kw):
    base = dict(
        prompt=None, repo=".", driver=None, dry_run=False,
        yes=False, policy=None, timeout=None, resume=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _fake_preflight_ok(repo, gates, spec_path=None):
    """Emula _preflight_gates com todos os portões viáveis."""
    return True, []


def _record_physical_gate(rp: Path, repo, run_id: str, name: str, ec: int, summary: str):
    """Emula run_gates fielmente: evidência em disco + tríade completa na run."""
    ev_dir = Path(repo) / ".henke" / "evidence" / run_id
    ev_dir.mkdir(parents=True, exist_ok=True)
    ev = ev_dir / f"{name}.out"
    ev.write_text(f"fake gate output: {summary}\n", encoding="utf-8")
    record_gate(rp, name, ec, summary,
                command=f"fake-gate {name}", evidence_file=ev)


def _fake_gates_ok(repo, gates, run_id, timeout_sec, test_cmd=None):
    """Emula henke_gates.run_gates(record=True): grava cada portão verde na run."""
    rp = Path(repo) / ".henke" / "runs" / f"{run_id}.json"
    verdicts = []
    for g in gates:
        _record_physical_gate(rp, repo, run_id, Path(g).stem, 0, "stub green (teste)")
        verdicts.append({"gate": g, "exit_code": 0, "passed": True})
    return {"verdicts": verdicts, "all_green": True, "ran": len(gates)}


def _fake_gates_red(repo, gates, run_id, timeout_sec, test_cmd=None):
    """Emula run_gates com o primeiro portão vermelho."""
    rp = Path(repo) / ".henke" / "runs" / f"{run_id}.json"
    verdicts = []
    for i, g in enumerate(gates):
        ec = 1 if i == 0 else 0
        _record_physical_gate(rp, repo, run_id, Path(g).stem, ec,
                              "ACHADO-TESTE" if ec else "stub green")
        verdicts.append({"gate": g, "exit_code": ec, "passed": ec == 0, "output": "ACHADO-TESTE"})
    return {"verdicts": verdicts, "all_green": False, "ran": len(gates)}


def _fake_test_ok(repo, test_cmd, timeout_sec):
    return 0, "Ran 3 tests in 0.01s\nOK\n"


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


def _run_files(repo: Path) -> list[Path]:
    runs_dir = repo / ".henke" / "runs"
    if not runs_dir.is_dir():
        return []
    return [p for p in runs_dir.glob("*.json") if ".envelope." not in p.name]


def _envelope_files(repo: Path) -> list[Path]:
    runs_dir = repo / ".henke" / "runs"
    if not runs_dir.is_dir():
        return []
    return list(runs_dir.glob("*.envelope.json"))


class AutoDryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")

    def test_dry_run_prints_plan_and_writes_nothing(self):
        before = _tree(self.repo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), dry_run=True,
            ))
        self.assertEqual(rc, 0)
        self.assertEqual(_tree(self.repo), before, "dry-run não pode escrever arquivo algum")
        plan = json.loads(buf.getvalue())
        for key in ("mode", "archetype", "confidence", "skills", "gates_required", "test_cmd"):
            self.assertIn(key, plan)
        self.assertEqual(plan["mode"], "EXECUTAR")
        self.assertTrue(plan["dry_run"])

    def test_dry_run_publish_still_writes_nothing(self):
        before = _tree(self.repo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_PUBLISH, repo=str(self.repo), dry_run=True,
            ))
        self.assertEqual(rc, 0)
        self.assertEqual(_tree(self.repo), before)
        plan = json.loads(buf.getvalue())
        self.assertTrue(plan["requires_human_auth"])


class AutoDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")

    def test_echo_persists_envelope_with_verbatim_sha256(self):
        buf = io.StringIO()
        with mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="echo",
            ))
        # echo é simulação: outcome limitado a 'unverified' → exit 2, nunca 0
        self.assertEqual(rc, 2, buf.getvalue())

        envs = _envelope_files(self.repo)
        self.assertEqual(len(envs), 1, "driver echo deve persistir o envelope")
        env = json.loads(envs[0].read_text(encoding="utf-8"))
        self.assertEqual(env["prompt"], PROMPT_EXEC)
        self.assertEqual(env["tools"], ["read", "write", "list", "exec", "gate", "run_state"])
        self.assertTrue(env["skills"], "route deveria retornar >=1 skill para o prompt EXEC")
        for sk in env["skills"]:
            raw = Path(sk["path"]).read_bytes()
            self.assertEqual(
                sk["sha256"], hashlib.sha256(raw).hexdigest(),
                f"sha256 do envelope diverge do SKILL.md em disco: {sk['name']}",
            )
            self.assertEqual(sk["content"], raw.decode("utf-8"), "conteúdo não é verbatim")

        run_file = envs[0].with_name(envs[0].name.replace(".envelope.json", ".json"))
        run = json.loads(run_file.read_text(encoding="utf-8"))
        self.assertEqual(run["outcome"], "unverified")
        self.assertTrue(
            any("SIMULATED-DRIVER" in r or "ECHO-UNVERIFIED" in r for r in run.get("reasons", [])),
            run.get("reasons"),
        )
        self.assertEqual(
            [s["sha256"] for s in run.get("skills_rodadas", [])],
            [s["sha256"] for s in env["skills"]],
            "run deve registrar skills_rodadas com o sha256 injetado (prova M2)",
        )
        # Estágios registrados na run
        stages = [s.get("stage") for s in run.get("stages", [])]
        for st in ("route", "authorize", "envelope", "dispatch", "gates"):
            self.assertIn(st, stages)

    def test_policy_efetiva_schema_and_no_protected_bootstrap(self):
        """Custódia (banca v7): a pipeline NUNCA materializa .henke/POLICY.json
        nem PROJECT.json — ambos estão na lista protegida do metaconfig e cada
        primeiro `henke auto` se auto-flagava. O schema efetivo é gravado na
        run como `policy_efetiva`; os arquivos só existem se o usuário criar."""
        result = json.dumps({"files_changed": ["x.py"], "notes": "ok"})
        with mock.patch.object(sys, "stdin", io.StringIO(result)), \
             mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(io.StringIO()):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="stdin",
            ))
        # stdin é driver simulado (resultado digitado, sem custódia) → unverified → rc 2
        self.assertEqual(rc, 2)
        self.assertFalse((self.repo / ".henke" / "POLICY.json").exists(),
                         "pipeline não pode materializar arquivo protegido do metaconfig")
        self.assertFalse((self.repo / ".henke" / "PROJECT.json").exists())
        run = sorted((self.repo / ".henke" / "runs").glob("*.json"))[-1]
        pol = json.loads(run.read_text(encoding="utf-8"))["policy_efetiva"]
        self.assertIn(pol["policy"], ("strict", "balanced", "yolo"))
        self.assertEqual(pol["always_ask"], ["publish", "destructive", "metaconfig"])

    def test_missing_driver_writes_envelope_and_exits_2(self):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver=None,
            ))
        self.assertEqual(rc, 2)
        self.assertEqual(len(_envelope_files(self.repo)), 1, "envelope deve ser gravado (fail-closed)")
        self.assertIn("driver", buf_err.getvalue().lower())

    def test_unknown_driver_exits_2(self):
        with mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="bogus-driver",
            ))
        self.assertEqual(rc, 2)

    def test_stdin_driver_invalid_json_exits_2(self):
        with mock.patch.object(sys, "stdin", io.StringIO("isso não é json")), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="stdin",
            ))
        self.assertEqual(rc, 2)

    def test_stdin_driver_valid_result_flows(self):
        result = json.dumps({"files_changed": ["parser.py"], "notes": "ajuste cirúrgico feito"})
        buf = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(result)), \
             mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="stdin",
            ))
        # stdin é driver simulado → outcome teto 'unverified' → rc 2
        self.assertEqual(rc, 2, buf.getvalue())
        runs = _run_files(self.repo)
        self.assertEqual(len(runs), 1)
        run = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertEqual(run.get("files_changed"), ["parser.py"])
        self.assertEqual(run.get("outcome"), "unverified")

    def test_red_gate_maps_exit_1(self):
        buf = io.StringIO()
        with mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_red), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="echo",
            ))
        self.assertEqual(rc, 1, buf.getvalue())
        runs = _run_files(self.repo)
        run = json.loads(runs[0].read_text(encoding="utf-8"))
        self.assertEqual(run["outcome"], "failed")
        self.assertTrue(any("GATE-FAILED" in r for r in run.get("reasons", [])))


class AutoAuthorizationTests(unittest.TestCase):
    """M8: PUBLISH/destrutivo SEMPRE exige `henke approve` — nem --yes nem yolo."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")

    def _write_policy(self, name: str) -> None:
        doc = dict(henke_auto.POLICY_DEFAULT)
        doc["policy"] = name
        hd = self.repo / ".henke"
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "POLICY.json").write_text(json.dumps(doc), encoding="utf-8")

    def test_publish_always_blocked_regardless_of_yes_and_policy(self):
        variants = [
            dict(),
            dict(yes=True),
            dict(yes=True, policy="yolo"),
            dict(policy="yolo"),
            dict(yes=True, policy="strict"),
        ]
        for extra in variants:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = henke_auto.run_auto(_ns(
                    prompt=PROMPT_PUBLISH, repo=str(self.repo), driver="echo", **extra,
                ))
            self.assertEqual(rc, 2, f"publish deve bloquear sempre: {extra}")
            self.assertIn("henke approve", buf.getvalue())
        # run bloqueada: marcada requires_human_auth, SEM envelope (bloqueio antes do despacho)
        self.assertEqual(_envelope_files(self.repo), [])
        runs = _run_files(self.repo)
        self.assertEqual(len(runs), len(variants))
        last = json.loads(runs[-1].read_text(encoding="utf-8"))
        self.assertTrue(last["requires_human_auth"])
        self.assertEqual(last["review_status"], "pending")
        self.assertEqual(last["outcome"], "blocked")

    def test_destructive_prompt_blocked_too(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt="deleta o banco de dados e faz wipe em tudo",
                repo=str(self.repo), driver="echo", yes=True, policy="yolo",
            ))
        self.assertEqual(rc, 2)
        self.assertIn("henke approve", buf.getvalue())

    def test_policy_strict_asks_yes_balanced_flows(self):
        # strict + modo mutante sem --yes → confirmação (exit 2, instrução --yes)
        self._write_policy("strict")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="echo",
            ))
        self.assertEqual(rc, 2)
        self.assertIn("--yes", buf.getvalue())

        # strict + --yes → flui (stdin é simulado → unverified → rc 2; o que o
        # teste valida é NÃO bloquear na autorização)
        result = json.dumps({"files_changed": ["x.py"], "notes": "ok"})
        buf2 = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(result)), \
             mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(buf2):
            rc2 = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="stdin", yes=True,
            ))
        self.assertEqual(rc2, 2, buf2.getvalue())
        self.assertNotIn("--yes", buf2.getvalue(), "autorização passou — não deve pedir --yes")

        # balanced → flui sem --yes
        self._write_policy("balanced")
        buf3 = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(result)), \
             mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(buf3):
            rc3 = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="stdin",
            ))
        self.assertEqual(rc3, 2, buf3.getvalue())
        self.assertNotIn("--yes", buf3.getvalue(), "balanced flui sem --yes")

        # ...mas publish NUNCA flui, em nenhuma policy
        for pol in ("strict", "balanced", "yolo"):
            self._write_policy(pol)
            bufp = io.StringIO()
            with contextlib.redirect_stdout(bufp):
                rcp = henke_auto.run_auto(_ns(
                    prompt=PROMPT_PUBLISH, repo=str(self.repo), driver="echo", yes=True,
                ))
            self.assertEqual(rcp, 2, f"publish bloqueia mesmo com policy={pol} + --yes")
            self.assertIn("henke approve", bufp.getvalue())


class AutoMainTests(unittest.TestCase):
    def test_main_help_and_empty_prompt(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(henke_auto.main(["--help"]), 0)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(henke_auto.main([]), 2)

    def test_main_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = henke_auto.main([PROMPT_EXEC, "--repo", td, "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertIn("gates_required", buf.getvalue())


class AutoModeTests(unittest.TestCase):
    """Kimi-audit: ANALISAR não despacha driver; preflight bloqueia antes do driver."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")

    def test_analisar_does_not_dispatch_driver(self):
        buf = io.StringIO()
        with mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             contextlib.redirect_stdout(buf):
            rc = henke_auto.run_auto(_ns(
                prompt="audita o código do parser", repo=str(self.repo),
                driver="cmd:binario-que-nao-existe-seria-erro-se-despachado",
            ))
        self.assertEqual(rc, 0, buf.getvalue())   # se despachasse, rc seria 1
        self.assertEqual(_envelope_files(self.repo), [], "ANALISAR não gera envelope de execução")
        run = json.loads(_run_files(self.repo)[0].read_text(encoding="utf-8"))
        stages = {s["stage"]: s for s in run.get("stages", [])}
        self.assertIn("ANALISAR", stages["dispatch"]["detail"])

    def test_preflight_blocks_inviable_gates_before_driver(self):
        # EXECUTAR num repo sem spec/HEAD: portões inviáveis → bloqueio ANTES do driver.
        buf_err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_EXEC, repo=str(self.repo), driver="echo",
            ))
        self.assertEqual(rc, 2)
        self.assertIn("PRÉ-DESPACHO", buf_err.getvalue())
        run = json.loads(_run_files(self.repo)[0].read_text(encoding="utf-8"))
        self.assertEqual(run["outcome"], "blocked")  # MANDATORY-GATE-MISSING
        stages = [s["stage"] for s in run.get("stages", [])]
        self.assertNotIn("dispatch", [s for s in stages if s != "envelope"], "driver não pode ter rodado")


class AutoResumeTests(unittest.TestCase):
    """Kimi-audit: --resume exige vínculo intacto (HEAD + escopo) da aprovação."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")

    def _blocked_run(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = henke_auto.run_auto(_ns(
                prompt=PROMPT_PUBLISH, repo=str(self.repo), driver="echo",
            ))
        self.assertEqual(rc, 2)
        return _run_files(self.repo)[0]

    def test_resume_requires_new_approval_when_scope_diverges(self):
        rp = self._blocked_run()
        approve_run(rp)
        data = json.loads(rp.read_text(encoding="utf-8"))
        data["prompt"] = "pedido adulterado após aprovação"   # fingerprint diverge
        rp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = henke_auto.run_auto(_ns(
                prompt="x", repo=str(self.repo), driver="echo", resume=rp.stem,
            ))
        self.assertEqual(rc, 2)
        data2 = json.loads(rp.read_text(encoding="utf-8"))
        self.assertEqual(data2["review_status"], "pending")  # nova aprovação exigida

    def test_resume_flows_when_binding_intact(self):
        rp = self._blocked_run()
        approve_run(rp)
        buf_err = io.StringIO()
        with mock.patch.object(henke_auto, "_invoke_gates", _fake_gates_ok), \
             mock.patch.object(henke_auto, "_invoke_test_cmd", _fake_test_ok), \
             mock.patch.object(henke_auto, "_preflight_gates", _fake_preflight_ok), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
            rc = henke_auto.run_auto(_ns(
                prompt="x", repo=str(self.repo), driver="echo", resume=rp.stem,
            ))
        # Mesmo driver do vínculo → retomada flui; echo é simulado → unverified → rc 2
        self.assertEqual(rc, 2)
        self.assertNotIn("RETOMADA RECUSADA", buf_err.getvalue())

    def test_resume_refused_when_driver_changes(self):
        """Trocar o driver entre approve e resume invalida a aprovação (driver=escopo)."""
        rp = self._blocked_run()  # aprovada com driver=echo
        approve_run(rp)
        result = json.dumps({"files_changed": ["a.py"], "notes": "ok"})
        buf_err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(result)), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
            rc = henke_auto.run_auto(_ns(
                prompt="x", repo=str(self.repo), driver="stdin", resume=rp.stem,
            ))
        self.assertEqual(rc, 2)
        self.assertIn("RETOMADA RECUSADA", buf_err.getvalue())


class DriverAgentAdapterTests(unittest.TestCase):
    """Kimi-audit: falhas do adapter propagam exit != 0 (JSON de erro no stdout)."""

    def _run_adapter(self, argv, stdin_text):
        from scripts import henke_driver_agent as adapter
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.object(sys, "stdin", io.StringIO(stdin_text)), \
             contextlib.redirect_stdout(out):
            rc = adapter.main()
        return rc, out.getvalue()

    def test_unknown_preset_exits_nonzero(self):
        rc, out = self._run_adapter(["prog", "nao-existe"], "{}")
        self.assertNotEqual(rc, 0)
        self.assertIn("sem preset", json.loads(out)["notes"])

    def test_unreadable_envelope_exits_nonzero(self):
        rc, out = self._run_adapter(["prog", "claude"], "isto nao e json")
        self.assertNotEqual(rc, 0)
        self.assertIn("envelope", json.loads(out)["notes"])

    def test_missing_cli_oserror_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("subprocess.run", side_effect=OSError("WinError 2")):
                rc, out = self._run_adapter(["prog", "claude"], json.dumps({"repo_root": td}))
        self.assertNotEqual(rc, 0)
        self.assertIn("indisponível", json.loads(out)["notes"])

    def test_agent_without_result_json_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="texto sem json", stderr="")
            with mock.patch("subprocess.run", return_value=fake):
                rc, out = self._run_adapter(["prog", "claude"], json.dumps({"repo_root": td}))
        self.assertNotEqual(rc, 0)
        self.assertIn("sem JSON", json.loads(out)["notes"])

    def test_agent_result_json_exit_0(self):
        with tempfile.TemporaryDirectory() as td:
            payload = '{"files_changed": ["a.py"], "notes": "feito"}'
            fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="log...\n" + payload, stderr="")
            with mock.patch("subprocess.run", return_value=fake):
                rc, out = self._run_adapter(["prog", "claude"], json.dumps({"repo_root": td}))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["files_changed"], ["a.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
