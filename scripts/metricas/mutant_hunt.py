#!/usr/bin/env python3
"""
mutant_hunt.py — Motor AST de testes de mutação diff-scoped para Python (PROTOCOLO v4.0, §10).

Gera mutantes cirúrgicos na AST (operadores aritméticos, comparadores, constantes booleanas
e retornos não-nulos) e executa o comando de teste para cada mutante — sempre sobre uma
CÓPIA do alvo em diretório temporário (sombreada via `sitecustomize` + meta_path no
PYTHONPATH), NUNCA sobre o fonte original. Hunts paralelas não se corrompem porque o
arquivo-fonte jamais é escrito.

Veredito honesto: antes de mutar, a suíte é executada uma vez contra o código ORIGINAL.
Se ela já está vermelha (`baseline_broken`) ou estoura o timeout (`baseline_timeout`),
não existe baseline confiável — saída 2. Timeout em mutante individual é INCONCLUSIVO
(não conta como kill) e zero mutantes gerados também é inconclusivo (saída 2), nunca 100%.

Modo `--auto-diff`: em vez de `--target`, enumera sozinho os arquivos `.py`
alterados no worktree (`git diff --name-only <ref>` + não-rastreados via
`ls-files --others`) e caça mutantes em cada um — é o modo usado quando o
portão entra na matriz EXECUTAR, onde o "alvo" é justamente o diff do driver.

Códigos de saída:
  0  kill_rate >= --min-kill-rate
  1  kill_rate < --min-kill-rate (testes fracos / mutantes sobreviventes acima do tolerado)
  2  arquivo inválido, erro de sintaxe, parâmetros incorretos, suíte base quebrada,
     timeout de baseline ou zero mutantes (resultado inconclusivo)
  3  NÃO-APLICÁVEL (--auto-diff): zero arquivos .py no diff — o portão rodou e
     reportou "nada a verificar"; o runner conta 3 como n/a, nunca verde
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
import tempfile

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


def _import_module_names(t: Path) -> list[str]:
    """Nomes de import que devem resolver para o mutante (ex.: 'calc', 'pkg.calc').

    Sobe a cadeia de pacotes enquanto existir `__init__.py` para também sombrear
    a forma dotted (`pkg.sub.mod`). Para alvo `__init__.py`, sombreia o pacote.
    """
    if t.stem == "__init__":
        parts = [t.parent.name]
        parent = t.parent.parent
    else:
        parts = [t.stem]
        parent = t.parent
    names = [parts[0]]
    while parent.name and (parent / "__init__.py").is_file():
        parts.append(parent.name)
        parent = parent.parent
    if len(parts) > 1:
        names.append(".".join(reversed(parts)))
    return names


_SITE_HOOK_TEMPLATE = """\
# mutant_hunt.py — hook gerado: força resolução do alvo para o mutante no tempdir.
# Meta-path finder tem prioridade sobre QUALQUER entrada de sys.path, então o
# mutante vence mesmo se o teste fizer sys.path.insert(0, <dir_original>).
import importlib.util as _ilu
import sys as _sys

_MUTANT_FILES = {mapping!r}


class _MutantFinder(object):
    def find_spec(self, fullname, path=None, target=None):
        src = _MUTANT_FILES.get(fullname)
        if src is None:
            return None
        return _ilu.spec_from_file_location(fullname, src)


if not any(isinstance(_f, _MutantFinder) for _f in _sys.meta_path):
    _sys.meta_path.insert(0, _MutantFinder())
"""


def _run_test_cmd(test_cmd: list[str], timeout_sec: float, env: dict | None = None) -> tuple[int | None, subprocess.CompletedProcess | None]:
    """Roda a suíte uma vez. Retorna (returncode, CompletedProcess); (None, None) em timeout."""
    try:
        res = subprocess.run(
            test_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=env,
        )
        return res.returncode, res
    except subprocess.TimeoutExpired:
        return None, None


def run_mutant_hunt(
    target: Path,
    test_cmd: list[str],
    max_mutants: int = 15,
    diff_lines: set[int] | None = None,
    timeout_sec: float = 10.0,
) -> dict:
    """Executa caça a mutantes sobre `target` SEM escrever no fonte original.

    Fluxo:
      1. Coleta mutantes AST; zero mutantes -> status 'no_mutants' (inconclusivo).
      2. Baseline: roda a suíte no código ORIGINAL; se já falha -> 'baseline_broken'
         (exit!=0 em código sadio não é evidência de kill) e timeout -> 'baseline_timeout'.
      3. Cada mutante é gravado como cópia em tempdir e sombreado via
         `sitecustomize`+meta_path (PYTHONPATH), de modo que `import <modulo>`
         resolva para a cópia mutada em qualquer processo Python da suíte.
         exit != 0 -> killed; exit 0 -> survived; timeout -> inconclusive (nunca kill).
    """
    t = Path(target).resolve()
    original_bytes = t.read_bytes()
    original_text = original_bytes.decode("utf-8")
    tree = ast.parse(original_text, filename=str(t))

    mutants = _collect_mutants(tree, diff_lines=diff_lines, max_mutants=max_mutants)
    total = len(mutants)

    report: dict = {
        "target": str(target),
        "status": "ok",
        "baseline_exit": None,
        "total_mutants": total,
        "killed": 0,
        "survived": 0,
        "inconclusive": 0,
        "kill_rate": None,
        "survivors": [],
        "inconclusive_mutants": [],
    }

    if total == 0:
        report["status"] = "no_mutants"
        return report

    # Baseline anti-falso-verde: a suíte precisa passar no código ORIGINAL,
    # senão qualquer exit!=0 contaria como kill (ex.: 'python -c exit(1)' -> 100%).
    base_rc, base_res = _run_test_cmd(test_cmd, timeout_sec)
    if base_rc is None:
        report["status"] = "baseline_timeout"
        return report
    report["baseline_exit"] = base_rc
    if base_rc != 0:
        report["status"] = "baseline_broken"
        if base_res is not None:
            tail = ((base_res.stderr or "") + (base_res.stdout or ""))[-800:]
            report["baseline_output_tail"] = tail.strip()
        return report

    killed = 0
    survivors: list[dict] = []
    inconclusive: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="mutant_hunt_") as td:
        stage = Path(td)
        mutant_path = stage / t.name
        mapping = {name: str(mutant_path) for name in _import_module_names(t)}
        (stage / "sitecustomize.py").write_text(
            _SITE_HOOK_TEMPLATE.format(mapping=mapping), encoding="utf-8")

        staged_env = os.environ.copy()
        old_pp = staged_env.get("PYTHONPATH", "")
        staged_env["PYTHONPATH"] = str(stage) + (os.pathsep + old_pp if old_pp else "")

        # Se o próprio alvo aparece como argumento do test_cmd (ex.: 'python alvo.py'),
        # o token é reescrito para a cópia mutada.
        staged_cmd = []
        for tok in test_cmd:
            try:
                same_target = (Path.cwd() / tok).resolve() == t
            except OSError:
                same_target = False
            staged_cmd.append(str(mutant_path) if same_target else tok)

        for lineno, desc, mutated_src in mutants:
            mutant_path.write_bytes((mutated_src + "\n").encode("utf-8"))
            rc, _res = _run_test_cmd(staged_cmd, timeout_sec, env=staged_env)
            if rc is None:
                # Timeout: mutante pode ter criado laço infinito — inconclusivo, NÃO kill.
                inconclusive.append({"line": lineno, "mutation": desc, "status": "timeout"})
            elif rc != 0:
                killed += 1
            else:
                survivors.append({
                    "line": lineno,
                    "mutation": desc,
                    "status": "survived",
                })

    report.update({
        "killed": killed,
        "survived": len(survivors),
        "inconclusive": len(inconclusive),
        "kill_rate": round(killed / total * 100.0, 1),
        "survivors": survivors,
        "inconclusive_mutants": inconclusive,
    })
    return report


def _split_cmd(cmd_str: str) -> list[str]:
    tokens = shlex.split(cmd_str, posix=(os.name != "nt"))
    return [tok.strip("\"'") if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'" else tok for tok in tokens]


def _git_z(repo: Path, *args: str) -> list[str]:
    """Lista de paths via saída NUL-safe (`-z`) — nomes com \\n/aspas não forjam."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if res.returncode != 0:
        return []
    return [p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _auto_diff_targets(repo: Path, git_ref: str) -> list[tuple[Path, bool]]:
    """Arquivos .py do worktree diff vs `git_ref` + não-rastreados.

    Retorna [(abs_path, is_new)]. `is_new` = arquivo não-rastreado (criado pelo
    driver): sem baseline no git → caça o arquivo inteiro (diff_lines=None).
    """
    repo = repo.resolve()
    seen: set[str] = set()
    out: list[tuple[Path, bool]] = []
    for rel in _git_z(repo, "diff", "-z", "--name-only", git_ref, "--", "*.py"):
        seen.add(rel.replace("/", os.sep).lower())
        p = (repo / rel).resolve()
        if p.is_file():
            out.append((p, False))
    for rel in _git_z(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", "*.py"):
        norm = rel.replace("/", os.sep).lower()
        if norm in seen:
            continue
        p = (repo / rel).resolve()
        if p.is_file():
            out.append((p, True))
    return out


def _main_auto_diff(args: argparse.Namespace) -> int:
    """Modo matriz: caça mutantes em todo .py do diff vs `--diff-against` (default HEAD)."""
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"ERRO: --repo não é worktree git: {repo}", file=sys.stderr)
        return 2
    git_ref = args.diff_against or "HEAD"
    targets = _auto_diff_targets(repo, git_ref)
    if not targets:
        print(
            f"N/A: zero arquivos .py no diff vs {git_ref} (inclui não-rastreados) — "
            "portão não aplicável (veredito n/a, não verde).",
            file=sys.stderr,
        )
        return 3

    cmd_list = _split_cmd(args.test_cmd or "")
    if not cmd_list:
        print(
            f"ERRO: {len(targets)} alvo(s) .py no diff mas --test-cmd ausente — "
            "inconclusivo, nunca verde.",
            file=sys.stderr,
        )
        return 2

    per_file: list[dict] = []
    total = killed = survived_n = inconclusive_n = 0
    hard_inconclusive = False
    for target_path, is_new in sorted(targets, key=lambda t: str(t[0])):
        diff_lines = None if is_new else extract_diff_lines(target_path, git_ref)
        try:
            rep = run_mutant_hunt(
                target_path, cmd_list,
                max_mutants=args.max_mutants,
                diff_lines=diff_lines,
                timeout_sec=args.timeout,
            )
        except (SyntaxError, OSError, UnicodeDecodeError) as exc:
            print(f"ERRO ao processar AST de {target_path}: {exc}", file=sys.stderr)
            hard_inconclusive = True
            continue
        rep["_file"] = str(target_path)
        rep["_is_new"] = is_new
        per_file.append(rep)
        if rep.get("status") in ("baseline_broken", "baseline_timeout"):
            hard_inconclusive = True
        total += rep.get("total_mutants") or 0
        killed += rep.get("killed") or 0
        survived_n += rep.get("survived") or 0
        inconclusive_n += rep.get("inconclusive") or 0

    if args.json:
        print(json.dumps({"files": per_file,
                          "total_mutants": total, "killed": killed,
                          "survived": survived_n, "inconclusive": inconclusive_n,
                          "kill_rate": round(killed / total * 100.0, 1) if total else None},
                         ensure_ascii=False, indent=2))
    else:
        for rep in per_file:
            kr = rep.get("kill_rate")
            kr_txt = f"{kr:.1f}%" if isinstance(kr, (int, float)) else rep.get("status", "?")
            print(f"  {rep['_file']}: {rep.get('killed', 0)}/{rep.get('total_mutants', 0)} killed ({kr_txt})")
            for surv in rep.get("survivors") or []:
                print(f"    SURVIVED-MUTANT {rep['_file']}:{surv['line']}  {surv['mutation']}")
        print(f"Mutant Hunt (auto-diff): {killed}/{total} killed, "
              f"{survived_n} survived, {inconclusive_n} inconclusive em {len(per_file)} arquivo(s)")

    if hard_inconclusive:
        print("ERRO: suíte base quebrada/timeout ou AST inválida em ≥1 alvo — inconclusivo.",
              file=sys.stderr)
        return 2
    if total == 0:
        print("NÃO-VEREDITO: 0 mutantes gerados nos alvos do diff — inconclusivo.",
              file=sys.stderr)
        return 2
    min_kr = args.min_kill_rate * 100.0 if 0.0 < args.min_kill_rate <= 1.0 else args.min_kill_rate
    kill_rate = killed / total * 100.0
    return 0 if kill_rate >= min_kr else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Motor AST de testes de mutação diff-scoped para Python.")
    parser.add_argument("target_pos", nargs="?", default=None, help="Arquivo .py alvo")
    parser.add_argument("--target", dest="target_opt", default=None, help="Arquivo .py alvo (alternativo)")
    parser.add_argument("--test-cmd", default=None,
                        help="Comando de teste (ex: 'python -m unittest discover -s tests'); "
                             "obrigatório exceto quando --auto-diff encontra zero alvos (veredito n/a)")
    parser.add_argument("--auto-diff", action="store_true",
                        help="Enumera alvos sozinho: .py do `git diff --against <ref>` + não-rastreados; "
                             "zero alvos → exit 3 (n/a), sem --test-cmd")
    parser.add_argument("--repo", type=Path, default=Path("."),
                        help="Raiz do repo git no modo --auto-diff (default: cwd)")
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
    parser.add_argument("--timeout", type=float, default=10.0, help="Timeout em segundos por execução da suíte")
    parser.add_argument("--json", action="store_true", help="Saída em formato JSON")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.auto_diff:
        return _main_auto_diff(args)

    raw_target = args.target_opt or args.target_pos
    if not raw_target:
        print("ERRO: informe o arquivo Python alvo (posicional ou --target).", file=sys.stderr)
        return 2

    target_path = Path(raw_target)
    if not target_path.is_file() or target_path.suffix.lower() != ".py":
        print(f"ERRO: alvo inválido ou não é arquivo .py: {target_path}", file=sys.stderr)
        return 2

    if not args.test_cmd:
        print("ERRO: --test-cmd obrigatório sem --auto-diff.", file=sys.stderr)
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
        for inc in report["inconclusive_mutants"]:
            print(f"INCONCLUSIVE-MUTANT        {target_path}:{inc['line']}  {inc['mutation']} ({inc['status']})")
        kr = report["kill_rate"]
        kr_txt = f"{kr:.1f}%" if isinstance(kr, (int, float)) else "inconclusivo"
        print(
            f"Mutant Hunt: {report['killed']}/{report['total_mutants']} killed "
            f"({kr_txt} kill rate), {report['survived']} survived, "
            f"{report['inconclusive']} inconclusive"
        )

    status = report.get("status")
    if status == "baseline_broken":
        print(
            f"ERRO: suíte base quebrada — '{args.test_cmd}' saiu com exit "
            f"{report.get('baseline_exit')} no código ORIGINAL; kill_rate não é veredito.",
            file=sys.stderr,
        )
        return 2
    if status == "baseline_timeout":
        print(
            "ERRO: suíte base excedeu --timeout no código ORIGINAL — resultado inconclusivo.",
            file=sys.stderr,
        )
        return 2
    if status == "no_mutants" or report["total_mutants"] == 0:
        print(
            "NÃO-VEREDITO: 0 mutantes AST gerados para o alvo — resultado inconclusivo.",
            file=sys.stderr,
        )
        return 2

    min_kr = args.min_kill_rate * 100.0 if 0.0 < args.min_kill_rate <= 1.0 else args.min_kill_rate
    return 0 if report["kill_rate"] >= min_kr else 1


if __name__ == "__main__":
    sys.exit(main())
