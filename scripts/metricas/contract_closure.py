#!/usr/bin/env python3
"""
contract_closure.py — Portão mecânico do Carpinteiro ("nem código de menos, nem ponte quebrada", PROTOCOLO v4.0).

Complementa `spec_drift.py`:
  - Enquanto `spec_drift.py` prova `touched ⊆ declared` ("não escreveu de mais"),
    `contract_closure.py` prova:
    (a) `declared ⊆ existing` (e opcionalmente `declared ⊆ touched` com `--require-touched`):
        todo arquivo obrigatório listado em `## 4. Escopo Declarado` existe de fato;
    (b) Fechamento das pontes declaradas em `## 5. Pontes de Contrato`:
        Formato: `- ponte: <origem>::<simbolo_ou_"canal"_ou_"ROUTE:METHOD /path"> -> <destino>`
        * Modo Identificador (`origem.ts::meuSimbolo -> destino.ts`):
          verifica que `meuSimbolo` é definido/exportado no topo de `origem` E é
          importado e referenciado no código de `destino`.
        * Modo Rota HTTP/REST (`origem.py::"ROUTE:GET /api/users/{id}" -> destino.ts`):
          normaliza parâmetros (`{id}` <-> `:id` <-> `<int:id>`) e verifica que a rota
          existe na origem e é consumida no destino.
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
import ast
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
    r"ponte:\s*`?([^`:\s\[]+)::(\"[^\"]+\"|'[^']+'|ROUTE:\s*[A-Z]+\s+\S+|[A-Za-z_$][\w$]*)\s*->\s*([^`\s\[]+)`?"
    r"(?:\s*\[\s*a[cç][oõ]es\s*:\s*([^\]]+)\])?",
    re.IGNORECASE
)
_CALLSITES_IPC_RE = re.compile(
    r"(?:ipcMain|ipcRenderer)\.[A-Za-z]+\s*\(|\.(?:handle|on|invoke|send|emit|register)\s*\(")
_COMPUTED_FIRST_ARG_RE = re.compile(
    r"\(\s*(?:atob\s*\(|`[^`\n]*\$\{|[\w.]+?\s*\.\s*(?:join|concat)\s*\(|btoa\s*\()")
_HIDDEN_DISPATCH_RE = re.compile(
    r"\b(?:handlers?|routes|actions|cmds?|fns|registry|obj|map)\s*\[\s*[^\]'\"\d][^\]]*\]|getattr\s*\(")
_ROUTE_BRIDGE_RE = re.compile(r"^ROUTE:\s*(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(\S+)$", re.IGNORECASE)
_ROUTE_PARAM_SPLIT_RE = re.compile(r"(\{[^}/]+\}|:[A-Za-z_][\w]*|<[^>]+>|\$\{[^}]+\})")


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


def _build_route_regex(route_path: str) -> re.Pattern:
    parts = _ROUTE_PARAM_SPLIT_RE.split(route_path.strip())
    regex_parts: list[str] = []
    param_pat = r"(?:\{[^}/'\"`\s]+\}|:[A-Za-z_][\w]*|<[^>/'\"`\s]+>|\$\{[^}]+\}|['\"`]\s*\+\s*[\w$.]+)"
    for part in parts:
        if not part:
            continue
        if _ROUTE_PARAM_SPLIT_RE.fullmatch(part):
            regex_parts.append(param_pat)
        else:
            regex_parts.append(re.escape(part))
    return re.compile("".join(regex_parts))


def _has_route_with_method(text: str, route_re: re.Pattern, method: str, window_chars: int = 300) -> bool:
    """Verifica se a rota e o método HTTP coexistem em proximidade contextual (mesmo decorator, chamada ou bloco)."""
    method_re = re.compile(r"(?i)\b" + re.escape(method) + r"\b")
    for m in route_re.finditer(text):
        start = max(0, m.start() - window_chars)
        end = min(len(text), m.end() + window_chars)
        window = text[start:end]
        if method_re.search(window):
            return True
        if method == "GET" and re.search(r"(?i)\bget\b", window):
            return True
    return False


def _iter_module_level_nodes(stmts: list[ast.stmt]):
    for node in stmts:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
            for child_attr in ("body", "orelse", "handlers", "finalbody"):
                sub = getattr(node, child_attr, None)
                if isinstance(sub, list):
                    for item in sub:
                        if isinstance(item, ast.ExceptHandler):
                            yield from _iter_module_level_nodes(item.body)
                        elif isinstance(item, ast.stmt):
                            yield from _iter_module_level_nodes([item])
        elif isinstance(node, ast.ClassDef):
            yield from _iter_module_level_nodes(node.body)


def _is_top_level_symbol(src_raw: str, src_code: str, ext: str, sym: str) -> bool:
    if ext == ".py":
        try:
            tree = ast.parse(src_raw)
            for node in _iter_module_level_nodes(tree.body):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == sym:
                    return True
                if isinstance(node, ast.Assign):
                    for tgt in node.targets:
                        for sub in ast.walk(tgt):
                            if isinstance(sub, ast.Name) and sub.id == sym:
                                return True
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == sym:
                    return True
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if (alias.asname or alias.name.split(".")[0]) == sym:
                            return True
            return False
        except SyntaxError:
            return bool(re.search(
                r"(?m)^(?:async\s+)?(?:def|class)\s+" + re.escape(sym) + r"\b|^" + re.escape(sym) + r"\s*[:=]",
                src_code
            ))

    if ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".rs", ".go"):
        if re.search(
            r"\bexport\b[^\n;{]*\b" + re.escape(sym) + r"\b"
            r"|\bexport\s*\{[^}]*\b" + re.escape(sym) + r"\b"
            r"|\b(?:module\.)?exports\b[^\n;]*\b" + re.escape(sym) + r"\b"
            r"|\bpub\b[^\n;{]*\b" + re.escape(sym) + r"\b",
            src_code
        ):
            return True
        fns = list(_pg.find_functions(src_raw, ext))
        for fn_name, _sl, _el, _ in fns:
            if fn_name == sym:
                return True
        code_lines = src_code.splitlines(keepends=True)
        top_lines = list(code_lines)
        for _fn_name, sl, el, _ in fns:
            for idx in range(max(0, sl - 1), min(el, len(top_lines))):
                top_lines[idx] = "\n"
        return bool(re.search(r"\b" + re.escape(sym) + r"\b", "".join(top_lines)))

    return True


def _has_valid_import(dst_nc: str, src_rel: str, src_stem: str, sym: str) -> bool:
    esc_stem = re.escape(src_stem)
    esc_sym = re.escape(sym)
    esc_name = re.escape(Path(src_rel).name)
    target_pat = rf"(?:\b{esc_stem}\b|\b{esc_sym}\b|{esc_name})"

    if re.search(rf"(?m)^\s*(?:from|import|export\s+.*?\bfrom|use|pub\s+use|mod)\b[^\n;]*{target_pat}", dst_nc):
        return True
    for m_imp in re.finditer(r"(?m)^\s*(?:from|import|export)\b[^;\n]*(?:\([^)]*\)|\{[^}]*\})?[^\n;]*", dst_nc):
        if re.search(target_pat, m_imp.group(0)):
            return True
    if re.search(
        rf"\b(?:require|import|spec_from_file_location|load_module|run|run_gate|subprocess\.run)\s*\([^)]*{target_pat}",
        dst_nc,
        re.S,
    ):
        return True
    return False


def check_contract_closure(
    spec_path: Path,
    repo_root: Path,
    against: str | None = None,
    require_touched: bool = False,
) -> tuple[bool, list[str]]:
    if not spec_path.is_file():
        return False, [f"SPEC-NOT-FOUND: '{spec_path}'"]

    repo_resolved = repo_root.resolve()
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
        if not target.is_relative_to(repo_resolved):
            errors.append(f"PATH-TRAVERSAL-BRIDGE: caminho declarado '{norm}' escapa da raiz do repositório")
            continue
        if not target.exists():
            errors.append(f"MISSING-DECLARED-FILE: arquivo declarado '{norm}' não existe no repositório")
        elif require_touched and norm not in touched:
            try:
                spec_rel = spec_path.resolve().relative_to(repo_resolved).as_posix()
            except ValueError:
                spec_rel = ""
            if norm != spec_rel:
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
        if not src_path.is_relative_to(repo_resolved) or not dst_path.is_relative_to(repo_resolved):
            errors.append(f"PATH-TRAVERSAL-BRIDGE: ponte '{src_rel} -> {dst_rel}' escapa da raiz do repositório")
            continue
        if not src_path.is_file():
            errors.append(f"BROKEN-BRIDGE-SRC: origem '{src_rel}' inexistente para ponte '{sym} -> {dst_rel}'")
            continue
        if not dst_path.is_file():
            errors.append(f"BROKEN-BRIDGE-DST: destino '{dst_rel}' inexistente para ponte '{src_rel}::{sym}'")
            continue

        src_raw = src_path.read_text(encoding="utf-8", errors="replace")
        dst_raw = dst_path.read_text(encoding="utf-8", errors="replace")
        src_ext = src_path.suffix.lower()
        dst_ext = dst_path.suffix.lower()

        is_quoted = (sym.startswith('"') and sym.endswith('"')) or (sym.startswith("'") and sym.endswith("'"))
        unquoted_sym = sym[1:-1].strip() if is_quoted else sym.strip()

        if unquoted_sym.upper().startswith("ROUTE:"):
            m_route = _ROUTE_BRIDGE_RE.match(unquoted_sym)
            if not m_route:
                errors.append(f"MALFORMED-BRIDGE: sintaxe de rota inválida em '{sym}' (esperado 'ROUTE:METHOD /path')")
                continue
            method = m_route.group(1).upper()
            route_path = m_route.group(2).strip()
            route_re = _build_route_regex(route_path)
            src_nc = _strip_comments_keep_strings(src_raw, src_ext)
            dst_nc = _strip_comments_keep_strings(dst_raw, dst_ext)

            if not _has_route_with_method(src_nc, route_re, method):
                errors.append(f"BROKEN-BRIDGE-ROUTE-SRC: rota '{unquoted_sym}' ausente na origem '{src_rel}'")
            if not route_re.search(dst_nc) or (method != "GET" and not _has_route_with_method(dst_nc, route_re, method)):
                errors.append(f"BROKEN-BRIDGE-ROUTE-DST: rota '{unquoted_sym}' não consumida no destino '{dst_rel}'")
        elif is_quoted:
            # Modo String / Canal IPC: verifica literal (com aspas simples ou duplas) sem comentários
            literal = sym[1:-1]
            src_nc = _strip_comments_keep_strings(src_raw, src_ext)
            dst_nc = _strip_comments_keep_strings(dst_raw, dst_ext)
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
            _check_ipc_handler_hygiene(_code_only(src_raw, src_ext), src_rel, errors)
            _check_ipc_handler_hygiene(_code_only(dst_raw, dst_ext), dst_rel, errors)
        else:
            # Modo Identificador: verifica símbolo top-level/exportado na origem e import/uso no destino
            src_code = _code_only(src_raw, src_ext)
            dst_code = _code_only(dst_raw, dst_ext)
            dst_nc = _strip_comments_keep_strings(dst_raw, dst_ext)
            pat = re.compile(r"\b" + re.escape(sym) + r"\b")
            if not pat.search(src_code):
                errors.append(f"BROKEN-BRIDGE-SYMBOL-SRC: símbolo '{sym}' ausente na origem '{src_rel}'")
            elif not _is_top_level_symbol(src_raw, src_code, src_ext, sym):
                errors.append(f"BROKEN-BRIDGE-SYMBOL-SRC: símbolo '{sym}' é apenas local (não exportado no topo) em '{src_rel}'")

            if not pat.search(dst_code):
                errors.append(f"BROKEN-BRIDGE-SYMBOL-DST: símbolo '{sym}' não referenciado no destino '{dst_rel}'")
            elif not _has_valid_import(dst_nc, src_rel, src_path.stem, sym):
                errors.append(f"BROKEN-BRIDGE-IMPORT: destino '{dst_rel}' referencia '{sym}' mas não importa '{src_rel}'")

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
