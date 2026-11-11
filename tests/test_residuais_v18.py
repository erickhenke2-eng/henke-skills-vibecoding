#!/usr/bin/env python3
"""Regressões dos 6 residuais da banca-v18 (branch fix/residuais-v18).

Cada classe cobre UM residual com prova física reproduzível:
  1. assertless_scan — dobra de match-case estrutural provadamente morto
     (sequência/mapping/class/singleton/or/guard-falso) sem falsos positivos.
  2. assertless_scan — `with <var>.assertRaises` só conta com binding
     TestCase-like; bare `X.assertRaises(E)` descartado é NO-ASSERTION.
  3. hazard_scan — functools.partial propaga para sinks de desserialização
     e código dinâmico (pickle/marshal/yaml/eval/exec), atribuído e inline.
  4. henke_driver_agent — argv com `%VAR%` em token citado é recusado antes
     de chegar ao re-parse do `cmd /c` (expansão em aspas é irreprimível).
  5. coverage_ledger — critic_passes teatral (placeholder/sem substância)
     reprova; entradas reais (agent+round+scope/findings) passam.
  6. event_store — migrate() inteiro sob o lock do append: N migrates
     concorrentes importam exatamente uma vez, sem duplicar nem perder.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "henke"))

from henke.metricas.assertless_scan import scan_test_file  # noqa: E402
from henke.metricas.hazard_scan import scan_file as hazard_scan_file  # noqa: E402
from henke.metricas.coverage_ledger import (  # noqa: E402
    verify_ledger, _file_sha256_and_lines)
import henke.event_store as es  # noqa: E402
import henke.henke_driver_agent as drv  # noqa: E402


def _scan_source(src: str):
    """Escreve `src` num test_*.py temporário e roda scan_test_file."""
    with tempfile.TemporaryDirectory(prefix="v18_scan_") as td:
        f = Path(td) / "test_fixture.py"
        f.write_text(src, encoding="utf-8")
        return scan_test_file(f, set())


def _findings_for(src: str):
    out = _scan_source(src)
    assert out is not None, "fixture não parseou"
    return out


# ---------------------------------------------------------------- residual 1

_DEAD_MATCH_SRC = '''\
RESULT = object()


def test_seq_pattern_vs_str_subject():
    x = "ab"
    match x:
        case [a, b]:
            assert a == RESULT  # morto: str nao e sequencia p/ match
        case _:
            pass


def test_class_pattern_wrong_builtin():
    match 5:
        case str(s):
            assert s == RESULT  # morto: isinstance(5, str) e False
        case _:
            pass


def test_guard_folds_false():
    match (1, 2):
        case y if 1 == 2:
            assert y == RESULT  # morto: guarda dobra pra falso
        case _:
            pass


def test_seq_len_mismatch():
    match (1, 2, 3):
        case [a, b]:
            assert a == RESULT  # morto: 2 padroes, 3 elementos
        case _:
            pass


def test_mapping_missing_key():
    match {"a": 1}:
        case {"b": v}:
            assert v == RESULT  # morto: chave ausente
        case _:
            pass


def test_seq_pattern_vs_int_subject():
    match 5:
        case [a, b]:
            assert a == RESULT  # morto: int nao e sequencia
        case _:
            pass


def test_mapping_vs_tuple_subject():
    match (1, 2):
        case {"a": v}:
            assert v == RESULT  # morto: tupla nao e mapping
        case _:
            pass


def test_singleton_dead():
    match 1:
        case True:
            assert RESULT == 1  # morto: case True usa `is`, 1 is not True
        case _:
            pass


def test_or_pattern_all_dead():
    match 3:
        case 4 | 5:
            assert RESULT == 3  # morto: const != const em todos os bracos
        case _:
            pass
'''

_LIVE_MATCH_SRC = '''\
def _unknown():
    return 1


def test_tuple_subject_seq_pattern_LIVE():
    # match (1,2): case [a,b] -> tupla E sequencia -> CASA em runtime
    match (1, 2):
        case [a, b]:
            assert a == 1
        case _:
            pass


def test_seq_star_LIVE():
    match (1, 2):
        case [a, *rest]:
            assert a == 1
        case _:
            pass


def test_guard_dynamic_LIVE():
    g = _unknown()
    match 5:
        case y if g:
            assert y == 5
        case _:
            pass


def test_class_pattern_matching_LIVE():
    match "x":
        case str(s):
            assert s == "x"
        case _:
            pass


def test_bool_is_int_LIVE():
    # isinstance(True, int) e True -> case int() casa -> vivo
    match True:
        case int(i):
            assert i == 1
        case _:
            pass


def test_unknown_subject_LIVE():
    x = _unknown()
    match x:
        case [a, b]:
            assert a == b
        case _:
            pass


def test_or_pattern_one_live():
    match 3:
        case 3 | 5:
            assert len([1]) == 1
        case _:
            pass


def test_mapping_present_key_LIVE():
    match {"a": 1}:
        case {"a": v}:
            assert v == 1
        case _:
            pass
'''


class TestResidual1DeadMatchFolding(unittest.TestCase):
    """match-case estrutural provadamente impossível dobra; controles vivos
    NÃO podem virar falso-positivo (na dúvida, não marca morto)."""

    def test_dead_structural_arms_fold(self):
        scanned, findings = _findings_for(_DEAD_MATCH_SRC)
        self.assertEqual(scanned, 9)
        self.assertEqual(len(findings), 9,
                         f"braços mortos ainda contaram como asserção: {findings}")
        self.assertTrue(all(v == "NO-ASSERTION" for v, *_ in findings))

    def test_live_controls_never_flagged(self):
        scanned, findings = _findings_for(_LIVE_MATCH_SRC)
        self.assertEqual(scanned, 8)
        self.assertEqual(findings, [],
                         f"falso-positivo em braço vivo: {findings}")


# ---------------------------------------------------------------- residual 2

_ASSERTCTX_SRC = '''\
import unittest


class MyTest(unittest.TestCase):
    pass


class _F:
    def assertRaises(self, e):
        import contextlib
        return contextlib.nullcontext()


def object_factory():
    return _F()


def test_with_bound_testcase_assertRaises():
    case = MyTest()
    with case.assertRaises(ValueError):
        raise ValueError("x")


def test_bare_assertRaises_no_callable():
    self = MyTest()
    self.assertRaises(ValueError)  # CM descartado: nao executa nada


def test_bare_assertRaises_with_callable_LIVE():
    self = MyTest()
    self.assertRaises(ValueError, int, "notanint")


class TestInside(unittest.TestCase):
    def test_bare_self_assertRaises(self):
        self.assertRaises(ValueError)  # bare-statement: descartado

    def test_with_self_assertRaises_LIVE(self):
        with self.assertRaises(ValueError):
            raise ValueError("x")


def test_with_testcase_named_var_LIVE():
    tc = MyTest()
    with tc.assertRaises(KeyError):
        raise KeyError("k")


def test_with_non_testcase_name_NOT_counted():
    obj = object_factory()
    with obj.assertRaises(ValueError):
        raise ValueError("x")
'''


class TestResidual2AssertContextManagers(unittest.TestCase):
    """`with <TestCase>.assertRaises` conta; bare CM-statement descartado e
    `with` em nome não-TestCase não contam como asserção."""

    def test_context_manager_classification(self):
        scanned, findings = _findings_for(_ASSERTCTX_SRC)
        self.assertEqual(scanned, 7)
        flagged = {fn.name for _v, _f, fn, _w in findings}
        self.assertEqual(
            flagged,
            {"test_bare_assertRaises_no_callable",
             "test_bare_self_assertRaises",
             "test_with_non_testcase_name_NOT_counted"},
            f"flagged={flagged} findings={findings}")
        self.assertTrue(all(v == "NO-ASSERTION" for v, *_ in findings))


# ---------------------------------------------------------------- residual 3

_PARTIAL_SRC = '''\
from functools import partial
import pickle
import marshal
import yaml

ploads = partial(pickle.loads)
mloads = partial(marshal.loads)
yload = partial(yaml.load)
peval = partial(eval)
pexec = partial(exec)

def go(blob, src):
    ploads(blob)
    mloads(blob)
    yload(src)
    peval(src)
    pexec(src)
    partial(pickle.loads, blob)()
'''

_PARTIAL_SAFE_SRC = '''\
from functools import partial
import yaml

# Loader explicitamente seguro continua isento.
safe_load = partial(yaml.load, Loader=yaml.SafeLoader)

def go(src):
    safe_load(src)
'''


class TestResidual3PartialSinkPropagation(unittest.TestCase):
    """functools.partial de sink de desserialização/código dinâmico propaga
    o hazard — atribuído e inline — e Loader seguro segue isento."""

    def _hazards(self, src: str):
        with tempfile.TemporaryDirectory(prefix="v18_haz_") as td:
            f = Path(td) / "fixture_partial.py"
            f.write_text(src, encoding="utf-8")
            scanned, findings, _skipped = hazard_scan_file(f)
        self.assertEqual(scanned, 1)
        return findings

    def test_partial_of_deser_and_exec_sinks_flagged(self):
        findings = self._hazards(_PARTIAL_SRC)
        self.assertGreaterEqual(len(findings), 6,
                                f"partial-sink nao propagou: {findings}")

    def test_partial_with_safe_loader_exempt(self):
        findings = self._hazards(_PARTIAL_SAFE_SRC)
        self.assertEqual(findings, [],
                         f"Loader seguro foi flagado: {findings}")


# ---------------------------------------------------------------- residual 4

@unittest.skipUnless(os.name == "nt", "wrap cmd /c so existe no Windows")
class TestResidual4CmdExpansionRefusal(unittest.TestCase):
    """Token citado com `%VAR%` nunca chega ao re-parse do `cmd /c` —
    expansão dentro de aspas não tem escape confiável (fail-closed rc=2)."""

    def _run_driver(self, instr: str):
        with tempfile.TemporaryDirectory(prefix="v18_drv_") as td:
            root = Path(td)
            repo = root / "repo"
            repo.mkdir()
            bindir = root / "bin"
            bindir.mkdir()
            marker = root / "marker.txt"
            shim = bindir / "claude.cmd"
            shim.write_text(
                f'@echo off\r\n> "{marker}" (echo ran)\r\n'
                'echo {"files_changed": [], "notes": "shim ran"}\r\n'
                "exit /b 0\r\n",
                encoding="utf-8")
            env_payload = json.dumps({"repo_root": str(repo), "prompt": "x"})
            old_path = os.environ.get("PATH", "")
            old_instr = drv._INSTR
            os.environ["PATH"] = str(bindir) + os.pathsep + old_path
            drv._INSTR = instr
            try:
                buf = io.StringIO()
                with patch("sys.stdin", io.StringIO(env_payload)), \
                     patch("sys.argv", ["henke_driver_agent.py", "claude"]), \
                     patch("sys.stdout", buf):
                    rc = drv.main()
            finally:
                os.environ["PATH"] = old_path
                drv._INSTR = old_instr
            # marker lido ANTES do TemporaryDirectory fechar — Path.exists()
            # depois do cleanup seria sempre False (falso-vermelho).
            return rc, buf.getvalue(), marker.exists()

    def test_percent_in_quoted_token_refused(self):
        rc, out, ran = self._run_driver(
            "executa tarefa %PWNED_V18_TEST% agora no arquivo {env_name}")
        self.assertEqual(rc, 2, f"argv com % nao foi recusado: {out}")
        self.assertFalse(ran, "shim executou apesar do token % recusado")
        self.assertIn("expande", out)

    def test_delayed_expansion_token_refused(self):
        rc, out, ran = self._run_driver(
            "executa tarefa !PWNED_V18_TEST! agora no arquivo {env_name}")
        # `!` dentro de token citado: sem % mas com delayed-expansion — o
        # wrap usa /v:off (neutraliza !X!) E token `!` sozinho nao expande,
        # mas o driver NAO pode depender do registry: aceita rc 0 (shim
        # rodou sob /v:off, expansao estruturalmente desligada) ou rc 2.
        self.assertIn(rc, (0, 2))
        if rc == 0:
            self.assertTrue(ran, "rc 0 mas shim nao rodou — falso verde")

    def test_legit_prompt_still_runs(self):
        rc, out, ran = self._run_driver(
            "executa a tarefa do arquivo {env_name} sem simbolos estranhos")
        self.assertEqual(rc, 0, f"prompt legitimo falhou: {out}")
        self.assertTrue(ran, "shim nao executou no controle")


# ---------------------------------------------------------------- residual 5


def _make_ledger(root: Path, critic_passes) -> Path:
    """Ledger mínimo-verificado: 1 unidade coberta + critic_passes dado."""
    src = root / "a.py"
    src.write_text("x = 1\n", encoding="utf-8")
    sha, lines = _file_sha256_and_lines(src)
    data = {
        "schema_version": "1.0",
        "metadata": {"root": str(root), "profile": "deep",
                     "tree_hash": "0" * 64, "total_units": 1,
                     "run_status": "in_progress"},
        "critic_passes": critic_passes,
        "findings": [],
        "units": [{
            "unit_id": "unit:a.py", "path": "a.py", "sha256": sha,
            "lines": lines, "priority": "low", "risk_score": 1,
            "risk_signals": {"hazard_hits": 0, "fan_in": 0,
                             "max_complexity": 1, "entry_surface": False},
            "status": "covered", "reason": "",
            "checks": [{"agent_id": "aud1", "invariant": "sem eval",
                        "result": "pass", "method": "source",
                        "assumptions": [{"text": "sem input externo",
                                         "status": "enforced"}]}],
            "attempts": [], "prior_status": None,
        }],
    }
    lf = root / "ledger.json"
    lf.write_text(json.dumps(data), encoding="utf-8")
    return lf


class TestResidual5CriticSubstance(unittest.TestCase):
    """critic_passes teatral (placeholder/ausência de substância) reprova;
    entrada com agent/auditor + round + scope/findings reais passa."""

    def _verify(self, critic_passes):
        with tempfile.TemporaryDirectory(prefix="v18_led_") as td:
            root = Path(td)
            lf = _make_ledger(root, critic_passes)
            return verify_ledger(lf, root_override=root,
                                 require_critic=True)

    def test_theatrical_entries_rejected(self):
        bad = [
            [{"agent": "a"}],            # sem round/scope
            [""],                        # entrada vazia
            [{"note": "ok"}],            # sem agent/round
            [{"agent": "a", "round": "r1"}],          # sem substância nenhuma
            [{"agent": "a", "round": "ok", "scope": "x"}],  # tudo placeholder
            [{"agent": "ok", "round": 1, "scope": "full"}], # agente placeholder
            [{"agent": "a", "round": 1, "scope": "ok"}],    # scope placeholder
        ]
        for cp in bad:
            with self.subTest(critic_passes=cp):
                ok, errors, _s = self._verify(cp)
                self.assertFalse(ok, f"teatral passou: {cp}")
                self.assertTrue(any("CRITIC" in e for e in errors),
                                f"sem erro de critic p/ {cp}: {errors}")

    def test_real_entries_accepted(self):
        good = [
            [{"agent": "banca-x", "round": "cross-exam",
              "scope": "banca adversarial"}],
            [{"auditor": "banca-1", "round": 2,
              "findings": ["f1", "f2"]}],
            [{"agent": "a", "round": 1, "scope": "full audit"}],
            [{"agent": "a", "round": "r2",
              "rationale": "revisão adversarial completa do diff inteiro"}],
        ]
        for cp in good:
            with self.subTest(critic_passes=cp):
                ok, errors, _s = self._verify(cp)
                self.assertTrue(ok, f"entrada real reprovada: {cp} → {errors}")


# ---------------------------------------------------------------- residual 6


class TestResidual6MigrateUnderLock(unittest.TestCase):
    """migrate() inteiro sob o lock do append — N migrates concorrentes
    importam exatamente uma vez, sem TimeoutError nem eventos perdidos."""

    def test_concurrent_migrate_exactly_once(self):
        with tempfile.TemporaryDirectory(prefix="v18_mig_") as td:
            repo = Path(td)
            (repo / ".henke" / "cache" / "scans").mkdir(parents=True)
            (repo / ".henke" / "exec_log.jsonl").write_text(
                "".join(json.dumps({"cmd": f"pytest {i}", "exit": 0,
                                    "testes_descobertos": 1,
                                    "entry_hash": f"h{i}"}) + "\n"
                        for i in range(30)), encoding="utf-8")

            results, errors = [], []
            def worker(tag):
                try:
                    results.append(es.migrate(repo))
                except Exception as e:  # noqa: BLE001 — reporta no assert
                    errors.append((tag, e))

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)

            self.assertFalse(any(t.is_alive() for t in threads),
                             "thread de migrate ficou presa no lock")
            self.assertEqual(errors, [], f"migrate concorrente falhou: {errors}")
            self.assertEqual(len(results), 4)
            self.assertEqual(sorted(r["exec"] for r in results), [0, 0, 0, 30],
                             "mais de um migrate importou — check 'already' "
                             "não rodou sob o mesmo lock dos appends")
            evs = list(es._iter_events(es.events_path(repo)))
            self.assertEqual(
                len([e for e in evs if e.get("kind") == "exec"]), 30)
            self.assertEqual(
                len([e for e in evs if e.get("kind") == "migrate_marker"]), 1)
            self.assertEqual(es.verify(repo), [])

    def test_migrate_idempotent_after_lock(self):
        with tempfile.TemporaryDirectory(prefix="v18_mig2_") as td:
            repo = Path(td)
            (repo / ".henke").mkdir(parents=True)
            (repo / ".henke" / "exec_log.jsonl").write_text(
                json.dumps({"cmd": "pytest", "exit": 0,
                            "testes_descobertos": 1, "entry_hash": "h0"}) + "\n",
                encoding="utf-8")
            s1 = es.migrate(repo)
            s2 = es.migrate(repo)
            self.assertEqual(s1["exec"], 1)
            self.assertTrue(s2["skipped"])
            self.assertEqual(s2["exec"], 0)
            self.assertEqual(es.verify(repo), [])


if __name__ == "__main__":
    unittest.main()
