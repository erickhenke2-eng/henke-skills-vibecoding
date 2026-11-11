"""auto_gates.py — Seams de portões, loop de rework (MAX_REWORK), estágio de testes e relatório PT-BR."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shlex
import sys

from henke.auto_driver import _run_capturing_tree
from henke.auto_kernel import (
    CLI_PATH,
    MAX_REWORK,
    _atomic_write_json,
    _file_lock,
    _load_module,
    _now_iso,
    _patch_run,
    _record_stage,
    derive_run_outcome,
    record_gate,
    verify_run_file,
)


def _invoke_gates(repo_root: Path, gates: list[str], run_id: str, timeout_sec: float,
                  test_cmd: str | None = None) -> dict | None:
    """Chama henke_gates.run_gates (contrato externo). None = runner indisponível
    ou falhou por completo — fail-closed, nunca traceback nem verde presumido."""
    mod = _load_module("henke_gates")
    if mod is None or not hasattr(mod, "run_gates"):
        return None
    try:
        return mod.run_gates(repo_root, list(gates), run_id=run_id, record=True,
                             timeout_sec=timeout_sec, test_cmd=test_cmd)
    except Exception:
        return None


def _gates_max_rework() -> int:
    mod = _load_module("henke_gates")
    if mod is not None and isinstance(getattr(mod, "MAX_REWORK", None), int):
        return mod.MAX_REWORK
    return MAX_REWORK


def _discover_spec(repo_root: Path) -> Path | None:
    """Spec ativa do projeto (docs/designs/*.md mais recente) — alimenta
    `init_run.spec_path` (lido por run_gates) e o preflight. Sem spec, os
    portões de contrato/drift são corretamente inviáveis → bloqueio fail-closed."""
    try:
        from henke.spec_drift import discover_latest_spec
    except ImportError:
        try:
            from spec_drift import discover_latest_spec
        except ImportError:
            return None
    try:
        return discover_latest_spec(repo_root)
    except Exception:
        return None


def _preflight_gates(
    repo_root: Path, gates: list[str], spec_path: str | None = None
) -> tuple[bool, list[str]]:
    """Valida ANTES do despacho se cada portão obrigatório é autoexecutável aqui.

    Reutiliza `henke_gates._resolve_gate` (mesma regra do runner): None =
    portão desconhecido/script ausente; (id, None) = conhecido mas sem
    args/insumos neste contexto (spec, baseline, HEAD, ledger...). Portão
    obrigatório inviável NUNCA vira verde — bloquear antes do driver editar é
    fail-closed. Sem henke_gates importável → inviável (mesmo critério).

    Retorna (todos_viáveis, problemas).
    """
    mod = _load_module("henke_gates")
    resolver = getattr(mod, "_resolve_gate", None) if mod is not None else None
    if resolver is None:
        return False, ["henke_gates indisponível — não há como executar os portões obrigatórios"]
    skip_reasons = getattr(mod, "_SKIP_REASONS", {}) or {}
    spec = Path(spec_path) if spec_path else None
    problems: list[str] = []
    for raw in gates:
        token = str(raw).strip()
        resolved = resolver(token, Path(repo_root), spec)
        if resolved is None:
            problems.append(f"portão desconhecido ou script ausente: '{token}'")
        elif resolved[1] is None:
            reason = skip_reasons.get(
                resolved[0], "sem args autoexecutáveis neste contexto (insumo ausente)"
            )
            problems.append(f"portão obrigatório '{resolved[0]}' não autoexecutável: {reason}")
    return (not problems), problems


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
        rc, out, err, timed_out = _run_capturing_tree(
            [sys.executable, "-Es", str(CLI_PATH), "exec", "--repo", str(repo_root), "--", *tokens],
            cwd=repo_root, timeout_sec=timeout_sec,
        )
    except OSError as exc:
        return 2, f"falha ao executar test_cmd: {exc}"
    if timed_out:
        return 2, f"[TIMEOUT] test_cmd expirou após {timeout_sec}s (árvore encerrada)"
    return int(rc or 0), out + err


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
        # exit 3 = n/a honesto ("nada a verificar") — não é achado a corrigir.
        if passed is True or ec in (0, 3):
            continue
        findings.append({
            "gate": v.get("gate", v.get("name", "?")),
            "exit_code": ec,
            "output": str(v.get("stderr_tail") or v.get("output") or v.get("summary") or "")[:2000],
        })
    return findings


def _evaluate_test_cmd_policy(
    project: dict | None, policy_efetiva: dict, is_analisar: bool
) -> tuple[str | None, bool, str | None]:
    """A1 (banca v11) + A3 (banca v16): valida allowlist de test_cmd ANTES dos portões."""
    test_cmd = (project or {}).get("test_cmd")
    _allowed = policy_efetiva.get("allowed_cmds") or []
    if isinstance(_allowed, str):
        _allowed = [_allowed]  # string solta não vira prefixo-por-caractere
    _allowed_set = {" ".join(str(p).split()) for p in _allowed if str(p).strip()}
    _norm_cmd = " ".join(str(test_cmd).split()) if test_cmd else None
    _rejected = bool(
        _norm_cmd and _norm_cmd not in _allowed_set
        and (_allowed_set or is_analisar))
    return test_cmd, _rejected, (None if _rejected else test_cmd)


def _execute_gates_with_rework(
    repo_root: Path,
    run_path: Path,
    run_id: str,
    gates_required: list[str],
    gate_test_cmd: str | None,
    timeout_opt: object,
    driver: str | None,
    real_driver: bool,
    envelope: dict | None,
    envelope_path: Path | None,
    driver_timeout: float,
    driver_result: dict | None,
    *,
    invoke_gates_fn=_invoke_gates,
    dispatch_fn=None,
    max_rework_fn=_gates_max_rework,
) -> tuple[dict | None, int, dict | None]:
    """Executa portões obrigatórios com até MAX_REWORK tentativas de auto-reparo."""
    gates_timeout = float(timeout_opt) if timeout_opt else 300.0
    if not math.isfinite(gates_timeout):
        gates_timeout = 300.0  # F-6 (banca v12): nan/inf → default seguro
    gres: dict | None = None
    rework_attempts = 0

    if not gates_required:
        _record_stage(run_path, "gates", "nenhum portão obrigatório declarado pelo route")
    else:
        while True:
            gres = invoke_gates_fn(repo_root, gates_required, run_id, gates_timeout,
                                   test_cmd=gate_test_cmd)
            if gres is None:
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
            if real_driver and dispatch_fn is not None and envelope is not None \
                    and envelope_path is not None and rework_attempts < max_rework_fn() and findings:
                rework_attempts += 1
                driver_result2, drv_err2, _ = dispatch_fn(
                    driver, envelope, envelope_path, repo_root,
                    driver_timeout, findings=findings,
                )
                if drv_err2:
                    _record_stage(run_path, "rework", f"rework #{rework_attempts} falhou: {drv_err2}", exit_code=1)
                    break
                driver_result = driver_result2 or driver_result
                _patch_run(run_path, lambda d: d.update({
                    "driver_result": driver_result,
                    "files_changed": list((driver_result or {}).get("files_changed") or []),
                }))
                _record_stage(run_path, "rework", f"rework #{rework_attempts}: {len(findings)} achado(s) devolvidos ao driver")
                continue
            _record_stage(
                run_path, "rework",
                ("sem auto-reparo: " + (
                    f"driver '{driver}' não executa rework" if not real_driver
                    else f"limite MAX_REWORK={max_rework_fn()} atingido"
                )) + f" · {len(findings)} achado(s) vermelho(s) reportados",
            )
            break
    _patch_run(run_path, lambda d: d.update({"rework_attempts": rework_attempts}))
    return gres, rework_attempts, driver_result


def _execute_project_tests(
    repo_root: Path,
    run_path: Path,
    run_id: str,
    test_cmd: str,
    test_cmd_rejected: bool,
    project: dict | None,
    timeout_opt: object,
    *,
    invoke_test_fn=_invoke_test_cmd,
) -> dict:
    """Estágio 7: executa test_cmd via `henke exec`, avalia contrato e grava tríade."""
    if test_cmd_rejected:
        t_exit, t_out = 2, (
            f"ALLOWED-CMDS-REJECTED: test_cmd '{test_cmd}' fora da "
            "allowlist de POLICY.json"
        )
    else:
        try:
            test_timeout = float(timeout_opt or (project or {}).get("timeout_sec") or 180.0)
            if not math.isfinite(test_timeout) or test_timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            test_timeout = 180.0
        t_exit, t_out = invoke_test_fn(repo_root, str(test_cmd), test_timeout)
    _verdict = None
    try:
        _ec = _load_module("exec_contract")
        if _ec is not None:
            _verdict = _ec.evaluate_execution(
                str(test_cmd), int(t_exit), t_out or "", "")
    except Exception:
        _verdict = None
    _recognized_runner = bool(_verdict.is_test) if _verdict else False
    discovered = _verdict.discovered if _verdict else 0
    passed = _verdict.passed if _verdict else 0
    _verdict_reason = _verdict.reason if _verdict else "contract_unavailable"
    detail = f"{passed}/{discovered} testes" if discovered else "saída registrada no exec_log"
    tests_ev_dir = repo_root / ".henke" / "evidence" / run_id
    tests_ev: Path | None = None
    try:
        tests_ev_dir.mkdir(parents=True, exist_ok=True)
        tests_ev = tests_ev_dir / "tests.out"
        tests_ev.write_text(t_out or "", encoding="utf-8")
    except OSError:
        tests_ev = None
    if tests_ev is None:
        record_gate(run_path, "tests", 2,
                    f"test_cmd=`{test_cmd}` → EVIDENCE-WRITE-FAILED", manual=True)
    elif _recognized_runner and discovered <= 0:
        record_gate(run_path, "tests", 2,
                    f"test_cmd=`{test_cmd}` → ZERO-TESTS-DISCOVERED "
                    "(runner reconhecido sem nenhum teste)", manual=True)
        detail = "0 testes descobertos — runner reconhecido"
    else:
        record_gate(run_path, "tests", int(t_exit),
                    f"test_cmd=`{test_cmd}` → {detail} "
                    f"[{_verdict_reason}]",
                    command=str(test_cmd), evidence_file=tests_ev)
    _record_stage(run_path, "tests", f"test_cmd=`{test_cmd}` exit={t_exit} {detail}", exit_code=0 if t_exit == 0 else 1)
    return {"cmd": test_cmd, "exit": t_exit, "detail": detail}


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
        names = ", ".join(
            f"{s.get('name', '?')} (sha256:{str(s.get('sha256', ''))[:8]}…)"
            for s in skills_run if isinstance(s, dict)
        )
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
            badge = "N/A" if ec == 3 else ("PASS" if ec == 0 else "FAIL")
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
    elif outcome == "unverified":
        print("Próximo passo: driver simulado (echo) — rode com um driver real (agent:<cli>/cmd:) para verificação de verdade.")
    else:
        print(f"Próximo passo: henke approve {run_path}  (ou revise os motivos no arquivo da run)")


def _finalize_auto_run(
    run_path: Path,
    prompt: str,
    route: dict,
    driver_result: dict | None,
    driver: str | None,
    is_analisar: bool,
    echo_dispatched: bool,
    gres: dict | None,
    gates_required: list[str],
    test_cmd: str | None,
    test_info: dict | None,
    sensitive_hit: bool,
) -> int:
    """Estágio 8: monta 'não verificado', aplica cap NO-OP, verifica run e emite resumo."""
    not_verified: list[str] = []
    if is_analisar:
        not_verified.append(
            "despacho de driver/edições — modo ANALISAR limita a ação à análise (portões/testes)"
        )
    if echo_dispatched:
        not_verified.append("execução real das skills (driver echo é simulado — nenhum arquivo alterado)")
    if driver == "stdin" and not is_analisar:
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

    mutating = str(route.get("mode", "")).upper() in ("EXECUTAR", "EXECUTAR_COMPOUND")
    if mutating and driver_result is not None and not echo_dispatched \
            and not (driver_result.get("files_changed") or []):
        with _file_lock(Path(run_path)):
            data = json.loads(run_path.read_text(encoding="utf-8"))
            derived, reasons = derive_run_outcome(data)
            if derived == "verified":
                data["outcome"] = "unverified"
                data["outcome_capped_from"] = "verified"
                data["reasons"] = list(reasons) + [
                    "NO-OP-UNVERIFIED: driver reportou files_changed=[] — nada a verificar"
                ]
                data["updated_at"] = _now_iso()
                _atomic_write_json(run_path, data)

    ok, _reasons = verify_run_file(run_path)
    run_data = json.loads(run_path.read_text(encoding="utf-8"))
    outcome = run_data.get("outcome", "blocked")
    _print_report(
        run_path, run_data, prompt, route, driver_result,
        (None if is_analisar else driver),
        gres, test_info, not_verified,
    )
    if outcome == "verified" and ok:
        return 0
    if outcome == "failed":
        return 1
    return 2  # blocked / unverified
