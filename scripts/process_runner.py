"""process_runner.py — fachada compatível da Wave 6 sobre `process_supervisor`.

`run_process(...)` mantém a assinatura antiga e agora retorna o
`ProcessExecutionResult` estendido do supervisor (campos novos são aditivos:
`duration_ms`, `stdout_sha256`, `stderr_sha256`, `output_truncated`,
`termination_reason`).

A morte por PID arbitrário (`kill_process_tree`) deixou de existir como API
pública: todo encerramento passa por um handle devolvido por
`ChildSupervisor.start` na mesma execução.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import sys
from typing import Sequence

from scripts.process_supervisor import (
    SUPERVISOR_UNAVAILABLE,
    TIMEOUT_RETURNCODE,
    ChildSupervisor,
    ProcessExecutionResult,
)

__all__ = [
    "SUPERVISOR_UNAVAILABLE",
    "TIMEOUT_RETURNCODE",
    "ChildSupervisor",
    "ProcessExecutionResult",
    "run_process",
]


def run_process(
    cmd: Sequence[str] | str,
    cwd: Path | str | None = None,
    timeout_sec: float | int | None = None,
    env: dict[str, str] | None = None,
    max_output_bytes: int | None = None,
) -> ProcessExecutionResult:
    """Executa comando com captura limitada de streams e supervisão da árvore.

    Parâmetros:
        cmd: Lista de argumentos ou string de comando a executar.
        cwd: Diretório de trabalho para execução (default: None).
        timeout_sec: Tempo limite em segundos antes do encerramento da árvore
            supervisionada (Job Object no Windows, process group próprio no POSIX).
        env: Variáveis de ambiente adicionais ou sobrescritas.
        max_output_bytes: Teto de retenção por stream; o excedente entra apenas
            no hash (`output_truncated` marca o corte). `None` (padrão da fachada)
            preserva a semântica da Wave 6 e retém o stream inteiro; chamadas de
            portões devem passar um teto explícito.

    Nota de compatibilidade: em timeout, `timed_out=True` e
    `termination_reason="timeout"` são fiéis, mas `returncode` é mapeado para 1,
    a forma da Wave 6. Chamadas novas que precisam do código 124/125 usam
    `ChildSupervisor.wait` diretamente.

    Retorna:
        ProcessExecutionResult com returncode do filho (timeout → 1 na fachada;
        125 = supervisor indisponível), streams retidos, hashes e flags de término.
    """
    if isinstance(cmd, str):
        raw_tokens = shlex.split(cmd, posix=sys.platform != "win32")
        tokens = [
            tok.strip("\"'") if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok
            for tok in raw_tokens
        ]
    else:
        tokens = [str(x) for x in cmd]

    if not tokens:
        raise ValueError("cmd cannot be empty")

    # Resolução de binário e comandos internos no Windows (sem shell=True)
    if sys.platform == "win32" and tokens:
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

    work_dir = Path(cwd) if cwd is not None else Path.cwd()

    with ChildSupervisor() as supervisor:
        child = supervisor.start(tokens, cwd=work_dir, env=child_env, max_output_bytes=max_output_bytes)
        res = supervisor.wait(child, timeout_sec=float(timeout_sec) if timeout_sec is not None else None)
    if res.timed_out:
        # Semântica Wave 6: timeout reporta código não-zero legado (1), nunca 124.
        res = ProcessExecutionResult(
            returncode=1,
            stdout=res.stdout,
            stderr=res.stderr,
            timed_out=res.timed_out,
            killed_tree=res.killed_tree,
            duration_ms=res.duration_ms,
            stdout_sha256=res.stdout_sha256,
            stderr_sha256=res.stderr_sha256,
            output_truncated=res.output_truncated,
            termination_reason=res.termination_reason,
        )
    return res
