#!/usr/bin/env python3
"""repo_signals.py — coletores universais de sinais do repositório (py/ts/tsx/js/mjs/rs/go).

Separa "medir" de "julgar": este script só produz TSV/JSON; o veredito é dos portões.

  churn   git log --no-merges --numstat -> TSV "commit<TAB>arquivos_tocados"
          (binários "-\t-" contam; commit com 0 arquivos nunca é emitido)
  metrics funções -> TSV "func<TAB>complexidade<TAB>cov%" (crap_score)
          --per-file -> "path<TAB>worst_crap<TAB>max_comp<TAB>cov%" (ratchet)
          .py via ast; .ts/.tsx/.js/.mjs/.rs/.go via lexer polyglot (aproximação
          declarada). Ignora .d.ts e arquivos de teste.
  edges   imports -> edges.json [{"from","to"}] só arestas internas (fence_check)
          py (ast) + js/ts (relativo, ESM .js->.ts, workspace packages) + rs + go

Cobertura (--coverage PATH): detecta formato automaticamente —
  coverage.py json (files->summary->percent_covered),
  Istanbul coverage-summary.json (lines.pct/statements.pct por arquivo),
  coverage-final.json (mapa 's' de execução por statement),
  lcov.info (SF:/LF:/LH:). Por ARQUIVO aplicado às funções do arquivo.
  Sem coverage o padrão é 0 (pior caso declarado), nunca número fabricado.

Exit: 0 ok · 2 uso/IO/entrada malformada (fail closed; nunca fabrica sinal).
"""
import argparse
import ast
import json
import math
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except OSError:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metricas import polyglot as pg  # noqa: E402

PY_EXT = {".py"}
ALL_EXTS = PY_EXT | pg.C_EXTS | {".d.ts"}


def _fail(msg):
    print(f"SPEC ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def iter_files(root: Path):
    yield from pg.iter_source_files(root, ALL_EXTS)


def relposix(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def complexity_py(func: ast.AST) -> int:
    """1 + pontos de decisão (aproximação de McCabe via AST)."""
    score = 1
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp,
                             ast.ExceptHandler, ast.Assert)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(gen.ifs) for gen in node.generators)
        elif isinstance(node, ast.Match):
            score += len(node.cases)
    return score


def funcs_in_py(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as e:
        # SKIP-PARSE: marker que o gate_pipeline traduz em falha (grafo incompleto)
        print(f"SKIP-PARSE: {path}: {e} (pulado)", file=sys.stderr)
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def iter_functions(path: Path):
    """yield (nome, lineno, complexidade) em qualquer linguagem suportada."""
    ext = path.suffix.lower()
    if ext == ".py":
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as e:
            print(f"SKIP-PARSE: {path}: {e} (pulado)", file=sys.stderr)
            return
        for fn in funcs_in_py(path):
            yield fn.name, fn.lineno, complexity_py(fn)
        # A5 (banca v9): decisões FORA de função (top-level do módulo ou corpo
        # de classe) contam — inlining do corpo de uma função no módulo não
        # pode zerar a métrica para enganar o ratchet.
        for stmt in tree.body:
            subs = stmt.body if isinstance(stmt, ast.ClassDef) else [stmt]
            for sub in subs:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                c = complexity_py(sub)
                if c > 1:
                    yield "<top-level>", sub.lineno, c
    elif ext in pg.C_EXTS:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"SKIP-PARSE: {path}: {e} (pulado)", file=sys.stderr)
            return
        lang = ".rs" if ext == ".rs" else ".go" if ext == ".go" else ext
        func_rows = list(pg.find_functions(text, ext))
        fsum = 0
        for name, sl, el, body in func_rows:
            c = pg.complexity_masked(body, ext)
            fsum += c - 1
            yield name, sl, c
        # Mesmo princípio: decisões fora de função em C-like não podem ser
        # invisíveis (aproximação: decisões totais − decisões das funções).
        top_dec = max(0, (pg.complexity_masked(text, ext) - 1) - fsum)
        if top_dec > 0:
            yield "<top-level>", 1, top_dec + 1


def cmd_churn(args):
    cmd = ["git", "-C", args.repo,
           "-c", "core.fsmonitor=false", "-c", "diff.external=",
           "log", "--no-merges", "--numstat",
           "--format=__C__%H"]
    if args.order == "oldest-first":
        cmd.append("--reverse")
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60,
                         # A19 (banca v12): GIT_* herdado lia outro repo.
                         env={k: v for k, v in os.environ.items()
                              if not k.startswith("GIT_")})
    if res.returncode != 0:
        _fail(f"git log falhou: {res.stderr.strip()[:200]}")
    sha, count = None, 0
    for line in res.stdout.splitlines():
        if line.startswith("__C__"):
            if sha is not None and count > 0:
                print(f"{sha[:12]}\t{count}")
            elif sha is not None:
                print(f"WARN: commit {sha[:12]} com 0 arquivos omitido",
                      file=sys.stderr)
            sha, count = line[5:].strip(), 0
        elif line.strip():
            parts = line.split("\t")
            if len(parts) >= 3:
                # "1\t2\tf" texto ou "-\t-\tf.png" binário: ambos contam
                count += 1
    if sha is not None and count > 0:
        print(f"{sha[:12]}\t{count}")
    return 0


def load_coverage(path):
    """Auto-detecta coverage.py json / Istanbul summary+final / lcov.info."""
    if not path:
        return {}
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        _fail(f"coverage ilegível: {e}")
    text = raw.decode("utf-8-sig", errors="replace")
    out = {}
    if p.suffix == ".info" or text.lstrip().startswith(("TN:", "SF:")):
        cur = None
        for line in text.splitlines():
            if line.startswith("SF:"):
                cur = line[3:].strip()
            elif line.startswith("LF:") and cur:
                try:
                    lf = int(line[3:].strip() or 0)
                except ValueError:
                    lf = 0
            elif line.startswith("LH:") and cur:
                try:
                    lh = int(line[3:].strip() or 0)
                except ValueError:
                    lh = 0
                if lf > 0:
                    out[Path(cur).as_posix()] = max(0.0, min(100.0, round(100 * lh / lf, 2)))
                cur = None
        return out
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        _fail("coverage: formato não reconhecido (esperado json ou lcov.info)")
    # coverage-final.json: {path: {"s": {id: count}, "statementMap": {...}}}
    if isinstance(data, dict) and all(
            isinstance(v, dict) and "s" in v for v in list(data.values())[:5]):
        for fpath, fdata in data.items():
            s = fdata.get("s") or {}
            if s:
                covered = sum(1 for v in s.values() if v)
                out[Path(fpath).as_posix()] = round(100 * covered / len(s), 2)
        return out
    for fpath, fdata in (data.get("files") or data).items():
        if not isinstance(fdata, dict):
            continue
        # coverage.py: summary.percent_covered
        pct = (fdata.get("summary") or {}).get("percent_covered")
        if pct is None:
            # coverage-summary.json: lines.pct / statements.pct
            pct = (fdata.get("lines") or fdata.get("statements") or {}).get("pct")
        if isinstance(pct, bool):
            continue
        if isinstance(pct, (int, float)):
            # NaN/Infinity (aceitos por json.loads) envenenariam todo cálculo
            # downstream de cobertura — rejeita e clampa a faixa válida.
            if not math.isfinite(pct):
                continue
            key = Path(fpath).as_posix()
            if fpath == "total":
                continue
            out[key] = max(0.0, min(100.0, float(pct)))
    return out


def _cov_lookup(cov: dict, rel: str, py: Path) -> float:
    """Match de coverage por caminho: rel exato → absoluto → sufixo '/'+rel →
    basename só se for único (index.ts de monorepo não pode colidir)."""
    if not cov:
        return 0.0
    if rel in cov:
        return cov[rel]
    ap = py.as_posix()
    if ap in cov:
        return cov[ap]
    hits = [v for k, v in cov.items() if k.endswith("/" + rel)]
    # A10 (banca v10): sufixo ambíguo (2+ hits) creditava o hits[0]
    # arbitrário — sem match único não há crédito.
    if len(hits) == 1:
        return hits[0]
    by_name = [v for k, v in cov.items()
               if k == py.name or k.endswith("/" + py.name)]
    return by_name[0] if len(by_name) == 1 else 0.0


def cmd_metrics(args):
    root = Path(args.root).resolve()
    cov = load_coverage(args.coverage)
    per_file = {}
    for py in iter_files(root):
        if py.name.endswith(".d.ts"):
            continue
        rel = relposix(py, root)
        # TEST_RE sobre o caminho RELATIVO ("/"+rel): aplicar no absoluto
        # contaminava tudo sob /tmp/test/... com 0 funções.
        if pg.TEST_RE.search("/" + rel):
            continue
        pct = _cov_lookup(cov, rel, py)
        worst_crap, max_comp = 0.0, 0
        for name, lineno, comp in iter_functions(py):
            crap = comp ** 2 * (1 - pct / 100) ** 3 + comp
            if args.per_file:
                worst_crap, max_comp = max(worst_crap, crap), max(max_comp, comp)
            else:
                print(f"{rel}::{name}:{lineno}\t{comp}\t{pct:.0f}")
        if args.per_file:
            per_file[rel] = (worst_crap, max_comp, pct)
    if args.per_file:
        for rel, (wc, mc, pct) in sorted(per_file.items()):
            print(f"{rel}\t{wc:.2f}\t{mc}\t{pct:.0f}")
    return 0


def resolve_py(mod_parts, base_dir: Path, level: int):
    target = base_dir
    for _ in range(max(level - 1, 0)):
        target = target.parent
    for cand in (target.joinpath(*mod_parts).with_suffix(".py"),
                 target.joinpath(*mod_parts, "__init__.py")):
        try:
            if cand.is_file():
                return cand.resolve()
        except OSError:
            pass
    return None


def _py_dynamic_imports(tree) -> list[str]:
    """Detecta `__import__(...)` e `*.import_module(...)` — grafo estático não cobre."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = ""
        if isinstance(f, ast.Name):
            name = f.id
        elif isinstance(f, ast.Attribute):
            name = f.attr
        if name in ("__import__", "import_module"):
            hits.append(name)
    return hits


def _abs_looks_internal(root: Path, first: str) -> bool:
    """True se o 1º componente de um import absoluto existe no repo."""
    return (root / first).is_dir() or (root / f"{first}.py").is_file()


def py_edges(py: Path, tree, root: Path, edges, problems=None):
    rel = relposix(py, root)
    if problems is not None:
        for dyn in _py_dynamic_imports(tree):
            problems.append((rel, f"dynamic_import:{dyn}"))
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Import):
            targets = [(n.name.split("."), 0) for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                targets = [(node.module.split("."), node.level)]
            elif node.level:
                targets = [(n.name.split("."), node.level) for n in node.names]
        for parts, level in targets:
            resolved = None
            for base in (py.parent, root):
                lv = max(level, base != py.parent)
                hit = resolve_py(list(parts), base, lv)
                # from pkg import mod: tenta pkg/mod.py antes de pkg/__init__.py
                if isinstance(node, ast.ImportFrom) and hit:
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        sub = resolve_py(list(parts) + [alias.name], base, lv)
                        if sub and root in sub.parents:
                            hit = sub
                            break
                if hit and root in hit.parents:
                    resolved = hit
                    break
            if resolved is not None:
                edges.append((rel, relposix(resolved, root)))
            elif problems is not None and level > 0:
                problems.append((rel, f"unresolved_relative:{'.' * level}{'.'.join(parts)}"))
            elif problems is not None and parts and _abs_looks_internal(root, parts[0]):
                problems.append((rel, f"unresolved_internal:{'.'.join(parts)}"))


def build_dependency_edges(root: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Extrai arestas internas de dependência + problemas por arquivo.

    Retorna `(edges, problems)`:
    - `edges`: lista deduplicada de `(from_rel, to_rel)` POSIX relativos a root.
    - `problems`: lista de `(rel, reason)` — import interno/relativo não
      resolvido, import dinâmico, erro de leitura ou de parse. Consumidores
      conservadores tratam qualquer entrada como incerteza total.
    """
    root = Path(root).resolve()
    pkgmap = pg.build_pkgmap(root)
    gomod = pg.go_module_path(root)
    seen, edges, problems = set(), [], []
    for f in iter_files(root):
        ext = f.suffix.lower()
        rel = relposix(f, root)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            problems.append((rel, f"read_error:{type(e).__name__}"))
            continue
        raw_edges = []
        if ext == ".py":
            try:
                tree = ast.parse(text, filename=str(f))
            except SyntaxError as e:
                problems.append((rel, f"syntax_error:{e.lineno or 0}"))
                continue
            py_edges(f, tree, root, raw_edges, problems)
        elif ext in pg.JS_EXTS:
            for spec in pg.find_imports(text, ext):
                hit = pg.resolve_js(spec, f.parent, root, pkgmap)
                if hit and root in hit.parents:
                    raw_edges.append((rel, relposix(hit, root)))
                elif spec.startswith(("./", "../")):
                    problems.append((rel, f"unresolved_relative:{spec}"))
        elif ext == ".rs":
            crate = pg.cargo_root(f, root)
            for spec in pg.find_imports(text, ext):
                internal = spec.startswith(("mod::", "crate::", "self::", "super::"))
                if spec.startswith("mod::"):
                    name = spec[5:]
                    cands = [f.parent / f"{name}.rs", f.parent / name / "mod.rs"]
                else:
                    parts = spec.split("::")[1:]
                    cands = [crate.joinpath(*parts).with_suffix(".rs"),
                             crate.joinpath(*parts, "mod.rs"),
                             crate.joinpath("src", *parts).with_suffix(".rs")]
                    # 'use crate::a::b::Simbolo' → cai pro módulo pai a/b.rs
                    if len(parts) > 1:
                        cands += [crate.joinpath(*parts[:-1]).with_suffix(".rs"),
                                  crate.joinpath(*parts[:-1], "mod.rs"),
                                  crate.joinpath("src", *parts[:-1]).with_suffix(".rs")]
                hit = None
                for c in cands:
                    if c.is_file():
                        hit = c.resolve()
                        break
                if hit is not None and root in hit.parents:
                    raw_edges.append((rel, relposix(hit, root)))
                elif internal:
                    problems.append((rel, f"unresolved_internal:{spec}"))
        elif ext == ".go":
            for spec in pg.find_imports(text, ext):
                rel_spec = spec[len(gomod):].lstrip("/") if gomod and spec.startswith(gomod) else \
                    spec.lstrip("./") if spec.startswith("./") else None
                if rel_spec:
                    d = (root / rel_spec) if not spec.startswith("./") else (f.parent / rel_spec)
                    if d.is_dir():
                        found = False
                        for g in sorted(d.glob("*.go")):
                            if not g.name.endswith("_test.go"):
                                raw_edges.append((rel, relposix(g.resolve(), root)))
                                found = True
                                break
                        if not found:
                            problems.append((rel, f"unresolved_internal:{spec}"))
                    elif spec.startswith(("./", gomod)):
                        problems.append((rel, f"unresolved_internal:{spec}"))
        for e in raw_edges:
            if e not in seen and e[0] != e[1]:
                seen.add(e)
                edges.append(e)
    return edges, problems


def cmd_edges(args):
    root = Path(args.root).resolve()
    edges, problems = build_dependency_edges(root)
    # problems seguem só no canal Python (build_dependency_edges) — stdout é
    # JSON puro de edges para coletores (gate_pipeline/canários) parsearem.
    print(json.dumps([{"from": a, "to": b} for a, b in edges], indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("churn", help="git numstat -> TSV commit<TAB>arquivos")
    p.add_argument("--repo", default=".")
    p.add_argument("--order", choices=("oldest-first", "newest-first"), required=True)
    p.set_defaults(fn=cmd_churn)

    p = sub.add_parser("metrics", help="funções -> TSV complexidade/coverage")
    p.add_argument("--root", default=".")
    p.add_argument("--per-file", action="store_true", help="linhas no formato ratchet")
    p.add_argument("--coverage", "--coverage-json", "--lcov", dest="coverage",
                   default=None,
                   help="coverage.py json | coverage-summary.json | coverage-final.json | lcov.info")
    p.set_defaults(fn=cmd_metrics)

    p = sub.add_parser("edges", help="imports -> edges.json (internas)")
    p.add_argument("--root", default=".")
    p.set_defaults(fn=cmd_edges)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except BrokenPipeError:
        sys.exit(2)


if __name__ == "__main__":
    sys.exit(main())
