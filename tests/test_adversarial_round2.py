"""Banca rodada 2: provas físicas de falhas de captura, Git e event log."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from henke import event_store
from henke.metricas import metaconfig_diff
from henke.process_supervisor import (
    _StreamDrain, ChildHandle, ChildSupervisor, SUPERVISOR_UNAVAILABLE,
)


class _FailingStream:
    """Simula pipe parcialmente legível: retorna bytes e falha antes do EOF."""

    def __init__(self):
        self.calls = 0

    def read(self, _n):
        self.calls += 1
        if self.calls == 1:
            return b"partial output"
        raise OSError("simulated pipe read failure")


class _ExitedProcess:
    returncode = 0
    stdout = None
    stderr = None
    stdin = None

    def wait(self, timeout=None):
        return 0


class TestSupervisorCaptureFailure(unittest.TestCase):
    def test_pipe_read_error_never_returns_success(self):
        bad = _StreamDrain(_FailingStream(), 4096)
        good = _StreamDrain(io.BytesIO(b""), 4096)
        bad.start()
        good.start()
        with ChildSupervisor() as supervisor:
            child = ChildHandle(supervisor, 1, _ExitedProcess(),
                                None, None, (bad, good))
            supervisor._children[1] = child
            result = supervisor.wait(child, timeout_sec=1.0)
        self.assertEqual(result.returncode, SUPERVISOR_UNAVAILABLE,
                         "pipe quebrado não pode fabricar exit 0")
        self.assertIn("stream_capture_incomplete", result.termination_reason)
        self.assertIn("partial output", result.stdout)


class TestEventStoreIntegrity(unittest.TestCase):
    def test_blank_line_in_signed_log_is_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            repo = temp / "repo"
            repo.mkdir()
            state_dir = temp / "state"
            tip = temp / "tip.json"
            with patch.dict(os.environ, {"HENKE_STATE_DIR": str(state_dir)}), \
                 patch.object(event_store, "_tip_path", return_value=tip):
                event_store.append_event(repo, "gate_result",
                                         {"gate": "hazard", "exit_code": 0})
                self.assertEqual(event_store.verify(repo), [])
                path = event_store.events_path(repo)
                with path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write("\n")
                self.assertTrue(event_store.verify(repo),
                                "linha extra fora do hash-chain passou como íntegra")


class TestMetaconfigGitFailures(unittest.TestCase):
    def test_failed_ls_files_must_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            for fail_type in ("untracked", "ignored"):
                with self.subTest(fail_type=fail_type):
                    def fake_git(_repo, *args):
                        if args and args[0] == "ls-files":
                            is_ignored = "--ignored" in args
                            if (fail_type == "ignored") == is_ignored:
                                return subprocess.CompletedProcess(
                                    args, 128, stdout="", stderr="fatal: index unreadable")
                        return subprocess.CompletedProcess(
                            args, 0, stdout="", stderr="")
                    with patch.object(metaconfig_diff, "_git", side_effect=fake_git):
                        changed, error = metaconfig_diff._changed_protected_files(
                            repo, "HEAD")
                    self.assertIsNone(changed)
                    self.assertIn("ls-files", error or "")


if __name__ == "__main__":
    unittest.main()
