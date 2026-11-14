"""rs_git.py — Snapshots git higienizados para baseline de run (HEAD, ignored, untracked)."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess

# A11 (banca v10): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG_* plantados
# no ambiente redirecionariam o git das âncoras/vereditos para outro repo/index.
# A4 (banca v14): env higienizado nos subprocessos git internos —
# PYTHONPATH/loader hooks subvertem tooling que produz evidência.
_GIT_ENV = {k: v for k, v in os.environ.items()
            if not (k.startswith("GIT_") or k.startswith("PYTHON")
                    or k.startswith("LD_") or k.startswith("DYLD_")
                    or k in ("BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS",
                             "PERL5OPT", "PERL5LIB", "RUBYOPT",
                             "DOTNET_STARTUP_HOOKS"))}


def _git_env_for(repo_root: Path) -> dict:
    """Delega para sanitized_child_env(repo_root) — filtra GIT_*, PYTHON*,
    npm_config_* case-insensitive e remove entradas relativas/internas de PATH."""
    from henke.state_paths import sanitized_child_env
    return sanitized_child_env(repo_root)


def _git_head(repo_root: Path) -> str | None:
    """HEAD atual do repo (None se git indisponível/sem commits — nunca inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, env=_git_env_for(repo_root),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = (res.stdout or "").strip()
    return sha if res.returncode == 0 and sha else None


def _git_root_commit(repo_root: Path) -> str | None:
    """Root commit do repo (F-06): identidade de CONTEÚDO estável entre
    clones e cópias congeladas da mesma linhagem de projeto — é o que
    permite a uma cópia isolada herdar as âncoras locais legítimas.
    None = git indisponível/sem commits (nunca inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "rev-list", "--max-parents=0", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, env=_git_env_for(repo_root),
        )
    except Exception:
        # git indisponível/mockado/falho → identidade cai para CONFIG/path;
        # nunca propaga (custódia é best-effort, não ponto de falha novo).
        return None
    if res.returncode != 0:
        return None
    roots = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
    return roots[0] if roots else None


def _git_ignored(repo_root: Path) -> list[str] | None:
    """Arquivos ignorados presentes no worktree (status --ignored → '!!').
    Snapshot ancorado no init: escrita em path ignorado é invisível a TODOS
    os portões de diff — só a diferença de snapshots a expõe (banca v8 F2).
    None = git indisponível (auditoria fica sem baseline, não inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "status", "--porcelain", "-uall", "--ignored", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_env_for(repo_root),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return sorted(
        ent[3:].strip() for ent in (res.stdout or "").split("\0")
        if ent.startswith("!! ") and ent[3:].strip()
    )


def _git_ignored_hashes(repo_root: Path) -> dict[str, dict] | None:
    """{path: {sha256,size}} dos ignorados presentes (DS-8): o conjunto de
    NOMES sozinho não vê reescrita de conteúdo — `.env`/`dist/payload.js`
    sobrescritos mantêm o mesmo `!!` e passam invisíveis a todo portão.
    None = git indisponível."""
    names = _git_ignored(repo_root)
    if names is None:
        return None
    out: dict[str, dict] = {}
    for rel in names:
        p = Path(repo_root) / rel
        try:
            if p.is_file() and not p.is_symlink():
                out[rel] = {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                            "size": p.stat().st_size}
            elif p.is_dir():
                out[rel] = {"sha256": None, "size": -1}
        except OSError:
            out[rel] = {"sha256": None, "size": -2}
    return out


def _git_untracked(repo_root: Path) -> list[str] | None:
    """Arquivos untracked não-ignorados (status -uall → '??').
    A2 (banca v16): baseline cobria só ignorados — deletar `??` era invisível
    a todo portão de diff (nunca foi tracked, some do conjunto). None = git
    indisponível."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "status", "--porcelain", "-uall", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_env_for(repo_root),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return sorted(
        ent[3:].strip() for ent in (res.stdout or "").split("\0")
        if ent.startswith("?? ") and ent[3:].strip()
    )
