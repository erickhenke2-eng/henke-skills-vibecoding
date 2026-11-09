#!/usr/bin/env python3
"""
hazard_scan.py — Portão mecânico poliglota de segurança e falhas de runtime (PROTOCOLO v3.4).

Sintetiza mecanismos de `cloudflare/skills` (workers-best-practices),
`cloudflare/security-audit-skill`, `trailofbits/skills` (sharp-edges) e `henke-blindagem`.

Arquitetura de 2 passes por arquivo (alinhada com swe-2-max):
  - Mapa de supressão extraído ESTRITAMENTE de comentários reais: `hazard-ok: <RULE> — <motivo>` isenta;
    `hazard-ok` sem regra ou sem motivo dispara `UNJUSTIFIED-SUPPRESSION` (exit 1). ALL implícito banido.
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

SCANNER_VERSION = "wave7.1"

import argparse
import fnmatch
import io
import json
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
_SUITE_ROOT = Path(__file__).resolve().parents[2]
_SUITE_MANIFEST = _SUITE_ROOT / "evals" / "hazard_fixtures_manifest.json"
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import polyglot as _pg  # noqa: E402

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB cap (evita bundles/minified)
SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".mjs", ".jsx", ".rs", ".go", ".vue", ".svelte"}
CONFIG_PATTERNS = (
    "*.tf", "*.tfvars", "Dockerfile*", "docker-compose*.yml", "docker-compose*.yaml",
    "serverless.yml", "serverless.yaml", "wrangler.toml", "cloudbuild.yml", "cloudbuild.yaml",
    "Pulumi.yml", "Pulumi.yaml", "*policy*.json", "iam*.json", "*bucket*.json", "firebase.json",
    ".gitlab-ci.yml",
)
_EXCLUDED_JSON_NAMES = {"package.json", "package-lock.json", "tsconfig.json", "jsconfig.json"}
SKIP_DIRS = {
    ".git", ".hg", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", "target", "dist", "build", "out", "release", "vendor",
    ".next", ".nuxt", ".svelte-kit", ".turbo", "coverage", ".venv", "venv",
}


def _is_ci_workflow(path: Path) -> bool:
    posix_low = "/" + path.as_posix().replace("\\", "/").lower().lstrip("/")
    if "/.github/workflows/" in posix_low and posix_low.endswith((".yml", ".yaml")):
        return True
    return path.name.lower() == ".gitlab-ci.yml"


def _is_config_file(path: Path) -> bool:
    name_low = path.name.lower()
    if name_low in _EXCLUDED_JSON_NAMES:
        return False
    if _is_ci_workflow(path):
        return True
    return any(fnmatch.fnmatch(name_low, pat.lower()) for pat in CONFIG_PATTERNS)


_is_config_surface = _is_config_file

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
_AUTH_TOKEN_WORDS_RE = re.compile(r"(?i)(?:token|secret|password|senha|session_?id|nonce|api_?key|jwt|csrf|auth_?code)")
_FILE_SINK_RE = re.compile(r"\b(?:readFileSync|readFile|writeFileSync|writeFile|sendFile|createReadStream)\s*\(\s*(?![`'\"])([A-Za-z_$][\w$.]*)")
_EXT_SOURCE_RE = re.compile(r"\b(?:req|request|params|query|userPath|inputPath|filePathFromUser)\b")
_PATH_SAFE_RE = re.compile(r"\b(?:resolve|normalize|basename|relpath|is_relative_to|safe_join|safe_member)\b")
_UNBOUNDED_CONC_RE = re.compile(r"\b(?:Promise\.all|asyncio\.gather)\s*\(")
_PROMISE_ALL_RE = _UNBOUNDED_CONC_RE
_HEAVY_EXEC_RE = re.compile(r"\b(?:spawn|exec|execFile|subprocess|ffmpeg|ffprobe|runGpu)\b")
_SEMAPHORE_RE = re.compile(r"\b(?:pLimit|p-limit|Semaphore|BoundedSemaphore|Bottleneck|runGpuBounded|maxConcurrency)\b")
_UNSAFE_DESER_RE = re.compile(r"(?<![.\w$])eval\s*\(|\bnew\s+Function\s*\(|\bpickle\.loads?\s*\(|\byaml\.load\s*\(")
_XSS_HTML_RE = re.compile(r"\bdangerouslySetInnerHTML\b|\.innerHTML\s*=(?!=)|\bv-html\s*=|\{@html\b")
_SANITIZER_RE = re.compile(r"(?i)\b(?:DOMPurify|sanitize|escapeHtml)\b")
_ELECTRON_INSEC_RE = re.compile(r"\bnodeIntegration\s*:\s*true\b|\bcontextIsolation\s*:\s*false\b")

# Regras Wave 2.5: Multi-Cloud (AWS/GCP/Cloudflare/Docker) & Pentest Red Team
_CLOUD_IAM_WILDCARD_RE = re.compile(
    r'(?i)"?(?:Action|actions)"?\s*[:=]\s*\[?\s*["\']\*["\']|\broles/(?:owner|editor)\b'
)
_CLOUD_BUCKET_PUB_RE = re.compile(
    r'\b(?:allUsers|allAuthenticatedUsers)\b|(?i:\bacl\s*[:=]\s*["\'](?:public-read|public-read-write)["\'])'
)
_CLOUD_SA_TYPE_RE = re.compile(r'"type"\s*:\s*"service_account"')
_DOCKER_ESC_RE = re.compile(r"(?m)\bprivileged\s*:\s*true\b|--privileged\b|/var/run/docker\.sock\b")
_IMDS_HOST_RE = re.compile(r"\b(?:169\.254\.169\.254|metadata\.google\.internal)\b")
_IMDS_V1_RE = _IMDS_HOST_RE
_IMDS_SAFE_RE = re.compile(r"(?i)\b(?:X-aws-ec2-metadata-token|Metadata-Flavor)\b")

# Regras Wave 5: Workflows de CI/CD (.github/workflows/*.y*ml, .gitlab-ci.yml)
_CI_PRT_RE = re.compile(r"\bpull_request_target\b")
_CI_PR_HEAD_RE = re.compile(r"\bgithub\.event\.pull_request\.head\b|\brefs/pull/")
_CI_CURL_PIPE_RE = re.compile(r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba)?sh\b")
_CI_SECRET_ECHO_RE = re.compile(r"\becho\s+[^\n]*\$\{\{\s*secrets\.")

_CORS_WILD_ORIGIN_RE = re.compile(
    r"""(?i)(?:Access-Control-Allow-Origin['"\s:,]+\*|\borigin\s*:\s*['"]\*['"]|\ballow_origins\s*=\s*\[\s*['"]\*['"]\s*\])"""
)
_CORS_CRED_TRUE_RE = re.compile(
    r"""(?i)(?:\bcredentials\s*:\s*true\b|Access-Control-Allow-Credentials['"\s:,]+true\b|\ballow_credentials\s*=\s*True\b)"""
)
_PROTO_POLLUTION_RE = re.compile(r"""\[\s*['"]__proto__['"]\s*\]|\.constructor\.prototype\s*=""")
_JWT_INSEC_FILE_RE = re.compile(
    r"""(?i)\balgorithms?\s*[:=]\s*\[?\s*['"]none['"]|\bverify_signature['"]?\s*[:=]\s*(?:false|False)\b|\bjwt\.decode\s*\([^)]*\bverify\s*=\s*False\b"""
)
_JWT_DECODE_CALL_RE = re.compile(r"\bjwt\.decode\s*\(")
_AUTH_FN_CONTEXT_RE = re.compile(r"(?i)(?:auth|token|verify_?token|session|guard|middleware|login)")
_FRAME_INTROSPECTION_RE = re.compile(
    r"sys\._getframe|inspect\.stack\s*\(|inspect\.currentframe|Error\.prepareStackTrace|new\s+Error\s*\([^)]*\)\s*\.stack")
_IPC_REGISTER_RE = re.compile(r"\bipcMain\b|\bipcRenderer\b|\.handle\s*\(|\.invoke\s*\(")
_DYN_INDEX_RE = re.compile(r"\b(?:handlers?|routes|actions|cmds?|fns|registry|obj)\s*\[\s*(?![\s'\"\d])\w+\s*\]")

_SSRF_SINK_RE = re.compile(
    r"""\b(?:fetch|axios\.(?:get|post|request)|got|requests\.(?:get|post|request)|httpx\.(?:get|post)|urlopen)\s*\(\s*(?![`'"])([A-Za-z_$][\w$.]*)"""
)
_SSRF_SOURCE_RE = re.compile(
    r"\b(?:req|request)\.(?:query|params|body|headers)\b|\b(?:targetUrl|webhookUrl|callbackUrl|redirectUrl|userUrl|externalUrl|endpointUrl)\b"
)
_SSRF_SAFE_RE = re.compile(
    r"\b(?:new\s+URL|urlparse|urlsplit|allowlist|whitelist|allowedHosts|isPrivateIP|ipaddress\.|hostname)\b"
)

_IDOR_SINK_RE = re.compile(
    r"\.(?:findById|findByPk|findOne|findUnique|findOneAndUpdate|findOneAndDelete|findByIdAndUpdate|findByIdAndDelete|deleteById)\s*\([^)]*\b(?:req|request)\.(?:params|query|body)\b"
)
_IDOR_SAFE_RE = re.compile(
    r"\b(?:req\.user|request\.user|userId|ownerId|tenantId|orgId|session|authorize|authorise|ability\.can|policy)\b|where\s*:\s*\{[^}]*\b(?:user|owner|tenant|org)"
)


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


def _extract_suppressions(raw: str, path_str: str, ext: str = "") -> tuple[dict[int, set[str]], list[tuple[str, str, str, str]]]:
    """Retorna ({lineno: {RULE}}, findings_de_supressao_invalida).
    Supressões são permitidas ESTRITAMENTE em comentários reais."""
    suppressed: dict[int, set[str]] = {}
    bad_findings: list[tuple[str, str, str, str]] = []

    lang = ext.lstrip(".").lower() if ext else (path_str.rsplit(".", 1)[-1].lower() if "." in path_str else "yaml")
    comment_spans = _pg.find_comment_spans(raw, lang)

    for span in comment_spans:
        c_text = span.text
        if "hazard-ok" not in c_text:
            continue
        for rel_ln, line in enumerate(c_text.splitlines()):
            cur_line = span.start_line + rel_ln
            if "hazard-ok" not in line:
                continue
            m = re.search(r"hazard-ok(.*)$", line)
            if not m:
                continue
            tail = m.group(1).strip()
            # Formato obrigatório: hazard-ok: RULE — motivo
            m_scoped = re.match(r"^:\s*([A-Z][A-Z0-9-]+)\s*(?:[—:-]|\s--)\s*(.+)$", tail)
            if m_scoped:
                rule = m_scoped.group(1).strip()
                reason = m_scoped.group(2).strip()
                if len(reason) < 3:
                    bad_findings.append((
                        "UNJUSTIFIED-SUPPRESSION", path_str, f"line:{cur_line}",
                        f"hazard-ok for {rule} without justification reason"
                    ))
                else:
                    suppressed.setdefault(cur_line, set()).add(rule)
                    if span.end_line != cur_line:
                        suppressed.setdefault(span.end_line, set()).add(rule)
            else:
                bad_findings.append((
                    "UNJUSTIFIED-SUPPRESSION", path_str, f"line:{cur_line}",
                    "hazard-ok marker without mandatory rule and justification (format: hazard-ok: RULE — motivo)"
                ))
    return suppressed, bad_findings


def _is_suppressed(suppressed: dict[int, set[str]], lineno: int, rule: str) -> bool:
    for ln in (lineno, lineno - 1):
        rules = suppressed.get(ln)
        if rules and rule in rules:
            return True
    return False


def _strip_hash_comments(raw: str) -> str:
    """Remove comentários `# ...` de arquivos IaC/YAML/TOML/Terraform/Dockerfile preservando quebras de linha."""
    return "\n".join(re.sub(r"\s*#.*$", "", ln) for ln in raw.splitlines())


_MANIFEST_CACHE: dict[str, set[tuple[str, str]]] = {}


def _load_manifest_pairs(m_path: Path) -> set[tuple[str, str]]:
    key = str(m_path)
    if key not in _MANIFEST_CACHE:
        try:
            data = json.loads(m_path.read_text(encoding="utf-8"))
            exempts = set()
            for item in data if isinstance(data, list) else []:
                f = item.get("file", "").replace("\\", "/").strip()
                r = item.get("rule", "").strip()
                if f and r:
                    exempts.add((f, r))
            _MANIFEST_CACHE[key] = exempts
        except Exception:
            _MANIFEST_CACHE[key] = set()
    return _MANIFEST_CACHE[key]


def _get_manifest_exemptions(file_path: Path, fixtures_manifest: Path | None = None) -> set[str]:
    """Retorna {rule} de isenções válidas para `file_path`.
    Nunca sobe diretórios pais de repositórios externos arbitrários: só honra `_SUITE_MANIFEST`
    quando `file_path` pertence a `_SUITE_ROOT`, ou `fixtures_manifest` passado explicitamente,
    exigindo igualdade exata do caminho relativo POSIX."""
    p_res = file_path.resolve()
    if fixtures_manifest is not None:
        m_path = Path(fixtures_manifest).resolve()
        if not m_path.is_file():
            return set()
        roots = [m_path.parent.parent, m_path.parent] if m_path.parent.name == "evals" else [m_path.parent, m_path.parent.parent]
    elif p_res.is_relative_to(_SUITE_ROOT) and _SUITE_MANIFEST.is_file():
        m_path = _SUITE_MANIFEST
        roots = [_SUITE_ROOT]
    else:
        return set()

    pairs = _load_manifest_pairs(m_path)
    if not pairs:
        return set()

    rel_candidates: set[str] = set()
    for root in roots:
        if p_res.is_relative_to(root):
            rel_candidates.add(p_res.relative_to(root).as_posix())

    exempt_rules: set[str] = set()
    for f_name, rule in pairs:
        if f_name in rel_candidates:
            exempt_rules.add(rule)
    return exempt_rules


def _scan_config_file(path: Path, no_comments: str, suppressed: dict[int, set[str]], add) -> None:
    nc_lines = no_comments.splitlines()
    for m in _CLOUD_IAM_WILDCARD_RE.finditer(no_comments):
        lineno = no_comments.count("\n", 0, m.start()) + 1
        tok = m.group(0)
        if "roles/" in tok:
            add("CLOUD-IAM-WILDCARD", lineno, f"line:{lineno}", f"over-permissive GCP IAM role ({tok.strip()})")
        else:
            win = "\n".join(nc_lines[max(0, lineno - 10):min(len(nc_lines), lineno + 10)])
            if re.search(r'(?i)\bAllow\b', win) and not re.search(r'(?i)"?Effect"?\s*[:=]\s*["\']Deny["\']', win):
                add("CLOUD-IAM-WILDCARD", lineno, f"line:{lineno}", "wildcard IAM Action '*' with Effect Allow")

    for m in _CLOUD_BUCKET_PUB_RE.finditer(no_comments):
        lineno = no_comments.count("\n", 0, m.start()) + 1
        add("CLOUD-BUCKET-PUBLIC", lineno, f"line:{lineno}", f"public cloud storage exposure ({m.group(0).strip()})")

    if _CLOUD_SA_TYPE_RE.search(no_comments) and '"private_key"' in no_comments:
        m_sa = _CLOUD_SA_TYPE_RE.search(no_comments)
        lineno = no_comments.count("\n", 0, m_sa.start()) + 1 if m_sa else 1
        add("CLOUD-SECRET-MATERIAL", lineno, "service_account", "GCP service_account JSON with private_key material")

    for m in _DOCKER_ESC_RE.finditer(no_comments):
        lineno = no_comments.count("\n", 0, m.start()) + 1
        add("DOCKER-ESCALATED", lineno, f"line:{lineno}", f"escalated container privilege or docker.sock mount ({m.group(0).strip()})")

    if _is_ci_workflow(path):
        m_prt = _CI_PRT_RE.search(no_comments)
        m_head = _CI_PR_HEAD_RE.search(no_comments)
        if m_prt and m_head:
            ln_prt = no_comments.count("\n", 0, m_prt.start()) + 1
            ln_head = no_comments.count("\n", 0, m_head.start()) + 1
            if not _is_suppressed(suppressed, ln_prt, "CI-PWN-REQUEST"):
                add("CI-PWN-REQUEST", ln_head, f"line:{ln_head}", "pull_request_target combined with untrusted PR head checkout")

        for m in _CI_CURL_PIPE_RE.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            block = "\n".join(nc_lines[max(0, lineno - 1):min(len(nc_lines), lineno + 2)])
            if "sha256" not in block.lower():
                add("CI-CURL-PIPE-SH", lineno, f"line:{lineno}", "unverified curl/wget pipe to shell in CI workflow")

        for m in _CI_SECRET_ECHO_RE.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("CI-SECRET-ECHO", lineno, f"line:{lineno}", "CI secret directly echoed to workflow log")


def _stmt_end_offset(text: str, pos: int) -> int:
    """Retorna o fim da instrução atual (primeiro ';' ou fim da linha após `pos`)."""
    nl = text.find("\n", pos)
    line_end = len(text) if nl == -1 else nl
    semi = text.find(";", pos, line_end)
    return semi if semi != -1 else line_end


DIST_EXTS = {".js", ".mjs", ".cjs", ".map", ".html", ".json"}
DIST_MAX_FILE_BYTES = 8 * 1024 * 1024


def scan_file(path: Path, extra_exts: set | None = None,
              max_bytes: int = MAX_FILE_BYTES,
              legacy_file_scope: bool = False,
              fixtures_manifest: Path | None = None) -> tuple[int, list[tuple[str, str, str, str]], bool]:
    """Escaneia um arquivo de código ou manifesto IaC/Cloud. Retorna (1 se escaneado, findings, skipped_oversize)."""
    ext = path.suffix.lower()
    is_cfg = _is_config_file(path)
    allowed = SCAN_EXTS | (extra_exts or set())
    if ext not in allowed and not is_cfg:
        return 0, [], False
    try:
        size = path.stat().st_size
    except OSError:
        return 0, [], True
    if size > max_bytes:
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
    suppressed, findings = _extract_suppressions(raw, path_str, ext)
    exempt_rules = _get_manifest_exemptions(path, fixtures_manifest=fixtures_manifest)

    def add(rule: str, line_no: int, scope: str, msg: str) -> None:
        if rule in exempt_rules:
            return
        if not _is_suppressed(suppressed, line_no, rule):
            findings.append((rule, f"{path_str}:{line_no}", scope, msg))

    # --- PASS 2 / CONFIG: Strings preservadas, comentários removidos ---
    if is_cfg and ext not in SCAN_EXTS:
        no_comments = _pg.mask_comments_only(raw, ".js") if ext == ".json" else _strip_hash_comments(raw)
    else:
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

    # Regras Multi-Cloud / IaC / CI Workflows
    if is_cfg:
        _scan_config_file(path, no_comments, suppressed, add)
        return 1, findings, False

    sql_pats = (_SQL_INTERP_PY_RE, _SQL_CONCAT_RE) if ext == ".py" else (_SQL_INTERP_JS_RE, _SQL_CONCAT_RE, _SQL_SPRINTF_RE)
    for sp in sql_pats:
        for m in sp.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("SQL-CONCAT", lineno, f"line:{lineno}", "SQL query built via string interpolation/concatenation")

    if legacy_file_scope:
        if not _IMDS_SAFE_RE.search(no_comments):
            for m in _IMDS_HOST_RE.finditer(no_comments):
                lineno = no_comments.count("\n", 0, m.start()) + 1
                add("IMDS-V1", lineno, m.group(0), "cloud metadata endpoint accessed without IMDSv2 token/Metadata-Flavor header")

    if _CORS_WILD_ORIGIN_RE.search(no_comments) and _CORS_CRED_TRUE_RE.search(no_comments):
        m_cors = _CORS_WILD_ORIGIN_RE.search(no_comments)
        lineno = no_comments.count("\n", 0, m_cors.start()) + 1 if m_cors else 1
        add("CORS-CRED-WILDCARD", lineno, f"line:{lineno}", "wildcard CORS origin ('*') combined with credentials=true")

    for m in _PROTO_POLLUTION_RE.finditer(no_comments):
        lineno = no_comments.count("\n", 0, m.start()) + 1
        add("PROTO-POLLUTION", lineno, f"line:{lineno}", f"direct prototype pollution vector ({m.group(0).strip()})")

    for m in _JWT_INSEC_FILE_RE.finditer(no_comments):
        lineno = no_comments.count("\n", 0, m.start()) + 1
        add("JWT-NONE-OR-UNVERIFIED", lineno, f"line:{lineno}", f"JWT verification disabled or 'none' algorithm allowed ({m.group(0).strip()})")

    # --- PASS 1: Código mascarado (strings e comentários vazios) ---
    masked = _pg.mask_py(raw) if ext == ".py" else _pg.mask_c_like(raw, ext)

    for m in _FRAME_INTROSPECTION_RE.finditer(masked):
        lineno = masked.count("\n", 0, m.start()) + 1
        add("FRAME-INTROSPECTION", lineno, f"line:{lineno}", "frame/stack introspection or Error().stack parsing")

    if ext == ".py":
        for m in _PY_SHELL_RE.finditer(masked):
            lineno = masked.count("\n", 0, m.start()) + 1
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

    for m in _ELECTRON_INSEC_RE.finditer(masked):
        lineno = masked.count("\n", 0, m.start()) + 1
        add("ELECTRON-INSECURE-CONFIG", lineno, f"line:{lineno}", f"insecure Electron webPreferences ({m.group(0).strip()})")

    if legacy_file_scope:
        if not _SANITIZER_RE.search(no_comments):
            for m in _XSS_HTML_RE.finditer(masked):
                lineno = masked.count("\n", 0, m.start()) + 1
                add("XSS-RAW-HTML", lineno, f"line:{lineno}", f"untrusted raw HTML injection ({m.group(0).strip()})")

        if not _SEMAPHORE_RE.search(no_comments):
            for m in _UNBOUNDED_CONC_RE.finditer(masked):
                open_p = masked.find("(", m.start())
                close_p = _pg._paren_end(masked, open_p) if open_p != -1 else -1
                span = masked[open_p:close_p + 1] if close_p != -1 else masked[m.start():m.start() + 200]
                if _HEAVY_EXEC_RE.search(span):
                    lineno = masked.count("\n", 0, m.start()) + 1
                    add("UNBOUNDED-CONCURRENCY", lineno, f"line:{lineno}", "unbounded Promise.all/asyncio.gather spawning heavy process without semaphore")

    # Escopo intraprocedural por função e código top-level
    nc_lines_full = no_comments.splitlines(keepends=True)
    if ext == ".py":
        fn_iter = []
        py_lines = masked.splitlines(keepends=True)
        top_level_lines = list(py_lines)
        top_level_nc_lines = list(nc_lines_full)
        for m in re.finditer(r"(?m)^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", masked):
            fn_name = m.group(2)
            s_line = masked.count("\n", 0, m.start()) + 1
            indent_len = len(m.group(1))
            e_line = s_line
            for l_idx in range(s_line, len(py_lines)):
                ln_strip = py_lines[l_idx].strip()
                if not ln_strip:
                    continue
                ln_indent = len(py_lines[l_idx]) - len(py_lines[l_idx].lstrip())
                if ln_indent <= indent_len:
                    break
                e_line = l_idx + 1
            fn_body = "".join(py_lines[s_line - 1:e_line])
            fn_body_nc = "".join(nc_lines_full[s_line - 1:e_line])
            fn_iter.append((fn_name, s_line, e_line, fn_body, fn_body_nc))
            for l_idx in range(s_line - 1, min(e_line, len(top_level_lines))):
                nl = "\n" if py_lines[l_idx].endswith("\n") else ""
                top_level_lines[l_idx] = nl
                if l_idx < len(top_level_nc_lines):
                    top_level_nc_lines[l_idx] = nl
        top_level_body = "".join(top_level_lines)
        top_level_nc_body = "".join(top_level_nc_lines)
        if top_level_body.strip() or top_level_nc_body.strip():
            fn_iter.append(("<top-level>", 1, len(py_lines), top_level_body, top_level_nc_body))
    else:
        fn_raw_iter = list(_pg.find_functions(raw, ext))
        c_lines = masked.splitlines(keepends=True)
        fn_iter = []
        top_level_lines = list(c_lines)
        top_level_nc_lines = list(nc_lines_full)
        for fn_name, sl, el, _ in fn_raw_iter:
            fn_body = "".join(c_lines[sl - 1:el])
            fn_body_nc = "".join(nc_lines_full[sl - 1:el])
            fn_iter.append((fn_name, sl, el, fn_body, fn_body_nc))
            for l_idx in range(max(0, sl - 1), min(el, len(top_level_lines))):
                nl = "\n" if c_lines[l_idx].endswith("\n") else ""
                top_level_lines[l_idx] = nl
                if l_idx < len(top_level_nc_lines):
                    top_level_nc_lines[l_idx] = nl
        top_level_body = "".join(top_level_lines)
        top_level_nc_body = "".join(top_level_nc_lines)
        if top_level_body.strip() or top_level_nc_body.strip():
            fn_iter.append(("<top-level>", 1, len(c_lines), top_level_body, top_level_nc_body))

    for fn_name, sl, _el, fn_body, fn_body_nc in fn_iter:
        if not legacy_file_scope:
            for m_xss in _XSS_HTML_RE.finditer(fn_body):
                rel_line = fn_body.count("\n", 0, m_xss.start()) + 1
                line_hit = sl + rel_line - 1
                stmt_end = _stmt_end_offset(fn_body, m_xss.end())
                fn_nc_upto = fn_body_nc[:stmt_end]
                if not _SANITIZER_RE.search(fn_nc_upto):
                    add("XSS-RAW-HTML", line_hit, fn_name, f"untrusted raw HTML injection ({m_xss.group(0).strip()})")

            for m_uc in _UNBOUNDED_CONC_RE.finditer(fn_body):
                open_p = fn_body.find("(", m_uc.start())
                close_p = _pg._paren_end(fn_body, open_p) if open_p != -1 else -1
                span = fn_body[open_p:close_p + 1] if close_p != -1 else fn_body[m_uc.start():m_uc.start() + 200]
                if _HEAVY_EXEC_RE.search(span):
                    rel_line = fn_body.count("\n", 0, m_uc.start()) + 1
                    line_hit = sl + rel_line - 1
                    stmt_end = close_p + 1 if close_p != -1 else _stmt_end_offset(fn_body, m_uc.end())
                    fn_nc_upto = fn_body_nc[:stmt_end]
                    has_sem = bool(_SEMAPHORE_RE.search(fn_nc_upto)) or (
                        fn_name != "<top-level>" and bool(_SEMAPHORE_RE.search(top_level_nc_body))
                    )
                    if not has_sem:
                        add("UNBOUNDED-CONCURRENCY", line_hit, fn_name, "unbounded Promise.all/asyncio.gather spawning heavy process without semaphore")

            if not _IMDS_SAFE_RE.search(fn_body_nc):
                for m_imds in _IMDS_HOST_RE.finditer(fn_body_nc):
                    line_hit = sl + fn_body_nc.count("\n", 0, m_imds.start())
                    add("IMDS-V1", line_hit, m_imds.group(0), "cloud metadata endpoint accessed without IMDSv2 token/Metadata-Flavor header")

        if _AUTH_TOKEN_WORDS_RE.search(fn_name + " " + fn_body):
            for m_wr in _WEAK_RAND_RE.finditer(fn_body):
                line_hit = sl + fn_body.count("\n", 0, m_wr.start())
                add("WEAK-CRYPTO", line_hit, fn_name, "non-CSPRNG random generator used in function handling token/secret/auth")
        if _EXT_SOURCE_RE.search(fn_body) and not _PATH_SAFE_RE.search(fn_body):
            for m_fs in _FILE_SINK_RE.finditer(fn_body):
                line_hit = sl + fn_body.count("\n", 0, m_fs.start())
                add("PATH-TRAVERSAL", line_hit, fn_name, "dynamic file sink with external input and no path normalization/resolve in function")
        if _SSRF_SOURCE_RE.search(fn_body) and not _SSRF_SAFE_RE.search(fn_body):
            for m_ss in _SSRF_SINK_RE.finditer(fn_body):
                line_hit = sl + fn_body.count("\n", 0, m_ss.start())
                add("SSRF-UNVALIDATED", line_hit, fn_name, "outbound HTTP request with external input and no URL/hostname validation in function")
        if _AUTH_FN_CONTEXT_RE.search(fn_name + " " + fn_body) and "jwt.verify" not in fn_body:
            if not _JWT_INSEC_FILE_RE.search(fn_body):
                for m_jwt in _JWT_DECODE_CALL_RE.finditer(fn_body):
                    line_hit = sl + fn_body.count("\n", 0, m_jwt.start())
                    add("JWT-NONE-OR-UNVERIFIED", line_hit, fn_name, "jwt.decode() used in auth context without jwt.verify()")
        if not _IDOR_SAFE_RE.search(fn_body):
            for m_id in _IDOR_SINK_RE.finditer(fn_body):
                line_hit = sl + fn_body.count("\n", 0, m_id.start())
                add("IDOR-NO-OWNERSHIP", line_hit, fn_name, "direct ORM lookup/mutation by req.params/query/body without ownership/session check in function")
        if _IPC_REGISTER_RE.search(fn_body):
            for m_di in _DYN_INDEX_RE.finditer(fn_body):
                line_hit = sl + fn_body.count("\n", 0, m_di.start())
                add("DYNAMIC-DISPATCH-IPC", line_hit, fn_name, f"blind dynamic dispatch inside IPC handler ({m_di.group(0)})")

    return 1, findings, False


def _check_env_committed(target: Path) -> list[tuple[str, str, str, str]]:
    cwd = target if target.is_dir() else target.parent
    try:
        top_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        if top_res.returncode != 0:
            return []
        git_root = Path(top_res.stdout.strip())
        res = subprocess.run(
            ["git", "-c", "core.quotePath=false", "ls-files"],
            cwd=str(git_root), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
        )
        if res.returncode != 0:
            return []
    except Exception:
        return []
    out = []
    target_resolved = target.resolve()
    for rel in res.stdout.splitlines():
        rel_clean = rel.strip().strip('"').replace("\\", "/")
        file_path = (git_root / rel_clean).resolve()
        if target.is_dir() and not file_path.is_relative_to(target_resolved):
            continue
        name = file_path.name.lower()
        if (
            name in (".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "credentials.json")
            or name.endswith((".pem", ".key", ".p12", ".kubeconfig"))
            or fnmatch.fnmatch(name, "*service*account*.json")
        ):
            out.append(("ENV-COMMITTED", str(file_path), "git-index", f"sensitive file '{rel_clean}' is tracked by git"))
    return out


def scan_path(target: Path, dist_mode: bool = False,
              legacy_file_scope: bool = False,
              fixtures_manifest: Path | None = None) -> tuple[int, list[tuple[str, str, str, str]], int]:
    """Escaneia arquivo ou diretório. Retorna (files_scanned, findings, oversize_skipped).
    `dist_mode` amplia extensões para bundles compilados (.js/.mjs/.cjs/.map/.html/.json)
    e eleva o teto para 8MB por arquivo."""
    if not target.exists():
        return 0, [], 0
    if target.is_file():
        sc, fnd, ov = scan_file(target, DIST_EXTS if dist_mode else None,
                                DIST_MAX_FILE_BYTES if dist_mode else MAX_FILE_BYTES,
                                legacy_file_scope=legacy_file_scope,
                                fixtures_manifest=fixtures_manifest)
        return sc, fnd, (1 if ov else 0)

    total_scanned = 0
    all_findings: list[tuple[str, str, str, str]] = []
    oversize = 0
    all_findings.extend(_check_env_committed(target))

    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() not in SCAN_EXTS and not (dist_mode and p.suffix.lower() in DIST_EXTS) and not _is_config_file(p):
                continue
            sc, fnd, ov = scan_file(p, DIST_EXTS if dist_mode else None,
                                    DIST_MAX_FILE_BYTES if dist_mode else MAX_FILE_BYTES,
                                    legacy_file_scope=legacy_file_scope,
                                    fixtures_manifest=fixtures_manifest)
            total_scanned += sc
            all_findings.extend(fnd)
            if ov:
                oversize += 1
    return total_scanned, all_findings, oversize


def main() -> None:
    ap = argparse.ArgumentParser(description="Scanner poliglota de hazards de segurança e runtime.")
    ap.add_argument("paths", nargs="*", help="Arquivos ou diretórios a escanear")
    ap.add_argument("--dist", type=str, default=None,
                    help="Re-escaneia diretório de build/bundle antes de PUBLISH (extensões de artefato, teto 8MB)")
    ap.add_argument("--legacy-file-scope", action="store_true",
                    help="Usa escopo legado de arquivo inteiro para XSS/UNBOUNDED-CONCURRENCY/IMDS")
    ap.add_argument("--fixtures-manifest", type=Path, default=None,
                    help="Caminho explícito para hazard_fixtures_manifest.json")
    ap.add_argument("--json", action="store_true", help="Emite saída em JSON")
    args = ap.parse_args()

    targets = list(args.paths)
    dist_mode = False
    if args.dist:
        targets = [args.dist]
        dist_mode = True
    if not targets:
        ap.error("informe ao menos um path ou --dist <dir>")

    total_files = 0
    findings: list[tuple[str, str, str, str]] = []
    oversize = 0
    for raw_p in targets:
        p = Path(raw_p)
        if not p.exists():
            print(f"ERRO: caminho inexistente: {raw_p}", file=sys.stderr)
            sys.exit(2)
        sc, fnd, ov = scan_path(p, dist_mode=dist_mode,
                                legacy_file_scope=args.legacy_file_scope,
                                fixtures_manifest=args.fixtures_manifest)
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
