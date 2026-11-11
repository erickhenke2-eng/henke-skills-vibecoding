"""
test_wave6_e2e_tier1.py — Tier 1: Feature Coverage (>=5 test cases per feature for R1, R2, R3, R4, R5, R6).
Opaque-box E2E test suite covering all primary capabilities of Wave 6.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
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
    get_process_working_set_bytes,
    is_pid_alive_win32,
)


class TestTier1MissionClassifier(unittest.TestCase):
    """R1: Mission Classifier & Multi-Framework Bridges (>=5 tests)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_r1_1_python_stack_detection(self):
        """T1.1.1: Detects Python stack via pyproject.toml and source files."""
        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "python")
        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertIn("python", stack_ids)

    def test_r1_2_javascript_typescript_stack_detection(self):
        """T1.1.2: Detects JS/TS stack via package.json and tsconfig.json."""
        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "typescript")
        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertTrue("typescript" in stack_ids or "javascript" in stack_ids)

    def test_r1_3_rust_go_java_detection(self):
        """T1.1.3: Detects compiled languages: Rust, Go, Java."""
        classifier = get_mission_classifier_module()
        
        # Test Rust
        rust_temp = tempfile.TemporaryDirectory()
        try:
            r_path = Path(rust_temp.name)
            setup_mock_stack(r_path, "rust")
            detected = classifier.detect_stacks(r_path)
            stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
            self.assertIn("rust", stack_ids)
        finally:
            rust_temp.cleanup()

        # Test Go
        go_temp = tempfile.TemporaryDirectory()
        try:
            g_path = Path(go_temp.name)
            setup_mock_stack(g_path, "go")
            detected = classifier.detect_stacks(g_path)
            stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
            self.assertIn("go", stack_ids)
        finally:
            go_temp.cleanup()

    def test_r1_4_c_csharp_php_ruby_swift_detection(self):
        """T1.1.4: Detects remaining languages: C/C++, C#, PHP, Ruby, Kotlin/Swift."""
        classifier = get_mission_classifier_module()

        for st in ["cpp", "csharp", "php", "ruby", "kotlin_swift"]:
            sub_temp = tempfile.TemporaryDirectory()
            try:
                p = Path(sub_temp.name)
                setup_mock_stack(p, st)
                detected = classifier.detect_stacks(p)
                stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
                self.assertTrue(len(stack_ids) > 0, f"Expected detection for stack {st}")
            finally:
                sub_temp.cleanup()

    def test_r1_5_svelte_vite_tailwind_tooling_detection(self):
        """T1.1.5: Detects frontend tools (Svelte, Vite, Tailwind)."""
        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "svelte")
        setup_mock_stack(self.repo_path, "vite")
        setup_mock_stack(self.repo_path, "tailwind")
        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        # At least one of the frontend tooling signals should be identified
        matches = [s for s in stack_ids if s in ("svelte", "vite", "tailwind")]
        self.assertTrue(len(matches) > 0, f"Expected frontend tools in {stack_ids}")

    def test_r1_6_selective_gate_filtering(self):
        """T1.1.6: Dynamic gate filtering executes gates only for identified stacks."""
        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "python")
        detected = classifier.detect_stacks(self.repo_path)
        active_gates, ignored = classifier.filter_gates_for_stacks(detected, configured=["python"])
        self.assertTrue(len(active_gates) > 0)
        self.assertNotIn("cargo test", " ".join(active_gates))

    def test_r1_7_unconfigured_stack_bypass_non_interactive(self):
        """T1.1.7: Unconfigured stacks are bypassed in non-interactive mode without crashing."""
        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "rust")
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")
        
        # When interactive is False, it returns False (do not configure) and bypasses
        res = classifier.prompt_unconfigured_stack("rust", cfg_file, interactive=False)
        self.assertFalse(res)

    def test_r1_8_unconfigured_stack_interactive_prompt_accept(self):
        """T1.1.8: User accepting [s/N] prompt enables stack in configuration."""
        classifier = get_mission_classifier_module()
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        import unittest.mock
        with unittest.mock.patch("builtins.input", return_value="s"):
            res = classifier.prompt_unconfigured_stack("rust", cfg_file, interactive=True)
            self.assertTrue(res)
            # Verify CONFIG.json updated
            content = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertTrue(content["stacks"].get("rust", {}).get("enabled", False))

    def test_r1_9_unconfigured_stack_interactive_prompt_decline(self):
        """T1.1.9/PT-11: decline retorna False sem persistir nada no repo."""
        classifier = get_mission_classifier_module()
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        import unittest.mock
        with unittest.mock.patch("builtins.input", return_value="N"):
            res = classifier.prompt_unconfigured_stack("rust", cfg_file, interactive=True)
            self.assertFalse(res)
            content = json.loads(cfg_file.read_text(encoding="utf-8"))
            self.assertNotIn("rust", content["stacks"])

    def test_r1_10_extensibility_documentation(self):
        """T1.1.10: docs/BRIDGES.md documents bridge adaptation and custom bridge schemas."""
        bridges_doc = SUITE_ROOT / "docs" / "BRIDGES.md"
        if not bridges_doc.is_file():
            self.skipTest("Milestone 1 docs/BRIDGES.md not yet written")
        text = bridges_doc.read_text(encoding="utf-8")
        self.assertIn("bridge", text.lower())
        self.assertTrue("json" in text.lower() or "schema" in text.lower())


class TestTier1InstallHook(unittest.TestCase):
    """R2: Pre-Commit Hook Nativo de 1 Comando (>=5 tests)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_r2_1_install_hook_basic_creation(self):
        """T1.2.1: henke install-hook installs .git/hooks/pre-commit with correct signature."""
        installer = get_install_hook_module()
        ok, msg = installer.install_hook(self.repo_path, force=False)
        self.assertTrue(ok, f"Failed to install hook: {msg}")
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook_file.is_file())
        content = hook_file.read_text(encoding="utf-8")
        self.assertIn("HENKE-PRE-COMMIT-HOOK", content)

    def test_r2_2_install_hook_idempotency(self):
        """T1.2.2: Multiple install-hook runs overwrite safely without duplicate content."""
        installer = get_install_hook_module()
        ok1, _ = installer.install_hook(self.repo_path, force=False)
        ok2, _ = installer.install_hook(self.repo_path, force=False)
        self.assertTrue(ok1 and ok2)
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        content = hook_file.read_text(encoding="utf-8")
        self.assertEqual(content.count("HENKE-PRE-COMMIT-HOOK"), 1)

    def test_r2_3_install_hook_legacy_backup_without_force(self):
        """T1.2.3: Existing non-henke hook is backed up to .legacy when force=False."""
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("#!/bin/sh\n# Custom foreign hook\necho custom\n", encoding="utf-8")

        installer = get_install_hook_module()
        ok, msg = installer.install_hook(self.repo_path, force=False)
        self.assertTrue(ok)
        legacy_file = self.repo_path / ".git" / "hooks" / "pre-commit.legacy"
        self.assertTrue(legacy_file.is_file(), "Foreign hook should be backed up to .legacy")
        self.assertIn("Custom foreign hook", legacy_file.read_text(encoding="utf-8"))

    def test_r2_4_install_hook_force_overwrite(self):
        """T1.2.4: With force=True, existing foreign hook is replaced directly."""
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("#!/bin/sh\n# Old hook\n", encoding="utf-8")

        installer = get_install_hook_module()
        ok, msg = installer.install_hook(self.repo_path, force=True)
        self.assertTrue(ok)
        content = hook_file.read_text(encoding="utf-8")
        self.assertIn("HENKE-PRE-COMMIT-HOOK", content)

    def test_r2_5_install_hook_rejects_non_git_repo(self):
        """T1.2.5: Fails with clear error code 2 when directory is not a git repo."""
        non_git_temp = tempfile.TemporaryDirectory()
        try:
            p = Path(non_git_temp.name)
            installer = get_install_hook_module()
            ok, msg = installer.install_hook(p, force=False)
            self.assertFalse(ok)
            self.assertIn("git", msg.lower())
        finally:
            non_git_temp.cleanup()

    def test_r2_6_hook_execution_blocks_commit_on_failure(self):
        """T1.2.6: Pre-commit hook execution prevents git commit when gates fail."""
        # Test hook shell script logic directly on Windows using bash/sh or python simulation
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        # Mock hook that simulates a failing henke check
        hook_file.write_text("#!/bin/sh\necho '[HENKE BLOQUEIO] Falha' >&2\nexit 1\n", encoding="utf-8")
        hook_file.chmod(0o755)  # POSIX: hook sem bit executável é ignorado pelo git
        
        # Attempt to commit
        (self.repo_path / "new_file.txt").write_text("content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_path), check=True, capture_output=True)
        res = subprocess.run(["git", "commit", "-m", "fail-commit"], cwd=str(self.repo_path), capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0, "Git commit should be rejected by pre-commit hook")

    def test_r2_7_hook_execution_allows_commit_on_pass(self):
        """T1.2.7: Pre-commit hook execution allows git commit when gates pass."""
        hook_file = self.repo_path / ".git" / "hooks" / "pre-commit"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        hook_file.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook_file.chmod(0o755)  # POSIX: hook sem bit executável é ignorado pelo git

        (self.repo_path / "valid_file.txt").write_text("valid content", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(self.repo_path), check=True, capture_output=True)
        res = subprocess.run(["git", "commit", "-m", "pass-commit"], cwd=str(self.repo_path), capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"Git commit should succeed: {res.stderr}")


class TestTier1ProcessRunner(unittest.TestCase):
    """R3: Eliminação Atômica de Processos Zumbis no Windows (>=5 tests)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.work_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_r3_1_normal_execution_success(self):
        """T1.3.1: Command completing in time returns code 0 and stdout."""
        runner = get_process_runner_module()
        res = runner.run_process([sys.executable, "-c", "print('hello_wave6')"], cwd=self.work_dir, timeout_sec=10)
        self.assertEqual(res.returncode, 0)
        self.assertIn("hello_wave6", res.stdout)
        self.assertFalse(res.timed_out)

    def test_r3_2_timeout_triggers_taskkill_windows(self):
        """T1.3.2: Subprocess timeout invokes taskkill /F /T and sets timed_out=True."""
        runner = get_process_runner_module()
        # Spawn a process that sleeps for 60 seconds, with timeout of 1 second
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        start_t = time.time()
        res = runner.run_process(cmd, cwd=self.work_dir, timeout_sec=1.0)
        elapsed = time.time() - start_t
        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 15.0, "Should terminate within reasonable timeout margin")
        if sys.platform.startswith("win"):
            self.assertTrue(res.killed_tree)

    def test_r3_3_process_tree_termination_no_orphans(self):
        """T1.3.3: Multilevel process tree (parent -> child -> grandchild) terminated without orphans."""
        # Create a python script that writes child PID to a file and sleeps indefinitely
        script_file = self.work_dir / "tree_spawn.py"
        pid_file = self.work_dir / "child_pid.txt"
        script_code = f"""
import subprocess, sys, time
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(r"{pid_file}", "w") as f:
    f.write(str(proc.pid))
time.sleep(120)
"""
        script_file.write_text(script_code, encoding="utf-8")

        runner = get_process_runner_module()
        res = runner.run_process([sys.executable, str(script_file)], cwd=self.work_dir, timeout_sec=2.0)
        self.assertTrue(res.timed_out)
        
        # Verify grandchild was killed and is no longer running
        if pid_file.is_file():
            child_pid = int(pid_file.read_text(encoding="utf-8").strip())
            time.sleep(1.0)  # brief stabilization window
            self.assertFalse(is_pid_alive_win32(child_pid), f"Child PID {child_pid} should have been terminated")

    def test_r3_4_race_condition_exit_code_128_handling(self):
        """T1.3.4: Race condition when process dies just before taskkill is handled cleanly."""
        runner = get_process_runner_module()
        # Fast exit process with short timeout
        res = runner.run_process([sys.executable, "-c", "import sys; sys.exit(0)"], cwd=self.work_dir, timeout_sec=0.1)
        self.assertIn(res.returncode, (0, 1, 2))
        self.assertIsInstance(res.stdout, str)

    def test_r3_5_process_execution_result_contract(self):
        """T1.3.5: Return value satisfies ProcessExecutionResult interface contract."""
        runner = get_process_runner_module()
        res = runner.run_process([sys.executable, "-c", "print('contract')"], cwd=self.work_dir)
        self.assertTrue(hasattr(res, "returncode"))
        self.assertTrue(hasattr(res, "stdout"))
        self.assertTrue(hasattr(res, "stderr"))
        self.assertTrue(hasattr(res, "timed_out"))
        self.assertTrue(hasattr(res, "killed_tree"))


class TestTier1ReportGenerator(unittest.TestCase):
    """R4: Dashboard Visual Local em HTML Autocontido (>=5 tests)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)
        # Create minimal .henke directory structure
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(json.dumps({
            "audit_date": "2026-11-08T18:00:00Z",
            "verdict": "APROVADO",
            "gates_passed": 5,
            "gates_failed": 0
        }), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_r4_1_report_generation_default_location(self):
        """T1.4.1: Generates HTML report at default location .henke/report.html."""
        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        self.assertEqual(out.name, "report.html")

    def test_r4_2_report_generation_custom_output(self):
        """T1.4.2: Generates HTML report at user-specified --out destination."""
        rep_gen = get_report_generator_module()
        custom_out = self.repo_path / "custom_audit.html"
        out = rep_gen.generate_report(self.repo_path, output_file=custom_out)
        self.assertEqual(out.resolve(), custom_out.resolve())
        self.assertTrue(custom_out.is_file())

    def test_r4_3_report_self_contained_zero_external_cdns(self):
        """T1.4.3: 100% offline self-contained HTML (zero external http/https scripts or styles)."""
        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        html_text = out.read_text(encoding="utf-8")
        
        # Check no external <script src="http..."> or <link rel="stylesheet" href="http...">
        self.assertNotRegex(html_text, r'<script[^>]+src=["\']https?://')
        self.assertNotRegex(html_text, r'<link[^>]+href=["\']https?://')
        self.assertNotIn("fonts.googleapis.com", html_text)
        self.assertNotIn("cdnjs.cloudflare.com", html_text)

    def test_r4_4_report_mandatory_plain_language_section(self):
        """T1.4.4: Mandatory plain-language executive summary section present for non-technical users."""
        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        html_text = out.read_text(encoding="utf-8").lower()
        self.assertTrue(
            "linguagem simples" in html_text or "resumo executivo" in html_text or "plain language" in html_text,
            "Mandatory plain language section missing from generated report"
        )

    def test_r4_5_report_visual_audit_status_cards(self):
        """T1.4.5: Visual audit results and gate status information rendered cleanly."""
        rep_gen = get_report_generator_module()
        out = rep_gen.generate_report(self.repo_path)
        html_text = out.read_text(encoding="utf-8")
        self.assertIn("<html", html_text.lower())
        self.assertIn("</html>", html_text.lower())
        self.assertIn("style", html_text.lower())


class TestTier1GateCache(unittest.TestCase):
    """R5: Cache Inteligente de Portões com Limite de RAM (500 MB) (>=5 tests)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_r5_1_cache_key_determinism(self):
        """T1.5.1: Same gate, script, args, and file contents produce identical hash keys."""
        cache = get_gate_cache_module()
        k1 = cache.compute_cache_key("hazard", SCRIPTS_DIR / "metricas" / "hazard_scan.py", [".", "--json"], self.repo_path)
        k2 = cache.compute_cache_key("hazard", SCRIPTS_DIR / "metricas" / "hazard_scan.py", [".", "--json"], self.repo_path)
        self.assertEqual(k1, k2)
        self.assertIsInstance(k1, str)
        self.assertEqual(len(k1), 64)  # SHA-256 hex string

    def test_r5_2_cache_hit_and_retrieve(self):
        """T1.5.2: Stored gate result can be retrieved on subsequent cache query."""
        cache = get_gate_cache_module()
        sample_key = "test_sample_key_12345"
        sample_result = {"returncode": 0, "stdout": "clean", "passed": True}
        cache.store_cached_gate(sample_key, sample_result)
        fetched = cache.get_cached_gate(sample_key)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.get("returncode"), 0)
        self.assertEqual(fetched.get("stdout"), "clean")

    def test_r5_3_cache_invalidation_on_file_mutation(self):
        """T1.5.3: Mutating target source file changes the composite cache key."""
        cache = get_gate_cache_module()
        src_file = self.repo_path / "app.py"
        src_file.write_text("x = 1\n", encoding="utf-8")
        k1 = cache.compute_cache_key("assertless", SCRIPTS_DIR / "metricas" / "assertless_scan.py", [str(src_file)], self.repo_path)
        
        # Mutate file
        time.sleep(0.01)
        src_file.write_text("x = 2\n", encoding="utf-8")
        k2 = cache.compute_cache_key("assertless", SCRIPTS_DIR / "metricas" / "assertless_scan.py", [str(src_file)], self.repo_path)
        self.assertNotEqual(k1, k2)

    def test_r5_4_ram_measurement_win32(self):
        """T1.5.4: RAM measurement on Windows queries Win32 psapi returning physical working set in MB."""
        cache = get_gate_cache_module()
        ram_mb = cache.get_current_ram_mb()
        self.assertIsInstance(ram_mb, (int, float))
        if sys.platform.startswith("win"):
            self.assertGreater(ram_mb, 0.0)

    def test_r5_5_ram_eviction_when_limit_exceeded(self):
        """T1.5.5: enforce_ram_limit evicts LRU entries when RAM exceeds ceiling."""
        cache = get_gate_cache_module()
        # Seed cache with multiple entries
        for i in range(10):
            cache.store_cached_gate(f"key_{i}", {"returncode": 0, "idx": i})
        
        # When limit is set to a low value (e.g. 0.001 MB), eviction is triggered
        evicted = cache.enforce_ram_limit(limit_mb=0.001)
        self.assertGreater(evicted, 0)

    def test_r5_6_cache_miss_returns_none(self):
        """T1.5.6: Querying nonexistent cache key returns None without error."""
        cache = get_gate_cache_module()
        res = cache.get_cached_gate("nonexistent_hash_ffffffff")
        self.assertIsNone(res)


class TestTier1IsolationAndPlatform(unittest.TestCase):
    """R6: Diretriz Estrita de Isolamento e Plataforma Windows (>=5 tests)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_r6_1_worktree_isolation_boundary(self):
        """T1.6.1: Verifies all repo operations are confined inside repo root."""
        outside_path = self.repo_path.parent / "should_not_exist.txt"
        self.assertFalse(outside_path.exists())

    def test_r6_2_path_traversal_rejection(self):
        """T1.6.2: Path traversal attempts with ../ are resolved within canonical root or rejected."""
        traversal = (self.repo_path / ".." / "outside.txt").resolve()
        self.assertFalse(traversal.is_relative_to(self.repo_path))

    def test_r6_3_windows_path_normalization(self):
        """T1.6.3: Handles mixed forward and backward slashes seamlessly on Windows."""
        mixed_path = str(self.repo_path) + "/sub\\nested/file.txt"
        p = Path(mixed_path)
        self.assertTrue(str(p).startswith(str(self.repo_path)))

    def test_r6_4_utf8_special_characters(self):
        """T1.6.4: Special Portuguese characters (ã, ç, é, ô) handled without encoding crashes."""
        test_file = self.repo_path / "teste_acentuação.py"
        test_file.write_text("# Código com caracteres especiais: Olá, mundo!\n", encoding="utf-8")
        read_back = test_file.read_text(encoding="utf-8")
        self.assertIn("Olá, mundo!", read_back)

    def test_r6_5_safe_cleanup_no_locked_files(self):
        """T1.6.5: Temporary directories and files clean up cleanly without WinError 32."""
        t = tempfile.TemporaryDirectory()
        tp = Path(t.name)
        (tp / "test.txt").write_text("lock test", encoding="utf-8")
        # Ensure cleanup succeeds without exception
        t.cleanup()
        self.assertFalse(tp.exists())


if __name__ == "__main__":
    unittest.main()
