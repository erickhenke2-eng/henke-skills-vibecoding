#!/usr/bin/env python3
"""
a11y_scan.py — Portão mecânico standalone de acessibilidade e anti-cheat .sr-only (PROTOCOLO v4.0, §4).

Escaneia arquivos de interface (.html, .tsx, .jsx, .vue, .svelte) caçando:
  1. A11Y-SR-ONLY-DUPLICATE: `.sr-only` / `.visually-hidden` vazio ou duplicando texto visível
     (ou sua tradução direta) no mesmo container para forçar testes `getByText` a passarem.
     Preserva o uso legítimo em botões de ícone (`<svg>` + `<span class="sr-only">Fechar</span>`).
  2. A11Y-HIDDEN-INTERACTIVE: elementos interativos (`button`, `a`, `input`, `select`, `textarea`)
     com `aria-hidden="true"` sem `tabindex="-1"`, ou ocultos com `display: none` para plantar texto de teste.
  3. A11Y-EMPTY-CONTROL: `<button>` sem texto visível, sem `.sr-only`, sem `aria-label`,
     sem `aria-labelledby` e sem `title` (ex: `<button><svg></svg></button>`).

Códigos de saída:
  0  zero violações encontradas (pelo menos 1 arquivo de UI foi escaneado)
  1  pelo menos 1 violação de acessibilidade/anti-cheat encontrada
  2  caminho inexistente ou erro de argumentos
  3  NÃO-APLICÁVEL: zero arquivos de UI escaneados — veredito honesto "nada a
     verificar", nunca verde por tautologia (o runner conta 3 como n/a)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

A11Y_EXTS = {".html", ".tsx", ".jsx", ".vue", ".svelte"}
SKIP_DIRS = {
    ".git", ".hg", ".henke", "__pycache__", ".pytest_cache", ".mypy_cache",
    "node_modules", "dist", "build", "out", "coverage", ".next", ".nuxt",
    ".svelte-kit", "target", "vendor", ".venv", "venv",
}

_SR_ONLY_RE = re.compile(
    r'<([a-zA-Z0-9]+)\b[^>]*(?:class|className)\s*=\s*(?:["\'][^"\']*\b(?:sr-only|visually-hidden)\b[^"\']*["\']|\{\s*["\'`][^"\'`]*\b(?:sr-only|visually-hidden)\b[^"\'`]*["\'`]\s*\})[^>]*>(.*?)</\1>',
    flags=re.S | re.I,
)

_TRANSLATIONS = {
    "save": "salvar", "salvar": "save",
    "close": "fechar", "fechar": "close",
    "delete": "excluir", "excluir": "delete",
    "cancel": "cancelar", "cancelar": "cancel",
    "submit": "enviar", "enviar": "submit",
}

_INTERACTIVE_OPEN_RE = re.compile(
    r"<(button|a|input|select|textarea)\b([^>]*?)(?:/>|>)",
    flags=re.S | re.I,
)

_ARIA_HIDDEN_TRUE_RE = re.compile(
    r'\baria-hidden\s*=\s*(?:["\']true["\']|\{\s*(?:true|["\']true["\'])\s*\})',
    flags=re.I,
)
_TABINDEX_MINUS_ONE_RE = re.compile(
    r'\btabindex\s*=\s*(?:["\']-1["\']|\{\s*-1\s*\}|\{\s*["\']-1["\']\s*\})',
    flags=re.I,
)
_DISPLAY_NONE_STYLE_RE = re.compile(
    r'\bstyle\s*=\s*(?:["\'][^"\']*display\s*:\s*none[^"\']*["\']|\{\s*\{[^}]*display\s*:\s*["\']none["\'][^}]*\}\s*\})',
    flags=re.S | re.I,
)

_BUTTON_BLOCK_RE = re.compile(
    r"<button\b([^>]*)>(.*?)</button>",
    flags=re.S | re.I,
)
_A11Y_LABEL_ATTR_RE = re.compile(
    r'\b(?:aria-label|aria-labelledby|title)\s*=\s*(?:["\']\s*[^"\'\s][^"\']*["\']|\{[^}]+\})',
    flags=re.S | re.I,
)
_SVG_BLOCK_RE = re.compile(r"<svg\b.*?</svg>", flags=re.S | re.I)


def _find_sr_only_violations(content: str) -> list[tuple[int, str]]:
    """Retorna lista de (lineno, detail) para violações A11Y-SR-ONLY-DUPLICATE."""
    violations: list[tuple[int, str]] = []
    matches = list(_SR_ONLY_RE.finditer(content))
    if not matches:
        return violations

    for sr_match in matches:
        lineno = content.count("\n", 0, sr_match.start()) + 1
        hidden_clean = re.sub(r"<[^>]+>", " ", sr_match.group(2)).strip().lower()
        hidden_words = set(re.findall(r"\w+", hidden_clean))
        if not hidden_words:
            violations.append((lineno, "empty .sr-only/.visually-hidden element without accessible text"))
            continue

        start = sr_match.start()
        before = content[:start]
        after = content[sr_match.end():]

        open_tags = list(re.finditer(r"<([a-zA-Z0-9]+)(?:\s+[^>]*)?>", before))
        for ot in reversed(open_tags):
            tag = ot.group(1).lower()
            if tag in ("svg", "path", "strong", "em", "i", "b", "span"):
                continue
            close_before = list(re.finditer(r"</" + re.escape(tag) + r">", before[ot.end():], flags=re.I))
            open_nested = list(re.finditer(r"<" + re.escape(tag) + r"(?:\s+[^>]*)?>", before[ot.end():], flags=re.I))
            if len(close_before) <= len(open_nested):
                close_after = re.search(r"</" + re.escape(tag) + r">", after, flags=re.I)
                if close_after:
                    container_content = before[ot.end():] + after[:close_after.start()]
                    visible_content = _SR_ONLY_RE.sub("", container_content)
                    visible_content = _SVG_BLOCK_RE.sub("", visible_content)
                    visible_words = set(re.findall(r"\w+", re.sub(r"<[^>]+>", " ", visible_content).lower()))
                    overlap = len(hidden_words & visible_words)
                    is_dup = False
                    if hidden_words and visible_words:
                        if hidden_words.issubset(visible_words) or visible_words.issubset(hidden_words):
                            is_dup = True
                        elif overlap >= 1 and (overlap / len(hidden_words) >= 0.5 or overlap / len(visible_words) >= 0.5):
                            is_dup = True

                    if is_dup:
                        violations.append((
                            lineno,
                            f"Cheat detectado: '.sr-only' duplica texto visível ('{hidden_clean}') dentro de <{tag}>.",
                        ))
                        break
                    translated_hit = False
                    for hw in hidden_words:
                        if hw in _TRANSLATIONS and _TRANSLATIONS[hw] in visible_words:
                            violations.append((
                                lineno,
                                f"Cheat detectado: '.sr-only' duplica significado de texto visível ('{hidden_clean}' ~ '{_TRANSLATIONS[hw]}') dentro de <{tag}>.",
                            ))
                            translated_hit = True
                            break
                    if translated_hit:
                        break
                    is_control = tag in ("button", "a", "summary", "label") or bool(
                        re.search(r'role\s*=\s*["\']button["\']', ot.group(0), flags=re.I)
                    )
                    if is_control:
                        break

    return violations


def check_a11y_sr_only_cheat(html_content: str) -> tuple[bool, str]:
    """Compatível com evals/canaries.py: retorna (is_valid, reason)."""
    violations = _find_sr_only_violations(html_content)
    if violations:
        return False, violations[0][1]
    return True, "Uso legítimo de acessibilidade ou nenhum hack detectado."


def scan_a11y_content(content: str, loc_prefix: str) -> list[tuple[str, str, str]]:
    """Escaneia o conteúdo de um arquivo e retorna [(rule, 'file:line', detail)]."""
    findings: list[tuple[str, str, str]] = []

    # 1. A11Y-SR-ONLY-DUPLICATE
    for lineno, detail in _find_sr_only_violations(content):
        findings.append(("A11Y-SR-ONLY-DUPLICATE", f"{loc_prefix}:{lineno}", detail))

    # 2. A11Y-HIDDEN-INTERACTIVE
    for m in _INTERACTIVE_OPEN_RE.finditer(content):
        tag = m.group(1).lower()
        attrs = m.group(2) or ""
        lineno = content.count("\n", 0, m.start()) + 1
        if _ARIA_HIDDEN_TRUE_RE.search(attrs) and not _TABINDEX_MINUS_ONE_RE.search(attrs):
            findings.append((
                "A11Y-HIDDEN-INTERACTIVE",
                f"{loc_prefix}:{lineno}",
                f"interactive <{tag}> has aria-hidden='true' without tabindex='-1'",
            ))
        elif _DISPLAY_NONE_STYLE_RE.search(attrs):
            findings.append((
                "A11Y-HIDDEN-INTERACTIVE",
                f"{loc_prefix}:{lineno}",
                f"interactive <{tag}> hidden via inline display:none",
            ))

    # 3. A11Y-EMPTY-CONTROL
    for m in _BUTTON_BLOCK_RE.finditer(content):
        attrs = m.group(1) or ""
        body = m.group(2) or ""
        lineno = content.count("\n", 0, m.start()) + 1
        if _A11Y_LABEL_ATTR_RE.search(attrs):
            continue
        # Remove ícones <svg>...</svg> e tags HTML para verificar se resta texto ou expressão dinâmica
        body_without_svg = _SVG_BLOCK_RE.sub(" ", body)
        text_only = re.sub(r"<[^>]+>", " ", body_without_svg).strip()
        if not re.search(r"\w+", text_only) and not re.search(r"\{[^}]+\}", text_only):
            findings.append((
                "A11Y-EMPTY-CONTROL",
                f"{loc_prefix}:{lineno}",
                "<button> control has no visible text, no .sr-only label, and no aria-label/title",
            ))

    return findings


def scan_a11y_file(path: Path, root: Path | None = None) -> list[tuple[str, str, str]] | None:
    """Escaneia um único arquivo suportado e retorna lista de findings.

    None = arquivo elegível porém ilegível — NUNCA conta como escaneado: um
    único arquivo de UI chmod-000 não pode vestir 'exit 0 sem leitura'
    (A12-banca)."""
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in A11Y_EXTS:
        return []
    try:
        # UTF-8 estrito: bytes inválidos → mojibake "escaneado" = falso-verde.
        raw = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if root is not None:
        try:
            loc = p.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            loc = p.as_posix()
    else:
        loc = p.as_posix()
    return scan_a11y_content(raw, loc)


def scan_a11y_path(target: Path) -> tuple[int, list[tuple[str, str, str]], list[str]]:
    """Escaneia arquivo ou diretório recursivamente.

    Retorna (files_scanned, findings, unreadable). unreadable não-vazio é
    erro de infraestrutura → exit 2, nunca verde por omissão."""
    t = Path(target)
    if not t.exists():
        return 0, [], []
    if t.is_file():
        if t.suffix.lower() not in A11Y_EXTS:
            return 0, [], []
        res = scan_a11y_file(t, root=t.parent)
        if res is None:
            return 0, [], [str(t)]
        return 1, res, []

    files_scanned = 0
    all_findings: list[tuple[str, str, str]] = []
    unreadable: list[str] = []
    root_resolved = t.resolve()
    for dirpath, dirnames, filenames in os.walk(t):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            fp = Path(dirpath) / fn
            if fp.suffix.lower() not in A11Y_EXTS:
                continue
            res = scan_a11y_file(fp, root=root_resolved)
            if res is None:
                unreadable.append(str(fp))
                continue
            files_scanned += 1
            all_findings.extend(res)
    return files_scanned, all_findings, unreadable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scanner de acessibilidade e anti-cheat .sr-only.")
    parser.add_argument("path", type=Path, help="Arquivo ou diretório a escanear")
    parser.add_argument("--json", action="store_true", help="Emite saída em formato JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if not args.path.exists():
        print(f"ERRO: caminho inexistente: {args.path}", file=sys.stderr)
        return 2

    files_scanned, findings, unreadable = scan_a11y_path(args.path)
    if unreadable:
        print(
            f"ERRO: {len(unreadable)} arquivo(s) de UI ilegível(is): "
            f"{', '.join(unreadable[:5])} — inconclusivo, nunca verde.",
            file=sys.stderr,
        )
        return 2
    if args.json:
        print(json.dumps({
            "files_scanned": files_scanned,
            "findings": [
                {"rule": r, "location": loc, "message": msg}
                for r, loc, msg in findings
            ],
        }, ensure_ascii=False, indent=2))
    else:
        for r, loc, msg in findings:
            print(f"{r:<26} {loc}  {msg}")
        print(f"{files_scanned} file(s) scanned, {len(findings)} finding(s)")

    if files_scanned == 0:
        print(
            "N/A: nenhum arquivo de UI elegível encontrado — portão não aplicável "
            "a este alvo (veredito n/a, não verde).",
            file=sys.stderr,
        )
        return 3
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
