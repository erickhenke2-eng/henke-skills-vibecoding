"""Integration checks against temporary destinations; no real user installation."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install_skills.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
TARGETS = (".agents/skills", ".gemini/config/skills", ".gemini/skills", ".claude/skills")


def snapshot(root):
    return {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.user = Path(self.temp.name)
        for name in TARGETS:
            target = self.user / name
            (target / "henke-skills").mkdir(parents=True)
            (target / "PROTOCOLO.md").write_text("old protocol")
            (target / "henke-skills/SKILL.md").write_text("old skill")
            (target / "unrelated.txt").write_text("preserve")

    def test_platform_wrapper_installs_valid_package(self):
        if sys.platform == "win32":
            command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "install.ps1"), "-UserRoot", str(self.user)]
        else:
            command = ["bash", str(ROOT / "install.sh"), "--user-root", str(self.user)]
        result = subprocess.run(command, capture_output=True, text=True, errors="replace")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in TARGETS:
            target = self.user / name
            self.assertEqual((target / "PROTOCOLO.md").read_bytes(), (ROOT / "PROTOCOLO.md").read_bytes())
            for skill in ROOT.glob("henke-*/SKILL.md"):
                self.assertEqual((target / skill.relative_to(ROOT)).read_bytes(), skill.read_bytes())
            self.assertEqual((target / "unrelated.txt").read_text(), "preserve")
            self.assertFalse(list(target.glob(".henke-install-*")))

    def test_failure_after_partial_publish_restores_all_targets(self):
        before = snapshot(self.user)
        replace = os.replace
        calls = 0
        def fail_once(source, destination):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("simulated disk failure")
            return replace(source, destination)
        with patch.object(installer.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "simulated"):
                installer.install(ROOT, self.user)
        self.assertGreater(calls, 4)  # Recovery ran after the failed write.
        self.assertEqual(snapshot(self.user), before)
        self.assertFalse(list(self.user.rglob(".henke-install-*")))

    def test_collision_does_not_replace_protocol(self):
        (self.user / TARGETS[-1] / "henke-arch-banco").write_text("collision")
        before = snapshot(self.user)
        with self.assertRaises(OSError):
            installer.install(ROOT, self.user)
        self.assertEqual(snapshot(self.user), before)

    def test_missing_dependency_has_actionable_error(self):
        result = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/validate_skills.py")], capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pip install -r requirements.txt", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_bootstrap_example_matches_preference_contract(self):
        import json
        import re
        text = (ROOT / "henke-prepara/SKILL.md").read_text(encoding="utf-8")
        config = json.loads(re.search(r"```json\s*(.*?)```", text, re.S).group(1))
        for skill in ("caveman", "ponytail"):
            self.assertIn(config["economia_tokens"][skill], ("auto", "on", "off"))
            self.assertNotIn(skill + "_ativo", config["economia_tokens"])
        for name in ("henke-skills", "henke-debug-sanidade"):
            self.assertNotIn(".henke/ESTADO.json", (ROOT / name / "SKILL.md").read_text(encoding="utf-8"))

    def test_dry_run_leaves_missing_destination_absent(self):
        target = self.user / "new-user"
        installer.install(ROOT, target, dry_run=True)
        self.assertFalse(target.exists())

    def test_manifest_written_and_zombies_removed(self):
        """F4: segunda instalação remove arquivos do manifesto anterior fora do inventário."""
        installer.install(ROOT, self.user)
        for name in TARGETS:
            target = self.user / name
            manifest_path = target / ".henke-manifest.json"
            self.assertTrue(manifest_path.is_file(), f"manifesto ausente em {name}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertIn("PROTOCOLO.md", manifest["files"])
            self.assertIn("henke-skills/SKILL.md", manifest["files"])
            # Simula skill renomeada: zumbi registrado no manifesto anterior.
            zombie_dir = target / "henke-zumbi"
            zombie_dir.mkdir()
            (zombie_dir / "SKILL.md").write_text("skill removida nesta versão")
            (zombie_dir / "references").mkdir()
            (zombie_dir / "references" / "velho.md").write_text("zumbi")
            manifest["files"] += ["henke-zumbi/SKILL.md", "henke-zumbi/references/velho.md"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        installer.install(ROOT, self.user)
        expected = {rel.as_posix() for rel in installer.inventory(ROOT)}
        for name in TARGETS:
            target = self.user / name
            self.assertFalse((target / "henke-zumbi").exists(), f"zumbi sobreviveu em {name}")
            self.assertEqual((target / "unrelated.txt").read_text(), "preserve")
            final = json.loads((target / ".henke-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(final["files"]), expected)

    def test_subdirectories_installed_and_caches_skipped(self):
        """F3: pacote completo copia references/ e ignora __pycache__/.git."""
        repo_copy = self.user / "repo-src"
        shutil.copytree(ROOT, repo_copy, ignore=shutil.ignore_patterns(".git", ".henke", "__pycache__", "tests"))
        (repo_copy / "henke-blindagem" / "references").mkdir()
        (repo_copy / "henke-blindagem" / "references" / "guia.md").write_text("ref")
        (repo_copy / "henke-blindagem" / "agents").mkdir()
        aux = repo_copy / "henke-blindagem" / "agents" / "aux.sh"
        aux.write_text("#!/bin/sh\nexit 0\n")
        if os.name == "posix":
            aux.chmod(0o755)
        (repo_copy / "henke-blindagem" / "__pycache__").mkdir()
        (repo_copy / "henke-blindagem" / "__pycache__" / "lixo.pyc").write_text("cache")
        dest_user = self.user / "dest"
        installer.install(repo_copy, dest_user)
        target = dest_user / TARGETS[0]
        self.assertEqual((target / "henke-blindagem" / "references" / "guia.md").read_text(), "ref")
        self.assertTrue((target / "henke-blindagem" / "agents" / "aux.sh").is_file())
        self.assertFalse((target / "henke-blindagem" / "__pycache__").exists())
        manifest = json.loads((target / ".henke-manifest.json").read_text(encoding="utf-8"))
        self.assertIn("henke-blindagem/references/guia.md", manifest["files"])
        self.assertNotIn("henke-blindagem/__pycache__/lixo.pyc", manifest["files"])
        if os.name == "posix":
            self.assertTrue(os.access(target / "henke-blindagem" / "agents" / "aux.sh", os.X_OK))

    def test_manifest_traversal_entry_rejected(self):
        """F4: entrada '../' no manifesto não deleta nada fora do destino."""
        installer.install(ROOT, self.user)
        target = self.user / TARGETS[0]
        protected = self.user / ".agents" / "protegido.txt"
        protected.write_text("não tocar")
        manifest_path = target / ".henke-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"].append("../protegido.txt")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            installer.install(ROOT, self.user)
        self.assertEqual(protected.read_text(), "não tocar")


if __name__ == "__main__":
    unittest.main()
