#!/usr/bin/env python3
"""Portão mecânico de custódia de baseline (PROTOCOLO v4.0, §5 e §10).

Uso:
  henke_gate.py baseline [--out CAMINHO]     — grava o baseline no início da tarefa
  henke_gate.py check [--baseline CAMINHO] [--scope GLOBO ...] [--allow-commit]

O baseline registra toplevel, branch, HEAD e todo arquivo sujo (index/worktree/
untracked) com hash de conteúdo. O check aprova (exit 0) quando a sujeira
pré-existente do usuário permanece intacta e toda mutação nova pertence ao
escopo declarado; bloqueia (exit 1) em mutação inexplicada, commit não
autorizado sobre o baseline, histórico reescrito ou worktree/branch violada.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
from datetime import datetime, timezone

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

DEFAULT_BASELINE = ".henke/gate-baseline.json"


def git(cwd, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} falhou: {result.stderr.strip()}")
    return result


def file_hash(path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def dirty_state(cwd):
    """Mapa relpath -> {status, hash} de tudo que difere do HEAD/index."""
    out = git(cwd, "status", "--porcelain=v1", "--untracked-files=all").stdout
    state = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        status, raw = line[:2], line[3:]
        rel = raw.split(" -> ")[-1].strip('"')
        if rel.startswith(".agents/") or rel.startswith(".agents\\") or rel == ".agents":
            continue
        state[rel] = {"status": status, "hash": file_hash(Path(cwd) / rel)}
    return state


def get_staged_blobs(cwd):
    blobs = {}
    ls_out = git(cwd, "ls-files", "-s").stdout
    for line in ls_out.splitlines():
        meta, sep, path = line.partition("\t")
        if not sep:
            continue
        parts = meta.split()
        if len(parts) >= 3:
            blobs[path.strip('"')] = parts[1]
    return blobs


def capture_baseline(cwd):
    top = git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
    branch = git(cwd, "branch", "--show-current").stdout.strip()
    head = git(cwd, "rev-parse", "HEAD").stdout.strip()
    return {
        "version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "toplevel": top,
        "branch": branch,
        "head": head,
        "dirty": dirty_state(cwd),
        "staged": get_staged_blobs(cwd),
    }


def in_scope(rel, patterns):
    is_win = sys.platform.startswith("win")
    rel_norm = "/" + str(rel).replace("\\", "/").strip("/")
    cmp_rel = rel_norm.lower() if is_win else rel_norm
    for pattern in patterns:
        pat_norm = "/" + str(pattern).replace("\\", "/").strip("/")
        cmp_pat = pat_norm.lower() if is_win else pat_norm
        if pat_norm.endswith("/**"):
            parent = pat_norm[:-3]
            cmp_parent = parent.lower() if is_win else parent
            if cmp_rel == cmp_parent or cmp_rel.startswith(cmp_parent + "/"):
                return True
        if PurePosixPath(cmp_rel).match(cmp_pat):
            return True
        if pat_norm.endswith("/*"):
            parent = pat_norm[:-2]
            cmp_parent = parent.lower() if is_win else parent
            if cmp_rel.startswith(cmp_parent + "/") and "/" not in cmp_rel[len(cmp_parent) + 1:]:
                return True
    return False


_FORBIDDEN_WILDCARD_SCOPES = {"*", "**", "**/*", "./*", "./**", "/*", "/**"}


def check_baseline(baseline, cwd, scope, allow_commit):
    violations, warnings = [], []
    for pat in scope or []:
        if str(pat).strip() in _FORBIDDEN_WILDCARD_SCOPES:
            violations.append(f"WILDCARD-SCOPE-FORBIDDEN: escopo curinga proibido '{pat}'")
    top = git(cwd, "rev-parse", "--show-toplevel").stdout.strip()
    if Path(top).resolve() != Path(baseline["toplevel"]).resolve():
        violations.append(f"Worktree/toplevel diverge do baseline: '{top}' != '{baseline['toplevel']}'")
        return violations, warnings  # repo errado: demais checagens não fazem sentido

    branch = git(cwd, "branch", "--show-current").stdout.strip()
    if branch != baseline["branch"]:
        violations.append(f"Branch diverge do baseline: '{branch}' != '{baseline['branch']}'")

    head = git(cwd, "rev-parse", "HEAD").stdout.strip()
    baseline_file_rel = baseline.get("_self_rel")
    if head != baseline["head"]:
        if not allow_commit:
            violations.append(f"HEAD moveu de {baseline['head'][:8]} para {head[:8]} sem --allow-commit")
        elif git(cwd, "merge-base", "--is-ancestor", baseline["head"], "HEAD", check=False).returncode != 0:
            violations.append(f"Histórico reescrito: {baseline['head'][:8]} não é ancestral de HEAD")
        else:
            touched = git(cwd, "diff", "--name-only", baseline["head"], "HEAD").stdout.splitlines()
            for rel in touched:
                if rel.startswith(".agents/") or rel.startswith(".agents\\") or rel == ".agents":
                    continue
                if rel != baseline_file_rel and not in_scope(rel, scope):
                    violations.append(f"Commit tocou arquivo fora do escopo declarado: {rel}")

    baseline_dirty = baseline.get("dirty", {})
    current_dirty = dirty_state(cwd)
    for rel, snap in baseline_dirty.items():
        if rel == baseline_file_rel:
            continue
        now = current_dirty.get(rel)
        if now is None:
            violations.append(f"Sujeira do usuário absorvida ou removida: {rel} estava '{snap['status']}' no baseline e sumiu do status")
        elif now["hash"] != snap["hash"]:
            if in_scope(rel, scope):
                violations.append(f"Arquivo sujo no baseline teve conteúdo sobrescrito: {rel} (sujeira preexistente do usuário não pode ser destruída)")
            else:
                violations.append(f"Arquivo fora do escopo teve conteúdo alterado: {rel}")

    # Verificar integridade do índice staged preexistente (G2)
    baseline_staged = baseline.get("staged", {})
    current_staged = get_staged_blobs(cwd)
    for path, old_blob in baseline_staged.items():
        if path == baseline_file_rel:
            continue
        new_blob = current_staged.get(path)
        if new_blob != old_blob:
            was_user_staged = (
                path in baseline_dirty
                and baseline_dirty[path]["status"][:1] not in (" ", "?")
            )
            if allow_commit and in_scope(path, scope) and not was_user_staged:
                continue
            violations.append(f"Índice Git (staged) do usuário foi alterado ou absorvido: {path}")

    for rel, now in current_dirty.items():
        if rel == baseline_file_rel:
            continue
        if rel.startswith(".agents/") or rel.startswith(".agents\\") or rel == ".agents":
            continue
        if rel not in baseline_dirty and not in_scope(rel, scope):
            violations.append(f"Mutação fora do escopo declarado: {rel} ('{now['status']}')")
    return violations, warnings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_base = sub.add_parser("baseline", help="Grava o baseline de custódia no início da tarefa")
    p_base.add_argument("--out", type=Path, default=None)
    p_base.add_argument("--force", action="store_true", help="Sobrescreve baseline existente")
    p_check = sub.add_parser("check", help="Compara o estado atual contra o baseline e o escopo")
    p_check.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório (default: .)")
    p_check.add_argument("--baseline", type=Path, default=None)
    p_check.add_argument("--spec", type=Path, default=None, help="Spec de referência para escopo declarado")
    p_check.add_argument("--scope", nargs="*", default=[], metavar="GLOBO",
                         help="Padrões de path permitidos (ex.: 'scripts/*', 'tests/**', 'PROTOCOLO.md')")
    p_check.add_argument("--allow-commit", action="store_true",
                         help="Permite commits novos desde o baseline (descendentes, dentro do escopo)")
    args = parser.parse_args(argv)

    cwd = Path(getattr(args, "repo", Path.cwd())).resolve()
    try:
        if args.command == "baseline":
            out = args.out or Path(git(cwd, "rev-parse", "--show-toplevel").stdout.strip()) / DEFAULT_BASELINE
            if out.exists() and not getattr(args, "force", False):
                print(f"ERRO: Baseline já existente em {out}. Conclua a tarefa com 'check' ou use '--force' para recapturar.", file=sys.stderr)
                return 1
            baseline = capture_baseline(cwd)
            try:
                baseline["_self_rel"] = out.resolve().relative_to(Path(baseline["toplevel"]).resolve()).as_posix()
            except ValueError:
                pass  # baseline fora do repo: não aparece como mutação
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"Baseline gravado em {out}: branch '{baseline['branch']}', HEAD {baseline['head'][:8]}, {len(baseline['dirty'])} arquivos sujos registrados.")
            return 0

        baseline_path = args.baseline or Path(git(cwd, "rev-parse", "--show-toplevel").stdout.strip()) / DEFAULT_BASELINE
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        scope = list(args.scope) if args.scope else []
        spec_path = getattr(args, "spec", None)
        if not scope and not spec_path:
            try:
                try:
                    from scripts.spec_drift import discover_latest_spec
                except ModuleNotFoundError:
                    from spec_drift import discover_latest_spec
                spec_path = discover_latest_spec(cwd)
            except Exception:
                spec_path = None
        if spec_path and Path(spec_path).is_file():
            try:
                try:
                    from scripts.spec_drift import parse_declared_scope
                except ModuleNotFoundError:
                    from spec_drift import parse_declared_scope
                for pat in parse_declared_scope(Path(spec_path)):
                    if pat not in scope:
                        scope.append(pat)
            except Exception:
                pass
        if ".henke/**" not in scope:
            scope.append(".henke/**")
        if ".agents/**" not in scope:
            scope.append(".agents/**")

        violations, warnings = check_baseline(baseline, cwd, scope, args.allow_commit)
        for warning in warnings:
            print(f"  [AVISO] {warning}")
        if violations:
            print("PORTÃO BLOQUEADO:")
            for violation in violations:
                print(f"  [VIOLAÇÃO] {violation}")
            return 1
        print("PORTÃO APROVADO: baseline do usuário preservado; mutações restritas ao escopo declarado.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Portão falhou: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
