"""gate_parallel.py — execução paralela de portões via ChildSupervisor (Wave 7, R1).

Cada portão é um processo `subprocess.Popen` supervisionado: Job Object próprio
no Windows, process group próprio no POSIX. O deadline começa quando o filho é
iniciado; um timeout encerra somente a árvore daquele portão e os demais
continuam até concluir ou expirar. Falha de um portão não cancela os demais.

Sem `ProcessPoolExecutor`, `ThreadPoolExecutor`, `multiprocessing` ou `asyncio`:
threads apenas orquestram os `wait` dos filhos criados por `Popen`.
"""
from __future__ import annotations

import os

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from henke.process_supervisor import (
    SUPERVISOR_UNAVAILABLE,
    TIMEOUT_RETURNCODE,
    ChildSupervisor,
)

_GATE_MAX_PARALLEL = 4
_GATE_MAX_OUTPUT = 1024 * 1024


@dataclass(frozen=True)
class GateSpec:
    name: str
    argv: tuple[str, ...]
    cwd: Path
    timeout_sec: float = 120.0


@dataclass(frozen=True)
class GateResult:
    name: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool
    cached: bool
    timed_out: bool
    duration_ms: int
    output_truncated: bool
    output_error: bool


def run_gates_parallel(specs: Sequence[GateSpec], *,
                       max_parallel: int = 4,
                       filter_root: Path | None = None) -> list[GateResult]:
    """Executa os portões em paralelo (teto de 4 filhos) e preserva a ordem.

    `filter_root` ancora o filtro de PATH dos filhos no repo AUDITADO
    (Delta-F8): em `--staged` o spec.cwd é o tempdir de extração e um
    filtro ancorado nele deixaria sobreviver PATH entries dentro do repo —
    netos dos portões (git -C etc.) resolveriam shims plantados."""
    if not 1 <= max_parallel <= _GATE_MAX_PARALLEL:
        raise ValueError(f"max_parallel deve estar entre 1 e {_GATE_MAX_PARALLEL}")
    if not specs:
        return []

    results: list[GateResult | None] = [None] * len(specs)
    sem = threading.Semaphore(min(max_parallel, len(specs)))
    threads: list[threading.Thread] = []

    with ChildSupervisor() as supervisor:
        def _worker(idx: int, spec: GateSpec, child) -> None:
            try:
                res = supervisor.wait(child, timeout_sec=spec.timeout_sec)
                drain_error = any(
                    d.error is not None for d in (child._drains or ())
                )
                results[idx] = GateResult(
                    name=spec.name,
                    exit_code=res.returncode,
                    stdout=res.stdout,
                    stderr=res.stderr,
                    passed=(res.returncode in (0, 3) and not res.timed_out
                            and not drain_error),
                    cached=False,
                    timed_out=res.timed_out,
                    duration_ms=res.duration_ms,
                    output_truncated=res.output_truncated,
                    output_error=drain_error,
                )
            except Exception as exc:
                results[idx] = GateResult(
                    name=spec.name,
                    exit_code=SUPERVISOR_UNAVAILABLE,
                    stdout="",
                    stderr=f"{type(exc).__name__}: {exc}",
                    passed=False,
                    cached=False,
                    timed_out=False,
                    duration_ms=0,
                    output_truncated=False,
                    output_error=True,
                )
            finally:
                sem.release()

        from henke.state_paths import sanitized_child_env
        for idx, spec in enumerate(specs):
            sem.acquire()
            try:
                child = supervisor.start(
                    spec.argv,
                    cwd=Path(spec.cwd),
                    env=sanitized_child_env(
                        filter_root if filter_root is not None
                        else Path(spec.cwd)),
                    max_output_bytes=_GATE_MAX_OUTPUT,
                )
            except (OSError, ValueError) as exc:
                # Executável ausente/argv inválido: falha DESTE portão, nunca dos demais.
                results[idx] = GateResult(
                    name=spec.name, exit_code=2, stdout="",
                    stderr=f"{type(exc).__name__}: {exc}", passed=False,
                    cached=False, timed_out=False, duration_ms=0,
                    output_truncated=False, output_error=True,
                )
                sem.release()
                continue
            t = threading.Thread(target=_worker, args=(idx, spec, child), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

    return [r for r in results]  # type: ignore[misc]


def aggregate_exit_code(results: Sequence[GateResult]) -> int:
    """Agregador: 2 se output_error/exit 2/125; 1 se timeout(124) ou não-zero;
    0 somente quando todos passam."""
    if any(r.output_error or r.exit_code in (2, SUPERVISOR_UNAVAILABLE)
           for r in results):
        return 2
    if any(not r.passed for r in results):
        return 1
    return 0
