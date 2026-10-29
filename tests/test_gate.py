"""Testes do portão mecânico de custódia (scripts/henke_gate.py).

Cobre o caminho permitido (baseline do usuário preservado + mutações só no
escopo) e o proibido (mutação inexplicada, commit não autorizado, histórico
reescrito, sujeira do usuário absorvida, branch violada).
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("henke_gate", ROOT / "scripts" / "henke_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

GIT_ENV = dict(
    os.environ,
    GIT_AUTHOR_NAME="gate-test", GIT_AUTHOR_EMAIL="gate@test",
    GIT_COMMITTER_NAME="gate-test", GIT_COMMITTER_EMAIL="gate@test",
)


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, env=GIT_ENV,
    )
    assert result.returncode == 0, f"git {' '.join(args)}: {result.stderr}"
    return result


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        git(self.repo, "init", "-b", "main")
        (self.repo / "main.py").write_text("print(1)\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "init")

    def commit_all(self, message="task"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", message)

    def test_clean_task_within_scope_approved(self):
        """Caminho permitido: baseline limpo + arquivos novos dentro do escopo."""
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "novo.py").write_text("x = 1\n")
        (self.repo / "main.py").write_text("print(2)\n")
        violations, _ = gate.check_baseline(baseline, self.repo, ["scripts/*", "main.py"], False)
        self.assertEqual(violations, [])

    def test_mutation_outside_scope_blocked(self):
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "main.py").write_text("print(2)\n")
        violations, _ = gate.check_baseline(baseline, self.repo, ["scripts/*"], False)
        self.assertTrue(any("fora do escopo" in v for v in violations), violations)

    def test_untracked_outside_scope_blocked(self):
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "stray.txt").write_text("x")
        violations, _ = gate.check_baseline(baseline, self.repo, ["scripts/*"], False)
        self.assertTrue(any("stray.txt" in v for v in violations), violations)

    def test_preexisting_dirt_preserved_passes(self):
        (self.repo / "main.py").write_text("print(2)\n")  # sujeira do usuário
        (self.repo / "draft.md").write_text("rascunho\n")  # untracked do usuário
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "novo.py").write_text("x = 1\n")
        violations, _ = gate.check_baseline(baseline, self.repo, ["scripts/*"], False)
        self.assertEqual(violations, [])

    def test_user_dirt_modified_by_task_blocked(self):
        (self.repo / "main.py").write_text("print(2)\n")
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "main.py").write_text("print(3)\n")  # agente alterou a sujeira
        violations, _ = gate.check_baseline(baseline, self.repo, [], False)
        self.assertTrue(any("conteúdo alterado" in v for v in violations), violations)

    def test_user_untracked_deleted_blocked(self):
        (self.repo / "nota.md").write_text("rascunho do usuário\n")
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "nota.md").unlink()
        violations, _ = gate.check_baseline(baseline, self.repo, [], False)
        self.assertTrue(any("absorvida ou removida" in v for v in violations), violations)

    def test_commit_without_allow_blocked(self):
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "feat.py").write_text("x = 1\n")
        self.commit_all()
        violations, _ = gate.check_baseline(baseline, self.repo, ["feat.py"], False)
        self.assertTrue(any("--allow-commit" in v for v in violations), violations)

    def test_commit_in_scope_allowed(self):
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "feat.py").write_text("x = 1\n")
        self.commit_all()
        violations, _ = gate.check_baseline(baseline, self.repo, ["feat.py"], True)
        self.assertEqual(violations, [])

    def test_commit_outside_scope_blocked(self):
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "main.py").write_text("print(2)\n")
        self.commit_all()
        violations, _ = gate.check_baseline(baseline, self.repo, ["feat.py"], True)
        self.assertTrue(any("fora do escopo" in v for v in violations), violations)

    def test_user_dirt_absorbed_by_commit_blocked(self):
        (self.repo / "main.py").write_text("print(2)\n")  # sujeira do usuário
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "feat.py").write_text("x = 1\n")
        self.commit_all()  # commit do agente engoliu a sujeira
        violations, _ = gate.check_baseline(baseline, self.repo, ["feat.py"], True)
        self.assertTrue(any("absorvida" in v for v in violations), violations)

    def test_history_rewrite_blocked(self):
        baseline = gate.capture_baseline(self.repo)
        (self.repo / "main.py").write_text("print(2)\n")
        git(self.repo, "add", "main.py")
        git(self.repo, "commit", "--amend", "-m", "rewritten")
        violations, _ = gate.check_baseline(baseline, self.repo, ["main.py"], True)
        self.assertTrue(any("Histórico reescrito" in v or "ancestral" in v for v in violations), violations)

    def test_branch_switch_blocked(self):
        baseline = gate.capture_baseline(self.repo)
        git(self.repo, "checkout", "-b", "outra")
        violations, _ = gate.check_baseline(baseline, self.repo, [], False)
        self.assertTrue(any("Branch diverge" in v for v in violations), violations)

    def test_baseline_file_itself_not_a_mutation(self):
        baseline_path = self.repo / ".henke" / "gate-baseline.json"
        baseline = gate.capture_baseline(self.repo)
        top = Path(baseline["toplevel"]).resolve()
        baseline["_self_rel"] = baseline_path.resolve().relative_to(top).as_posix()
        baseline_path.parent.mkdir(parents=True)
        import json
        baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
        violations, _ = gate.check_baseline(baseline, self.repo, [], False)
        self.assertEqual(violations, [])

    def test_cli_exit_codes(self):
        """--help funciona; baseline+check via CLI retornam 0/1 nos caminhos certos."""
        script = ROOT / "scripts" / "henke_gate.py"
        help_res = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(help_res.returncode, 0)
        out = self.repo / ".gate.json"
        res = subprocess.run(
            [sys.executable, str(script), "baseline", "--out", str(out)],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "ok.py").write_text("x = 1\n")
        ok = subprocess.run(
            [sys.executable, str(script), "check", "--baseline", str(out), "--scope", "scripts/*"],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        (self.repo / "violacao.txt").write_text("fora\n")
        bad = subprocess.run(
            [sys.executable, str(script), "check", "--baseline", str(out), "--scope", "scripts/*"],
            cwd=self.repo, capture_output=True, text=True,
        )
        self.assertEqual(bad.returncode, 1, bad.stdout + bad.stderr)


if __name__ == "__main__":
    unittest.main()
