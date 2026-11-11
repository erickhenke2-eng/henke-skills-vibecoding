#!/usr/bin/env python3
"""Regressão P0: `check` JAMAIS retorna 0 quando um gate falha.

Caso-hostil do audit: repo contendo os.system → hazard_scan FAIL →
exit agregado obrigatoriamente != 0. Também prova o contraponto
(repo limpo → 0 ou N/A honesto) e o caminho JSON.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "henke"))

from henke.henke_cli import main as henke_cli_main  # noqa: E402


def _run_check(repo: Path, *extra: str) -> tuple[int, str]:
    buf, ebuf = StringIO(), StringIO()
    with redirect_stdout(buf), redirect_stderr(ebuf):
        rc = henke_cli_main(["check", "--repo", str(repo), "--no-cache",
                             *extra])
    return rc, buf.getvalue() + ebuf.getvalue()


class TestCheckAggregation(unittest.TestCase):
    def test_hostile_repo_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="hostile_check_") as td:
            repo = Path(td)
            (repo / "evil.py").write_text('import os\nos.system("id")\n')
            rc, out = _run_check(repo)
            self.assertNotEqual(rc, 0,
                                f"check aprovou repo com os.system:\n{out}")
            self.assertIn("FAIL", out)

    def test_hostile_repo_json_not_passed(self):
        with tempfile.TemporaryDirectory(prefix="hostile_check_") as td:
            repo = Path(td)
            (repo / "evil.py").write_text('import os\nos.system("id")\n')
            buf = StringIO()
            with redirect_stdout(buf), redirect_stderr(StringIO()):
                rc = henke_cli_main(["check", "--repo", str(repo),
                                     "--no-cache", "--json"])
            self.assertNotEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertFalse(data["passed"])
            hz = [g for g in data["gates"] if g["gate"] == "hazard_scan"]
            self.assertTrue(hz and not hz[0]["passed"])

    def test_clean_repo_passes_or_na(self):
        with tempfile.TemporaryDirectory(prefix="clean_check_") as td:
            repo = Path(td)
            (repo / "app.py").write_text("x = 1\n")
            rc, out = _run_check(repo)
            self.assertEqual(rc, 0, f"check reprovou repo limpo:\n{out}")

    def test_check_state_dir_redirects_and_restores_env(self):
        import os
        with tempfile.TemporaryDirectory(prefix="state_dir_repo_") as td_repo, \
             tempfile.TemporaryDirectory(prefix="state_dir_ext_") as td_ext:
            repo = Path(td_repo)
            ext = Path(td_ext)
            (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
            prev = os.environ.get("HENKE_STATE_DIR")
            rc, out = _run_check(repo, "--state-dir", str(ext))
            self.assertEqual(rc, 0, f"check --state-dir falhou:\n{out}")
            self.assertEqual(os.environ.get("HENKE_STATE_DIR"), prev)
            self.assertFalse((repo / ".henke").exists(),
                             "check --state-dir sujou o repo auditado com .henke/")
            self.assertTrue((ext / "events.jsonl").is_file(),
                            "events.jsonl deveria ter sido gravado em --state-dir")

    def test_check_spec_standalone_and_tampered_sidecar_fails_closed(self):
        import subprocess
        from henke.run_state import init_run
        with tempfile.TemporaryDirectory(prefix="spec_check_") as td:
            repo = Path(td)
            subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=str(repo), check=True)
            subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
            (repo / "src").mkdir()
            (repo / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
            (repo / "docs" / "designs").mkdir(parents=True)
            spec_p = repo / "docs" / "designs" / "feat.md"
            spec_p.write_text(
                "# Spec\n\n## 4. Escopo Declarado\n\n- `src/app.py`\n\n## 7. Contratos\n\nNenhum.\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(repo), check=True)

            # 1) Standalone check --spec num repo sem .henke/ deve passar e não sujar .henke/
            rc, out = _run_check(repo, "--spec", "docs/designs/feat.md")
            self.assertEqual(rc, 0, f"standalone check --spec falhou:\n{out}")
            self.assertFalse((repo / ".henke").exists(),
                             "standalone check --spec criou .henke/ sem init")

            # 2) Run iniciada ancora spec_sha256 no sidecar fora do repo;
            #    adulterar a spec e deletar o run file ainda deve reprovar (fail-closed).
            run_file = init_run(
                repo,
                "r-spec",
                mode="EXECUTAR",
                archetype="CARPINTEIRO",
                spec_path="docs/designs/feat.md",
            )
            spec_p.write_text(
                "# Spec\n\n## 4. Escopo Declarado\n\n- `src/app.py`\n- `src/evil.py`\n\n## 7. Contratos\n\nNenhum.\n",
                encoding="utf-8",
            )
            run_file.unlink()
            rc_tampered, out_tampered = _run_check(repo, "--spec", "docs/designs/feat.md")
            self.assertNotEqual(
                rc_tampered, 0,
                f"check --spec aprovou spec adulterada após deleção do run file:\n{out_tampered}",
            )


if __name__ == "__main__":
    unittest.main()
