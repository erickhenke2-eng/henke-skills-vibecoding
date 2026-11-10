"""Testes do faxineiro com quarentena (`scripts/henke_clean.py`).

Contrato: dry-run sem mutações, quarentena transacional com manifesto e hash,
restore exigente (destino ausente + hash fiel), purga só após expiração com
ID exato — nunca automática nem em massa.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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

    def test_failed_second_rename_preserves_item(self):
        """Falha persistente na 2ª perna: origem restaurada, staging retido."""
        cache = self._seed_cache()
        cand = CleanCandidate(source=cache, kind="repo-cache", size_bytes=16)
        real_replace = os.replace

        def flaky(src, dst):
            if "payload" in str(dst):
                raise OSError("segunda perna forçada")
            return real_replace(src, dst)

        with patch("scripts.henke_clean.os.replace", side_effect=flaky):
            batch = quarantine(self.repo, [cand])

        # Nada movido; a origem voltou intacta e o staging guarda a cópia.
        self.assertEqual(batch.items, ())
        self.assertTrue((cache / "a.pyc").is_file())
        self.assertEqual((cache / "a.pyc").read_bytes(), b"deadbeef")
        self.assertTrue(staging_leftovers(self.repo),
                        "cópia em staging deve sobreviver para recuperação")

    def test_transient_second_rename_recovers_via_copy(self):
        """Falha única na 2ª perna: rollback + fallback copia e conclui."""
        cache = self._seed_cache()
        cand = CleanCandidate(source=cache, kind="repo-cache", size_bytes=16)
        real_replace = os.replace
        state = {"failed": False}

        def flaky(src, dst):
            if "payload" in str(dst) and not state["failed"]:
                state["failed"] = True
                raise OSError("primeira tentativa forçada")
            return real_replace(src, dst)

        with patch("scripts.henke_clean.os.replace", side_effect=flaky):
            batch = quarantine(self.repo, [cand])

        self.assertEqual(len(batch.items), 1)
        self.assertFalse(cache.exists())
        payload = (self.repo / ".henke" / "quarantine" / batch.batch_id
                   / "payload" / batch.items[0]["dest"])
        self.assertTrue((payload / "a.pyc").is_file())
        self.assertEqual((payload / "a.pyc").read_bytes(), b"deadbeef")

    def test_origin_race_preserves_staged_and_source(self):
        """Corrida na origem: staged NUNCA é apagado — os dois lados ficam.

        Se a 2ª perna falha e a origem reaparece com conteúdo novo (outro
        processo correndo), apagar o staged destruiria a única cópia do
        item movido. O correto é falhar preservando staged + src.
        """
        cache = self._seed_cache()
        cand = CleanCandidate(source=cache, kind="repo-cache", size_bytes=16)
        real_replace = os.replace

        def flaky(src, dst):
            if "payload" in str(dst):
                # Corrida: recria a origem com conteúdo novo antes de falhar.
                cache.mkdir(parents=True, exist_ok=True)
                (cache / "novo.pyc").write_bytes(b"racer")
                raise OSError("segunda perna com origem recriada")
            return real_replace(src, dst)

        with patch("scripts.henke_clean.os.replace", side_effect=flaky):
            batch = quarantine(self.repo, [cand])

        self.assertEqual(batch.items, ())
        self.assertTrue((cache / "novo.pyc").is_file(),
                        "origem concorrente deve permanecer intacta")
        leftovers = staging_leftovers(self.repo)
        self.assertTrue(leftovers, "staged do item movido não pode ser apagado")
        staged_item = leftovers[0]
        self.assertEqual((staged_item / "a.pyc").read_bytes(), b"deadbeef")

    def test_cross_volume_src_removal_is_fail_safe(self):
        """Fallback cross-volume: origem trancada não derruba o item movido.

        A remoção final de src é best-effort (ignore_errors=True): um arquivo
        bloqueado não pode virar falha depois do payload já estar íntegro —
        mutante `ignore_errors=False` propaga a OSError e perde o item.
        """
        cache = self._seed_cache()
        cand = CleanCandidate(source=cache, kind="repo-cache", size_bytes=16)
        real_replace = os.replace
        real_rmtree = shutil.rmtree
        state = {"failed": False}

        def flaky(src_, dst):
            if "payload" in str(dst) and not state["failed"]:
                state["failed"] = True
                raise OSError("segunda perna forçada (cross-volume)")
            return real_replace(src_, dst)

        def honored(path, *a, **kw):
            if not kw.get("ignore_errors", False):
                raise OSError("arquivo bloqueado por outro processo")
            return real_rmtree(path, *a, **kw)

        with patch("scripts.henke_clean.os.replace", side_effect=flaky), \
             patch("scripts.henke_clean.shutil.rmtree", side_effect=honored):
            batch = quarantine(self.repo, [cand])

        self.assertEqual(len(batch.items), 1)
        payload = (self.repo / ".henke" / "quarantine" / batch.batch_id
                   / "payload" / batch.items[0]["dest"])
        self.assertTrue((payload / "a.pyc").is_file())
        self.assertEqual((payload / "a.pyc").read_bytes(), b"deadbeef")

    def test_post_move_hash_divergence_logged_as_hash_mismatch(self):
        """Divergência pós-move registra result='hash_mismatch' no log.

        O item movido EXISTE no payload — fica rastreado no manifesto com
        verified=False e o hash real do conteúdo (não o esperado), para que
        purge/restore nunca o tratem como órfão ou íntegro.
        """
        cache = self._seed_cache()
        cand = CleanCandidate(source=cache, kind="repo-cache", size_bytes=16)
        with patch("scripts.henke_clean._tree_sha256",
                   side_effect=["a" * 64, "b" * 64, "b" * 64]):
            batch = quarantine(self.repo, [cand])
        self.assertEqual(len(batch.items), 1)
        item = batch.items[0]
        self.assertIs(item["verified"], False)
        self.assertEqual(item["sha256"], "b" * 64,
                         "sha256 deve registrar o hash REAL do payload")
        self.assertEqual(item["expected_sha256"], "a" * 64)
        self.assertEqual(item["allowed_root"], "repo",
                         "repo-cache deve registrar allowed_root='repo'")
        log = self.repo / ".henke" / "clean_log.jsonl"
        lines = [json.loads(l) for l in
                 log.read_text(encoding="utf-8").splitlines() if l.strip()]
        item_logs = [e for e in lines if e["action"] == "quarantine_item"]
        self.assertTrue(any(e["result"] == "hash_mismatch" for e in item_logs),
                        f"log sem hash_mismatch: {item_logs}")


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

    def test_purge_refuses_nonempty_staging(self):
        """Staging com leftover bloqueia a purga do batch inteiro."""
        batch_dir = (self.repo / ".henke" / "quarantine" / self.batch.batch_id)
        leftover = batch_dir / "staging" / "em_transito"
        leftover.mkdir(parents=True)
        (leftover / "x.pyc").write_bytes(b"keep")
        future = _utcnow() + timedelta(days=8)
        with self.assertRaises(ValueError):
            purge_expired_batch(self.repo, self.batch.batch_id, now=future)
        self.assertTrue(batch_dir.exists(),
                        "batch não pode ser removido com staging não vazio")

    def test_purge_refuses_unregistered_payload(self):
        """Item em payload/ fora do manifesto bloqueia a purga (nunca apaga
        conteúdo não rastreado em massa)."""
        batch_dir = (self.repo / ".henke" / "quarantine" / self.batch.batch_id)
        stray = batch_dir / "payload" / "9999-stray"
        stray.mkdir()
        (stray / "x.pyc").write_bytes(b"orphan")
        future = _utcnow() + timedelta(days=8)
        with self.assertRaises(ValueError):
            purge_expired_batch(self.repo, self.batch.batch_id, now=future)
        self.assertTrue(batch_dir.exists(),
                        "batch não pode ser removido com payload não registrado")

    def test_restore_skips_unverified_item(self):
        """Item com verified=False nunca é restaurado como se fosse íntegro."""
        batch_dir = (self.repo / ".henke" / "quarantine" / self.batch.batch_id)
        manifest_path = batch_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["items"][0]["verified"] = False
        manifest["items"][0]["sha256"] = "f" * 64
        # Banca-A 3: manifesto é assinado — adultera só o item e re-assina
        # com a chave da instalação para exercitar o caminho verified=False.
        from scripts.henke_clean import _manifest_hmac
        manifest["hmac"] = _manifest_hmac(self.repo, manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        n = restore_batch(self.repo, self.batch.batch_id)
        self.assertEqual(n, 0)
        self.assertFalse(self.cache.exists(),
                         "item não verificado não pode voltar à origem")

    def test_restore_refuses_tampered_manifest(self):
        """Banca-A 3: manifesto adulterado sem re-assinatura é recusado."""
        batch_dir = (self.repo / ".henke" / "quarantine" / self.batch.batch_id)
        manifest_path = batch_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["items"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ValueError):
            restore_batch(self.repo, self.batch.batch_id)


class TestStagingAndLogging(unittest.TestCase):
    def test_staging_leftovers_reported_not_purged(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            staging = repo / ".henke" / "quarantine" / "batchx" / "staging" / "leftover"
            staging.mkdir(parents=True)
            (staging / "f.txt").write_text("x")
            leftovers = staging_leftovers(repo)
            # CI Windows: TEMP do runner usa short-name 8.3 (RUNNER~1) —
            # staging_leftovers resolve o path; comparar pelo resolvido.
            self.assertIn(staging.resolve(), leftovers)

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
