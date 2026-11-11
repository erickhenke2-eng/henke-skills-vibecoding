"""cli_exec_proc.py — Parser determinístico de test runners e supervisão de árvore de processos do `henke exec`."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess

from henke.cli_kernel import _PYTEST_FAIL_RE

_PYTEST_RE = re.compile(r"(?i)\b(\d+)\s+passed\b")
_UNITTEST_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?\s+in\s+", flags=re.I)
_UNITTEST_FAIL_RE = re.compile(r"FAILED\s*\(([^)]+)\)", flags=re.I)
# banca r4 — H10: skipped aparece em "OK (skipped=N)" / "FAILED (..., skipped=N)".
_UNITTEST_SKIP_RE = re.compile(r"\bskipped=(\d+)", flags=re.I)
# banca r4 — F2: "expected failures" (xfail que falhou = conhecido-quebrado,
# não executa asserção que passa) e "unexpected successes" (xfail que passou;
# unittest conta como FALHA e sai != 0).
_UNITTEST_XFAIL_RE = re.compile(r"expected\s+failures=(\d+)", flags=re.I)
_UNITTEST_USUCCESS_RE = re.compile(r"unexpected\s+successes=(\d+)", flags=re.I)
_JEST_VITEST_RE = re.compile(
    r"Tests:\s*(?:(\d+)\s+failed\s*[|,]\s*)?(?:(\d+)\s+skipped\s*[|,]\s*)?(\d+)\s+passed",
    flags=re.I,
)
_CARGO_TEST_RE = re.compile(
    r"test result:\s+\w+\.\s+(\d+)\s+passed;\s+(\d+)\s+failed",
    flags=re.I,
)
_GO_TEST_OK_RE = re.compile(r"(?m)^ok\s+\S+")
_GO_TEST_FAIL_RE = re.compile(r"(?m)^FAIL\s+\S+")
_CANARY_RE = re.compile(r"(\d+)/(\d+)\s+can[aá]rios\s+aprovados", flags=re.I)


def parse_test_runner_output(stdout: str, stderr: str) -> tuple[int, int]:
    """Extrai deterministicamente (testes_descobertos, testes_passados) da saída de um test runner."""
    combined = (stdout or "") + "\n" + (stderr or "")

    # 1. Python unittest ("Ran N tests in X.XXXs" — usa a última ocorrência).
    # banca r4 — H12: verificado ANTES do Jest. Um teste pode IMPRIMIR
    # "Tests: N passed" na saída; o regex do Jest, rodando primeiro sobre
    # stdout+stderr, casaria essa linha e inflaria a contagem de uma run
    # unittest. "Ran N tests in" é assinatura inequívoca de unittest.
    m_ut = list(_UNITTEST_RAN_RE.finditer(combined))
    if m_ut:
        ran = int(m_ut[-1].group(1))
        failed_count = 0
        for m_f in _UNITTEST_FAIL_RE.finditer(combined):
            # lookbehind barra "expected failures=" de contar como failures=.
            for num in re.findall(r"(?<!expected )(?:failures|errors)=(\d+)", m_f.group(1), flags=re.I):
                failed_count += int(num)
        # banca r4 — H10: "Ran N" inclui os pulados, mas skip não executa
        # asserção — suíte 100% skip é vitória por tautologia. Pulados saem
        # dos descobertos E dos passados.
        skipped = sum(int(n) for n in _UNITTEST_SKIP_RE.findall(combined))
        # banca r4 — F2: expected failures fora dos descobertos (irmão do skip);
        # unexpected successes contam como falha (unittest sai != 0 neles).
        xfail = sum(int(n) for n in _UNITTEST_XFAIL_RE.findall(combined))
        usucc = sum(int(n) for n in _UNITTEST_USUCCESS_RE.findall(combined))
        discovered = max(0, ran - skipped - xfail)
        passed = max(0, discovered - failed_count - usucc)
        return discovered, passed

    # 2. Jest / Vitest ("Tests: 1 failed, 4 passed, 5 total" ou "Tests: 5 passed")
    m_jv = _JEST_VITEST_RE.search(combined)
    if m_jv:
        failed = int(m_jv.group(1) or 0)
        passed = int(m_jv.group(3) or 0)
        return passed + failed, passed

    # 3. Cargo test ("test result: ok. 12 passed; 0 failed")
    m_cargo = list(_CARGO_TEST_RE.finditer(combined))
    if m_cargo:
        passed = sum(int(m.group(1)) for m in m_cargo)
        failed = sum(int(m.group(2)) for m in m_cargo)
        return passed + failed, passed

    # 4. Canários Henke ("5/5 canários aprovados")
    m_can = _CANARY_RE.search(combined)
    if m_can:
        passed = int(m_can.group(1))
        total = int(m_can.group(2))
        return total, passed

    # 5. Pytest ("12 passed, 1 failed")
    m_py = _PYTEST_RE.search(combined)
    if m_py:
        passed = int(m_py.group(1))
        failed = sum(int(m.group(1)) for m in _PYTEST_FAIL_RE.finditer(combined))
        return passed + failed, passed

    # 6. Go test ("ok   pkg/foo 0.012s")
    go_ok = len(_GO_TEST_OK_RE.findall(combined))
    go_fail = len(_GO_TEST_FAIL_RE.findall(combined))
    if go_ok > 0 or go_fail > 0:
        return go_ok + go_fail, go_ok

    return 0, 0


_TEST_CMD_RE = re.compile(
    r"\b(pytest|jest|vitest|mocha|cargo\s+test|go\s+test|unittest|npm\s+test|bun\s+test|pnpm\s+test|yarn\s+test)\b",
    re.IGNORECASE,
)

_NON_TEST_CMDS = {
    "echo", "print", "printf", "cat", "type", "git", "dir", "ls", "find", "grep",
    "head", "tail", "curl", "wget", "touch", "rm", "del", "mkdir", "cd", "cp",
    "mv", "copy", "move", "tar", "zip", "unzip", "sed", "awk",
}

_AL_MOD_CACHE = None


def _audit_ledger_mod():
    """audit_ledger por path explícito (fonte única de is_test_cmd — A13 v16)."""
    global _AL_MOD_CACHE
    if _AL_MOD_CACHE is not None:
        return _AL_MOD_CACHE
    try:
        import importlib.util as _ilu
        _p = Path(__file__).resolve().with_name("audit_ledger.py")
        _sp = _ilu.spec_from_file_location("henke_audit_ledger2", str(_p))
        _m = _ilu.module_from_spec(_sp) if _sp else None
        if _m is not None:
            _sp.loader.exec_module(_m)  # type: ignore[union-attr]
        _AL_MOD_CACHE = _m
    except Exception:
        _AL_MOD_CACHE = None
    return _AL_MOD_CACHE


def is_test_command(cmd_tokens: list[str], cmd_str: str) -> bool:
    """P1-02: classificador ÚNICO — audit_ledger.is_test_cmd via path
    explícito. O fallback local (~75 linhas de heurística própria) era a
    segunda fonte que o audit flagou: classificadores divergentes mintavam
    num caminho e não no outro. _al indisponível → False (nunca minta,
    fail-closed; o caller zera contagens de qualquer forma).
    `cmd_tokens` permanece na assinatura p/ compat dos call-sites."""
    del cmd_tokens  # classificação é por cmd_str — tokens eram do fallback
    _al = _audit_ledger_mod()
    if _al is not None and hasattr(_al, "is_test_cmd"):
        try:
            return bool(_al.is_test_cmd(cmd_str))
        except Exception:
            return False
    return False


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Derruba `proc` e todos os descendentes — subprocess.run(timeout)
    mata só o filho direto e abandona netos vivos (DS-12)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        # start_new_session: o filho é líder do próprio grupo — killpg só
        # atinge a árvore dele, nunca o nosso (DS-15 do relatório wave6).
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def _run_capture_tree(argv: list[str], *, cwd: Path, env: dict,
                      timeout_sec: float) -> tuple[int, bytes, bytes, bool]:
    """Popen com kill de árvore no timeout. Retorna (rc, stdout, stderr,
    timed_out) — o chamador registra o timeout no ledger em vez de sair
    sem rastro (DS-12)."""
    if os.name == "nt":
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    else:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        # banca-v18 (rev B-F3): communicate() pós-kill sem timeout travava o
        # exec para sempre quando neto órfão (setsid escapa do killpg)
        # segurava o pipe — henke_gates já tinha o mesmo fix com timeout=10.
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
    return proc.returncode, out or b"", err or b"", timed_out


def _resolve_exec_timeout(repo_root: Path, cli_timeout: float | None) -> float:
    """Resolve o teto de timeout do `henke exec`.

    Ordem de precedência honesta:
      1. flag `--timeout N` (quando > 0);
      2. campo `timeout_sec` de `.henke/PROJECT.json` (quando numérico e > 0);
      3. fallback fixo de 180s.
    """
    # B1 (banca v13): --timeout nan/inf passava nas checagens e o subprocesso
    # nunca expirava — finito+positivo em ambas as fontes.
    # A13 (banca v14): timeout controlado pelo repo podia ser 1e9 — o stage
    # pendurava ~31 anos. Teto honesto de 1h.
    _CAP = 3600.0
    if (cli_timeout is not None and math.isfinite(cli_timeout)
            and cli_timeout > 0):
        return min(float(cli_timeout), _CAP)
    project_json = repo_root / ".henke" / "PROJECT.json"
    try:
        pdata = json.loads(project_json.read_text(encoding="utf-8"))
        val = pdata.get("timeout_sec") if isinstance(pdata, dict) else None
        if (isinstance(val, (int, float)) and not isinstance(val, bool)
                and math.isfinite(val) and val > 0):
            return min(float(val), _CAP)
    except Exception:
        pass  # PROJECT.json ausente/inválido -> fallback silencioso para 180s
    return 180.0
