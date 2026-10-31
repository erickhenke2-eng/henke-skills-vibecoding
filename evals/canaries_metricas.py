#!/usr/bin/env python3
"""
Canários bilaterais dos portões métricos (spec: docs/designs/portoes-metricos-ub.md).

Cada portão precisa provar as duas coisas:
  (a) reprova um caso comprovadamente ruim (known-dirty fixture);
  (b) aprova o caminho permitido — um portão que reprova tudo não serve.

Cobertura: assertless_scan, crap_score, ratchet, fence_check, tornado_scan
+ coletores (churn/metrics/edges) + disciplina de exit 2 em entrada malformada.
"""
from pathlib import Path
import json
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parents[1]
MET = repo_root / "scripts" / "metricas"
SIGNALS = repo_root / "scripts" / "repo_signals.py"


def run(script: Path, *args, input_text=None, cwd=None):
    res = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        capture_output=True, text=True, input=input_text, cwd=cwd,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def run_all_metric_canaries() -> tuple[int, int, list[str]]:
    passed = total = 0
    reports = []

    def expect(name, cond, detail=""):
        nonlocal passed, total
        total += 1
        if cond:
            passed += 1
            reports.append(f"[PASSOU] {name}")
        else:
            reports.append(f"[FALHOU] {name} {detail}")

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)

        # --- 1/2 assertless: dirty reprova, limpo aprova ---
        dirty = t / "test_dirty.py"
        dirty.write_text(
            "def test_sem_assert():\n    x = 1 + 1\n    print(x)\n", encoding="utf-8")
        code, _ = run(MET / "assertless_scan.py", dirty)
        expect("Canário M1: assertless reprova teste sem asserção", code == 1)
        clean = t / "test_clean.py"
        clean.write_text("def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
        code, _ = run(MET / "assertless_scan.py", clean)
        expect("Canário M2: assertless aprova teste com asserção", code == 0)

        # --- 3/4 crap_score: breach reprova, ok aprova ---
        code, _ = run(MET / "crap_score.py", "--threshold", "6",
                      input_text="bar\t20\t0\n")
        expect("Canário M3: crap reprova função complexa sem cobertura", code == 1)
        code, _ = run(MET / "crap_score.py", "--threshold", "6",
                      input_text="foo\t3\t95\n")
        expect("Canário M4: crap aprova função simples coberta", code == 0)

        # --- 5/6 ratchet: piora reprova, melhora aprova ---
        base = t / "base.tsv"
        base.write_text("src/a.py\t3.0\t4\t80\n", encoding="utf-8")
        worse = t / "worse.tsv"
        worse.write_text("src/a.py\t25.0\t12\t40\n", encoding="utf-8")
        better = t / "better.tsv"
        better.write_text("src/a.py\t2.0\t3\t85\n", encoding="utf-8")
        code, _ = run(MET / "ratchet.py", "--baseline", base, "--current", worse)
        expect("Canário M5: ratchet reprova arquivo que piorou", code == 1)
        code, _ = run(MET / "ratchet.py", "--baseline", base, "--current", better)
        expect("Canário M6: ratchet aprova arquivo que melhorou", code == 0)

        # --- 7/8 fence: outward reprova, inward aprova ---
        fence = t / "fence.json"
        fence.write_text(json.dumps({
            "layers": ["ui", "nucleo", "executores"],
            "modules": {"src/ui": "ui", "src/core": "nucleo", "src/exec": "executores"},
        }), encoding="utf-8")
        bad = t / "edges_bad.json"
        bad.write_text(json.dumps(
            [{"from": "src/exec/x.py", "to": "src/ui/y.py"}]), encoding="utf-8")
        good = t / "edges_good.json"
        good.write_text(json.dumps(
            [{"from": "src/ui/y.py", "to": "src/core/z.py"},
             {"from": "src/core/z.py", "to": "src/exec/x.py"}]), encoding="utf-8")
        code, _ = run(MET / "fence_check.py", fence, bad)
        expect("Canário M7: fence reprova dependência outward (exec→ui)", code == 1)
        code, _ = run(MET / "fence_check.py", fence, good)
        expect("Canário M8: fence aprova dependência inward (ui→core→exec)", code == 0)

        # --- 9/10 tornado: tendência subindo reprova, plana aprova ---
        rising = "c1\t2\nc2\t2\nc3\t3\nc4\t2\nc5\t3\nc6\t2\nc7\t3\nc8\t2\nc9\t3\nc10\t2\n" \
                 "c11\t9\nc12\t10\nc13\t11\nc14\t9\nc15\t12\n"
        flat = "c1\t2\nc2\t3\nc3\t2\nc4\t3\nc5\t2\nc6\t3\nc7\t2\nc8\t3\nc9\t2\nc10\t3\n" \
               "c11\t2\nc12\t3\nc13\t2\nc14\t3\nc15\t2\n"
        code, _ = run(MET / "tornado_scan.py", "--order", "oldest-first", input_text=rising)
        expect("Canário M9: tornado reprova amplificação crescente", code == 1)
        code, _ = run(MET / "tornado_scan.py", "--order", "oldest-first", input_text=flat)
        expect("Canário M10: tornado aprova tendência estável", code == 0)

        # --- 11: entrada malformada = exit 2 (não-veredito), nunca verde ---
        code, _ = run(MET / "crap_score.py", input_text="lixo sem tab\n")
        expect("Canário M11: entrada malformada sai 2 (fail closed)", code == 2)

        # --- 12/13/14 coletores: churn, metrics, edges produzem sinal real ---
        proj = t / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "a.py").write_text(
            "import b\n\ndef f(x):\n    if x and x > 0 or x == -1:\n        return 1\n"
            "    for i in range(3):\n        x += i\n    return x\n", encoding="utf-8")
        (proj / "src" / "b.py").write_text("CONST = 1\n", encoding="utf-8")

        code, out = run(SIGNALS, "metrics", "--root", proj)
        expect("Canário M12: metrics emite linha por função com complexidade",
               code == 0 and "a.py::f:3\t" in out)

        code, out = run(SIGNALS, "edges", "--root", proj)
        found = code == 0 and json.loads(out) if code == 0 else []
        expect("Canário M13: edges encontra aresta interna a.py→b.py",
               any(e["from"].endswith("a.py") and e["to"].endswith("b.py") for e in found))

        # churn precisa de git: repo temporário mínimo
        subprocess.run(["git", "init"], cwd=proj, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=proj, check=True)
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-m", "c1"], cwd=proj,
                       capture_output=True, check=True)
        code, out = run(SIGNALS, "churn", "--repo", proj, "--order", "oldest-first")
        expect("Canário M14: churn emite 1 commit com 2 arquivos",
               code == 0 and out.strip().endswith("\t2"))

    return passed, total, reports


def main():
    print("Canários bilaterais dos portões métricos (UB-assimilados)...")
    passed, total, reports = run_all_metric_canaries()
    for r in reports:
        print(" ", r)
    print(f"\nResultado: {passed}/{total} canários aprovados "
          f"({(passed/total)*100:.1f}% de recall).")
    if passed != total:
        sys.exit(1)
    print("OK: portões métricos reprovam o ruim e aprovam o permitido.")


if __name__ == "__main__":
    main()
