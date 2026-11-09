"""state_paths.py — resolução do diretório de estado Henke (PROTOCOLO v4.0, Wave 7).

`HENKE_STATE_DIR`, quando definido como caminho absoluto, redireciona `.henke` para
auditoria e testes. Sem ele, o estado fica em `<repo>/.henke`. Um override relativo
é erro de uso explícito, nunca um redirecionamento silencioso para dentro do repo.
"""
from __future__ import annotations

import os
from pathlib import Path


def state_dir(repo_root: Path) -> Path:
    override = os.environ.get("HENKE_STATE_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError("HENKE_STATE_DIR deve ser absoluto")
        return path
    return repo_root.resolve() / ".henke"
