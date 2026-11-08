"""Testes de execução atômica de processos e eliminação de zumbis no Windows.

Valida scripts/process_runner.py:
1. Execução normal (exit 0, stdout, stderr, cwd, env, UTF-8, built-ins, stream sem deadlock).
2. Timeout com eliminação atômica da árvore de processos (filho + neto) via taskkill /F /T.
3. Ausência de zumbis no Windows via tasklist.
4. Mocks de edge cases (taskkill 128 / já finalizado, fallback não-Windows, falhas).
5. Execução rápida e determinística (< 3s total) e limpeza garantida via addCleanup.
"""
from __future__ import annotations

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

from scripts.process_runner import (
    ProcessExecutionResult,
    kill_process_tree,
    run_process,
)


def is_pid_alive_tasklist(pid: int) -> bool:
    """Verifica de forma agnóstica a idioma se o PID existe no tasklist do Windows."""
    if not sys.platform.startswith("win"):
        return False
    try:
        res = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        for line in res.stdout.strip().splitlines():
            parts = [p.strip(' "') for p in line.split(",")]
            if len(parts) >= 2 and parts[1] == str(pid):
                return True
        return False
    except Exception:
        return False


def wait_until_dead(pid: int, timeout_sec: float = 2.0, interval: float = 0.05) -> bool:
    """Aguarda até que o PID desapareça do tasklist, retornando True se morto."""
    start = time.time()
    while time.time() - start < timeout_sec:
        if not is_pid_alive_tasklist(pid):
            return True
        time.sleep(interval)
    return not is_pid_alive_tasklist(pid)


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
        res = run_process(cmd, timeout_sec=5.0)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)
        lines = res.stdout.strip().splitlines()
        self.assertEqual(len(lines), line_count)

    @unittest.skipUnless(sys.platform.startswith("win"), "Comandos internos cmd.exe exigem Windows")
    def test_windows_builtins_resolution(self):
        res = run_process(["echo", "henke_process_runner"])
        self.assertEqual(res.returncode, 0)
        self.assertIn("henke_process_runner", res.stdout)


class TestProcessRunnerTimeoutTreeKill(unittest.TestCase):
    """Testes de timeout e eliminação atômica da árvore de processos no Windows."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def _force_cleanup_pids(self, pids: list[int]):
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass

    @unittest.skipUnless(sys.platform.startswith("win"), "Exige Windows para validação física do taskkill /F /T")
    def test_timeout_kills_child_and_grandchild_tree(self):
        """Valida que o timeout elimina atomicamente tanto o processo filho quanto o neto."""
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

        self.assertTrue(pid_file.exists(), "Arquivo de PIDs deveria ter sido gerado pelo filho")
        content = pid_file.read_text(encoding="utf-8").strip()
        parts = [int(p) for p in content.split(",") if p.isdigit()]
        self.assertEqual(len(parts), 2, f"Esperava 2 PIDs (filho e neto), obteve: {content}")
        child_pid, grandchild_pid = parts[0], parts[1]

        self.addCleanup(self._force_cleanup_pids, [child_pid, grandchild_pid])

        child_dead = wait_until_dead(child_pid, timeout_sec=2.0)
        grandchild_dead = wait_until_dead(grandchild_pid, timeout_sec=2.0)

        self.assertTrue(child_dead, f"Processo filho (PID {child_pid}) ainda está vivo após taskkill!")
        self.assertTrue(grandchild_dead, f"Processo neto (PID {grandchild_pid}) ainda está vivo após taskkill!")

    def test_partial_stdout_captured_on_timeout(self):
        """Valida que buffers de stdout gravados antes do timeout são preservados."""
        code = "import sys, time; sys.stdout.write('SAIDA_PARCIAL_HENKE\\n'); sys.stdout.flush(); time.sleep(30)"
        cmd = [sys.executable, "-c", code]
        res = run_process(cmd, timeout_sec=0.5)
        self.assertTrue(res.timed_out)
        self.assertIn("SAIDA_PARCIAL_HENKE", res.stdout)


class TestProcessRunnerEdgeCasesAndMocking(unittest.TestCase):
    """Testes de casos de borda e simulações com mock."""

    @patch("subprocess.run")
    def test_taskkill_already_exited_race_condition(self, mock_run):
        """Simula taskkill retornando 128 (processo morreu antes do taskkill executar)."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["taskkill", "/F", "/T", "/PID", "12345"],
            returncode=128,
            stdout="",
            stderr="ERRO: o processo 12345 não foi encontrado.",
        )
        success = kill_process_tree(12345)
        self.assertTrue(success)

    @patch("subprocess.run", side_effect=FileNotFoundError("taskkill not found"))
    def test_taskkill_missing_fallback(self, mock_run):
        """Simula ausência de taskkill, garantindo que não lance exceção fatal."""
        success = kill_process_tree(12345)
        self.assertFalse(success)

    @patch("sys.platform", "linux")
    @patch("os.killpg", create=True)
    @patch("os.getpgid", create=True, return_value=54321)
    def test_non_windows_platform_fallback(self, mock_getpgid, mock_killpg):
        """Valida que em ambientes não-Windows o kill_process_tree usa os.killpg."""
        success = kill_process_tree(54321)
        self.assertTrue(success)
        mock_killpg.assert_called_once()

    def test_kill_process_tree_invalid_pid(self):
        self.assertIs(kill_process_tree(0), False)
        self.assertIs(kill_process_tree(-10), False)


if __name__ == "__main__":
    unittest.main()
