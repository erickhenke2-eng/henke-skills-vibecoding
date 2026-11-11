"""import_graph.py — grafo de dependências internas do repositório (Wave 7, R3/R5).

Reutiliza `repo_signals.build_dependency_edges` como motor único de extração:
nenhum segundo parser de imports é criado. `direct` mapeia cada arquivo para os
módulos internos DIRETAMENTE importados; `unresolved` registra imports
internos/relativos não resolvidos, imports dinâmicos, erros de leitura e de
parse — qualquer entrada é incerteza e força rebuild integral no consumidor.

`reverse_closure(changed)` retorna `changed` mais todos os arquivos que
dependem direta ou transitivamente deles (fechamento no sentido reverso).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from henke.repo_signals import build_dependency_edges

_GRAPH_VERSION = 1


@dataclass(frozen=True)
class ImportGraph:
    direct: Mapping[str, frozenset[str]]
    unresolved: Mapping[str, tuple[str, ...]]

    @property
    def graph_version(self) -> int:
        return _GRAPH_VERSION

    def reverse_closure(self, changed: Iterable[str]) -> frozenset[str]:
        """`changed` ∪ arquivos que dependem direta/transitivamente de `changed`.

        Ciclos são visitados uma única vez.
        """
        rev: dict[str, set[str]] = {}
        for src, deps in self.direct.items():
            for dep in deps:
                rev.setdefault(dep, set()).add(src)
        seen = set(changed)
        stack = list(seen)
        while stack:
            node = stack.pop()
            for parent in rev.get(node, ()):
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return frozenset(seen)

    def forward_closure(self, roots: Iterable[str]) -> frozenset[str]:
        """`roots` ∪ tudo que `roots` importa direta/transitivamente."""
        seen = set(roots)
        stack = list(seen)
        while stack:
            node = stack.pop()
            for dep in self.direct.get(node, ()):
                if dep not in seen:
                    seen.add(dep)
                    stack.append(dep)
        return frozenset(seen)


def build_import_graph(repo_root: Path) -> ImportGraph:
    """Constrói o grafo a partir das arestas/problemas de `repo_signals`."""
    edges, problems = build_dependency_edges(Path(repo_root))
    direct: dict[str, set[str]] = {}
    for src, dst in edges:
        direct.setdefault(src, set()).add(dst)
        direct.setdefault(dst, direct.get(dst, set()))
    unresolved: dict[str, list[str]] = {}
    for rel, reason in problems:
        unresolved.setdefault(rel, []).append(reason)
    return ImportGraph(
        direct={k: frozenset(v) for k, v in direct.items()},
        unresolved={k: tuple(sorted(v)) for k, v in unresolved.items()},
    )
