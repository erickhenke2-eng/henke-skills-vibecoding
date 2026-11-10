#!/usr/bin/env python3
"""
install_hook.py — Instalador nativo de hook Git de 1 comando (PROTOCOLO v4.0 - Wave 6 / R2).

Permite instalar pre-commit hooks compatíveis com Windows (Git Bash / MSYS2 / sh.exe)
e ambientes POSIX com descoberta dinâmica do diretório de hooks via `git rev-parse --git-path hooks`.
Garante idempotência, preserva backups legados com timestamping e encadeia hooks pré-existentes.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Tuple

HENKE_HOOK_MARKER = "# HENKE-PRE-COMMIT-HOOK"

HOOK_SCRIPT_TEMPLATE = """#!/bin/sh
# HENKE-PRE-COMMIT-HOOK
# Installed automatically by 'henke install-hook'
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
python scripts/henke_cli.py check --repo "$REPO_ROOT" --staged --no-cache --full
exit $?
"""


def get_hook_template(cli_path: Path, python_exe: Path) -> str:
    """Gera o script de hook embutindo os caminhos canônicos absolutos do interpretador e da CLI."""
    cli_posix = cli_path.resolve().as_posix()
    py_posix = python_exe.resolve().as_posix()

    return f"""#!/bin/sh
# HENKE-PRE-COMMIT-HOOK
# Installed automatically by 'henke install-hook'
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

# 1. Executa bateria de portões via Henke CLI autoritativa
#    O pre-commit NUNCA usa atalhos de watch/manifesto: sempre --staged --no-cache --full.
if [ -f "{cli_posix}" ]; then
    "{py_posix}" "{cli_posix}" check --repo "$REPO_ROOT" --staged --no-cache --full
    HENKE_EXIT=$?
elif command -v henke >/dev/null 2>&1; then
    henke check --repo "$REPO_ROOT" --staged --no-cache --full
    HENKE_EXIT=$?
else
    echo "[HENKE HOOK] ERRO CRÍTICO: interpretador ou CLI do Henke não encontrado em {cli_posix}." >&2
    HENKE_EXIT=1
fi

if [ $HENKE_EXIT -ne 0 ]; then
    exit $HENKE_EXIT
fi

# 2. Encadeia execução do hook legado mais recente se houver
HOOKS_DIR="$(git rev-parse --git-path hooks 2>/dev/null)"
if [ -n "$HOOKS_DIR" ] && [ -d "$HOOKS_DIR" ]; then
    LEGACY_HOOK="$HOOKS_DIR/pre-commit.legacy"
    if [ -f "$LEGACY_HOOK" ]; then
        sh "$LEGACY_HOOK" "$@"
        LEGACY_EXIT=$?
        if [ $LEGACY_EXIT -ne 0 ]; then
            exit $LEGACY_EXIT
        fi
    fi
fi

exit 0
"""


def discover_hooks_dir(repo_path: Path | str) -> Path | None:
    """Descobre deterministicamente o diretório de hooks do repositório Git.

    Executa `git rev-parse --git-path hooks` em `repo_path`.
    Retorna o caminho resolvido para o diretório de hooks ou None se não for um repo Git válido.
    """
    repo = Path(repo_path).resolve()
    if not repo.exists() or not repo.is_dir():
        return None

    if not (repo / ".git").exists():
        return None

    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode != 0:
            return None
        hooks_rel = res.stdout.strip()
        if not hooks_rel:
            return None
        hooks_p = Path(hooks_rel)
        if hooks_p.is_absolute():
            return hooks_p.resolve()
        return (repo / hooks_p).resolve()
    except Exception:
        return None


def _is_within(root: Path, cand: Path) -> bool:
    """Contenção lexical com normcase — local para não exigir scripts.winpath
    (este módulo é distribuído standalone via load_module_from_path)."""
    root_n = os.path.normcase(os.path.normpath(os.fspath(root))).rstrip(os.sep)
    cand_n = os.path.normcase(os.path.normpath(os.fspath(cand)))
    return cand_n == root_n or cand_n.startswith(root_n + os.sep)


def _no_reparse(path: Path) -> bool:
    """Componentes ainda INEXISTENTES não bloqueiam (um hooksPath custom pode
    ser criado depois). Qualquer ancestral existente reparse/symlink -> recusa."""
    p = Path(path)
    for anc in [p, *p.parents]:
        try:
            if not anc.exists() and not anc.is_symlink():
                continue  # ainda não criado — nada a redirecionar
            if anc.is_symlink():
                return False
            st = anc.stat(follow_symlinks=False)
            if os.name == "nt" and getattr(st, "st_file_attributes", 0) & 0x400:
                return False
        except OSError:
            return False
        if anc.parent == anc:
            break
    return True


def _git_common_dir(repo: Path) -> Path | None:
    """git-common-dir resolvido — em worktree aponta o .git do repo principal
    (onde os hooks efetivamente vivem)."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(repo), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if res.returncode != 0:
            return None
        raw = res.stdout.strip()
        if not raw:
            return None
        p = Path(raw)
        return p.resolve() if p.is_absolute() else (repo / p).resolve()
    except Exception:
        return None


def install_hook(
    repo_path: Path | str,
    hook_type: str = "pre-commit",
    force: bool = False,
) -> Tuple[bool, str]:
    """Instala o hook do Henke no repositório Git informado.

    Args:
        repo_path: Diretório raiz do repositório Git.
        hook_type: Tipo do hook (padrão: "pre-commit").
        force: Se True, sobrescreve sem mover para .legacy — o hook existente
            é sempre preservado em <hook>.legacy.<ts> (PT-04).

    Returns:
        (sucesso: bool, mensagem: str)
    """
    repo = Path(repo_path).resolve()
    hooks_dir = discover_hooks_dir(repo)
    if hooks_dir is None:
        return (False, "Erro: diretório não é um repositório Git válido")

    # PT-04: core.hooksPath pode resolver FORA do repo (o PoC escreveu 2
    # níveis acima). Só se aceita o hooks dir dentro do worktree ou do git
    # common-dir (worktree vinculada compartilha os hooks do repo principal);
    # qualquer outro destino é recusa fail-closed — nada é escrito.
    common_dir = _git_common_dir(repo)
    if common_dir is None:
        return (False, "Erro: git common-dir indisponível — instalação recusada")
    if not (_is_within(repo, hooks_dir) or _is_within(common_dir, hooks_dir)):
        return (False,
                f"Erro: core.hooksPath resolve para fora do repositório "
                f"({hooks_dir}) — instalação recusada")
    # Ancestral reparse/symlink redirecionaria a escrita para fora do repo
    # mesmo com contenção lexical — recusa igual (PT-04).
    if not _no_reparse(hooks_dir):
        return (False,
                f"Erro: diretório de hooks sob reparse point ({hooks_dir}) "
                "— instalação recusada")

    try:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        target_file = hooks_dir / hook_type
        legacy_file = hooks_dir / f"{hook_type}.legacy"

        if target_file.exists():
            try:
                existing_content = target_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                existing_content = ""

            has_content = bool(existing_content.strip())
            is_henke_hook = HENKE_HOOK_MARKER in existing_content

            if has_content and not is_henke_hook:
                if not force:
                    # Move o backup legado anterior para timestamp para que pre-commit.legacy seja sempre o mais recente
                    if legacy_file.exists():
                        ts = int(time.time() * 1000)
                        archived = hooks_dir / f"{hook_type}.legacy.{ts}"
                        try:
                            os.replace(legacy_file, archived)
                        except Exception as err:
                            return (False, f"Erro ao arquivar backup anterior do hook legado: {err}")
                    try:
                        os.replace(target_file, legacy_file)
                    except Exception as err:
                        return (False, f"Erro ao criar backup do hook legado: {err}")
                else:
                    # PT-04: --force também preserva — o PoC mostrou hook
                    # estrangeiro sobrescrito sem backup. Arquiva em
                    # <hook>.legacy.<ts> (mesma convenção timestamped já
                    # usada acima) antes de sobrescrever. NÃO usar .legacy
                    # puro: testes exigem sua ausência no modo force.
                    ts = int(time.time() * 1000)
                    archived = hooks_dir / f"{hook_type}.legacy.{ts}"
                    try:
                        os.replace(target_file, archived)
                    except Exception as err:
                        return (False,
                                f"Erro ao preservar hook existente (--force): {err}")

        # Determina caminhos canônicos da suíte Henke
        cli_path = Path(__file__).parent / "henke_cli.py"
        python_exe = Path(sys.executable)

        hook_content = get_hook_template(cli_path, python_exe)
        normalized_content = hook_content.replace("\r\n", "\n")
        target_file.write_text(normalized_content, encoding="utf-8", newline="\n")

        try:
            os.chmod(target_file, 0o755)
        except Exception:
            pass

        return (True, f"Pre-commit hook instalado com sucesso em {target_file}")
    except Exception as exc:
        return (False, f"Erro ao instalar hook: {exc}")
