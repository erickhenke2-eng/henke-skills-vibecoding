"""
tests/test_install_hook.py — Testes unitários e de integração para o instalador de pre-commit hook.

Valida:
1. Instalação padrão em repositório Git (.git/hooks/pre-commit com marcador HENKE-PRE-COMMIT-HOOK).
2. Idempotência: reexecução não duplica blocos de hook.
3. Backup de hook estrangeiro: renomeação para .legacy quando force=False.
4. Sobrescrita direta com force=True.
5. Rejeição de diretórios que não são repositórios Git (retorno False e código 2 no CLI).
6. Resolução dinâmica de core.hooksPath e git worktrees.
7. Substituição segura de hook vazio ou somente com whitespace.
8. Execução via CLI (`henke install-hook`).
9. Execução real do hook durante `git commit` (bloqueio quando portão falha, sucesso quando portão passa).
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts.henke_cli import main as henke_cli_main
from scripts.install_hook import (
    HENKE_HOOK_MARKER,
    HOOK_SCRIPT_TEMPLATE,
    discover_hooks_dir,
    install_hook,
)


def _init_git_repo(repo_path: Path) -> None:
    """Inicializa um repositório Git sandbox para testes determinísticos."""
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Henke Hook Test",
        GIT_AUTHOR_EMAIL="test@henke.hook",
        GIT_COMMITTER_NAME="Henke Hook Test",
        GIT_COMMITTER_EMAIL="test@henke.hook",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_path), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.name", "Henke Hook Test"], cwd=str(repo_path), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@henke.hook"], cwd=str(repo_path), check=True, capture_output=True, env=env)
    readme = repo_path / "README.md"
    readme.write_text("# Sandbox Git Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_path), check=True, capture_output=True, env=env)


class TestDiscoverHooksDir(unittest.TestCase):
    """Testes para a função discover_hooks_dir."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        _init_git_repo(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_discover_standard_hooks_dir(self):
        hooks_dir = discover_hooks_dir(self.repo_path)
        self.assertIsNotNone(hooks_dir)
        self.assertEqual(hooks_dir.resolve(), (self.repo_path / ".git" / "hooks").resolve())

    def test_discover_custom_core_hooks_path(self):
        custom_hooks = self.repo_path / ".custom_hooks"
        custom_hooks.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "config", "core.hooksPath", ".custom_hooks"], cwd=str(self.repo_path), check=True, capture_output=True)
        hooks_dir = discover_hooks_dir(self.repo_path)
        self.assertIsNotNone(hooks_dir)
        self.assertEqual(hooks_dir.resolve(), custom_hooks.resolve())

    def test_discover_non_git_directory_returns_none(self):
        with tempfile.TemporaryDirectory() as non_git:
            res = discover_hooks_dir(Path(non_git))
            self.assertIsNone(res)

    def test_discover_nonexistent_directory_returns_none(self):
        non_existent = self.repo_path / "does_not_exist"
        res = discover_hooks_dir(non_existent)
        self.assertIsNone(res)


class TestInstallHookUnit(unittest.TestCase):
    """Testes unitários para a função install_hook."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        _init_git_repo(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_normal_install_creates_hook_with_marker(self):
        ok, msg = install_hook(self.repo_path, force=False)
        self.assertTrue(ok, f"Instalação falhou: {msg}")
        self.assertIn("instalado com sucesso", msg.lower())

        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook_file.is_file())
        content = hook_file.read_text(encoding="utf-8")
        self.assertIn(HENKE_HOOK_MARKER, content)
        self.assertIn("henke_cli.py", content)
        self.assertIn("check", content)

        # Verifica que as quebras de linha são LF
        raw_bytes = hook_file.read_bytes()
        self.assertNotIn(b"\r\n", raw_bytes, "O hook deve ter quebras de linha estritamente POSIX (LF)")

        # Verifica permissão executável (POSIX) ou permissão de escrita (Windows)
        mode = hook_file.stat().st_mode
        if not sys.platform.startswith("win"):
            self.assertTrue(bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)))
        else:
            self.assertTrue(bool(mode & stat.S_IWRITE))

    def test_idempotent_reinstall_overwrites_cleanly(self):
        ok1, _ = install_hook(self.repo_path, force=False)
        ok2, _ = install_hook(self.repo_path, force=False)
        self.assertTrue(ok1 and ok2)

        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        content = hook_file.read_text(encoding="utf-8")
        self.assertEqual(content.count(HENKE_HOOK_MARKER), 1)

    def test_legacy_backup_when_foreign_hook_exists(self):
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        foreign_script = "#!/bin/sh\n# External foreign linter\necho lint\n"
        hook_file.write_text(foreign_script, encoding="utf-8")

        ok, msg = install_hook(self.repo_path, force=False)
        self.assertTrue(ok)

        legacy_file = self.repo_path / ".git" / "hooks" / "pre-commit.legacy"
        self.assertTrue(legacy_file.is_file(), "Hook estrangeiro deve ser preservado em pre-commit.legacy")
        self.assertEqual(legacy_file.read_text(encoding="utf-8"), foreign_script)

        content = hook_file.read_text(encoding="utf-8")
        self.assertIn(HENKE_HOOK_MARKER, content)

    def test_force_overwrite_replaces_foreign_hook_directly(self):
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        foreign_script = "#!/bin/sh\n# Old script\nexit 0\n"
        hook_file.write_text(foreign_script, encoding="utf-8")

        ok, msg = install_hook(self.repo_path, force=True)
        self.assertTrue(ok)

        legacy_file = self.repo_path / ".git" / "hooks" / "pre-commit.legacy"
        self.assertFalse(legacy_file.exists(), "Com force=True, não deve criar backup .legacy")

        content = hook_file.read_text(encoding="utf-8")
        self.assertIn(HENKE_HOOK_MARKER, content)

    def test_whitespace_only_hook_replaced_without_backup(self):
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("   \n\t  \n  ", encoding="utf-8")

        ok, msg = install_hook(self.repo_path, force=False)
        self.assertTrue(ok)

        legacy_file = self.repo_path / ".git" / "hooks" / "pre-commit.legacy"
        self.assertFalse(legacy_file.exists(), "Hook vazio não deve gerar .legacy")

        content = hook_file.read_text(encoding="utf-8")
        self.assertIn(HENKE_HOOK_MARKER, content)

    def test_rejection_of_non_git_directory(self):
        with tempfile.TemporaryDirectory() as non_git:
            ok, msg = install_hook(Path(non_git), force=False)
            self.assertFalse(ok)
            self.assertIn("git", msg.lower())

    def test_worktree_hooks_resolution(self):
        with tempfile.TemporaryDirectory() as wt_dir:
            wt_path = Path(wt_dir)
            subprocess.run(["git", "worktree", "add", str(wt_path), "-b", "test-wt-branch"], cwd=str(self.repo_path), check=True, capture_output=True)
            self.assertTrue((wt_path / ".git").is_file())

            ok, msg = install_hook(wt_path)
            self.assertTrue(ok)

            common_hook = self.repo_path / ".git" / "hooks" / "pre-commit"
            self.assertTrue(common_hook.is_file())
            self.assertIn(HENKE_HOOK_MARKER, common_hook.read_text(encoding="utf-8"))


class TestInstallHookCLI(unittest.TestCase):
    """Testes de integração para a invocação de install-hook via CLI henke."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        _init_git_repo(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_install_hook_success(self):
        code = henke_cli_main(["install-hook", "--repo", str(self.repo_path)])
        self.assertEqual(code, 0)
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook_file.is_file())

    def test_cli_install_hook_with_force(self):
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("#!/bin/sh\n# custom\n", encoding="utf-8")

        code = henke_cli_main(["install-hook", "--repo", str(self.repo_path), "--force"])
        self.assertEqual(code, 0)
        legacy_file = self.repo_path / ".git" / "hooks" / "pre-commit.legacy"
        self.assertFalse(legacy_file.exists())
        self.assertIn(HENKE_HOOK_MARKER, hook_file.read_text(encoding="utf-8"))

    def test_cli_install_hook_rejects_non_git(self):
        with tempfile.TemporaryDirectory() as non_git:
            code = henke_cli_main(["install-hook", "--repo", str(non_git)])
            self.assertEqual(code, 2)


class TestLiveGitCommitExecution(unittest.TestCase):
    """Testes de execução real do Git invocando o hook em tempo de git commit."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        _init_git_repo(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_live_hook_blocks_commit_when_check_fails(self):
        # Instala o pre-commit hook nativo
        ok, msg = install_hook(self.repo_path)
        self.assertTrue(ok)

        # Cria e faz stage de um arquivo com vulnerabilidade real (SHELL-INJECTION)
        bad_file = self.repo_path / "perigo.py"
        bad_file.write_text("import os\nos.system('echo perigo ' + user_input)\n", encoding="utf-8")
        subprocess.run(["git", "add", "perigo.py"], cwd=str(self.repo_path), check=True, capture_output=True)

        # Executa git commit real
        res = subprocess.run(
            ["git", "commit", "-m", "mensagem de teste que deve falhar"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertNotEqual(res.returncode, 0, "Git commit deve falhar quando há vulnerabilidade detectada")
        combined_output = (res.stdout or "") + (res.stderr or "")
        self.assertTrue("hazard_scan" in combined_output or "SHELL-INJECTION" in combined_output)

    def test_live_hook_allows_commit_when_check_passes(self):
        # Instala o pre-commit hook nativo
        ok, msg = install_hook(self.repo_path)
        self.assertTrue(ok)

        # Cria e faz stage de um arquivo limpo
        clean_file = self.repo_path / "limpo.py"
        clean_file.write_text("print('codigo seguro e limpo')\n", encoding="utf-8")
        subprocess.run(["git", "add", "limpo.py"], cwd=str(self.repo_path), check=True, capture_output=True)

        # Executa git commit real
        res = subprocess.run(
            ["git", "commit", "-m", "mensagem de teste que deve passar"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(res.returncode, 0, f"Git commit deve passar quando não há hazards. Erro: {res.stderr}")
        combined_output = (res.stdout or "") + (res.stderr or "")
        self.assertIn("hazard_scan", combined_output)

    def test_live_hook_blocks_when_staged_index_has_hazard_even_if_working_tree_is_clean(self):
        """Verifica que o hook bloqueia commit quando o index contém hazard mesmo se o working tree foi limpo."""
        ok, msg = install_hook(self.repo_path)
        self.assertTrue(ok)

        # 1. Cria e faz stage do arquivo com hazard
        bad_file = self.repo_path / "staged_hazard.py"
        bad_file.write_text("import os\nos.system('echo injection ' + user_param)\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged_hazard.py"], cwd=str(self.repo_path), check=True, capture_output=True)

        # 2. Deleta ou limpa o arquivo do working tree para tentar enganar a checagem
        bad_file.unlink()

        # 3. Tenta commitar o index
        res = subprocess.run(
            ["git", "commit", "-m", "tentativa de bypass com working tree limpo"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertNotEqual(res.returncode, 0, "Commit com hazard no index DEVE ser bloqueado")
        combined_output = (res.stdout or "") + (res.stderr or "")
        self.assertTrue("hazard_scan" in combined_output or "FAIL" in combined_output)


if __name__ == "__main__":
    unittest.main()
