"""test_impact.py — Test Impact Analysis conservativa por arquivo (Wave 7, R3).

Seleciona arquivos de teste a partir do diff (`git diff --name-status
--diff-filter=ACDMRTUXB -M <base> --` + untracked). Regras conservadoras:
teste alterado seleciona a si mesmo; teste é selecionado quando qualquer
arquivo alterado pertence ao fechamento de suas dependências diretas. Arquivo
de produção alterado fora da união dos fechamentos, arquivo desconhecido,
mudança não-Python/config/lock/workflow, deleção/renome, import dinâmico,
sintaxe inválida, fixture compartilhada ou qualquer incerteza ⇒ `mode="full"`.
Nenhuma seleção sob incerteza; `--full` (CI/pré-merge) ignora a otimização.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

import scripts.winpath as winpath

_DIFF_FILTERS = "ACDMRTUXB"
_TEST_DIR_PREFIX = "tests/"
_SHARED_FIXTURE_BASENAMES = {"conftest.py"}
_SHARED_FIXTURE_PATHS = {"tests/e2e/e2e_common.py", "tests/e2e/e2e_common_helpers.py"}
_CONFIG_NAMES = {
    "pyproject.toml", "setup.cfg", "tox.ini", "requirements.txt",
    "requirements-dev.txt", "Pipfile", "Pipfile.lock", "poetry.lock",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "tsconfig.json", ".pre-commit-config.yaml",
}


class GitDiscoveryError(RuntimeError):
    """Falha do Git durante descoberta — o chamador converte em exit 2."""


@dataclass(frozen=True)
class ImpactSelection:
    mode: Literal["selected", "full"]
    changed_files: tuple[str, ...]
    test_files: tuple[str, ...]
    reasons: tuple[str, ...]
    unresolved_files: tuple[str, ...]


def _git(repo_root: Path, args: list[str]) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=15,
    )
    if res.returncode != 0:
        raise GitDiscoveryError(res.stderr.strip()[:300] or "git falhou")
    return res.stdout


def _discover(repo_root: Path, base: str) -> tuple[tuple[str, ...], frozenset[str]]:
    """(changed incl. origem+destino de renomes + untracked, deletados)."""
    out = _git(repo_root, [
        "diff", "--name-status", "-M", f"--diff-filter={_DIFF_FILTERS}", base, "--",
    ])
    changed: list[str] = []
    deleted: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        status = parts[0]
        if status.startswith("R") or status.startswith("C"):
            if len(parts) >= 3:
                changed.extend((parts[1].strip(), parts[2].strip()))
            continue
        if status.startswith("D"):
            if len(parts) >= 2:
                deleted.add(parts[1].strip())
                changed.append(parts[1].strip())
            continue
        if len(parts) >= 2:
            changed.append(parts[1].strip())
    out_u = _git(repo_root, ["ls-files", "--others", "--exclude-standard"])
    for line in out_u.splitlines():
        if line.strip():
            changed.append(line.strip())
    return tuple(dict.fromkeys(changed)), frozenset(deleted)


def discover_changed_files(repo_root: Path, base: str) -> tuple[str, ...]:
    """Diff `<base>`: ACDMRTUXB + untracked; renomes incluem origem e destino."""
    changed, _deleted = _discover(Path(repo_root), base)
    return changed


def _is_test_file(rel: str) -> bool:
    if not rel.endswith(".py"):
        return False
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith(_TEST_DIR_PREFIX) and (name.startswith("test_") or name.endswith("_test.py"))


# --- Execução dinâmica: invisível ao grafo de imports → incerteza → full ----

_DYNAMIC_NAME_CALLS = frozenset({"exec"})
_DYNAMIC_ATTR_CALLS = frozenset({
    "spec_from_file_location",  # importlib.util.spec_from_file_location
    "run_module", "run_path",   # runpy.*
})
_SPAWN_WITH_INTERPRETER = frozenset({
    "run", "Popen", "call", "check_call", "check_output",
    "getoutput", "getstatusoutput",  # subprocess.*
    "spawnl", "spawnlp", "spawnv", "spawnvp",
    "spawnve", "spawnvpe", "system", "popen",  # os.* legados
})


def _has_sys_executable(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and sub.attr == "executable"
                and isinstance(sub.value, ast.Name) and sub.value.id == "sys"):
            return True
    return False


def _test_uses_dynamic_exec(path: Path) -> bool:
    """True se o arquivo de teste executa código invisível ao grafo de imports.

    Cobre `exec`, `spec_from_file_location`, `runpy.run_module`/`run_path` e
    spawns de processo cujos argumentos citam `sys.executable` — por atributo
    (`subprocess.run(...)`, `os.system(...)`) ou por nome importado
    (`from subprocess import run; run(sys.executable ...)`,
    `Popen(sys.executable, ...)`). Arquivo não parseável também conta como
    incerteza (fail-closed).
    """
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError, RecursionError):
        return True
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _DYNAMIC_NAME_CALLS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _DYNAMIC_ATTR_CALLS:
            return True
        call_args = (*node.args, *(kw.value for kw in node.keywords))
        if any(_has_sys_executable(a) for a in call_args):
            callee = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else "")
            if callee in _SPAWN_WITH_INTERPRETER:
                return True
    return False


def _dynamic_exec_test_files(repo: Path) -> list[str]:
    """Test files on disk que executam código fora do grafo → incerteza."""
    tests_root = Path(repo) / "tests"
    if not tests_root.is_dir():
        return []
    flagged: set[str] = set()
    try:
        for entry in winpath.iter_tree(tests_root,
                                       prune_names=frozenset({"__pycache__"})):
            rel = f"{_TEST_DIR_PREFIX}{entry.relative_path}"
            if entry.kind == "error":
                flagged.add(f"<walk_error:{rel}>")
                continue
            if entry.kind != "file" or not _is_test_file(rel):
                continue
            if _test_uses_dynamic_exec(entry.path):
                flagged.add(rel)
    except winpath.PathSafetyError:
        flagged.add("<tests_root_unsafe>")
    return sorted(flagged)


def _is_shared_fixture(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (
        name in _SHARED_FIXTURE_BASENAMES
        or rel in _SHARED_FIXTURE_PATHS
        or rel.startswith("tests/fixtures/")
        or (rel.startswith(_TEST_DIR_PREFIX) and name == "__init__.py")
    )


def _is_config_or_nonpython(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1].lower()
    if not rel.endswith(".py"):
        return True
    if name in {n.lower() for n in _CONFIG_NAMES}:
        return True
    if "/.github/workflows/" in ("/" + rel.lower()) or rel.lower().startswith(".github/"):
        return True
    return False


def _forward_closure(direct: Mapping[str, frozenset[str]], roots) -> frozenset[str]:
    seen = set(roots)
    stack = list(seen)
    while stack:
        node = stack.pop()
        for dep in direct.get(node, ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return frozenset(seen)


def select_tests(repo_root: Path, base: str,
                 direct: Mapping[str, frozenset[str]],
                 *, unresolved: Mapping[str, tuple[str, ...]] | None = None) -> ImpactSelection:
    """Seleção conservativa por arquivo de teste.

    `direct` = mapa de imports internos diretos (`ImportGraph.direct`).
    `unresolved` = problemas do grafo (`ImportGraph.unresolved`) — qualquer
    entrada força `full`.
    """
    repo = Path(repo_root).resolve()
    changed, deleted = _discover(repo, base)
    changed_t = tuple(changed)

    if not changed_t:
        return ImpactSelection(
            "selected", (), (), ("no_changes",), (),
        )

    reasons: list[str] = []
    unresolved_files: list[str] = []

    # Incerteza total do grafo → suíte completa.
    if unresolved:
        unresolved_files = sorted(unresolved)
        reasons.append("graph_unresolved")

    # Testes que executam código fora do grafo (subprocess com
    # sys.executable, spec_from_file_location, runpy, exec) tornam o
    # fechamento de imports não-probante → incerteza → suíte completa.
    for rel in _dynamic_exec_test_files(repo):
        reasons.append(f"dynamic_exec:{rel}")

    all_tests = sorted(
        rel for rel in direct
        if _is_test_file(rel) and (repo / rel).is_file()
    )
    test_closures = {t: _forward_closure(direct, (t,)) for t in all_tests}
    union_closure = frozenset().union(*test_closures.values()) if test_closures else frozenset()

    selected: set[str] = set()
    for rel in changed_t:
        if rel in deleted:
            reasons.append(f"deleted:{rel}")
            continue
        if _is_config_or_nonpython(rel):
            reasons.append(f"non_python_or_config:{rel}")
            continue
        if _is_shared_fixture(rel):
            reasons.append(f"shared_fixture:{rel}")
            continue
        if _is_test_file(rel):
            selected.add(rel)
            continue
        # Arquivo .py de produção: precisa pertencer à união dos fechamentos.
        if rel in union_closure:
            for t, clo in test_closures.items():
                if rel in clo:
                    selected.add(t)
        else:
            reasons.append(f"unknown_or_unreachable:{rel}")

    if unresolved_files or reasons:
        return ImpactSelection(
            "full", changed_t, (),
            tuple(sorted(set(reasons))), tuple(unresolved_files),
        )
    return ImpactSelection(
        "selected", changed_t, tuple(sorted(selected)),
        tuple(sorted(r for r in (f"impacted:{t}" for t in selected))),
        (),
    )


def build_unittest_command(selection: ImpactSelection,
                           repo_root: Path) -> list[str]:
    """Comando unittest: seleção por módulos pontuados ou discover completo."""
    if selection.mode == "full" or not selection.test_files:
        return [sys.executable, "-m", "unittest", "discover",
                "-s", "tests", "-p", "test_*.py"]
    modules = [
        rel[:-3].replace("/", ".").replace("\\", ".")
        for rel in selection.test_files
    ]
    return [sys.executable, "-m", "unittest", *modules]
