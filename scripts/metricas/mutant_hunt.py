#!/usr/bin/env python3
"""
mutant_hunt.py — Motor AST de testes de mutação diff-scoped para Python (PROTOCOLO v4.0, §10).

Gera mutantes cirúrgicos na AST (operadores aritméticos, comparadores, constantes booleanas
e retornos não-nulos), executa o comando de teste para cada mutante e restaura os bytes
originais do arquivo-alvo de forma garantida via bloco `try ... finally`.

Códigos de saída:
  0  kill_rate >= --min-kill-rate
  1  kill_rate < --min-kill-rate (testes fracos / mutantes sobreviventes acima do tolerado)
  2  arquivo inválido, erro de sintaxe ou parâmetros incorretos
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

_BINOP_SWAPS: dict[type[ast.operator], tuple[type[ast.operator], str]] = {
    ast.Add: (ast.Sub, "BinOp(+ -> -)"),
    ast.Sub: (ast.Add, "BinOp(- -> +)"),
    ast.Mult: (ast.FloorDiv, "BinOp(* -> //)"),
    ast.FloorDiv: (ast.Mult, "BinOp(// -> *)"),
}

_CMPOP_SWAPS: dict[type[ast.cmpop], tuple[type[ast.cmpop], str]] = {
    ast.Eq: (ast.NotEq, "Compare(== -> !=)"),
    ast.NotEq: (ast.Eq, "Compare(!= -> ==)"),
    ast.Lt: (ast.GtE, "Compare(< -> >=)"),
    ast.GtE: (ast.Lt, "Compare(>= -> <)"),
    ast.Gt: (ast.LtE, "Compare(> -> <=)"),
    ast.LtE: (ast.Gt, "Compare(<= -> >)"),
}


def _is_main_guard(node: ast.Compare) -> bool:
    if isinstance(node.left, ast.Name) and node.left.id == "__name__":
        return True
    for comp in node.comparators:
        if isinstance(comp, ast.Constant) and comp.value == "__main__":
            return True
    return False


def _collect_mutants(tree: ast.AST, diff_lines: set[int] | None, max_mutants: int) -> list[tuple[int, str, str]]:
    """Retorna lista de (lineno, description, mutated_source) limitada a max_mutants."""
    candidates: list[tuple[int, str, str]] = []

    # Enumera nós em ordem determinística (pré-ordem)
    all_nodes = list(ast.walk(tree))
    for idx, node in enumerate(all_nodes):
        if len(candidates) >= max_mutants:
            break
        lineno = getattr(node, "lineno", None)
        if lineno is None:
            continue
        if diff_lines is not None and lineno not in diff_lines:
            continue

        if isinstance(node, ast.BinOp) and type(node.op) in _BINOP_SWAPS:
            new_op_cls, desc = _BINOP_SWAPS[type(node.op)]
            cloned = copy.deepcopy(tree)
            target_node = list(ast.walk(cloned))[idx]
            if isinstance(target_node, ast.BinOp):
                target_node.op = new_op_cls()
                ast.fix_missing_locations(cloned)
                candidates.append((lineno, desc, ast.unparse(cloned)))

        elif isinstance(node, ast.Compare) and node.ops and not _is_main_guard(node):
            first_op = node.ops[0]
            if type(first_op) in _CMPOP_SWAPS:
                new_cmp_cls, desc = _CMPOP_SWAPS[type(first_op)]
                cloned = copy.deepcopy(tree)
                target_node = list(ast.walk(cloned))[idx]
                if isinstance(target_node, ast.Compare) and target_node.ops:
                    target_node.ops[0] = new_cmp_cls()
                    ast.fix_missing_locations(cloned)
                    candidates.append((lineno, desc, ast.unparse(cloned)))

        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            new_val = not node.value
            desc = f"Constant({node.value} -> {new_val})"
            cloned = copy.deepcopy(tree)
            target_node = list(ast.walk(cloned))[idx]
            if isinstance(target_node, ast.Constant):
                target_node.value = new_val
                ast.fix_missing_locations(cloned)
                candidates.append((lineno, desc, ast.unparse(cloned)))

        elif isinstance(node, ast.Return) and node.value is not None:
            if isinstance(node.value, ast.Constant) and node.value.value is None:
                continue
            desc = "Return(expr -> None)"
            cloned = copy.deepcopy(tree)
            target_node = list(ast.walk(cloned))[idx]
            if isinstance(target_node, ast.Return):
                target_node.value = ast.Constant(value=None)
                ast.fix_missing_locations(cloned)
                candidates.append((lineno, desc, ast.unparse(cloned)))

    return candidates


def extract_diff_lines(target: Path, git_ref: str = "HEAD") -> set[int] | None:
    """Extrai o conjunto de linhas modificadas/adicionadas em `target` via `git diff -U0`."""
    t = Path(target).resolve()
    try:
        res = subprocess.run(
            ["git", "diff", "-U0", git_ref, "--", str(t)],
            cwd=str(t.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return None
        lines: set[int] = set()
        for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", res.stdout, flags=re.M):
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            for ln in range(start, start + max(count, 1)):
                lines.add(ln)
        return lines if lines else None
    except Exception:
        return None


def run_mutant_hunt(
    target: Path,
    test_cmd: list[str],
    max_mutants: int = 15,
    diff_lines: set[int] | None = None,
    timeout_sec: float = 10.0,
) -> dict:
    """Executa caça a mutantes sobre `target` restaurando os bytes originais em `finally`."""
    t = Path(target).resolve()
    original_bytes = t.read_bytes()
    original_text = original_bytes.decode("utf-8")
    tree = ast.parse(original_text, filename=str(t))

    mutants = _collect_mutants(tree, diff_lines=diff_lines, max_mutants=max_mutants)
    total = len(mutants)
    killed = 0
    survivors: list[dict] = []

    try:
        for lineno, desc, mutated_src in mutants:
            try:
                t.write_bytes((mutated_src + "\n").encode("utf-8"))
                try:
                    res = subprocess.run(
                        test_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout_sec,
                    )
                    if res.returncode != 0:
                        killed += 1
                    else:
                        survivors.append({
                            "line": lineno,
                            "mutation": desc,
                            "status": "survived",
                        })
                except subprocess.TimeoutExpired:
                    # Timeout infinito causado pela mutação conta como mutante detectado/morto
                    killed += 1
            finally:
                t.write_bytes(original_bytes)
    finally:
        t.write_bytes(original_bytes)

    kill_rate = (killed / total * 100.0) if total > 0 else 100.0
    return {
        "target": str(target),
        "total_mutants": total,
        "killed": killed,
        "survived": len(survivors),
        "kill_rate": round(kill_rate, 1),
        "survivors": survivors,
    }


def _split_cmd(cmd_str: str) -> list[str]:
    tokens = shlex.split(cmd_str, posix=(os.name != "nt"))
    return [tok.strip("\"'") if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok for tok in tokens]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Motor AST de testes de mutação diff-scoped para Python.")
    parser.add_argument("target_pos", nargs="?", default=None, help="Arquivo .py alvo")
    parser.add_argument("--target", dest="target_opt", default=None, help="Arquivo .py alvo (alternativo)")
    parser.add_argument("--test-cmd", required=True, help="Comando de teste (ex: 'python -m unittest discover -s tests')")
    parser.add_argument("--max-mutants", type=int, default=15, help="Máximo de mutantes gerados (default: 15)")
    parser.add_argument("--min-kill-rate", type=float, default=0.0, help="Taxa mínima de abate em %% (ex: 80.0 ou 0.80)")
    parser.add_argument(
        "--diff-against",
        "--diff",
        dest="diff_against",
        nargs="?",
        const="HEAD",
        default=None,
        help="Ref Git para filtrar linhas do diff (default se sem valor: HEAD)",
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout em segundos por mutante")
    parser.add_argument("--json", action="store_true", help="Saída em formato JSON")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    raw_target = args.target_opt or args.target_pos
    if not raw_target:
        print("ERRO: informe o arquivo Python alvo (posicional ou --target).", file=sys.stderr)
        return 2

    target_path = Path(raw_target)
    if not target_path.is_file() or target_path.suffix.lower() != ".py":
        print(f"ERRO: alvo inválido ou não é arquivo .py: {target_path}", file=sys.stderr)
        return 2

    cmd_list = _split_cmd(args.test_cmd)
    if not cmd_list:
        print("ERRO: --test-cmd vazio.", file=sys.stderr)
        return 2

    diff_lines = extract_diff_lines(target_path, args.diff_against) if args.diff_against else None

    try:
        report = run_mutant_hunt(
            target_path,
            cmd_list,
            max_mutants=args.max_mutants,
            diff_lines=diff_lines,
            timeout_sec=args.timeout,
        )
    except (SyntaxError, OSError, UnicodeDecodeError) as exc:
        print(f"ERRO ao processar AST de {target_path}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for surv in report["survivors"]:
            print(f"SURVIVED-MUTANT            {target_path}:{surv['line']}  {surv['mutation']}")
        print(
            f"Mutant Hunt: {report['killed']}/{report['total_mutants']} killed "
            f"({report['kill_rate']:.1f}% kill rate), {report['survived']} survived"
        )

    min_kr = args.min_kill_rate * 100.0 if 0.0 < args.min_kill_rate <= 1.0 else args.min_kill_rate
    return 0 if report["kill_rate"] >= min_kr else 1


if __name__ == "__main__":
    sys.exit(main())
