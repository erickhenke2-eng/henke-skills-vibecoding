"""auto_kernel.py — Constantes, vocabulário de segurança, env higienizado, redação e helpers de run."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = SCRIPTS_DIR.parent
CLI_PATH = SCRIPTS_DIR / "henke_cli.py"

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
# SCRIPTS_DIR NÃO entra no sys.path: com ele, `import test_impact` resolve
# henke/test_impact.py e colide com tests/test_impact.py no discover do CI.

from henke._paths import suite_root as _suite_root  # noqa: E402

try:
    from henke.security_signals import (  # noqa: F401
        _norm, _has_signal, METACONFIG_SIGNALS,
        DESTRUCTIVE_SIGNALS, PUBLISH_ONLY_SIGNALS,
    )
except ImportError:  # execução standalone (python henke/henke_auto.py)
    from security_signals import (  # type: ignore[no-redef]  # noqa: F401
        _norm, _has_signal, METACONFIG_SIGNALS,
        DESTRUCTIVE_SIGNALS, PUBLISH_ONLY_SIGNALS,
    )

SUITE_ROOT = _suite_root()  # checkout OU <prefix>/henke_data (wheel)

# --- Contrato M3: limite real de auto-reparo (PROTOCOLO.md — "no máximo 2 iterações") ---
MAX_REWORK = 2

# Catálogo de ferramentas mecânicas exposto ao worker dentro do envelope (M2).
ENVELOPE_TOOLS = ["read", "write", "list", "exec", "gate", "run_state"]

# Schema exato da POLICY.json (contrato externo — NÃO alterar campos/ordem semântica).
POLICY_DEFAULT: dict = {
    "policy": "balanced",
    "always_ask": ["publish", "destructive", "metaconfig"],
    "auto_approve": ["non-publish"],
    "allowed_cmds": [],
}
POLICY_VALID = ("strict", "balanced", "yolo")

# Piso duro de autorização (M8): estes escopos JAMAIS são liberados por
# --yes, policy=yolo ou edição da POLICY.json — sempre `henke approve` humano.
HARD_ALWAYS_ASK = frozenset({"publish", "destructive", "metaconfig"})

# Heurística lexical de escopo sensível (M8 §3): complementa a classificação do
# route (requires_human_auth) para o léxico real do vibecoder.
# P1-04: vocabulário destrutivo/PUBLISH/metaconfig vem da FONTE ÚNICA
# henke/security_signals.py (união router+auto — direção mais forte).
# Aliases mantidos p/ compat com corpus/testes existentes.
PUBLISH_VERBS = PUBLISH_ONLY_SIGNALS
DESTRUCTIVE_VERBS = DESTRUCTIVE_SIGNALS

# Skills cujo despacho implica escopo sensível (classificação do route, por slug).
SKILL_SCOPE_MAP = {
    "henke-ops-deploy": "publish",
    "henke-mem-faxina": "destructive",
    "henke-debug-reversao": "destructive",
}

# --- run_state (mesma suíte — import direto; henke_cli.py já faz o mesmo) ---
try:
    from henke.run_state import (
        _ANCHORED_FIELDS,
        _file_lock,
        amend_anchor_fields,
        derive_run_outcome,
        init_run,
        load_anchor_sidecar,
        record_gate,
        verify_approval_token,
        verify_run_file,
        write_anchor_sidecar,
    )
except Exception:  # pragma: no cover - execução como script solto dentro de henke/
    from run_state import (  # type: ignore
        _ANCHORED_FIELDS,
        _file_lock,
        amend_anchor_fields,
        derive_run_outcome,
        init_run,
        load_anchor_sidecar,
        record_gate,
        verify_approval_token,
        verify_run_file,
        write_anchor_sidecar,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: object) -> None:
    """Escrita atômica (.tmp + os.replace), padrão da suíte."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, path)


def _child_env(repo: Path | None = None) -> dict:
    # A2 (banca v11): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG_* do
    # ambiente redirecionam o git interno (âncora HEAD, status --ignored,
    # check-ignore) para outro repo/index — strip em todo filho.
    # A4 (banca v14): PYTHONPATH/loader hooks subvertem o tooling que
    # produz evidência — strip amplo igual ao do `henke exec`.
    # Delta-F5: delega para o helper canônico — a strip-list local ficou
    # mais fraca (NODE_*/NPM_CONFIG/PERL5*/DOTNET_*/JAVA_*/GOFLAGS/RUST*/
    # VIRTUAL_ENV/CLASSPATH etc. sobreviviam e mintavam evidência via
    # driver cmd:/agent:/test_cmd). O helper também cobre o filtro de
    # PATH ancorado no repo (A4 banca v16 / DS-12b).
    from henke.state_paths import sanitized_child_env
    env = sanitized_child_env(repo)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # F-07 (banca v8): CreateProcess/`shutil.which` procuram no CWD no
    # Windows — um `git.exe`/`npm.cmd` plantado no repo seria executado por
    # driver `cmd:`/agent:/test_cmd/git interno. Remove o CWD do path de busca.
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def _load_module(name: str):
    """Importa henke/<name>.py como `henke.<name>`; None se ausente."""
    try:
        return importlib.import_module(f"henke.{name}")
    except Exception:
        return None


def _patch_run(run_path: Path, mutator) -> dict:
    """Carrega a run, aplica mutator(data), re-deriva outcome e grava atomicamente.

    Coberto pelo mesmo _file_lock do run_state: sem ele, um `henke run approve`
    concorrente perde a aprovação num lost-update de read-modify-write."""
    with _file_lock(Path(run_path)):
        data = json.loads(Path(run_path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"run file não é objeto JSON: {run_path}")
        mutator(data)
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
    return data


def _record_stage(run_path: Path, stage: str, detail: str, exit_code: int = 0) -> None:
    """Cada estágio do pipeline deixa rastro na run (gates{} + lista stages[]).

    Stages são bookkeeping — manual=True para NUNCA contarem como portão físico
    (senão um run de só stages passaria no guarda anti-N/A por ter 'verde')."""
    record_gate(run_path, f"stage_{stage}", exit_code, detail[:400], manual=True)

    def _add(data: dict) -> None:
        stages = data.setdefault("stages", [])
        if not isinstance(stages, list):
            stages = []
            data["stages"] = stages
        stages.append({"stage": stage, "at": _now_iso(), "exit_code": exit_code, "detail": detail[:400]})

    _patch_run(run_path, _add)


def _git_head(repo_root: Path) -> str | None:
    """HEAD atual do repo (None se sem git/sem commits) — vínculo do --resume."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, env=_child_env(repo_root),
        )
        if res.returncode == 0:
            return (res.stdout or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _tracked_dirty_snapshot(repo_root: Path) -> dict[str, str]:
    """{relpath: sha256|DELETED|NONFILE|UNREADABLE} dos arquivos RASTREADOS
    sujos (DS-2 — aprovação não vinculava o worktree): aprovar, editar um
    arquivo tracked sem commit e dar --resume mantinha o mesmo HEAD e era
    aceito — execução sobre conteúdo diferente do aprovado, ou trabalho
    pré-existente do usuário creditado ao driver."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "status", "--porcelain", "-z", "--untracked-files=no"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_child_env(repo_root),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if res.returncode != 0 or not res.stdout:
        return {}
    snap: dict[str, str] = {}
    _VALID_XY = " MADRCUT?!"
    toks = res.stdout.split("\x00")
    i = 0
    while i < len(toks):
        ent = toks[i]
        i += 1
        if len(ent) < 4 or ent[2] != " " or any(
                c not in _VALID_XY for c in ent[:2]):
            continue
        xy = ent[:2]
        rel = ent[3:].strip()
        orig = None
        if ("R" in xy or "C" in xy) and i < len(toks):
            orig = toks[i].strip()
            i += 1
        for path_rel in (rel, orig):
            if not path_rel:
                continue
            p = Path(repo_root) / path_rel
            try:
                if p.is_file() and not p.is_symlink():
                    snap[path_rel] = hashlib.sha256(p.read_bytes()).hexdigest()
                elif not p.exists():
                    snap[path_rel] = "DELETED"
                else:
                    snap[path_rel] = "NONFILE"
            except OSError:
                snap[path_rel] = "UNREADABLE"
    return snap


# --- Redação de segredos (crouton R2): prompt/memória vão para disco em
# .henke/runs/*.json (rastreado) e para o stdin do driver (processo externo).
# Redigir ANTES de persistir/despachar protege os dois lados.
_SECRET_REDACT_RES = (
    re.compile(r"\bsk-(?:proj-|live-)?[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b"),
    re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"(https?://[A-Za-z0-9._%-]+):([^/\s'\"@]{3,})@"),
)


def _redact_secrets(text: str) -> tuple[str, int]:
    """Substitui padrões de credencial por <REDACTED-SECRET>; retorna (texto, n)."""
    count = 0

    def _bump(m):
        nonlocal count
        count += 1
        return "<REDACTED-SECRET>"

    def _bump_url(m):
        nonlocal count
        count += 1
        return f"{m.group(1)}:<REDACTED-SECRET>@"

    out = text
    for pat in _SECRET_REDACT_RES[:-1]:
        out = pat.sub(_bump, out)
    out = _SECRET_REDACT_RES[-1].sub(_bump_url, out)
    return out, count


def _approval_fingerprint(prompt: str, route_snapshot: dict | None,
                          driver: str | None = None,
                          extra: dict | None = None) -> str:
    """sha256 do escopo aprovado (prompt + route_snapshot + driver — trocar o
    driver entre approve/resume invalida a aprovação: outro driver = outro risco).
    `extra` ancora test_cmd+política efetiva (banca v9 — M3): editar
    PROJECT.json/POLICY.json entre approve e resume executaria outro comando
    sob o selo da aprovação."""
    payload = json.dumps(
        {"prompt": prompt, "route_snapshot": route_snapshot or {},
         "driver": driver or "", "extra": extra or {}},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _approval_extra(project: dict | None, policy_efetiva: dict | None) -> dict:
    """Campos de execução vinculados à aprovação: o comando de teste real e a
    política efetiva — não o arquivo (o usuário pode editar entre approve e
    resume; divergência = re-aprovação, que é o comportamento correto)."""
    return {
        "test_cmd": (project or {}).get("test_cmd"),
        "policy": policy_efetiva or {},
    }
