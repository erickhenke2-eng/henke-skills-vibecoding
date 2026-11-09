"""Testes para scripts/metricas/scan_manifest.py (Wave 7, R5)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.metricas.scan_manifest import (
    IncrementalScanResult, ScannerSpec, run_incremental_scan, scanner_specs,
)

import tempfile


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _a11y_repo(root: Path) -> None:
    """Repo mínimo com engine copiada para hash de motor consistente."""
    eng = ROOT / "scripts"
    for rel in (
        "import_graph.py", "repo_signals.py", "winpath.py", "state_paths.py",
    ):
        _write(root, f"scripts/{rel}", (eng / rel).read_text(encoding="utf-8"))
    met = eng / "metricas"
    for rel in ("scan_manifest.py", "polyglot.py", "a11y_scan.py",
                "hazard_scan.py", "assertless_scan.py"):
        _write(root, f"scripts/metricas/{rel}",
               (met / rel).read_text(encoding="utf-8"))


class TestScanManifest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)
        self._sd = os.environ.pop("HENKE_STATE_DIR", None)
        self.addCleanup(lambda: os.environ.__setitem__("HENKE_STATE_DIR", self._sd) if self._sd else None)
        _a11y_repo(self.repo)
        self.specs = {s.scanner_id: s for s in scanner_specs(self.repo)}

    def test_full_then_incremental_reuse(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        r1 = run_incremental_scan(self.repo, spec, full=True)
        self.assertIn(r1.exit_code, (0, 1))
        self.assertTrue(r1.full_scan)
        r2 = run_incremental_scan(self.repo, spec)
        self.assertFalse(r2.full_scan)
        self.assertIn("page.html", r2.reused_files)
        self.assertNotIn("page.html", r2.scanned_files)

    def test_dirty_file_rescanned(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        _write(self.repo, "page.html", "<html><body>changed</body></html>")
        r = run_incremental_scan(self.repo, spec)
        self.assertIn("page.html", r.scanned_files)

    def test_corrupt_manifest_rebuilds_not_empty(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mdir = self.repo / ".henke" / "scan-manifests"
        for m in mdir.glob("*.json"):
            m.write_text("{corrupted", encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertIn("page.html", r.scanned_files)

    def test_incomplete_tmp_ignored(self):
        spec = self.specs["a11y"]
        mdir = self.repo / ".henke" / "scan-manifests"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "a11y.json.tmp").write_text("{partial", encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertEqual(r.exit_code, 0 if not r.scanned_files else r.exit_code)
        self.assertFalse(r.findings and r.exit_code == 2)

    def test_scanner_version_change_invalidates(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        # Bump real: SCANNER_VERSION do módulo do scanner muda o fingerprint.
        sp = self.repo / "scripts" / "metricas" / "a11y_scan.py"
        sp.write_text(
            sp.read_text(encoding="utf-8").replace(
                'SCANNER_VERSION = "wave7.1"', 'SCANNER_VERSION = "wave7.999"'),
            encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)

    def test_manifest_written(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = self.repo / ".henke" / "scan-manifests" / "a11y.json"
        self.assertTrue(mp.is_file())
        data = json.loads(mp.read_text(encoding="utf-8"))
        self.assertEqual(data["scanner"]["id"], "a11y")
        self.assertIn("engine_sha256", data["scanner"])
        self.assertIn("page.html", data["files"])

    def test_removed_file_dropped(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        _write(self.repo, "old.html", "<html><body>old</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        (self.repo / "old.html").unlink()
        r = run_incremental_scan(self.repo, spec)
        self.assertNotIn("old.html", r.reused_files)
        self.assertNotIn("old.html", r.scanned_files)


if __name__ == "__main__":
    unittest.main()
