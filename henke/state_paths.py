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

# Banca-Adversarial 2: helper único de env para TODOS os subprocessos.
# Combina a escova de loader-hooks do process_runner com o filtro de PATH
# do run_state._git_env_for — um `git`/shim plantado no repo (ou PATH com
# entrada relativa) resolvia binário hostil dentro do custodiante.
_CHILD_ENV_STRIP_PREFIX = (
    "GIT_", "PYTHON", "LD_", "DYLD_", "NODE_", "NPM_CONFIG", "PERL5",
    "RUBYOPT", "DOTNET_", "JAVA_", "MAVEN_", "GRADLE_", "SBT_", "PHP",
    "GOFLAGS", "RUST", "VIRTUAL_ENV", "CARGO_", "GEM_HOME", "BUNDLE_",
)
_CHILD_ENV_STRIP_EXACT = frozenset({
    "BASH_ENV", "ENV", "ZDOTDIR", "CDPATH", "PSModulePath", "ComSpec",
    "PATHEXT", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "NODE_OPTIONS",
    "PERL5OPT", "PERL5LIB", "DOTNET_STARTUP_HOOKS",
})


def sanitized_child_env(repo_root: Path | None = None) -> dict:
    """os.environ escovado para filhos: sem loader-hooks/GIT_* e com PATH
    sem entradas relativas nem dentro do repo auditado."""
    # Comparação case-insensitive: npm lê npm_config_* minúsculo no POSIX
    # (cli_exec já filtrava ambos); no Windows o env é case-insensitive.
    env = {
        k: v for k, v in os.environ.items()
        if not (k.upper().startswith(_CHILD_ENV_STRIP_PREFIX)
                or k.upper() in _CHILD_ENV_STRIP_EXACT)}
    if repo_root is not None:
        try:
            rr = Path(repo_root).resolve()
        except OSError:
            rr = Path(repo_root)
        keep = []
        for d in env.get("PATH", "").split(os.pathsep):
            if not d or not os.path.isabs(d):
                continue
            try:
                dr = Path(d).resolve()
            except OSError:
                continue
            if dr == rr or rr in dr.parents:
                continue
            keep.append(d)
        env["PATH"] = os.pathsep.join(keep)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env

