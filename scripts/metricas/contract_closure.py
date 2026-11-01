#!/usr/bin/env python3
"""
contract_closure.py — Portão mecânico do Carpinteiro ("nem código de menos, nem ponte quebrada", PROTOCOLO v3.4).

Complementa `spec_drift.py`:
  - Enquanto `spec_drift.py` prova `touched ⊆ declared` ("não escreveu de mais"),
    `contract_closure.py` prova:
    (a) `declared ⊆ existing` (e opcionalmente `declared ⊆ touched` com `--require-touched`):
        todo arquivo obrigatório listado em `## 4. Escopo Declarado` existe de fato;
    (b) Fechamento das pontes declaradas em `## 5. Pontes de Contrato`:
        Formato: `- ponte: <origem>::<simbolo_ou_"canal"> -> <destino>`
        * Modo Identificador (`origem.ts::meuSimbolo -> destino.ts`):
          verifica que `meuSimbolo` existe no código mascarado de `origem` E é
          referenciado no código mascarado de `destino`.
        * Modo String/IPC (`main.ts::"clips:list" -> preload.ts`):
          verifica no texto sem comentários (com strings preservadas!) que o literal
          `"clips:list"` / `'clips:list'` consta em `origem` E em `destino` — provando
          o fechamento de canais IPC/Tauri/Electron (Regra 11 do VirusClip).

Códigos de saída:
  0  escopo completo e todas as pontes fechadas
  1  arquivo declarado faltando ou ponte quebrada
  2  spec inexistente, malformada ou stdout fechado
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
for _p in (str(_HERE), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import polyglot as _pg       # noqa: E402
import hazard_scan as _hs    # noqa: E402
import spec_drift as _sd     # noqa: E402

_BRIDGE_RE = re.compile(
    r"ponte:\s*`?([^`:\s\[]+)::(\"[^\"]+\"|'[^']+'|[A-Za-z_$][\w$]*)\s*->\s*([^`\s\[]+)`?"
    r"(?:\s*\[\s*a[cç][oõ]es\s*:\s*([^\]]+)\])?",
    re.IGNORECASE
)
_CALLSITES_IPC_RE = re.compile(
    r"(?:ipcMain|ipcRenderer)\.[A-Za-z]+\s*\(|\.(?:handle|on|invoke|send|emit|register)\s*\(")
_COMPUTED_FIRST_ARG_RE = re.compile(
    r"\(\s*(?:atob\s*\(|`[^`\n]*\$\{|[\w.]+?\s*\.\s*(?:join|concat)\s*\(|btoa\s*\()")
_HIDDEN_DISPATCH_RE = re.compile(
    r"\b(?:handlers?|routes|actions|cmds?|fns|registry|obj|map)\s*\[\s*[^\]'\"\d][^\]]*\]|getattr\s*\(")


def parse_declared_bridges_with_validation(spec_path: Path) -> tuple[bool, list[tuple[str, str, str, list[str]]], list[str]]:
    """Extrai `(origem, simbolo, destino, ações)` da seção '## 5. Ponte[s]'.
    Retorna (has_section, bridges, malformed_lines)."""
    text = spec_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    match = re.search(r"(?mi)^## 5\..*?Ponte.*?\n(.*?)(?=\n## |\Z)", text, flags=re.S)
    if not match:
        return False, [], []
    bridges: list[tuple[str, str, str, list[str]]] = []
    malformed: list[str] = []
    body_clean = re.sub(r"<!--[\s\S]*?-->", "", match.group(1))
    for line in body_clean.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("-"):
            m = _BRIDGE_RE.search(line)
            if m:
                src = m.group(1).strip().replace("\\", "/")
                sym = m.group(2).strip()
                dst = m.group(3).strip().replace("\\", "/")
                actions = re.findall(r"""["']([^"']+)["']""", m.group(4) or "")
                bridges.append((src, sym, dst, actions))
            else:
                malformed.append(line)
    return True, bridges, malformed


def parse_declared_bridges(spec_path: Path) -> list[tuple[str, str, str, list[str]]]:
    """Extrai `(origem, simbolo, destino, ações)` da seção '## 5. Ponte[s]'."""
    _, bridges, _ = parse_declared_bridges_with_validation(spec_path)
    return bridges


def _check_ipc_handler_hygiene(masked_code: str, rel: str, errors: list[str]) -> None:
    """Reprova callsites de registro IPC com canal computado, dispatch cego e
    switch sem default-deny dentro de ~25 linhas após o callsite."""
    lines = masked_code.splitlines()
    for i, ln in enumerate(lines):
        if not _CALLSITES_IPC_RE.search(ln):
            continue
        after_paren = ln[ln.index("("):]
        if _COMPUTED_FIRST_ARG_RE.match(after_paren):
            errors.append(
                f"COMPUTED-CHANNEL: callsite IPC com primeiro argumento computado "
                f"(atob/join/concat/template interpolado) em '{rel}:{i + 1}'")
        window = "\n".join(lines[i:i + 25])
        m_hd = _HIDDEN_DISPATCH_RE.search(window)
        if m_hd:
            errors.append(
                f"HIDDEN-DISPATCH: handler IPC usa indexação dinâmica cega "
                f"({m_hd.group(0).strip()}) em '{rel}:{i + 1}' — ação escondida do contrato")
        if re.search(r"\bswitch\s*\(", window) and not re.search(r"\bdefault\s*:", window):
            errors.append(
                f"NO-DEFAULT-DENY: handler IPC com switch sem cláusula 'default' em '{rel}:{i + 1}'")


def _is_callsite_arg(literal: str, masked_code: str) -> bool:
    """Literal aparece como argumento direto de um callsite (não declaração morta)."""
    esc = re.escape(literal)
    return bool(re.search(
        r"[\w$.]+\s*\(\s*['\"]" + esc + r"['\"]|[,(]\s*['\"]" + esc + r"['\"]",
        masked_code))


def _strip_comments_keep_strings(raw: str, ext: str) -> str:
    if ext == ".py":
        return _hs._strip_py_comments(raw)
    return _pg.mask_comments_only(raw, ext)


def _code_only(raw: str, ext: str) -> str:
    if ext == ".py":
        return _hs._strip_py_comments(raw)
    return _pg.mask_c_like(raw, ext)


def check_contract_closure(
    spec_path: Path,
    repo_root: Path,
    against: str | None = None,
    require_touched: bool = False,
) -> tuple[bool, list[str]]:
    if not spec_path.is_file():
        return False, [f"SPEC-NOT-FOUND: '{spec_path}'"]

    declared_paths = _sd.parse_declared_scope(spec_path)
    if not declared_paths:
        return False, [f"EMPTY-SCOPE: seção '## 4. Escopo Declarado' vazia ou ausente em '{spec_path.name}'"]

    errors: list[str] = []
    touched = _sd.get_touched_files(repo_root, against=against, active_spec=spec_path) if require_touched else set()

    # 1. Verifica que nenhum arquivo concreto declarado foi esquecido (declared ⊆ existing/touched)
    for decl in declared_paths:
        norm = decl.replace("\\", "/").strip()
        if "*" in norm or norm.endswith("/"):
            continue
        target = (repo_root / norm).resolve()
        if not target.exists():
            errors.append(f"MISSING-DECLARED-FILE: arquivo declarado '{norm}' não existe no repositório")
        elif require_touched and norm not in touched and norm != spec_path.relative_to(repo_root).as_posix():
            errors.append(f"UNTOUCHED-DECLARED-FILE: arquivo declarado '{norm}' não foi tocado no diff")

    # 2. Verifica fechamento de cada ponte declarada em ## 5. Pontes de Contrato
    has_bridges_sec, bridges, malformed = parse_declared_bridges_with_validation(spec_path)
    if malformed:
        for m_line in malformed:
            errors.append(f"MALFORMED-BRIDGE: linha malformada na seção de pontes: '{m_line}' (esperado: '- ponte: <origem>::<simbolo_ou_\"canal\"> -> <destino>')")
    if has_bridges_sec and not bridges and not malformed:
        errors.append(f"EMPTY-BRIDGES: seção '## 5' presente em '{spec_path.name}', mas 0 pontes válidas foram declaradas")

    for src_rel, sym, dst_rel, actions in bridges:
        src_path = (repo_root / src_rel).resolve()
        dst_path = (repo_root / dst_rel).resolve()
        if not src_path.is_file():
            errors.append(f"BROKEN-BRIDGE-SRC: origem '{src_rel}' inexistente para ponte '{sym} -> {dst_rel}'")
            continue
        if not dst_path.is_file():
            errors.append(f"BROKEN-BRIDGE-DST: destino '{dst_rel}' inexistente para ponte '{src_rel}::{sym}'")
            continue

        src_raw = src_path.read_text(encoding="utf-8", errors="replace")
        dst_raw = dst_path.read_text(encoding="utf-8", errors="replace")

        if (sym.startswith('"') and sym.endswith('"')) or (sym.startswith("'") and sym.endswith("'")):
            # Modo String / Canal IPC: verifica literal (com aspas simples ou duplas) sem comentários
            literal = sym[1:-1]
            src_nc = _strip_comments_keep_strings(src_raw, src_path.suffix.lower())
            dst_nc = _strip_comments_keep_strings(dst_raw, dst_path.suffix.lower())
            pat = re.compile(r"""['"`]""" + re.escape(literal) + r"""['"`]""")
            if not pat.search(src_nc):
                errors.append(f"BROKEN-BRIDGE-STRING-SRC: literal {sym} ausente na origem '{src_rel}'")
            elif _CALLSITES_IPC_RE.search(src_nc) and not _is_callsite_arg(literal, src_nc):
                errors.append(
                    f"CHANNEL-LITERAL-DEAD: literal {sym} existe na origem '{src_rel}' mas "
                    "não é argumento direto de callsite (plantação de literal morta)")
            if not pat.search(dst_nc):
                errors.append(f"BROKEN-BRIDGE-STRING-DST: literal {sym} não consumido no destino '{dst_rel}'")
            # v2: par (canal, ação) — cada ação declarada precisa estar viva no destino
            for act in actions:
                pat_act = re.compile(r"""['"`]""" + re.escape(act) + r"""['"`]""")
                if not pat_act.search(dst_nc):
                    errors.append(
                        f"BROKEN-BRIDGE-ACTION: ação '{act}' declarada na ponte {src_rel}::{sym} "
                        f"ausente no destino '{dst_rel}'")
            # v2: higiene de handler IPC nas duas pontas da ponte
            _check_ipc_handler_hygiene(_code_only(src_raw, src_path.suffix.lower()), src_rel, errors)
            _check_ipc_handler_hygiene(_code_only(dst_raw, dst_path.suffix.lower()), dst_rel, errors)
        else:
            # Modo Identificador: verifica símbolo no código mascarado de origem e destino
            src_code = _code_only(src_raw, src_path.suffix.lower())
            dst_code = _code_only(dst_raw, dst_path.suffix.lower())
            pat = re.compile(r"\b" + re.escape(sym) + r"\b")
            if not pat.search(src_code):
                errors.append(f"BROKEN-BRIDGE-SYMBOL-SRC: símbolo '{sym}' ausente na origem '{src_rel}'")
            if not pat.search(dst_code):
                errors.append(f"BROKEN-BRIDGE-SYMBOL-DST: símbolo '{sym}' não referenciado no destino '{dst_rel}'")

    return len(errors) == 0, errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Portão de Fechamento de Contrato (declared ⊆ existing + pontes).")
    ap.add_argument("--spec", required=True, help="Caminho para docs/designs/<feature>.md")
    ap.add_argument("--repo", default=".", help="Raiz do repositório Git")
    ap.add_argument("--against", default=None, help="Commit base opcional para --require-touched")
    ap.add_argument("--require-touched", action="store_true", help="Exige que todo arquivo declarado tenha sido tocado no diff")
    args = ap.parse_args()

    spec_path = Path(args.spec).resolve()
    repo_root = Path(args.repo).resolve()
    if not spec_path.is_file() or not repo_root.is_dir():
        print(f"ERRO: spec ('{spec_path}') ou repo ('{repo_root}') inexistente.", file=sys.stderr)
        sys.exit(2)

    ok, errors = check_contract_closure(
        spec_path, repo_root, against=args.against, require_touched=args.require_touched
    )
    if errors and any(e.startswith(("SPEC-NOT-FOUND", "EMPTY-SCOPE", "EMPTY-BRIDGES", "MALFORMED-BRIDGE")) for e in errors):
        for e in errors:
            print(f"ERRO: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        if not ok:
            for e in errors:
                print(f"FAIL: {e}")
            sys.stdout.flush()
            sys.exit(1)
        bridges = parse_declared_bridges(spec_path)
        decls = _sd.parse_declared_scope(spec_path)
        print(f"OK: {len(decls)} caminho(s) declarado(s) presente(s) e {len(bridges)} ponte(s) de contrato fechada(s).")
        sys.stdout.flush()
        sys.exit(0)
    except BrokenPipeError:
        sys.exit(2)


if __name__ == "__main__":
    main()
