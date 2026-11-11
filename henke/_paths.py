#!/usr/bin/env python3
"""_paths.py — resolução compartilhada da raiz da suíte (checkout vs wheel).

Ordem de precedência:
1. o próprio checkout (dir acima de henke/ com `henke-*/SKILL.md` presentes);
2. dados instalados pelo pacote (`sysconfig` data dir / `henke_data` — wheel,
   incluindo o scheme de usuário onde `pip install --user` e pipx colocam dados);
3. o dir acima de henke/ mesmo sem inventário (último recurso honesto —
   o doctor reporta `skills_count` baixo e degrada em vez de fingir).
"""
from __future__ import annotations

import site
import sysconfig
from pathlib import Path

_HERE = Path(__file__).resolve().parents[1]


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    # DS-8: `--prefix`/`--target` instalam scripts em <alvo>/scripts e dados
    # em <alvo>/henke_data — fora dos sysconfig paths do interpretador atual.
    # Procura henke_data ao lado de _HERE e nos ancestrais do próprio pacote
    # (site-packages → <prefix>). A checagem exige henke-*/SKILL.md dentro.
    for anc in (_HERE, *_HERE.parents):
        roots.append(anc / "henke_data")
    keys = ("data", "platlib", "purelib")
    roots += [Path(sysconfig.get_path(k)) / "henke_data" for k in keys]
    # Scheme de usuário — `pip install --user` e pipx instalam dados aqui,
    # fora do alcance dos três paths padrão acima (ALTO instalador, banca).
    for scheme in ("nt_user", "posix_user"):
        try:
            roots.append(Path(sysconfig.get_path("data", scheme=scheme)) / "henke_data")
        except Exception:
            pass
    try:
        roots.append(Path(site.getusersitepackages()) / "henke_data")
    except Exception:
        pass
    try:
        roots.append(Path(site.getuserbase()) / "henke_data")
    except Exception:
        pass
    return roots


def suite_root() -> Path:
    if any(_HERE.glob("henke-*/SKILL.md")):
        return _HERE
    for cand in _candidate_roots():
        try:
            if cand.is_dir() and any(cand.glob("henke-*/SKILL.md")):
                return cand
        except OSError:
            continue
    return _HERE
