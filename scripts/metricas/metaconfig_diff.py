#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metaconfig_diff.py — Trava de metaconfiguração & anti-TOCTOU (spec: docs/designs/wave4-consenso-adversarial.md).

Portões auditam código-fonte, mas o pipeline real executa metaconfiguração:
`package.json.scripts` (pretest/test/posttest/prepack/postinstall), manifests de
dependências, lockfiles, `build.rs`, `conftest.py`, workflows e `.cargo/config`.
Mutar ou adicionar esses arquivos entre a auditoria e o publish executa código nunca auditado.

Auditoria em DUAS superfícies independentes — mutação em qualquer uma conta:
  - worktree: `git diff --name-status -z --no-renames <against>` + untracked (??)
  - index/staged: `git diff --cached --name-status -z --no-renames <against>`,
    com conteúdo lido via `git show :<path>` — pega mutação "staged-only"
    (ex.: postinstall malicioso commitado no índice com worktree limpo).

Regra: qualquer mutação em metaconfiguração exige aprovação humana explícita —
saída carrega `requires_human_auth: true`. Arquivo listado em `## 4. Escopo
Declarado` da spec fornecida via `--spec` conta como autorizado.

Exit codes:
  0  sem mutação não autorizada
  1  mutação não autorizada encontrada (requires_human_auth)
  2  erro de uso / ref inválida / repo ilegível
"""
from pathlib import Path
import argparse
import json
import re
import subprocess
import sys

try:
    import tomllib
except ImportError:  # pragma: no cover - py<3.11
    tomllib = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
for _p in (str(_HERE), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import spec_drift as _sd  # noqa: E402

_PKG_WATCHED_KEYS = (
    "scripts", "dependencies", "devDependencies", "peerDependencies",
    "optionalDependencies", "overrides", "resolutions", "publishConfig", "bin",
    # campos que trocam entry point/resolução do pacote (A1-banca)
    "workspaces", "main", "exports", "imports", "files", "type",
    # instalação condicionada / exec nativo (M5 banca v6)
    "gypfile", "os", "cpu", "libc", "engines", "config",
    # hook-runners de commit/install — executam comandos arbitrários (C4 v7)
    "husky", "lint-staged", "simple-git-hooks", "gitHooks", "nano-staged",
    "precommit-hook", "packageManager",
)
# Qualquer arquivo cujo diff inteiro é sensível (não há seção segura)
_WHOLE_FILE_SENSITIVE = (
    "build.rs", "conftest.py", ".cargo/config", ".cargo/config.toml",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "uv.lock", "Cargo.lock", ".npmrc", "Makefile",
)
_WHOLE_FILE_BASENAMES = {
    "build.rs", "conftest.py",
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "uv.lock", "Cargo.lock",
    ".npmrc", "Makefile", ".gitlab-ci.yml",
    # install-time code exec / supply-chain (A1-banca)
    "setup.py", "setup.cfg", "MANIFEST.in",
    "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".gitmodules", ".travis.yml", "tox.ini",
    "rust-toolchain", "rust-toolchain.toml",
    # task runners / hooks / CI fora de .github (M5 banca v6)
    ".envrc", "justfile", "Justfile", "justfile.toml",
    ".pre-commit-config.yaml", ".pre-commit-config.yml",
    "lefthook.yml", "lefthook.yaml", "mise.toml", ".mise.toml",
    "Jenkinsfile", "bitbucket-pipelines.yml", "azure-pipelines.yml",
    "Brewfile", "Earthfile", ".gitlab-ci.yml",
    # mutação invisível/exec via git: ignore esconde arquivos dos portões de
    # diff; attributes+diff=driver executa comando externo (F-02/F-10 adj.)
    ".gitignore", ".gitattributes", ".mailmap",
}
# requirements*.txt / Pipfile* / Dockerfile* — matching por padrão, não exato
_WHOLE_FILE_GLOBS = (
    "requirements*.txt", "Pipfile", "Pipfile.lock", "Dockerfile*",
    ".env", ".env.*",
)
# Composite actions do GitHub também executam código
_ACTION_FILE_RE = re.compile(r"(^|/|\\)\.github/actions/[^/\\]+/action\.ya?ml$", re.IGNORECASE)
_WORKFLOW_DIR_RE = re.compile(r"(^|/|\\)\.github/workflows/[^/\\]+\.ya?ml$", re.IGNORECASE)
# Outros CI dirs fora de .github (M5 banca v6)
_ALT_CI_RE = re.compile(r"(^|/|\\)\.circleci/[^/\\]+\.ya?ml$|(^|/|\\)\.husky/[^/\\]+$", re.IGNORECASE)
# Arquivos de custódia do próprio Henke — adulterá-los é metaconfig também
# (defesa em profundidade: o spec_drift os veria como touched, mas só se a
# spec existir e a âncora não tiver sido movida).
_HENKE_CUSTODY = frozenset({
    ".henke/POLICY.json", ".henke/PROJECT.json", ".henke/fence.json",
    ".henke/ratchet-baseline.tsv", ".henke/gate-baseline.json",
    ".henke/coverage-ledger.json",
})


def _strip_dot_prefix(rel: str) -> str:
    """Remove prefixo './' apenas — NUNCA lstrip('./'): isso comeria o ponto de
    '.github/workflows/x.yml' → 'github/...' e toda metaconfig-dotfile sumia do
    diff (C1-banca: CI adulterado invisível)."""
    n = rel.replace("\\", "/")
    while n.startswith("./"):
        n = n[2:]
    return n


def _is_protected(rel: str) -> str | None:
    """Classifica o path relativo; retorna o tipo de inspeção ou None."""
    import fnmatch
    rel_n = _strip_dot_prefix(rel)
    base = rel_n.rsplit("/", 1)[-1]
    # Case-fold: em FS case-insensitive (Windows/macOS), 'Package.json' ou
    # '.GITHUB/workflows/x.yml' SÃO os arquivos protegidos de verdade —
    # comparação exata os deixava invisíveis (banca v7 — C3). Direção
    # conservadora: flag extra em FS sensível a caso é falso-vermelho, não
    # falso-verde.
    base_l = base.lower()
    rel_l = rel_n.lower()
    if base_l == "package.json":
        return "package_json"
    if base_l in ("pyproject.toml", "cargo.toml"):
        return "toml"
    if (
        rel_l in {s.lower() for s in _WHOLE_FILE_SENSITIVE}
        or rel_l in {s.lower() for s in _HENKE_CUSTODY}
        or base_l in {s.lower() for s in _WHOLE_FILE_BASENAMES}
    ):
        return "whole_file"
    if any(fnmatch.fnmatchcase(base_l, pat.lower()) or fnmatch.fnmatchcase(rel_l, pat.lower())
           for pat in _WHOLE_FILE_GLOBS):
        return "whole_file"
    if (_WORKFLOW_DIR_RE.search(rel_n) or _ACTION_FILE_RE.search(rel_n)
            or _ALT_CI_RE.search(rel_n)):
        return "whole_file"
    return None


_is_metaconfig = _is_protected


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false",
         "-c", "core.fsmonitor=false", "-c", "diff.external=", *args],
        cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )


def _parse_name_status_z(raw: str) -> list[tuple[str, str]]:
    """Parse de `git diff --name-status -z --no-renames`: registros `STATUS\\0PATH\\0`.
    Com -z os paths NÃO vêm quoted; --no-renames elimina registros R/C (que viram pares)."""
    out: list[tuple[str, str]] = []
    toks = raw.split("\0")
    i = 0
    while i + 1 < len(toks):
        status = toks[i].strip()
        path = toks[i + 1].strip().replace("\\", "/")
        i += 2
        if status and path:
            out.append((status, path))
    return out


def _changed_protected_files(repo: Path, against: str) -> tuple[list[tuple[str, str, str, str]] | None, str | None]:
    """Retorna [(status, rel, kind, surface)] para arquivos protegidos alterados.

    `surface` é "worktree" (diff worktree vs ref + untracked ??) ou "staged"
    (`git diff --cached` — conteúdo do índice via `git show :<rel>`). As duas
    superfícies são auditadas separadamente: mutação em qualquer uma conta.
    Retorna (None, erro) se algum diff falhar."""
    res = _git(repo, "diff", "--name-status", "-z", "--no-renames", against, "--")
    if res.returncode != 0:
        return None, res.stderr.strip() or "git diff falhou"
    entries_worktree = _parse_name_status_z(res.stdout)

    res_cached = _git(repo, "diff", "--cached", "--name-status", "-z", "--no-renames", against, "--")
    if res_cached.returncode != 0:
        return None, res_cached.stderr.strip() or "git diff --cached falhou"
    entries_staged = _parse_name_status_z(res_cached.stdout)

    changed: list[tuple[str, str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for surface, entries in (("worktree", entries_worktree), ("staged", entries_staged)):
        for status, rel in entries:
            kind = _is_protected(rel)
            if not kind:
                continue
            key = (surface, rel)
            if key in seen:
                continue
            seen.add(key)
            changed.append((status, rel, kind, surface))

    # Captura arquivos untracked (??) como adições ('A') na superfície worktree
    res_untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    if res_untracked.returncode == 0:
        for rel in res_untracked.stdout.split("\0"):
            rel = rel.strip().replace("\\", "/")
            if not rel or ("worktree", rel) in seen:
                continue
            kind = _is_protected(rel)
            if kind:
                changed.append(("A", rel, kind, "worktree"))
                seen.add(("worktree", rel))

    # Arquivos IGNORADOS (.gitignore) também são superfície: um payload em path
    # ignorado (.env.local, dist/, .envrc) é invisível ao diff normal — vetor
    # supply-chain direto (M3 banca v6). Filtramos aos nomes protegidos.
    res_ignored = _git(repo, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    if res_ignored.returncode == 0:
        for rel in res_ignored.stdout.split("\0"):
            rel = rel.strip().replace("\\", "/")
            if not rel or ("worktree", rel) in seen:
                continue
            kind = _is_protected(rel)
            if kind:
                changed.append(("A", rel, kind, "worktree"))
                seen.add(("worktree", rel))

    return changed, None


def _blob(repo: Path, against: str, rel: str) -> str | None:
    res = _git(repo, "show", f"{against}:{rel}")
    return res.stdout if res.returncode == 0 else None


def _blob_index(repo: Path, rel: str) -> str | None:
    """Conteúdo do arquivo NO INDEX (staged): `git show :<rel>`."""
    res = _git(repo, "show", f":{rel}")
    return res.stdout if res.returncode == 0 else None


def _diff_pkg_keys(old_text: str | None, new_text: str | None) -> list[str]:
    """Diff semântico das seções vigiadas de package.json; textual se não parsear."""
    try:
        old = json.loads(old_text) if old_text else {}
        new = json.loads(new_text) if new_text else {}
    except json.JSONDecodeError:
        return ["package.json não parseável — diff textual conta como mutação"]
    diffs = []
    for key in _PKG_WATCHED_KEYS:
        ov, nv = old.get(key), new.get(key)
        if ov == nv:
            continue
        if isinstance(ov, dict) and isinstance(nv, dict):
            for sub in sorted(set(ov) | set(nv)):
                if ov.get(sub) != nv.get(sub):
                    diffs.append(f"package.json:{key}.{sub}")
        else:
            diffs.append(f"package.json:{key}")
    return diffs


def _toml_sections(text: str) -> dict:
    if tomllib is None:
        # py<3.11 sem parser TOML: fail-closed — arquivo não-vazio que não
        # podemos parsear conta como mutação, nunca como "sem diff".
        return {"_unparsed": text} if text.strip() else {}
    try:
        return tomllib.loads(text)
    except Exception:
        return {"_unparsed": text}


_TOML_WATCH = ("dependencies", "dev-dependencies", "build-dependencies",
               "build-system", "project", "tool",
               # Cargo.toml supply-chain: [patch] redireciona deps para fonte
               # arbitrária; [source]/[replace] trocam o registry inteiro (A2).
               "patch", "source", "replace", "target", "package",
               "features", "workspace",
               # PEP 735: onde uv/pdm colocam dev-deps hoje (M5 banca v6)
               "dependency-groups")


def _diff_toml(old_text: str | None, new_text: str | None) -> list[str]:
    old = _toml_sections(old_text or "")
    new = _toml_sections(new_text or "")
    if "_unparsed" in old or "_unparsed" in new:
        return ["toml não parseável — diff textual conta como mutação"]
    diffs = []

    def walk(o, n, path):
        if o == n:
            return
        if isinstance(o, dict) and isinstance(n, dict):
            for k in sorted(set(o) | set(n)):
                walk(o.get(k), n.get(k), f"{path}.{k}" if path else k)
        else:
            diffs.append(path)

    for top in _TOML_WATCH:
        if old.get(top) is not None or new.get(top) is not None:
            walk(old.get(top), new.get(top), top)
    return diffs


def _spec_declared(spec: Path | None) -> set[str]:
    if spec is None or not spec.is_file():
        return set()
    try:
        return {p.replace("\\", "/") for p in _sd.parse_declared_scope(spec)}
    except Exception:
        return set()


def check_metaconfig(repo: Path | str, against: str, spec: Path | str | None = None) -> tuple[list[str], bool]:
    """Retorna (mutações_não_autorizadas, requires_human_auth).

    Audita staged (index, `git show :<rel>`) E worktree separadamente:
    mutação em qualquer superfície conta."""
    repo = Path(repo)
    spec_p = Path(spec) if spec else None
    declared = _spec_declared(spec_p)
    declared_list = list(declared)

    changed, err = _changed_protected_files(repo, against)
    if changed is None:
        raise RuntimeError(f"metaconfig_diff: não foi possível diff contra '{against}': {err}")

    mutations: list[str] = []
    for status, rel, kind, surface in changed:
        rel_n = rel.replace("\\", "/")
        if rel_n in declared or (declared_list and _sd.is_path_in_scope(rel_n, declared_list)):
            continue
        if status.startswith("A"):
            mutations.append(f"[{surface}:A] {rel_n}: metaconfig alterado sem autorização na spec (METACONFIG-MUTATION)")
            continue
        if status.startswith("D"):
            mutations.append(f"[{surface}:D] {rel_n}: metaconfig removido sem autorização na spec (METACONFIG-MUTATION)")
            continue
        if surface == "staged":
            new_text = _blob_index(repo, rel_n)
        else:
            work = repo / rel
            new_text = work.read_text(encoding="utf-8", errors="replace") if work.is_file() else None
        old_text = _blob(repo, against, rel_n)
        if kind == "package_json":
            details = _diff_pkg_keys(old_text, new_text)
        elif kind == "toml":
            details = _diff_toml(old_text, new_text)
        else:
            details = ["metaconfig alterado sem autorização na spec"]
        for d in details:
            mutations.append(f"METACONFIG-MUTATION [{surface}:{status}] {rel_n}: {d}")
    return mutations, bool(mutations)


def main() -> int:
    ap = argparse.ArgumentParser(description="Trava de metaconfiguração anti-TOCTOU (diff semântico, staged+worktree).")
    ap.add_argument("--repo", type=str, required=True)
    ap.add_argument("--against", type=str, required=True, help="ref git de comparação (ex.: HEAD, main)")
    ap.add_argument("--spec", type=str, default=None, help="spec cujo '## 4. Escopo Declarado' autoriza arquivos")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    # `--against` vai ao argv do git: valor começando com '-' vira opção
    # (--output= / --ext-diff → file write/exec). Fail-closed, mesma regra
    # do spec_drift — e a ref precisa resolver para um commit real.
    against = str(args.against or "").strip()
    if not against or against.startswith("-"):
        print("ERRO: --against inválido (não pode ser vazio nem começar com '-')", file=sys.stderr)
        return 2
    chk = _git(repo, "rev-parse", "--verify", f"{against}^{{commit}}")
    if chk.returncode != 0:
        print(f"ERRO: --against '{against}' não resolve para um commit", file=sys.stderr)
        return 2
    try:
        mutations, requires_auth = check_metaconfig(repo, against, args.spec)
    except RuntimeError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    payload = {
        "mutations": mutations,
        "requires_human_auth": requires_auth,
        "outcome": "failed" if mutations else "verified",
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        for m in mutations:
            print(f"FAIL: {m}")
        print(json.dumps({"outcome": payload["outcome"], "requires_human_auth": requires_auth}))
    return 1 if mutations else 0


if __name__ == "__main__":
    sys.exit(main())
