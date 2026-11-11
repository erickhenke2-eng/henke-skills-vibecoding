"""auto_policy.py — Roteamento, detecção de stack do projeto e política de autorização por escopo (M8)."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from henke.auto_kernel import (
    CLI_PATH,
    DESTRUCTIVE_VERBS,
    HARD_ALWAYS_ASK,
    METACONFIG_SIGNALS,
    POLICY_DEFAULT,
    POLICY_VALID,
    PUBLISH_VERBS,
    SKILL_SCOPE_MAP,
    SUITE_ROOT,
    _atomic_write_json,
    _child_env,
    _has_signal,
    _load_module,
)


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
                [sys.executable, "-Es", str(CLI_PATH), "route", prompt, "--repo", str(repo_root), "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_child_env(repo_root),
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


def _load_project_config(repo_root: Path, detect_fn=_detect_project) -> dict:
    """Detecta o projeto e aplica overrides explícitos de `.henke/PROJECT.json`."""
    project = detect_fn(repo_root)
    # .henke/PROJECT.json explícito do usuário SOBREPÕE a detecção — é a fonte
    # documentada do test_cmd no relatório ("test_cmd ausente em
    # .henke/PROJECT.json"). Malformado → detecção pura (fail-safe, como a
    # policy); campo vazio/None não apaga o detectado.
    _proj_file = repo_root / ".henke" / "PROJECT.json"
    if _proj_file.is_file():
        try:
            _pj = json.loads(_proj_file.read_text(encoding="utf-8"))
            if isinstance(_pj, dict):
                project.update({k: v for k, v in _pj.items()
                                if v is not None and v != ""})
        except Exception:
            pass
    return project


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


def _resolve_policy_efetiva(
    repo_root: Path, policy_flag: str | None
) -> tuple[dict, str | None, dict]:
    """Carrega POLICY.json e coage campos para forma segura (fail-safe)."""
    policy_doc, policy_source = _load_policy(repo_root)
    policy_name = _effective_policy_name(policy_flag, policy_doc)
    if policy_name is None:
        return policy_doc, None, {}
    # A9 (banca v11): campo-lista com tipo errado (`always_ask: 5`) fazia
    # list(5) explodir TypeError antes da classificação — coage: não-lista
    # vira lista-vazia (fail-safe: perde restrição, não ganha permissão).
    def _plist(v):
        return v if isinstance(v, list) else []

    # MÉDIO-4 (banca v13): allowed_cmds com tipo errado DESLIGAVA a allowlist
    # silenciosamente — str vira lista de um; outro tipo vira deny-all.
    _ac = policy_doc.get("allowed_cmds")
    if isinstance(_ac, str) and _ac.strip():
        _allowed_cmds = [_ac.strip()]
    elif isinstance(_ac, list):
        _allowed_cmds = _ac
    elif _ac is None:
        _allowed_cmds = []
    else:
        _allowed_cmds = ["__deny_all__"]
    policy_efetiva = {
        "policy": policy_name,
        "always_ask": _plist(policy_doc.get("always_ask")),
        "auto_approve": _plist(policy_doc.get("auto_approve")),
        "allowed_cmds": _allowed_cmds,
        "source": policy_source,
        "flag": policy_flag,
    }
    return policy_doc, policy_name, policy_efetiva


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
