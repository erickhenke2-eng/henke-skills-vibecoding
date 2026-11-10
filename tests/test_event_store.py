#!/usr/bin/env python3
"""P1-03: event store v2 — append encadeado, verificação, migração, views."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import scripts.event_store as es  # noqa: E402


class TestAppendAndChain(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="evstore_")
        self.repo = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def test_append_chains_and_verifies(self):
        e1 = es.append_event(self.repo, "exec", {"cmd": "pytest", "exit": 0})
        e2 = es.append_event(self.repo, "gate_result", {"key": "k", "gate": "h"})
        self.assertEqual(e2["seq"], e1["seq"] + 1)
        self.assertEqual(e2["prev_hash"], e1["entry_hash"])
        self.assertEqual(es.verify(self.repo), [])

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            es.append_event(self.repo, "bogus", {})

    def test_tamper_detected(self):
        es.append_event(self.repo, "exec", {"a": 1})
        es.append_event(self.repo, "exec", {"a": 2})
        p = es.events_path(self.repo)
        lines = p.read_text(encoding="utf-8").splitlines()
        obj = json.loads(lines[0])
        obj["payload"] = {"forjado": True}
        lines[0] = json.dumps(obj, ensure_ascii=False)
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        errs = es.verify(self.repo)
        self.assertTrue(errs, "adulteração passou na verificação")

    def test_tail_truncation_detected_by_tip(self):
        es.append_event(self.repo, "exec", {"a": 1})
        es.append_event(self.repo, "exec", {"a": 2})
        p = es.events_path(self.repo)
        lines = p.read_text(encoding="utf-8").splitlines()
        p.write_text(lines[0] + "\n", encoding="utf-8")  # corta o rabo
        errs = es.verify(self.repo)
        self.assertTrue(any("âncora" in e for e in errs),
                        f"truncamento passou: {errs}")


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="evmig_")
        self.repo = Path(self.td.name)
        (self.repo / ".henke" / "cache" / "scans").mkdir(parents=True)

    def tearDown(self):
        self.td.cleanup()

    def test_migrate_imports_legacy_stores(self):
        (self.repo / ".henke" / "exec_log.jsonl").write_text(
            json.dumps({"cmd": "pytest", "exit": 0,
                        "testes_descobertos": 5, "entry_hash": "x"}) + "\n",
            encoding="utf-8")
        (self.repo / ".henke" / "cache" / "gate_cache.json").write_text(
            json.dumps({"entries": {"k1": {"key": "k1", "gate": "h",
                                          "exit_code": 0}}}),
            encoding="utf-8")
        (self.repo / ".henke" / "cache" / "scans" / "hazard.json").write_text(
            json.dumps({"scanner": "hazard", "exit": 0}), encoding="utf-8")
        stats = es.migrate(self.repo)
        self.assertEqual(stats["exec"], 1)
        self.assertEqual(stats["gate_result"], 1)
        self.assertEqual(stats["scan_result"], 1)
        self.assertEqual(es.verify(self.repo), [])
        # Idempotente
        stats2 = es.migrate(self.repo)
        self.assertTrue(stats2["skipped"])
        self.assertEqual(stats2["exec"], 0)

    def test_rebuild_derived_regenerates_ledger(self):
        (self.repo / ".henke" / "exec_log.jsonl").write_text(
            json.dumps({"cmd": "python -m pytest", "exit": 0,
                        "testes_descobertos": 7, "testes_passados": 7,
                        "stdout_sha256": "s", "entry_hash": "h"}) + "\n",
            encoding="utf-8")
        es.migrate(self.repo)
        out = es.rebuild_derived(self.repo)
        self.assertEqual(out["ledger_entries"], 1)
        ledger = json.loads(
            (self.repo / ".henke" / "LEDGER.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger[0]["testes_descobertos"], 7)


if __name__ == "__main__":
    unittest.main()
