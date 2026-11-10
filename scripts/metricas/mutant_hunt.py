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
import secrets
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

SUITE_ROOT = Path(__file__).resolve().parents[2]
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))


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


# A11 (banca v10): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG_* plantados
# no ambiente redirecionariam o git dos vereditos para outro repo/index.
# Delta-F10: strip-list módulo-level era mais fraca e NÃO filtrava PATH —
# shim `git` no repo mintava diffs. Delega para o helper canônico.
def _git_env(root: Path) -> dict:
    from scripts.state_paths import sanitized_child_env
    return sanitized_child_env(root)


def extract_diff_lines(target: Path, git_ref: str = "HEAD") -> set[int] | None:
    """Extrai o conjunto de linhas modificadas/adicionadas em `target` via `git diff -U0`."""
    t = Path(target).resolve()
    try:
        res = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "diff", "--no-ext-diff", "--no-textconv", "-U0", git_ref, "--", str(t)],
            cwd=str(t.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=_git_env(t.parent),
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

    Sobe a cadeia de pacotes enquanto existir `__init__.py`. Arquivo DENTRO de
    pacote sombreia APENAS a forma dotted (`pkg.mod`): o stem nu (`mod`) é
    absoluto em py3 e sombreá-lo roubaria imports de módulos homônimos de
    outros pacotes/site-packages (A5-banca). Para alvo `__init__.py`, sombreia
    o pacote inteiro."""
    if t.stem == "__init__":
        parts = [t.parent.name]
        parent = t.parent.parent
    else:
        parts = [t.stem]
        parent = t.parent
    while parent.name and (parent / "__init__.py").is_file():
        parts.append(parent.name)
        parent = parent.parent
    return [".".join(reversed(parts))]


_SITE_HOOK_TEMPLATE = """\
# mutant_hunt.py — hook gerado: força resolução do alvo para o mutante no tempdir.
# Meta-path finder tem prioridade sobre QUALQUER entrada de sys.path, então o
# mutante vence mesmo se o teste fizer sys.path.insert(0, <dir_original>).
import importlib.abc as _ia
import importlib.util as _ilu
import sys as _sys
from pathlib import Path as _P

# fullname -> (mutant_path, original_path, is_package, original_pkg_dir)
_MUTANT_FILES = {mapping!r}


class {cls}(object):
    def find_spec(self, fullname, path=None, target=None):
        ent = _MUTANT_FILES.get(fullname)
        if ent is None:
            return None
        src, orig, is_pkg, pkg_dir = ent
        loader = _MutantLoader(src)
        spec = _ilu.spec_from_loader(fullname, loader, origin=orig, is_package=is_pkg)
        if is_pkg:
            # submodule_search_locations aponta para o pacote ORIGINAL — sem
            # isso 'pkg' vira módulo comum e 'import pkg.mod' quebra a suíte
            # inteira → todo mutante contado como morto (C3-banca: falso-verde).
            spec.submodule_search_locations = [pkg_dir]
        return spec


class _MutantLoader(_ia.Loader):
    \"\"\"Executa o código do MUTANTE mas reporta __file__/__package__ do ORIGINAL:
    sem isso, recursos relativos a __file__ (assets ao lado do módulo) quebram
    no import e o mutante é 'morto' sem teste nenhum (A4-banca).\"\"\"

    def __init__(self, src):
        self._src = src

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        data = _P(self._src).read_bytes()
        code = compile(data, module.__spec__.origin or self._src, "exec")
        exec(code, module.__dict__)


if not any(isinstance(_f, {cls}) for _f in _sys.meta_path):
    _sys.meta_path.insert(0, {cls}())
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

    # F4 (banca v11): prefixo "mutant_hunt_" + nome fixo _MutantFinder deixavam
    # a suíte hostil DETECTAR o harness (PYTHONPATH/meta_path) e fingir kill.
    # Prefixo genérico + nome de classe aleatório por execução.
    _cls = "_I" + secrets.token_hex(8)
    with tempfile.TemporaryDirectory() as td:
        stage = Path(td)
        mutant_path = stage / t.name
        is_pkg = t.stem == "__init__"
        # pkg_dir: para __init__.py, o diretório do pacote ORIGINAL — onde os
        # submódulos vivem e para onde submodule_search_locations aponta.
        pkg_dir = str(t.parent.resolve()) if is_pkg else ""
        mapping = {
            name: (str(mutant_path), str(t.resolve()), is_pkg, pkg_dir)
            for name in _import_module_names(t)
        }
        (stage / "sitecustomize.py").write_text(
            _SITE_HOOK_TEMPLATE.format(mapping=mapping, cls=_cls), encoding="utf-8")

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


def _git_z(repo: Path, *args: str) -> list[str] | None:
    """Lista de paths via saída NUL-safe (`-z`) — nomes com \\n/aspas não forjam.

    None = falha de execução/git — distinguível de 'lista vazia legítima'.
    Confundir os dois converte repo corrompido em 'nada a verificar' (A6-banca)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo),
             "-c", "core.fsmonitor=false", "-c", "diff.external=", *args],
            capture_output=True, timeout=10, env=_git_env(repo),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return [p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p]


def _auto_diff_targets(repo: Path, git_ref: str) -> list[tuple[Path, bool]] | None:
    """Arquivos .py do worktree diff vs `git_ref` + não-rastreados.

    Retorna [(abs_path, is_new)] ou None se o git falhou (erro → exit 2, não
    n/a). `is_new` = arquivo não-rastreado (criado pelo driver): sem baseline
    no git → caça o arquivo inteiro (diff_lines=None).
    """
    repo = repo.resolve()
    seen: set[str] = set()
    out: list[tuple[Path, bool]] = []
    # `:(icase)` — FS case-insensitive abriga `EVIL.PY` que é Python e escapa
    # ao pathspec `*.py` case-sensitive (banca v7 — D1).
    diff_list = _git_z(repo, "diff", "-z", "--name-only", git_ref, "--", ":(icase)*.py")
    others_list = _git_z(repo, "ls-files", "-z", "--others", "--exclude-standard", "--", ":(icase)*.py")
    if diff_list is None or others_list is None:
        return None
    def _in_repo(p: Path) -> bool:
        return repo in (p, *p.parents)

    for rel in diff_list:
        p = (repo / rel).resolve()
        if not p.is_file() or not _in_repo(p):
            continue
        seen.add(os.path.normcase(str(p)))
        out.append((p, False))
    for rel in others_list:
        p = (repo / rel).resolve()
        norm = os.path.normcase(str(p))
        if norm in seen or not p.is_file() or not _in_repo(p):
            continue
        out.append((p, True))
    return out


def _main_auto_diff(args: argparse.Namespace) -> int:
    """Modo matriz: caça mutantes em todo .py do diff vs `--diff-against` (default HEAD)."""
    repo = Path(args.repo).resolve()
    if not (repo / ".git").exists():
        print(f"ERRO: --repo não é worktree git: {repo}", file=sys.stderr)
        return 2
    git_ref = args.diff_against or "HEAD"
    if git_ref.startswith("-") or not git_ref.strip():
        print(f"ERRO: --diff-against inválido: {git_ref!r}", file=sys.stderr)
        return 2
    targets = _auto_diff_targets(repo, git_ref)
    if targets is None:
        print(
            f"ERRO: git falhou ao enumerar o diff vs {git_ref} — inconclusivo "
            "(erro de infraestrutura ≠ 'nada a verificar').",
            file=sys.stderr,
        )
        return 2
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
        # Todos os alvos parsearam e a baseline passou, mas as linhas do diff
        # não têm nós mutáveis (só docstring/comentário/import). É o veredito
        # n/a honesto: não há superfície mutável a verificar — nem verde nem
        # vermelho, e a run ainda exige ≥1 verde real de outro portão.
        print("N/A: 0 mutantes gerados nos alvos do diff — nada mutável a "
              "verificar (veredito n/a, não verde).", file=sys.stderr)
        return 3
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
    # A13 (banca v10): default 0.0 tornava `kill_rate >= min` tautologicamente
    # verde sem a flag — portão que sempre passa não é portão.
    parser.add_argument("--min-kill-rate", type=float, default=None, help="Taxa mínima de abate em %% (ex: 80.0 ou 0.80) — obrigatória")
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
        return exc.code if isinstance(exc.code, int) and exc.code != 0 else 2

    if args.min_kill_rate is None:
        print("ERRO: --min-kill-rate é obrigatória — portão sem critério de "
              "abate é verde tautológico.", file=sys.stderr)
        return 2
    # F29 (banca v11): <=0 também é tautológico — kill_rate >= 0 sempre vale.
    if args.min_kill_rate <= 0:
        print("ERRO: --min-kill-rate deve ser > 0 — critério <= 0 é verde "
              "tautológico.", file=sys.stderr)
        return 2

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

    # --diff-against vira argumento do `git diff` — sem validação, uma ref
    # `--output=C:/x` injeta flag e grava patch em caminho arbitrário (F-07).
    if args.diff_against:
        if args.diff_against.startswith("-"):
            print(f"ERRO: --diff-against inválida (parece flag): {args.diff_against!r}",
                  file=sys.stderr)
            return 2
        _v = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet",
             f"{args.diff_against}^{{commit}}"],
            cwd=str(target_path.parent), capture_output=True, timeout=10,
            env=_git_env(target_path.parent),
        )
        if _v.returncode != 0:
            # Modo single-target: a ref só ESTREITA o escopo do arquivo alvo
            # já nomeado — fora de um repo (ou ref inexistente) degrada para
            # arquivo inteiro com aviso. Injeção de flag segue bloqueada acima;
            # o caminho de portão (--auto-diff) mantém INVALID-AGAINST rígido.
            print(f"AVISO: --diff-against não resolve neste diretório: "
                  f"{args.diff_against!r} — caçando o arquivo inteiro.", file=sys.stderr)
            args.diff_against = None
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
