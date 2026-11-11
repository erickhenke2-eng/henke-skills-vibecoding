"""Testes do detector de projeto (henke/henke_detect.py).

Cobre as fixtures da proposta M4: package.json+vitest, pyproject+pytest.ini,
requirements.txt+tests/, diretório vazio (nulls sem erro) e o roundtrip de
.henke/PROJECT.json via write_project e via CLI --write.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "henke" / "henke_detect.py"
spec = importlib.util.spec_from_file_location("henke_detect", SCRIPT)
detect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detect)


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)

    def w(self, rel, text=""):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    # (a) package.json com vitest -> javascript + `npm test`
    def test_package_json_vitest(self):
        self.w("package.json", json.dumps({
            "name": "app",
            "scripts": {"test": "vitest run", "build": "tsc -b",
                        "lint": "eslint ."},
            "devDependencies": {"vitest": "^2.0.0"},
        }))
        self.w("vitest.config.ts", "export default {}\n")
        res = detect.detect_project(str(self.repo))
        self.assertIn("javascript", res["linguagens"])
        self.assertEqual(res["test_cmd"], "npm test")
        self.assertEqual(res["build_cmd"], "npm run build")
        self.assertEqual(res["lint_cmd"], "npm run lint")
        self.assertIsNone(res["framework"])
        self.assertEqual(res["source"], ["package.json", "vitest.config.ts"])

    def test_package_manager_pnpm(self):
        self.w("package.json", json.dumps({
            "packageManager": "pnpm@9.1.0",
            "scripts": {"test": "vitest run", "build": "vite build"},
        }))
        res = detect.detect_project(str(self.repo))
        self.assertEqual(res["test_cmd"], "pnpm test")
        self.assertEqual(res["build_cmd"], "pnpm build")

    def test_vitest_config_sem_script_test(self):
        """Sem script "test", a presença de vitest.config.* resolve o runner."""
        self.w("package.json", json.dumps({"name": "app"}))
        self.w("vitest.config.ts", "export default {}\n")
        res = detect.detect_project(str(self.repo))
        self.assertIn("javascript", res["linguagens"])
        self.assertEqual(res["test_cmd"], "npx vitest run")

    # (b) pyproject.toml + pytest.ini -> `python -m pytest`
    def test_pyproject_pytest_ini(self):
        self.w("pyproject.toml", '[project]\nname = "x"\nversion = "0.1"\n')
        self.w("pytest.ini", "[pytest]\ntestpaths = tests\n")
        res = detect.detect_project(str(self.repo))
        self.assertIn("python", res["linguagens"])
        self.assertEqual(res["test_cmd"], "python -m pytest")
        self.assertEqual(res["source"], ["pyproject.toml", "pytest.ini"])

    # (c) requirements.txt + tests/ -> unittest discover
    def test_requirements_tests_unittest(self):
        self.w("requirements.txt", "requests==2.31.0\n")
        self.w("tests/test_smoke.py", "import unittest\n")
        res = detect.detect_project(str(self.repo))
        self.assertIn("python", res["linguagens"])
        self.assertEqual(res["test_cmd"],
                         "python -m unittest discover -s tests")
        self.assertEqual(res["source"], ["requirements.txt", "tests"])

    # (d) diretório vazio -> nulls sem erro
    def test_diretorio_vazio(self):
        res = detect.detect_project(str(self.repo))
        self.assertEqual(res["linguagens"], [])
        self.assertIsNone(res["framework"])
        self.assertIsNone(res["test_cmd"])
        self.assertIsNone(res["build_cmd"])
        self.assertIsNone(res["lint_cmd"])
        self.assertEqual(res["source"], [])

    # (e) write_project cria .henke/PROJECT.json com roundtrip idêntico
    def test_write_project_roundtrip(self):
        self.w("requirements.txt", "requests\n")
        self.w("tests/test_a.py", "pass\n")
        data = detect.detect_project(str(self.repo))
        path = Path(detect.write_project(str(self.repo), data))
        self.assertTrue(path.is_file())
        self.assertEqual(path,
                         self.repo.resolve() / ".henke" / "PROJECT.json")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), data)

    # ecossistemas restantes
    def test_cargo(self):
        self.w("Cargo.toml", '[package]\nname = "x"\nversion = "0.1.0"\n')
        res = detect.detect_project(str(self.repo))
        self.assertIn("rust", res["linguagens"])
        self.assertEqual(res["test_cmd"], "cargo test")
        self.assertEqual(res["build_cmd"], "cargo build")

    def test_go_mod(self):
        self.w("go.mod", "module exemplo.com/x\n\ngo 1.22\n")
        res = detect.detect_project(str(self.repo))
        self.assertIn("go", res["linguagens"])
        self.assertEqual(res["test_cmd"], "go test ./...")

    def test_makefile_test_target(self):
        self.w("Makefile", ".PHONY: test\ntest:\n\t@echo ok\n")
        res = detect.detect_project(str(self.repo))
        self.assertEqual(res["test_cmd"], "make test")
        self.assertIn("Makefile", res["source"])

    def test_root_invalido_levanta(self):
        with self.assertRaises(NotADirectoryError):
            detect.detect_project(str(self.repo / "nao-existe"))

    # CLI
    def test_cli_json_e_root_invalido(self):
        ok = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.repo)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        payload = json.loads(ok.stdout)
        self.assertEqual(payload["linguagens"], [])
        bad = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.repo / "nao-existe")],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(bad.returncode, 2)

    def test_cli_write(self):
        self.w("requirements.txt", "requests\n")
        res = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.repo), "--write"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(res.returncode, 0, res.stderr)
        out_path = self.repo.resolve() / ".henke" / "PROJECT.json"
        self.assertTrue(out_path.is_file())
        self.assertEqual(res.stdout.strip(), str(out_path))
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8")),
                         detect.detect_project(str(self.repo)))


if __name__ == "__main__":
    unittest.main()
