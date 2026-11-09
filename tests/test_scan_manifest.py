"""Testes para scripts/metricas/scan_manifest.py (Wave 7, R5).

Scanners, motor e configs resolvem SEMPRE da suíte Henke instalada — o
repo auditado é somente dados. As fixtures usam repos externos sem
`scripts/` para provar que o manifesto não depende nem executa código do
repo alvo (MUST FIX #1 da auditoria adversarial).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.metricas.scan_manifest import (
    IncrementalScanResult, ScannerSpec, run_incremental_scan, scanner_specs,
)


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestScanManifest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = Path(self._td.name)  # repo externo, sem scripts/ Henke
        self._sd = os.environ.pop("HENKE_STATE_DIR", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("HENKE_STATE_DIR", self._sd)
            if self._sd else None)
        self.specs = {s.scanner_id: s for s in scanner_specs(self.repo)}

    def test_scanners_resolve_from_suite_not_repo(self):
        """Specs apontam para a suíte; repo externo sem scripts/ é escaneado."""
        for spec in scanner_specs(self.repo):
            self.assertTrue(spec.script_path.is_file(),
                            f"scanner inexistente: {spec.script_path}")
            self.assertTrue(
                spec.script_path.resolve().is_relative_to(ROOT.resolve()),
                f"scanner fora da suíte: {spec.script_path}")
            self.assertFalse(
                spec.script_path.resolve().is_relative_to(self.repo.resolve()),
                f"scanner resolvido dentro do repo auditado: {spec.script_path}")
        _write(self.repo, "p.html", "<html><body>x</body></html>")
        r = run_incremental_scan(self.repo, self.specs["a11y"], full=True)
        self.assertIn(r.exit_code, (0, 1))
        self.assertIn("p.html", r.scanned_files)

    def test_manifest_findings_equivalent_to_script(self):
        """O adapter do manifesto produz o mesmo veredito do script real."""
        _write(self.repo, "bad.html",
               '<html><body><button aria-hidden="true">x</button>'
               "</body></html>")
        spec = self.specs["a11y"]
        r = run_incremental_scan(self.repo, spec, full=True)

        script = ROOT / "scripts" / "metricas" / "a11y_scan.py"
        proc = subprocess.run(
            [sys.executable, str(script), str(self.repo), "--json"],
            capture_output=True, text=True, timeout=60)
        data = json.loads(proc.stdout)
        script_findings = {(f["rule"], f["location"], f["message"])
                           for f in data["findings"]}
        manifest_findings = {(f["rule"], f["location"], f["message"])
                             for f in r.findings}
        self.assertTrue(script_findings, "fixture deveria gerar finding a11y")
        self.assertEqual(script_findings, manifest_findings)
        self.assertEqual(r.exit_code, proc.returncode)
        self.assertEqual(data["files_scanned"], len(r.scanned_files))

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
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        mdir = self.repo / ".henke" / "scan-manifests"
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / "a11y.json.tmp").write_text("{partial", encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertEqual(r.exit_code, 0)
        self.assertIn("page.html", r.scanned_files)

    def test_reuse_with_zero_scanned_never_passes(self):
        """2ª rodada com só reuse de scanned_count=0 → ausência de veredito.

        Regressão do MUST FIX #2: `scanned_now or reused` tratava arquivos
        reaproveitados como prova de scan — uma suíte de teste sem funções
        de teste voltava PASS na segunda rodada sem auditar nada.
        """
        spec = self.specs["assertless"]
        _write(self.repo, "tests/test_empty.py", "X = 1  # sem funções de teste\n")
        r1 = run_incremental_scan(self.repo, spec, full=True)
        self.assertEqual(r1.exit_code, 3)
        r2 = run_incremental_scan(self.repo, spec)
        self.assertFalse(r2.full_scan)
        self.assertIn("tests/test_empty.py", r2.reused_files)
        self.assertEqual(r2.exit_code, 3)

    def test_scanner_version_change_invalidates(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        # Spec injetada apontando para cópia com SCANNER_VERSION bumpado:
        # fingerprint diverge do manifesto → full rebuild.
        suite_scanner = ROOT / "scripts" / "metricas" / "a11y_scan.py"
        with tempfile.TemporaryDirectory() as ext:
            bumped = Path(ext) / "a11y_scan.py"
            bumped.write_text(
                suite_scanner.read_text(encoding="utf-8").replace(
                    'SCANNER_VERSION = "wave7.1"',
                    'SCANNER_VERSION = "wave7.999"'),
                encoding="utf-8")
            spec_bumped = ScannerSpec("a11y", "wave7.999", bumped, ())
            r = run_incremental_scan(self.repo, spec_bumped)
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
