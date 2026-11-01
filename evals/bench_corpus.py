#!/usr/bin/env python3
"""
bench_corpus.py — benchmark adversarial da suíte de portões.

Monta um corpus sintético com defeitos plantados e mede o RECALL de detecção
de uma versão da suíte (--suite-dir). Rodável contra qualquer checkout:

  python evals/bench_corpus.py --suite-dir <checkout-antigo>
  python evals/bench_corpus.py --suite-dir <checkout-novo>

Categorias de resultado por defeito:
  DETECTED        portão reprovou o defeito (exit 1 / retorno False)
  MISSED          portão existe mas aprovou o defeito (falso-verde)
  NO-INSTRUMENT   a suíte não tem instrumento capaz de consumir o caso
  ERROR:c         execução quebrou (exit 2 / exceção) — não-veredito
  APPROVED        controle legítimo aprovado (esperado)
  FALSE-RED       controle legítimo reprovado (falso-positivo)

O denominador de recall conta SÓ defeitos plantados. Controles (D9) ficam fora
do denominador e são reportados à parte — aprovar um controle não é detecção.

Exit 0 sempre (benchmark mede, não julga).
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def sh(args, cwd=None, input_text=None, merge_err=True):
    """merge_err=False devolve stdout limpo (para alimentar outro portão com TSV/JSON)."""
    res = subprocess.run(args, capture_output=True, text=True, input=input_text, cwd=cwd)
    out = res.stdout or ""
    if merge_err:
        out += res.stderr or ""
    return res.returncode, out


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Suite:
    """Portões localizáveis num checkout da suíte."""
    def __init__(self, root: Path):
        self.root = root

    def script(self, *parts):
        p = self.root.joinpath(*parts)
        return p if p.is_file() else None

    def has(self, *parts) -> bool:
        return self.script(*parts) is not None

    def run_gate(self, *parts, args=(), input_text=None, cwd=None):
        s = self.script(*parts)
        if s is None:
            return "NO-INSTRUMENT", ""
        code, out = sh([sys.executable, str(s), *map(str, args)],
                       cwd=cwd, input_text=input_text)
        return code, out


def build_corpus(t: Path):
    """Corpus: repo sintético com defeitos plantados. Retorna dict de artefatos."""
    proj = t / "proj"
    (proj / "src" / "ui").mkdir(parents=True)
    (proj / "src" / "exec").mkdir(parents=True)
    (proj / "tests").mkdir(parents=True)

    # D1: teste-fantasma (sem asserção + except engolidor)
    (proj / "tests" / "test_fake.py").write_text(
        "def test_finge():\n"
        "    x = compute()\n"
        "    print(x)\n"
        "def test_engole():\n"
        "    try:\n        raise ValueError('x')\n"
        "    except Exception:\n        pass\n", encoding="utf-8")
    # D9: teste legítimo (controle de falso-positivo)
    (proj / "tests" / "test_real.py").write_text(
        "def test_soma():\n    assert 1 + 1 == 2\n", encoding="utf-8")

    # D2: função monstro sem cobertura
    monster = ["def monstro(x):"]
    for i in range(20):
        monster.append(f"    if x == {i} and x > {i} or x < -{i}:")
        monster.append(f"        x += {i}")
    monster.append("    return x")
    (proj / "src" / "exec" / "worker.py").write_text(
        "\n".join(monster) + "\n", encoding="utf-8")

    # D3/D4: executor importando UI (dependência outward) + baseline p/ ratchet
    (proj / "src" / "ui" / "tela.py").write_text("TITLE = 'x'\n", encoding="utf-8")
    (proj / "src" / "exec" / "invasor.py").write_text(
        "from src.ui.tela import TITLE\nprint(TITLE)\n", encoding="utf-8")
    (proj / "src" / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "src" / "exec" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "src" / "__init__.py").write_text("", encoding="utf-8")
    (proj / "fence.json").write_text(json.dumps({
        "layers": ["ui", "nucleo", "executores"],
        "modules": {"src/ui": "ui", "src/exec": "executores"}}), encoding="utf-8")

    # D5: histórico com amplificação crescente (15 commits: 10 leves, 5 pesados)
    sh(["git", "init"], cwd=proj)
    sh(["git", "config", "user.name", "B"], cwd=proj)
    sh(["git", "config", "user.email", "b@b"], cwd=proj)
    for i in range(10):  # commits pequenos
        (proj / "src" / "exec" / f"f{i}.py").write_text(f"C={i}\n", encoding="utf-8")
        sh(["git", "add", "-A"], cwd=proj)
        sh(["git", "commit", "-m", f"small{i}", "-q"], cwd=proj)
    for i in range(5):   # commits tornado: tocam 9-12 arquivos
        for j in range(9 + i):
            f = proj / "src" / "exec" / f"g{i}_{j}.py"
            f.write_text(f"V{i}_{j}=1\n", encoding="utf-8")
        sh(["git", "add", "-A"], cwd=proj)
        sh(["git", "commit", "-m", f"big{i}", "-q"], cwd=proj)

    # D3 (ratchet): baseline calculada no commit atual; depois piora worker.py
    # (feito no fluxo do benchmark, ver run())

    # D6: spec_drift — spec delimita src/ok.py, diff toca fora do escopo
    (proj / "spec.md").write_text(
        "# Spec\n## 4. Escopo Declarado\n- `src/ok.py`\n", encoding="utf-8")
    (proj / "src" / "ok.py").write_text("X=1\n", encoding="utf-8")
    (proj / "src" / "fora.py").write_text("X=2\n", encoding="utf-8")

    # D10: teste-fantasma TypeScript (.test.ts sem asserção real)
    (proj / "tests" / "fake.test.ts").write_text(
        'import { describe, it, expect, vi } from "vitest";\n'
        'import { monstroTs } from "../src/exec/monstroTs";\n'
        'describe("ts", () => {\n'
        '  it("fantasma", () => { const r = monstroTs(1); console.log(r); });\n'
        '  it("mock only", () => { const m = vi.fn(); m();'
        ' expect(m).toHaveBeenCalledTimes(1); });\n'
        '  it("engole", () => { try { monstroTs(9); } catch (e) { } });\n'
        "});\n", encoding="utf-8")

    # D11: função monstro TypeScript sem cobertura
    monster_ts = ["export function monstroTs(x: number): number {"]
    for i in range(20):
        monster_ts.append(f"  if (x === {i} && x > {i} || x < -{i}) x += {i};")
    monster_ts.append("  return x;\n}")
    (proj / "src" / "exec" / "monstroTs.ts").write_text(
        "\n".join(monster_ts) + "\n", encoding="utf-8")

    # D12: dependência outward em TypeScript (exec importa ui)
    (proj / "src" / "ui" / "telaTs.ts").write_text(
        "export const TITLE = 'x';\n", encoding="utf-8")
    (proj / "src" / "exec" / "invasor.ts").write_text(
        'import { TITLE } from "../ui/telaTs.js";\nconsole.log(TITLE);\n',
        encoding="utf-8")
    return proj


def run(suite: Suite):
    rows = []

    def record(defeito, resultado, detalhe="", control=False):
        rows.append((defeito, resultado, detalhe, control))

    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        proj = build_corpus(t)

        # --- D1 assertless em teste-fantasma ---
        code, _ = suite.run_gate("scripts", "metricas", "assertless_scan.py",
                                 args=(proj / "tests" / "test_fake.py",))
        record("D1 teste-fantasma (sem asserção)",
               {1: "DETECTED", 0: "MISSED"}.get(code, code))

        # --- D9 controle: teste legítimo NÃO pode reprovar ---
        code, _ = suite.run_gate("scripts", "metricas", "assertless_scan.py",
                                 args=(proj / "tests" / "test_real.py",))
        record("D9 teste legítimo (controle falso-positivo)",
               {0: "APPROVED", 1: "FALSE-RED"}.get(code, code), control=True)

        # --- D2 monstro: coleta métricas -> crap ---
        sig = suite.script("scripts", "repo_signals.py")
        if sig and suite.has("scripts", "metricas", "crap_score.py"):
            code, tsv = sh([sys.executable, str(sig), "metrics", "--root", proj],
                           merge_err=False)
            code2, _ = sh([sys.executable, str(suite.script("scripts", "metricas", "crap_score.py")),
                           "--threshold", "6"], input_text=tsv)
            record("D2 função complexa sem cobertura",
                   {1: "DETECTED", 0: "MISSED"}.get(code2, f"ERROR:{code2}"))
        else:
            record("D2 função complexa sem cobertura", "NO-INSTRUMENT")

        # --- D3 ratchet: baseline -> piora -> compara ---
        if sig and suite.has("scripts", "metricas", "ratchet.py"):
            _, base_tsv = sh([sys.executable, str(sig), "metrics",
                              "--root", proj, "--per-file"], merge_err=False)
            (proj / "src" / "exec" / "worker.py").write_text(
                "\n".join(["def monstro2(x):"] + sum(
                    [[f"    if x=={i} and y or z:", f"        x+={i}"] for i in range(30)], [])
                ) + "\n    return x\n", encoding="utf-8")
            _, cur_tsv = sh([sys.executable, str(sig), "metrics",
                             "--root", proj, "--per-file"], merge_err=False)
            bf, cf = t / "b.tsv", t / "c.tsv"
            bf.write_text(base_tsv, encoding="utf-8")
            cf.write_text(cur_tsv, encoding="utf-8")
            code, _ = suite.run_gate("scripts", "metricas", "ratchet.py",
                                     args=("--baseline", bf, "--current", cf))
            record("D3 arquivo piorou vs baseline",
                   {1: "DETECTED", 0: "MISSED"}.get(code, f"ERROR:{code}"))
        else:
            record("D3 arquivo piorou vs baseline", "NO-INSTRUMENT")

        all_edges = []
        # --- D4 fence: edges -> outward (filtrado por .py) ---
        if sig and suite.has("scripts", "metricas", "fence_check.py"):
            _, edges_raw = sh([sys.executable, str(sig), "edges", "--root", proj],
                              merge_err=False)
            try:
                all_edges = json.loads(edges_raw)
            except json.JSONDecodeError:
                all_edges = []
            py_edges = [e for e in all_edges if e["from"].endswith(".py")]
            ef = t / "edges_py.json"
            ef.write_text(json.dumps(py_edges), encoding="utf-8")
            code, _ = suite.run_gate("scripts", "metricas", "fence_check.py",
                                     args=(proj / "fence.json", ef))
            record("D4 executor importa UI (outward, .py)",
                   {1: "DETECTED", 0: "MISSED"}.get(code, code))
        else:
            record("D4 executor importa UI (outward, .py)", "NO-INSTRUMENT")

        # --- D5 tornado: churn crescente ---
        if sig and suite.has("scripts", "metricas", "tornado_scan.py"):
            _, churn = sh([sys.executable, str(sig), "churn", "--repo", proj,
                           "--order", "oldest-first"], merge_err=False)
            code, _ = suite.run_gate("scripts", "metricas", "tornado_scan.py",
                                     args=("--order", "oldest-first"), input_text=churn)
            record("D5 amplificação de mudança crescente",
                   {1: "DETECTED", 0: "MISSED"}.get(code, code))
        else:
            record("D5 amplificação de mudança crescente", "NO-INSTRUMENT")

        # --- D6 spec_drift (portão antigo, controle) ---
        sd = suite.script("scripts", "spec_drift.py")
        if sd:
            try:
                mod = load_module(sd, "sd_mod")
                spec_hash = mod.compute_spec_hash(proj / "spec.md")
                code, out = sh([sys.executable, str(sd), "--spec", proj / "spec.md",
                                "--expected-hash", spec_hash, "--repo", proj,
                                "--against", "HEAD~5"], cwd=proj)
                record("D6 diff toca arquivo fora do escopo da spec",
                       "DETECTED" if code != 0 and "escopo" in out else
                       "MISSED" if code == 0 else f"ERROR:{code}")
            except Exception as e:
                record("D6 diff toca arquivo fora do escopo da spec", f"ERROR:{e}")
        else:
            record("D6 diff toca arquivo fora do escopo da spec", "NO-INSTRUMENT")

        # --- D7 audit_ledger tautologia (portão antigo, controle) ---
        al = suite.script("scripts", "audit_ledger.py")
        if al:
            try:
                mod = load_module(al, "al_mod")
                ok, _, _ = mod.audit_ledger_content(
                    [{"cmd": "npm test", "exit": 0, "testes_descobertos": 0}])
                record("D7 ledger 0 testes (vitória por tautologia)",
                       "DETECTED" if not ok else "MISSED")
            except Exception as e:
                record("D7 ledger 0 testes (vitória por tautologia)", f"ERROR:{e}")
        else:
            record("D7 ledger 0 testes (vitória por tautologia)", "NO-INSTRUMENT")

        # --- D8 cheat .sr-only (canário antigo, controle) ---
        can = suite.script("evals", "canaries.py")
        if can:
            try:
                mod = load_module(can, "can_mod")
                ok, _ = mod.check_a11y_sr_only_cheat(
                    '<button>Salvar <span class="sr-only">Salvar</span></button>')
                record("D8 cheat .sr-only duplicando texto",
                       "DETECTED" if not ok else "MISSED")
            except Exception as e:
                record("D8 cheat .sr-only duplicando texto", f"ERROR:{e}")
        else:
            record("D8 cheat .sr-only duplicando texto", "NO-INSTRUMENT")

        # --- D10 teste-fantasma TypeScript ---
        code, _ = suite.run_gate("scripts", "metricas", "assertless_scan.py",
                                 args=(proj / "tests" / "fake.test.ts",))
        record("D10 teste-fantasma .test.ts",
               {1: "DETECTED", 0: "MISSED"}.get(code, code))

        # --- D11 monstro TypeScript sem cobertura ---
        if sig and suite.has("scripts", "metricas", "crap_score.py"):
            code, tsv = sh([sys.executable, str(sig), "metrics", "--root", proj],
                           merge_err=False)
            ts_tsv = "".join(l + "\n" for l in tsv.splitlines() if ".ts" in l)
            code2, _ = sh([sys.executable, str(suite.script("scripts", "metricas", "crap_score.py")),
                           "--threshold", "6"], input_text=ts_tsv)
            record("D11 função complexa .ts sem cobertura",
                   {1: "DETECTED", 0: "MISSED"}.get(code2, f"ERROR:{code2}"))
        else:
            record("D11 função complexa .ts sem cobertura", "NO-INSTRUMENT")

        # --- D12 outward TypeScript ---
        if sig and suite.has("scripts", "metricas", "fence_check.py"):
            ts_edges = [e for e in all_edges if e["from"].endswith((".ts", ".tsx"))]
            ef = t / "edges_ts.json"
            ef.write_text(json.dumps(ts_edges), encoding="utf-8")
            code, _ = suite.run_gate("scripts", "metricas", "fence_check.py",
                                     args=(proj / "fence.json", ef))
            record("D12 executor importa UI (outward, .ts)",
                   {1: "DETECTED", 0: "MISSED"}.get(code, code))
        else:
            record("D12 executor importa UI (outward, .ts)", "NO-INSTRUMENT")

        # --- D13..D16 + C2: defeitos de segurança, ledger de cobertura e fechamento de ponte (Wave 2) ---
        fake_key = "sk-proj-" + ("Z9y8X7w6V5u4T3s2R1q0" * 2)
        (proj / "src" / "exec" / "inseguro.py").write_text(
            "import subprocess\n"
            f"OPENAI_KEY = '{fake_key}'\n"
            "def roda(cmd):\n"
            "    subprocess.run(cmd, shell=True)\n",
            encoding="utf-8")
        (proj / "src" / "exec" / "inseguroTs.ts").write_text(
            "import { readFileSync } from 'fs';\n"
            "export function ler(req: any, db: any) {\n"
            "  const token = Math.random().toString(36);\n"
            "  db.query(`SELECT * FROM secrets WHERE token = '${token}'`);\n"
            "  return readFileSync(req.query.path);\n"
            "}\n",
            encoding="utf-8")
        (proj / "src" / "exec" / "seguro.py").write_text(
            "import subprocess\n"
            "AWS_DOC = 'AKIAIOSFODNN7EXAMPLE'\n"
            "def roda_seguro(caminho: str):\n"
            "    subprocess.run(['ffmpeg', '-i', caminho], shell=False, check=True)\n",
            encoding="utf-8")

        if suite.has("scripts", "metricas", "hazard_scan.py"):
            code, _ = suite.run_gate("scripts", "metricas", "hazard_scan.py",
                                     args=(proj / "src" / "exec" / "inseguro.py",))
            record("D13 segredo hardcoded + shell=True (.py)",
                   {1: "DETECTED", 0: "MISSED"}.get(code, f"ERROR:{code}"))
            code, _ = suite.run_gate("scripts", "metricas", "hazard_scan.py",
                                     args=(proj / "src" / "exec" / "inseguroTs.ts",))
            record("D14 SQLi template + path traversal + weak crypto (.ts)",
                   {1: "DETECTED", 0: "MISSED"}.get(code, f"ERROR:{code}"))
            code, _ = suite.run_gate("scripts", "metricas", "hazard_scan.py",
                                     args=(proj / "src" / "exec" / "seguro.py",))
            record("C2 subprocess seguro + chave AWS documental (controle)",
                   {0: "APPROVED", 1: "FALSE-RED"}.get(code, f"ERROR:{code}"), control=True)
        else:
            record("D13 segredo hardcoded + shell=True (.py)", "NO-INSTRUMENT")
            record("D14 SQLi template + path traversal + weak crypto (.ts)", "NO-INSTRUMENT")

        if suite.has("scripts", "metricas", "coverage_ledger.py"):
            led_out = t / "bench_ledger.json"
            suite.run_gate("scripts", "metricas", "coverage_ledger.py",
                           args=("init", "--root", proj / "src", "--out", led_out))
            code, _ = suite.run_gate("scripts", "metricas", "coverage_ledger.py",
                                     args=("verify", led_out))
            record("D15 auditoria incompleta (unidades pending no coverage_ledger)",
                   {1: "DETECTED", 0: "MISSED"}.get(code, f"ERROR:{code}"))
        else:
            record("D15 auditoria incompleta (unidades pending no coverage_ledger)", "NO-INSTRUMENT")

        (proj / "spec_incompleta.md").write_text(
            "# Spec\n## 4. Escopo Declarado\n- `src/ok.py`\n- `src/nao_implementado.py`\n"
            "## 5. Pontes de Contrato\n- ponte: `src/ok.py::funcaoFantasma -> src/ui/tela.py`\n",
            encoding="utf-8")
        if suite.has("scripts", "metricas", "contract_closure.py"):
            code, _ = suite.run_gate("scripts", "metricas", "contract_closure.py",
                                     args=("--spec", proj / "spec_incompleta.md", "--repo", proj))
            record("D16 código de menos (arquivo declarado ausente + ponte quebrada)",
                   {1: "DETECTED", 0: "MISSED"}.get(code, f"ERROR:{code}"))
        else:
            record("D16 código de menos (arquivo declarado ausente + ponte quebrada)", "NO-INSTRUMENT")

    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite-dir", required=True, help="checkout da suíte a medir")
    args = ap.parse_args()
    suite = Suite(Path(args.suite_dir).resolve())
    if not (suite.root / "scripts").is_dir():
        print(f"ERRO: {suite.root} não parece um checkout da suíte", file=sys.stderr)
        sys.exit(2)

    print(f"Benchmark adversarial — suite: {suite.root}\n")
    rows = run(suite)
    defects = [(d, r, i) for d, r, i, c in rows if not c]
    controls = [(d, r, i) for d, r, i, c in rows if c]
    det = sum(1 for _, r, _ in defects if r == "DETECTED")
    miss = sum(1 for _, r, _ in defects if r == "MISSED")
    noins = sum(1 for _, r, _ in defects if r == "NO-INSTRUMENT")
    err = sum(1 for _, r, _ in defects if str(r).startswith("ERROR"))
    appr = sum(1 for _, r, _ in controls if r == "APPROVED")
    fred = sum(1 for _, r, _ in controls if r == "FALSE-RED")

    for d, r, det_info, c in rows:
        tag = " [controle]" if c else ""
        print(f"  {r:<14} {d}  {det_info}{tag}")

    instrumented = det + miss
    print(f"\nResumo (defeitos={len(defects)}, controles={len(controls)}):")
    print(f"  {det} detectado(s) · {miss} perdido(s) · "
          f"{noins} sem instrumento · {err} erro(s)")
    print(f"  controles: {appr} aprovado(s) · {fred} falso-vermelho")
    if instrumented:
        print(f"Recall de defeitos (sobre instrumentados): {det}/{instrumented} "
              f"({100*det/instrumented:.0f}%)")
    print(f"Cobertura instrumental: {instrumented}/{len(defects)} defeitos "
          f"({100*instrumented/len(defects):.0f}%)")


if __name__ == "__main__":
    main()
