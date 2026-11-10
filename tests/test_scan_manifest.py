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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.state_paths import cache_root
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
        # DS-5: o segredo mora no perfil — isola em tempdir para nunca
        # escrever chaves de teste no perfil real do usuário.
        self._secret_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._secret_td.cleanup)
        self._prev_secret_dir = os.environ.get("HENKE_SECRET_DIR")
        os.environ["HENKE_SECRET_DIR"] = self._secret_td.name
        def _restore_secret_dir():
            if self._prev_secret_dir is None:
                os.environ.pop("HENKE_SECRET_DIR", None)
            else:
                os.environ["HENKE_SECRET_DIR"] = self._prev_secret_dir
        self.addCleanup(_restore_secret_dir)
        # PT-11: manifestos vivem em cache_root (perfil). Isola também o
        # cache — testes nunca tocam o perfil real do usuário.
        self._cache_td = tempfile.TemporaryDirectory()
        self.addCleanup(self._cache_td.cleanup)
        self._prev_cache_dir = os.environ.get("HENKE_CACHE_DIR")
        os.environ["HENKE_CACHE_DIR"] = str(
            Path(self._cache_td.name) / "cache")
        def _restore_cache_dir():
            if self._prev_cache_dir is None:
                os.environ.pop("HENKE_CACHE_DIR", None)
            else:
                os.environ["HENKE_CACHE_DIR"] = self._prev_cache_dir
        self.addCleanup(_restore_cache_dir)
        self.specs = {s.scanner_id: s for s in scanner_specs()}

    def test_scanners_resolve_from_suite_not_repo(self):
        """Specs apontam para a suíte; repo externo sem scripts/ é escaneado."""
        for spec in scanner_specs():
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
        mdir = cache_root(self.repo) / "scan-manifests"
        for m in mdir.glob("*.json"):
            m.write_text("{corrupted", encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertIn("page.html", r.scanned_files)

    def test_incomplete_tmp_ignored(self):
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        mdir = cache_root(self.repo) / "scan-manifests"
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
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
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

    def test_manifest_written_with_hmac(self):
        """Manifesto persistido carrega assinatura HMAC-SHA256 na raiz."""
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        data = json.loads(mp.read_text(encoding="utf-8"))
        self.assertIsInstance(data.get("hmac"), str)
        self.assertEqual(len(data["hmac"]), 64)

    def test_tampered_manifest_forces_rescan(self):
        """PoC do auditor: payload adulterado (findings zerados) é rejeitado.

        Sem HMAC, zerar findings no JSON transformava FAIL em PASS sem
        escanear nada. Agora o manifesto adulterado é descartado e a rodada
        faz re-scan completo com razão hmac_invalid.
        """
        _write(self.repo, "bad.html",
               '<html><body><button aria-hidden="true">x</button>'
               "</body></html>")
        spec = self.specs["a11y"]
        r1 = run_incremental_scan(self.repo, spec, full=True)
        self.assertEqual(r1.exit_code, 1)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        data = json.loads(mp.read_text(encoding="utf-8"))
        for entry in data["files"].values():
            entry["findings"] = []
        mp.write_text(json.dumps(data), encoding="utf-8")
        r2 = run_incremental_scan(self.repo, spec)
        self.assertTrue(r2.full_scan)
        self.assertTrue(any("hmac_invalid" in x for x in r2.reasons),
                        f"reasons={r2.reasons}")
        self.assertIn("bad.html", r2.scanned_files)
        self.assertTrue(r2.findings, "findings reais reaparecem no re-scan")
        self.assertEqual(r2.exit_code, 1)

    def test_missing_hmac_rejected(self):
        """Manifesto sem campo hmac (ex.: versão antiga) é descartado."""
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        data = json.loads(mp.read_text(encoding="utf-8"))
        del data["hmac"]
        mp.write_text(json.dumps(data), encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertTrue(any("hmac_invalid" in x for x in r.reasons),
                        f"reasons={r.reasons}")

    def test_planted_repo_secret_never_trusts_manifest(self):
        """DS-5: `.cache_secret` plantado no repo é ignorado — a chave real
        vive no perfil; manifesto assinado com a chave plantada falha HMAC."""
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        r1 = run_incremental_scan(self.repo, spec, full=True)
        self.assertEqual(r1.exit_code, 0)
        # O repo não pode conter a chave real — só o perfil do usuário.
        self.assertFalse((self.repo / ".henke" / ".cache_secret").exists())
        # Um tarball hostil entrega chave + manifesto assinado com ela:
        # o verificador usa a chave do perfil → hmac_invalid → full scan.
        from scripts.metricas.scan_manifest import _manifest_hmac
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        data = json.loads(mp.read_text(encoding="utf-8"))
        data["hmac"] = _manifest_hmac(b"attacker-planted-key", data)
        mp.write_text(json.dumps(data), encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertTrue(any("hmac_invalid" in x for x in r.reasons),
                        f"reasons={r.reasons}")
        # E mesmo após forja detectada, rodada seguinte reusa o manifesto
        # legítimo reescrito com a chave real do perfil.
        r3 = run_incremental_scan(self.repo, spec)
        self.assertEqual(r3.exit_code, 0)

    def test_mid_scan_mutation_discards_entry_and_fails(self):
        """PoC TOCTOU: arquivo alterado entre o snapshot e o fim do scan.

        Sem a re-verificação pós-scan, o manifesto gravaria o hash de uma
        versão com os findings de outra — falso-verde permanente. A entrada
        corrompida é descartada, a rodada falha fechada (exit 2) e o
        manifesto anterior permanece byte a byte intacto.
        """
        _write(self.repo, "page.html", "<html><body>v1</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        before = mp.read_bytes()

        _write(self.repo, "page.html", "<html><body>v2</body></html>")

        import scripts.metricas.scan_manifest as sm
        real_scan_one = sm._scan_one

        def racing(repo_root, spec_, path, rel, mod, **kw):
            out = real_scan_one(repo_root, spec_, path, rel, mod, **kw)
            path.write_text("<html><body>v3</body></html>",
                            encoding="utf-8")
            return out

        with patch.object(sm, "_scan_one", side_effect=racing):
            r = run_incremental_scan(self.repo, spec)
        self.assertEqual(r.exit_code, 2)
        self.assertTrue(any(x.startswith("race:page.html") for x in r.reasons),
                        f"reasons={r.reasons}")
        self.assertEqual(mp.read_bytes(), before,
                         "manifesto não pode gravar par (hash, findings) corrompido")

    def test_non_utf8_manifest_rebuilds_with_corrupted_reason(self):
        """Bytes não-UTF-8 → corrupted_file + full rebuild, nunca crash."""
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        mp.write_bytes(b'\xff\xfe{"schema_version": 1')
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertTrue(any("corrupted_file" in x for x in r.reasons),
                        f"reasons={r.reasons}")
        self.assertIn("page.html", r.scanned_files)

    def test_non_dict_manifest_is_corrupted_file(self):
        """JSON válido sem forma de manifesto → corrupted_file."""
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        mp.write_text("[1, 2, 3]", encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertTrue(any("corrupted_file" in x for x in r.reasons),
                        f"reasons={r.reasons}")

    def test_non_ascii_hmac_rejected_not_crashed(self):
        """hmac não-ASCII → hmac_invalid (sem TypeError em compare_digest)."""
        _write(self.repo, "page.html", "<html><body>ok</body></html>")
        spec = self.specs["a11y"]
        run_incremental_scan(self.repo, spec, full=True)
        mp = cache_root(self.repo) / "scan-manifests" / "a11y.json"
        data = json.loads(mp.read_text(encoding="utf-8"))
        data["hmac"] = "áéíóú" * 13
        mp.write_text(json.dumps(data), encoding="utf-8")
        r = run_incremental_scan(self.repo, spec)
        self.assertTrue(r.full_scan)
        self.assertTrue(any("hmac_invalid" in x for x in r.reasons),
                        f"reasons={r.reasons}")

    def test_hmac_predicates_return_exact_booleans(self):
        """Predicados retornam bool/tupla exatos — nunca None disfarçado."""
        from scripts.metricas.scan_manifest import (
            _load_manifest, _manifest_hmac_ok, _secret_is_tracked,
        )
        outside = Path(self._td.name).parent / f"outside_{os.getpid()}"
        self.assertIs(_secret_is_tracked(self.repo, outside), False)
        with patch("scripts.metricas.scan_manifest.subprocess.run",
                   side_effect=OSError("sem git")):
            self.assertIs(_secret_is_tracked(
                self.repo, self.repo / ".henke" / ".cache_secret"), False)
        self.assertIs(_manifest_hmac_ok(None, {"hmac": "x" * 64}), False)
        self.assertIs(_manifest_hmac_ok(b"k", {}), False)
        self.assertIs(_manifest_hmac_ok(b"k", {"hmac": ""}), False)
        self.assertIs(_manifest_hmac_ok(b"k", {"hmac": 123}), False)
        spec = self.specs["a11y"]
        mdir = cache_root(self.repo) / "scan-manifests"
        mdir.mkdir(parents=True)
        (mdir / "a11y.json").write_text(
            json.dumps({"schema_version": "old"}), encoding="utf-8")
        self.assertEqual(_load_manifest(self.repo, spec, b"k"),
                         (None, "corrupted_file"))


if __name__ == "__main__":
    unittest.main()
