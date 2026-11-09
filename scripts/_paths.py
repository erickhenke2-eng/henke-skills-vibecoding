#!/usr/bin/env python3
"""_paths.py — resolução compartilhada da raiz da suíte (checkout vs wheel).

Ordem de precedência:
1. o próprio checkout (dir acima de scripts/ com `henke-*/SKILL.md` presentes);
2. dados instalados pelo pacote (`sysconfig` data dir / `henke_data` — wheel);
3. o dir acima de scripts/ mesmo sem inventário (último recurso honesto —
   o doctor reporta `skills_count` baixo e degrada em vez de fingir).
"""
from __future__ import annotations

from pathlib import Path
import sysconfig

_HERE = Path(__file__).resolve().parents[1]


def suite_root() -> Path:
    if any(_HERE.glob("henke-*/SKILL.md")):
        return _HERE
    for key in ("data", "platlib", "purelib"):
        cand = Path(sysconfig.get_path(key)) / "henke_data"
        if any(cand.glob("henke-*/SKILL.md")):
            return cand
    return _HERE
