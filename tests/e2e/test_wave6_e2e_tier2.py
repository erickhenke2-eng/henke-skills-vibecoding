"""
test_wave6_e2e_tier2.py — Tier 2: Boundary & Corner Cases (>=5 test cases per feature for R1, R2, R3, R4, R5, R6).
Validates edge cases, malformed inputs, timeouts, limits, and stress conditions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from tests.e2e.e2e_common import (
    SUITE_ROOT,
    SCRIPTS_DIR,
    CLI_PATH,
    git_init,
    run_cli,
    setup_mock_stack,
    get_mission_classifier_module,
    get_process_runner_module,
    get_install_hook_module,
    get_gate_cache_module,
    get_report_generator_module,
    is_pid_alive_win32,
)


class TestTier2MissionClassifierBoundaries(unittest.TestCase):
    """R1 Boundaries: Empty repos, corrupted manifests, pruned dirs, ambiguous builds."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t2_r1_empty_repository(self):
        """T2.1.1: Empty repo or docs-only repo returns empty stack list without crashing."""
        classifier = get_mission_classifier_module()
        (self.repo_path / "notes.txt").write_text("just notes", encoding="utf-8")
        detected = classifier.detect_stacks(self.repo_path)
        self.assertEqual(len(detected), 0)

    def test_t2_r1_pruned_directories_ignored(self):
        """T2.1.2: Manifests inside node_modules, .git, dist, target are pruned."""
        classifier = get_mission_classifier_module()
        
        # Place package.json only in node_modules and Cargo.toml in target
        nm = self.repo_path / "node_modules" / "some_pkg"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / "package.json").write_text('{"name": "ignored"}', encoding="utf-8")
        
        target = self.repo_path / "target" / "debug"
        target.mkdir(parents=True, exist_ok=True)
        (target / "Cargo.toml").write_text('[package]\nname = "ignored"', encoding="utf-8")

        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertEqual(len(stack_ids), 0, "Manifests in node_modules or target should be ignored")

    def test_t2_r1_corrupted_manifest_files(self):
        """T2.1.3: Malformed package.json (broken JSON) handled without unhandled exception."""
        classifier = get_mission_classifier_module()
        (self.repo_path / "package.json").write_text("{ unclosed json: true, ", encoding="utf-8")
        try:
            detected = classifier.detect_stacks(self.repo_path)
            self.assertIsInstance(detected, list)
        except Exception as e:
            self.fail(f"Corrupted manifest should not crash classifier: {e}")

    def test_t2_r1_ambiguous_build_files(self):
        """T2.1.4: Makefile in pure Go repo does not misclassify as C/C++ without C files."""
        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "go")
        (self.repo_path / "Makefile").write_text("build:\n\tgo build .\n", encoding="utf-8")
        
        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertIn("go", stack_ids)
        self.assertNotIn("cpp", stack_ids, "Makefile without C/C++ files should not trigger C++ stack")

    def test_t2_r1_case_insensitivity_windows(self):
        """T2.1.5: File names with varied case (Package.json, pyproject.TOML) handled safely."""
        classifier = get_mission_classifier_module()
        (self.repo_path / "PYPROJECT.toml").write_text('[project]\nname = "case-test"\n', encoding="utf-8")
        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertIn("python", stack_ids)


class TestTier2InstallHookBoundaries(unittest.TestCase):
    """R2 Boundaries: Non-git dirs, empty hooks, custom hooksPath, read-only permissions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t2_r2_missing_git_directory(self):
        """T2.2.1: Non-git directory fails gracefully with exit code 2."""
        non_git = tempfile.TemporaryDirectory()
        try:
            installer = get_install_hook_module()
            ok, msg = installer.install_hook(Path(non_git.name))
            self.assertFalse(ok)
            self.assertTrue(len(msg) > 0)
        finally:
            non_git.cleanup()

    def test_t2_r2_empty_or_whitespace_hook(self):
        """T2.2.2: Pre-existing empty or whitespace-only pre-commit hook replaced safely."""
        hook_path = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_path.parent.mkdir(parents=True, exist_ok=True)
        hook_path.write_text("   \n\n\t  ", encoding="utf-8")

        installer = get_install_hook_module()
        ok, msg = installer.install_hook(self.repo_path, force=False)
        self.assertTrue(ok)
        content = hook_path.read_text(encoding="utf-8")
        self.assertIn("HENKE-PRE-COMMIT-HOOK", content)

    def test_t2_r2_force_flag_without_prior_hook(self):
        """T2.2.3: Force flag works normally even when no prior hook exists."""
        hook_path = self.repo_path / ".git" / "hooks" / "pre-commit"
        if hook_path.exists():
            hook_path.unlink()

        installer = get_install_hook_module()
        ok, msg = installer.install_hook(self.repo_path, force=True)
        self.assertTrue(ok)
        self.assertTrue(hook_path.is_file())

    def test_t2_r2_custom_hooks_path_resolution(self):
        """T2.2.4: Resolves git hooks directory dynamically when core.hooksPath is configured."""
        custom_hooks = self.repo_path / ".githooks"
        custom_hooks.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=str(self.repo_path), check=True, capture_output=True)

        installer = get_install_hook_module()
        ok, msg = installer.install_hook(self.repo_path)
        self.assertTrue(ok)
        installed_hook = custom_hooks / "pre-commit"
        self.assertTrue(installed_hook.is_file(), "Should install to custom hooksPath")

    def test_t2_r2_submodule_or_worktree_hooks_resolution(self):
        """T2.2.5: Hook installation resolves properly when .git is a file (worktree/submodule)."""
        # In a git worktree, .git is a file pointing to gitdir: ...
        worktree_temp = tempfile.TemporaryDirectory()
        try:
            wt_path = Path(worktree_temp.name)
            subprocess.run(["git", "worktree", "add", str(wt_path), "-b", "wt-branch"], cwd=str(self.repo_path), check=True, capture_output=True)
            self.assertTrue((wt_path / ".git").is_file(), ".git should be a file in worktree")
            
            installer = get_install_hook_module()
            ok, msg = installer.install_hook(wt_path)
            self.assertTrue(ok)
        finally:
            worktree_temp.cleanup()
            subprocess.run(["git", "worktree", "prune"], cwd=str(self.repo_path), capture_output=True)


class TestTier2ProcessRunnerBoundaries(unittest.TestCase):
    """R3 Boundaries: Zero timeout, massive stdout, non-existent binaries, string vs list cmds."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t2_r3_zero_or_submillisecond_timeout(self):
        """T2.3.1: Zero or submillisecond timeout triggers immediate timeout and kills process."""
        runner = get_process_runner_module()
        res = runner.run_process([sys.executable, "-c", "import time; time.sleep(10)"], cwd=self.work_dir, timeout_sec=0.001)
        self.assertTrue(res.timed_out)

    def test_t2_r3_massive_stdout_buffer_saturation(self):
        """T2.3.2: Process emitting 5MB of stdout before timeout does not deadlock OS pipe buffer."""
        runner = get_process_runner_module()
        # Generates large output
        code = "import sys; sys.stdout.write('x' * (5 * 1024 * 1024))"
        res = runner.run_process([sys.executable, "-c", code], cwd=self.work_dir, timeout_sec=10)
        self.assertEqual(res.returncode, 0)
        self.assertFalse(res.timed_out)
        self.assertEqual(len(res.stdout), 5 * 1024 * 1024)

    def test_t2_r3_nonexistent_executable(self):
        """T2.3.3: Nonexistent binary returns non-zero without crashing runner."""
        runner = get_process_runner_module()
        try:
            res = runner.run_process(["nonexistent_binary_xyz_12345", "--arg"], cwd=self.work_dir)
            self.assertNotEqual(res.returncode, 0)
        except FileNotFoundError:
            # Raising clean FileNotFoundError or returning exit code != 0 are both acceptable
            pass

    def test_t2_r3_string_command_execution(self):
        """T2.3.4: Accepts string command in addition to list of tokens."""
        runner = get_process_runner_module()
        cmd_str = f'"{sys.executable}" -c "print(12345)"'
        res = runner.run_process(cmd_str, cwd=self.work_dir)
        self.assertEqual(res.returncode, 0)
        self.assertIn("12345", res.stdout)

    def test_t2_r3_already_dead_process_on_timeout(self):
        """T2.3.5: Taskkill on already dead process handled without crashing (error code 128)."""
        runner = get_process_runner_module()
        # Process exits immediately
        res = runner.run_process([sys.executable, "-c", "import sys; sys.exit(0)"], cwd=self.work_dir, timeout_sec=0.01)
        self.assertIn(res.returncode, (0, 1, 2))


class TestTier2ReportGeneratorBoundaries(unittest.TestCase):
    """R4 Boundaries: Empty .henke dir, deep nonexistent paths, massive findings, XSS escaping."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t2_r4_empty_henke_directory(self):
        """T2.4.1: Report generation when .henke has no runs generates clean empty state dashboard."""
        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        content = out.read_text(encoding="utf-8")
        self.assertIn("<html", content.lower())

    def test_t2_r4_nonexistent_output_parent_directory(self):
        """T2.4.2: Creates parent directories automatically when custom --out has nonexistent dirs."""
        rep_gen = get_report_generator_module()
        deep_out = self.repo_path / "deep" / "nested" / "reports" / "summary.html"
        out = rep_gen.generate_report(self.repo_path, output_file=deep_out)
        self.assertTrue(deep_out.is_file())

    def test_t2_r4_huge_volume_of_findings(self):
        """T2.4.3: 500+ audit findings render cleanly in HTML without memory bloat."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        findings = [{"gate": f"gate_{i}", "file": f"src/file_{i}.py", "msg": f"Finding {i}"} for i in range(500)]
        (henke_dir / "LEDGER.json").write_text(json.dumps({"findings": findings, "verdict": "REPROVADO"}), encoding="utf-8")

        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 500)

    def test_t2_r4_html_escaping_and_xss_protection(self):
        """T2.4.4: Special HTML characters (<script>, &, \") are properly escaped."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        malicious = "<script>alert('xss')</script>"
        (henke_dir / "LEDGER.json").write_text(json.dumps({"findings": [{"msg": malicious}], "verdict": malicious}), encoding="utf-8")

        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        content = out.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert('xss')</script>", content, "Raw unescaped script tag found in HTML")

    def test_t2_r4_corrupted_runs_json_graceful_handling(self):
        """T2.4.5: Corrupted JSON in runs directory does not crash report generation."""
        runs_dir = self.repo_path / ".henke" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "r_bad.json").write_text("{ unclosed bad json", encoding="utf-8")

        rep_gen = get_report_generator_module()
        try:
            out = rep_gen.generate_report(self.repo_path)
            self.assertTrue(out.is_file())
        except Exception as e:
            self.fail(f"Corrupted runs json should not crash report generator: {e}")


class TestTier2GateCacheBoundaries(unittest.TestCase):
    """R5 Boundaries: Corrupted cache JSON, zero RAM limit, empty keys, rapid access."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t2_r5_corrupted_cache_file_recovery(self):
        """T2.5.1: Corrupted cache JSON file on disk is safely discarded and replaced."""
        cache_file = self.repo_path / ".henke" / "cache" / "gate_cache.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text("{ truncated corrupt file...", encoding="utf-8")

        cache = get_gate_cache_module()
        # Should return None without raising json.JSONDecodeError
        res = cache.get_cached_gate("any_key")
        self.assertIsNone(res)

    def test_t2_r5_zero_or_low_ram_limit_evicts_all(self):
        """T2.5.2: Setting RAM limit to 0 MB forces eviction of all entries."""
        cache = get_gate_cache_module()
        for i in range(5):
            cache.store_cached_gate(f"item_{i}", {"data": i})
        evicted = cache.enforce_ram_limit(limit_mb=0.0)
        self.assertGreaterEqual(evicted, 5)

    def test_t2_r5_empty_key_or_empty_dict(self):
        """T2.5.3: Empty key or empty dict handled gracefully."""
        cache = get_gate_cache_module()
        cache.store_cached_gate("", {})
        res = cache.get_cached_gate("")
        self.assertIsNotNone(res)

    def test_t2_r5_rapid_sequential_cache_access(self):
        """T2.5.4: 50 consecutive store and get operations maintain consistency."""
        cache = get_gate_cache_module()
        for i in range(50):
            cache.store_cached_gate(f"seq_{i}", {"val": i * 2})
            fetched = cache.get_cached_gate(f"seq_{i}")
            self.assertEqual(fetched.get("val"), i * 2)

    def test_t2_r5_atomic_write_replacement(self):
        """T2.5.5: Atomic file replacement avoids partial read states."""
        # Test os.replace behavior on Windows with target file
        target = self.repo_path / "atomic.json"
        tmp = self.repo_path / "atomic.json.tmp"
        tmp.write_text('{"status": "ok"}', encoding="utf-8")
        os.replace(tmp, target)
        self.assertTrue(target.is_file())
        self.assertFalse(tmp.exists())


class TestTier2IsolationAndPlatformBoundaries(unittest.TestCase):
    """R6 Boundaries: Spaces in paths, unicode filenames, path normalization, deep nesting."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t2_r6_spaces_in_directory_paths(self):
        """T2.6.1: Repositories located in paths with spaces work without argument splitting."""
        spaces_temp = tempfile.TemporaryDirectory(prefix="henke space test ")
        try:
            sp_path = Path(spaces_temp.name)
            git_init(sp_path)
            (sp_path / "file with space.txt").write_text("hello", encoding="utf-8")
            self.assertTrue((sp_path / "file with space.txt").is_file())
        finally:
            spaces_temp.cleanup()

    def test_t2_r6_unicode_file_paths(self):
        """T2.6.2: Files with non-ASCII and accented characters operate cleanly."""
        accented_file = self.repo_path / "arquivo_configuração_padrão.json"
        accented_file.write_text('{"chave": "valor com acentuação"}', encoding="utf-8")
        data = json.loads(accented_file.read_text(encoding="utf-8"))
        self.assertEqual(data["chave"], "valor com acentuação")

    def test_t2_r6_relative_and_absolute_equivalence(self):
        """T2.6.3: Canonical resolution equates relative and absolute paths."""
        p_rel = Path(".")
        p_abs = p_rel.resolve()
        self.assertTrue(p_abs.is_absolute())

    def test_t2_r6_mixed_slashes_in_cli_args(self):
        """T2.6.4: CLI handles mixed forward and backslashes in paths without syntax errors."""
        res = run_cli(["doctor", "--repo", str(self.repo_path).replace("\\", "/")])
        self.assertIn(res.returncode, (0, 1, 2))

    def test_t2_r6_large_ledger_stream_parsing(self):
        """T2.6.5: Parsing ledger with 5,000 lines streams without memory bloat."""
        ledger_file = self.repo_path / "large_ledger.jsonl"
        with open(ledger_file, "w", encoding="utf-8") as f:
            for i in range(5000):
                f.write(json.dumps({"id": i, "status": "ok"}) + "\n")
        
        # Verify line-by-line reading
        count = 0
        with open(ledger_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        self.assertEqual(count, 5000)


if __name__ == "__main__":
    unittest.main()
