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
  2  entrada inválida, arquivo(s) pulados por tamanho/legibilidade sem `--allow-oversize`
     (lacuna de cobertura não declarada — segredo em arquivo grande não sai verde),
     zero arquivos escaneados ou stdout fechado
"""
from __future__ import annotations

SCANNER_VERSION = "wave7.1"

import argparse
import ast
import fnmatch
import io
import json
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
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
if str(_SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SUITE_ROOT))
import polyglot as _pg  # noqa: E402
from henke.state_paths import sanitized_child_env  # noqa: E402

MAX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB cap (evita bundles/minified)
SCAN_EXTS = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".mts", ".cts",
             ".jsx", ".rs", ".go", ".vue", ".svelte"}
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


def _is_reparse_dir(p: Path) -> bool:
    """Junction/symlink/reparse — portável py3.11+ (Path.is_junction é 3.12+).
    Em dúvida, True: diretório que não lemos não é descido."""
    try:
        if p.is_symlink():
            return True
        st = p.lstat()
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _is_ci_workflow(path: Path) -> bool:
    posix_low = "/" + path.as_posix().replace("\\", "/").lower().lstrip("/")
    if "/.github/workflows/" in posix_low and posix_low.endswith((".yml", ".yaml")):
        return True
    return path.name.lower() == ".gitlab-ci.yml"


def _is_config_file(path: Path) -> bool:
    name_low = path.name.lower()
    # F30 (banca v11): esses JSONs NÃO são config IaC (o '*' de dependência
    # não é wildcard de IAM — canário M47) — mas ganham varredura restrita
    # de segredos/hooks em _scan_excluded_json.
    if name_low in _EXCLUDED_JSON_NAMES:
        return False
    if _is_ci_workflow(path):
        return True
    return any(fnmatch.fnmatch(name_low, pat.lower()) for pat in CONFIG_PATTERNS)


_is_config_surface = _is_config_file

# Regras de Pass 2 (strings preservadas, comentários removidos)
_SECRET_PATTERNS = (
    ("OpenAI/API Key", re.compile(r"\bsk-(?:proj-|live-)?[A-Za-z0-9]{20,}\b")),
    ("GitHub Token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b")),
    ("Private Key PEM", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
    ("Public Env Secret", re.compile(r"\b(?:VITE|NEXT_PUBLIC|PUBLIC)_[A-Z0-9_]*(?:SECRET|PRIVATE|API_KEY|TOKEN)\s*=\s*['\"][^'\"]{8,}['\"]")),
    # Cobertura ampliada (banca v7 — A9): providers com formato estável.
    ("Stripe Key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("Anthropic Key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GitLab PAT", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("npm Token", re.compile(r"\bnpm_[A-Za-z0-9]{36,}\b")),
    ("PyPI Token", re.compile(r"\bpypi-[A-Za-z0-9_-]{16,}\b")),
    ("DigitalOcean Token", re.compile(r"\bdop_v1_[a-f0-9]{64}\b")),
)
_DOCUMENTED_EXAMPLE_KEYS = {
    "AKIAIOSFODNN7EXAMPLE",
    "AKIAY34FZKBOKMUTVV7A",
}

# A7 (banca v12): DSNs fora de http — postgres/mysql/mongo/redis/amqp/ssh.
_URL_CRED_RE = re.compile(
    r"(?:https?|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|"
    r"amqps?|ldaps?|ssh|git\+https?|ftps?)://"
    r"([A-Za-z0-9._%-]+):([^/\s'\"@]{3,})@([A-Za-z0-9.-]+)")
_SQL_INTERP_PY_RE = re.compile(
    # A1 (banca v16): SQL canônica tem `'` dentro da f-string —
    # `WHERE n='{x}'` quebrava o [^'"]*; tail tolera ' e cobre .format/%(.
    # BANCA-C3: `\b` antes da alternação impedia TODO match com aspa de
    # abertura (`"..."` segue sempre char não-word) — %s-era inalcançável.
    # Três formas explícitas: f-string com {...}, str.format, e interpolação
    # %"..."%arg — a canônica SQLi `"...%s" % uid` agora casa.
    r"""(?ix)(?:
         \bf['"]{1,3}[^'"]*\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^'"]*\{[^}]+\}
       | ['"]{1,3}[^'"]*\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^'"]*['"]{1,3}\s*\.format\s*\(
       | ['"]{1,3}[^'"]*\b(?:SELECT\s+.+?\s+FROM|INSERT\s+INTO\s+\w+|UPDATE\s+\w+\s+SET|DELETE\s+FROM\s+\w+)[^'"]*%[sd][^'"]*['"]{1,3}\s*%\s*[\w(]
       )""",
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
_PY_SHELL_RE = re.compile(
    r"\bsubprocess\.(?:run|Popen|call|check_output|check_call)\s*\([^)]*\bshell\s*=\s*True\b|\bos\.system\s*\("
    # H1 (banca v14): sinks que só existiam no AST ficavam invisíveis quando
    # ast.parse falhava — fallback regex cobre os mesmos nomes.
    r"|\bos\.popen\s*\(|\bos\.(?:exec|spawn)[lvp]{0,3}\w*\s*\("
    r"|\b(?:asyncio\.)?create_subprocess_(?:shell|exec)\s*\("
    r"|\bsubprocess\.get(?:status)?output\s*\(|\bpty\.(?:spawn|fork)\s*\("
    r"|(?<![.\w$])exec\s*\(")
_JS_EXEC_RE = re.compile(r"(?<![.\w$])(?:child_process\s*\.\s*)?exec(?:Sync)?\s*\(\s*(?!['\"][^\n$'\"`]*['\"]\s*[,)])")
# Sinks de shell/código que o lookbehind do _JS_EXEC_RE não pega (banca v7 —
# A4): binding comum de child_process (`cp.exec`, `childProcess.spawn`),
# bracket-notation (`cp["exec"]` — o membro vira espaço na máscara), chamadas
# diretas de spawn/execFile, vm.*, strings em timers, require/import
# dinâmico e `obj.eval(`.
_JS_SHELL_OBJ_RE = re.compile(
    # exec/execSync = modo shell SEMPRE — flag mesmo com argv literal.
    r"\b(?:child_process|childProcess|cp|proc|child)\s*\.\s*"
    r"(?:exec|execSync)\s*\("
    # spawn/execFile/fork NÃO usam shell — só flag com argv dinâmico
    # (literal 'ffmpeg' é a forma segura; na máscara o literal vira espaço e
    # o primeiro char vivo é ',' — identificador ou '+' = dinâmico).
    r"|\b(?:child_process|childProcess|cp|proc|child)\s*\.\s*"
    r"(?:execFile|execFileSync|spawn|spawnSync|fork)\s*\(\s*(?:\+|[A-Za-z_$])"
    r"|\b(?:child_process|childProcess|cp|proc|child)\s*\[\s*\]\s*\("
    r"|\b(?:execFile|execFileSync|spawn|spawnSync)\s*\(\s*(?:\+|[A-Za-z_$])"
    r"|\bvm\.(?:runIn|Script)\w*\s*\("
    r"|\b(?:setTimeout|setInterval)\s*\(\s*[,)]"
    r"|\brequire\s*\(\s*(?!\s*\))"
    r"|\bimport\s*\(\s*(?!\s*\))"
    # H2 (banca v14): spawn/execFile com argv0 shell literal —
    # spawn("sh",["-c",u]) é shell real e não era flagado.
    r"|\b(?:spawn|spawnSync|execFile|execFileSync)\s*\(\s*\)"
    r"|\b(?:exec|execSync|execa|sh)\s*`"  # tagged template cp.exec`u`
    r"|(?<![.\w$])eval\s*\(|\.eval\s*\("
)
# H2 (banca v14): alias ESM de child_process — `import * as sh` /
# `import sh from 'child_process'` evadiam a allowlist de receptores.
_JS_CP_ALIAS_RE = re.compile(
    r"\bimport\s+(?:\*\s+as\s+(\w+)|(\w+)\s*(?:,\s*\{[^}]*\})?\s*from)"
    r"\s*['\"](?:node:)?child_process['\"]"
    # H6 (banca v15): `import { exec as run }`/`const { exec: run } =
    # require(...)` — destructure renomeado evadia tudo.
    r"|\b(?:import\s+\{[^}]*\b(?:exec|execSync|spawn|fork|execFile)\s+as\s+(\w+)"
    r"[^}]*\}\s*from\s*['\"](?:node:)?child_process['\"]"
    r"|(?:const|let|var)\s*\{[^}]*\b(?:exec|execSync|spawn|fork|execFile)"
    r"\s*:\s*(\w+)[^}]*\}\s*=\s*require\s*\()"
    # A2 (banca v16): `const mycp = require('child_process')` — módulo
    # inteiro num alias qualquer → usado como receptor `mycp.exec(u)`.
    r"|(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"](?:node:)?"
    r"child_process['\"]\s*\)")
_JS_SHELL_ARGV0_RE = re.compile(
    r"\b(?:spawn|spawnSync|execFile|execFileSync)\s*\(\s*['\"]"
    r"(?:[^'\"]*[\\/])?(?:sh|bash|zsh|cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh(?:\.exe)?)['\"]")
_RS_SHELL_RE = re.compile(r'Command::new\s*\(\s*\)\s*|\bCommand::new\s*\([^)]*\)\s*\.\s*arg\s*\(\s*"(?:-c|/c)"')
# Go não tinha nenhuma regra de execução (banca v9 A3): os/exec + syscall.Exec
# + plugin.Open são os sinks equivalentes a exec.Command("sh","-c",user).
_GO_SHELL_RE = re.compile(
    r'(?<![.\w])(?:exec\.Command(?:Context)?|syscall\.Exec|plugin\.Open)\s*\(')
_WEAK_RAND_RE = re.compile(r"\b(?:Math\.random|random\.random|random\.randint|rand::random)\s*\(")
_AUTH_TOKEN_WORDS_RE = re.compile(r"(?i)(?:token|secret|password|senha|session_?id|nonce|api_?key|jwt|csrf|auth_?code)")
_FILE_SINK_RE = re.compile(
    r"\b(?:readFileSync|readFile|writeFileSync|writeFile|sendFile|createReadStream|"
    # `open("prefixo/" + user)` — literal à esquerda vira espaço na máscara e
    # o primeiro char vivo é '+' (banca v7 — A7: lookahead anti-literal
    # pulava exatamente o caso de concatenação com variável).
    r"send_file|send_from_directory|open)\s*\(\s*(?:(\+)|([A-Za-z_$][\w$.]*))"
    r"|\.(?:read_text|read_bytes|write_text|write_bytes)\s*\(\s*\)"
    r"|Path\s*\(\s*([A-Za-z_$][\w$.]*)\s*\)"
)
_EXT_SOURCE_RE = re.compile(
    # Nomes web genéricos só são fonte externa com member-access (req.x /
    # req[...]) — um bare `for req in ...`/`req = f()` não é entrada HTTP
    # (dogfooding banca v8 — F1). Nomes explicitamente tainted contam bare.
    r"\b(?:req|request|params|query)\s*[.\[]|\b(?:userPath|inputPath|filePathFromUser)\b"
)
# Decoy-token: `let resolve;` não pode apagar o check — só conta se CHAMADO
# (banca v7 — A8).
_PATH_SAFE_RE = re.compile(r"\b(?:resolve|normalize|basename|relpath|is_relative_to|safe_join|safe_member)\s*\(")
_UNBOUNDED_CONC_RE = re.compile(r"\b(?:Promise\.all|asyncio\.gather)\s*\(")
_PROMISE_ALL_RE = _UNBOUNDED_CONC_RE
_HEAVY_EXEC_RE = re.compile(r"\b(?:spawn|exec|execFile|subprocess|ffmpeg|ffprobe|runGpu)\b")
# Decoy-token: `const pLimit = 1` (sem chamar) não é semáforo (A8).
_SEMAPHORE_RE = re.compile(r"\b(?:pLimit|Semaphore|BoundedSemaphore|Bottleneck|runGpuBounded)\s*\(|\bp-limit\b|\bmaxConcurrency\s*[:=]")
_UNSAFE_DESER_RE = re.compile(
    r"(?<![.\w$])eval\s*\(|\bnew\s+Function\s*\(|\bpickle\.loads?\s*\("
    r"|\byaml\.(?:load|load_all|unsafe_load|full_load)\s*\("
    # F6 (banca v11): família eval indireta — (0,eval), globalThis["eval"],
    # Function() sem new, eval?.(, eval.call( — nenhuma casava antes.
    r"|\(\s*0\s*,\s*eval\s*\)|globalThis\s*\[\s*['\"]eval['\"]\s*\]"
    r"|(?<![.\w$'\"`])Function\s*\(|eval\s*\?\.\s*\(|eval\.(?:call|apply)\s*\("
)
_XSS_HTML_RE = re.compile(
    r"\bdangerouslySetInnerHTML\b|\.innerHTML\s*\+?=(?!=)|\.outerHTML\s*\+?=(?!=)"
    r"|\binsertAdjacentHTML\s*\(|\bdocument\.write\s*\(|\bv-html\s*=|\{@html\b"
    r"|\.html\s*\(\s*(?![`'\"]\s*\))"
)
# Decoy-token: `let sanitize;` não isenta — a regra exige a CHAMADA do
# sanitizador (banca v7 — A8).
_SANITIZER_RE = re.compile(r"(?i)\b(?:DOMPurify|sanitize|escapeHtml)[\w$.]*\s*\(")
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
_DYN_INDEX_RE = re.compile(r"\b(?:handlers?|routes|actions|cmds?|fns|registry|obj|map|channels|commands)\s*\[\s*(?![\s'\"\d])\w+\s*\]")

_SSRF_SINK_RE = re.compile(
    r"""\b(?:fetch|axios\.(?:get|post|request)|got|requests\.(?:get|post|request)|httpx\.(?:get|post)|urlopen)\s*\(\s*(?:[+]|[A-Za-z_$][\w$.]*)"""
)
_SSRF_SOURCE_RE = re.compile(
    r"\b(?:req|request)\.(?:query|params|body|headers)\b|\b(?:targetUrl|webhookUrl|callbackUrl|redirectUrl|userUrl|externalUrl|endpointUrl)\b"
)
_SSRF_SAFE_RE = re.compile(
    # Decoy-token: `let whitelist;`/`let session;` não isentam (A8) —
    # validadores contam por CHAMADA ou uso concreto.
    r"\b(?:new\s+URL|urlparse|urlsplit|allowlist|whitelist|isPrivateIP)\s*\("
    r"|\ballowedHosts\b|\bipaddress\.|\.hostname\b"
)

_IDOR_SINK_RE = re.compile(
    r"\.(?:findById|findByPk|findOne|findUnique|findOneAndUpdate|findOneAndDelete|findByIdAndUpdate|findByIdAndDelete|deleteById)\s*\([^)]*\b(?:req|request)\.(?:params|query|body)\b"
)
_IDOR_SAFE_RE = re.compile(
    r"\b(?:req\.user|request\.user|userId|ownerId|tenantId|orgId|ability\.can|policy)\b"
    r"|\bsession\s*[.(\[]|\b(?:authorize|authorise)\s*\("
    r"|where\s*:\s*\{[^}]*\b(?:user|owner|tenant|org)"
)


def _strip_py_comments(raw: str) -> str:
    """Remove comentários `# ...` em Python usando `tokenize` stdlib, preservando linhas e strings."""
    # M3 (banca v14): splitlines() quebra em \x0b\x0c\x85\u2028 — o
    # tokenize conta só \n e os índices dessincronizavam (comentário
    # podia apagar a LINHA ERRADA, inclusive a do segredo).
    lines = [ln + "\n" for ln in raw.split("\n")]
    if lines:
        lines[-1] = lines[-1].rstrip("\n")
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


_HAZARD_OK_WEAK_WORDS = frozenset({"wip", "todo", "fixme", "ok", "later", "temp", "x"})


def _hazard_ok_reason_weak(reason: str) -> bool:
    """PT-13: motivo fraco/placeholder não vale supressão — exige >= 8 chars
    e não pode ser só palavra-genérica (wip/todo/fixme/ok/later/temp/x)."""
    r = reason.strip().lower()
    if len(r) < 8:
        return True
    toks = [t for t in re.split(r"[^a-z0-9]+", r) if t]
    return bool(toks) and all(t in _HAZARD_OK_WEAK_WORDS for t in toks)


def _extract_suppressions(raw: str, path_str: str, ext: str = "",
                          suppressed_log: list[tuple[str, str, int, str]] | None = None,
                          ) -> tuple[dict[int, set[str]], list[tuple[str, str, str, str]]]:
    """Retorna ({lineno: {RULE}}, findings_de_supressao_invalida).
    Supressões são permitidas ESTRITAMENTE em comentários reais.
    `suppressed_log` (opcional) recebe (rule, path, lineno, motivo) de cada
    supressão válida — inventário PT-13: supressão nunca é invisível."""
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
            # Formato obrigatório do marcador: ':' + RULE + ' — ' + motivo
            m_scoped = re.match(r"^:\s*([A-Z][A-Z0-9-]+)\s*(?:[—:-]|\s--)\s*(.+)$", tail)
            if m_scoped:
                rule = m_scoped.group(1).strip()
                reason = m_scoped.group(2).strip()
                # PT-13: razão fraca/placeholder (<8 chars ou só
                # wip/todo/fixme/ok/later/temp/x) invalida a supressão.
                if _hazard_ok_reason_weak(reason):
                    bad_findings.append((
                        "UNJUSTIFIED-SUPPRESSION", path_str, f"line:{cur_line}",
                        f"hazard-ok for {rule} with weak/placeholder reason "
                        "(require >=8 chars and a meaningful justification)"
                    ))
                else:
                    suppressed.setdefault(cur_line, set()).add(rule)
                    if span.end_line != cur_line:
                        suppressed.setdefault(span.end_line, set()).add(rule)
                    if suppressed_log is not None:
                        suppressed_log.append((rule, path_str, cur_line, reason))
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


def _mask_yaml_comments(raw: str) -> str:
    """PT-09: '#' em YAML só abre comentário fora de aspas e (a) no início da
    linha (só whitespace antes) ou (b) após whitespace. O strip cego
    `\\s*#.*$` apagava `'echo "build #42"; curl -s evil | sh'` — o '#' dentro
    de string escondia o pipe-to-shell.

    Mini-scanner char-a-char por linha: '...' escapa com '' e "..." com \\
    (estado de aspa não cruza linhas — conservador: conteúdo de scalar
    multi-linha permanece visível às regras). Block scalars (|/>) não são
    rastreados: '#' segue a regra de whitespace, que coincide com a
    semântica de comentário do shell em blocos run:/script:.
    """
    out_lines: list[str] = []
    for ln in raw.split("\n"):
        i, n = 0, len(ln)
        cut = n
        in_sq = False
        in_dq = False
        while i < n:
            c = ln[i]
            if in_sq:
                if c == "'":
                    if i + 1 < n and ln[i + 1] == "'":
                        i += 2  # '' escapado dentro de '...'
                        continue
                    in_sq = False
                i += 1
                continue
            if in_dq:
                if c == "\\":
                    i += 2  # \" / \\ dentro de "..."
                    continue
                if c == '"':
                    in_dq = False
                i += 1
                continue
            if c == "'":
                in_sq = True
                i += 1
                continue
            if c == '"':
                in_dq = True
                i += 1
                continue
            if c == "#" and (i == 0 or ln[i - 1] in " \t"):
                cut = i
                break
            i += 1
        out_lines.append(ln[:cut].rstrip(" \t") if cut < n else ln)
    return "\n".join(out_lines)


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


# ---------------------------------------------------------------------------
# Pass AST (.py) — resolve aliases de import que o regex não enxerga:
# `from subprocess import run` → `run(..., shell=True)`, `import os as o` →
# `o.popen(...)`. Só roda em arquivos .py; SyntaxError simplesmente omite o
# pass (os regexes de Pass 1/2 ainda cobrem o arquivo).
# ---------------------------------------------------------------------------
_AST_SHELL_FUNCS = {
    "os.system", "os.popen",
    "asyncio.create_subprocess_shell",
    "commands.getoutput", "commands.getstatusoutput",
    # Sempre-shell por definição (banca v7 — A5): getoutput/getstatusoutput
    # rodam via shell, pty.spawn executa direto.
    "subprocess.getoutput", "subprocess.getstatusoutput",
    "pty.spawn", "os.posix_spawn", "os.posix_spawnp",
}
_AST_SUBPROC_SHELL_KW = {
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_output", "subprocess.check_call",
    # argv explícito para interpretador conta igual (A5):
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    "os.execl", "os.execlp", "os.execle", "os.execlpe",
    "os.execv", "os.execvp", "os.execve", "os.execvpe",
    "os.spawnl", "os.spawnlp", "os.spawnle", "os.spawnlpe",
    "os.spawnv", "os.spawnvp", "os.spawnve", "os.spawnvpe",
    "pty.fork",
}
_AST_DESER_FUNCS = {
    "pickle.load", "pickle.loads", "dill.load", "dill.loads",
    "marshal.load", "marshal.loads", "shelve.open",
    # Wrappers de pickle (banca v7 — A5): todos deserializam sem sanitizer.
    "joblib.load", "torch.load", "pandas.read_pickle",
}
# getattr(mod, "system") / __import__("os") — ofuscação de import/acesso que
# o resolve() por nome não enxerga (A5).
_AST_DANGER_ATTRS = {
    "system", "popen", "getoutput", "getstatusoutput", "spawn",
    "load", "loads", "eval", "exec", "run", "Popen", "call",
}
_AST_DANGER_MODS = {"os", "subprocess", "pickle", "dill", "commands", "pty", "sys",
                    "builtins", "__builtins__"}
_AST_YAML_LOAD = {"yaml.load", "yaml.load_all"}
# unsafe_load/full_load nunca são seguras — nem com kwarg (A7-banca)
_AST_YAML_ALWAYS_UNSAFE = {"yaml.unsafe_load", "yaml.full_load"}
_AST_EXEC_BUILTINS = {"eval", "exec"}
# Interpretadores de comando quando passados como argv[0] com flag de comando
_AST_SHELL_BINS = {"sh", "bash", "zsh", "dash", "ash", "ksh", "csh",
                   "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe"}


def _py_ast_findings(raw: str, path_str: str) -> list[tuple[str, int, str, str]]:
    """Findings AST (rule, lineno, scope, msg) com resolução de alias de import."""
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        # H1 (banca v14): SyntaxError desligava TODOS os sinks AST e o
        # arquivo contava como escaneado-verde — fail-loud.
        return [("AST-UNPARSEABLE", 1, path_str,
                 "arquivo .py não parseia — análise estática cega; "
                 "corrija a sintaxe para o hazard gate avaliar")]
    aliases: dict[str, str] = {}
    star_mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                # `import a.b as x` → x resolve p/ a.b; sem alias, o nome raiz
                aliases[a.asname or a.name.split(".")[0]] = a.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for a in node.names:
                if a.name == "*":
                    # A2 (banca v12): `from os import *` — o nome vira bare
                    # (`system(cmd)`); resolve tenta `os.<nome>` contra os
                    # conjuntos de sink antes de aceitar o bare.
                    star_mods.append(mod)
                    continue
                aliases[a.asname or a.name] = f"{mod}.{a.name}"

    def resolve(func) -> str:
        if isinstance(func, ast.Name):
            if func.id in aliases:
                return aliases[func.id]
            for _sm in star_mods:
                for _ss in (_AST_SHELL_FUNCS, _AST_DESER_FUNCS,
                            _AST_SUBPROC_SHELL_KW, _AST_EXEC_BUILTINS):
                    if f"{_sm}.{func.id}" in _ss:
                        return f"{_sm}.{func.id}"
            return func.id
        if isinstance(func, ast.Attribute):
            base = resolve(func.value)
            return f"{base}.{func.attr}" if base else func.attr
        if isinstance(func, ast.Subscript):
            # __builtins__["eval"] / vars(os)["system"] — subscript literal
            # sobre namespace resolve como base.<literal> (banca v10 A5).
            base = resolve(func.value)
            sl = func.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                return f"{base}.{sl.value}" if base else sl.value
            return base
        if isinstance(func, ast.Call):
            # importlib.import_module("os").system(x) — o Call retorna o
            # módulo nomeado pelo arg literal; getattr(mod, "attr") idem.
            f = resolve(func.func)
            if f.rsplit(".", 1)[-1] in ("import_module", "__import__") and func.args:
                a0 = func.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    return a0.value
            if f.rsplit(".", 1)[-1] == "getattr" and len(func.args) >= 2:
                a0, a1 = resolve(func.args[0]), func.args[1]
                if isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                    return f"{a0}.{a1.value}" if a0 else a1.value
            return f
        return ""

    # Bindings de "loader" por nome — `SafeLoader = yaml.Loader` (ou
    # `class SafeLoader(yaml.Loader)`) é isenção falsificada por NOME
    # (banca v7 — A5): `Loader=SafeLoader` só isenta se o nome resolve de
    # fato para um loader seguro.
    loader_map: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            bound = resolve(node.value)
            if bound:
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        loader_map[t.id] = bound
                        # A3 (banca v12): `f = eval` / `s = os.system` — alias
                        # por atribuição (só import-alias era rastreado).
                        for _ss in (_AST_SHELL_FUNCS, _AST_DESER_FUNCS,
                                    _AST_SUBPROC_SHELL_KW, _AST_EXEC_BUILTINS):
                            if bound in _ss or bound.rsplit(".", 1)[-1] in _ss:
                                aliases.setdefault(t.id, bound)
        elif isinstance(node, ast.ClassDef) and node.bases:
            loader_map[node.name] = resolve(node.bases[0])

    _SAFE_LOADERS = ("SafeLoader", "CSafeLoader", "UniqueKeyLoader")

    def _loader_is_safe(expr) -> bool:
        if isinstance(expr, ast.Name):
            bound = loader_map.get(expr.id)
            if bound is not None:
                return bound.rsplit(".", 1)[-1] in _SAFE_LOADERS
            return expr.id in _SAFE_LOADERS
        if isinstance(expr, ast.Attribute):
            return expr.attr in _SAFE_LOADERS
        return False

    # Funções locais — `subprocess.run(cmd, **helper())` é verificável se o
    # helper retorna dict-literal sem a chave "shell" (senão o spread de
    # kwargs esconderia `shell=True` do check por nome — banca v7 — A5).
    local_fns = {n.name: n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    out: list[tuple[str, int, str, str]] = []
    raw_lines = raw.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = resolve(node.func)
        if not name:
            continue
        line = node.lineno
        scope = f"line:{line}"
        if name in _AST_SHELL_FUNCS or name.startswith("os.spawn"):
            out.append(("SHELL-INJECTION", line, scope, f"shell invocation via {name}() (AST)"))
            continue
        if name in _AST_SUBPROC_SHELL_KW:
            # Shell explícito por argv — ["sh","-c",x] / ["cmd","/c",x] — não
            # precisa de shell=True para executar via interpretador (A8-banca).
            if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
                elts = node.args[0].elts
                if (len(elts) >= 2
                        and isinstance(elts[0], ast.Constant) and isinstance(elts[0].value, str)
                        and isinstance(elts[1], ast.Constant) and isinstance(elts[1].value, str)):
                    exe = elts[0].value.replace("\\", "/").rsplit("/", 1)[-1].lower()
                    flag = elts[1].value.lower()
                    if exe in _AST_SHELL_BINS and flag in ("-c", "/c", "-command", "-enc", "-encodedcommand"):
                        out.append(("SHELL-INJECTION", line, scope,
                                   f"{name}() invokes {exe} {flag} (AST)"))
                        continue
            elif (node.args and isinstance(node.args[0], ast.Constant)
                  and isinstance(node.args[0].value, str)
                  and node.args[0].value.replace("\\", "/").rsplit("/", 1)[-1].lower() in _AST_SHELL_BINS):
                # `os.execvp("sh", ...)` / `create_subprocess_exec("sh",...)`
                # — argv0 escalar apontando direto pro shell (banca v7 — A5).
                out.append(("SHELL-INJECTION", line, scope,
                            f"{name}() executes shell binary {node.args[0].value!r} directly (AST)"))
                continue
            for kw in node.keywords:
                if kw.arg == "shell":
                    if isinstance(kw.value, ast.Constant) and not kw.value.value:
                        break  # shell=False explícito — seguro
                    out.append(("SHELL-INJECTION", line, scope, f"{name}(shell=...) truthy/dynamic (AST)"))
                    break
                if kw.arg is None:
                    # `**{"shell": True}` / `**opts` / `**helper()` — spread
                    # de kwargs que o check por nome não enxerga. Resolvido a
                    # False = comprovadamente sem "shell"; None = não
                    # verificável → flag conservador.
                    _shell_in_spread = None
                    if isinstance(kw.value, ast.Dict):
                        for k, v in zip(kw.value.keys, kw.value.values):
                            if (isinstance(k, ast.Constant) and k.value == "shell"):
                                _shell_in_spread = not (
                                    isinstance(v, ast.Constant) and not v.value
                                )
                    elif (isinstance(kw.value, ast.Call)
                          and isinstance(kw.value.func, ast.Name)
                          and kw.value.func.id in local_fns):
                        rets = [r.value for r in ast.walk(local_fns[kw.value.func.id])
                                if isinstance(r, ast.Return)]
                        dict_rets = [r for r in rets if isinstance(r, ast.Dict)]
                        if dict_rets and len(dict_rets) == len(rets):
                            _shell_in_spread = any(
                                isinstance(k, ast.Constant) and k.value == "shell"
                                and not (isinstance(v, ast.Constant) and not v.value)
                                for d in dict_rets for k, v in zip(d.keys, d.values))
                    if _shell_in_spread is False:
                        continue
                    out.append(("SHELL-INJECTION", line, scope,
                                f"{name}(**kwargs) — shell possivelmente oculto em spread (AST)"))
                    break
            continue
        if name in _AST_DESER_FUNCS:
            out.append(("UNSAFE-DESERIALIZE", line, scope, f"unsafe deserialization via {name}() (AST)"))
            continue
        if name in _AST_YAML_LOAD or name in _AST_YAML_ALWAYS_UNSAFE:
            if name in _AST_YAML_ALWAYS_UNSAFE:
                # unsafe_load/full_load são perigosos INDEPENDENTE de kwargs
                out.append(("UNSAFE-DESERIALIZE", line, scope, f"{name}() is never safe (AST)"))
                continue
            # Só isenta com loader comprovadamente seguro no kwarg — nome EXATO
            # do loader via AST ('NotSafeLoader' contém 'SafeLoader' por
            # substring, e mencionar SafeLoader num comentário não torna a
            # chamada segura — F2 banca v6).
            safe_loader = any(
                kw.arg in ("Loader", "loader") and _loader_is_safe(kw.value)
                for kw in node.keywords
            )
            if safe_loader:
                continue
            out.append(("UNSAFE-DESERIALIZE", line, scope, "yaml.load() without SafeLoader (AST)"))
            continue
        if name == "numpy.load":
            # numpy.load é seguro por default (allow_pickle=False) — só flag
            # quando o chamador religa pickle explicitamente (A5).
            _pickle_on = any(
                kw.arg == "allow_pickle"
                and not (isinstance(kw.value, ast.Constant) and not kw.value.value)
                for kw in node.keywords
            )
            if _pickle_on:
                out.append(("UNSAFE-DESERIALIZE", line, scope,
                            "numpy.load(allow_pickle=True) — deserialização arbitrária (AST)"))
            continue
        if name == "__import__":
            if (node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.split(".")[0] in _AST_DANGER_MODS):
                out.append(("UNSAFE-DESERIALIZE", line, scope,
                            f"__import__({node.args[0].value!r}) — import ofuscado de módulo perigoso (AST)"))
            continue
        if name == "getattr":
            if (len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and node.args[1].value in _AST_DANGER_ATTRS):
                mod_res = resolve(node.args[0])
                if mod_res.split(".")[0] in _AST_DANGER_MODS or not mod_res:
                    out.append(("UNSAFE-DESERIALIZE", line, scope,
                                f"getattr(..., {node.args[1].value!r}) — acesso ofuscado a sink (AST)"))
            continue
        # `builtins.eval(x)` resolve para "builtins.eval" — o sufixo é o sink
        # (e `obj.eval(` oferecido à força é direção segura, fail-red).
        if name in _AST_EXEC_BUILTINS or name.rsplit(".", 1)[-1] in _AST_EXEC_BUILTINS:
            out.append(("UNSAFE-DESERIALIZE", line, scope, f"dynamic code execution via {name}() (AST)"))
    return out


_NPM_LIFECYCLE_RE = re.compile(
    r'"(?:preinstall|postinstall|prepare|prepublish(?:Only)?|prepack|postpack)"'
    r'\s*:\s*"([^"]*)"')


def _scan_excluded_json(path: Path, max_bytes: int
                        ) -> tuple[int, list[tuple[str, str, str, str]], bool]:
    """Varredura restrita para JSONs excluídos do scan IaC (F30 banca v11):
    segredos, credenciais em URL, pipe-to-shell e lifecycle hooks perigosos.
    Retorna (1 se escaneado, findings, skipped_oversize)."""
    try:
        if path.stat().st_size > max_bytes:
            return 0, [], True
        raw = path.read_text(encoding="utf-8-sig")  # A7 v16: BOM → wedge
    except (OSError, UnicodeDecodeError):
        return 0, [], True
    out: list[tuple[str, str, str, str]] = []
    for label, pat in _SECRET_PATTERNS:
        for m in pat.finditer(raw):
            if m.group(0) in _DOCUMENTED_EXAMPLE_KEYS or "EXAMPLE" in m.group(0):
                continue
            out.append(("HARDCODED-SECRET", f"{path}:{raw.count(chr(10), 0, m.start()) + 1}",
                        label, f"hardcoded credential/key pattern ({label})"))
    for m in _URL_CRED_RE.finditer(raw):
        host = m.group(3)
        if host.lower() not in ("localhost", "127.0.0.1", "example.com"):
            out.append(("URL-CRED", f"{path}:{raw.count(chr(10), 0, m.start()) + 1}",
                        "url", "credenciais embutidas em URL"))
    for m in _CI_CURL_PIPE_RE.finditer(raw):
        out.append(("CURL-PIPE-SH", f"{path}:{raw.count(chr(10), 0, m.start()) + 1}",
                    "pkg", "pipe download->shell em JSON de pacote"))
    for m in _NPM_LIFECYCLE_RE.finditer(raw):
        if re.search(r"curl|wget|\bsh\b|bash|node\s+-e|eval", m.group(1)):
            out.append(("NPM-LIFECYCLE-RISK",
                        f"{path}:{raw.count(chr(10), 0, m.start()) + 1}",
                        "pkg", f"lifecycle hook com comando perigoso: {m.group(1)[:60]}"))
    return 1, out, False


def scan_file(path: Path, extra_exts: set | None = None,
              max_bytes: int = MAX_FILE_BYTES,
              legacy_file_scope: bool = False,
              fixtures_manifest: Path | None = None,
              suppressed_log: list[tuple[str, str, int, str]] | None = None
              ) -> tuple[int, list[tuple[str, str, str, str]], bool]:
    """Escaneia um arquivo de código ou manifesto IaC/Cloud. Retorna (1 se escaneado, findings, skipped_oversize)."""
    ext = path.suffix.lower()
    is_cfg = _is_config_file(path)
    allowed = SCAN_EXTS | (extra_exts or set())
    if ext not in allowed and not is_cfg:
        # F30 (banca v11): package.json & cia não são IaC, mas segredos,
        # URL-CRED e lifecycle hooks perigosos não podem sair sem varredura.
        if path.name.lower() in _EXCLUDED_JSON_NAMES and ext == ".json":
            return _scan_excluded_json(path, max_bytes)
        return 0, [], False
    try:
        size = path.stat().st_size
    except OSError:
        return 0, [], True
    if size > max_bytes:
        return 0, [], True
    try:
        raw = path.read_text(encoding="utf-8-sig")  # A7 v16: BOM → wedge
    except UnicodeDecodeError:
        try:
            _b = path.read_bytes()
        except OSError:
            return 0, [], True
        # UTF-16 vira mojibake 'e\x00v\x00a\x00l\x00' em latin-1 — padrão
        # nenhum casa e o arquivo conta como escaneado-verde (banca v7 — A11).
        # BOM ou densidade de NUL no head ⇒ decodifica como UTF-16 de verdade.
        _head = _b[:2048]
        _nul_ratio = _head.count(b"\x00") / max(len(_head), 1)
        # F15 (banca v11): UTF-32LE BOM (FF FE 00 00) casava com o prefixo
        # FF FE → decodificava como UTF-16 e gerava mojibake 'e\x00v\x00…'
        # que nenhuma regra casa. UTF-32 verificado ANTES de UTF-16.
        if _b.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            try:
                raw = _b.decode("utf-32")
            except (UnicodeDecodeError, ValueError):
                return 1, [("SUSPICIOUS-ENCODING",
                            f"{path}:1", "file",
                            "arquivo UTF-32 não decodificável — "
                            "conteúdo não auditável (fail-closed)")], False
        elif _b.startswith((b"\xff\xfe", b"\xfe\xff")) or _nul_ratio > 0.15:
            try:
                raw = _b.decode("utf-16")
            except (UnicodeDecodeError, ValueError):
                return 1, [("SUSPICIOUS-ENCODING",
                            f"{path}:1", "file",
                            "arquivo com BOM/NUL-bytes não decodificável — "
                            "conteúdo não auditável (fail-closed)")], False
        else:
            try:
                raw = _b.decode("latin-1")
            except (UnicodeDecodeError, ValueError):
                return 0, [], True
    except OSError:
        return 0, [], True

    path_str = str(path)
    suppressed, findings = _extract_suppressions(raw, path_str, ext, suppressed_log)
    exempt_rules = _get_manifest_exemptions(path, fixtures_manifest=fixtures_manifest)

    def add(rule: str, line_no: int, scope: str, msg: str) -> None:
        if rule in exempt_rules:
            return
        if not _is_suppressed(suppressed, line_no, rule):
            findings.append((rule, f"{path_str}:{line_no}", scope, msg))

    # --- PASS 2 / CONFIG: Strings preservadas, comentários removidos ---
    if is_cfg and ext not in SCAN_EXTS:
        if ext == ".json":
            no_comments = _pg.mask_comments_only(raw, ".js")
        elif ext in (".yml", ".yaml"):
            no_comments = _mask_yaml_comments(raw)  # PT-09
        else:
            no_comments = _strip_hash_comments(raw)
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
        # F19 (banca v11): 'admin:password@prod.internal' era isento — cred
        # "placeholder" só vale em host claramente de exemplo/localhost.
        if (user.lower() in ("user", "username", "test", "foo")
                and pwd.lower() in ("pass", "password", "secret", "bar", "1234")
                and host.lower().split(":")[0] in (
                    "localhost", "127.0.0.1", "example.com", "example.org",
                    "example.net", "host", "hostname", "127.0.1.1")):
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
    elif ext == ".go":
        for m in _GO_SHELL_RE.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("SHELL-INJECTION", lineno, f"line:{lineno}",
                f"execução de processo/código dinâmico ({m.group(0).strip()})")
    else:
        for m in _JS_EXEC_RE.finditer(masked):
            lineno = masked.count("\n", 0, m.start()) + 1
            add("SHELL-INJECTION", lineno, f"line:{lineno}", "dynamic child_process.exec/execSync call")
        for m in _JS_SHELL_OBJ_RE.finditer(masked):
            lineno = masked.count("\n", 0, m.start()) + 1
            add("SHELL-INJECTION", lineno, f"line:{lineno}",
                f"dynamic process/code execution sink ({m.group(0).strip()[:40]})")
        # H2 (banca v14): spawn/execFile com argv0 shell literal e alias ESM
        # `import * as sh from 'child_process'` — rodam no texto cru, onde o
        # literal/alias ainda é visível (na máscara vira espaço).
        for m in _JS_SHELL_ARGV0_RE.finditer(no_comments):
            lineno = no_comments.count("\n", 0, m.start()) + 1
            add("SHELL-INJECTION", lineno, f"line:{lineno}",
                "spawn/execFile com argv0 de shell literal")
        for _am in _JS_CP_ALIAS_RE.finditer(no_comments):
            _al = _am.group(1) or _am.group(2) or _am.group(5)
            if _al:
                _al_re = re.compile(
                    r"\b" + re.escape(_al)
                    + r"\s*\.\s*(?:exec|execSync)\s*\(|"
                    + r"\b" + re.escape(_al)
                    + r"\s*\.\s*(?:spawn|spawnSync|fork|execFile)\s*\(\s*(?:\+|[A-Za-z_$'\"`])")
                for m in _al_re.finditer(masked):
                    lineno = masked.count("\n", 0, m.start()) + 1
                    add("SHELL-INJECTION", lineno, f"line:{lineno}",
                        f"child_process alias '{_al}' shell sink")
            _al2 = _am.group(3) or _am.group(4)
            if _al2:
                # H6 (banca v15): alias desestruturado É o nome do sink —
                # `{ exec as run }` → `run(cmd)`.
                for m in re.finditer(
                        r"\b" + re.escape(_al2)
                        + r"\s*\(\s*(?:\+|[A-Za-z_$'\"`])", masked):
                    lineno = masked.count("\n", 0, m.start()) + 1
                    add("SHELL-INJECTION", lineno, f"line:{lineno}",
                        f"child_process destructured alias '{_al2}' sink")

    for m in _UNSAFE_DESER_RE.finditer(masked):
        tok = m.group(0)
        lineno = masked.count("\n", 0, m.start()) + 1
        if "yaml.load" in tok:
            line_raw = raw.splitlines()[lineno - 1] if lineno <= len(raw.splitlines()) else ""
            # Isenção exige o kwarg Loader=<nome exato seguro> — substring nua
            # casaria 'NotSafeLoader' e comentários mencionando SafeLoader (F2).
            if not re.search(r"Loader\s*=\s*(?:\w+\.)*(?:CSafeLoader|SafeLoader|UniqueKeyLoader)\b", line_raw):
                pass
            else:
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

    # --- Pass AST (.py): aliases de import que o regex não resolve ---
    if ext == ".py":
        for rule, line_no, scope, msg in _py_ast_findings(raw, path_str):
            add(rule, line_no, scope, msg)

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
                # F8: ident capturado ligado a literal na MESMA função não é
                # traversal (`const p = "x.txt"; writeFile(p, req.body)`).
                ident = m_fs.group(2) or m_fs.group(3)
                if ident and re.search(
                    r"\b(?:const|let|var)?\s*" + re.escape(ident) + r"\s*=\s*['\"`]",
                    fn_body,
                ):
                    continue
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

    # Dedup (rule, location): regex e AST cobrem o mesmo call-site.
    if len(findings) > 1:
        _seen: set[tuple[str, str]] = set()
        findings = [f for f in findings if (f[0], f[1]) not in _seen and not _seen.add((f[0], f[1]))]
    return 1, findings, False


def _check_env_committed(target: Path) -> list[tuple[str, str, str, str]]:
    cwd = target if target.is_dir() else target.parent
    try:
        top_res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
            # A19 (banca v12): GIT_* herdado resolvia toplevel de repo estranho.
            env={**sanitized_child_env(cwd), "LC_ALL": "C"},
        )
        if top_res.returncode != 0:
            err_l = (top_res.stderr or "").lower()
            if "not a git repository" in err_l or "not a git repo" in err_l:
                # Alvo FORA de qualquer worktree (ex.: snapshot --staged extraído
                # para tempdir): não existe índice, logo nada commitado —
                # a verificação é n/a, não falha de infraestrutura.
                return []
            # rc!=0 silencioso = .env commitado invisível (banca v10 A7).
            return [("ENV-CHECK-UNAVAILABLE", str(target), "git",
                     "git rev-parse falhou — arquivos sensíveis commitados não auditados")]
        git_root = Path(top_res.stdout.strip())
        res = subprocess.run(
            ["git", "-c", "core.quotePath=false",
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "ls-files"],
            cwd=str(git_root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
            env=sanitized_child_env(git_root),
        )
        if res.returncode != 0:
            return [("ENV-CHECK-UNAVAILABLE", str(target), "git",
                     "git ls-files falhou — arquivos sensíveis commitados não auditados")]
    except Exception:
        # Git indisponível ≠ repo limpo — falha de enumeração vira finding de
        # infraestrutura (fail-closed, banca v7 — A10).
        return [("ENV-CHECK-UNAVAILABLE", str(target), "git",
                 "git ls-files falhou — arquivos sensíveis commitados não auditados")]
    out = []
    target_resolved = target.resolve()
    for rel in res.stdout.splitlines():
        rel_clean = rel.strip().strip('"').replace("\\", "/")
        file_path = (git_root / rel_clean).resolve()
        if target.is_dir() and not file_path.is_relative_to(target_resolved):
            continue
        name = file_path.name.lower()
        if (
            name.startswith(".env") or name.endswith(".env")
            or name in ("id_rsa", "id_ed25519", "id_dsa", "id_ecdsa", "credentials.json")
            or name.startswith(("id_rsa", "id_ed25519", "id_dsa", "id_ecdsa"))
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".kubeconfig", ".env"))
            or fnmatch.fnmatch(name, "*service*account*.json")
        ):
            out.append(("ENV-COMMITTED", str(file_path), "git-index", f"sensitive file '{rel_clean}' is tracked by git"))
    return out


def scan_path(target: Path, dist_mode: bool = False,
              legacy_file_scope: bool = False,
              fixtures_manifest: Path | None = None,
              suppressed_log: list[tuple[str, str, int, str]] | None = None
              ) -> tuple[int, list[tuple[str, str, str, str]], int]:
    """Escaneia arquivo ou diretório. Retorna (files_scanned, findings, oversize_skipped).
    `dist_mode` amplia extensões para bundles compilados (.js/.mjs/.cjs/.map/.html/.json)
    e eleva o teto para 8MB por arquivo."""
    if not target.exists():
        return 0, [], 0
    if target.is_file():
        sc, fnd, ov = scan_file(target, DIST_EXTS if dist_mode else None,
                                DIST_MAX_FILE_BYTES if dist_mode else MAX_FILE_BYTES,
                                legacy_file_scope=legacy_file_scope,
                                fixtures_manifest=fixtures_manifest,
                                suppressed_log=suppressed_log)
        return sc, fnd, (1 if ov else 0)

    total_scanned = 0
    all_findings: list[tuple[str, str, str, str]] = []
    oversize = 0
    all_findings.extend(_check_env_committed(target))

    for dirpath, dirnames, filenames in os.walk(target):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        dirnames[:] = [d for d in dirnames
                       if not _is_reparse_dir(Path(dirpath) / d)]

        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() not in SCAN_EXTS and not (dist_mode and p.suffix.lower() in DIST_EXTS) and not _is_config_file(p):
                continue
            sc, fnd, ov = scan_file(p, DIST_EXTS if dist_mode else None,
                                    DIST_MAX_FILE_BYTES if dist_mode else MAX_FILE_BYTES,
                                    legacy_file_scope=legacy_file_scope,
                                    fixtures_manifest=fixtures_manifest,
                                    suppressed_log=suppressed_log)
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
    ap.add_argument("--allow-oversize", action="store_true",
                    help="Aceita EXPLICITAMENTE a lacuna de cobertura de arquivos pulados "
                         "por tamanho/legibilidade (oversize). Sem a flag, oversize>0 -> exit 2.")
    ap.add_argument("--json", action="store_true", help="Emite saída em JSON")
    try:
        args = ap.parse_args()
    except SystemExit:
        # --help sai 0 no argparse — um portão sem veredito nunca é verde.
        sys.exit(2)

    targets = list(args.paths)
    dist_mode = False
    if args.dist:
        targets = [args.dist]
        dist_mode = True
    if not targets:
        ap.error("informe ao menos um path ou --dist <dir>")

    total_files = 0
    findings: list[tuple[str, str, str, str]] = []
    suppressed_log: list[tuple[str, str, int, str]] = []
    oversize = 0
    for raw_p in targets:
        p = Path(raw_p)
        if not p.exists():
            print(f"ERRO: caminho inexistente: {raw_p}", file=sys.stderr)
            sys.exit(2)
        sc, fnd, ov = scan_path(p, dist_mode=dist_mode,
                                legacy_file_scope=args.legacy_file_scope,
                                fixtures_manifest=args.fixtures_manifest,
                                suppressed_log=suppressed_log)
        total_files += sc
        findings.extend(fnd)
        oversize += ov

    oversize_blocked = oversize > 0 and not args.allow_oversize
    oversize_gap = oversize > 0 and args.allow_oversize

    if total_files == 0 and not findings:
        if oversize:
            print(
                f"ERRO: {oversize} arquivo(s) pulados sem análise (oversize > teto ou ilegível)"
                + (" — lacuna aceita via --allow-oversize, mas " if oversize_gap else " — ")
                + "0 arquivos efetivamente escaneados.",
                file=sys.stderr,
            )
        print("NÃO-VEREDITO: 0 arquivos suportados escaneados.", file=sys.stderr)
        sys.exit(2)

    try:
        if args.json:
            payload = {
                "files_scanned": total_files,
                "oversize_skipped": oversize,
                "oversize_blocked": oversize_blocked,
                "findings": [
                    {"rule": r, "location": loc, "scope": sc, "message": msg}
                    for r, loc, sc, msg in findings
                ],
                # PT-13: inventário de supressões válidas — nunca invisível.
                "suppressed": [
                    {"rule": r, "location": f"{loc}:{ln}", "reason": rs}
                    for r, loc, ln, rs in suppressed_log
                ],
            }
            if oversize_gap:
                payload["coverage_gaps"] = [
                    f"OVERSIZE-GAP: {oversize} arquivo(s) pulados sem análise (--allow-oversize declarado)"
                ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for r, loc, sc, msg in findings:
                print(f"{r:<26} {loc}  [{sc}]  {msg}")
            if oversize_gap:
                print(f"{'OVERSIZE-GAP':<26} --allow-oversize  [coverage-gap]  "
                      f"{oversize} arquivo(s) pulados sem análise — lacuna de cobertura aceita explicitamente")
            # PT-13: supressão nunca é invisível — inventário na seção final.
            _seen_sup: set[tuple[str, str, int, str]] = set()
            for r, loc, ln, rs in suppressed_log:
                if (r, loc, ln, rs) in _seen_sup:
                    continue
                _seen_sup.add((r, loc, ln, rs))
                print(f"SUPPRESSED: {r} @ {loc}:{ln} ({rs})")
            print(f"{total_files} file(s) scanned, {len(findings)} finding(s), {oversize} oversize skipped")
        sys.stdout.flush()
    except BrokenPipeError:
        sys.exit(2)

    if oversize_blocked:
        print(
            f"ERRO: {oversize} arquivo(s) pulados sem análise (oversize > teto ou ilegível) — "
            "lacuna de cobertura NÃO declarada: segredo em arquivo grande sairia verde. "
            "Re-execute com --allow-oversize para aceitar a lacuna explicitamente.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(1 if findings else 0)


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
