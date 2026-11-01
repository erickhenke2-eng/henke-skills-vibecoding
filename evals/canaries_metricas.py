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

        # --- 15: fence com edges=[] é não-veredito (exit 2), nunca GREEN ---
        empty = t / "edges_empty.json"
        empty.write_text("[]", encoding="utf-8")
        code, out = run(MET / "fence_check.py", fence, empty)
        expect("Canário M15: fence edges=[] sai 2 (falso-verde eliminado)",
               code == 2 and "GREEN" not in out)

        # --- 16/17 assertless poliglota: .test.ts sujo reprova, limpo aprova ---
        tsproj = t / "tsproj"
        (tsproj / "src").mkdir(parents=True)
        ts_dirty = tsproj / "src" / "x.test.ts"
        ts_dirty.write_text(
            'import { describe, it, expect } from "vitest";\n'
            'describe("s", () => {\n'
            '  it("fantasma", () => { const x = 1 + 1; });\n'
            '  it("mock only", () => { const m = vi.fn(); m(); '
            'expect(m).toHaveBeenCalledTimes(1); });\n'
            '  it("real", () => { expect(1 + 1).toBe(2); });\n'
            "});\n", encoding="utf-8")
        code, out = run(MET / "assertless_scan.py", ts_dirty)
        expect("Canário M16: assertless .test.ts reprova fantasma+mock (exit 1)",
               code == 1 and "NO-ASSERTION" in out and "MOCK-ONLY" in out)
        ts_clean = tsproj / "src" / "y.test.ts"
        ts_clean.write_text(
            'import { it, expect } from "vitest";\n'
            'it("real", () => { expect(40 + 2).toBe(42); });\n'
            'it("timeout vitest", { timeout: 999 }, () => {'
            ' expect(1).toBe(1); });\n', encoding="utf-8")
        code, _ = run(MET / "assertless_scan.py", ts_clean)
        expect("Canário M17: assertless .test.ts aprova teste com asserção",
               code == 0)

        # --- 18/19 metrics+edges em TypeScript ---
        (tsproj / "src" / "helper.ts").write_text(
            "export function pick(xs: number[]): number {\n"
            "  if (xs.length === 0) return 0;\n"
            "  for (const x of xs) { if (x > 0 && x < 9 || x === 99) return x; }\n"
            "  return -1;\n}\n", encoding="utf-8")
        (tsproj / "src" / "main.ts").write_text(
            'import { pick } from "./helper.js";\n'
            "export const run = () => pick([1]);\n", encoding="utf-8")
        code, out = run(SIGNALS, "metrics", "--root", tsproj)
        expect("Canário M18: metrics emite funções .ts (pick+run, ignora .test.ts)",
               code == 0 and "helper.ts::pick" in out and "run" in out
               and ".test.ts" not in out)
        code, out = run(SIGNALS, "edges", "--root", tsproj)
        ts_edges = json.loads(out) if code == 0 else []
        expect("Canário M19: edges resolve ./helper.js → helper.ts",
               any(e["to"].endswith("helper.ts") for e in ts_edges))

        # --- 20: churn conta commit binário (numstat "-\t-") ---
        (proj / "bin.dat").write_bytes(b"\x00\x01\x02\xff\xfe")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", "bin"], cwd=proj, check=True,
                       capture_output=True)
        code, out = run(SIGNALS, "churn", "--repo", proj, "--order", "oldest-first")
        rows = [l for l in out.splitlines() if "\t" in l]
        expect("Canário M20: churn conta binário e nunca emite count=0",
               code == 0 and len(rows) == 2
               and all(int(r.split("\t")[-1]) >= 1 for r in rows))

        # --- 21..24: load_coverage nos 4 formatos de mercado ---
        cov_cases = {
            "cov_py.json": (json.dumps({"files": {"src/a.py": {"summary":
                            {"percent_covered": 88.5}}}}), "a.py", 88),
            "cov_summary.json": (json.dumps({"total": {"lines": {"pct": 70}},
                                 "src/helper.ts": {"lines": {"pct": 77.4}}}),
                                 "helper.ts", 77),
            "cov_final.json": (json.dumps({"src/helper.ts":
                               {"s": {"0": 1, "1": 1, "2": 0, "3": 2}}}),
                               "helper.ts", 75),
            "cov.info": ("TN:\nSF:src/helper.ts\nLF:8\nLH:6\nend_of_record\n",
                         "helper.ts", 75),
        }
        for i, (fname, (content, needle, pct)) in enumerate(sorted(cov_cases.items())):
            cf = t / fname
            cf.write_text(content, encoding="utf-8")
            code, out = run(SIGNALS, "metrics", "--root", tsproj if "ts" in needle else proj,
                            "--coverage", cf)
            hit = [l for l in out.splitlines() if needle in l and f"\t{pct}" in l]
            expect(f"Canário M{21+i}: coverage {fname} alimenta metrics ({pct}%)",
                   code == 0 and bool(hit))

        # --- 25: varredura poda node_modules ---
        nm = tsproj / "node_modules" / "dep"
        nm.mkdir(parents=True)
        (nm / "junk.ts").write_text("export function junk() { return 1; }\n",
                                    encoding="utf-8")
        code, out = run(SIGNALS, "metrics", "--root", tsproj)
        expect("Canário M25: metrics poda node_modules",
               code == 0 and "node_modules" not in out)

        # --- 26: aspas simples no fim da linha + 'src/*' não corrompem o lexer ---
        sq = tsproj / "src" / "sq.test.ts"
        sq.write_text(
            "import { join } from 'path';\n"
            "import { tmpdir } from 'os';\n"
            "it('aspas simples', () => {\n"
            "  const p = join(tmpdir(), 'vc-blender-'));\n"
            "  expect(p).toContain('vc-blender-');\n"
            "});\n"
            "it('glob em aspas', () => { expect('src/*').toBe('src/*'); });\n",
            encoding="utf-8")
        code, out = run(MET / "assertless_scan.py", sq)
        expect("Canário M26: 'aspas simples' no fim da linha não corrompe lexer",
               code == 0 and "2 test function(s)" in out)

        # --- 27: template literal aninhado ${`...`} ---
        tpl = tsproj / "src" / "tpl.test.ts"
        tpl.write_text(
            "it('aninhado', () => {\n"
            "  const s = { title: 'a', status: 'b', error: 'e' };\n"
            "  const t = `${s.title}:${s.status}${s.error ? ` (${s.error})` : ''}`;\n"
            "  expect(t).toContain('a');\n"
            "});\n"
            "it('depois do template', () => { expect(1).toBe(1); });\n",
            encoding="utf-8")
        code, out = run(MET / "assertless_scan.py", tpl)
        expect("Canário M27: template aninhado preserva teste seguinte",
               code == 0 and "2 test function(s)" in out)

        # --- 28/29: Go multi-retorno e TS genérico <T> nas métricas ---
        poly2 = t / "poly2"
        (poly2 / "src").mkdir(parents=True)
        (poly2 / "src" / "parse.go").write_text(
            "package p\n"
            'import "strconv"\n'
            "func Parse(s string) (int, error) {\n"
            "  if s == \"\" {\n    return 0, strconv.ErrSyntax\n  }\n"
            "  return strconv.Atoi(s)\n}\n", encoding="utf-8")
        (poly2 / "src" / "gen.ts").write_text(
            "export async function wrap<T extends object>(x: T): Promise<T> {\n"
            "  if (x && typeof x === 'object' || !x) return x;\n"
            "  return x;\n}\n", encoding="utf-8")
        (poly2 / "src" / "m.rs").write_text(
            "use crate::a::b::Simbolo;\n"
            "fn calc(x: i32) -> i32 {\n"
            "  match x { 0 => 1, _ => 2 }\n}\n", encoding="utf-8")
        code, out = run(SIGNALS, "metrics", "--root", poly2)
        expect("Canário M28: metrics vê Go (int, error) e TS <T> e Rust match",
               code == 0 and "Parse" in out and "wrap" in out and "calc" in out)
        calc_row = [l for l in out.splitlines() if "calc" in l]
        expect("Canário M29: match Rust conta complexidade (>=2)",
               bool(calc_row) and int(calc_row[0].split("\t")[1]) >= 2)

        # --- 30: edges './tela.service' → tela.service.ts ---
        (poly2 / "src" / "ui").mkdir()
        (poly2 / "src" / "ui" / "tela.service.ts").write_text(
            "export const S = 1;\n", encoding="utf-8")
        (poly2 / "src" / "ui" / "main.ts").write_text(
            'import { S } from "./tela.service";\nexport const M = S;\n',
            encoding="utf-8")
        code, out = run(SIGNALS, "edges", "--root", poly2)
        svc = json.loads(out) if code == 0 else []
        expect("Canário M30: edges resolve ./tela.service → tela.service.ts",
               any(e["to"].endswith("tela.service.ts") for e in svc))

        # --- 31: TEST_RE não contamina path absoluto terminando em '/test/' ---
        tdir = t / "test" / "src"
        tdir.mkdir(parents=True)
        (tdir / "code.ts").write_text(
            "export function g(x: number) { if (x) return 1; return 0; }\n",
            encoding="utf-8")
        code, out = run(SIGNALS, "metrics", "--root", t / "test")
        expect("Canário M31: metrics não zera quando root se chama 'test'",
               code == 0 and "code.ts::g" in out)

        # --- 32: catch(err) { code = (err as { code?: string }).code } + expect(code) ---
        cast_catch = tsproj / "src" / "cast_catch.test.ts"
        cast_catch.write_text(
            "it('captura campo com cast TS', () => {\n"
            "  let code1 = '';\n"
            "  try { throw { code: 'E1' }; } catch (err) {\n"
            "    code1 = (err as { code?: string }).code ?? '';\n"
            "  }\n"
            "  expect(code1).toBe('E1');\n"
            "});\n",
            encoding="utf-8")
        code, out = run(MET / "assertless_scan.py", cast_catch)
        expect("Canário M32: catch com cast TS (err as ...).code inspecionado fora é aprovado",
               code == 0 and "1 test function(s)" in out)

        # --- 33: arrow TS com retorno de objeto entre parênteses sem ponto-e-vírgula ---
        (tdir / "arrow.ts").write_text(
            "export const makeObj = (x: number) => ({\n"
            "  ok: x > 0 && x < 10\n"
            "})\n",
            encoding="utf-8")
        code, out = run(SIGNALS, "metrics", "--root", t / "test")
        expect("Canário M33: metrics detecta arrow => ({ ... }) sem ';' com decisão",
               code == 0 and "arrow.ts::makeObj:1\t2\t" in out)

        # --- 34..37: hazard_scan.py bilateral (scan_path + CLI) ---
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from scripts.metricas.hazard_scan import scan_path
        from scripts.metricas.coverage_ledger import init_ledger
        from scripts.metricas.contract_closure import check_contract_closure

        haz_bad = t / "haz_bad.py"
        fake_sk = "sk-proj-" + ("A9b8C7d6E5f4G3h2J1k0" * 2)
        haz_bad.write_text(
            "import subprocess\n"
            f"API_KEY = '{fake_sk}'\n"
            "def run_cmd(u):\n"
            "    subprocess.run(u, shell=True)\n"
            "    q = f'SELECT * FROM users WHERE name = {u}'\n"
            "    return q\n",
            encoding="utf-8")
        sc_cnt, fnds, _ = scan_path(haz_bad)
        rules_found = {r for r, _, _, _ in fnds}
        expect("Canário M34: hazard_scan reprova HARDCODED-SECRET + SHELL-INJECTION + SQL-CONCAT",
               sc_cnt == 1 and {"HARDCODED-SECRET", "SHELL-INJECTION", "SQL-CONCAT"} <= rules_found)

        haz_good = t / "haz_good.py"
        haz_good.write_text(
            "# Exemplo documental AWS: AKIAIOSFODNN7EXAMPLE e sk-proj-1234567890123456789012345 em comentario\n"
            "import subprocess\n"
            "AWS_DOC = 'AKIAIOSFODNN7EXAMPLE'\n"
            "def safe_cmd(arg: str):\n"
            "    subprocess.run(['ffmpeg', '-i', arg], shell=False, check=True)\n"
            "    subprocess.run('git status', shell=True)  # hazard-ok: SHELL-INJECTION — comando fixo auditado\n",
            encoding="utf-8")
        code, out = run(MET / "hazard_scan.py", haz_good)
        expect("Canário M35: hazard_scan aprova spawn seguro, AKIA documental, comentário '#' e hazard-ok com razão",
               code == 0 and "0 finding(s)" in out)

        haz_bad_sup = t / "haz_badsup.ts"
        haz_bad_sup.write_text(
            "export function makeToken(secret: string) {\n"
            "  return Math.random().toString(36) + secret; // hazard-ok\n"
            "}\n",
            encoding="utf-8")
        code, out = run(MET / "hazard_scan.py", haz_bad_sup)
        expect("Canário M36: hazard_scan reprova WEAK-CRYPTO em função de token e hazard-ok sem motivo",
               code == 1 and "UNJUSTIFIED-SUPPRESSION" in out and "WEAK-CRYPTO" in out)

        haz_trav = t / "haz_trav.ts"
        haz_trav.write_text(
            "import { readFileSync } from 'fs';\n"
            "import { exec } from 'child_process';\n"
            "export async function handleReq(req: any, items: string[]) {\n"
            "  await Promise.all(items.map(x => exec(x)));\n"
            "  return readFileSync(req.query.file);\n"
            "}\n",
            encoding="utf-8")
        code, out = run(MET / "hazard_scan.py", haz_trav)
        expect("Canário M37: hazard_scan reprova PATH-TRAVERSAL e UNBOUNDED-CONCURRENCY",
               code == 1 and "PATH-TRAVERSAL" in out and "UNBOUNDED-CONCURRENCY" in out)

        # --- 38..40: coverage_ledger.py bilateral (init_ledger + verify + render-gaps) ---
        led_proj = t / "led_proj"
        (led_proj / "src").mkdir(parents=True, exist_ok=True)
        (led_proj / "src" / "a.ts").write_text("export function a(x: number) { return x + 1; }\n", encoding="utf-8")
        (led_proj / "src" / "b.ts").write_text("export function b(y: number) { return y * 2; }\n", encoding="utf-8")
        led_file = t / "ledger.json"
        init_ledger(led_proj, led_file, profile="standard")
        code, out = run(MET / "coverage_ledger.py", "verify", led_file)
        expect("Canário M38: coverage_ledger reprova unidades pending logo após init",
               code == 1 and "PENDING-UNIT" in out)

        led_data = json.loads(led_file.read_text(encoding="utf-8"))
        for u in led_data["units"]:
            u["status"] = "covered"
            u["checks"] = [{
                "agent_id": "sherlock-1",
                "method": "source",
                "invariant": "função pura sem I/O externo",
                "result": "ok",
                "assumptions": [{"text": "caller valida tipo numérico", "status": "unenforced"}],
            }]
        led_data["findings"] = [{
            "id": "F-1",
            "status": "needs_validation",
            "severity": None,
            "blockers": "WAF externo fora do repo",
            "validation_plan": "inspecionar config Cloudflare",
        }]
        led_file.write_text(json.dumps(led_data), encoding="utf-8")
        code, out = run(MET / "coverage_ledger.py", "verify", led_file)
        code_g, out_g = run(MET / "coverage_ledger.py", "render-gaps", led_file)
        expect("Canário M39: coverage_ledger aprova 100% coberto + needs_validation sem severity e renderiza gaps",
               code == 0 and code_g == 0 and "WAF externo fora do repo" in out_g and '"unenforced_assumptions": 2' in out)

        # Modifica a.ts no disco -> deve reprovar como STALE-HASH
        (led_proj / "src" / "a.ts").write_text("export function a(x: number) { return x + 999; }\n", encoding="utf-8")
        code, out = run(MET / "coverage_ledger.py", "verify", led_file)
        expect("Canário M40: coverage_ledger detecta mutação pós-auditoria (STALE-HASH)",
               code == 1 and "STALE-HASH" in out)

        # --- 41..43: contract_closure.py bilateral ---
        cc_proj = t / "cc_proj"
        (cc_proj / "src").mkdir(parents=True, exist_ok=True)
        (cc_proj / "src" / "main.ts").write_text(
            "export function buildClip() { return 1; }\n"
            "ipcMain.handle('clips:bake', () => {});\n", encoding="utf-8")
        (cc_proj / "src" / "preload.ts").write_text(
            "import { buildClip } from './main';\n"
            "export const api = () => ipcRenderer.invoke('clips:bake') + buildClip();\n", encoding="utf-8")
        spec_ok = cc_proj / "spec_ok.md"
        spec_ok.write_text(
            "# Spec\n## 4. Escopo Declarado\n- `src/main.ts`\n- `src/preload.ts`\n"
            "## 5. Pontes de Contrato\n"
            "- ponte: `src/main.ts::buildClip -> src/preload.ts`\n"
            "- ponte: `src/main.ts::\"clips:bake\" -> src/preload.ts`\n",
            encoding="utf-8")
        ok_cc, errs_cc = check_contract_closure(spec_ok, cc_proj)
        expect("Canário M41: contract_closure aprova escopo completo + ponte identificador + ponte canal IPC",
               ok_cc and not errs_cc)

        spec_bad = cc_proj / "spec_bad.md"
        spec_bad.write_text(
            "# Spec\n## 4. Escopo Declarado\n- `src/main.ts`\n- `src/esquecido.ts`\n"
            "## 5. Pontes de Contrato\n"
            "- ponte: `src/main.ts::\"clips:inexistente\" -> src/preload.ts`\n",
            encoding="utf-8")
        code, out = run(MET / "contract_closure.py", "--spec", spec_bad, "--repo", cc_proj)
        expect("Canário M42: contract_closure reprova arquivo declarado faltando e canal IPC desconectado",
               code == 1 and "MISSING-DECLARED-FILE" in out and "BROKEN-BRIDGE-STRING" in out)

        # --- 43..45: henke_router.py bilateral ---
        ROUTER = repo_root / "scripts" / "henke_router.py"
        code, out = run(ROUTER, "--prompt", "audita a segurança desse projeto e caça vulnerabilidades")
        r_sec = json.loads(out) if code == 0 else {}
        sec_skills = [s["name"] for s in r_sec.get("skills", [])]
        expect("Canário M43: henke_router seleciona henke-audit-seguranca em modo ANALISAR/SHERLOCK",
               code == 0 and r_sec.get("mode") == "ANALISAR" and r_sec.get("archetype") == "SHERLOCK"
               and "henke-audit-seguranca" in sec_skills)

        code, out = run(ROUTER, "--prompt", "como funciona um deploy na nuvem?")
        r_q = json.loads(out) if code == 0 else {}
        q_skills = [s["name"] for s in r_q.get("skills", [])]
        expect("Canário M44: henke_router veta EXECUTAR em pergunta sobre deploy e exige human_auth",
               code == 0 and r_q.get("mode") == "ANALISAR" and r_q.get("requires_human_auth") is True
               and "henke-ops-deploy" not in q_skills)

        code, out = run(ROUTER, "--prompt", "qual a receita de bolo de cenoura?")
        r_none = json.loads(out) if code == 0 else {}
        expect("Canário M45: henke_router devolve 0 skills (confidence=none, exit 0) quando fora de escopo",
               code == 0 and r_none.get("confidence") == "none" and r_none.get("skills") == [])

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
