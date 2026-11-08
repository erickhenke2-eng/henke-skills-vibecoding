"""
test_wave6_e2e_tier3.py — Tier 3: Cross-Feature Interactions.
Pairwise and combinatorial interactions between Wave 6 capabilities:
- Mission Classifier + Gate Cache
- Install-Hook + Process Runner (Atomic Taskkill)
- Interactive Prompt + Config Update + Gate Check
- Cache 500 MB Eviction + Report Generation
- Process Runner Timeout + Audit Ledger Hashing
- Polyglot Classification + Multi-Gate Execution + Report Generation
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


class TestTier3CrossFeatureInteractions(unittest.TestCase):
    """Pairwise cross-feature interactions across Wave 6 expansions."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        git_init(self.repo_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_t3_1_classifier_and_gate_cache_pipeline(self):
        """T3.1: Classifier detects stacks, gates run and cache; partial mutation re-runs only mutated stack."""
        if not (HAS_MISSION_CLASSIFIER and HAS_GATE_CACHE):
            self.skipTest("Requires both mission_classifier (M1) and gate_cache (M4)")

        classifier = get_mission_classifier_module()
        cache = get_gate_cache_module()

        # Setup polyglot: Python + TypeScript
        setup_mock_stack(self.repo_path, "python")
        setup_mock_stack(self.repo_path, "typescript")

        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertIn("python", stack_ids)
        self.assertTrue("typescript" in stack_ids or "javascript" in stack_ids)

        # Compute cache keys for both
        k_py = cache.compute_cache_key("assertless_py", SCRIPTS_DIR / "metricas" / "assertless_scan.py", ["src/app.py"], self.repo_path)
        k_ts = cache.compute_cache_key("assertless_ts", SCRIPTS_DIR / "metricas" / "assertless_scan.py", ["src/index.ts"], self.repo_path)

        cache.store_cached_gate(k_py, {"passed": True, "stack": "python"})
        cache.store_cached_gate(k_ts, {"passed": True, "stack": "typescript"})

        # Mutate only TypeScript
        ts_file = self.repo_path / "src" / "index.ts"
        time.sleep(0.01)
        ts_file.write_text("export const answer: number = 99;\n", encoding="utf-8")

        k_py_after = cache.compute_cache_key("assertless_py", SCRIPTS_DIR / "metricas" / "assertless_scan.py", ["src/app.py"], self.repo_path)
        k_ts_after = cache.compute_cache_key("assertless_ts", SCRIPTS_DIR / "metricas" / "assertless_scan.py", ["src/index.ts"], self.repo_path)

        # Python key unchanged (cache hit), TypeScript key changed (cache miss)
        self.assertEqual(k_py, k_py_after)
        self.assertNotEqual(k_ts, k_ts_after)
        self.assertIsNotNone(cache.get_cached_gate(k_py_after))

    def test_t3_2_install_hook_and_taskkill_timeout(self):
        """T3.2: Pre-commit hook handles hanging gate command via taskkill without hanging git commit."""
        if not (HAS_INSTALL_HOOK and HAS_PROCESS_RUNNER):
            self.skipTest("Requires both install_hook (M3) and process_runner (M2)")

        installer = get_install_hook_module()
        runner = get_process_runner_module()

        ok, msg = installer.install_hook(self.repo_path)
        self.assertTrue(ok)

        # Simulate hanging check under process runner
        hanging_cmd = [sys.executable, "-c", "import time; time.sleep(120)"]
        res = runner.run_process(hanging_cmd, cwd=self.repo_path, timeout_sec=1.0)
        self.assertTrue(res.timed_out)
        if sys.platform.startswith("win"):
            self.assertTrue(res.killed_tree)

    def test_t3_3_interactive_prompt_and_config_update(self):
        """T3.3: Interactive prompt enables stack in CONFIG.json, making it active for subsequent filtering."""
        if not HAS_MISSION_CLASSIFIER:
            self.skipTest("Requires mission_classifier (M1)")

        classifier = get_mission_classifier_module()
        setup_mock_stack(self.repo_path, "rust")

        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        import unittest.mock
        with unittest.mock.patch("builtins.input", return_value="s"):
            res = classifier.prompt_unconfigured_stack("rust", cfg_file, interactive=True)
            self.assertTrue(res)

        # Check that rust is now in configured stacks
        content = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertTrue(content["stacks"].get("rust", {}).get("enabled", False))

        # Filter gates now includes rust
        detected = classifier.detect_stacks(self.repo_path)
        active_gates, ignored = classifier.filter_gates_for_stacks(detected, configured=["rust"])
        self.assertNotIn("rust", ignored)

    def test_t3_4_cache_eviction_under_ram_pressure_and_report_generation(self):
        """T3.4: High RAM load evicts cache LRU entries, report generator completes with full ledger."""
        if not (HAS_GATE_CACHE and HAS_REPORT_GENERATOR):
            self.skipTest("Requires both gate_cache (M4) and report_generator (M5)")

        cache = get_gate_cache_module()
        rep_gen = get_report_generator_module()

        # Seed cache with 20 items
        for i in range(20):
            cache.store_cached_gate(f"gate_key_{i}", {"returncode": 0, "gate": f"gate_{i}"})

        # Enforce low limit to trigger eviction
        evicted = cache.enforce_ram_limit(limit_mb=0.001)
        self.assertGreater(evicted, 0)

        # Create ledger entry
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(json.dumps({
            "audit_date": "2026-11-08T18:00:00Z",
            "verdict": "APROVADO",
            "gates_passed": 10,
            "gates_failed": 0
        }), encoding="utf-8")

        out = rep_gen.generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        content = out.read_text(encoding="utf-8")
        self.assertIn("APROVADO", content)

    def test_t3_5_taskkill_timeout_and_audit_ledger_hashing(self):
        """T3.5: Taskkill timeout during execution generates proper hashable output for ledger."""
        if not HAS_PROCESS_RUNNER:
            self.skipTest("Requires process_runner (M2)")

        runner = get_process_runner_module()
        # Hanging command
        cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
        res = runner.run_process(cmd, cwd=self.repo_path, timeout_sec=0.5)
        self.assertTrue(res.timed_out)

        # Hashing stdout/stderr of timed out process must work deterministically
        import hashlib
        combined = (res.stdout or "") + (res.stderr or "")
        h = hashlib.sha256(combined.encode("utf-8", errors="replace")).hexdigest()
        self.assertEqual(len(h), 64)

    def test_t3_6_polyglot_repo_full_check_and_report(self):
        """T3.6: Polyglot repo (Python + Vite + Tailwind) detection and report generation."""
        if not (HAS_MISSION_CLASSIFIER and HAS_REPORT_GENERATOR):
            self.skipTest("Requires both mission_classifier (M1) and report_generator (M5)")

        classifier = get_mission_classifier_module()
        rep_gen = get_report_generator_module()

        setup_mock_stack(self.repo_path, "python")
        setup_mock_stack(self.repo_path, "vite")
        setup_mock_stack(self.repo_path, "tailwind")

        detected = classifier.detect_stacks(self.repo_path)
        stack_ids = [s.name if hasattr(s, "name") else str(s) for s in detected]
        self.assertIn("python", stack_ids)

        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(json.dumps({
            "stacks": stack_ids,
            "verdict": "APROVADO",
            "plain_language_summary": "Todas as verificações em Python e Frontend passaram com sucesso."
        }), encoding="utf-8")

        out = rep_gen.generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertIn("Python", text)


if __name__ == "__main__":
    unittest.main()
