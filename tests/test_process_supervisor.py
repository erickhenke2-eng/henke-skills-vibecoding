"""Testes do supervisor de filhos por propriedade (`henke/process_supervisor.py`).

Contrato (Wave 7, R7/R1):
1. Filho + neto dormem; timeout do pai retorna 124 e os dois PIDs desaparecem.
2. `SUPERVISOR_UNAVAILABLE` (125) quando a supervisão falha — o filho nunca corre livre.
3. Handle é propriedade do supervisor: uso externo, duplo wait ou uso pós-close
   gera erro explícito; `close()` encerra filhos pendentes.
4. Streams drenados até EOF com teto de retenção e hash do fluxo inteiro.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from henke.process_supervisor import (
    SUPERVISOR_UNAVAILABLE,
    TIMEOUT_RETURNCODE,
    ChildSupervisor,
    SupervisorUnavailableError,
)

_IS_WINDOWS = sys.platform == "win32"


def _pid_alive(pid: int) -> bool:
    """Consulta passiva (sem autoridade de morte) sobre a existência do PID."""
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        STILL_ACTIVE = 259
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _wait_dead(pid: int, timeout_sec: float = 3.0) -> bool:
    end = time.monotonic() + timeout_sec
    while time.monotonic() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def _safety_cleanup(pids: list[int]) -> None:
    """Rede de segurança para fixtures que vazarem — mata somente PIDs do teste."""
    for pid in pids:
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    check=False, capture_output=True, timeout=5,
                )
            else:
                os.kill(pid, 9)
        except Exception:
            pass


class TestSupervisorNormalExecution(unittest.TestCase):
    """Execução supervisionada sem timeout."""

    def test_normal_run_collects_streams_and_hashes(self):
        with ChildSupervisor() as sup:
            child = sup.start(
                [sys.executable, "-c",
                 "import sys; sys.stdout.write('OUT_HENKE'); sys.stderr.write('ERR_HENKE')"],
                cwd=Path.cwd(),
            )
            res = sup.wait(child, timeout_sec=15.0)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "OUT_HENKE")
        self.assertEqual(res.stderr, "ERR_HENKE")
        self.assertFalse(res.timed_out)
        self.assertFalse(res.killed_tree)
        self.assertEqual(res.termination_reason, "")
        self.assertEqual(res.stdout_sha256, hashlib.sha256(b"OUT_HENKE").hexdigest())
        self.assertEqual(res.stderr_sha256, hashlib.sha256(b"ERR_HENKE").hexdigest())
        self.assertFalse(res.output_truncated)
        self.assertGreaterEqual(res.duration_ms, 0)

    def test_nonzero_exit_is_propagated(self):
        with ChildSupervisor() as sup:
            child = sup.start([sys.executable, "-c", "import sys; sys.exit(7)"], cwd=Path.cwd())
            res = sup.wait(child, timeout_sec=15.0)
        self.assertEqual(res.returncode, 7)
        self.assertFalse(res.timed_out)

    def test_bounded_retention_marks_truncation_but_hashes_all(self):
        payload = b"X" * (64 * 1024)
        code = (
            "import sys; sys.stdout.buffer.write(b'X' * 65536); sys.stdout.buffer.flush()"
        )
        with ChildSupervisor() as sup:
            child = sup.start([sys.executable, "-c", code], cwd=Path.cwd(),
                              max_output_bytes=4096)
            res = sup.wait(child, timeout_sec=15.0)
        self.assertEqual(res.returncode, 0)
        self.assertTrue(res.output_truncated)
        self.assertEqual(len(res.stdout.encode("utf-8")), 4096)
        self.assertEqual(res.stdout_sha256, hashlib.sha256(payload).hexdigest())

    def test_empty_argv_raises(self):
        with ChildSupervisor() as sup:
            with self.assertRaises(ValueError):
                sup.start([], cwd=Path.cwd())


class TestSupervisorTimeoutAndTreeKill(unittest.TestCase):
    """Timeout retorna 124 e encerra a árvore registrada (filho + neto)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.fixture_pids: list[int] = []
        self.addCleanup(_safety_cleanup, self.fixture_pids)

    def test_timeout_returns_124_and_tree_pids_die(self):
        pid_file = Path(self.temp_dir.name) / "tree_pids.txt"
        child_code = (
            "import sys, os, subprocess, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
            "    f.write(f'{os.getpid()},{p.pid}')\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        with ChildSupervisor() as sup:
            child = sup.start([sys.executable, "-c", child_code, str(pid_file)],
                              cwd=Path(self.temp_dir.name))
            res = sup.wait(child, timeout_sec=1.0)

        self.assertTrue(res.timed_out)
        self.assertEqual(res.returncode, TIMEOUT_RETURNCODE)
        self.assertEqual(res.returncode, 124)
        self.assertTrue(res.killed_tree)
        self.assertEqual(res.termination_reason, "timeout")

        content = pid_file.read_text(encoding="utf-8").strip()
        child_pid, grandchild_pid = (int(p) for p in content.split(","))
        self.fixture_pids.extend([child_pid, grandchild_pid])
        self.assertTrue(_wait_dead(child_pid), f"filho {child_pid} sobreviveu ao timeout")
        self.assertTrue(_wait_dead(grandchild_pid), f"neto {grandchild_pid} sobreviveu ao timeout")

    def test_partial_stdout_survives_timeout(self):
        code = (
            "import sys, time; sys.stdout.write('PARCIAL_SUP\\n'); "
            "sys.stdout.flush(); time.sleep(60)"
        )
        with ChildSupervisor() as sup:
            child = sup.start([sys.executable, "-c", code], cwd=Path.cwd())
            res = sup.wait(child, timeout_sec=0.6)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.returncode, 124)
        self.assertIn("PARCIAL_SUP", res.stdout)


class TestSupervisorOwnershipContract(unittest.TestCase):
    """Handle pertence ao supervisor; violações geram erro explícito."""

    def test_double_wait_raises(self):
        with ChildSupervisor() as sup:
            child = sup.start([sys.executable, "-c", "print(1)"], cwd=Path.cwd())
            sup.wait(child, timeout_sec=15.0)
            with self.assertRaises(RuntimeError):
                sup.wait(child, timeout_sec=1.0)

    def test_foreign_handle_rejected(self):
        with ChildSupervisor() as sup_a, ChildSupervisor() as sup_b:
            child = sup_a.start([sys.executable, "-c", "print(1)"], cwd=Path.cwd())
            with self.assertRaises(ValueError):
                sup_b.wait(child, timeout_sec=1.0)
            sup_a.wait(child, timeout_sec=15.0)

    def test_use_after_close_raises(self):
        sup = ChildSupervisor()
        child = sup.start([sys.executable, "-c", "print(1)"], cwd=Path.cwd())
        sup.wait(child, timeout_sec=15.0)
        sup.close()
        with self.assertRaises(RuntimeError):
            sup.start([sys.executable, "-c", "print(1)"], cwd=Path.cwd())

    def test_terminate_rejects_foreign_handle(self):
        with ChildSupervisor() as sup_a, ChildSupervisor() as sup_b:
            child = sup_a.start([sys.executable, "-c", "import time; time.sleep(30)"],
                                cwd=Path.cwd())
            with self.assertRaises(ValueError):
                sup_b.terminate(child, reason="invasao")
            sup_a.terminate(child, reason="cancelled")
            res = sup_a.wait(child, timeout_sec=15.0)
            self.assertEqual(res.termination_reason, "cancelled")
            self.assertFalse(res.timed_out)

    def test_close_terminates_pending_children(self):
        sup = ChildSupervisor()
        child = sup.start([sys.executable, "-c", "import time; time.sleep(60)"],
                          cwd=Path.cwd())
        pid = child.pid
        self.assertIsNotNone(pid)
        sup.close()
        self.assertTrue(_wait_dead(pid), f"filho {pid} sobreviveu ao close()")


class TestSupervisorUnavailable(unittest.TestCase):
    """Falha de supervisão → 125 e filho nunca executa sem Job."""

    def test_failed_start_reports_125(self):
        with ChildSupervisor() as sup:
            backend = "_start_windows" if _IS_WINDOWS else "_start_posix"
            with patch.object(sup, backend, side_effect=SupervisorUnavailableError("falha simulada")):
                child = sup.start([sys.executable, "-c", "print('NAO_DEVE_RODAR')"],
                                  cwd=Path.cwd())
            res = sup.wait(child, timeout_sec=5.0)
        self.assertEqual(res.returncode, SUPERVISOR_UNAVAILABLE)
        self.assertEqual(res.returncode, 125)
        self.assertEqual(res.termination_reason, "supervisor_unavailable")
        self.assertFalse(res.timed_out)
        self.assertEqual(res.stdout, "")

    def test_dead_handle_terminate_releases_and_wait_fails(self):
        """Handle de filho que nunca executou: terminate libera e wait falha."""
        with ChildSupervisor() as sup:
            backend = "_start_windows" if _IS_WINDOWS else "_start_posix"
            with patch.object(sup, backend, side_effect=SupervisorUnavailableError("falha")):
                child = sup.start([sys.executable, "-c", "print(1)"], cwd=Path.cwd())
            sup.terminate(child, reason="impossivel")
            with self.assertRaises(RuntimeError):
                sup.wait(child)

    @unittest.skipUnless(_IS_WINDOWS, "caminho Win32: Job não vaza quando Popen falha")
    def test_popen_failure_closes_job_handle(self):
        """Popen falhando (executável ausente) fecha o Job já criado — sem leak."""
        fake_job = 424242
        with ChildSupervisor() as sup:
            with patch("henke.process_supervisor._win_create_job",
                       return_value=fake_job), \
                 patch("subprocess.Popen",
                       side_effect=FileNotFoundError("exe ausente")) as popen_mock, \
                 patch("henke.process_supervisor._k32.CloseHandle") as close_mock:
                with self.assertRaises(FileNotFoundError):
                    sup._start_windows([r"C:\inexistente.exe"], Path.cwd(), {}, 1024)
        close_mock.assert_called_once_with(fake_job)
        self.assertIs(popen_mock.call_args.kwargs["close_fds"], True)

    @unittest.skipUnless(_IS_WINDOWS, "caminho Win32: associação falha antes do resume")
    def test_assign_failure_never_runs_unsupervised(self):
        marker = Path(self._tmpfile())
        with ChildSupervisor() as sup:
            with patch("henke.process_supervisor._k32.AssignProcessToJobObject", return_value=0):
                child = sup.start(
                    [sys.executable, "-c",
                     f"open(r'{marker}', 'w').write('RODOU_SEM_SUPERVISAO')"],
                    cwd=Path.cwd(),
                )
            res = sup.wait(child, timeout_sec=10.0)
        self.assertEqual(res.returncode, 125)
        time.sleep(0.5)
        self.assertFalse(marker.exists(), "filho executou mesmo com associação falha")

    def _tmpfile(self) -> str:
        fd, name = tempfile.mkstemp(suffix=".marker")
        os.close(fd)
        os.unlink(name)
        return name


if __name__ == "__main__":
    unittest.main()
