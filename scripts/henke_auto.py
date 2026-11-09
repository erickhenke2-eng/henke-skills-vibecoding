#!/usr/bin/env python3
"""
henke_auto.py — A "máquina de uma frase" da Suíte Henke (PROTOCOLO v4.0).

Implementa as propostas M1+M2+M8 da auditoria `r1-maquina-2026-11-09`
(`03-gap-maquina.md` §3 e §4): transforma uma frase do usuário em execução
auditada, orquestrando — nunca reimplementando — as peças já mecânicas da suíte:

    resolve repo → route → garantias .henke/ → autorização por escopo (M8) →
    envelope com SKILL.md VERBATIM injetado (M2, sha256 como prova) →
    driver de agente (echo|stdin|cmd:<template>) → portões obrigatórios com
    auto-reparo limitado (MAX_REWORK=2) → testes do projeto via `henke exec` →
    fecho com outcome derivado por run_state + resumo PT-BR para não-técnicos.

Filosofia (auditoria §4):
  - DESPACHO, não reescrita: o SKILL.md entra byte a byte no envelope; quem
    evolui a regra continua sendo a skill.
  - Fail-closed: sem driver válido, o envelope é gravado, a instrução de uso é
    impressa e sai 2 — mesmo espírito do hard-stop Goldfish.
  - Autorização humana preservada: PUBLISH/destrutivo/metaconfig SEMPRE exigem
    `henke approve` humano. Nem `--yes` nem `policy=yolo` liberam — nunca.

Contratos externos (entregues por outros workers — importados com fallback):
  - `scripts/henke_detect.py`: `detect_project(root)->dict`,
    `write_project(root, data)->str`
  - `scripts/henke_gates.py`: `run_gates(repo_root, gates, run_id, record,
    timeout_sec)->{"verdicts":[...], "all_green":bool, "ran":int}` e
    `MAX_REWORK == 2`
  - `.henke/POLICY.json` schema exato:
    {"policy":"strict|balanced|yolo","always_ask":["publish","destructive",
    "metaconfig"],"auto_approve":["non-publish"],"allowed_cmds":[]}

API pública (consumida pelo cli-pack):
  - `run_auto(args: argparse.Namespace) -> int` — ns esperado:
    prompt(str), repo(str), driver(str|None), dry_run(bool), yes(bool),
    policy(str|None), timeout(float|None)
  - `main(argv: list[str] | None = None) -> int` — argparse próprio p/ uso direto

Códigos de saída:
  0  outcome derivado 'verified' (ou --dry-run concluído)
  1  outcome derivado 'failed' (portão vermelho, driver falhou, skill ilegível)
  2  bloqueio/erro de uso: autorização humana pendente, sem driver, driver
     inválido, resultado de driver malformado, repo inexistente, prompt vazio
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unicodedata
import uuid

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SUITE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SUITE_ROOT / "scripts"
CLI_PATH = SCRIPTS_DIR / "henke_cli.py"

if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

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
PUBLISH_VERBS = (
    "deploy", "publica", "publicar", "publico", "sobe", "subir", "coloca no ar",
    "producao", "production", "rollback", "reverte", "reversao",
)
DESTRUCTIVE_VERBS = (
    "deleta", "deletar", "delete", "exclui", "excluir", "apaga", "apagar",
    "drop", "wipe", "truncate", "destroy", "destroi", "destruir", "purge",
    "force-push", "push --force", "force push", "push -f", "rm -rf", "derruba",
)
METACONFIG_SIGNALS = (
    "package.json", "package-lock", "lockfile", "requirements", "pyproject",
    "workflow", "workflows", "ci.yml", "metaconfig", "build.rs", "dockerfile",
    "makefile", "lock.json",
)

# Skills cujo despacho implica escopo sensível (classificação do route, por slug).
SKILL_SCOPE_MAP = {
    "henke-ops-deploy": "publish",
    "henke-mem-faxina": "destructive",
    "henke-debug-reversao": "destructive",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(text: str) -> str:
    """Normalização idêntica à do henke_router (NFKD, minúsculas, sem acentos)."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _has_signal(text: str, sig: str) -> bool:
    """Word-boundary para sinais simples (mesma regra do henke_router)."""
    sig_clean = sig.strip()
    if not sig_clean:
        return False
    if re.fullmatch(r"[\w-]+", sig_clean):
        return bool(re.search(r"\b" + re.escape(sig_clean) + r"\b", text))
    prefix = r"\b" if sig_clean[0].isalnum() else ""
    suffix = r"\b" if sig_clean[-1].isalnum() else ""
    return bool(re.search(prefix + re.escape(sig_clean) + suffix, text))


def _atomic_write_json(path: Path, data: object) -> None:
    """Escrita atômica (.tmp + os.replace), padrão da suíte."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_path, path)


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _load_module(name: str):
    """Importa scripts/<name>.py como `scripts.<name>` ou `<name>`; None se ausente."""
    for modname in (f"scripts.{name}", name):
        try:
            return importlib.import_module(modname)
        except Exception:
            continue
    return None


# --- run_state (mesma suíte — import direto; henke_cli.py já faz o mesmo) ---
try:
    from scripts.run_state import (
        derive_run_outcome,
        init_run,
        record_gate,
        verify_run_file,
    )
except Exception:  # pragma: no cover - execução como script solto dentro de scripts/
    from run_state import (  # type: ignore
        derive_run_outcome,
        init_run,
        record_gate,
        verify_run_file,
    )


def _patch_run(run_path: Path, mutator) -> dict:
    """Carrega a run, aplica mutator(data), re-deriva outcome e grava atomicamente."""
    data = json.loads(Path(run_path).read_text(encoding="utf-8"))
    mutator(data)
    derived, reasons = derive_run_outcome(data)
    data["outcome"] = derived
    data["reasons"] = reasons
    data["updated_at"] = _now_iso()
    _atomic_write_json(run_path, data)
    return data


def _record_stage(run_path: Path, stage: str, detail: str, exit_code: int = 0) -> None:
    """Cada estágio do pipeline deixa rastro na run (gates{} + lista stages[])."""
    record_gate(run_path, f"stage_{stage}", exit_code, detail[:400])

    def _add(data: dict) -> None:
        stages = data.setdefault("stages", [])
        if not isinstance(stages, list):
            stages = []
            data["stages"] = stages
        stages.append({"stage": stage, "at": _now_iso(), "exit_code": exit_code, "detail": detail[:400]})

    _patch_run(run_path, _add)


# ---------------------------------------------------------------------------
# Estágio: route (subprocess da CLI → fallback import henke_router)
# ---------------------------------------------------------------------------

def _normalize_route(data: dict) -> dict:
    """Garante o schema mínimo do route e resolve `path` de cada skill.

    Aceita tanto a saída completa de `route_request` (skills com `path`)
    quanto a saída compacta da CLI (skills como lista de nomes).
    """
    route = {
        "mode": data.get("mode", "ANALISAR"),
        "archetype": data.get("archetype", "SHERLOCK"),
        "confidence": data.get("confidence", "none"),
        "requires_human_auth": bool(data.get("requires_human_auth", False)),
        "skills": [],
        "gates_required": list(data.get("gates_required") or []),
        "scope_paths": list(data.get("scope_paths") or []),
    }
    for item in data.get("skills") or []:
        if isinstance(item, str):
            name = item
            entry = {"name": name}
        elif isinstance(item, dict):
            entry = dict(item)
            name = str(entry.get("name", "")).strip()
        else:
            continue
        if not name:
            continue
        entry["name"] = name
        if not entry.get("path"):
            entry["path"] = str((SUITE_ROOT / name / "SKILL.md")).replace("\\", "/")
        route["skills"].append(entry)
    return route


def _route(prompt: str, repo_root: Path) -> dict:
    """Roteia via `henke_cli.py route "<prompt>" --repo <root> --json` (subprocess).

    Fallback: import direto de `henke_router.route_request` (função limpa que
    devolve o JSON completo, incluindo `skills[].path`).
    """
    if CLI_PATH.is_file():
        try:
            res = subprocess.run(
                [sys.executable, str(CLI_PATH), "route", prompt, "--repo", str(repo_root), "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(),
                timeout=60,
            )
            if res.returncode == 0 and (res.stdout or "").strip():
                data = json.loads(res.stdout)
                if isinstance(data, dict) and data.get("mode"):
                    return _normalize_route(data)
        except Exception:
            pass  # cai no fallback de import

    router = _load_module("henke_router")
    if router is None or not hasattr(router, "route_request"):
        raise RuntimeError("henke_router indisponível: nem subprocess da CLI nem import direto funcionaram")
    return _normalize_route(router.route_request(prompt, repo_root=repo_root, suite_root=SUITE_ROOT))


# ---------------------------------------------------------------------------
# Estágio: detect (henke_detect externo → fallback fingerprint mínimo)
# ---------------------------------------------------------------------------

def _detect_fallback(repo_root: Path) -> dict:
    """Detecção mínima honesta usada só quando `henke_detect` ainda não existe."""
    data: dict = {
        "linguagens": [],
        "framework": None,
        "test_cmd": None,
        "build_cmd": None,
        "lint_cmd": None,
        "detect_fallback": True,
    }
    if (repo_root / "package.json").is_file():
        data["linguagens"].append("javascript")
        data["test_cmd"] = "npm test"
        data["build_cmd"] = "npm run build"
    if (
        (repo_root / "pyproject.toml").is_file()
        or any(repo_root.glob("requirements*.txt"))
        or any(repo_root.glob("*.py"))
    ):
        data["linguagens"].append("python")
        if (repo_root / "tests").is_dir():
            data["test_cmd"] = data["test_cmd"] or "python -m unittest discover -s tests"
    if (repo_root / "Cargo.toml").is_file():
        data["linguagens"].append("rust")
        data["test_cmd"] = data["test_cmd"] or "cargo test"
    if (repo_root / "go.mod").is_file():
        data["linguagens"].append("go")
        data["test_cmd"] = data["test_cmd"] or "go test ./..."
    return data


def _detect_project(repo_root: Path) -> dict:
    mod = _load_module("henke_detect")
    if mod is not None and hasattr(mod, "detect_project"):
        try:
            data = mod.detect_project(repo_root)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return _detect_fallback(repo_root)


def _write_project(repo_root: Path, data: dict) -> str:
    mod = _load_module("henke_detect")
    if mod is not None and hasattr(mod, "write_project"):
        try:
            return str(mod.write_project(repo_root, data))
        except Exception:
            pass
    path = repo_root / ".henke" / "PROJECT.json"
    _atomic_write_json(path, data)
    return str(path)


# ---------------------------------------------------------------------------
# Estágio: política de autorização por escopo (M8)
# ---------------------------------------------------------------------------

def _load_policy(repo_root: Path) -> tuple[dict, str]:
    """Lê .henke/POLICY.json. Ausente → default. Malformada → default endurecido
    (policy=strict, auto_approve vazio): a política é superfície de segurança,
    então falha de parse nunca afrouxa."""
    policy_path = repo_root / ".henke" / "POLICY.json"
    if not policy_path.is_file():
        return dict(POLICY_DEFAULT), "default"
    try:
        doc = json.loads(policy_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("raiz não é objeto")
    except Exception:
        hardened = dict(POLICY_DEFAULT)
        hardened["policy"] = "strict"
        hardened["auto_approve"] = []
        return hardened, "malformed-hardened"
    merged = dict(POLICY_DEFAULT)
    merged.update({k: doc[k] for k in POLICY_DEFAULT.keys() if k in doc})
    return merged, "file"


def _effective_policy_name(flag_value: str | None, doc: dict) -> str | None:
    """Resolve o nome da política efetiva; None sinaliza valor inválido de flag."""
    if flag_value:
        return flag_value if flag_value in POLICY_VALID else None
    name = str(doc.get("policy") or "balanced")
    # valor inválido vindo de arquivo endurece para strict (nunca afrouxa)
    return name if name in POLICY_VALID else "strict"


def _classify_scopes(prompt_norm: str, route: dict) -> set[str]:
    """Classifica escopos sensíveis do pedido: classificação do route
    (requires_human_auth + skills despachadas) + heurística de verbos."""
    scopes: set[str] = set()
    if route.get("requires_human_auth"):
        scopes.add("publish")  # route marca PUBLISH+destrutivo no mesmo flag
    for sig in PUBLISH_VERBS:
        if _has_signal(prompt_norm, sig):
            scopes.add("publish")
            break
    for sig in DESTRUCTIVE_VERBS:
        if _has_signal(prompt_norm, sig):
            scopes.add("destructive")
            break
    for sig in METACONFIG_SIGNALS:
        if _has_signal(prompt_norm, sig):
            scopes.add("metaconfig")
            break
    for sk in route.get("skills") or []:
        mapped = SKILL_SCOPE_MAP.get(str(sk.get("name", "")))
        if mapped:
            scopes.add(mapped)
    return scopes


def _decide_authorization(
    scopes: set[str],
    policy_doc: dict,
    policy_name: str,
    mode: str,
    yes: bool,
) -> tuple[bool, str]:
    """Decide a autorização. Retorna (precisa_de_humano, motivo).

    Regras (M8):
      - escopo ∩ (HARD_ALWAYS_ASK ∪ always_ask da policy) → SEMPRE humano;
        nem --yes nem yolo liberam.
      - policy=strict em modo mutante (PLANEJAR/EXECUTAR) sem --yes → exige
        confirmação humana via re-execução com --yes.
      - demais escopos (auto_approve "non-publish") → flui.
    """
    always_ask = set(HARD_ALWAYS_ASK)
    doc_ask = policy_doc.get("always_ask")
    if isinstance(doc_ask, list):
        always_ask.update(str(s) for s in doc_ask)
    hit = sorted(scopes & always_ask)
    if hit:
        return True, f"escopo sensível: {', '.join(hit)}"
    if policy_name == "strict" and mode != "ANALISAR" and not yes:
        return True, "policy=strict exige confirmação: re-execute com --yes"
    return False, ""


# ---------------------------------------------------------------------------
# Estágio: envelope de execução (M2 — SKILL.md verbatim + sha256 de injeção)
# ---------------------------------------------------------------------------

def _build_envelope(
    prompt: str,
    route: dict,
    project: dict,
    policy_efetiva: dict,
    repo_root: Path,
    run_id: str,
    driver: str | None,
) -> tuple[dict | None, str]:
    """Monta o envelope. Retorna (envelope, erro) — erro != "" falha o despacho."""
    skills_payload: list[dict] = []
    for sk in route.get("skills") or []:
        name = str(sk.get("name", "")).strip()
        raw_path = str(sk.get("path", "")).strip()
        skill_path = Path(raw_path) if raw_path else (SUITE_ROOT / name / "SKILL.md")
        if not skill_path.is_file():
            return None, f"SKILL.md ilegível para a skill roteada '{name}': {skill_path}"
        try:
            raw_bytes = skill_path.read_bytes()
            content = raw_bytes.decode("utf-8")
        except Exception as exc:
            return None, f"falha ao ler SKILL.md de '{name}': {exc}"
        skills_payload.append({
            "name": name,
            "path": str(skill_path.resolve()).replace("\\", "/"),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),  # prova de injeção verbatim
            "content": content,
        })
    envelope = {
        "run_id": run_id,
        "repo_root": str(repo_root),
        "driver": driver,
        "generated_at": _now_iso(),
        "prompt": prompt,
        "route_json": route,
        "project": project,
        "policy_efetiva": policy_efetiva,
        "skills": skills_payload,
        "tools": list(ENVELOPE_TOOLS),
    }
    # Memória do projeto injetada verbatim (BambooAI-style): driver com contexto
    # das decisões/diário recentes — sem isso cada run nasce amnésica.
    memory: dict[str, str] = {}
    for key, fname in (("diario", "DIARIO.md"), ("decisoes", "DECISOES.md")):
        try:
            f = repo_root / ".henke" / fname
            if f.is_file():
                memory[key] = f.read_text(encoding="utf-8", errors="replace")[-6000:]
        except OSError:
            pass
    if memory:
        envelope["memory"] = memory
    return envelope, ""


# ---------------------------------------------------------------------------
# Estágio: drivers (echo | stdin | cmd:<template>)
# ---------------------------------------------------------------------------

def _validate_driver_result(obj: object) -> dict | None:
    """Valida o JSON de resultado do driver: {"files_changed": [...], "notes": str}."""
    if not isinstance(obj, dict):
        return None
    files_changed = obj.get("files_changed", [])
    notes = obj.get("notes", "")
    if not isinstance(files_changed, list) or not all(isinstance(f, str) for f in files_changed):
        return None
    if not isinstance(notes, str):
        return None
    return {"files_changed": files_changed, "notes": notes}


def _call_cmd_driver(template: str, envelope: dict, repo_root: Path, timeout_sec: float) -> tuple[dict | None, str]:
    """Invoca `cmd:<template>` com o envelope JSON no stdin; stdout deve ser o
    JSON de resultado do driver. Retorna (resultado_validado, erro)."""
    try:
        tokens = shlex.split(template, posix=(os.name != "nt"))
    except ValueError as exc:
        return None, f"template de driver inválido: {exc}"
    if not tokens:
        return None, "template de driver vazio após 'cmd:'"
    payload = json.dumps(envelope, ensure_ascii=False)
    try:
        res = subprocess.run(
            tokens,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            env=_child_env(),
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return None, f"driver expirou após {timeout_sec}s: '{template}'"
    except OSError as exc:
        return None, f"falha ao executar driver '{template}': {exc}"
    if res.returncode != 0:
        err_tail = (res.stderr or res.stdout or "").strip().splitlines()
        tail = err_tail[-1][:200] if err_tail else "sem saída"
        return None, f"driver '{template}' saiu com exit {res.returncode}: {tail}"
    try:
        obj = json.loads(res.stdout or "")
    except Exception:
        return None, "driver retornou stdout que não é JSON de resultado válido"
    validated = _validate_driver_result(obj)
    if validated is None:
        return None, "JSON de resultado do driver fora do schema {files_changed[], notes}"
    return validated, ""


def _dispatch_driver(
    driver: str | None,
    envelope: dict,
    envelope_path: Path,
    repo_root: Path,
    timeout_sec: float,
    findings: list[dict] | None = None,
) -> tuple[dict | None, str, int]:
    """Despacha o envelope ao driver. Retorna (resultado, erro, exit_code_sugerido).

    - echo  : modo teste/CI — envelope persistido, estágio 'dispatch' simulado.
    - stdin : envelope no stdout, JSON de resultado lido do stdin (schema validado).
    - cmd:X : subprocess do template com envelope no stdin (timeout ns ou 600s).
    - None  : fail-closed — envelope gravado + instrução de uso (exit 2).
    - outro : exit 2.
    """
    if driver is None:
        msg = (
            "NENHUM DRIVER CONFIGURADO — fail-closed.\n"
            f"Envelope de execução gravado em: {envelope_path}\n"
            "Para executar de verdade, rode:\n"
            "  henke auto \"<prompt>\" --driver echo        # simulado (teste/CI)\n"
            "  henke auto \"<prompt>\" --driver stdin       # lê resultado JSON do stdin\n"
            "  henke auto \"<prompt>\" --driver cmd:<template>  # driver real (claude/gemini/devin/...)"
        )
        return None, msg, 2

    if findings is not None:  # rework: alimenta findings de portões vermelhos ao driver
        envelope["rework"] = {
            "findings": findings,
            "instruction": "Corrija APENAS os achados listados e responda o mesmo JSON de resultado.",
        }
        _atomic_write_json(envelope_path, envelope)

    if driver == "echo":
        return {
            "files_changed": [],
            "notes": "driver echo (simulado): envelope persistido, nenhuma execução real",
        }, "", 0

    if driver == "stdin":
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        raw = sys.stdin.read()
        try:
            obj = json.loads(raw)
        except Exception:
            return None, "resultado do driver no stdin não é JSON válido", 2
        validated = _validate_driver_result(obj)
        if validated is None:
            return None, "resultado do driver no stdin fora do schema {files_changed[], notes}", 2
        return validated, "", 0

    if driver.startswith("cmd:"):
        result, err = _call_cmd_driver(driver[4:], envelope, repo_root, timeout_sec)
        if err:
            return None, err, 1  # driver executou e falhou: falha mecânica, não uso
        return result, "", 0

    if isinstance(driver, str) and driver.split(":", 1)[0] == "opencode":
        model = driver.split(":", 1)[1] if ":" in driver else "opencode/deepseek-v4.1-flash-free"
        adapter = SCRIPTS_DIR / "henke_driver_opencode.py"
        result, err = _call_cmd_driver(
            f'"{sys.executable}" "{adapter}" {model}', envelope, repo_root, timeout_sec
        )
        if err:
            return None, err, 1
        return result, "", 0

    return None, (
        f"driver desconhecido '{driver}'. Use echo | stdin | opencode[:model] | cmd:<template>."
    ), 2


# ---------------------------------------------------------------------------
# Estágio: portões e testes (seams injetáveis p/ testes determinísticos)
# ---------------------------------------------------------------------------

def _invoke_gates(repo_root: Path, gates: list[str], run_id: str, timeout_sec: float) -> dict | None:
    """Chama henke_gates.run_gates (contrato externo). None = runner indisponível."""
    mod = _load_module("henke_gates")
    if mod is None or not hasattr(mod, "run_gates"):
        return None
    return mod.run_gates(repo_root, list(gates), run_id=run_id, record=True, timeout_sec=timeout_sec)


def _gates_max_rework() -> int:
    mod = _load_module("henke_gates")
    if mod is not None and isinstance(getattr(mod, "MAX_REWORK", None), int):
        return mod.MAX_REWORK
    return MAX_REWORK


def _invoke_test_cmd(repo_root: Path, test_cmd: str, timeout_sec: float) -> tuple[int, str]:
    """Executa o test_cmd via `henke exec -- <cmd>` (subprocess da CLI) para que a
    evidência caia em exec_log.jsonl/LEDGER.json. Retorna (exit_code, saída)."""
    try:
        tokens = shlex.split(test_cmd, posix=(os.name != "nt"))
    except ValueError:
        tokens = [test_cmd]
    if not tokens:
        return 2, "test_cmd vazio"
    try:
        res = subprocess.run(
            [sys.executable, str(CLI_PATH), "exec", "--repo", str(repo_root), "--", *tokens],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(repo_root),
            env=_child_env(),
            timeout=timeout_sec,
        )
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except subprocess.TimeoutExpired:
        return 2, f"test_cmd expirou após {timeout_sec}s"
    except OSError as exc:
        return 2, f"falha ao executar test_cmd: {exc}"


def _red_verdicts(gres: dict | None) -> list[dict]:
    """Extrai os vereditos vermelhos para alimentar o rework do driver."""
    if not isinstance(gres, dict):
        return []
    findings = []
    for v in gres.get("verdicts") or []:
        if not isinstance(v, dict):
            continue
        if v.get("skipped"):
            continue
        ec = v.get("exit_code", v.get("exit", 1))
        passed = v.get("passed", ec == 0)
        if passed is True or ec == 0:
            continue
        findings.append({
            "gate": v.get("gate", v.get("name", "?")),
            "exit_code": ec,
            "output": str(v.get("stderr_tail") or v.get("output") or v.get("summary") or "")[:2000],
        })
    return findings


# ---------------------------------------------------------------------------
# Relatório final PT-BR (M6-light: deriva tudo do run — zero campos inventados)
# ---------------------------------------------------------------------------

def _print_report(
    run_path: Path,
    run_data: dict,
    prompt: str,
    route: dict,
    driver_result: dict | None,
    driver: str | None,
    gates_info: dict | None,
    test_info: dict | None,
    not_verified: list[str],
) -> None:
    outcome = run_data.get("outcome", "?")
    skills_run = run_data.get("skills_rodadas") or []
    files_changed = (driver_result or {}).get("files_changed") or run_data.get("files_changed") or []

    print("=== Henke Auto — Resumo ===")
    print(f"Pedido: \"{prompt}\"")
    print(
        f"Modo: {route.get('mode')} · Arquétipo: {route.get('archetype')} · "
        f"Confiança: {route.get('confidence')}"
    )
    if skills_run:
        names = ", ".join(f"{s['name']} (sha256:{s['sha256'][:8]}…)" for s in skills_run)
        print(f"Skills despachadas (verbatim): {names}")
    else:
        print("Skills despachadas: nenhuma (route confidence=none ou escopo fora do catálogo)")
    print(f"Driver: {driver or 'nenhum'}")
    print(f"Run: {run_path}")

    print("\nO que mudou:")
    if files_changed:
        for f in files_changed:
            print(f"  - {f}")
    else:
        print("  (nada registrado pelo driver)")

    print("\nVerificado:")
    gates = run_data.get("gates") or {}
    ran_gates = {k: v for k, v in gates.items() if not k.startswith("stage_")}
    if ran_gates:
        for g_name, g_info in ran_gates.items():
            ec = g_info.get("exit_code")
            badge = "PASS" if ec == 0 else "FAIL"
            print(f"  [{badge}] {g_name} (exit {ec})")
    elif gates_info is not None:
        print("  (portões executados — ver run para detalhes)")
    else:
        print("  (nenhum portão executado)")
    if test_info:
        print(f"  test_cmd `{test_info['cmd']}` → exit {test_info['exit']} ({test_info.get('detail', '')})")

    print("\nO que NÃO foi verificado:")
    for item in not_verified:
        print(f"  - {item}")

    print(f"\nOutcome derivado: {outcome}")
    if outcome == "verified":
        print("Próximo passo: revise o diff da run e feche o ciclo — commit é ato humano (custódia).")
    elif outcome == "failed":
        print("Próximo passo: corrija os achados acima e rode `henke auto` de novo.")
    else:
        print(f"Próximo passo: henke approve {run_path}  (ou revise os motivos no arquivo da run)")


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

def run_auto(args: argparse.Namespace) -> int:
    """Executa a máquina de uma frase. Recebe o namespace do cli-pack
    (prompt, repo, driver, dry_run, yes, policy, timeout)."""
    prompt = str(getattr(args, "prompt", None) or "").strip()
    if not prompt:
        print("ERRO: prompt vazio. Uso: henke auto \"<frase>\"", file=sys.stderr)
        return 2
    repo_root = Path(getattr(args, "repo", ".") or ".").resolve()
    if not repo_root.is_dir():
        print(f"ERRO: diretório --repo inexistente: {repo_root}", file=sys.stderr)
        return 2
    driver = getattr(args, "driver", None)
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    policy_flag = getattr(args, "policy", None)
    timeout_opt = getattr(args, "timeout", None)
    if timeout_opt is not None and float(timeout_opt) <= 0:
        print("ERRO: --timeout deve ser > 0.", file=sys.stderr)
        return 2

    # ---- estágio: route + detect (comum ao dry-run e ao fluxo real) ----
    try:
        route = _route(prompt, repo_root)
    except Exception as exc:
        print(f"ERRO no estágio route: {exc}", file=sys.stderr)
        return 2
    project = _detect_project(repo_root)
    policy_doc, policy_source = _load_policy(repo_root)
    policy_name = _effective_policy_name(policy_flag, policy_doc)
    if policy_name is None:
        print(f"ERRO: --policy inválida '{policy_flag}'. Válidas: {', '.join(POLICY_VALID)}", file=sys.stderr)
        return 2
    policy_efetiva = {
        "policy": policy_name,
        "always_ask": list(policy_doc.get("always_ask") or []),
        "auto_approve": list(policy_doc.get("auto_approve") or []),
        "allowed_cmds": list(policy_doc.get("allowed_cmds") or []),
        "source": policy_source,
        "flag": policy_flag,
    }

    # ---- estágio 1: --dry-run → plano sem tocar em NADA ----
    if dry_run:
        plan = {
            "dry_run": True,
            "mode": route.get("mode"),
            "archetype": route.get("archetype"),
            "confidence": route.get("confidence"),
            "requires_human_auth": route.get("requires_human_auth"),
            "skills": [s.get("name") for s in route.get("skills") or []],
            "gates_required": route.get("gates_required"),
            "test_cmd": project.get("test_cmd"),
            "policy": policy_name,
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    # ---- resume: retoma uma run bloqueada APÓS `henke approve` ----
    # O escopo executado é o da run aprovada (route_snapshot), nunca re-roteado —
    # a aprovação fica vinculada àquela run (C-fix banca: approve não era caminho).
    resume_ref = getattr(args, "resume", None)
    resumed: dict | None = None
    if resume_ref:
        runs_dir = (repo_root / ".henke" / "runs").resolve()
        cand = Path(str(resume_ref))
        cand = cand if cand.is_absolute() else runs_dir / (
            cand.name if cand.name.endswith(".json") else f"{cand.name}.json"
        )
        try:
            cand = cand.resolve()
            cand.relative_to(runs_dir)
        except (ValueError, OSError):
            print("ERRO: --resume aceita apenas runs dentro de .henke/runs/.", file=sys.stderr)
            return 2
        if not cand.is_file():
            print(f"ERRO: run inexistente: {cand}", file=sys.stderr)
            return 2
        try:
            resumed = json.loads(cand.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print("ERRO: run ilegível/corrompida.", file=sys.stderr)
            return 2
        if resumed.get("review_status") != "approved":
            print("ERRO: run não aprovada. Rode primeiro:", file=sys.stderr)
            print(f"  henke approve {cand}", file=sys.stderr)
            return 2
        route = resumed.get("route_snapshot") or route
        prompt = str(resumed.get("prompt") or prompt)
        driver = driver or resumed.get("driver")
        print(f"Retomando run aprovada: {cand.name}")

    # ---- estágio 2: garantias .henke/ + run ----
    henke_dir = repo_root / ".henke"
    (henke_dir / "runs").mkdir(parents=True, exist_ok=True)
    project_path = henke_dir / "PROJECT.json"
    if not project_path.is_file():
        _write_project(repo_root, project)
        try:
            project = json.loads(project_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    policy_path = henke_dir / "POLICY.json"
    if not policy_path.is_file():
        _atomic_write_json(policy_path, POLICY_DEFAULT)

    # ---- estágio 3: autorização por escopo (M8) — ANTES de qualquer despacho ----
    prompt_norm = _norm(prompt)
    scopes = _classify_scopes(prompt_norm, route)
    needs_human, auth_reason = _decide_authorization(
        scopes, policy_doc, policy_name, str(route.get("mode", "")), yes
    )
    sensitive_hit = bool(scopes & (set(HARD_ALWAYS_ASK) | set(policy_doc.get("always_ask") or [])))

    if resumed is not None:
        run_id = cand.stem
        run_path = cand
        _patch_run(run_path, lambda d: d.update({
            "mandatory_gates": list(route.get("gates_required") or []),
            "driver": driver,
        }))
        _record_stage(run_path, "authorize", "RETOMADA pós-approve humana — escopo vinculado à run aprovada")
        needs_human, sensitive_hit = False, False
    else:
        run_id = f"auto-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        run_path = init_run(
            repo_root,
            run_id,
            mode=str(route.get("mode", "ANALISAR")),
            archetype=str(route.get("archetype", "SHERLOCK")),
            spec_path=None,
            requires_human_auth=sensitive_hit,
            # run bloqueada por auth não declara mandatory_gates (nunca rodarão)
            mandatory_gates=None if sensitive_hit else list(route.get("gates_required") or []),
        )
    _patch_run(run_path, lambda d: d.update({
        "prompt": prompt,
        "scope_paths": list(route.get("scope_paths") or []),
        "policy_efetiva": policy_efetiva,
        "driver": driver,
        "rework_attempts": 0,
        # snapshot do route p/ retomada futura (--resume) — escopo vinculado à run
        "route_snapshot": {
            "mode": route.get("mode"),
            "archetype": route.get("archetype"),
            "confidence": route.get("confidence"),
            "skills": route.get("skills"),
            "gates_required": route.get("gates_required"),
            "scope_paths": route.get("scope_paths"),
            "requires_human_auth": route.get("requires_human_auth"),
        },
    }))
    _record_stage(
        run_path, "route",
        f"mode={route.get('mode')} archetype={route.get('archetype')} "
        f"confidence={route.get('confidence')} skills={len(route.get('skills') or [])}",
    )

    if needs_human:
        _record_stage(run_path, "authorize", f"BLOQUEADO: {auth_reason}", exit_code=0)
        if sensitive_hit:
            # Trava PUBLISH/destrutivo/metaconfig: SEM exceção para --yes/yolo.
            print(f"AUTORIZAÇÃO HUMANA OBRIGATÓRIA ({auth_reason}).")
            print("Nem --yes nem policy=yolo liberam este escopo (M8).")
            print(f"Para autorizar: henke approve {run_path}")
        else:
            print(f"CONFIRMAÇÃO NECESSÁRIA ({auth_reason}).")
            print("Re-execute com --yes para confirmar o escopo comum.")
        print(f"Run registrada: {run_path}")
        return 2
    _record_stage(run_path, "authorize", f"escopo comum autorizado (policy={policy_name}, yes={yes})")

    # ---- estágio 4: envelope com SKILL.md verbatim (M2) ----
    envelope, env_err = _build_envelope(
        prompt, route, project, policy_efetiva, repo_root, run_id, driver
    )
    if env_err:
        _record_stage(run_path, "dispatch", f"ERRO: {env_err}", exit_code=2)
        print(f"ERRO no despacho: {env_err}", file=sys.stderr)
        return 1
    envelope_path = henke_dir / "runs" / f"{run_id}.envelope.json"
    _atomic_write_json(envelope_path, envelope)
    _record_stage(
        run_path, "envelope",
        f"envelope gravado em {envelope_path.name} com {len(envelope['skills'])} skill(s)",
    )
    # Prova de injeção verbatim na run (aceitação M2): skills_rodadas = sha256 do disco
    _patch_run(run_path, lambda d: d.update({
        "skills_rodadas": [
            {"name": s["name"], "path": s["path"], "sha256": s["sha256"]}
            for s in envelope["skills"]
        ],
        "envelope": str(envelope_path),
    }))
    record_gate(
        run_path, "dispatch", 0,
        f"{len(envelope['skills'])} skill(s) injetada(s) verbatim (sha256 conferido com disco)",
    )

    # ---- estágio 5: driver ----
    driver_timeout = float(timeout_opt) if timeout_opt else 600.0
    driver_result, drv_err, drv_exit = _dispatch_driver(
        driver, envelope, envelope_path, repo_root, driver_timeout
    )
    if drv_err:
        _record_stage(run_path, "dispatch", f"ERRO: {drv_err}", exit_code=drv_exit or 2)
        print(drv_err, file=sys.stderr)
        return drv_exit or 2
    _patch_run(run_path, lambda d: d.update({
        "driver_result": driver_result,
        "files_changed": list(driver_result.get("files_changed") or []),
    }))
    if driver == "echo":
        _record_stage(run_path, "dispatch", "driver=echo (simulated) — modo teste/CI")
    else:
        _record_stage(
            run_path, "dispatch",
            f"driver={driver} → {len(driver_result.get('files_changed') or [])} arquivo(s); "
            f"notes: {(driver_result.get('notes') or '')[:200]}",
        )

    # ---- estágio 6: portões obrigatórios + auto-reparo (MAX_REWORK) ----
    gates_required = list(route.get("gates_required") or [])
    gates_timeout = float(timeout_opt) if timeout_opt else 300.0
    gres: dict | None = None
    rework_attempts = 0
    real_driver = isinstance(driver, str) and driver.startswith(("cmd:", "opencode"))

    if not gates_required:
        _record_stage(run_path, "gates", "nenhum portão obrigatório declarado pelo route")
    else:
        while True:
            gres = _invoke_gates(repo_root, gates_required, run_id, gates_timeout)
            if gres is None:
                # Runner externo indisponível: registra cada portão como não-veredito
                # (exit 2) — fail-closed, nunca verde presumido.
                for g in gates_required:
                    record_gate(run_path, Path(g).stem, 2, "AUTO-GATES-UNAVAILABLE: henke_gates indisponível")
                _record_stage(run_path, "gates", "henke_gates indisponível — portões registrados como não-veredito", exit_code=2)
                break
            _record_stage(
                run_path, "gates",
                f"portões executados: {gres.get('ran', '?')} · all_green={gres.get('all_green')}",
            )
            if gres.get("all_green"):
                break
            findings = _red_verdicts(gres)
            if real_driver and rework_attempts < _gates_max_rework():
                rework_attempts += 1
                driver_result2, drv_err2, _ = _dispatch_driver(
                    driver, envelope, envelope_path, repo_root,
                    driver_timeout, findings=findings,
                )
                if drv_err2:
                    _record_stage(run_path, "rework", f"rework #{rework_attempts} falhou: {drv_err2}", exit_code=1)
                    break
                driver_result = driver_result2 or driver_result
                _patch_run(run_path, lambda d: d.update({
                    "driver_result": driver_result,
                    "files_changed": list(driver_result.get("files_changed") or []),
                }))
                _record_stage(run_path, "rework", f"rework #{rework_attempts}: {len(findings)} achado(s) devolvidos ao driver")
                continue
            _record_stage(
                run_path, "rework",
                ("sem auto-reparo: " + (
                    f"driver '{driver}' não executa rework" if not real_driver
                    else f"limite MAX_REWORK={_gates_max_rework()} atingido"
                )) + f" · {len(findings)} achado(s) vermelho(s) reportados",
            )
            break
    _patch_run(run_path, lambda d: d.update({"rework_attempts": rework_attempts}))

    # ---- estágio 7: testes do projeto via `henke exec` ----
    test_info: dict | None = None
    test_cmd = (project or {}).get("test_cmd")
    if test_cmd:
        test_timeout = float(timeout_opt or (project or {}).get("timeout_sec") or 180.0)
        t_exit, t_out = _invoke_test_cmd(repo_root, str(test_cmd), test_timeout)
        discovered = passed = 0
        try:
            cli_mod = _load_module("henke_cli")
            if cli_mod is not None and hasattr(cli_mod, "parse_test_runner_output"):
                discovered, passed = cli_mod.parse_test_runner_output(t_out, "")
        except Exception:
            pass
        detail = f"{passed}/{discovered} testes" if discovered else "saída registrada no exec_log"
        record_gate(run_path, "tests", int(t_exit), f"test_cmd=`{test_cmd}` → {detail}")
        _record_stage(run_path, "tests", f"test_cmd=`{test_cmd}` exit={t_exit} {detail}", exit_code=0 if t_exit == 0 else 1)
        test_info = {"cmd": test_cmd, "exit": t_exit, "detail": detail}

    # ---- estágio 8: fecho — outcome derivado + resumo PT-BR ----
    not_verified: list[str] = []
    if driver == "echo":
        not_verified.append("execução real das skills (driver echo é simulado — nenhum arquivo alterado)")
    if driver == "stdin":
        not_verified.append("conferência humana do resultado lido do stdin")
    if gres is None and gates_required:
        not_verified.append("portões obrigatórios não executados (henke_gates indisponível)")
    elif gres is not None and not gres.get("all_green"):
        for f in _red_verdicts(gres):
            not_verified.append(f"portão vermelho: {f['gate']} (exit {f['exit_code']})")
    if not test_cmd:
        not_verified.append("testes do projeto (test_cmd ausente em .henke/PROJECT.json)")
    if sensitive_hit:
        not_verified.append("ação sensível não executada — aguardando aprovação humana")
    not_verified.append("revisão Goldfish/contexto limpo e commit final (atos humanos — PROTOCOLO §9)")

    ok, _reasons = verify_run_file(run_path)
    run_data = json.loads(run_path.read_text(encoding="utf-8"))
    outcome = run_data.get("outcome", "blocked")
    _print_report(
        run_path, run_data, prompt, route, driver_result, driver,
        gres, test_info, not_verified,
    )
    if outcome == "verified" and ok:
        return 0
    if outcome == "failed":
        return 1
    return 2  # blocked / unverified


def main(argv: list[str] | None = None) -> int:
    """Argparse próprio para uso direto: `python scripts/henke_auto.py "<frase>" ...`."""
    ap = argparse.ArgumentParser(
        prog="henke auto",
        description="A máquina de uma frase: route → autorização → despacho de skills → portões → resumo.",
    )
    ap.add_argument("prompt_pos", nargs="?", default=None, help="Frase do usuário")
    ap.add_argument("--prompt", dest="prompt_opt", default=None, help="Frase do usuário (alternativo)")
    ap.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    ap.add_argument("--driver", default=None, help="Driver de agente: echo | stdin | cmd:<template>")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true", help="Imprime o plano sem escrever nada")
    ap.add_argument("--yes", action="store_true", help="Confirma escopos comuns (NUNCA libera publish/destrutivo)")
    ap.add_argument("--policy", choices=POLICY_VALID, default=None, help="Sobrepõe a policy da POLICY.json")
    ap.add_argument("--timeout", type=float, default=None, help="Timeout (s) do driver/portões/testes")
    ap.add_argument("--resume", default=None, metavar="RUN", help="Retoma run bloqueada após `henke approve` (id ou caminho em .henke/runs)")
    try:
        ns = ap.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    ns.prompt = ns.prompt_opt if ns.prompt_opt is not None else ns.prompt_pos
    return run_auto(ns)


if __name__ == "__main__":
    sys.exit(main())
