#!/usr/bin/env python3
"""
Portão Mecânico de Spec Drift (PROTOCOLO v4.0, §3.1, §3.3 e §10).
Compara o diff real do Git contra o escopo declarado em docs/designs/<feature>.md
e verifica a integridade do hash da especificação pós-aprovação.

Anti-TOCTOU: a spec é lida UMA única vez (bytes) e tanto o sha256 quanto o escopo
declarado são derivados do MESMO buffer — impossível combinar hash antigo com
escopo novo se o arquivo for adulterado entre leituras.
"""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_FORBIDDEN_WILDCARD_SCOPES = {"*", "**", "**/*", "./*", "./**", "/*", "/**"}


def _decode_spec(raw: bytes) -> str:
    """Normaliza os bytes da spec (UTF-8, CRLF->LF) — fonte única para hash e escopo."""
    return raw.decode("utf-8").replace("\r\n", "\n")


def _hash_spec_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_spec_hash(spec_path: Path) -> str:
    return _hash_spec_text(_decode_spec(spec_path.read_bytes()))


def _parse_scope_text(text: str) -> list[str]:
    """Extrai os caminhos declarados na seção '## 4. Escopo Declarado' do texto já lido."""
    match = re.search(r"(?m)^## 4\..*?Escopo.*?\n(.*?)(?=\n## |\Z)", text, flags=re.S)
    if not match:
        return []

    section_text = match.group(1)
    paths = []
    # Encontra itens de lista: - `path/to/file`, `path/to/file::symbol` ou - path/to/file
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line_item = line.lstrip("-* ").strip()
        code_matches = re.findall(r"`([^`]+)`", line_item)
        raw_items = code_matches if code_matches else [line_item]
        candidates = [c.split("::")[0].split(":")[0].strip() for c in raw_items]
        for candidate in candidates:
            if candidate and not candidate.startswith("<!--"):
                paths.append(candidate.replace("\\", "/"))
    return paths


def parse_declared_scope(spec_path: Path) -> list[str]:
    """Extrai os caminhos declarados na seção '## 4. Escopo Declarado'."""
    return _parse_scope_text(_decode_spec(spec_path.read_bytes()))


def _parse_symbols_text(text: str) -> list[tuple[str, str]]:
    """Extrai pares (caminho, simbolo) para itens com '::' a partir do texto já lido."""
    match = re.search(r"(?m)^## 4\..*?Escopo.*?\n(.*?)(?=\n## |\Z)", text, flags=re.S)
    if not match:
        return []
    symbols: list[tuple[str, str]] = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line_item = line.lstrip("-* ").strip()
        code_matches = re.findall(r"`([^`]+)`", line_item)
        raw_items = code_matches if code_matches else [line_item]
        for raw in raw_items:
            if "::" in raw and not raw.strip().startswith("<!--"):
                p_part, sym_part = raw.split("::", 1)
                p_clean = p_part.strip().replace("\\", "/")
                sym_clean = sym_part.split()[0].strip()
                if p_clean and sym_clean:
                    symbols.append((p_clean, sym_clean))
    return symbols


def parse_declared_symbols(spec_path: Path) -> list[tuple[str, str]]:
    """Extrai pares (caminho, simbolo) para itens com '::' em '## 4. Escopo Declarado'."""
    return _parse_symbols_text(_decode_spec(spec_path.read_bytes()))


def get_git_root(cwd: Path) -> Path:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True
        )
        return Path(res.stdout.strip()).resolve()
    except Exception as e:
        sys.exit(f"ERRO: Não foi possível determinar a raiz do repositório Git: {e}")


def discover_latest_spec(designs_dir: Path) -> Path | None:
    """Descobre a spec mais recente em docs/designs/*.md ordenada por (mtime, name)."""
    target_dir = designs_dir / "docs" / "designs" if (designs_dir / "docs" / "designs").is_dir() else designs_dir
    if not target_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in target_dir.glob("*.md") if p.is_file() and p.name != "TEMPLATE.md"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    return candidates[-1] if candidates else None


def get_touched_files(git_root: Path, against: str | None = None, active_spec: Path | None = None) -> set[str]:
    """Obtém a lista de arquivos modificados e untracked no repositório."""
    touched = set()

    # 1. Arquivos modificados no diff (passa core.quotePath=false para suporte pleno a Unicode)
    diff_cmd = ["git", "-c", "core.quotePath=false", "diff", "--name-only"]
    if against:
        diff_cmd.append(against)
    res_diff = subprocess.run(diff_cmd, cwd=str(git_root), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    for line in res_diff.stdout.splitlines():
        p = line.strip().strip('"').replace("\\", "/")
        if p:
            touched.add(p)

    # 2. Arquivos no stage (staged)
    res_staged = subprocess.run(
        ["git", "-c", "core.quotePath=false", "diff", "--cached", "--name-only"],
        cwd=str(git_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True
    )
    for line in res_staged.stdout.splitlines():
        p = line.strip().strip('"').replace("\\", "/")
        if p:
            touched.add(p)

    # 3. Arquivos untracked (não rastreados)
    res_status = subprocess.run(
        ["git", "-c", "core.quotePath=false", "status", "--porcelain", "-uall"],
        cwd=str(git_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True
    )
    for line in res_status.stdout.splitlines():
        if line.startswith("??"):
            p = line[3:].strip().strip('"').replace("\\", "/")
            if p:
                touched.add(p)

    # Exclusões estritas (S3): apenas runtime metadata e a spec ativa exata
    ignored_exact = {
        ".henke/gate-baseline.json",
        ".henke/BASELINE.json",
        ".henke/LEDGER.json",
        ".henke/exec_log.jsonl",
        ".henke/DIARIO.md",
        ".henke/DECISOES.md",
        ".henke/MAPA.md",
    }
    if active_spec:
        try:
            ignored_exact.add(str(active_spec.resolve().relative_to(git_root.resolve())).replace("\\", "/"))
        except ValueError:
            pass

    clean_touched = set()
    for p in touched:
        if p.startswith(".git/") or p.startswith(".henke/runs/"):
            continue
        if p.startswith(".agents/") or p.startswith(".agents\\") or p == ".agents":
            continue
        if p in ignored_exact:
            continue
        clean_touched.add(p)
    return clean_touched


def is_path_in_scope(touched_file: str, declared_paths: list[str]) -> bool:
    """Verifica se um arquivo tocado corresponde ou está dentro de algum caminho declarado."""
    norm_touched = touched_file.replace("\\", "/").strip("/")
    for decl in declared_paths:
        norm_decl = decl.replace("\\", "/").strip()
        if norm_decl in _FORBIDDEN_WILDCARD_SCOPES:
            continue
        if norm_decl.endswith("/**"):
            base = norm_decl[:-3].rstrip("/").strip("/")
            if base and (norm_touched == base or norm_touched.startswith(base + "/")):
                return True
            continue
        if norm_decl.endswith("/*"):
            base = norm_decl[:-2].rstrip("/").strip("/")
            if base and (norm_touched == base or norm_touched.startswith(base + "/")):
                return True
            continue
        norm_decl = norm_decl.strip("/")
        if norm_touched == norm_decl or norm_touched.startswith(norm_decl + "/"):
            return True
        if "*" in norm_decl and (fnmatch.fnmatch(norm_touched, norm_decl) or PurePosixPath(norm_touched).match(norm_decl)):
            return True
    return False


def check_spec_drift(
    spec_path: Path,
    expected_hash: str | None = None,
    against: str | None = None,
    git_root: Path | None = None
) -> tuple[bool, list[str]]:
    if not spec_path.is_file():
        return False, [f"Arquivo de especificação não encontrado: '{spec_path}'"]

    errors = []
    # Leitura ÚNICA dos bytes da spec (anti-TOCTOU): hash e escopo derivam do MESMO
    # buffer — impossível casar hash antigo com escopo adulterado entre leituras.
    try:
        spec_text = _decode_spec(spec_path.read_bytes())
    except (OSError, UnicodeDecodeError) as exc:
        return False, [f"Spec ilegível ou não decodificável em UTF-8: '{spec_path}': {exc}"]

    # 1. Verificação de hash pós-aprovação (S1 obrigatório)
    current_hash = _hash_spec_text(spec_text)
    if expected_hash is None:
        errors.append("Spec-hash obrigatório não fornecido (--expected-hash). Aprovação de design não vinculada.")
    elif current_hash != expected_hash:
        errors.append(
            f"Spec-hash alterado pós-aprovação! Esperado: {expected_hash}, Atual: {current_hash}. Aprovação invalidada."
        )

    # 2. Parse do escopo declarado e bloqueio de curinga irrestrito
    declared_scope = _parse_scope_text(spec_text)
    if not declared_scope:
        errors.append(f"Seção '## 4. Escopo Declarado' vazia ou ausente em '{spec_path.name}'.")
    for decl in declared_scope:
        if decl.strip() in _FORBIDDEN_WILDCARD_SCOPES:
            errors.append(
                f"WILDCARD-SCOPE-FORBIDDEN: escopo curinga irrestrito '{decl}' não é permitido na spec."
            )

    if not git_root:
        git_root = get_git_root(spec_path.parent)

    # 3. Auto-detecção de commits anteriores (S2)
    if against is None:
        baseline_file = git_root / ".henke/gate-baseline.json"
        try:
            b_data = json.loads(baseline_file.read_text(encoding="utf-8"))
            against = b_data.get("head") or b_data.get("head_commit")
        except Exception:
            against = None
        if not against:
            return False, [
                "Referência base para comparação não encontrada: forneça '--against <commit>' "
                "ou inicialize o baseline com 'henke_gate.py baseline'."
            ]

    touched = get_touched_files(git_root, against=against, active_spec=spec_path)
    unauthorized = []
    for f in sorted(touched):
        if not is_path_in_scope(f, declared_scope):
            unauthorized.append(f)

    if unauthorized:
        for u in unauthorized:
            errors.append(f"Arquivo modificado fora do escopo declarado na spec: '{u}'")

    return len(errors) == 0, errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, help="Caminho do arquivo de design spec (docs/designs/<feature>.md)")
    parser.add_argument("--expected-hash", type=str, help="Hash SHA256 esperado da spec pré-aprovada")
    parser.add_argument("--against", type=str, default=None, help="Commit/Ref do git para comparação do diff")
    parser.add_argument("--repo", type=Path, default=None, help="Raiz do repositório Git")
    args = parser.parse_args()

    git_root = (args.repo or get_git_root(Path.cwd())).resolve()

    spec_path = args.spec
    if not spec_path:
        designs_dir = git_root / "docs" / "designs"
        spec_path = discover_latest_spec(designs_dir)
        if not spec_path:
            sys.exit("ERRO: Nenhuma spec especificada via --spec e nenhuma encontrada em docs/designs/*.md")

    ok, errors = check_spec_drift(spec_path, expected_hash=args.expected_hash, against=args.against, git_root=git_root)
    if not ok:
        print(f"FALHA: Spec Drift detectado em relação a '{spec_path.name}':", file=sys.stderr)
        for err in errors:
            print(f"  [DRIFT] {err}", file=sys.stderr)
        sys.exit(1)

    print(f"OK: Código em conformidade com o escopo declarado em '{spec_path.name}'. Zero arquivos não autorizados.")


if __name__ == "__main__":
    main()
