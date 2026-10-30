#!/usr/bin/env python3
"""
Portão Mecânico de Spec Drift (PROTOCOLO v3.1.1, §3.1, §3.3 e §10).
Compara o diff real do Git contra o escopo declarado em docs/designs/<feature>.md
e verifica a integridade do hash da especificação pós-aprovação.
"""
import argparse
import hashlib
from pathlib import Path
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


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


def compute_spec_hash(spec_path: Path) -> str:
    content = spec_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_declared_scope(spec_path: Path) -> list[str]:
    """Extrai os caminhos declarados na seção '## 4. Escopo Declarado'."""
    text = spec_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(r"(?m)^## 4\..*?Escopo.*?\n(.*?)(?=\n## |\Z)", text, flags=re.S)
    if not match:
        return []

    section_text = match.group(1)
    paths = []
    # Encontra itens de lista: - `path/to/file` ou - path/to/file
    for line in section_text.splitlines():
        line = line.strip()
        if not line.startswith("-"):
            continue
        line_item = line.lstrip("-* ").strip()
        # Se contiver crases: - `caminho`: descrição
        code_match = re.search(r"`([^`]+)`", line_item)
        if code_match:
            candidate = code_match.group(1).split(":")[0].strip()
        else:
            candidate = line_item.split(":")[0].strip()
        if candidate and not candidate.startswith("<!--"):
            paths.append(candidate.replace("\\", "/").rstrip("/"))
    return paths


import json

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
        if p.startswith(".git/"):
            continue
        if p in ignored_exact:
            continue
        clean_touched.add(p)
    return clean_touched


def is_path_in_scope(touched_file: str, declared_paths: list[str]) -> bool:
    """Verifica se um arquivo tocado corresponde ou está dentro de algum caminho declarado."""
    norm_touched = touched_file.replace("\\", "/").strip("/")
    for decl in declared_paths:
        norm_decl = decl.replace("\\", "/").strip("/")
        # Correspondência exata de arquivo
        if norm_touched == norm_decl:
            return True
        # Apenas aceita prefixo de diretório se foi explicitamente declarado como tal (terminando em / ou /*)
        if norm_decl.endswith("/") or norm_decl.endswith("/*"):
            dir_prefix = norm_decl.rstrip("/*").rstrip("/") + "/"
            if norm_touched.startswith(dir_prefix):
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
    # 1. Verificação de hash pós-aprovação (S1 obrigatório)
    current_hash = compute_spec_hash(spec_path)
    if expected_hash is None:
        errors.append("Spec-hash obrigatório não fornecido (--expected-hash). Aprovação de design não vinculada.")
    elif current_hash != expected_hash:
        errors.append(
            f"Spec-hash alterado pós-aprovação! Esperado: {expected_hash}, Atual: {current_hash}. Aprovação invalidada."
        )

    # 2. Parse do escopo declarado
    declared_scope = parse_declared_scope(spec_path)
    if not declared_scope:
        errors.append(f"Seção '## 4. Escopo Declarado' vazia ou ausente em '{spec_path.name}'.")

    if not git_root:
        git_root = get_git_root(spec_path.parent)

    # 3. Auto-detecção de commits anteriores (S2)
    if against is None:
        baseline_file = git_root / ".henke/gate-baseline.json"
        if baseline_file.is_file():
            try:
                b_data = json.loads(baseline_file.read_text(encoding="utf-8"))
                against = b_data.get("head")
            except Exception:
                pass

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
        # Tenta auto-descobrir na pasta docs/designs
        designs_dir = git_root / "docs" / "designs"
        candidates = sorted(p for p in designs_dir.glob("*.md") if p.is_file() and p.name != "TEMPLATE.md")
        if candidates:
            spec_path = candidates[-1]  # Mais recente
        else:
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
