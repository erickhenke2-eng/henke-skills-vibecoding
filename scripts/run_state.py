#!/usr/bin/env python3
"""
run_state.py — Máquina de Estados determinística de execução (.henke/runs/<run_id>.json).

Implementa o contrato mecânico do PROTOCOLO v4.0 (§2):
- `outcome` ('verified' | 'failed' | 'blocked') é SEMPRE derivado dos portões executados,
  das emendas de escopo justificadas e do status de aprovação humana.
- Rejeita `outcome: "verified"` declarado manualmente quando a derivação mecânica
  diverge (`STATE-DERIVED-CONFLICT`).
- Todas as mutações de estado usam escrita atômica (`.tmp` + `os.replace`).

Códigos de saída:
  0  operação concluída / run verificada com sucesso
  1  conflito de estado derivado, portão reprovado ou emenda sem justificativa
  2  erro de uso, arquivo inexistente ou JSON malformado
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


@contextlib.contextmanager
def _file_lock(target: Path, timeout: float = 10.0, poll: float = 0.05):
    """Lock interprocessos por arquivo-alvo (`<target>.lock`).

    Stdlib pura e multi-plataforma: `os.open(O_CREAT|O_EXCL)` com retry até
    `timeout`. Cobre a transação read-modify-write inteira — sem ele, dois
    processos leem o mesmo JSON e um write é perdido.
    """
    lock_path = Path(str(Path(target)) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timeout aguardando lock de escrita: {lock_path}")
            time.sleep(poll)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        os.close(fd)
        try:
            lock_path.unlink()
        except OSError:
            pass


def _repo_root_for_run(run_path: Path) -> Path | None:
    """Deriva a raiz do repo de `.henke/runs/<id>.json` (None fora desse layout)."""
    p = Path(run_path).resolve()
    try:
        if p.parent.name == "runs" and p.parent.parent.name == ".henke":
            return p.parent.parent.parent
    except (OSError, IndexError):
        pass
    return None


def _git_head(repo_root: Path) -> str | None:
    """HEAD atual do repo (None se git indisponível/sem commits — nunca inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = (res.stdout or "").strip()
    return sha if res.returncode == 0 and sha else None


def derive_run_outcome(run_data: dict) -> tuple[str, list[str]]:
    """Deriva mecanicamente ('verified' | 'failed' | 'blocked', reasons) a partir de run_data."""
    if not isinstance(run_data, dict):
        return "failed", ["INVALID-RUN-DATA: estrutura de run não é um objeto JSON"]

    reasons: list[str] = []

    # 1. Valida emendas de escopo antes de tudo (emenda sem justificativa invalida a run)
    amendments = run_data.get("scope_amendments", [])
    if not isinstance(amendments, list):
        return "failed", ["UNJUSTIFIED-SCOPE-AMENDMENT: scope_amendments deve ser uma lista"]
    for idx, item in enumerate(amendments):
        if not isinstance(item, dict):
            reasons.append(f"UNJUSTIFIED-SCOPE-AMENDMENT: item #{idx + 1} inválido")
            continue
        f_rel = str(item.get("file", "")).strip()
        rsn = str(item.get("reason", "")).strip()
        if not f_rel or len(rsn) < 5:
            reasons.append(
                f"UNJUSTIFIED-SCOPE-AMENDMENT: arquivo '{f_rel or '<vazio>'}' sem motivo válido (mínimo 5 caracteres)"
            )
    if reasons:
        return "failed", reasons

    # 2. Verifica execução física dos portões
    gates = run_data.get("gates", {})
    if not isinstance(gates, dict):
        return "blocked", ["NO-GATES-EXECUTED: 'gates' não é um objeto na run"]

    # Atestações manuais (`manual: true` — record-gate sem --command/--evidence-file)
    # NUNCA contam como portão físico; falha declarada manualmente segue reprovando.
    physical_gates: dict = {}
    manual_failures: list[str] = []
    for gate_name, gate_info in gates.items():
        if isinstance(gate_info, dict) and gate_info.get("manual") is True:
            ec = gate_info.get("exit_code")
            if isinstance(ec, int) and not isinstance(ec, bool) and ec != 0:
                manual_failures.append(
                    f"GATE-FAILED: {gate_name} (exit {ec} — atestação manual, sem evidência física)"
                )
            continue
        physical_gates[gate_name] = gate_info

    if not physical_gates:
        if manual_failures:
            return "failed", manual_failures
        if gates:
            return "blocked", [
                "NO-GATES-EXECUTED: apenas atestações manuais (manual: true) na run — "
                "nenhum portão físico registrado"
            ]
        return "blocked", ["NO-GATES-EXECUTED: nenhum portão registrado na run"]

    failed_gates: list[str] = list(manual_failures)
    for gate_name, gate_info in physical_gates.items():
        if not isinstance(gate_info, dict):
            failed_gates.append(f"GATE-FAILED: {gate_name} (formato inválido)")
            continue
        ec = gate_info.get("exit_code")
        if isinstance(ec, bool) or not isinstance(ec, int) or ec != 0:
            failed_gates.append(f"GATE-FAILED: {gate_name} (exit {ec})")
    if failed_gates:
        return "failed", failed_gates

    # 2b. Verifica portões obrigatórios declarados na run (ou gates_required)
    mandatory_gates = run_data.get("mandatory_gates") or run_data.get("gates_required")
    if not mandatory_gates and str(run_data.get("mode", "")).upper() == "EXECUTAR":
        # Fallback de portões obrigatórios para o modo EXECUTAR:
        # Exige ao menos os portões fundamentais executados (hazard_scan, assertless_scan, a11y_scan)
        # com exit code 0, barrando runs compostas unicamente por portões fictícios/dummy.
        core_fundamental = ("hazard_scan", "assertless_scan", "a11y_scan")
        executed_core = [
            g for g in core_fundamental
            if any(
                (g_name == g or Path(g_name).stem == g)
                and isinstance(g_info, dict)
                and g_info.get("exit_code") == 0
                for g_name, g_info in physical_gates.items()
            )
        ]
        if not executed_core:
            return "blocked", [
                "MANDATORY-GATE-MISSING: nenhum dos portões fundamentais "
                "('hazard_scan', 'assertless_scan', 'a11y_scan') foi executado com sucesso"
            ]
        if run_data.get("spec_path"):
            spec_gates = ["spec_drift", "contract_closure"]
            missing_spec = [
                req for req in spec_gates
                if not any(
                    (g_name == req or Path(g_name).stem == req)
                    and isinstance(g_info, dict)
                    and g_info.get("exit_code") == 0
                    for g_name, g_info in physical_gates.items()
                )
            ]
            if missing_spec:
                return "blocked", [
                    f"MANDATORY-GATE-MISSING: portão obrigatório de spec '{req}' não executado com sucesso"
                    for req in missing_spec
                ]
    elif mandatory_gates and isinstance(mandatory_gates, list):
        missing_mandatory: list[str] = []
        for req in mandatory_gates:
            gate_stem = Path(req).stem if ("/" in req or "\\" in req) else req
            found = any(
                (g_name == req or Path(g_name).stem == gate_stem)
                and isinstance(g_info, dict)
                and g_info.get("exit_code") == 0
                for g_name, g_info in physical_gates.items()
            )
            if not found:
                missing_mandatory.append(
                    f"MANDATORY-GATE-MISSING: portão obrigatório '{req}' não executado com sucesso"
                )
        if missing_mandatory:
            return "blocked", missing_mandatory

    # 3. Verifica trava de autorização humana para operações sensíveis/PUBLISH
    if run_data.get("requires_human_auth") is True and run_data.get("review_status") != "approved":
        return "blocked", ["HUMAN-AUTH-REQUIRED: review_status pendente de aprovação humana"]

    return "verified", []


def verify_run_file(run_path: Path) -> tuple[bool, list[str]]:
    """Verifica um arquivo .henke/runs/<run_id>.json contra adulteração de estado e falhas."""
    p = Path(run_path)
    if not p.is_file():
        return False, [f"RUN-FILE-MISSING: arquivo não encontrado em '{p}'"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"RUN-JSON-INVALID: {exc}"]

    if not isinstance(data, dict):
        return False, ["RUN-JSON-INVALID: raiz do JSON deve ser um objeto"]

    declared = data.get("outcome")
    derived, reasons = derive_run_outcome(data)

    if declared == "verified" and derived != "verified":
        conflict_msg = (
            f"STATE-DERIVED-CONFLICT: outcome declarado como 'verified' "
            f"mas derivação mecânica resultou em '{derived}'"
        )
        return False, [conflict_msg] + reasons

    if derived != "verified":
        return False, reasons

    return True, []


def init_run(
    repo_root: Path,
    run_id: str,
    mode: str = "EXECUTAR",
    archetype: str = "CARPINTEIRO",
    spec_path: str | None = None,
    requires_human_auth: bool = False,
    mandatory_gates: list[str] | None = None,
) -> Path:
    """Inicializa .henke/runs/<run_id>.json com escrita atômica."""
    safe_id = "".join(c for c in run_id if c.isalnum() or c in ("-", "_")).strip()
    if not safe_id:
        raise ValueError("run_id inválido")
    run_path = Path(repo_root).resolve() / ".henke" / "runs" / f"{safe_id}.json"
    now = _now_iso()
    data: dict = {
        "version": 1,
        "run_id": safe_id,
        "mode": mode,
        "archetype": archetype,
        "spec_path": spec_path,
        "created_at": now,
        "updated_at": now,
        "requires_human_auth": bool(requires_human_auth),
        "review_status": "pending" if requires_human_auth else "not_required",
        "gates": {},
        "scope_amendments": [],
    }
    if mandatory_gates is not None:
        data["mandatory_gates"] = list(mandatory_gates)
    derived, reasons = derive_run_outcome(data)
    data["outcome"] = derived
    data["reasons"] = reasons
    _atomic_write_json(run_path, data)
    return run_path


def _load_run(run_path: Path) -> dict:
    p = Path(run_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Arquivo de run não encontrado: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Conteúdo da run não é um objeto JSON")
    return data


def record_gate(
    run_path: Path,
    gate_name: str,
    exit_code: int,
    summary: str = "",
    *,
    command: str | None = None,
    evidence_file: str | Path | None = None,
    manual: bool = False,
) -> dict:
    """Registra o veredito de um portão na run e recalcula o outcome derivado.

    Evidência física: `command` + `evidence_file` (arquivo de saída do comando)
    gravam `command`/`evidence_file`/`evidence_sha256` — é o que prova execução
    real. `manual=True` marca `manual: true`: atestação que NUNCA conta como
    portão físico em `derive_run_outcome`. Chamadas de API sem evidência e sem
    `manual=True` mantêm o comportamento legado (físico) — a exigência de
    evidência vale na superfície CLI (`record-gate`).

    Transação leitura→escrita coberta por `_file_lock(run_path)`.
    """
    has_cmd = bool(command and str(command).strip())
    has_ev = evidence_file is not None and str(evidence_file).strip() != ""
    if has_cmd != has_ev:
        raise ValueError(
            "evidência incompleta: informe command E evidence_file juntos "
            "(ou nenhum dos dois para atestação manual)"
        )
    evidence_path: Path | None = None
    evidence_sha: str | None = None
    if has_cmd and has_ev:
        evidence_path = Path(str(evidence_file))
        if not evidence_path.is_file():
            raise FileNotFoundError(f"evidence-file inexistente: {evidence_path}")
        evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    with _file_lock(run_path):
        data = _load_run(run_path)
        gates = data.setdefault("gates", {})
        if not isinstance(gates, dict):
            gates = {}
            data["gates"] = gates
        entry: dict = {
            "exit_code": int(exit_code),
            "summary": summary,
            "recorded_at": _now_iso(),
        }
        if evidence_sha is not None:
            entry["command"] = str(command)
            entry["evidence_file"] = str(evidence_path)
            entry["evidence_sha256"] = evidence_sha
        elif manual:
            entry["manual"] = True
        gates[gate_name] = entry
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
    return data


def amend_scope(run_path: Path, file_rel: str, reason: str) -> dict:
    """Adiciona uma emenda de escopo auditada à run e recalcula o outcome."""
    with _file_lock(run_path):
        data = _load_run(run_path)
        amendments = data.setdefault("scope_amendments", [])
        if not isinstance(amendments, list):
            amendments = []
            data["scope_amendments"] = amendments
        amendments.append({
            "file": file_rel.replace("\\", "/").strip(),
            "reason": reason.strip(),
            "amended_at": _now_iso(),
        })
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
    return data


def approve_run(run_path: Path, approver: str = "human") -> dict:
    """Registra aprovação humana explícita para destravar runs com requires_human_auth.

    Grava `approved_head` (git rev-parse HEAD do repo da run, ou None) como
    evidência informativa — o vínculo executável (`approval_binding`) é criado
    no nascimento da run e checado por `henke auto --resume`.
    """
    with _file_lock(run_path):
        data = _load_run(run_path)
        data["review_status"] = "approved"
        data["approved_by"] = approver.strip() or "human"
        data["approved_at"] = _now_iso()
        repo_root = _repo_root_for_run(run_path)
        data["approved_head"] = _git_head(repo_root) if repo_root is not None else None
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gerenciador de estado determinístico de runs (.henke/runs/).")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_init = sub.add_parser("init", help="Inicializa um novo arquivo de run")
    p_init.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório")
    p_init.add_argument("--run-id", required=True, help="Identificador único da run")
    p_init.add_argument("--mode", default="EXECUTAR", choices=("ANALISAR", "PLANEJAR", "EXECUTAR"))
    p_init.add_argument("--archetype", default="CARPINTEIRO", choices=("SHERLOCK", "ESTADISTA", "CARPINTEIRO"))
    p_init.add_argument("--spec", default=None, help="Caminho opcional da spec")
    p_init.add_argument("--requires-human-auth", action="store_true")
    p_init.add_argument("--mandatory-gates", nargs="*", default=None, help="Portões obrigatórios declarados")

    p_gate = sub.add_parser("record-gate", help="Registra execução de um portão na run")
    p_gate.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_gate.add_argument("--gate", required=True, help="Nome do portão")
    p_gate.add_argument("--exit-code", type=int, required=True, help="Código de saída do portão")
    p_gate.add_argument("--summary", default="", help="Resumo curto da saída")
    p_gate.add_argument("--command", default=None,
                        help="Comando real executado (evidência física do veredito)")
    p_gate.add_argument("--evidence-file", dest="evidence_file", type=Path, default=None,
                        help="Arquivo de saída do comando; sha256 é registrado como evidência")

    p_amend = sub.add_parser("amend-scope", help="Registra emenda de escopo justificada")
    p_amend.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_amend.add_argument("--file", required=True, help="Arquivo relativo adicionado ao escopo")
    p_amend.add_argument("--reason", required=True, help="Justificativa técnica (>= 5 caracteres)")

    p_app = sub.add_parser("approve", help="Aprova manualmente uma run pendente de autorização humana")
    p_app.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_app.add_argument("--approver", default="human", help="Identificador do aprovador humano")

    p_ver = sub.add_parser("verify", help="Verifica integridade e outcome derivado de uma run")
    p_ver.add_argument("run_file", nargs="?", type=Path, default=None, help="Caminho para .henke/runs/<id>.json")
    p_ver.add_argument("--run", dest="run_opt", type=Path, default=None, help="Caminho para .henke/runs/<id>.json (alternativo)")
    p_ver.add_argument("--json", action="store_true", help="Saída em JSON")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        if args.subcmd == "init":
            out_path = init_run(
                args.repo,
                args.run_id,
                mode=args.mode,
                archetype=args.archetype,
                spec_path=args.spec,
                requires_human_auth=args.requires_human_auth,
                mandatory_gates=args.mandatory_gates,
            )
            print(f"OK: Run inicializada em {out_path}")
            return 0

        if args.subcmd == "record-gate":
            has_cmd = bool(args.command and str(args.command).strip())
            has_ev = args.evidence_file is not None
            if has_cmd != has_ev:
                print(
                    "ERRO: evidência incompleta — informe --command E --evidence-file juntos. "
                    "Sem ambos, o veredito vira atestação manual (manual: true) e não conta "
                    "como portão físico.",
                    file=sys.stderr,
                )
                return 2
            data = record_gate(
                args.run_file, args.gate, args.exit_code, args.summary,
                command=args.command, evidence_file=args.evidence_file,
                manual=not has_cmd,
            )
            tag = " (atestação manual — não conta como portão físico)" if not has_cmd else ""
            print(f"OK: Gate '{args.gate}' (exit {args.exit_code}) registrado{tag} -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "amend-scope":
            data = amend_scope(args.run_file, args.file, args.reason)
            if data["outcome"] == "failed" and any("UNJUSTIFIED-SCOPE-AMENDMENT" in r for r in data.get("reasons", [])):
                for r in data["reasons"]:
                    print(f"  [RUN-ERROR] {r}", file=sys.stderr)
                return 1
            print(f"OK: Escopo emendado com '{args.file}' -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "approve":
            data = approve_run(args.run_file, args.approver)
            print(f"OK: Run aprovada por '{args.approver}' -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "verify":
            run_target = args.run_opt or args.run_file
            if run_target is None:
                print("ERRO: informe o arquivo de run (posicional ou --run).", file=sys.stderr)
                return 2
            if not run_target.is_file():
                print(f"ERRO: arquivo de run inexistente: {run_target}", file=sys.stderr)
                return 2
            try:
                raw_json = json.loads(run_target.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"ERRO: JSON inválido em {run_target}: {exc}", file=sys.stderr)
                return 2
            ok, reasons = verify_run_file(run_target)
            derived, _ = derive_run_outcome(raw_json) if isinstance(raw_json, dict) else ("failed", [])
            if args.json:
                print(json.dumps({
                    "run_file": str(run_target),
                    "verified": ok,
                    "derived_outcome": derived,
                    "reasons": reasons,
                }, ensure_ascii=False, indent=2))
            else:
                if ok:
                    print(f"OK: Run '{run_target}' verificada com sucesso (outcome='verified').")
                else:
                    print(f"FALHA NA VERIFICAÇÃO DA RUN '{run_target}' (derived='{derived}'):", file=sys.stderr)
                    for r in reasons:
                        print(f"  [RUN-ERROR] {r}", file=sys.stderr)
            return 0 if ok else 1

    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
