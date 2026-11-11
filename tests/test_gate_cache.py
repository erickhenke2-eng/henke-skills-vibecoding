"""
test_gate_cache.py — Comprehensive Unit & Integration Tests for Smart Gate Cache (R5).
Covers:
  - Merkle cache key determinism and invalidation on file, argument, or script mutation.
  - Cache hit, miss, and metadata lifecycle tracking.
  - Zero-file repository handling.
  - Win32 GetProcessMemoryInfo measurement and EmptyWorkingSet trimming.
  - 500 MB RAM limit LRU eviction and forced flushes.
  - Disk persistence and corrupted cache file recovery.
  - Multi-threaded concurrency safety.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from henke.gate_cache import (
    compute_cache_key,
    get_cached_gate,
    store_cached_gate,
    get_current_ram_mb,
    trim_working_set,
    enforce_ram_limit,
    clear_cache,
)


class TestGateCacheUnit(unittest.TestCase):
    """Unit tests for gate cache hashing, storage, RAM monitoring, and eviction."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)
        clear_cache(self.repo_path)

    def tearDown(self):
        clear_cache(self.repo_path)
        self.temp_dir.cleanup()

    def test_compute_cache_key_determinism(self):
        """Same gate, script, arguments, and repo files produce identical 64-char key."""
        dummy_script = self.repo_path / "dummy_gate.py"
        dummy_script.write_text("print('gate')\n", encoding="utf-8")
        target_file = self.repo_path / "main.py"
        target_file.write_text("def run(): pass\n", encoding="utf-8")

        k1 = compute_cache_key("dummy", dummy_script, [str(target_file)], self.repo_path)
        k2 = compute_cache_key("dummy", dummy_script, [str(target_file)], self.repo_path)
        self.assertEqual(k1, k2)
        self.assertEqual(len(k1), 64)

    def test_compute_cache_key_invalidation_on_file_edit(self):
        """Mutating the target source file modifies the resulting cache key."""
        dummy_script = self.repo_path / "gate.py"
        dummy_script.write_text("print('ok')\n", encoding="utf-8")
        src = self.repo_path / "source.py"
        src.write_text("a = 1\n", encoding="utf-8")

        k1 = compute_cache_key("gate", dummy_script, [str(src)], self.repo_path)
        time.sleep(0.01)
        src.write_text("a = 2\n", encoding="utf-8")
        k2 = compute_cache_key("gate", dummy_script, [str(src)], self.repo_path)
        self.assertNotEqual(k1, k2)

    def test_compute_cache_key_invalidation_on_arg_change(self):
        """Changing command-line arguments invalidates the cache key."""
        dummy_script = self.repo_path / "gate.py"
        dummy_script.write_text("print('ok')\n", encoding="utf-8")
        src = self.repo_path / "source.py"
        src.write_text("a = 1\n", encoding="utf-8")

        k1 = compute_cache_key("gate", dummy_script, [str(src), "--strict"], self.repo_path)
        k2 = compute_cache_key("gate", dummy_script, [str(src), "--loose"], self.repo_path)
        self.assertNotEqual(k1, k2)

    def test_compute_cache_key_invalidation_on_script_change(self):
        """Modifying the gate script itself invalidates the cache key."""
        dummy_script = self.repo_path / "gate.py"
        dummy_script.write_text("print('v1')\n", encoding="utf-8")
        src = self.repo_path / "source.py"
        src.write_text("a = 1\n", encoding="utf-8")

        k1 = compute_cache_key("gate", dummy_script, [str(src)], self.repo_path)
        time.sleep(0.01)
        dummy_script.write_text("print('v2')\n", encoding="utf-8")
        k2 = compute_cache_key("gate", dummy_script, [str(src)], self.repo_path)
        self.assertNotEqual(k1, k2)

    def test_zero_file_repository_handling(self):
        """Empty repository generates deterministic valid SHA-256 key without errors."""
        empty_dir = self.repo_path / "empty_repo"
        empty_dir.mkdir(parents=True, exist_ok=True)
        dummy_script = self.repo_path / "dummy.py"
        dummy_script.write_text("pass\n", encoding="utf-8")

        k = compute_cache_key("empty_check", dummy_script, [], empty_dir)
        self.assertIsInstance(k, str)
        self.assertEqual(len(k), 64)

    def test_cache_store_and_hit(self):
        """Stored gate verdict is retrieved with updated last_accessed timestamp and access_count."""
        key = "valid_hash_key_123"
        payload = {"returncode": 0, "stdout": "All clear", "passed": True}
        store_cached_gate(key, payload, self.repo_path)

        res = get_cached_gate(key, self.repo_path)
        self.assertIsNotNone(res)
        self.assertEqual(res.get("returncode"), 0)
        self.assertEqual(res.get("stdout"), "All clear")
        self.assertTrue(res.get("passed"))
        self.assertIn("last_accessed", res)
        self.assertGreaterEqual(res.get("access_count", 0), 1)

    def test_cache_miss_returns_none(self):
        """Querying nonexistent cache key returns None."""
        self.assertIsNone(get_cached_gate("nonexistent_key_999", self.repo_path))

    def test_empty_key_and_custom_fields(self):
        """Empty string key and custom payload fields are supported gracefully."""
        store_cached_gate("", {"custom": 42}, self.repo_path)
        res = get_cached_gate("", self.repo_path)
        self.assertIsNotNone(res)
        self.assertEqual(res.get("custom"), 42)

    def test_corrupted_disk_cache_recovery(self):
        """Corrupted JSON files in disk cache are safely discarded and treated as cache miss."""
        cache_dir = self.repo_path / ".henke" / "cache"
        gates_dir = cache_dir / "gates"
        gates_dir.mkdir(parents=True, exist_ok=True)

        bad_gate = gates_dir / "bad_key.json"
        bad_gate.write_text("{ truncated json...", encoding="utf-8")

        bad_cons = cache_dir / "gate_cache.json"
        bad_cons.write_text("invalid json format", encoding="utf-8")

        res = get_cached_gate("bad_key", self.repo_path)
        self.assertIsNone(res)

    def test_get_current_ram_mb_win32(self):
        """Working set RAM measurement returns positive float value on Windows."""
        ram_mb = get_current_ram_mb()
        self.assertIsInstance(ram_mb, float)
        if sys.platform.startswith("win"):
            self.assertGreater(ram_mb, 0.0)

    def test_trim_working_set_win32(self):
        """EmptyWorkingSet call executes without exception on Windows."""
        res = trim_working_set()
        if sys.platform.startswith("win"):
            self.assertIsInstance(res, bool)

    def test_enforce_ram_limit_below_ceiling(self):
        """enforce_ram_limit does not evict when current RAM is well below limit."""
        for i in range(5):
            store_cached_gate(f"item_{i}", {"val": i}, self.repo_path)

        evicted = enforce_ram_limit(limit_mb=500.0, repo_root=self.repo_path)
        self.assertEqual(evicted, 0)
        self.assertIsNotNone(get_cached_gate("item_0", self.repo_path))

    def test_enforce_ram_limit_zero_evicts_all(self):
        """enforce_ram_limit with limit_mb=0.0 evicts all cached entries."""
        for i in range(10):
            store_cached_gate(f"evict_test_{i}", {"val": i}, self.repo_path)

        evicted = enforce_ram_limit(limit_mb=0.0, repo_root=self.repo_path)
        self.assertGreaterEqual(evicted, 10)

    def test_enforce_ram_limit_force_flush(self):
        """force_flush=True forces LRU eviction regardless of limit_mb."""
        for i in range(8):
            store_cached_gate(f"flush_{i}", {"val": i}, self.repo_path)

        evicted = enforce_ram_limit(limit_mb=1000.0, force_flush=True, repo_root=self.repo_path)
        self.assertGreater(evicted, 0)

    def test_clear_cache(self):
        """clear_cache removes both in-memory entries and files on disk."""
        store_cached_gate("to_clear", {"ok": True}, self.repo_path)
        self.assertIsNotNone(get_cached_gate("to_clear", self.repo_path))

        count = clear_cache(self.repo_path)
        self.assertGreaterEqual(count, 1)
        self.assertIsNone(get_cached_gate("to_clear", self.repo_path))

    def test_concurrency_thread_safety(self):
        """Concurrent reads, writes, and evictions across multiple threads remain consistent."""
        def worker(idx: int):
            for step in range(20):
                k = f"thread_{idx}_step_{step}"
                store_cached_gate(k, {"idx": idx, "step": step}, self.repo_path)
                item = get_cached_gate(k, self.repo_path)
                if item is not None:
                    assert item.get("step") == step

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(worker, i) for i in range(6)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        evicted = enforce_ram_limit(limit_mb=0.0, repo_root=self.repo_path)
        self.assertGreater(evicted, 0)

    def test_cache_rejects_forged_unauthenticated_entry(self):
        """Entradas gravadas manualmente sem assinatura HMAC válida são sumariamente rejeitadas."""
        key = "forged_probe_key"
        cache_dir = self.repo_path / ".henke" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        gate_file = cache_dir / "gates" / f"{key}.json"
        gate_file.parent.mkdir(parents=True, exist_ok=True)
        # Gravado por um atacante sem o segredo criptográfico
        gate_file.write_text(json.dumps({"key": key, "exit_code": 0, "passed": True}), encoding="utf-8")

        res = get_cached_gate(key, repo_root=self.repo_path)
        self.assertIsNone(res, "Entrada sem HMAC válido deve ser rejeitada para impedir falso-verde")

    def test_git_state_change_invalidates_key(self):
        """Alterações no working tree ou git status mudam a chave de cache deterministicamente."""
        subprocess.run(["git", "init"], cwd=str(self.repo_path), check=True, capture_output=True)
        gate_script = self.repo_path / "gate.py"
        gate_script.write_text("print(1)\n", encoding="utf-8")

        k1 = compute_cache_key("test_gate", gate_script, [], self.repo_path)
        # Cria um novo arquivo no repositório Git
        (self.repo_path / "new_file.txt").write_text("modified", encoding="utf-8")
        k2 = compute_cache_key("test_gate", gate_script, [], self.repo_path)

        self.assertNotEqual(k1, k2, "A chave deve mudar quando o status do Git muda")


if __name__ == "__main__":
    unittest.main()
