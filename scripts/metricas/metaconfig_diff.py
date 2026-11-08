#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metaconfig_diff.py — Trava de metaconfiguração & anti-TOCTOU (spec: docs/designs/wave4-consenso-adversarial.md).

Portões auditam código-fonte, mas o pipeline real executa metaconfiguração:
`package.json.scripts` (pretest/test/posttest/prepack/postinstall), manifests de
dependências, lockfiles, `build.rs`, `conftest.py`, workflows e `.cargo/config`.
Mutar ou adicionar esses arquivos entre a auditoria e o publish executa código nunca auditado.

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
}
_WORKFLOW_DIR_RE = re.compile(r"(^|/|\\)\.github/workflows/[^/\\]+\.ya?ml$")


def _is_protected(rel: str) -> str | None:
    """Classifica o path relativo; retorna o tipo de inspeção ou None."""
    rel_n = rel.replace("\\", "/").lstrip("./")
    base = rel_n.rsplit("/", 1)[-1]
    if base == "package.json":
        return "package_json"
    if base in ("pyproject.toml", "Cargo.toml"):
        return "toml"
    if rel_n in _WHOLE_FILE_SENSITIVE or base in _WHOLE_FILE_BASENAMES:
        return "whole_file"
    if _WORKFLOW_DIR_RE.search(rel_n):
        return "whole_file"
    return None


_is_metaconfig = _is_protected


def _git(repo: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _changed_protected_files(repo: Path, against: str) -> tuple[list[tuple[str, str, str]] | None, str | None]:
    """Retorna [(status, rel, kind)] para arquivos protegidos alterados ou untracked, ou (None, erro)."""
    res = _git(repo, "diff", "--name-status", against, "--")
    if res.returncode != 0:
        return None, res.stderr.strip() or "git diff falhou"
    changed: list[tuple[str, str, str]] = []
    seen_rels: set[str] = set()
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        rel = parts[-1].strip().strip('"').replace("\\", "/")
        kind = _is_protected(rel)
        if kind:
            changed.append((status, rel, kind))
            seen_rels.add(rel)

    # Captura arquivos untracked (??) como adições ('A')
    res_untracked = _git(repo, "ls-files", "--others", "--exclude-standard")
    if res_untracked.returncode == 0:
        for line in res_untracked.stdout.splitlines():
            rel = line.strip().strip('"').replace("\\", "/")
            if not rel or rel in seen_rels:
                continue
            kind = _is_protected(rel)
            if kind:
                changed.append(("A", rel, kind))
                seen_rels.add(rel)

    return changed, None


def _blob(repo: Path, against: str, rel: str) -> str | None:
    res = _git(repo, "show", f"{against}:{rel}")
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
        return {}
    try:
        return tomllib.loads(text)
    except Exception:
        return {"_unparsed": text}


_TOML_WATCH = ("dependencies", "dev-dependencies", "build-dependencies",
               "build-system", "project", "tool")


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
    """Retorna (mutações_não_autorizadas, requires_human_auth)."""
    repo = Path(repo)
    spec_p = Path(spec) if spec else None
    declared = _spec_declared(spec_p)
    declared_list = list(declared)

    changed, err = _changed_protected_files(repo, against)
    if changed is None:
        raise RuntimeError(f"metaconfig_diff: não foi possível diff contra '{against}': {err}")

    mutations: list[str] = []
    for status, rel, kind in changed:
        rel_n = rel.replace("\\", "/")
        if rel_n in declared or (declared_list and _sd.is_path_in_scope(rel_n, declared_list)):
            continue
        if status.startswith("A"):
            mutations.append(f"[A] {rel_n}: metaconfig alterado sem autorização na spec (METACONFIG-MUTATION)")
            continue
        if status.startswith("D"):
            mutations.append(f"[D] {rel_n}: metaconfig removido sem autorização na spec (METACONFIG-MUTATION)")
            continue
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
            mutations.append(f"METACONFIG-MUTATION [{status}] {rel_n}: {d}")
    return mutations, bool(mutations)


def main() -> int:
    ap = argparse.ArgumentParser(description="Trava de metaconfiguração anti-TOCTOU (diff semântico).")
    ap.add_argument("--repo", type=str, required=True)
    ap.add_argument("--against", type=str, required=True, help="ref git de comparação (ex.: HEAD, main)")
    ap.add_argument("--spec", type=str, default=None, help="spec cujo '## 4. Escopo Declarado' autoriza arquivos")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    try:
        mutations, requires_auth = check_metaconfig(repo, args.against, args.spec)
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
