"""test_wave7_integration.py — integração ponta-a-ponta da Wave 7."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import henke.henke_cli  # noqa: F401,E402  (ponte declarada)

CLI = ROOT / "henke" / "henke_cli.py"


def _cli(*args, cwd=None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("HENKE_STATE_DIR", None)
    return subprocess.run(
        [sys.executable, str(CLI), *map(str, args)],
        capture_output=True, text=True, cwd=cwd or str(ROOT), env=env,
        timeout=180,
    )


def _git_repo(path: Path) -> None:
    for cmd in (["git", "init"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=path, capture_output=True)
    (path / "a.py").write_text("X = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)


class TestWatchForegroundOnly(unittest.TestCase):
    def test_watch_requires_tty(self):
        res = _cli("watch", "--repo", ROOT)
        self.assertEqual(res.returncode, 2)
        self.assertIn("foreground", res.stderr + res.stdout)


class TestStagedStrict(unittest.TestCase):
    def test_staged_no_manifest_no_cache(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git_repo(repo)
            res = _cli("check", "--repo", repo, "--staged")
            henke = repo / ".henke"
            self.assertFalse((henke / "scan-manifests").exists(),
                             "staged não deve gravar manifestos")
            self.assertFalse((henke / "cache").exists(),
                             "staged não deve gravar cache")
            self.assertEqual(res.returncode, 0,
                             f"check --staged em repo limpo deve passar: "
                             f"{res.stdout[-600:]} {res.stderr[-300:]}")


class TestTestImpactCLI(unittest.TestCase):
    def test_json_mode(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _git_repo(repo)
            res = _cli("test-impact", "--repo", repo, "--base", "HEAD",
                       "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            data = json.loads(res.stdout)
            self.assertEqual(data["mode"], "selected")
            self.assertEqual(data["test_files"], [])

    def test_git_failure_exit_2(self):
        with tempfile.TemporaryDirectory() as td:
            res = _cli("test-impact", "--repo", td, "--base", "NOPE", "--json")
            self.assertEqual(res.returncode, 2)


class TestCleanCLI(unittest.TestCase):
    def test_dry_run_no_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            pc = repo / "__pycache__"
            pc.mkdir()
            (pc / "x.pyc").write_bytes(b"0")
            res = _cli("clean", "--repo", repo, "--json")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((pc / "x.pyc").is_file(),
                            "dry-run não pode mutar")
            data = json.loads(res.stdout)
            self.assertTrue(data["dry_run"])

    def test_execute_quarantines(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            pc = repo / "__pycache__"
            pc.mkdir()
            (pc / "x.pyc").write_bytes(b"0")
            res = _cli("clean", "--repo", repo, "--execute")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertFalse(pc.exists())

    def test_restore_returns_exact_zero(self):
        """rc exato 0 no restore — 'return None' seria exit 0 mas falha aqui."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            pc = repo / "__pycache__"
            pc.mkdir()
            (pc / "x.pyc").write_bytes(b"0")
            res = _cli("clean", "--repo", repo, "--execute")
            self.assertEqual(res.returncode, 0, res.stderr)
            batch_id = next(
                (repo / ".henke" / "quarantine").iterdir()).name
            rc = henke.henke_cli._cmd_clean(
                repo, execute=True, restore=batch_id)
            self.assertEqual(rc, 0)
            self.assertTrue((pc / "x.pyc").is_file())


class TestSessionCLI(unittest.TestCase):
    def test_start_heartbeat_end(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            res = _cli("session", "start", "--agent", "t", "--workspace", ws,
                       "--run-id", "r1")
            self.assertEqual(res.returncode, 0, res.stderr)
            sid = res.stdout.strip().splitlines()[-1]
            env_dir = ws / ".henke" / "sessions" / f"{sid}.sentinel.json"
            # sentinel pode estar sob state_dir do workspace
            found = env_dir.is_file() or any(
                (ws / ".henke" / "sessions").glob("*.sentinel.json")
            ) if (ws / ".henke" / "sessions").is_dir() else env_dir.is_file()
            self.assertTrue(found, f"sentinel não encontrado: {res.stdout}")
            res = _cli("session", "heartbeat", sid, "--workspace", ws)
            self.assertEqual(res.returncode, 0, res.stderr)
            res = _cli("session", "end", sid, "--workspace", ws)
            self.assertEqual(res.returncode, 0, res.stderr)


class TestCheckParallelAndManifests(unittest.TestCase):
    def test_check_jobs_and_full(self):
        res = _cli("check", "--repo", ROOT, "--jobs", "4", "--no-cache")
        self.assertEqual(res.returncode, 0,
                         f"check na própria suíte deve passar limpo: "
                         f"{res.stdout[-800:]} {res.stderr[-400:]}")

    def test_manifest_files_created_on_check(self):
        """PT-11: manifestos vivem em cache_root (fora do repo), não em state_dir."""
        with tempfile.TemporaryDirectory() as td:
            cd = Path(td) / "cache"
            env = os.environ.copy()
            env["HENKE_CACHE_DIR"] = str(cd)
            res = subprocess.run(
                [sys.executable, str(CLI), "check", "--repo", ROOT],
                capture_output=True, text=True, env=env, timeout=180,
            )
            self.assertEqual(res.returncode, 0,
                             f"check na suíte deve passar limpo: "
                             f"{res.stdout[-600:]} {res.stderr[-300:]}")
            self.assertTrue(
                (cd / "scan-manifests" / "a11y.json").is_file(),
                f"manifesto a11y ausente em {cd}: {res.stderr[:300]}")


if __name__ == "__main__":
    unittest.main()
