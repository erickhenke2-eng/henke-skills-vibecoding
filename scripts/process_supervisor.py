"""process_supervisor.py — supervisão de filhos por propriedade (Wave 7, R1/R7).

Invariante de propriedade: só se encerra um processo por um handle devolvido por
`ChildSupervisor.start` na mesma execução. Sentinel, PID serializado e nome de
executável não são autoridade de morte.

Windows: um Job Object por filho com JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. O filho é
criado suspenso, associado ao Job e só então sua thread principal é liberada.
Se a associação falhar, o processo ainda suspenso é encerrado pelo próprio handle e
o resultado é SUPERVISOR_UNAVAILABLE — nenhum filho executa fora de supervisão.

POSIX: `start_new_session=True`; o PGID esperado é exatamente o PID do filho
criado. Timeout sinaliza somente esse grupo com SIGTERM e, após graça curta,
SIGKILL. Nunca se usa o grupo do processo pai.

Streams: threads drenam stdout/stderr até EOF, retendo apenas os primeiros
`max_output_bytes`; o restante entra no hash e é descartado (`output_truncated`).
Nenhum executor, `shell=True`, `taskkill` ou enumeração global de processos é usado.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

_IS_WINDOWS = sys.platform == "win32"

TIMEOUT_RETURNCODE = 124
SUPERVISOR_UNAVAILABLE = 125
_GRACE_SEC = 0.5


class SupervisorUnavailableError(RuntimeError):
    """Supervisão nativa indisponível; o filho nunca executa sem ela."""


if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", ctypes.c_long),
            ("tpDeltaPri", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
        ]

    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _k32.Thread32First.restype = wintypes.BOOL
    _k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _k32.Thread32Next.restype = wintypes.BOOL
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateProcess.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


@dataclass(frozen=True)
class ProcessExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    killed_tree: bool
    duration_ms: int
    stdout_sha256: str
    stderr_sha256: str
    output_truncated: bool
    termination_reason: str


def _win_create_job() -> int:
    """Cria Job Object com KILL_ON_JOB_CLOSE. Retorna handle ou 0 em falha."""
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        return 0
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _k32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        _k32.CloseHandle(job)
        return 0
    return int(job)


def _win_resume_process_threads(pid: int) -> bool:
    """Libera as threads do processo recém-criado via Toolhelp. Não toca em outros PIDs."""
    snap = _k32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snap or snap == _INVALID_HANDLE:
        return False
    resumed_any = False
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        has = _k32.Thread32First(snap, ctypes.byref(entry))
        while has:
            if entry.th32OwnerProcessID == pid:
                th = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if th:
                    try:
                        while True:
                            prev = _k32.ResumeThread(th)
                            if prev == 0xFFFFFFFF or prev <= 1:
                                if prev != 0xFFFFFFFF:
                                    resumed_any = True
                                break
                    finally:
                        _k32.CloseHandle(th)
            has = _k32.Thread32Next(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return resumed_any


class _StreamDrain(threading.Thread):
    """Drena um stream até EOF retendo só o teto; hash cobre o fluxo inteiro."""

    def __init__(self, stream, max_output_bytes: int):
        super().__init__(daemon=True)
        self._stream = stream
        self._max = max_output_bytes
        self.hasher = hashlib.sha256()
        self.retained = bytearray()
        self.truncated = False
        self.error: str | None = None

    def run(self) -> None:
        try:
            self._drain()
        except Exception as exc:  # nunca mata a thread em silêncio
            self.error = f"{type(exc).__name__}: {exc}"

    def _drain(self) -> None:
        while True:
            try:
                chunk = self._stream.read(65536)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            self.hasher.update(chunk)
            if self._max is None:
                self.retained += chunk
                continue
            room = self._max - len(self.retained)
            if room > 0:
                self.retained += chunk[:room]
            if len(chunk) > room:
                self.truncated = True

    def finish(self) -> tuple[str, str, bool]:
        data = bytes(self.retained).decode("utf-8", errors="replace")
        return data, self.hasher.hexdigest(), self.truncated


_EMPTY_DRAIN_RESULT = ("", hashlib.sha256(b"").hexdigest(), False)


class ChildHandle:
    """Handle opaco de um filho criado por `ChildSupervisor.start`.

    Pertence a uma única instância de supervisor; usar em outro supervisor, após
    fechamento ou duas vezes em `wait` gera erro explícito.
    """

    __slots__ = ("_owner", "_id", "_proc", "_job", "_pgid", "_drains", "_done",
                 "_terminated", "_term_reason")

    def __init__(self, owner: "ChildSupervisor", handle_id: int,
                 proc: subprocess.Popen | None,
                 job: int | None, pgid: int | None,
                 drains: tuple[_StreamDrain, _StreamDrain] | None):
        self._owner = owner
        self._id = handle_id
        self._proc = proc
        self._job = job
        self._pgid = pgid
        self._drains = drains
        self._done = False
        self._terminated = False
        self._term_reason = ""

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None


class ChildSupervisor:
    """Cria e supervisiona filhos; cada handle registrado pertence a esta instância."""

    def __init__(self) -> None:
        self._children: dict[int, ChildHandle] = {}
        self._next_id = 0
        self._closed = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ start
    def start(self, argv: Sequence[str], *, cwd: Path,
              env: Mapping[str, str] | None = None,
              max_output_bytes: int | None = 1024 * 1024) -> ChildHandle:
        if self._closed:
            raise RuntimeError("ChildSupervisor fechado")
        tokens = [str(a) for a in argv]
        if not tokens:
            raise ValueError("argv não pode ser vazio")
        work_dir = str(Path(cwd)) if cwd is not None else None
        child_env = dict(env) if env is not None else None

        try:
            if _IS_WINDOWS:
                handle = self._start_windows(tokens, work_dir, child_env, max_output_bytes)
            else:
                handle = self._start_posix(tokens, work_dir, child_env, max_output_bytes)
        except SupervisorUnavailableError:
            # Filho nunca executou: handle morto cujo wait() reporta 125.
            handle = ChildHandle(self, 0, None, None, None, None)
            handle._terminated = True
            handle._term_reason = "supervisor_unavailable"
        with self._lock:
            self._next_id += 1
            handle._id = self._next_id
            self._children[handle._id] = handle
        return handle

    def _open_pipes(self, proc: subprocess.Popen, max_output_bytes: int) -> tuple[_StreamDrain, _StreamDrain]:
        drains = (
            _StreamDrain(proc.stdout, max_output_bytes),
            _StreamDrain(proc.stderr, max_output_bytes),
        )
        drains[0].start()
        drains[1].start()
        return drains

    def _start_windows(self, tokens, work_dir, child_env, max_output_bytes) -> ChildHandle:
        job = _win_create_job()
        try:
            proc = subprocess.Popen(
                tokens,
                cwd=work_dir,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                creationflags=_CREATE_SUSPENDED,
            )
        except BaseException:
            # Executável ausente/Popen falho: o Job criado acima não pode vazar.
            if job:
                _k32.CloseHandle(job)
            raise
        try:
            if not job:
                raise SupervisorUnavailableError("CreateJobObject falhou")
            if not _k32.AssignProcessToJobObject(job, proc._handle):
                raise SupervisorUnavailableError("AssignProcessToJobObject falhou")
            if not _win_resume_process_threads(proc.pid):
                raise SupervisorUnavailableError("ResumeThread indisponível para o filho suspenso")
        except BaseException:
            if proc.poll() is None:
                try:
                    _k32.TerminateProcess(proc._handle, 1)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            if job:
                _k32.CloseHandle(job)
            raise
        drains = self._open_pipes(proc, max_output_bytes)
        return ChildHandle(self, 0, proc, job, None, drains)

    def _start_posix(self, tokens, work_dir, child_env, max_output_bytes) -> ChildHandle:
        proc = subprocess.Popen(
            tokens,
            cwd=work_dir,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        drains = self._open_pipes(proc, max_output_bytes)
        return ChildHandle(self, 0, proc, None, pgid, drains)

    # -------------------------------------------------------------- terminate
    def _check_handle(self, child: ChildHandle, *, for_wait: bool = False) -> None:
        if not isinstance(child, ChildHandle) or child._owner is not self:
            raise ValueError("handle não pertence a este supervisor")
        if child._done:
            raise RuntimeError("handle já encerrado" + ("; wait() já foi chamado" if for_wait else ""))

    def terminate(self, child: ChildHandle, *, reason: str) -> None:
        self._check_handle(child)
        child._terminated = True
        child._term_reason = reason
        proc = child._proc
        if proc is None or proc.poll() is not None:
            self._release_handle(child)
            return
        if _IS_WINDOWS:
            if child._job:
                _k32.CloseHandle(child._job)  # KILL_ON_JOB_CLOSE encerra a árvore
                child._job = None
            else:
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass
        else:
            if child._pgid is not None:
                try:
                    os.killpg(child._pgid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    proc.wait(timeout=_GRACE_SEC)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child._pgid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            else:
                try:
                    proc.kill()
                except (OSError, ProcessLookupError):
                    pass

    # -------------------------------------------------------------------- wait
    def wait(self, child: ChildHandle, *, timeout_sec: float | None = None) -> ProcessExecutionResult:
        self._check_handle(child, for_wait=True)
        if child._proc is None:
            # Filho nunca executou (supervisão indisponível antes do resume).
            self._release_handle(child)
            return ProcessExecutionResult(
                returncode=SUPERVISOR_UNAVAILABLE,
                stdout="",
                stderr="",
                timed_out=False,
                killed_tree=False,
                duration_ms=0,
                stdout_sha256=_EMPTY_DRAIN_RESULT[1],
                stderr_sha256=_EMPTY_DRAIN_RESULT[1],
                output_truncated=False,
                termination_reason=child._term_reason or "supervisor_unavailable",
            )

        started = time.monotonic()
        timed_out = False
        killed_tree = False
        try:
            if timeout_sec is None:
                child._proc.wait()
            else:
                child._proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.terminate(child, reason="timeout")
            killed_tree = True
            try:
                child._proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                child._proc.kill()
                child._proc.wait()
        duration_ms = int((time.monotonic() - started) * 1000)
        if child._drains is not None:
            for d in child._drains:
                d.join(timeout=10.0)
            out_text, out_hash, out_trunc = child._drains[0].finish()
            err_text, err_hash, err_trunc = child._drains[1].finish()
        else:
            out_text, out_hash, out_trunc = _EMPTY_DRAIN_RESULT
            err_text, err_hash, err_trunc = _EMPTY_DRAIN_RESULT
        self._close_streams(child)

        if timed_out:
            returncode = TIMEOUT_RETURNCODE
        else:
            rc = child._proc.returncode
            returncode = rc if isinstance(rc, int) else (0 if not child._terminated else 1)

        self._release_handle(child)
        return ProcessExecutionResult(
            returncode=returncode,
            stdout=out_text,
            stderr=err_text,
            timed_out=timed_out,
            killed_tree=killed_tree,
            duration_ms=duration_ms,
            stdout_sha256=out_hash,
            stderr_sha256=err_hash,
            output_truncated=out_trunc or err_trunc,
            termination_reason=child._term_reason,
        )

    def _close_streams(self, child: ChildHandle) -> None:
        proc = child._proc
        if proc is None:
            return
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is None:
                continue
            try:
                stream.close()
            except Exception:
                pass

    def _release_handle(self, child: ChildHandle) -> None:
        child._done = True
        if _IS_WINDOWS and child._job:
            try:
                _k32.CloseHandle(child._job)
            except Exception:
                pass
            child._job = None
        with self._lock:
            self._children.pop(child._id, None)

    # ------------------------------------------------------------------ close
    def close(self) -> None:
        self._closed = True
        with self._lock:
            pending = list(self._children.values())
        for child in pending:
            if not child._done:
                try:
                    self.terminate(child, reason="supervisor_close")
                except Exception:
                    pass
                if child._proc is not None:
                    try:
                        child._proc.wait(timeout=5.0)
                    except Exception:
                        try:
                            child._proc.kill()
                        except Exception:
                            pass
                if child._drains is not None:
                    for d in child._drains:
                        d.join(timeout=5.0)
                self._close_streams(child)
                self._release_handle(child)

    def __enter__(self) -> "ChildSupervisor":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
