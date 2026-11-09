"""Testes do faxineiro com quarentena (`scripts/henke_clean.py`).

Contrato: dry-run sem mutações, quarentena transacional com manifesto e hash,
restore exigente (destino ausente + hash fiel), purga só após expiração com
ID exato — nunca automática nem em massa.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from scripts.henke_clean import (
    CleanCandidate,
    plan_clean,
    purge_expired_batch,
    quarantine,
    restore_batch,
    staging_leftovers,
)


def _utcnow():
    return datetime.now(timezone.utc)


class TestPlanClean(unittest.TestCase):
    """Dry-run: enumera alvos allowlist e nunca muta."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name)

    def test_finds_cache_dirs_inside_repo(self):
        (self.repo / "pkg" / "__pycache__").mkdir(parents=True)
        (self.repo / "pkg" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"x" * 10)
        (self.repo / ".pytest_cache").mkdir()
        (self.repo / "src" / "main.py").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "src" / "main.py").write_text("x=1", encoding="utf-8")

        candidates = plan_clean(self.repo)
        kinds = {c.kind for c in candidates}
        sources = {c.source.name for c in candidates}
        self.assertEqual(kinds, {"repo-cache"})
        self.assertIn("__pycache__", sources)
        self.assertIn(".pytest_cache", sources)
        self.assertTrue(all(c.size_bytes >= 0 for c in candidates))
        # dry-run não muta nada
        self.assertTrue((self.repo / "pkg" / "__pycache__").exists())

    def test_dry_run_ignores_outside_allowlist(self):
        (self.repo / "node_modules" / "pkg").mkdir(parents=True)
        (self.repo / "dist").mkdir()
        (self.repo / "docs").mkdir()
        candidates = plan_clean(self.repo)
        self.assertEqual(candidates, ())

    def test_includes_henke_cache(self):
        (self.repo / ".henke" / "cache").mkdir(parents=True)
        candidates = plan_clean(self.repo)
        self.assertTrue(any(c.source.name == "cache" for c in candidates))


class TestQuarantine(unittest.TestCase):
    """Movimentação transacional com manifesto e hash agregado."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name)

    def _seed_cache(self) -> Path:
        cache = self.repo / "pkg" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "a.pyc").write_bytes(b"deadbeef")
        (cache / "b.pyc").write_bytes(b"feedface")
        return cache

    def test_quarantine_moves_and_records_manifest(self):
        cache = self._seed_cache()
        cand = CleanCandidate(source=cache, kind="repo-cache", size_bytes=16)
        batch = quarantine(self.repo, [cand])
        self.assertFalse(cache.exists())
        batch_dir = self.repo / ".henke" / "quarantine" / batch.batch_id
        self.assertTrue((batch_dir / "manifest.json").is_file())
        manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["items"]), 1)
        item = manifest["items"][0]
        self.assertEqual(item["original_path"], str(cache.resolve()))
        self.assertEqual(len(item["sha256"]), 64)
        moved = batch_dir / "payload" / item["dest"]
        self.assertTrue((moved / "a.pyc").is_file())

    def test_quarantine_rejects_outside_source(self):
        outside = Path(self.temp_dir.name).parent / f"outside_{os.getpid()}"
        outside.mkdir(exist_ok=True)
        try:
            cand = CleanCandidate(source=outside, kind="repo-cache", size_bytes=0)
            batch = quarantine(self.repo, [cand])
            self.assertEqual(batch.items, ())
            self.assertTrue(outside.exists())
        finally:
            import shutil
            shutil.rmtree(outside, ignore_errors=True)

    def test_retention_days_must_be_positive(self):
        with self.assertRaises(ValueError):
            quarantine(self.repo, [], retention_days=0)


class TestRestoreAndPurge(unittest.TestCase):
    """Restore exigente e purga somente após expiração."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repo = Path(self.temp_dir.name)
        cache = self.repo / "mod" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "m.pyc").write_bytes(b"payload")
        self.cache = cache
        self.batch = quarantine(self.repo, [CleanCandidate(cache, "repo-cache", 7)])

    def test_restore_recreates_original_with_same_hash(self):
        self.assertFalse(self.cache.exists())
        n = restore_batch(self.repo, self.batch.batch_id)
        self.assertEqual(n, 1)
        self.assertTrue((self.cache / "m.pyc").is_file())
        self.assertEqual((self.cache / "m.pyc").read_bytes(), b"payload")

    def test_restore_refuses_when_dest_exists(self):
        self.cache.mkdir(parents=True)
        (self.cache / "conflito.pyc").write_bytes(b"novo")
        n = restore_batch(self.repo, self.batch.batch_id)
        self.assertEqual(n, 0)
        self.assertTrue((self.cache / "conflito.pyc").exists())

    def test_purge_before_expiry_rejected(self):
        with self.assertRaises(ValueError):
            purge_expired_batch(self.repo, self.batch.batch_id, now=_utcnow())
        batch_dir = self.repo / ".henke" / "quarantine" / self.batch.batch_id
        self.assertTrue(batch_dir.exists())

    def test_purge_after_expiry_removes_batch(self):
        future = _utcnow() + timedelta(days=8)
        n = purge_expired_batch(self.repo, self.batch.batch_id, now=future)
        self.assertEqual(n, 1)
        batch_dir = self.repo / ".henke" / "quarantine" / self.batch.batch_id
        self.assertFalse(batch_dir.exists())

    def test_purge_rejects_traversal_batch_id(self):
        future = _utcnow() + timedelta(days=8)
        with self.assertRaises(ValueError):
            purge_expired_batch(self.repo, "..", now=future)
        with self.assertRaises(ValueError):
            purge_expired_batch(self.repo, "../..", now=future)


class TestStagingAndLogging(unittest.TestCase):
    def test_staging_leftovers_reported_not_purged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            staging = repo / ".henke" / "quarantine" / "batchx" / "staging" / "leftover"
            staging.mkdir(parents=True)
            (staging / "f.txt").write_text("x")
            leftovers = staging_leftovers(repo)
            self.assertIn(staging, leftovers)

    def test_clean_log_written(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            cache = repo / "__pycache__"
            cache.mkdir()
            (cache / "a.pyc").write_bytes(b"x")
            quarantine(repo, [CleanCandidate(cache, "repo-cache", 1)])
            log = repo / ".henke" / "clean_log.jsonl"
            self.assertTrue(log.is_file())
            lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertTrue(any(e["action"] == "quarantine_batch" for e in lines))


if __name__ == "__main__":
    unittest.main()
