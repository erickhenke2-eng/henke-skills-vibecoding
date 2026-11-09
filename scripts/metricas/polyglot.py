#!/usr/bin/env python3
"""polyglot.py — lexer/scanner O(N) compartilhado para TS/TSX/JS/MJS/RS/GO.

Não é parser completo: mascara comentários/strings preservando linhas e posições,
localiza funções por chaveamento de chaves e extrai imports por padrão léxico.
Aproximação declarada (spec v2 §7): generics TS aninhados, macros Rust e regex
ambíguos podem deslocar fronteiras — erra no lado conservador, nunca crasha.

Usado por: repo_signals.py (metrics/edges) e assertless_scan.py (testes não-Python).
"""
import bisect
import io
import json
import os
import re
import tokenize
from pathlib import Path
from typing import NamedTuple

class CommentSpan(NamedTuple):
    start_line: int  # 1-indexed
    start_col: int   # 0-indexed
    end_line: int    # 1-indexed
    end_col: int     # 0-indexed
    text: str        # comment slice

C_EXTS = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".mts", ".cts", ".jsx", ".rs", ".go"}
JS_EXTS = {".ts", ".tsx", ".js", ".mjs", ".cjs", ".mts", ".cts", ".jsx"}
TEST_RE = re.compile(
    r"(\.test\.|\.spec\.|_test\.go$|_test\.rs$|(?:^|/)test_[^/]*\.py$|_test\.py$|"
    r"(?:^|/)tests?/|__tests__)")
PRUNE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "target",
              "dist", "out", "release", "vendor", ".runtime", "coverage",
              ".next", ".nuxt", "build", ".turbo", ".cache", ".henke", ".worktrees"}

KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "new", "delete",
            "typeof", "case", "do", "else", "try", "throw", "finally", "in", "of",
            "function", "class", "extends", "import", "export", "const", "let",
            "var", "match", "loop", "impl", "fn", "pub", "mod", "use", "struct",
            "enum", "trait", "where", "mut", "ref", "move", "async", "await",
            "select", "go", "defer", "chan", "range", "type", "interface",
            "package", "func", "map", "fallthrough", "default", "break",
            "continue", "yield", "static", "readonly", "public", "private",
            "protected", "get", "set", "constructor", "this", "super", "void",
            "assert", "expect", "describe", "it", "test", "specify"}


def iter_source_files(root: Path, exts=None):
    """os.walk com poda in-place de diretórios mortos (O(N) em arquivos vivos)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if exts is None or Path(fn).suffix.lower() in exts:
                yield Path(dirpath) / fn


def mask_c_like(text: str, lang: str = "") -> str:
    """Mascara comentários/strings/template-literals preservando \n e colunas.
    Saída tem o mesmo comprimento; código vive, conteúdo textual vira espaço."""
    lang = lang.lstrip(".").lower()
    out = list(text)
    i, n = 0, len(text)
    prev = ""  # último char significativo (heurística p/ regex literal JS)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                # `/*` sem fechamento (p.ex. dentro de regex `/ab\/*/` em
                # início de linha, lido como divisão) NÃO mascara até o EOF —
                # engolia todo o arquivo seguinte (falso-verde, banca v10 A3).
                j = text.find("\n", i)
                j = n if j == -1 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
            else:
                for k in range(i, j + 2):
                    if out[k] != "\n":
                        out[k] = " "
                i = j + 2
        elif text.startswith("<!--", i):
            if lang in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
                # Annex B: `<!--` em JS é comentário de LINHA — tratá-lo como
                # bloco até `-->` mascara o resto do arquivo (falso-verde A1).
                j = text.find("\n", i)
                j = n if j == -1 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
            elif lang not in ("rs", "go"):
                j = text.find("-->", i + 4)
                if j == -1:
                    # F2 (banca v11): '<!--' não fechado mascarava até EOF —
                    # igual ao caso '/*': só a linha corrente some.
                    j = text.find("\n", i)
                    j = n if j == -1 else j
                    for k in range(i, j):
                        out[k] = " "
                    i = j
                else:
                    for k in range(i, j + 3):
                        if out[k] != "\n":
                            out[k] = " "
                    i = j + 3
            else:
                if not c.isspace():
                    prev = c
                i += 1
        elif c in "'\"":
            # 'a/'static é lifetime — existe só em Rust. Fora de rs, ' é string.
            if c == "'" and lang == "rs" and _looks_like_lifetime(text, i):
                prev = c
                i += 1
                continue
            j = _string_end(text, i, c)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif c == "`":
            i = _mask_template(text, out, i)
        elif c == "/" and lang in ("js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts") and _regex_ctx_ok(text, i):
            j = _regex_end(text, i)
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            if not c.isspace():
                prev = c
            i += 1
    return "".join(out)


def _looks_like_lifetime(text, i):
    """Rust apenas: 'a, 'static, '_ = lifetime (sem ' de fechamento).
    Char literal ('x', 'é', escape de 1 char) tem ' de fechamento OBRIGATÓRIO
    — nunca newline. Retorna True só se o próximo delimitador não for '
    próximo (lifetime)."""
    j = i + 1
    if j < len(text) and text[j] == "\\":
        j += 2
    while j < len(text) and text[j] not in "'\n":
        j += 1
    # fechou com ' a até 4 chars ('a', 'é', '\n', '\t') → char literal → mascara
    if j < len(text) and text[j] == "'" and j - i <= 4:
        return False
    return True


def _string_end(text, i, q):
    n = len(text)
    j = i + 1
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == q or text[j] == "\n":
            return j + 1
        j += 1
    return n


def _mask_template(text, out, i):
    """Mascara `template` mantendo ${...} como código (interpolação conta).
    Dentro de ${...} strings, comentários e templates aninhados são tratados
    como código — o backtick interno nunca fecha o template externo."""
    n = len(text)
    out[i] = " "
    j = i + 1
    depth = 0
    while j < n:
        c = text[j]
        nxt = text[j + 1] if j + 1 < n else ""
        if c == "\\" and j + 1 < n:
            out[j] = out[j + 1] = " "
            j += 2
            continue
        if depth == 0 and c == "`":
            out[j] = " "
            return j + 1
        if depth > 0:
            # dentro de ${...}: strings/comments/templates aninhados são código
            if c == "/" and nxt == "/":
                k = text.find("\n", j)
                k = n if k == -1 else k
                for x in range(j, k):
                    out[x] = " "
                j = k
                continue
            if c == "/" and nxt == "*":
                k = text.find("*/", j + 2)
                k = n - 2 if k == -1 else k
                for x in range(j, min(k + 2, n)):
                    if out[x] != "\n":
                        out[x] = " "
                j = min(k + 2, n)
                continue
            if c in "'\"":
                k = _string_end(text, j, c)
                for x in range(j, k):
                    if out[x] != "\n":
                        out[x] = " "
                j = k
                continue
            if c == "`":
                j = _mask_template(text, out, j)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    out[j] = " "
                    j += 1
                    continue
        elif c == "$" and nxt == "{":
            depth = 1
            out[j] = out[j + 1] = " "
            j += 2
            continue
        if depth == 0 and out[j] != "\n":
            out[j] = " "
        j += 1
    return n


def mask_comments_only(text: str, lang: str = "") -> str:
    """Apaga apenas comentários, preservando strings (imports/specifiers vivem nelas)."""
    lang = lang.lstrip(".").lower()
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            if j == -1:
                # `/*` sem fechamento (p.ex. dentro de regex `/ab\/*/` em
                # início de linha, lido como divisão) NÃO mascara até o EOF —
                # engolia todo o arquivo seguinte (falso-verde, banca v10 A3).
                j = text.find("\n", i)
                j = n if j == -1 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
            else:
                for k in range(i, j + 2):
                    if out[k] != "\n":
                        out[k] = " "
                i = j + 2
        elif text.startswith("<!--", i):
            if lang in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
                j = text.find("\n", i)
                j = n if j == -1 else j
                for k in range(i, j):
                    out[k] = " "
                i = j
            elif lang not in ("rs", "go"):
                j = text.find("-->", i + 4)
                if j == -1:
                    # F2 (banca v11): '<!--' não fechado mascarava até EOF.
                    j = text.find("\n", i)
                    j = n if j == -1 else j
                    for k in range(i, j):
                        out[k] = " "
                    i = j
                else:
                    for k in range(i, j + 3):
                        if out[k] != "\n":
                            out[k] = " "
                    i = j + 3
            else:
                i += 1
        elif c == "/" and lang in ("js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts") and _regex_ctx_ok(text, i):
            # Regex literal: pula sem mascarar — `/*` dentro não pode abrir
            # comentário (A2: antes, máscara comia até EOF).
            i = _regex_end(text, i)
        elif c in "'\"`":
            if c == "'" and lang == "rs" and _looks_like_lifetime(text, i):
                i += 1
                continue
            i = _template_end(text, i) if c == "`" else _string_end(text, i, c)
        else:
            i += 1
    return "".join(out)


def find_comment_spans(text: str, lang: str = "") -> list[CommentSpan]:
    """Extrai spans de comentários reais para C-like, Python, HTML/Vue/Svelte e hash-configs.
    Retorna lista de CommentSpan(start_line, start_col, end_line, end_col, text)."""
    lang = lang.lstrip(".").lower()
    spans: list[CommentSpan] = []

    if lang in ("py", "python"):
        try:
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            for tok_type, tok_str, start, end, _ in tokens:
                if tok_type == tokenize.COMMENT:
                    spans.append(CommentSpan(start[0], start[1], end[0], end[1], tok_str))
            return spans
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass

    line_starts = [0]
    for idx, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(idx + 1)

    def _offset_to_pos(pos: int) -> tuple[int, int]:
        l_idx = bisect.bisect_right(line_starts, pos) - 1
        return l_idx + 1, pos - line_starts[l_idx]

    def _regex_end(text: str, start: int) -> int:
        """Posição após o '/' de fechamento de um regex literal; -1 se não fecha
        antes do fim da linha (regex JS não cruza \n — então não era regex)."""
        i = start + 1
        in_class = False
        while i < len(text):
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "[":
                in_class = True
            elif ch == "]":
                in_class = False
            elif ch == "/" and not in_class:
                return i + 1
            elif ch == "\n":
                return -1
            i += 1
        return -1

    i, n = 0, len(text)
    is_hash_lang = lang in ("py", "python", "tf", "tfvars", "toml", "yaml", "yml", "dockerfile", "sh", "bash")
    can_html_comment = lang not in ("rs", "go")
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            sl, sc = _offset_to_pos(i)
            el, ec = _offset_to_pos(j)
            spans.append(CommentSpan(sl, sc, el, ec, text[i:j]))
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            end_pos = n if j == -1 else j + 2
            sl, sc = _offset_to_pos(i)
            el, ec = _offset_to_pos(end_pos)
            spans.append(CommentSpan(sl, sc, el, ec, text[i:end_pos]))
            i = end_pos
        elif can_html_comment and text.startswith("<!--", i):
            if lang in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
                # Annex B: comentário de LINHA em JS — o span não pode engolir
                # o resto do arquivo (A1).
                j = text.find("\n", i)
                end_pos = n if j == -1 else j
            else:
                j = text.find("-->", i + 4)
                end_pos = n if j == -1 else j + 3
            sl, sc = _offset_to_pos(i)
            el, ec = _offset_to_pos(end_pos)
            spans.append(CommentSpan(sl, sc, el, ec, text[i:end_pos]))
            i = end_pos
        elif is_hash_lang and c == "#":
            j = text.find("\n", i)
            j = n if j == -1 else j
            sl, sc = _offset_to_pos(i)
            el, ec = _offset_to_pos(j)
            spans.append(CommentSpan(sl, sc, el, ec, text[i:j]))
            i = j
        elif c in "'\"`":
            if c == "'" and lang == "rs" and _looks_like_lifetime(text, i):
                i += 1
                continue
            i = _template_end(text, i) if c == "`" else _string_end(text, i, c)
        elif c == "/" and lang in ("js", "jsx", "ts", "tsx", "mjs", "cjs", "mts", "cts"):
            # Regex literal JS: `/a\/*/` contém `/*` que, lido como comentário,
            # engole o resto da linha — inclusive um marker de supressão forjado
            # logo depois (A15/A11/F1-banca). Char anterior operador → regex;
            # word-char → retrocede o identificador e exige keyword (`return`,
            # `typeof`, ...) — `foo /re/` é divisão, `return /re/` é regex.
            if _regex_ctx_ok(text, i):
                end = _regex_end(text, i)
                i = end if end != -1 else i + 1
            else:
                i += 1
        else:
            i += 1
    return spans


def mask_py(text: str) -> str:
    """Mascara comentários e strings em Python preservando \n e colunas."""
    out = list(text)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        line_starts = [0]
        for idx, ch in enumerate(text):
            if ch == "\n":
                line_starts.append(idx + 1)
        for tok_type, tok_str, start, end, _ in tokens:
            if tok_type in (tokenize.COMMENT, tokenize.STRING):
                s_off = line_starts[start[0] - 1] + start[1]
                e_off = line_starts[end[0] - 1] + end[1]
                for k in range(s_off, min(e_off, len(out))):
                    if out[k] != "\n":
                        out[k] = " "
        return "".join(out)
    except Exception:
        def _repl(m):
            return "".join("\n" if c == "\n" else " " for c in m.group(0))
        t = re.sub(r"#.*$", lambda m: " " * len(m.group(0)), text, flags=re.M)
        t = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"[^"\n]*"|\'[^\'\n]*\'', _repl, t)
        return t


def _template_end(text, i):
    n = len(text)
    j, depth = i + 1, 0
    while j < n:
        c = text[j]
        if c == "\\" and j + 1 < n:
            j += 2
            continue
        if depth == 0 and c == "`":
            return j + 1
        if depth > 0:
            if c in "'\"":
                j = _string_end(text, j, c)
                continue
            if c == "`":
                j = _template_end(text, j)
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
        elif c == "$" and j + 1 < n and text[j + 1] == "{":
            depth = 1
            j += 2
            continue
        j += 1
    return n


# Keywords JS/TS após as quais '/' abre regex (operando), não divisão.
# `=>` e operadores já são cobertos pelo conjunto de chars.
_REGEX_KEYWORDS = frozenset({
    "return", "typeof", "case", "in", "of", "throw", "void", "new",
    "delete", "do", "else", "instanceof", "yield", "await", "default",
})

# `)` que fecha parêntese de CONTROLE DE FLUXO abre posição de statement —
# `if (x) /re/` é regex. `)` de chamada comum (`f() /re/`) é divisão.
_REGEX_PAREN_KW = frozenset({"if", "for", "while", "switch", "catch", "with"})


def _regex_ctx_ok(text: str, i: int) -> bool:
    """True se '/' em `i` abre regex literal (posição de operando)."""
    j = i - 1
    while j >= 0 and text[j] in " \t":
        j -= 1
    if j < 0:
        return True  # início de arquivo = posição de expressão
    c = text[j]
    # `\n` NÃO é contexto de regex: `a\n/eval(x)/g` é divisão legítima em
    # JS/Rust — tratar newline como abertura de regex mascara sinks inteiros.
    if c == "<" and i >= 1 and text[i - 1] == "<":
        # F3 (banca v11): `</` em posição de tag de fechamento JSX
        # (`<div></a>{eval(x)}</div>`) não é início de regex — o "regex"
        # engolia o sink até a próxima '/'. Char antes do '<' em posição de
        # elemento (`>`,`{`,`(`,`=`, operadores) fecha a heurística.
        m = i - 2
        while m >= 0 and text[m] in " \t":
            m -= 1
        if m >= 0 and text[m] in ">{([=,:!&|?;":
            return False
    if c in "(,=:[!&|?{};+-*/%^~<>":
        return True
    if c == ")":
        # `if (x) /re/` — `)` de parêntese de keyword de controle abre statement:
        # regex. `f(a) /re/` — `)` de chamada: divisão. Retrocede ao `(` de
        # abertura e exige a keyword de controle.
        depth = 0
        k = j
        while k >= 0:
            if text[k] == ")":
                depth += 1
            elif text[k] == "(":
                depth -= 1
                if depth == 0:
                    break
            k -= 1
        if k <= 0:
            return False
        m = k - 1
        while m >= 0 and text[m] in " \t":
            m -= 1
        e = m
        while m >= 0 and (text[m].isalnum() or text[m] in "_$"):
            m -= 1
        return text[m + 1:e + 1] in _REGEX_PAREN_KW
    if c.isalnum() or c in "_$":
        k = j
        while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
            k -= 1
        return text[k + 1:j + 1] in _REGEX_KEYWORDS
    return False


def _regex_start(prev):
    return prev in ("", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";")


def _regex_end(text, i):
    n = len(text)
    j = i + 1
    in_class = False
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            return j
        elif c == "\n":
            return i + 1  # era divisão, não regex — devolve sem mascarar
        j += 1
    return i + 1


_FUNC_RE = re.compile(
    r"(?:^|[^\w$.])(?:export\s+)?(?:default\s+)?(?:async\s+)?function\*?\s*([A-Za-z_$][\w$]*)\s*[<(]"
    r"|(?:^|[^\w$.])(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+([A-Za-z_][\w]*)\s*[<(]"
    r"|(?:^|[^\w$.])func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\("
    r"|(?:^|[,{\n]\s*)(?:get\s+|set\s+|static\s+|async\s+|public\s+|private\s+|protected\s+|readonly\s+|override\s+)*"
    r"([A-Za-z_$][\w$]*)\s*\([^()]*\)\s*(?::\s*[\w<>[\]|&,. ]+)?\s*\{"
    r"|([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^()]*\)\s*(?::\s*[\w<>[\]|&,. ]+)?\s*=>\s*([{(])?"
    r"|([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*=>",
    re.M)

_TESTCALL_RE = re.compile(
    r"(?<![.\w$])(?:it|test|specify|xit|fit)"
    r"(?:\.(?:each|skip|only|todo|concurrent|fails|runIf|skipIf)\s*"
    r"(?:\((?:[^()]|\([^()]*\))*\))?)*"
    r"\s*\(", re.M)
_RUST_TEST_RE = re.compile(r"#\s*\[(?:tokio::|async_std::)?test(?:\s*\([^)]*\))?\]\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)")
_GO_TEST_RE = re.compile(r"func\s+(Test[A-Z0-9_]\w*)\s*\(\s*\w+\s*\*?testing\.[A-Z]\w*")


def _brace_end(masked: str, open_idx: int) -> int:
    """Índice do '}' que fecha o '{' em open_idx; -1 se nunca fecha."""
    depth = 0
    for i in range(open_idx, len(masked)):
        if masked[i] == "{":
            depth += 1
        elif masked[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _paren_end(masked: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(masked)):
        if masked[i] == "(":
            depth += 1
        elif masked[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _angle_end(masked: str, open_idx: int) -> int:
    """Índice do '>' que fecha o '<' em open_idx (genéricos TS/Rust)."""
    depth = 0
    for i in range(open_idx, len(masked)):
        c = masked[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return i
        elif c in "{;\n":
            return -1
    return -1


def find_functions(text: str, lang: str):
    """yield (nome, start_line, end_line, corpo_mascarado). O(N), fronteiras por chaves."""
    masked = mask_c_like(text, lang)
    seen = set()
    for m in _FUNC_RE.finditer(masked):
        name = next((g for g in m.groups() if g), None)
        if not name or name in KEYWORDS:
            continue
        seg = masked[m.start():m.end()]
        tail = masked[m.end() - 1]
        if tail == "(" and "=>" not in seg:
            # assinatura: pula params com casamento de parêntese
            pend = _paren_end(masked, m.end() - 1)
            if pend == -1:
                continue
            after = pend + 1
        elif tail == "<":        # generics <T> ts/rust: casa '>' depois '(...)'
            gt = _angle_end(masked, m.end() - 1)
            if gt == -1:
                continue
            pp = masked.find("(", gt)
            if pp == -1:
                continue
            pend = _paren_end(masked, pp)
            if pend == -1:
                continue
            after = pend + 1
        elif tail == "{":        # arrow/method: o próprio match já abriu o corpo
            b = m.end() - 1
            e = _brace_end(masked, b)
            if e == -1:
                continue
            sl = masked.count("\n", 0, m.start() + len(seg) - len(seg.lstrip())) + 1
            el = masked.count("\n", 0, e) + 1
            key = (sl, name)
            if key not in seen:
                seen.add(key)
                yield name, sl, el, masked[b:e + 1]
            continue
        elif tail in (">", "(") or seg.rstrip().endswith("=>"):
            sb = m.end() - 1 if tail == "(" else m.end()
            while sb < len(masked) and masked[sb] in " \t\r\n":
                sb += 1
            if sb >= len(masked):
                continue
            if masked[sb] == "{":
                e = _brace_end(masked, sb)
            elif masked[sb] == "(":
                e = _paren_end(masked, sb)
            else:
                depth = 0
                e = len(masked) - 1
                for i in range(sb, len(masked)):
                    c = masked[i]
                    if c in "([{":
                        depth += 1
                    elif c in ")]}":
                        if depth == 0:
                            e = i - 1
                            break
                        depth -= 1
                    elif depth == 0 and c in ";\n":
                        e = i
                        break
            if e == -1 or e < sb:
                continue
            sl = masked.count("\n", 0, m.start() + len(seg) - len(seg.lstrip())) + 1
            el = masked.count("\n", 0, e) + 1
            key = (sl, name)
            if key not in seen:
                seen.add(key)
                yield name, sl, el, masked[sb:e + 1]
            continue
        else:
            continue
        b = masked.find("{", after)
        if b == -1:
            continue
        between = masked[after:b]
        # tipo de retorno pode conter '(' (Go "(T, error)", Rust tupla) —
        # só ';' denuncia que a assinatura não era de função.
        if ";" in between:
            continue
        e = _brace_end(masked, b)
        if e == -1:
            continue
        sl = masked.count("\n", 0, m.start() + len(seg) - len(seg.lstrip())) + 1
        el = masked.count("\n", 0, e) + 1
        key = (sl, name)
        if key in seen:
            continue
        seen.add(key)
        yield name, sl, el, masked[b:e + 1]


_DECIDE_WORD = re.compile(r"\b(if|for|while|case|catch|match|select)\b")
_DECIDE_OP = re.compile(r"&&|\|\||\?\?|\?\.")
_RUST_ARM = re.compile(r"=>")


def complexity_masked(body: str, lang: str) -> int:
    """1 + decisões léxicas. Ternário '?' conta só se não for '?:'/param opcional."""
    score = 1
    score += len(_DECIDE_WORD.findall(body))
    score += len(_DECIDE_OP.findall(body))
    if lang.lstrip(".").lower() == "rs":
        score += len(_RUST_ARM.findall(body))
    else:
        score += len(re.findall(r"\?(?![.:,)\]\w?]*:\s*[\w<>[\]|&])(?!\?)(?!\.)(?=[^:]*:)", body))
    return score


def find_test_blocks(text: str, lang: str):
    """yield (nome, start_line, end_line, corpo_mascarado) de testes não-Python."""
    masked = mask_c_like(text, lang)
    bare = lang.lstrip(".").lower()
    if lang in JS_EXTS or bare in ("ts", "tsx", "js", "mjs", "jsx"):
        for m in _TESTCALL_RE.finditer(masked):
            head = masked[m.start():m.end()]
            if re.search(r"\.todo\s*\($", head):
                continue  # it.todo(...) não tem callback — não é teste executável
            pos = m.end() - 1  # '(' de abertura da chamada
            pend = _paren_end(masked, pos)
            if pend == -1:
                continue
            inner = masked[pos:pend]
            # callback = primeiro '=>'/'function' em profundidade 1 dos args —
            # '=>' aninhados DENTRO do corpo (.map(x => y)) não contam.
            arrow = None
            depth = 0
            for am in re.finditer(r"=>|function\b|[()\[\]{}]", inner):
                tok = am.group(0)
                if tok in "([":
                    depth += 1
                elif tok in ")]}":
                    depth -= 1
                elif depth == 1:
                    arrow = am
                    break
            if arrow is None:
                continue
            after = pos + arrow.end()
            b = masked.find("{", after)
            if b != -1 and b <= pend:
                e = _brace_end(masked, b)
                if e == -1:
                    continue
                body = masked[b:e + 1]
            else:
                # arrow concisa: `() => expr` — corpo = expr até o ')' da chamada
                e = pend
                body = masked[after:pend]
            nm = re.search(r"[\"'`]([^\"'`]+)[\"'`]", text[pos:pend])
            name = nm.group(1)[:60] if nm else "(anon)"
            sl = masked.count("\n", 0, m.start()) + 1
            el = masked.count("\n", 0, e) + 1
            yield name, sl, el, body
    elif bare == "rs":
        for m in _RUST_TEST_RE.finditer(masked):
            b = masked.find("{", m.end())
            if b == -1:
                continue
            e = _brace_end(masked, b)
            if e == -1:
                continue
            yield m.group(1), masked.count("\n", 0, m.start()) + 1, \
                masked.count("\n", 0, e) + 1, masked[b:e + 1]
    elif bare == "go":
        for m in _GO_TEST_RE.finditer(masked):
            b = masked.find("{", m.end())
            if b == -1:
                continue
            e = _brace_end(masked, b)
            if e == -1:
                continue
            yield m.group(1), masked.count("\n", 0, m.start()) + 1, \
                masked.count("\n", 0, e) + 1, masked[b:e + 1]


_JS_IMPORT = re.compile(
    r"""(?:import|export)\s+[^'"]*?from\s*['"]([^'"]+)['"]"""
    r"""|import\s*\(\s*['"]([^'"]+)['"]"""
    r"""|require\s*\(\s*['"]([^'"]+)['"]"""
    r"""|import\s*['"]([^'"]+)['"]""", re.M)
_RS_MOD = re.compile(r"(?:^|\n)\s*(?:pub\s+)?mod\s+([A-Za-z_][\w]*)\s*;", re.M)
_RS_USE = re.compile(
    r"(?:^|\n)\s*(?:pub(?:\([^)]*\))?\s+)?use\s+((?:crate|self|super)::[\w:]+)", re.M)
_GO_IMPORT_BLK = re.compile(r"import\s*\(([^)]*)\)", re.S)
_GO_IMPORT_ONE = re.compile(r"import\s+(?:\w+\s+)?\"([^\"]+)\"")
_GO_QUOTED = re.compile(r'"([^"]+)"')


def find_imports(text: str, lang: str):
    """Lista de specifiers de import. Mascara só comentários: o specifier
    mora dentro da string e precisa chegar inteiro ao resolvedor."""
    masked = mask_comments_only(text, lang)
    out = []
    if lang in JS_EXTS:
        for m in _JS_IMPORT.finditer(masked):
            spec = next((g for g in m.groups() if g), None)
            if spec:
                out.append(spec)
    elif lang == ".rs":
        out += [f"mod::{g}" for g in _RS_MOD.findall(masked)]
        out += _RS_USE.findall(masked)
    elif lang == ".go":
        for blk in _GO_IMPORT_BLK.findall(masked):
            out += _GO_QUOTED.findall(blk)
        out += _GO_IMPORT_ONE.findall(masked)
    return out


def resolve_js(spec: str, base: Path, root: Path, pkgmap: dict):
    """Resolve import JS/TS para arquivo interno; None se externo."""
    if spec.startswith("."):
        cand_base = (base / spec).resolve()
        tries = []
        if cand_base.suffix in (".js", ".mjs", ".jsx", ".ts", ".tsx"):
            stem = cand_base.with_suffix("")
            tries += [stem.with_suffix(e) for e in
                      (".ts", ".tsx", ".js", ".mjs", ".jsx")]
        # concatena antes de with_suffix: 'tela.service' → 'tela.service.ts',
        # não 'tela.ts' (with_suffix engoliria '.service')
        tries += [Path(str(cand_base) + e) for e in
                  (".ts", ".tsx", ".js", ".mjs", ".jsx")]
        tries += [cand_base.with_suffix(e) for e in
                  (".ts", ".tsx", ".js", ".mjs", ".jsx")]
        tries += [(cand_base / "index").with_suffix(e) for e in
                  (".ts", ".tsx", ".js", ".mjs", ".jsx")]
        for c in tries:
            if c.is_file():
                return c
        return None
    first = spec.split("/")[0]
    scope = "/".join(spec.split("/")[:2]) if spec.startswith("@") else first
    if scope in pkgmap:
        pkgdir = pkgmap[scope]
        rest = spec[len(scope):].lstrip("/")
        for c in ([pkgdir / "src" / "index.ts", pkgdir / "src" / "index.tsx",
                   pkgdir / "index.ts", pkgdir / rest] if not rest else
                  [pkgdir / "src" / (rest + ".ts"), pkgdir / (rest + ".ts"),
                   pkgdir / rest]):
            if c.is_file():
                return c.resolve()
    return None


def build_pkgmap(root: Path, max_depth=4):
    """name -> dir de packages de workspace (package.json com 'name')."""
    pkgmap = {}
    for pj in iter_source_files(root, {".json"}):
        if pj.name != "package.json":
            continue
        try:
            if len(pj.relative_to(root).parts) > max_depth:
                continue
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            pkgmap[name] = pj.parent
    return pkgmap


def go_module_path(root: Path):
    """Lê 'module x' do go.mod mais próximo da raiz; None se ausente."""
    gm = root / "go.mod"
    try:
        for line in gm.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("module "):
                return line.split(None, 1)[1].strip()
    except OSError:
        pass
    return None


def cargo_root(path: Path, stop: Path):
    """Sobe de path até achar Cargo.toml (crate root); fallback stop."""
    d = path.parent
    while (d != d.parent and stop in d.parents) or d == stop:
        if (d / "Cargo.toml").is_file():
            return d
        if d == stop:
            break
        d = d.parent
    return stop
