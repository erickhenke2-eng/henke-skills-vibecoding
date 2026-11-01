#!/usr/bin/env python3
"""
hazard_scan.py — Portão mecânico poliglota de segurança e falhas de runtime (PROTOCOLO v3.3).

Sintetiza mecanismos de `cloudflare/skills` (workers-best-practices),
`cloudflare/security-audit-skill`, `trailofbits/skills` (sharp-edges) e `henke-blindagem`.

Arquitetura de 2 passes por arquivo (alinhada com swe-2-max):
  - Mapa de supressão extraído do texto RAW: `hazard-ok: <RULE> — <motivo>` isenta;
    `hazard-ok` sem motivo dispara `UNJUSTIFIED-SUPPRESSION` (exit 1).
  - Pass 1 (código com strings/comentários mascarados + escopo por função):
      * SHELL-INJECTION (`shell=True`, `os.system`, `exec`/`execSync` dinâmico, `Command::new("sh").arg("-c")`)
      * WEAK-CRYPTO (`Math.random()` / `random.random()` na mesma função que lida com token/secret/session/auth)
      * PATH-TRAVERSAL (`readFile`/`open`/`sendFile` dinâmico com input externo sem `resolve`/`normalize` na função)
      * UNBOUNDED-CONCURRENCY (`Promise.all`/`asyncio.gather` disparando `exec`/`spawn`/`ffmpeg` sem semáforo)
      * UNSAFE-DESERIALIZE (`eval(`, `new Function(`, `pickle.loads(`, `yaml.load(` sem `SafeLoader`)
      * XSS-RAW-HTML (`dangerouslySetInnerHTML`, `.innerHTML =`, `v-html=`, `{@html` sem sanitizador)
      * ELECTRON-INSECURE-CONFIG (`nodeIntegration: true`, `contextIsolation: false`)
  - Pass 2 (texto RAW com comentários removidos via `tokenize` em `.py` e `mask_comments_only` em C-like):
      * HARDCODED-SECRET (chaves reais `sk-`, `ghp_`, `AKIA` != EXAMPLE, `AIza`, `PRIVATE KEY`, ou `VITE_`/`NEXT_PUBLIC_` expondo segredo)
      * URL-CRED (`https://user:pass@host`)
      * SQL-CONCAT (f-string, `${}`, `%` ou `+` concatenando `SELECT/INSERT/UPDATE/DELETE`)
  - Pass Repo (`git ls-files`):
      * ENV-COMMITTED (`.env`, `*.pem`, `*.key`, `id_rsa` rastreados no Git)

Códigos de saída:
  0  zero hazards encontrados
  1  pelo menos 1 hazard encontrado
  2  entrada inválida, arquivo > 2 MB sem escopo válido ou stdout fechado
"""
from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tokenize

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import polyglot as _pg  # noqa: E402

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB cap (evita bundles/minified)
SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".mjs", ".jsx", ".rs", ".go", ".vue", ".svelte"}
SKIP_DIRS = {
    ".git", ".hg", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "target", "dist", "build", "out", "release", "vendor",
    ".next", ".nuxt", ".svelte-kit", ".turbo", "coverage", ".venv", "venv",
}

# Regras de Pass 2 (strings preservadas, comentários removidos)
_SECRET_PATTERNS = (
    ("OpenAI/API Key", re.compile(r"\bsk-(?:proj-|live-)?[A-Za-z0-9]{20,}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
    ("Private Key PEM", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("Public Env Secret", re.compile(r"\b(?:VITE|NEXT_PUBLIC|PUBLIC)_[A-Z0-9_]*(?:SECRET|PRIVATE|API_KEY|TOKEN)\s*=\s*['\"][^'\"]{8,}['\"]")),
)
_DOCUMENTED_EXAMPLE_KEYS = {
    "AKIAIOSFODNN7EXAMPLE",
    "AKIAY34FZKBOKMUTVV7A",
}

_URL_CRED_RE = re.compile(r"https?://([A-Za-z0-9._%-]+):([^/\s'\"@]{3,})@([A-Za-z0-9.-]+)")
_SQL_INTERP_PY_RE = re.compile(
    r"""(?ix)\b(?:f['"]{1,3}|['"]{1,3}[^'"]*%s)[^'"]*\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^'"]*(?:\{[^}]+\}|['"]\s*%\s*\w)"""
)
_SQL_INTERP_JS_RE = re.compile(
    r"""(?ix)`[^`]*\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^`]*\$\{[^}]+\}[^`]*`"""
)
_SQL_CONCAT_RE = re.compile(
    r"""(?ix)['"]\s*(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^'"]*['"]\s*\+\s*[A-Za-z_$]"""
)
_SQL_SPRINTF_RE = re.compile(
    r"""(?ix)\bfmt\.Sprintf\s*\(\s*['"`][^'"`]*\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^'"`]*%[svq]"""
)

# Regras de Pass 1 (código mascarado)
_PY_SHELL_RE = re.compile(r"\bsubprocess\.(?:run|Popen|call|check_output|check_call)\s*\([^)]*\bshell\s*=\s*True\b|\bos\.system\s*\(")
_JS_EXEC_RE = re.compile(r"(?<![.\w$])(?:child_process\s*\.\s*)?exec(?:Sync)?\s*\(\s*(?!['\"][^\n$'\"`]*['\"]\s*[,)])")
_RS_SHELL_RE = re.compile(r'Command::new\s*\(\s*\)\s*|\bCommand::new\s*\([^)]*\)\s*\.\s*arg\s*\(\s*"(?:-c|/c)"')
_WEAK_RAND_RE = re.compile(r"\b(?:Math\.random|random\.random|random\.randint|rand::random)\s*\(")
_AUTH_TOKEN_WORDS_RE = re.compile(r"(?i)\b(?:token|secret|password|senha|session_?id|nonce|api_?key|jwt|csrf|auth_?code)\b")
_FILE_SINK_RE = re.compile(r"\b(?:readFileSync|readFile|writeFileSync|writeFile|sendFile|createReadStream)\s*\(\s*(?![`'\"])([A-Za-z_$][\w$.]*)")
_EXT_SOURCE_RE = re.compile(r"\b(?:req|request|params|query|userPath|inputPath|filePathFromUser)\b")
_PATH_SAFE_RE = re.compile(r"\b(?:resolve|normalize|basename|relpath|is_relative_to|safe_join|safe_member)\b")
_UNBOUNDED_CONC_RE = re.compile(r"\b(?:Promise\.all|asyncio\.gather)\s*\(")
_HEAVY_EXEC_RE = re.compile(r"\b(?:spawn|exec|execFile|subprocess|ffmpeg|ffprobe|runGpu)\b")
_SEMAPHORE_RE = re.compile(r"\b(?:pLimit|p-limit|Semaphore|BoundedSemaphore|Bottleneck|runGpuBounded|maxConcurrency)\b")
_UNSAFE_DESER_RE = re.compile(r"(?<![.\w$])eval\s*\(|\bnew\s+Function\s*\(|\bpickle\.loads?\s*\(|\byaml\.load\s*\(")
_XSS_HTML_RE = re.compile(r"\bdangerouslySetInnerHTML\b|\.innerHTML\s*=(?!=)|\bv-html\s*=|\{@html\b")
_SANITIZER_RE = re.compile(r"(?i)\b(?:DOMPurify|sanitize|escapeHtml)\b")
_ELECTRON_INSEC_RE = re.compile(r"\bnodeIntegration\s*:\s*true\b|\bcontextIsolation\s*:\s*false\b")


def _strip_py_comments(raw: str) -> str:
    """Remove comentários `# ...` em Python usando `tokenize` stdlib, preservando linhas e strings."""
    lines = raw.splitlines(keepends=True)
    if not lines:
        return raw
    comment_spans: list[tuple[int, int, int]] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(raw).readline)
        for tok_type, tok_str, start, end, _ in tokens:
            if tok_type == tokenize.COMMENT:
                comment_spans.append((start[0] - 1, start[1], end[1]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return re.sub(r"(?m)^(\s*)#.*$", r"\1", raw)

    for line_idx, c_start, c_end in comment_spans:
        if 0 <= line_idx < len(lines):
            ln = lines[line_idx]
            lines[line_idx] = ln[:c_start] + (" " * max(0, c_end - c_start)) + ln[c_end:]
    return "".join(lines)


def _extract_suppressions(raw: str, path_str: str) -> tuple[dict[int, set[str]], list[tuple[str, str, str, str]]]:
    """Retorna ({lineno: {RULE | 'ALL'}}, findings_de_supressao_invalida)."""
    suppressed: dict[int, set[str]] = {}
    bad_findings: list[tuple[str, str, str, str]] = []
    for idx, line in enumerate(raw.splitlines(), 1):
        if "hazard-ok" not in line:
            continue
        m = re.search(r"hazard-ok(.*)$", line)
        if not m:
            continue
        tail = m.group(1).strip()
        # Formato 1: hazard-ok: RULE — motivo  (ou hazard-ok: RULE: motivo / hazard-ok: RULE - motivo)
        m_scoped = re.match(r"^:\s*([A-Z][A-Z0-9-]+)\s*(?:[—:-]\s*(.+))?$", tail)
        if m_scoped:
            rule = m_scoped.group(1).strip()
            reason = (m_scoped.group(2) or "").strip()
            if len(reason) < 3:
                bad_findings.append((
                    "UNJUSTIFIED-SUPPRESSION", path_str, f"line:{idx}",
                    f"hazard-ok for {rule} without justification reason"
                ))
            else:
                suppressed.setdefault(idx, set()).add(rule)
            continue
        # Formato 2: hazard-ok: motivo
        m_generic = re.match(r"^[:—-]\s*(.+)$", tail)
        if m_generic and len(m_generic.group(1).strip()) >= 3:
            suppressed.setdefault(idx, set()).add("ALL")
        else:
            bad_findings.append((
                "UNJUSTIFIED-SUPPRESSION", path_str, f"line:{idx}",
                "hazard-ok marker without mandatory justification reason"
            ))
    return suppressed, bad_findings


def _is_suppressed(suppressed: dict[int, set[str]], lineno: int, rule: str) -> bool:
    for ln in (lineno, lineno - 1):
        rules = suppressed.get(ln)
        if rules and ("ALL" in rules or rule in rules):
            return True
    return False


def scan_file(path: Path) -> tuple[int, list[tuple[str, str, str, str]], bool]:
    """Escaneia um arquivo. Retorna (1 se escaneado, findings, skipped_oversize)."""
    ext = path.suffix.lower()
    if ext not in SCAN_EXTS:
        return 0, [], False
    try:
        size = path.stat().st_size
    except OSError:
        return 0, [], True
    if size > MAX_FILE_BYTES:
        return 0, [], True
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            raw = path.read_text(encoding="latin-1")
        except OSError:
            return 0, [], True
    except OSError:
        return 0, [], True

    path_str = str(path)
    suppressed, findings = _extract_suppressions(raw, path_str)

    def add(rule: str, line_no: int, scope: str, msg: str) -> None:
        if not _is_suppressed(suppressed, line_no, rule):
            findings.append((rule, f"{path_str}:{line_no}", scope, msg))

    # --- PASS 2: Strings preservadas, comentários removidos ---
    no_comments = _strip_py_comments(raw) if ext == ".py" else _pg.mask_comments_only(raw, ext)

    for label, pat in _SECRET_PATTERNS:
        for m in pat.finditer(no_comments):
            matched = m.group(0)
            if matched in _DOCUMENTED_EXAMPLE_KEYS or "EXAMPLE" in matched:
                continue
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("HARDCODED-SECRET", lineno, label, f"hardcoded credential/key pattern ({label})")

    for m in _URL_CRED_RE.finditer(no_comments):
        user, pwd, host = m.group(1), m.group(2), m.group(3)
        if user.lower() in ("user", "username", "admin", "test", "foo") and pwd.lower() in ("pass", "password", "secret", "bar", "1234"):
            continue
        if host.lower() in ("localhost", "127.0.0.1", "example.com"):
            continue
        lineno = no_comments.count("\n", 0, m.start()) + 1
        add("URL-CRED", lineno, host, "credentials embedded in URL")

    sql_pats = (_SQL_INTERP_PY_RE, _SQL_CONCAT_RE) if ext == ".py" else (_SQL_INTERP_JS_RE, _SQL_CONCAT_RE, _SQL_SPRINTF_RE)
    for sp in sql_pats:
        for m in sp.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("SQL-CONCAT", lineno, f"line:{lineno}", "SQL query built via string interpolation/concatenation")

    # --- PASS 1: Código mascarado (strings e comentários vazios) ---
    masked = _pg.mask_c_like(raw, ext) if ext != ".py" else _strip_py_comments(raw)

    if ext == ".py":
        for m in _PY_SHELL_RE.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("SHELL-INJECTION", lineno, f"line:{lineno}", "subprocess with shell=True or os.system")
    elif ext == ".rs":
        for m in _RS_SHELL_RE.finditer(no_comments):
            if ".arg" in m.group(0):
                lineno = no_comments.count("\n", 0, m.start()) + 1
                add("SHELL-INJECTION", lineno, f"line:{lineno}", "shell invocation via Command::arg(\"-c\"|\"/c\")")
    else:
        for m in _JS_EXEC_RE.finditer(masked):
            lineno = masked.count("\n", 0, m.start()) + 1
            add("SHELL-INJECTION", lineno, f"line:{lineno}", "dynamic child_process.exec/execSync call")

    for m in _UNSAFE_DESER_RE.finditer(masked):
        tok = m.group(0)
        lineno = masked.count("\n", 0, m.start()) + 1
        if "yaml.load" in tok:
            line_raw = raw.splitlines()[lineno - 1] if lineno <= len(raw.splitlines()) else ""
            if "SafeLoader" in line_raw or "UniqueKeyLoader" in line_raw:
                continue
        add("UNSAFE-DESERIALIZE", lineno, f"line:{lineno}", f"unsafe code/data deserialization ({tok.strip()})")

    if not _SANITIZER_RE.search(raw):
        for m in _XSS_HTML_RE.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("XSS-RAW-HTML", lineno, f"line:{lineno}", f"untrusted raw HTML injection ({m.group(0).strip()})")

    for m in _ELECTRON_INSEC_RE.finditer(no_comments):
        lineno = no_comments.count("\n", 0, m.start()) + 1
        add("ELECTRON-INSECURE-CONFIG", lineno, f"line:{lineno}", f"insecure Electron webPreferences ({m.group(0).strip()})")

    if not _SEMAPHORE_RE.search(raw):
        for m in _UNBOUNDED_CONC_RE.finditer(masked):
            open_p = masked.find("(", m.start())
            close_p = _pg._paren_end(masked, open_p) if open_p != -1 else -1
            span = masked[open_p:close_p + 1] if close_p != -1 else masked[m.start():m.start() + 200]
            if _HEAVY_EXEC_RE.search(span):
                lineno = masked.count("\n", 0, m.start()) + 1
                add("UNBOUNDED-CONCURRENCY", lineno, f"line:{lineno}", "unbounded Promise.all/asyncio.gather spawning heavy process without semaphore")

    # Escopo intraprocedural por função (WEAK-CRYPTO e PATH-TRAVERSAL)
    if ext == ".py":
        # Extrai blocos def simples por indentação/regex ou arquivo como fallback
        fn_iter = [
            (m.group(1), raw.count("\n", 0, m.start()) + 1, 0, raw[m.start():m.start() + 1500])
            for m in re.finditer(r"(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", no_comments)
        ]
    else:
        fn_iter = list(_pg.find_functions(raw, ext))

    for fn_name, sl, _el, fn_body in fn_iter:
        if _WEAK_RAND_RE.search(fn_body) and _AUTH_TOKEN_WORDS_RE.search(fn_body):
            add("WEAK-CRYPTO", sl, fn_name, "non-CSPRNG random generator used in function handling token/secret/auth")
        if _FILE_SINK_RE.search(fn_body) and _EXT_SOURCE_RE.search(fn_body) and not _PATH_SAFE_RE.search(fn_body):
            add("PATH-TRAVERSAL", sl, fn_name, "dynamic file sink with external input and no path normalization/resolve in function")

    return 1, findings, False


def _check_env_committed(target: Path) -> list[tuple[str, str, str, str]]:
    root = target if target.is_dir() else target.parent
    try:
        res = subprocess.run(
            ["git", "-c", "core.quotePath=false", "ls-files"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        if res.returncode != 0:
            return []
    except Exception:
        return []
    out = []
    for rel in res.stdout.splitlines():
        rel_clean = rel.strip().strip('"').replace("\\", "/")
        name = Path(rel_clean).name.lower()
        if name in (".env", ".env.local", ".env.production", "id_rsa", "id_ed25519") or name.endswith((".pem", ".key")):
            out.append(("ENV-COMMITTED", str(root / rel_clean), "git-index", f"sensitive file '{rel_clean}' is tracked by git"))
    return out


def scan_path(target: Path) -> tuple[int, list[tuple[str, str, str, str]], int]:
    """Escaneia arquivo ou diretório. Retorna (files_scanned, findings, oversize_skipped)."""
    if not target.exists():
        return 0, [], 0
    if target.is_file():
        sc, fnd, ov = scan_file(target)
        return sc, fnd, (1 if ov else 0)

    total_scanned = 0
    all_findings: list[tuple[str, str, str, str]] = []
    oversize = 0
    all_findings.extend(_check_env_committed(target))

    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() not in SCAN_EXTS:
                continue
            sc, fnd, ov = scan_file(p)
            total_scanned += sc
            all_findings.extend(fnd)
            if ov:
                oversize += 1
    return total_scanned, all_findings, oversize


def main() -> None:
    ap = argparse.ArgumentParser(description="Scanner poliglota de hazards de segurança e runtime.")
    ap.add_argument("paths", nargs="+", help="Arquivos ou diretórios a escanear")
    ap.add_argument("--json", action="store_true", help="Emite saída em JSON")
    args = ap.parse_args()

    total_files = 0
    findings: list[tuple[str, str, str, str]] = []
    oversize = 0
    for raw_p in args.paths:
        p = Path(raw_p)
        if not p.exists():
            print(f"ERRO: caminho inexistente: {raw_p}", file=sys.stderr)
            sys.exit(2)
        sc, fnd, ov = scan_path(p)
        total_files += sc
        findings.extend(fnd)
        oversize += ov

    if total_files == 0 and not findings:
        print("NÃO-VEREDITO: 0 arquivos suportados escaneados.", file=sys.stderr)
        sys.exit(2)

    try:
        if args.json:
            print(json.dumps({
                "files_scanned": total_files,
                "oversize_skipped": oversize,
                "findings": [
                    {"rule": r, "location": loc, "scope": sc, "message": msg}
                    for r, loc, sc, msg in findings
                ],
            }, ensure_ascii=False, indent=2))
        else:
            for r, loc, sc, msg in findings:
                print(f"{r:<26} {loc}  [{sc}]  {msg}")
            print(f"{total_files} file(s) scanned, {len(findings)} finding(s), {oversize} oversize skipped")
        sys.stdout.flush()
    except BrokenPipeError:
        sys.exit(2)

    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
