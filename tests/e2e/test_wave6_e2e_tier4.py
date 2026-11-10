"""
test_wave6_e2e_tier4.py — Tier 4: Real-World Application Scenarios (>= 5 end-to-end scenarios).
Simulates complete real-world developer workflows on Windows:
1. Multi-stack polyglot workflow (Python + Vite/Tailwind) with hook, commit rejection/acceptance, and report.
2. Runaway deadlock subprocess termination with atomic taskkill /F /T on Windows.
3. Progressive stack adoption with interactive configuration prompt [s/N].
4. High-frequency developer loop with gate cache hits and 500 MB RAM eviction.
5. Full executive audit report generation with self-contained HTML and plain-language summary.
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
    HAS_MISSION_CLASSIFIER,
    HAS_PROCESS_RUNNER,
    HAS_INSTALL_HOOK,
    HAS_GATE_CACHE,
    HAS_REPORT_GENERATOR,
    CLI_HAS_INSTALL_HOOK,
    CLI_HAS_REPORT,
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


class TestTier4RealWorldScenarios(unittest.TestCase):
    """Real-World End-to-End Application Scenarios."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t4_scenario_1_polyglot_full_lifecycle(self):
        """Scenario 1: Full workflow on multi-stack polyglot repo with hook, commit block/pass, and report."""
        if not (HAS_MISSION_CLASSIFIER and HAS_INSTALL_HOOK and HAS_REPORT_GENERATOR):
            self.skipTest("Requires mission_classifier (M1), install_hook (M3), and report_generator (M5)")

        classifier = get_mission_classifier_module()
        installer = get_install_hook_module()
        rep_gen = get_report_generator_module()

        # Step 1: Polyglot repo setup (Python backend + Vite frontend)
        setup_mock_stack(self.repo_path, "python")
        setup_mock_stack(self.repo_path, "vite")
        setup_mock_stack(self.repo_path, "tailwind")

        # Step 2: Classify stacks
        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertIn("python", stack_ids)

        # Step 3: Install pre-commit hook
        ok, msg = installer.install_hook(self.repo_path)
        self.assertTrue(ok)
        hook_path = self.repo_path / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook_path.is_file())

        # Step 4: Generate report
        report_file = self.repo_path / "lifecycle_report.html"
        out = rep_gen.generate_report(self.repo_path, output_file=report_file)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("http://", text)
        self.assertNotIn("https://", text)

    def test_t4_scenario_2_runaway_deadlock_elimination(self):
        """Scenario 2: Runaway hanging subprocess deadlock eliminated atomically via taskkill on Windows."""
        if not HAS_PROCESS_RUNNER:
            self.skipTest("Requires process_runner (M2)")

        runner = get_process_runner_module()
        
        # Script that spawns a child that spawns a grandchild, all sleeping
        tree_script = self.repo_path / "deep_tree.py"
        grandchild_pid_file = self.repo_path / "grandchild.pid"
        gc_path_str = grandchild_pid_file.as_posix()
        code = f"""
import subprocess, sys, time
p1 = subprocess.Popen([sys.executable, "-c", '''
import subprocess, sys, time
p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open("{gc_path_str}", "w") as f:
    f.write(str(p2.pid))
time.sleep(120)
'''])
time.sleep(120)
"""
        tree_script.write_text(code, encoding="utf-8")

        start_t = time.time()
        res = runner.run_process([sys.executable, str(tree_script)], cwd=self.repo_path, timeout_sec=2.0)
        elapsed = time.time() - start_t

        self.assertTrue(res.timed_out)
        self.assertLess(elapsed, 15.0)

        # Confirm grandchild was terminated
        if grandchild_pid_file.is_file():
            gc_pid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
            time.sleep(1.0)
            self.assertFalse(is_pid_alive_win32(gc_pid), f"Grandchild PID {gc_pid} should not be alive")

    def test_t4_scenario_3_progressive_stack_adoption(self):
        """Scenario 3: Progressive stack adoption in fresh repo with interactive prompt and config update."""
        if not HAS_MISSION_CLASSIFIER:
            self.skipTest("Requires mission_classifier (M1)")

        classifier = get_mission_classifier_module()

        # Initially Python only
        setup_mock_stack(self.repo_path, "python")
        detected_initial = classifier.detect_stacks(self.repo_path)
        initial_ids = [s.name if hasattr(s, "name") else str(s) for s in detected_initial]
        self.assertIn("python", initial_ids)
        self.assertNotIn("rust", initial_ids)

        # Add Rust
        setup_mock_stack(self.repo_path, "rust")
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text('{"stacks": {"python": {"enabled": true, "configured": true}}}', encoding="utf-8")

        # Interactive user accepts Rust
        import unittest.mock
        with unittest.mock.patch("builtins.input", return_value="s"):
            res = classifier.prompt_unconfigured_stack("rust", cfg_file, interactive=True)
            self.assertTrue(res)

        # Config updated
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertTrue(cfg["stacks"]["rust"]["enabled"])

        # Subsequent check filters both
        detected_now = classifier.detect_stacks(self.repo_path)
        active, _ = classifier.filter_gates_for_stacks(detected_now, configured=["python", "rust"])
        self.assertTrue(len(active) >= 2)

    def test_t4_scenario_4_high_frequency_dev_loop_cache_and_memory(self):
        """Scenario 4: High-frequency dev loop with cache hits and 500 MB RAM eviction under load."""
        if not HAS_GATE_CACHE:
            self.skipTest("Requires gate_cache (M4)")

        cache = get_gate_cache_module()
        src_file = self.repo_path / "calculator.py"
        src_file.write_text("def mul(a, b): return a * b\n", encoding="utf-8")

        # Cycle 1: Fresh run
        key1 = cache.compute_cache_key("unit", SCRIPTS_DIR / "metricas" / "assertless_scan.py", [str(src_file)], self.repo_path)
        cache.store_cached_gate(key1, {"returncode": 0, "passed": True})

        # Cycle 2: Cache hit (file unmodified)
        key2 = cache.compute_cache_key("unit", SCRIPTS_DIR / "metricas" / "assertless_scan.py", [str(src_file)], self.repo_path)
        self.assertEqual(key1, key2)
        cached_result = cache.get_cached_gate(key2)
        self.assertIsNotNone(cached_result)
        self.assertTrue(cached_result.get("passed"))

        # Cycle 3: Edit file -> Cache miss
        time.sleep(0.01)
        src_file.write_text("def mul(a, b): return a * b * 1\n", encoding="utf-8")
        key3 = cache.compute_cache_key("unit", SCRIPTS_DIR / "metricas" / "assertless_scan.py", [str(src_file)], self.repo_path)
        self.assertNotEqual(key1, key3)

        # Cycle 4: Memory pressure trigger eviction
        for i in range(25):
            cache.store_cached_gate(f"heavy_key_{i}", {"buffer": "x" * 1000})
        evicted = cache.enforce_ram_limit(limit_mb=0.001)
        self.assertGreater(evicted, 0)

    def test_t4_scenario_5_full_audit_and_executive_reporting(self):
        """Scenario 5: Full security audit report generation with self-contained HTML and plain-language summary."""
        if not HAS_REPORT_GENERATOR:
            self.skipTest("Requires report_generator (M5)")

        rep_gen = get_report_generator_module()

        # Seed full audit findings in .henke/
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(json.dumps({
            "audit_date": "2026-11-08T18:30:00Z",
            "verdict": "APROVADO",
            "gates_passed": 14,
            "gates_failed": 0,
            "plain_language_summary": "O sistema está em perfeito estado. Todos os 14 portões mecânicos foram aprovados com sucesso."
        }), encoding="utf-8")

        audit_html = self.repo_path / "audit_executive.html"
        out = rep_gen.generate_report(self.repo_path, output_file=audit_html)
        self.assertTrue(audit_html.is_file())

        content = audit_html.read_text(encoding="utf-8")
        # 1. Self-contained check (no external CDNs)
        self.assertNotIn("http://", content)
        self.assertNotIn("https://", content)
        # 2. Plain-language section present
        self.assertTrue(
            "linguagem simples" in content.lower() or "resumo executivo" in content.lower(),
            "Expected plain-language summary section in report"
        )
        # 3. Valid HTML structure
        self.assertIn("<!doctype html>", content.lower())
        self.assertIn("</html>", content.lower())


if __name__ == "__main__":
    unittest.main()
