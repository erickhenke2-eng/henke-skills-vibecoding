#!/usr/bin/env python3
"""
henke_report.py — Relatório PT-BR rastreável de uma run (`.henke/runs/<id>.json`).

Renderiza markdown com 5 seções fixas:
  ## O que foi pedido
  ## O que mudou
  ## O que foi verificado
  ## O que NÃO foi verificado
  ## Próximo passo

Toda afirmação é rastreável a um campo real de:
  - `.henke/runs/<run_id>.json`  (pedido, escopo, portões, outcome, review_status)
  - `.henke/exec_log.jsonl`      (comandos executados, testes descobertos/passados)
  - `.henke/LEDGER.json`         (ledger de execuções de teste)

Campo ausente é declarado explicitamente como "não registrado"; nada é inventado.
O outcome exibido é o DERIVADO mecanicamente (via `scripts/run_state.py`); quando a
derivação está indisponível, o relatório usa o outcome declarado e rotula a origem.

Códigos de saída:
  0  relatório renderizado
  2  run inexistente, JSON malformado ou erro de uso
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = SCRIPTS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from scripts._paths import suite_root as _suite_root  # noqa: E402

SUITE_ROOT = _suite_root()  # checkout OU <prefix>/henke_data (wheel)

# Chaves alternativas que uma run pode usar para declarar o pedido original
_PROMPT_KEYS = ("prompt", "pedido", "request", "task", "instrucao", "solicitacao")
# Chaves alternativas que uma run pode usar para declarar arquivos alterados
_FILES_KEYS = ("files_changed", "touched_files", "changed_files", "files", "arquivos_alterados")


def _load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict]:
    """Lê um JSONL tolerante: linhas malformadas são ignoradas (declaradas no relatório)."""
    entries: list[dict] = []
    p = Path(path)
    if not p.is_file():
        return entries
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
            except Exception:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
    except OSError:
        pass
    return entries


def _resolve_run_path(repo_root: Path, run_ref: str) -> Path:
    """Resolve `<run_id>` ou caminho para `.henke/runs/<id>.json` existente.

    Paths arbitrários fora de .henke/runs/ são recusados (mesma política de
    _resolve_run_ref do CLI): `henke report C:\\x.json` não lê JSON solto
    como se fosse run (banca v7 — F-11)."""
    runs_dir = (Path(repo_root) / ".henke" / "runs").resolve()
    cand = Path(run_ref)
    if cand.is_file():
        resolved = cand.resolve()
        try:
            resolved.relative_to(runs_dir)
        except ValueError:
            raise FileNotFoundError(
                f"run fora de .henke/runs/ recusada: {run_ref}"
            )
        return resolved
    name = run_ref if run_ref.endswith(".json") else f"{run_ref}.json"
    path = (runs_dir / name).resolve()
    try:
        path.relative_to(runs_dir)
    except ValueError:
        # F-08 (banca v8): `henke report "../../x"` resolvia para fora de
        # .henke/runs/ e lia qualquer .json do disco como se fosse run.
        raise FileNotFoundError(
            f"run fora de .henke/runs/ recusada: {run_ref}"
        )
    if path.is_file():
        return path
    raise FileNotFoundError(f"run '{run_ref}' não encontrada em {runs_dir}")


def _derive_outcome(repo_root: Path, run_path: Path, data: dict) -> tuple[str, list[str], str]:
    """Retorna (outcome, reasons, origem).

    origem='derivado' quando a máquina de estados está disponível;
    'declarado' (rotulado) quando só resta o campo gravado no arquivo.
    """
    try:
        from scripts.run_state import derive_run_outcome
    except ImportError:
        derive_run_outcome = None
    if derive_run_outcome is not None:
        try:
            derived, reasons = derive_run_outcome(data)
            return str(derived), [str(r) for r in reasons], "derivado"
        except Exception:
            pass
    try:
        res = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "run_state.py"), "verify", "--json", str(run_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        payload = json.loads(res.stdout or "{}")
        derived = payload.get("derived_outcome")
        if derived:
            return str(derived), [str(r) for r in (payload.get("reasons") or [])], "derivado"
    except Exception:
        pass
    declared = data.get("outcome")
    reasons = data.get("reasons")
    return (
        str(declared) if declared else "desconhecido",
        [str(r) for r in reasons] if isinstance(reasons, list) else [],
        "declarado (derivação mecânica indisponível)",
    )


def _to_int(val) -> int:
    """Converte tolerante para int; valores malformados viram 0 (declarados, não inventados)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def _fmt_gate_line(name: str, info: dict) -> str:
    exit_code = info.get("exit_code", "?")
    summary = str(info.get("summary") or "").strip()
    recorded = str(info.get("recorded_at") or "não registrado")
    tail = f" — {summary}" if summary else ""
    return f"`{name}` (exit {exit_code}, registrado em {recorded}){tail}"


def render_run_report(repo_root: Path, run_ref: str) -> str:
    """Renderiza o relatório markdown das 5 seções para a run indicada."""
    repo_root = Path(repo_root).resolve()
    run_path = _resolve_run_path(repo_root, run_ref)
    try:
        data = _load_json(run_path)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON malformado em {run_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"run inválida (raiz não é um objeto JSON): {run_path}")

    henke_dir = repo_root / ".henke"
    exec_entries = _load_jsonl(henke_dir / "exec_log.jsonl")
    ledger_path = henke_dir / "LEDGER.json"
    ledger: list = []
    ledger_erro: str | None = None
    if ledger_path.is_file():
        try:
            ledger_raw = _load_json(ledger_path)
            if isinstance(ledger_raw, list):
                ledger = ledger_raw
            else:
                ledger_erro = "raiz não é uma lista JSON"
        except Exception as exc:
            ledger_erro = str(exc)

    derived, reasons, origem = _derive_outcome(repo_root, run_path, data)
    declared = data.get("outcome")
    review_status = data.get("review_status")
    run_id = str(data.get("run_id") or run_path.stem)
    gerado_em = datetime.now(timezone.utc).isoformat()

    L: list[str] = []
    L.append(f"# Relatório da Run `{run_id}`")
    L.append("")
    L.append(f"- **Arquivo:** `{run_path}`")
    L.append(f"- **Outcome:** `{derived}` ({origem})")
    if isinstance(declared, str) and declared != derived:
        L.append(f"- **Outcome declarado no arquivo:** `{declared}` ⚠️ diverge do derivado")
    L.append(f"- **Gerado em:** {gerado_em}")
    L.append("")

    # ------------------------------------------------------------------
    L.append("## O que foi pedido")
    L.append("")
    prompt_val = None
    for key in _PROMPT_KEYS:
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            prompt_val = v.strip()
            break
    if prompt_val:
        L.append(f"- **Pedido:** {prompt_val}")
    else:
        L.append("- **Pedido:** não registrado na run (campo `prompt`/`request`/`task` ausente)")
    L.append(f"- **Modo:** `{data.get('mode') or 'não registrado'}`")
    L.append(f"- **Arquétipo:** `{data.get('archetype') or 'não registrado'}`")
    spec_path = data.get("spec_path")
    L.append(f"- **Spec:** `{spec_path}`" if spec_path else "- **Spec:** não registrado")
    L.append(f"- **Criada em:** `{data.get('created_at') or 'não registrado'}`")
    L.append(f"- **Atualizada em:** `{data.get('updated_at') or 'não registrado'}`")
    if "requires_human_auth" in data:
        L.append(f"- **Autorização humana exigida:** `{bool(data.get('requires_human_auth'))}`")
    else:
        L.append("- **Autorização humana exigida:** não registrado")
    L.append(f"- **review_status:** `{review_status or 'não registrado'}`")
    mandatory = data.get("mandatory_gates") or data.get("gates_required")
    if isinstance(mandatory, list) and mandatory:
        L.append(f"- **Portões obrigatórios declarados:** {', '.join(f'`{g}`' for g in mandatory)}")
    L.append("")

    # ------------------------------------------------------------------
    L.append("## O que mudou")
    L.append("")
    files_val = None
    for key in _FILES_KEYS:
        v = data.get(key)
        if isinstance(v, list) and v:
            files_val = v
            break
    if files_val:
        L.append("Arquivos declarados como alterados na run:")
        for f in files_val:
            L.append(f"- `{f}`")
    amendments = data.get("scope_amendments")
    if isinstance(amendments, list) and amendments:
        L.append("Emendas de escopo registradas:")
        for item in amendments:
            if isinstance(item, dict):
                L.append(
                    f"- `{item.get('file', '?')}` — {item.get('reason', '?')} "
                    f"(em {item.get('amended_at', 'não registrado')})"
                )
            else:
                L.append(f"- {item}")
    if not files_val and not (isinstance(amendments, list) and amendments):
        L.append(
            "- não registrado — a run não declara arquivos alterados "
            "(campos `files_changed`/`touched_files`/`scope_amendments` ausentes ou vazios)"
        )
    L.append("")

    # ------------------------------------------------------------------
    L.append("## O que foi verificado")
    L.append("")
    gates = data.get("gates")
    gates_ok: list[str] = []
    gates_fail: list[str] = []
    if isinstance(gates, dict) and gates:
        for gname, ginfo in sorted(gates.items()):
            if isinstance(ginfo, dict) and ginfo.get("exit_code") == 0:
                gates_ok.append(_fmt_gate_line(gname, ginfo))
            else:
                ec = ginfo.get("exit_code") if isinstance(ginfo, dict) else "?"
                gates_fail.append(f"`{gname}` (exit {ec})")
    if gates_ok:
        L.append("Portões aprovados (exit 0):")
        for g in gates_ok:
            L.append(f"- {g}")
    test_entries = [e for e in exec_entries if _to_int(e.get("testes_descobertos")) > 0]
    if test_entries:
        L.append("Execuções de teste registradas em `.henke/exec_log.jsonl`:")
        for e in test_entries:
            L.append(
                f"- `{e.get('cmd', '?')}` → exit {e.get('exit', '?')}, "
                f"{e.get('testes_passados', '?')}/{e.get('testes_descobertos', '?')} testes passaram "
                f"(em {e.get('timestamp', 'não registrado')}; "
                f"stdout_sha256 `{str(e.get('stdout_sha256', ''))[:12]}…`)"
            )
    if ledger:
        L.append(
            f"Ledger `.henke/LEDGER.json`: {len(ledger)} execução(ões) de teste registrada(s) "
            f"({sum(1 for i in ledger if isinstance(i, dict) and i.get('exit') == 0)} com exit 0)."
        )
    if not gates_ok and not test_entries and not ledger:
        L.append("- nenhuma verificação registrada (nenhum portão aprovado, nenhum teste no exec_log)")
    L.append("")

    # ------------------------------------------------------------------
    L.append("## O que NÃO foi verificado")
    L.append("")
    nao_verificado: list[str] = []
    if not isinstance(gates, dict) or not gates:
        nao_verificado.append("nenhum portão registrado na run (`gates` vazio/ausente)")
    for g in gates_fail:
        nao_verificado.append(f"portão {g} não aprovado")
    for r in reasons:
        nao_verificado.append(str(r))
    if isinstance(declared, str) and declared == "verified" and derived != "verified":
        nao_verificado.append(
            "CONFLITO: outcome declarado como 'verified' diverge do derivado "
            f"('{derived}') — possível auto-atestado (STATE-DERIVED-CONFLICT)"
        )
    failed_cmds = [e for e in exec_entries if _to_int(e.get("exit")) != 0]
    for e in failed_cmds:
        nao_verificado.append(
            f"comando `{e.get('cmd', '?')}` falhou (exit {e.get('exit', '?')}) "
            f"em {e.get('timestamp', 'não registrado')}"
        )
    non_test_cmds = [
        e for e in exec_entries
        if _to_int(e.get("exit")) == 0 and _to_int(e.get("testes_descobertos")) == 0
    ]
    for e in non_test_cmds:
        nao_verificado.append(
            f"comando `{e.get('cmd', '?')}` executado (exit 0) sem evidência de testes associada "
            f"em {e.get('timestamp', 'não registrado')}"
        )
    if review_status == "goldfish_unavailable":
        nao_verificado.append("certificação Goldfish indisponível (revisão isolada não executada)")
    elif review_status == "pending":
        nao_verificado.append("aprovação/revisão humana pendente")
    if ledger_erro:
        nao_verificado.append(f"`.henke/LEDGER.json` ilegível/corrompido: {ledger_erro}")
    if not exec_entries:
        nao_verificado.append("`.henke/exec_log.jsonl` ausente ou vazio — nenhum comando auditado registrado")
    if nao_verificado:
        for item in nao_verificado:
            L.append(f"- {item}")
    else:
        L.append("- nada pendente registrado")
    L.append("")

    # ------------------------------------------------------------------
    L.append("## Próximo passo")
    L.append("")
    if derived == "verified":
        L.append(
            "- Nenhum bloqueio mecânico: a run está `verified`. "
            "Próximo passo: revisão humana e merge/publicação conforme o fluxo do projeto."
        )
    elif derived == "blocked":
        L.append("- Run `blocked` — destravar as pendências listadas acima:")
        L.append("  - registrar portões: `henke run record-gate <run_file> --gate <nome> --exit-code <N>`")
        if review_status == "pending" or data.get("requires_human_auth"):
            L.append("  - aprovar a run: `henke approve <run_file> --approver <nome>`")
    else:
        L.append("- Run `failed` — corrigir as falhas acima e re-executar os portões afetados.")
    if review_status == "goldfish_unavailable":
        L.append(
            "- Para certificação Goldfish: `henke goldfish --driver \"<comando do revisor>\" "
            f"--run {run_id}` (revisão isolada em contexto zerado)."
        )
    L.append("")

    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="henke_report.py",
        description="Renderiza relatório markdown rastreável de uma run (.henke/runs/<id>.json).",
    )
    parser.add_argument("run_id", help="ID da run ou caminho do arquivo .henke/runs/<id>.json")
    parser.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        print(render_run_report(args.repo.resolve(), args.run_id))
        return 0
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
