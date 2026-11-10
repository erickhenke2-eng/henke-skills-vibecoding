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


def secret_dir(repo_root: Path) -> Path:
    """Diretório de segredos FORA da árvore auditada (DS-5).

    A chave HMAC do gate cache / scan-manifest não pode morar em `.henke/`
    do próprio repo: um zip/tarball entregue com `.cache_secret` embutido
    assinaria manifestos com os achados zerados e o verificador aceitaria.
    O segredo vive no perfil do usuário, chaveado pelo repo auditado.

    `HENKE_SECRET_DIR` (absoluto) redireciona para testes/auditoria.
    """
    override = os.environ.get("HENKE_SECRET_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError("HENKE_SECRET_DIR deve ser absoluto")
        return path
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "henke" / "secrets"


def secret_key_name(repo_root: Path) -> str:
    """Nome de arquivo estável por repo — repos distintos nunca compartilham chave."""
    import hashlib
    digest = hashlib.sha256(
        str(repo_root.resolve()).encode("utf-8")).hexdigest()
    return f"{digest[:24]}.key"


def cache_root(repo_root: Path) -> Path:
    """Raiz de cache de portões FORA do repo auditado (DS-14): `henke check`
    não pode escrever `.henke/cache` na árvore sob análise — suja git status
    e viola ANALISAR. Keyed por repo, ao lado do dir de segredos."""
    import hashlib
    override = os.environ.get("HENKE_CACHE_DIR")
    if override:
        return Path(override)
    digest = hashlib.sha256(
        str(repo_root.resolve()).encode("utf-8")).hexdigest()[:24]
    base = secret_dir(repo_root).parent  # <profile>/henke/
    return base / "cache" / digest
