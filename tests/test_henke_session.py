"""Testes do sentinel de sessão (`scripts/henke_session.py`).

Contrato: sentinel JSON por sessão, heartbeat de 1h, registro de workspaces
efêmeras validadas, GC que só marca `reaped` expirados e delega a
`ephemeral_gc` — owner_pid nunca é autoridade de morte.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts.ephemeral import EphemeralConfig, ephemeral_workspace
from scripts.henke_session import (
    end_session,
    heartbeat,
    load_session,
    register_ephemeral,
    session_gc,
    start_session,
)


def _utcnow():
    return datetime.now(timezone.utc)


class _RepoSessionCase(unittest.TestCase):
    """Exporta HENKE_WORKSPACE para as funções que recebem só session_id."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name)
        self._env = patch.dict(os.environ, {"HENKE_WORKSPACE": str(self.repo)})
        self._env.start()
        self.addCleanup(self._env.stop)


class TestSessionLifecycle(_RepoSessionCase):

    def test_start_session_creates_active_sentinel(self):
        s = start_session("devin-test", self.repo, "run-123")
        self.assertEqual(s.state, "active")
        self.assertEqual(s.agent, "devin-test")
        self.assertEqual(s.run_id, "run-123")
        self.assertTrue(s.session_id.startswith("ses-"))
        path = self.repo / ".henke" / "sessions" / f"{s.session_id}.sentinel.json"
        self.assertTrue(path.is_file())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["owner_pid"], os.getpid())

    def test_heartbeat_renews_window(self):
        s = start_session("devin-test", self.repo, "run-1")
        before = s.heartbeat_expires_at
        import time as _t
        _t.sleep(0.02)
        heartbeat(s.session_id)
        s2 = load_session(s.session_id)
        self.assertGreater(s2.heartbeat_expires_at, before)

    def test_end_session_marks_closed(self):
        s = start_session("devin-test", self.repo, "run-1")
        end_session(s.session_id)
        self.assertEqual(load_session(s.session_id).state, "closed")

    def test_heartbeat_after_close_rejected(self):
        s = start_session("devin-test", self.repo, "run-1")
        end_session(s.session_id)
        with self.assertRaises(RuntimeError):
            heartbeat(s.session_id)

    def test_session_id_traversal_rejected(self):
        with self.assertRaises(ValueError):
            load_session("../../etc/passwd")
        with self.assertRaises(ValueError):
            heartbeat("../x")


class TestRegisterEphemeral(_RepoSessionCase):

    def test_register_valid_workspace(self):
        s = start_session("devin-test", self.repo, "run-1")
        with ephemeral_workspace(EphemeralConfig(ttl_seconds=300), repo_root=self.repo) as ws:
            register_ephemeral(s.session_id, ws.path)
            s2 = load_session(s.session_id)
            self.assertIn(str(ws.path.resolve()), s2.ephemeral_workspaces)

    def test_register_rejects_invalid_marker(self):
        s = start_session("devin-test", self.repo, "run-1")
        bogus = Path(tempfile.gettempdir()) / f"henke-eph-bogus-{os.getpid()}"
        bogus.mkdir(exist_ok=True)
        try:
            with self.assertRaises(ValueError):
                register_ephemeral(s.session_id, bogus)
        finally:
            import shutil
            shutil.rmtree(bogus, ignore_errors=True)


class TestSessionGC(_RepoSessionCase):

    def test_gc_reaps_expired_active_only(self):
        s_old = start_session("devin-old", self.repo, "run-old")
        s_new = start_session("devin-new", self.repo, "run-new")
        end_session(s_new.session_id)  # closed, não deve virar reaped

        path = self.repo / ".henke" / "sessions" / f"{s_old.session_id}.sentinel.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["heartbeat_expires_at"] = (_utcnow() - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(data), encoding="utf-8")

        reaped = session_gc(self.repo, now=_utcnow())
        self.assertEqual(reaped, 1)
        self.assertEqual(load_session(s_old.session_id).state, "reaped")
        self.assertEqual(load_session(s_new.session_id).state, "closed")

    def test_gc_respects_active_unexpired(self):
        s = start_session("devin-live", self.repo, "run-1")
        reaped = session_gc(self.repo, now=_utcnow())
        self.assertEqual(reaped, 0)
        self.assertEqual(load_session(s.session_id).state, "active")

    def test_gc_never_uses_owner_pid_for_death(self):
        """O sentinel não mata por PID: apenas marca reaped e chama ephemeral_gc."""
        s = start_session("devin", self.repo, "run-1")
        path = self.repo / ".henke" / "sessions" / f"{s.session_id}.sentinel.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["heartbeat_expires_at"] = (_utcnow() - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(data), encoding="utf-8")

        with patch("scripts.henke_session.ephemeral_gc") as mock_gc:
            reaped = session_gc(self.repo, now=_utcnow())
        self.assertEqual(reaped, 1)
        mock_gc.assert_called_once()
        # nenhum signal enviado ao owner_pid — o próprio teste continua vivo


if __name__ == "__main__":
    unittest.main()
