#!/usr/bin/env python3
"""
henke_auto.py — A "máquina de uma frase" da Suíte Henke (PROTOCOLO v5.0).

Implementa as propostas M1+M2+M8 da auditoria `r1-maquina-2026-11-09`
(`03-gap-maquina.md` §3 e §4): transforma uma frase do usuário em execução
auditada, orquestrando — nunca reimplementando — as peças já mecânicas da suíte:

    resolve repo → route → garantias .henke/ → autorização por escopo (M8) →
    envelope com SKILL.md VERBATIM injetado (M2, sha256 como prova) →
    driver de agente (echo|stdin|cmd:<template>) → portões obrigatórios com
    auto-reparo limitado (MAX_REWORK=2) → testes do projeto via `henke exec` →
    fecho com outcome derivado por run_state + resumo PT-BR para não-técnicos.

Desmembrado por responsabilidade em:
  - `auto_kernel.py`  : constantes, sinais de segurança, env sanitizado, redação e helpers
  - `auto_policy.py`  : roteamento, detecção de projeto e política de autorização (M8)
  - `auto_driver.py`  : envelope (SKILL.md verbatim + sha256) e supervisão de drivers
  - `auto_custody.py` : confinamento .henke/, retomada --resume e nascimento/vínculo da run
  - `auto_audit.py`   : auditoria forense files_changed↔diff pós-driver e pós-rework
  - `auto_gates.py`   : portões, rework loop (MAX_REWORK), estágio de testes e relatório PT-BR

Códigos de saída:
  0  outcome derivado 'verified' (ou --dry-run concluído)
  1  outcome derivado 'failed' (portão vermelho, driver falhou, skill ilegível)
  2  bloqueio/erro de uso: autorização humana pendente, sem driver, driver
     inválido, resultado de driver malformado, repo inexistente, prompt vazio,
     portão obrigatório inviável (pré-despacho), --resume divergente ou
     outcome 'unverified'/'blocked' (driver echo é simulação: nunca 'verified')
"""
from __future__ import annotations

import argparse
import importlib.util as _ilu_auto
import json
import math
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# Ancora submódulos auto_* ao diretório físico da suíte (à prova de PYTHONPATH/standalone).
for _sib in ("auto_kernel", "auto_policy", "auto_driver", "auto_custody", "auto_audit", "auto_gates"):
    _full = f"henke.{_sib}"
    _target = _HERE / f"{_sib}.py"
    _cur = sys.modules.get(_full)
    if _cur is None or Path(getattr(_cur, "__file__", "")).resolve() != _target:
        _sp = _ilu_auto.spec_from_file_location(_full, str(_target))
        if _sp is not None and _sp.loader is not None:
            _mod = _ilu_auto.module_from_spec(_sp)
            sys.modules[_full] = _mod
            _sp.loader.exec_module(_mod)

from henke.auto_audit import (  # noqa: E402
    _audit_driver_changes,
    _audit_rework_changes,
)
from henke.auto_custody import (  # noqa: E402
    _check_henke_confinement,
    _init_or_bind_run,
    _validate_resume,
)
from henke.auto_driver import (  # noqa: E402
    _build_envelope,
    _call_cmd_driver,
    _dispatch_driver,
    _kill_process_tree,
    _popen_tree_kwargs,
    _run_capturing_tree,
    _run_driver_argv,
    _validate_driver_result,
)
from henke.auto_gates import (  # noqa: E402
    _discover_spec,
    _evaluate_test_cmd_policy,
    _execute_gates_with_rework,
    _execute_project_tests,
    _finalize_auto_run,
    _gates_max_rework,
    _invoke_gates,
    _invoke_test_cmd,
    _preflight_gates,
    _print_report,
    _red_verdicts,
)
from henke.auto_kernel import (  # noqa: E402
    CLI_PATH,
    DESTRUCTIVE_SIGNALS,
    DESTRUCTIVE_VERBS,
    ENVELOPE_TOOLS,
    HARD_ALWAYS_ASK,
    MAX_REWORK,
    METACONFIG_SIGNALS,
    POLICY_DEFAULT,
    POLICY_VALID,
    PUBLISH_ONLY_SIGNALS,
    PUBLISH_VERBS,
    SCRIPTS_DIR,
    SKILL_SCOPE_MAP,
    SUITE_ROOT,
    _SECRET_REDACT_RES,
    _approval_extra,
    _approval_fingerprint,
    _atomic_write_json,
    _child_env,
    _file_lock,
    _git_head,
    _has_signal,
    _load_module,
    _norm,
    _now_iso,
    _patch_run,
    _record_stage,
    _redact_secrets,
    _tracked_dirty_snapshot,
    derive_run_outcome,
    init_run,
    record_gate,
    verify_approval_token,
    verify_run_file,
    write_anchor_sidecar,
)
from henke.auto_policy import (  # noqa: E402
    _classify_scopes,
    _decide_authorization,
    _detect_fallback,
    _detect_project,
    _effective_policy_name,
    _load_policy,
    _load_project_config,
    _normalize_route,
    _resolve_policy_efetiva,
    _route,
    _write_project,
)


def run_auto(args: argparse.Namespace) -> int:
    """Single-writer por worktree — vale tanto para `henke auto` (cli-pack, que
    antes travava por fora) quanto para `python henke/henke_auto.py` direto,
    que não tinha lock nenhum (ALTO-1 da banca)."""
    repo_root = Path(getattr(args, "repo", ".") or ".").resolve()
    lock_target = repo_root / ".henke" / "auto-writer"
    try:
        with _file_lock(lock_target, timeout=2.0):
            return _run_auto_inner(args)
    except TimeoutError:
        print(
            "ERRO: outra run `auto`/`trigger` ativa nesta worktree (single-writer). "
            "Se a run anterior morreu, remova '.henke/auto-writer.lock'.",
            file=sys.stderr,
        )
        return 2


def _run_auto_inner(args: argparse.Namespace) -> int:
    """Orquestra os 8 estágios da máquina de uma frase."""
    prompt = str(getattr(args, "prompt", None) or "").strip()
    if not prompt:
        print("ERRO: prompt vazio. Uso: henke auto \"<frase>\"", file=sys.stderr)
        return 2
    prompt, n_redacted = _redact_secrets(prompt)
    if n_redacted:
        print(f"AVISO: {n_redacted} possível(is) segredo(s) redigido(s) do prompt antes de persistir/despachar.", file=sys.stderr)
    repo_root = Path(getattr(args, "repo", ".") or ".").resolve()
    if not repo_root.is_dir():
        print(f"ERRO: diretório --repo inexistente: {repo_root}", file=sys.stderr)
        return 2
    driver = getattr(args, "driver", None)
    dry_run = bool(getattr(args, "dry_run", False))
    yes = bool(getattr(args, "yes", False))
    policy_flag = getattr(args, "policy", None)
    timeout_opt = getattr(args, "timeout", None)
    if timeout_opt is not None and (
            not math.isfinite(float(timeout_opt)) or float(timeout_opt) <= 0):
        print("ERRO: --timeout deve ser um número finito > 0.", file=sys.stderr)
        return 2

    # ---- estágio 0: route + detect + policy ----
    try:
        route = _route(prompt, repo_root)
    except Exception as exc:
        print(f"ERRO no estágio route: {exc}", file=sys.stderr)
        return 2
    project = _load_project_config(repo_root, detect_fn=_detect_project)
    policy_doc, policy_name, policy_efetiva = _resolve_policy_efetiva(repo_root, policy_flag)
    if policy_name is None:
        print(f"ERRO: --policy inválida '{policy_flag}'. Válidas: {', '.join(POLICY_VALID)}", file=sys.stderr)
        return 2

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
    resume_ref = getattr(args, "resume", None)
    cand: Path | None = None
    resumed: dict | None = None
    if resume_ref:
        cand, resumed, resume_err = _validate_resume(
            repo_root, resume_ref, driver, project, policy_efetiva
        )
        if resume_err is not None or cand is None or resumed is None:
            return resume_err or 2
        route = resumed.get("route_snapshot") or route
        prompt = str(resumed.get("prompt") or prompt)
        driver = driver or resumed.get("driver")
        print(f"Retomando run aprovada: {cand.name}")

    # ---- estágio 2: garantias de confinamento .henke/ ----
    conf_err = _check_henke_confinement(repo_root)
    if conf_err is not None:
        return conf_err
    henke_dir = repo_root / ".henke"

    # ---- estágio 3: autorização por escopo (M8) + nascimento/vínculo da run ----
    scopes = _classify_scopes(_norm(prompt), route)
    needs_human, auth_reason = _decide_authorization(
        scopes, policy_doc, policy_name, str(route.get("mode", "")), yes
    )
    sensitive_hit = bool(scopes & (set(HARD_ALWAYS_ASK) | set(policy_doc.get("always_ask") or [])))
    spec_path = _discover_spec(repo_root)
    run_id, run_path, bind_err = _init_or_bind_run(
        repo_root, prompt, route, project, policy_efetiva, driver,
        spec_path, sensitive_hit, resumed, cand,
    )
    if bind_err is not None:
        return bind_err
    if resumed is not None:
        needs_human, sensitive_hit = False, False

    _record_stage(
        run_path, "route",
        f"mode={route.get('mode')} archetype={route.get('archetype')} "
        f"confidence={route.get('confidence')} skills={len(route.get('skills') or [])}",
    )
    if needs_human:
        _record_stage(run_path, "authorize", f"BLOQUEADO: {auth_reason}", exit_code=0)
        if sensitive_hit:
            print(f"AUTORIZAÇÃO HUMANA OBRIGATÓRIA ({auth_reason}).")
            print("Nem --yes nem policy=yolo liberam este escopo (M8).")
            print(f"Para autorizar: henke approve {run_path}")
        else:
            print(f"CONFIRMAÇÃO NECESSÁRIA ({auth_reason}).")
            print("Re-execute com --yes para confirmar o escopo comum.")
        print(f"Run registrada: {run_path}")
        return 2
    _record_stage(run_path, "authorize", f"escopo comum autorizado (policy={policy_name}, yes={yes})")

    is_analisar = str(route.get("mode", "")).upper() == "ANALISAR"
    gates_required = list(route.get("gates_required") or [])
    try:
        driver_timeout = float(timeout_opt) if timeout_opt else 600.0
        if not math.isfinite(driver_timeout) or driver_timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        driver_timeout = 600.0
    driver_result: dict | None = None
    envelope: dict | None = None
    envelope_path: Path | None = None
    echo_dispatched = False
    real_driver = (
        not is_analisar
        and isinstance(driver, str)
        and driver.startswith(("cmd:", "opencode", "agent:"))
    )

    if is_analisar:
        _record_stage(
            run_path, "dispatch",
            f"modo ANALISAR — driver '{driver or 'nenhum'}' NÃO despachado; "
            "ação limitada aos portões/testes",
        )
    else:
        # ---- estágio 4: envelope com SKILL.md verbatim (M2) ----
        envelope, env_err = _build_envelope(
            prompt, route, project, policy_efetiva, repo_root, run_id, driver
        )
        if env_err or envelope is None:
            _record_stage(run_path, "dispatch", f"ERRO: {env_err}", exit_code=2)
            print(f"ERRO no despacho: {env_err}", file=sys.stderr)
            return 1
        envelope_path = henke_dir / "runs" / f"{run_id}.envelope.json"
        _atomic_write_json(envelope_path, envelope)
        _record_stage(
            run_path, "envelope",
            f"envelope gravado em {envelope_path.name} com {len(envelope['skills'])} skill(s)",
        )
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
            manual=True,
        )

        # ---- estágio 4b: PRE-FLIGHT de portões obrigatórios ----
        if gates_required:
            pf_ok, pf_problems = _preflight_gates(
                repo_root, gates_required, str(spec_path) if spec_path else None
            )
            if not pf_ok:
                detail = "; ".join(pf_problems)
                _record_stage(
                    run_path, "gates",
                    f"PRE-FLIGHT bloqueou o despacho (portões inviáveis): {detail}",
                )
                print(
                    "BLOQUEIO PRÉ-DESPACHO — portões obrigatórios inviáveis neste "
                    "repositório; nada foi despachado:",
                    file=sys.stderr,
                )
                for p in pf_problems:
                    print(f"  - {p}", file=sys.stderr)
                print(
                    "Forneça os insumos (spec, baseline, HEAD/commit) ou revise o "
                    f"route. Run registrada: {run_path}",
                    file=sys.stderr,
                )
                return 2
            _record_stage(
                run_path, "preflight",
                f"{len(gates_required)} portão(ões) obrigatório(s) viáveis",
            )

        # ---- estágio 5: driver + auditoria forense files_changed↔diff ----
        driver_result, drv_err, drv_exit = _dispatch_driver(
            driver, envelope, envelope_path, repo_root, driver_timeout
        )
        if drv_err or driver_result is None:
            _record_stage(run_path, "dispatch", f"ERRO: {drv_err}", exit_code=drv_exit or 2)
            print(drv_err, file=sys.stderr)
            return drv_exit or 2
        echo_dispatched = (driver == "echo")
        declared_changes = list(driver_result.get("files_changed") or [])
        _patch_run(run_path, lambda d: d.update({
            "driver_result": driver_result,
            "files_changed": declared_changes,
        }))
        if real_driver:
            _audit_driver_changes(repo_root, run_path, declared_changes)
        if driver == "echo":
            _record_stage(
                run_path, "dispatch",
                "driver=echo (simulated) — modo teste/CI; outcome teto 'unverified'",
            )
        else:
            _record_stage(
                run_path, "dispatch",
                f"driver={driver} → {len(driver_result.get('files_changed') or [])} arquivo(s); "
                f"notes: {(driver_result.get('notes') or '')[:200]}",
            )

    # ---- estágio 6: portões obrigatórios + auto-reparo (MAX_REWORK) ----
    test_cmd, _test_cmd_rejected, _gate_test_cmd = _evaluate_test_cmd_policy(
        project, policy_efetiva, is_analisar
    )
    gres, rework_attempts, driver_result = _execute_gates_with_rework(
        repo_root, run_path, run_id, gates_required, _gate_test_cmd, timeout_opt,
        driver, real_driver, envelope, envelope_path, driver_timeout, driver_result,
        invoke_gates_fn=_invoke_gates,
        dispatch_fn=_dispatch_driver,
        max_rework_fn=_gates_max_rework,
    )
    if rework_attempts and real_driver:
        _audit_rework_changes(repo_root, run_path, driver_result)

    # ---- estágio 7: testes do projeto via `henke exec` ----
    test_info: dict | None = None
    if test_cmd:
        test_info = _execute_project_tests(
            repo_root, run_path, run_id, str(test_cmd), _test_cmd_rejected,
            project, timeout_opt, invoke_test_fn=_invoke_test_cmd,
        )

    # ---- estágio 8: fecho — outcome derivado + resumo PT-BR ----
    return _finalize_auto_run(
        run_path, prompt, route, driver_result, driver, is_analisar,
        echo_dispatched, gres, gates_required, test_cmd, test_info, sensitive_hit,
    )


def main(argv: list[str] | None = None) -> int:
    """Argparse próprio para uso direto: `python henke/henke_auto.py "<frase>" ...`."""
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
    sys.exit(main())
