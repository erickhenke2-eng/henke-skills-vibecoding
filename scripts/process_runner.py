"""Módulo de execução atômica de processos e eliminação de zumbis no Windows (Wave 6 - R3).

Garante que timeouts de subprocessos eliminem recursivamente toda a árvore de processos
descendentes via `taskkill /F /T` no Windows, prevenindo processos órfãos, travamento de
arquivos e vazamento de portas.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import NamedTuple, Sequence


class ProcessExecutionResult(NamedTuple):
    """Resultado imutável da execução de um subprocesso."""
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    killed_tree: bool


def kill_process_tree(pid: int, timeout_sec: float = 10.0) -> bool:
    """Termina atomicamente uma árvore de processos no Windows via taskkill /F /T.

    Retorna True se taskkill finalizou a árvore com sucesso (código 0) ou se o
    processo já havia sido encerrado (código 128 / corrida benigna).
    Retorna False em caso de PID inválido (<= 0) ou falha irrecuperável.
    """
    if pid <= 0:
        return False

    if sys.platform.startswith("win"):
        try:
            res = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
            )
            # Código 0: árvore finalizada com sucesso
            # Código 128: processo já encerrado no momento do disparo (race benigna)
            return res.returncode in (0, 128)
        except (subprocess.TimeoutExpired, OSError):
            return False
    else:
        # Fallback defensivo para ambientes POSIX (testes / CI híbrido)
        try:
            import signal
            sig = getattr(signal, "SIGKILL", getattr(signal, "SIGTERM", 9))
            if hasattr(os, "getpgid") and hasattr(os, "killpg"):
                pgid = os.getpgid(pid)
                os.killpg(pgid, sig)
                return True
            elif hasattr(os, "kill"):
                os.kill(pid, sig)
                return True
            return False
        except Exception:
            return False


def run_process(
    cmd: Sequence[str] | str,
    cwd: Path | str | None = None,
    timeout_sec: float | int | None = None,
    env: dict[str, str] | None = None,
) -> ProcessExecutionResult:
    """Executa comando com captura limpa de streams e eliminação atômica da árvore no timeout.

    Parâmetros:
        cmd: Lista de argumentos ou string de comando a executar.
        cwd: Diretório de trabalho para execução (default: None).
        timeout_sec: Tempo limite em segundos antes do encerramento forçado da árvore.
        env: Dicionário de variáveis de ambiente adicionais ou sobrescritas.

    Retorna:
        ProcessExecutionResult com returncode, stdout, stderr, timed_out e killed_tree.
    """
    if isinstance(cmd, str):
        raw_tokens = shlex.split(cmd, posix=not sys.platform.startswith("win"))
        tokens = [
            tok.strip("\"'") if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok
            for tok in raw_tokens
        ]
    else:
        tokens = [str(x) for x in cmd]

    if not tokens:
        raise ValueError("cmd cannot be empty")

    # Resolução de binário e comandos internos no Windows (sem shell=True)
    if sys.platform.startswith("win") and tokens:
        resolved = shutil.which(tokens[0])
        if resolved:
            tokens[0] = resolved
        elif tokens[0].lower() in ("echo", "dir", "type", "del", "copy", "move", "md", "rd", "cls"):
            tokens = ["cmd.exe", "/c"] + tokens

    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)
    child_env.setdefault("PYTHONUTF8", "1")
    child_env.setdefault("PYTHONIOENCODING", "utf-8")

    work_dir = str(cwd) if cwd is not None else None

    proc = subprocess.Popen(
        tokens,
        cwd=work_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return ProcessExecutionResult(
            returncode=proc.returncode if proc.returncode is not None else 0,
            stdout=stdout or "",
            stderr=stderr or "",
            timed_out=False,
            killed_tree=False,
        )
    except subprocess.TimeoutExpired:
        # 1. Eliminação atômica da árvore de processos descendentes via taskkill
        killed = kill_process_tree(proc.pid)

        # 2. Defesa em profundidade: encerra o handle principal do processo
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass

        # 3. Drena buffers acumulados antes do timeout
        try:
            rem_stdout, rem_stderr = proc.communicate(timeout=5.0)
        except Exception:
            rem_stdout, rem_stderr = "", ""

        ret = proc.returncode if proc.returncode not in (None, 0) else 1
        return ProcessExecutionResult(
            returncode=ret,
            stdout=rem_stdout or "",
            stderr=rem_stderr or "",
            timed_out=True,
            killed_tree=killed,
        )
