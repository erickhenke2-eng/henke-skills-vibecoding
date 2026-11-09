"""Testes de workspaces efêmeros (`scripts/ephemeral.py`).

Contrato: TTL com auto-destruição, marker validado, contenção de escrita,
supervisão de filhos próprios e GC que só remove expirados com marker válido.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts.ephemeral import (
    EphemeralConfig,
    _MARKER_NAME,
    ephemeral_gc,
    ephemeral_test,
    ephemeral_workspace,
)


class TestEphemeralWorkspace(unittest.TestCase):
    """Criação, escrita contida e cleanup idempotente."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name)

    def test_workspace_created_with_marker_and_ttl(self):
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo) as ws:
            self.assertTrue(ws.path.is_dir())
            marker = json.loads((ws.path / _MARKER_NAME).read_text(encoding="utf-8"))
            self.assertEqual(marker["kind"], "henke-ephemeral")
            self.assertEqual(marker["repo"], str(self.repo.resolve()))
            self.assertIn("expires_at", marker)
            self.assertTrue(ws.path.name.startswith("henke-eph-"))

    def test_close_removes_workspace_dir(self):
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo) as ws:
            path = ws.path
        self.assertFalse(path.exists(), "close() deve remover o diretório próprio")

    def test_close_is_idempotent(self):
        ws = ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo)
        path = ws.path
        ws.close()
        ws.close()  # segunda chamada não pode explodir
        self.assertFalse(path.exists())

    def test_write_and_path_for_contained(self):
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo) as ws:
            p = ws.write_text("a/b/arquivo.txt", "conteudo")
            self.assertEqual(p.read_text(encoding="utf-8"), "conteudo")
            self.assertIn(ws.path.resolve(), p.resolve().parents)

    def test_path_escape_rejected(self):
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo) as ws:
            with self.assertRaises(ValueError):
                ws.path_for("../fora.txt")
            with self.assertRaises(ValueError):
                ws.write_text("../../sistema.txt", "x")
            with self.assertRaises(ValueError):
                ws.path_for("C:/Windows/falso.txt" if sys.platform == "win32" else "/etc/falso")

    def test_write_after_close_raises(self):
        ws = ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo)
        ws.close()
        with self.assertRaises(RuntimeError):
            ws.write_text("x.txt", "x")

    def test_run_uses_supervisor_and_respects_ttl(self):
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=30), repo_root=self.repo) as ws:
            res = ws.run([sys.executable, "-c", "print('ok_ws')"], timeout_sec=10)
            self.assertEqual(res.returncode, 0)
            self.assertIn("ok_ws", res.stdout)
            res2 = ws.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout_sec=0.3)
            self.assertTrue(res2.timed_out)
            self.assertEqual(res2.returncode, 124)

    def test_close_kills_running_child(self):
        ws = ephemeral_workspace(EphemeralConfig(ttl_seconds=300), repo_root=self.repo)
        import threading as _t
        result_holder = []
        def _runner():
            result_holder.append(
                ws.run([sys.executable, "-c", "import time; time.sleep(60)"], timeout_sec=None)
            )
        t = _t.Thread(target=_runner, daemon=True)
        t.start()
        time.sleep(0.5)
        ws.close()
        t.join(timeout=10.0)
        self.assertEqual(len(result_holder), 1)
        res = result_holder[0]
        # O filho foi encerrado pelo fechamento do supervisor: o sinal correto é
        # termination_reason (o exit code de morte por Job não é normativo).
        self.assertEqual(res.termination_reason, "supervisor_close")
        self.assertFalse(res.timed_out)


class TestEphemeralGC(unittest.TestCase):
    """ephemeral_gc só remove expirados com marker válido do repo."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name)

    def _make_expired_workspace(self, minutes_ago: int = 10) -> Path:
        ws = ephemeral_workspace(EphemeralConfig(ttl_seconds=60), repo_root=self.repo)
        ws.close()  # remove o dir — recriamos manualmente como resto de crash
        root = ws.path
        root.mkdir(parents=True)
        expires = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        marker = {
            "kind": "henke-ephemeral",
            "workspace_id": root.name.rsplit("-", 1)[-1],
            "session_id": "test",
            "repo": str(self.repo.resolve()),
            "created_at": "2020-01-01T00:00:00Z",
            "expires_at": expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }
        (root / _MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
        return root

    def test_gc_removes_expired_valid_workspace(self):
        leftover = self._make_expired_workspace()
        removed = ephemeral_gc(self.repo, now=datetime.now(timezone.utc))
        self.assertEqual(removed, 1)
        self.assertFalse(leftover.exists())

    def test_gc_keeps_unexpired_and_unmarked(self):
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=3600), repo_root=self.repo) as ws:
            alive = ws.path
            unmarked = Path(tempfile.gettempdir()) / f"henke-eph-fake-{os.getpid()}"
            unmarked.mkdir(exist_ok=True)
            try:
                removed = ephemeral_gc(self.repo, now=datetime.now(timezone.utc))
                self.assertEqual(removed, 0)
                self.assertTrue(alive.exists())
                self.assertTrue(unmarked.exists())
            finally:
                import shutil
                shutil.rmtree(unmarked, ignore_errors=True)


class TestEphemeralTestDecorator(unittest.TestCase):
    """`@ephemeral_test` injeta `ws` e sempre fecha."""

    def test_decorator_injects_ws_and_closes(self):
        captured = {}

        @ephemeral_test(ttl_seconds=60)
        def sample(ws):
            captured["path"] = ws.path
            captured["inside"] = ws.path.is_dir()
            return ws.path

        out = sample()
        self.assertTrue(captured["inside"])
        self.assertFalse(out.exists(), "workspace do decorator deve ser removida ao sair")

    def test_decorator_propagates_exception_and_cleans(self):
        captured = {}

        @ephemeral_test(ttl_seconds=60)
        def boom(ws):
            captured["path"] = ws.path
            raise RuntimeError("falha de teste")

        with self.assertRaises(RuntimeError):
            boom()
        self.assertFalse(captured["path"].exists())


if __name__ == "__main__":
    unittest.main()
