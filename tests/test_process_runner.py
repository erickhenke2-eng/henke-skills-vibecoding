"""Testes da fachada compatível de execução de processos (`scripts/process_runner.py`).

Valida:
1. Execução normal (exit 0, stdout, stderr, cwd, env, UTF-8, built-ins, stream sem deadlock).
2. Timeout com eliminação da árvore de processos (filho + neto) via supervisão por
   propriedade — Job Object no Windows, process group próprio no POSIX.
3. Ausência da API pública de morte por PID (`kill_process_tree` foi removida na
   Wave 7; permanece apenas um sentinel não-chamável).
4. Semântica Wave 6 da fachada: timeout → `timed_out=True` com returncode legado.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

import scripts.process_runner as process_runner
from scripts.process_runner import (
    ProcessExecutionResult,
    run_process,
)

_IS_WINDOWS = sys.platform == "win32"


def _pid_alive(pid: int) -> bool:
    """Consulta passiva sobre a existência do PID (nunca usada para matar)."""
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


class TestProcessRunnerNormalExecution(unittest.TestCase):
    """Testes de execução padrão sem timeout."""

    def test_normal_execution_exit_0(self):
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.stderr.write('world')"]
        res = run_process(cmd)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "hello")
        self.assertEqual(res.stderr, "world")
        self.assertFalse(res.timed_out)
        self.assertFalse(res.killed_tree)

    def test_normal_execution_exit_nonzero(self):
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('erro_esperado'); sys.exit(42)"]
        res = run_process(cmd)
        self.assertEqual(res.returncode, 42)
        self.assertEqual(res.stderr, "erro_esperado")
        self.assertFalse(res.timed_out)
        self.assertFalse(res.killed_tree)

    def test_utf8_decoding_and_replacement(self):
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write('ação 漢字 café'); sys.stderr.write('aviso ⚠️')"]
        res = run_process(cmd)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "ação 漢字 café")
        self.assertEqual(res.stderr, "aviso ⚠️")

    def test_custom_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cmd = [sys.executable, "-c", "import os; print(os.getcwd())"]
            res = run_process(cmd, cwd=tmpdir)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(Path(res.stdout.strip()).resolve(), Path(tmpdir).resolve())

    def test_custom_env(self):
        cmd = [sys.executable, "-c", "import os; print(os.environ.get('HENKE_TEST_VAR', ''))"]
        res = run_process(cmd, env={"HENKE_TEST_VAR": "henke_val_42"})
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "henke_val_42")

    def test_none_timeout_runs_to_completion(self):
        cmd = [sys.executable, "-c", "print('done')"]
        res = run_process(cmd, timeout_sec=None)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "done")
        self.assertFalse(res.timed_out)

    def test_string_cmd_tokenization(self):
        cmd_str = f'"{sys.executable}" -c "print(12345)"'
        res = run_process(cmd_str)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), "12345")

    def test_empty_cmd_raises_value_error(self):
        with self.assertRaises(ValueError):
            run_process([])
        with self.assertRaises(ValueError):
            run_process("")

    def test_large_output_stream_no_deadlock(self):
        """Verifica que saída superior a 100 KB não causa deadlock no pipe OS."""
        line_count = 2000
        cmd = [sys.executable, "-c", f"for i in range({line_count}): print('linha_' * 10 + str(i))"]
        res = run_process(cmd, timeout_sec=10.0)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)
        lines = res.stdout.strip().splitlines()
        self.assertEqual(len(lines), line_count)

    def test_stream_hashes_cover_full_output(self):
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write('HENKE_HASH'); sys.stderr.write('E2')"]
        res = run_process(cmd)
        import hashlib
        self.assertEqual(res.stdout_sha256, hashlib.sha256(b"HENKE_HASH").hexdigest())
        self.assertEqual(res.stderr_sha256, hashlib.sha256(b"E2").hexdigest())

    def test_bounded_retention_truncates_and_flags(self):
        cmd = [sys.executable, "-c", "import sys; sys.stdout.write('Y' * 32768)"]
        res = run_process(cmd, max_output_bytes=1024)
        self.assertEqual(res.returncode, 0)
        self.assertTrue(res.output_truncated)
        self.assertEqual(len(res.stdout), 1024)

    @unittest.skipUnless(sys.platform.startswith("win"), "Comandos internos cmd.exe exigem Windows")
    def test_windows_builtins_resolution(self):
        res = run_process(["echo", "henke_process_runner"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("henke_process_runner", res.stdout)


class TestProcessRunnerTimeoutTreeKill(unittest.TestCase):
    """Timeout elimina a árvore supervisionada (filho + neto)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.fixture_pids: list[int] = []
        self.addCleanup(self._safety_cleanup)

    def _safety_cleanup(self):
        for pid in self.fixture_pids:
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

    def test_timeout_kills_child_and_grandchild_tree(self):
        """Timeout elimina filho e neto criados pelo filho supervisionado."""
        pid_file = Path(self.temp_dir.name) / "pids.txt"
        child_code = (
            "import sys, os, subprocess, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
            "with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
            "    f.write(f'{os.getpid()},{p.pid}')\n"
            "time.sleep(30)\n"
        )
        cmd = [sys.executable, "-c", child_code, str(pid_file)]

        res = run_process(cmd, timeout_sec=0.8)

        self.assertTrue(res.timed_out, "Deveria ter marcado timed_out=True")
        self.assertTrue(res.killed_tree, "Deveria ter marcado killed_tree=True")
        self.assertNotEqual(res.returncode, 0, "Returncode em timeout não deve ser 0")
        self.assertEqual(res.termination_reason, "timeout")

        self.assertTrue(pid_file.exists(), "Arquivo de PIDs deveria ter sido gerado pelo filho")
        content = pid_file.read_text(encoding="utf-8").strip()
        parts = [int(p) for p in content.split(",") if p.isdigit()]
        self.assertEqual(len(parts), 2, f"Esperava 2 PIDs (filho e neto), obteve: {content}")
        child_pid, grandchild_pid = parts[0], parts[1]
        self.fixture_pids.extend([child_pid, grandchild_pid])

        self.assertTrue(_wait_dead(child_pid), f"Processo filho (PID {child_pid}) ainda está vivo!")
        self.assertTrue(_wait_dead(grandchild_pid), f"Processo neto (PID {grandchild_pid}) ainda está vivo!")

    def test_partial_stdout_captured_on_timeout(self):
        """Valida que buffers de stdout gravados antes do timeout são preservados."""
        code = "import sys, time; sys.stdout.write('SAIDA_PARCIAL_HENKE\\n'); sys.stdout.flush(); time.sleep(30)"
        cmd = [sys.executable, "-c", code]
        res = run_process(cmd, timeout_sec=0.5)
        self.assertTrue(res.timed_out)
        self.assertIn("SAIDA_PARCIAL_HENKE", res.stdout)


class TestNoPublicPidKillApi(unittest.TestCase):
    """Morte por PID arbitrário não faz mais parte da API pública."""

    def test_kill_process_tree_is_not_callable_api(self):
        sentinel = getattr(process_runner, "kill_process_tree", None)
        self.assertFalse(callable(sentinel),
                         "kill_process_tree não pode ser função pública na Wave 7")

    def test_no_helper_kills_arbitrary_pid(self):
        """Nenhuma função definida no módulo mata processo por PID fornecido."""
        for name in dir(process_runner):
            if name.startswith("_"):
                continue
            obj = getattr(process_runner, name)
            if not callable(obj):
                continue
            if getattr(obj, "__module__", None) != process_runner.__name__:
                continue  # símbolo importado/re-exportado, não é API definida aqui
            if name not in {"run_process"}:
                self.fail(f"API pública inesperada exposta: {name}")


if __name__ == "__main__":
    unittest.main()
