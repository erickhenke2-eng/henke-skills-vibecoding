#!/usr/bin/env python3
"""repo_signals.py — coletores de sinais do repositório para os portões de scripts/metricas/.

Separa "medir" de "julgar": este script só produz TSV/JSON; o veredito é dos portões.

  churn   git log --numstat -> TSV "commit<TAB>arquivos_tocados" (entrada do tornado_scan)
  metrics AST stdlib      -> TSV por função "func<TAB>complexidade<TAB>cov%" (crap_score)
                           ou --per-file "path<TAB>worst_crap<TAB>max_comp<TAB>cov%" (ratchet)
  edges   AST imports     -> edges.json [{"from","to"}] só arestas internas (fence_check)

Complexidade = aproximação honesta de McCabe via AST (1 + pontos de decisão);
não é idêntica ao radon — declare isso no relatório. Cobertura: coverage.py JSON
por ARQUIVO aplicada às funções do arquivo (aproximação declarada); sem coverage
o padrão é 0 (pior caso), nunca um número fabricado.

Exit: 0 ok · 2 uso/IO/entrada malformada (fail closed; nunca fabrica sinal).
"""
import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".henke", ".worktrees"}


def _fail(msg):
    print(f"SPEC ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def iter_py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


def relposix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def complexity_of(func: ast.AST) -> int:
    """1 + pontos de decisão (aproximação de McCabe via AST)."""
    score = 1
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp,
                             ast.ExceptHandler, ast.Assert, ast.MatchAs, ast.MatchValue)):
            score += 1
        elif isinstance(node, ast.BoolOp):
            score += len(node.values) - 1
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            score += sum(1 + len(gen.ifs) for gen in node.generators)
        elif isinstance(node, ast.Match):
            score += len(node.cases)
    return score


def funcs_in_file(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as e:
        print(f"WARN: {path}: {e} (pulado)", file=sys.stderr)
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def load_coverage(json_path):
    """coverage.py json: {"files": {"path": {"summary": {"percent_covered": N}}}}.
    Retorna dict relpath_normalizado -> pct."""
    if not json_path:
        return {}
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _fail(f"coverage json ilegível: {e}")
    out = {}
    for fpath, fdata in (data.get("files") or {}).items():
        pct = (fdata.get("summary") or {}).get("percent_covered")
        if isinstance(pct, (int, float)):
            out[Path(fpath).as_posix()] = float(pct)
    return out


def cmd_churn(args):
    order_flag = "" if args.order == "newest-first" else "--reverse"
    cmd = ["git", "-C", args.repo, "log", "--numstat", "--format=__C__%H"]
    if order_flag:
        cmd.append(order_flag)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        _fail(f"git log falhou: {res.stderr.strip()[:200]}")
    sha, count = None, 0
    for line in res.stdout.splitlines():
        if line.startswith("__C__"):
            if sha is not None:
                print(f"{sha[:12]}\t{count}")
            sha, count = line[5:].strip(), 0
        elif line.strip():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].strip("-").isdigit():
                count += 1
    if sha is not None:
        print(f"{sha[:12]}\t{count}")
    return 0


def cmd_metrics(args):
    root = Path(args.root).resolve()
    cov = load_coverage(args.coverage_json)
    per_file = {}
    for py in iter_py_files(root):
        rel = relposix(py, root)
        pct = cov.get(rel, cov.get(py.as_posix(), 0.0))
        worst_crap, max_comp = 0.0, 0
        for fn in funcs_in_file(py):
            comp = complexity_of(fn)
            crap = comp ** 2 * (1 - pct / 100) ** 3 + comp
            if args.per_file:
                worst_crap, max_comp = max(worst_crap, crap), max(max_comp, comp)
            else:
                print(f"{rel}::{fn.name}:{fn.lineno}\t{comp}\t{pct:.0f}")
        if args.per_file:
            per_file[rel] = (worst_crap, max_comp, pct)
    if args.per_file:
        for rel, (wc, mc, pct) in sorted(per_file.items()):
            print(f"{rel}\t{wc:.2f}\t{mc}\t{pct:.0f}")
    return 0


def resolve_import(mod_parts, base_dir: Path, level: int, root: Path):
    """Resolve import para arquivo interno; None se externo/inexistente."""
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


def cmd_edges(args):
    root = Path(args.root).resolve()
    edges = []
    for py in iter_py_files(root):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [(n.name.split("."), 0) for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    targets = [(node.module.split("."), node.level)]
                elif node.level:  # "from . import x" → resolve names como módulos
                    targets = [(n.name.split("."), node.level) for n in node.names]
            for parts, level in targets:
                for base in (py.parent, root):
                    hit = resolve_import(list(parts), base, max(level, base != py.parent), root)
                    if hit and root in hit.parents:
                        edges.append({"from": relposix(py, root), "to": relposix(hit, root)})
                        break
    seen, out = set(), []
    for e in edges:
        k = (e["from"], e["to"])
        if k not in seen:
            seen.add(k)
            out.append(e)
    print(json.dumps(out, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("churn", help="git numstat -> TSV commit<TAB>arquivos")
    p.add_argument("--repo", default=".")
    p.add_argument("--order", choices=("oldest-first", "newest-first"), required=True)
    p.set_defaults(fn=cmd_churn)

    p = sub.add_parser("metrics", help="AST -> TSV complexidade/coverage")
    p.add_argument("--root", default=".")
    p.add_argument("--per-file", action="store_true", help="emite linhas no formato do ratchet")
    p.add_argument("--coverage-json", default=None, help="coverage.py json (opcional)")
    p.set_defaults(fn=cmd_metrics)

    p = sub.add_parser("edges", help="AST imports -> edges.json (internas)")
    p.add_argument("--root", default=".")
    p.set_defaults(fn=cmd_edges)

    args = ap.parse_args()
    try:
        return args.fn(args)
    except BrokenPipeError:
        sys.exit(2)  # stdout morto: não-veredito, nunca verde falso


if __name__ == "__main__":
    sys.exit(main())
