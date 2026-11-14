#!/usr/bin/env python3
"""
Portão Mecânico de Spec Drift (PROTOCOLO v5.0, §3.1, §3.3 e §10).
Compara o diff real do Git contra o escopo declarado em docs/designs/<feature>.md
e verifica a integridade do hash da especificação pós-aprovação.

Anti-TOCTOU: a spec é lida UMA única vez (bytes) e tanto o sha256 quanto o escopo
declarado são derivados do MESMO buffer — impossível combinar hash antigo com
escopo novo se o arquivo for adulterado entre leituras.
"""
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

_SUITE_ROOT = Path(__file__).resolve().parents[1]
if str(_SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUITE_ROOT))

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


def _md_clean(text: str) -> str:
    r"""H5 (banca v14): comentários HTML e fenced code blocks podiam conter
    `## 4.` falso ou itens `- \`path\`` — o parser lia o que o humano não
    aprovou. Strip antes de qualquer parse de seção."""
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"(?m)^```[^\n]*\n[\s\S]*?^```\s*$", " ", text)
    return text


# H5 (banca v14): `.*?Escopo` casava 'Itens fora de Escopo' (inversão
# semântica — exclusões viravam escopo). Rejeita negação antes do termo.
_SCOPE_HEAD_RE = re.compile(
    # BANCA-SEC: wordlist de negação era loteria — 'off-limits' e 'vedado'
    # passavam como escopo autorizado.
    r"(?m)^## 4\.\s+(?![^\n]*\b(?:fora|non-|not\s|out\s+of|exclu|proibid|"
    r"n[aã]o|negad|forbid|denied|restrit|unauthor|off.?\s?limit|vedad|"
    r"barrad|impedid))Escopo\b[^\n]*?\n"
    r"(.*?)(?=\n#{1,6} |\Z)", flags=re.S)


def _parse_scope_text(text: str) -> list[str]:
    """Extrai os caminhos declarados na seção '## 4. Escopo Declarado' do texto já lido."""
    # A seção termina em QUALQUER heading seguinte (#, ##, ###...) — uma
    # subseção '### 4.1 Não-autorizado' não pode injetar itens no escopo (B1).
    match = _SCOPE_HEAD_RE.search(_md_clean(text))
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
    match = _SCOPE_HEAD_RE.search(_md_clean(text))
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


# A11 (banca v10): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG_* plantados
# no ambiente redirecionariam o git dos vereditos para outro repo/index.
def _GIT_ENV_F(repo: Path) -> dict:
    # Banca-A 2: PATH filtrado — shim `git` no repo resolvia dentro do scan.
    from henke.state_paths import sanitized_child_env
    return sanitized_child_env(repo)


def get_git_root(cwd: Path) -> Path:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
            env=_GIT_ENV_F(cwd),
        )
        return Path(res.stdout.strip()).resolve()
    except Exception as e:
        # Infra (sem git root) ≠ veredito-vermelho — exit 2, não 1.
        print(f"ERRO: Não foi possível determinar a raiz do repositório Git: {e}",
              file=sys.stderr)
        sys.exit(2)


def discover_latest_spec(designs_dir: Path) -> Path | None:
    """Descobre a spec mais recente em docs/designs/*.md ordenada por (mtime, name).

    NUNCA cai para '*.md' da raiz: README/AGENTS.md não são spec — sem
    docs/designs/ a resposta honesta é None (fail-closed), não lixo como escopo.
    """
    target_dir = designs_dir / "docs" / "designs" if (designs_dir / "docs" / "designs").is_dir() else designs_dir
    if target_dir.name != "designs" or not target_dir.is_dir():
        return None
    candidates = sorted(
        (p for p in target_dir.glob("*.md") if p.is_file() and p.name != "TEMPLATE.md"),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    return candidates[-1] if candidates else None


def _git_paths_z(git_root: Path, *args: str) -> list[str]:
    """Paths de um comando git com saída `-z` (NUL-safe): nomes com \\n/aspas/
    espaços-nas-pontas não forjam nem se quebram em pseudo-paths."""
    try:
        res = subprocess.run(
            ["git", "-c", "core.quotePath=false",
             # Repo hostil pode configurar core.fsmonitor=<cmd> (executado pelo
             # status/diff) e diff.external/textconv (executado em diffs com
             # patch) — neutralizado para TODA enumeração (banca v7 — F-02).
             # BANCA-SEC: filter.*.clean via .gitattributes também executa —
             # attr-file global isolado + lfs off (drivers custom seguem
             # needs_validation, documentado no relatório).
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "-c", f"core.attributesFile={os.devnull}",
             "-c", "filter.lfs.clean=", "-c", "filter.lfs.smudge=",
             "-c", "filter.lfs.process=", "-c", "filter.lfs.required=false",
             *args],
            cwd=str(git_root), capture_output=True, check=True, timeout=60,
            env=_GIT_ENV_F(git_root),
        )
    # F36 v11 + H4 v15: CalledProcessError/TimeoutExpired/OSError escapavam
    # como traceback exit 1 — crash veste código de veredito. RuntimeError→2.
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError) as exc:
        raise RuntimeError(
            f"git {' '.join(args)} falhou ({type(exc).__name__})") from exc
    return [p for p in res.stdout.decode("utf-8", errors="replace").split("\0") if p]


def get_touched_files(git_root: Path, against: str | None = None, active_spec: Path | None = None, *, staged_only: bool = False) -> set[str]:
    """Obtém a lista de arquivos modificados e untracked no repositório."""
    # Ref-âncora validada AQUI também: o files_changed_audit chama esta função
    # direto com a âncora do sidecar — `--cached`/`--output=...` injetado no
    # argv do git mudaria a semântica do diff (banca v7 — C2). check_spec_drift
    # valida antes de chegar aqui; esta é a trava do consumidor direto.
    if against:
        if against.startswith("-"):
            raise ValueError(f"ref '--against' inválida (parece flag): {against!r}")
        # +revisão cega: TimeoutExpired vazava traceback exit 1 (veredito
        # vermelho) — mesma classe F36 v11 do _git_paths_z. RuntimeError→2.
        try:
            vres = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet",
                 f"{against}^{{commit}}"],
                cwd=str(git_root), capture_output=True, timeout=30,
                env=_GIT_ENV_F(git_root),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RuntimeError(
                f"git rev-parse falhou ({type(exc).__name__})") from exc
        if vres.returncode != 0:
            raise ValueError(f"ref '--against' não resolve para commit: {against!r}")

    # Arquivos com assume-unchanged/skip-worktree são INVISÍVEIS ao diff/status:
    # `git update-index --assume-unchanged package.json` + editar = mutação
    # que nenhum portão de diff enxerga (banca v7 — C1). `ls-files -v` marca
    # tag minúscula (assume-unchanged) ou 'S' (skip-worktree) → fail-closed.
    blind = [
        e[2:] for e in _git_paths_z(git_root, "ls-files", "-v", "-z")
        if e[:1] and (e[:1].islower() or e[:1] == "S")
    ]
    if blind:
        raise RuntimeError(
            "GIT-BLIND-FILES: assume-unchanged/skip-worktree deixa arquivos "
            "invisíveis ao diff — " + ", ".join(sorted(blind)[:10])
        )

    if staged_only:
        # Snapshot do índice: nunca mistura arquivos unstaged com o commit.
        # --against compara o índice ao commit âncora, inclusive diferenças
        # já commitadas após a aprovação da spec.
        diff_args = ["diff", "--cached", "--name-only", "-z"]
        if against:
            diff_args.append(against)
        touched = set(_git_paths_z(git_root, *diff_args))
    else:
        touched = set()

    # 1+2. Modificados e staged (diff worktree + --cached), NUL-safe
    if not staged_only:
        diff_args = ["diff", "--name-only", "-z"]
        if against:
            diff_args.append(against)
        touched.update(_git_paths_z(git_root, *diff_args))
        touched.update(_git_paths_z(git_root, "diff", "--cached", "--name-only", "-z"))

        # Untracked não pertence ao índice, portanto só no modo worktree.
        for entry in _git_paths_z(git_root, "status", "--porcelain", "-z", "-uall"):
            if entry.startswith("??"):
                touched.add(entry[3:])

    # Exclusões estritas (S3): APENAS artefatos transitórios de runtime da run.
    # A spec ativa NÃO é excluída — adulterá-la para ampliar escopo é exatamente o
    # ataque que este portão existe para detectar. Memória (.henke/*.md), configs
    # (.agents/, fence.json, baseline) e locks de terceiros também são drift:
    # qualquer escrita fora do escopo declarado deve ser visível.
    ignored_exact = {
        ".henke/gate-baseline.json",
        ".henke/BASELINE.json",
        ".henke/LEDGER.json",
        ".henke/exec_log.jsonl",
        ".henke/events.jsonl",
        ".henke/auto-writer.lock",
        ".henke-install.lock",
    }
    clean_touched = set()
    for p in touched:
        p = p.replace("\\", "/")
        if (p.startswith(".git/") or p.startswith(".henke/runs/")
                or p.startswith(".henke/evidence/")):
            continue
        if p in ignored_exact:
            continue
        clean_touched.add(p)
    return clean_touched


def _is_wildcard_scope(decl: str) -> bool:
    """Escopo curinga irrestrito? Normaliza (`*/`→`*`, `/**/`→`**`, `./**`→`**`)
    antes de comparar — um decl reduzido a só `*`, `/` e `.` cobre o repo inteiro."""
    norm = decl.replace("\\", "/").strip()
    if norm in _FORBIDDEN_WILDCARD_SCOPES:
        return True
    reduced = norm.strip("/")
    return not reduced.strip("./").strip("*")


def _scope_glob_re(pattern: str) -> re.Pattern:
    """Glob de escopo ancorado À ESQUERDA com semântica de path: `*` e `?` não
    atravessam `/`; `**` atravessa. `PurePosixPath.match` casa por SUFIXO
    ('src/*.py' autorizaria 'vendor/src/x.py' — B1 banca v6) e `fnmatch` deixa
    `*` comer '/'. Este tradutor é o meio-termo estrito correto."""
    out: list[str] = ["^"]
    i = 0
    n = len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            # Classes de caracteres são glob real: 'docs/[wv]*.md' deve casar
            # 'docs/wave-x.md' (banca v8 — F3). Copia o conteúdo da classe com
            # escape do ']' interno; classe vazia/não-fechada vira literal.
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j < n and j > i + 1:
                body = pattern[i + 1:j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j + 1
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        elif pattern[i] == "\\" and i + 1 < n:
            # '\*' é literal escapado — não pode globar (banca v8 — F3).
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def is_path_in_scope(touched_file: str, declared_paths: list[str]) -> bool:
    """Verifica se um arquivo tocado corresponde ou está dentro de algum caminho declarado."""
    norm_touched = touched_file.replace("\\", "/").strip("/")
    for decl in declared_paths:
        norm_decl = decl.replace("\\", "/").strip()
        if _is_wildcard_scope(norm_decl):
            continue
        if norm_decl.endswith("/**"):
            base = norm_decl[:-3].rstrip("/").strip("/")
            if base and (norm_touched == base or norm_touched.startswith(base + "/")):
                return True
            continue
        if norm_decl.endswith("/*"):
            # 'docs/*' autoriza UM nível ('docs/x') — nunca atravessa '/' como
            # 'docs/**' faria. Antes, o prefixo solto autorizava 'docs/a/b/c'
            # divergindo de _scope_glob_re e de henke_gate.in_scope (banca v8 F4).
            base = norm_decl[:-2].rstrip("/").strip("/")
            if base and norm_touched.startswith(base + "/"):
                rest = norm_touched[len(base) + 1:]
                if rest and "/" not in rest:
                    return True
            continue
        norm_decl = norm_decl.strip("/")
        if norm_touched == norm_decl or norm_touched.startswith(norm_decl + "/"):
            return True
        if ("*" in norm_decl or "?" in norm_decl) and _scope_glob_re(norm_decl).match(norm_touched):
            return True
    return False


def check_spec_drift(
    spec_path: Path,
    expected_hash: str | None = None,
    against: str | None = None,
    git_root: Path | None = None,
    staged_only: bool = False,
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
        if _is_wildcard_scope(decl):
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

    # A ref-âncora do diff é dado sensível (pode vir de .henke/gate-baseline.json,
    # arquivo ao alcance de escrita do driver): rejeita flags (`--output=...`) e
    # exige commit verificável antes de alimentar o argv do git.
    if against.startswith("-"):
        return False, [f"Ref '--against' inválida (parece flag): {against!r}"]
    vres = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{against}^{{commit}}"],
        cwd=str(git_root), capture_output=True, text=True, timeout=30,
        env=_GIT_ENV_F(git_root),
    )
    if vres.returncode != 0:
        return False, [
            f"Ref '--against' não resolve para commit: {against!r} — "
            "âncora do diff corrompida ou inválida (fail-closed)."
        ]

    touched = get_touched_files(git_root, against=against, active_spec=spec_path, staged_only=staged_only)
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
    parser.add_argument("--staged-index", action="store_true", help="Verifica apenas arquivos preparados no índice Git")
    try:
        args = parser.parse_args()
    except SystemExit:
        # --help sai 0 no argparse — um portão sem veredito nunca é verde.
        sys.exit(2)

    git_root = (args.repo or get_git_root(Path.cwd())).resolve()

    spec_path = args.spec
    if not spec_path:
        designs_dir = git_root / "docs" / "designs"
        spec_path = discover_latest_spec(designs_dir)
        if not spec_path:
            print("ERRO: Nenhuma spec especificada via --spec e nenhuma encontrada em docs/designs/*.md",
                  file=sys.stderr)
            sys.exit(2)

    # F2 (opus v15): RuntimeError de timeout/OSError do git virava traceback
    # exit 1 (código de veredito-vermelho) — erro de infra sai como 2.
    try:
        ok, errors = check_spec_drift(spec_path, expected_hash=args.expected_hash, against=args.against, git_root=git_root, staged_only=args.staged_index)
    except RuntimeError as exc:
        print(f"ERRO: spec drift indisponível: {exc}", file=sys.stderr)
        sys.exit(2)
    if not ok:
        print(f"FALHA: Spec Drift detectado em relação a '{spec_path.name}':", file=sys.stderr)
        for err in errors:
            print(f"  [DRIFT] {err}", file=sys.stderr)
        sys.exit(1)

    print(f"OK: Código em conformidade com o escopo declarado em '{spec_path.name}'. Zero arquivos não autorizados.")


if __name__ == "__main__":
    # ISO-BOOT canônico (henke/_isoboot.py) — sitecustomize/usercustomize/
    # .pth hostis executam no startup e patcham verificadores no pai; o
    # respawn -Es nasce sem esses canais (prova ESTRUTURAL: sys.flags
    # autenticado + os.sys — nunca variável de ambiente). Texto sincronizado
    # por tests/test_isoboot_guard.py — editar só alterando o canônico.
    import importlib.util as _ilu
    import os.path as _osp
    _ib_p = _osp.join(_osp.dirname(_osp.abspath(__file__)), "_isoboot.py")
    if not _osp.isfile(_ib_p):
        _ib_p = _osp.join(_osp.dirname(_osp.dirname(_osp.abspath(__file__))),
                          "_isoboot.py")
    _ib_spec = _ilu.spec_from_file_location("henke_isoboot", _ib_p)
    _ib_mod = _ilu.module_from_spec(_ib_spec)
    _ib_spec.loader.exec_module(_ib_mod)
    _ib_mod.ensure_isolated(__file__, sys.argv[1:])
    main()
