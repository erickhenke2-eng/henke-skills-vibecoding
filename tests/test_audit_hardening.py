"""Regressões para instalação, índice staged e fingerprint de cache."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from henke.gate_cache import compute_cache_key
from henke.spec_drift import check_spec_drift, get_touched_files

spec = importlib.util.spec_from_file_location(
    "henke_installer_hardening", ROOT / "henke" / "install_skills.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", check=True,
    )
    return res.stdout.strip()


def _init_git(repo: Path) -> str:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Henke Test")
    _git(repo, "config", "user.email", "test@henke.invalid")
    (repo / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return _git(repo, "rev-parse", "HEAD")


class TestInstallTargetAncestors(unittest.TestCase):
    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks indisponíveis")
    def test_rejects_symlinked_agent_ancestor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            home.mkdir()
            external = root / "outside"
            external.mkdir()
            try:
                (home / ".agents").symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"criação de symlink indisponível: {exc}")
            target = home / ".agents" / "skills"
            with self.assertRaises(ValueError):
                installer._assert_install_target(home, target)
            self.assertFalse((external / "skills").exists())

    def test_allows_regular_subdirectories(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            target = home / ".agents" / "skills"
            self.assertEqual(installer._assert_install_target(home, target),
                             home.resolve())


class TestIndexSemantics(unittest.TestCase):
    def test_staged_only_ignores_unstaged_edits(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            head = _init_git(repo)
            (repo / "added file.py").write_text("y = 2\n", encoding="utf-8")
            _git(repo, "add", "added file.py")
            (repo / "base.py").write_text("x = 999\n", encoding="utf-8")
            paths = get_touched_files(repo, against=head, staged_only=True)
            self.assertIn("added file.py", paths)
            self.assertNotIn("base.py", paths)

    def test_spec_drift_staged_scope_excludes_unstaged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            head = _init_git(repo)
            designs = repo / "docs" / "designs"
            designs.mkdir(parents=True)
            spec_path = designs / "feature.md"
            spec_path.write_text(
                "# Teste\n\n## 4. Escopo Declarado\n- `src/ok.py`\n"
                "- `docs/designs/feature.md`\n",
                encoding="utf-8",
            )
            (repo / "src").mkdir()
            (repo / "src" / "ok.py").write_text("ok = True\n", encoding="utf-8")
            _git(repo, "add", "src/ok.py", "docs/designs/feature.md")
            (repo / "base.py").write_text("x = 99\n", encoding="utf-8")
            expected = hashlib.sha256(spec_path.read_bytes()).hexdigest()
            ok, errors = check_spec_drift(
                spec_path, expected_hash=expected, against=head,
                git_root=repo, staged_only=True)
            self.assertTrue(ok, errors)
            ok_worktree, errors_worktree = check_spec_drift(
                spec_path, expected_hash=expected, against=head,
                git_root=repo, staged_only=False)
            self.assertFalse(ok_worktree)
            self.assertTrue(any("base.py" in error for error in errors_worktree))

    def test_cli_staged_accepts_filename_containing_spaces(self):
        from henke.henke_cli import main
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_git(repo)
            (repo / "hello world.py").write_text("hello = 42\n", encoding="utf-8")
            _git(repo, "add", "hello world.py")
            self.assertEqual(main(["check", "--repo", str(repo), "--staged",
                                   "--no-cache", "--full"]), 0)


class TestCacheFingerprint(unittest.TestCase):
    def test_runtime_events_do_not_invalidate_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            script = repo / "gate.py"
            script.write_text("print('pass')\n", encoding="utf-8")
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            state = repo / ".henke"
            state.mkdir()
            (state / "CONFIG.json").write_text('{"enabled":true}', encoding="utf-8")
            key1 = compute_cache_key("test", script, [str(repo)], repo)
            (state / "events.jsonl").write_text('{"event":1}\n', encoding="utf-8")
            (state / "runs").mkdir()
            (state / "runs" / "run1.json").write_text('{"status":1}', encoding="utf-8")
            key2 = compute_cache_key("test", script, [str(repo)], repo)
            self.assertEqual(key1, key2)
            (state / "CONFIG.json").write_text('{"enabled":false}', encoding="utf-8")
            key3 = compute_cache_key("test", script, [str(repo)], repo)
            self.assertNotEqual(key2, key3)


if __name__ == "__main__":
    unittest.main()
