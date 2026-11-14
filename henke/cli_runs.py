"""cli_runs.py — ciclo de vida de runs: list/run/status/report/session + derivadores."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from henke.cli_kernel import (
    SCRIPTS_DIR,
    _scrub_env,
)



def _cmd_session(args) -> int:
    import henke.henke_session as hs
    if args.session_cmd == "start":
        s = hs.start_session(args.agent, args.workspace.resolve(), args.run_id)
        print(f"HENKE_SESSION_ID={s.session_id}")
        print(s.session_id)
        return 0
    ws = args.workspace.resolve() if getattr(args, "workspace", None) else Path.cwd()
    os.environ["HENKE_WORKSPACE"] = str(ws)
    if args.session_cmd == "heartbeat":
        hs.heartbeat(args.session_id)
        print(f"[OK] heartbeat renovado: {args.session_id}")
        return 0
    if args.session_cmd == "end":
        hs.end_session(args.session_id)
        print(f"[OK] sessão encerrada: {args.session_id}")
        return 0
    if args.session_cmd == "gc":
        n = hs.session_gc(ws, now=datetime.now(timezone.utc))
        print(f"[OK] session gc: {n} sentinel(s) reaped.")
        return 0
    return 2


def _iter_run_files(repo_root: Path) -> list[Path]:
    """Lista `.henke/runs/*.json`, excluindo envelopes (`*.envelope.json`, `*.goldfish.json`)."""
    runs_dir = repo_root / ".henke" / "runs"
    if not runs_dir.is_dir():
        return []
    return sorted(
        p for p in runs_dir.glob("*.json")
        if p.is_file()
        and not p.name.endswith(".goldfish.json")
        and not p.name.endswith(".envelope.json")
    )


def _derive_outcome_safe(run_path: Path, run_data: dict) -> str:
    """Outcome DERIVADO mecanicamente (nunca o declarado no arquivo).

    Tenta `henke.run_state.derive_run_outcome` via import; se o módulo não
    estiver importável, cai para subprocess `run_state.py verify --json`.
    """
    try:
        from henke.run_state import derive_run_outcome
    except ImportError:
        derive_run_outcome = None
    if derive_run_outcome is not None:
        try:
            derived, _ = derive_run_outcome(run_data)
            return str(derived)
        except Exception:
            pass
    try:
        res = subprocess.run(
            [sys.executable, "-Es", str(SCRIPTS_DIR / "run_state.py"), "verify", "--json", str(run_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=_scrub_env())
        payload = json.loads(res.stdout or "{}")
        derived = payload.get("derived_outcome")
        if derived:
            return str(derived)
    except Exception:
        pass
    return "desconhecido"


def _cmd_list_runs(repo_root: Path, as_json: bool = False) -> int:
    """Lista runs de `.henke/runs/` com outcome DERIVADO (via run_state), não declarado."""
    rows: list[dict] = []
    for rp in _iter_run_files(repo_root):
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        rows.append({
            "run_id": str(data.get("run_id") or rp.stem),
            "mode": str(data.get("mode") or "não registrado"),
            "archetype": str(data.get("archetype") or "não registrado"),
            "outcome_derived": _derive_outcome_safe(rp, data),
            "outcome_declared": str(data.get("outcome") or "não registrado"),
            "human_approval": str(data.get("human_approval") or data.get("review_status") or "não registrado"),
            "review_status": str(data.get("review_status") or "não registrado"),
            "updated_at": str(data.get("updated_at") or "não registrado"),
        })

    if as_json:
        print(json.dumps({"repo": str(repo_root), "runs": rows}, ensure_ascii=False, indent=2))
        return 0

    runs_dir = repo_root / ".henke" / "runs"
    if not rows:
        print(f"Nenhuma run registrada em {runs_dir}.")
        return 0

    print(f"Runs em {runs_dir} (outcome DERIVADO, não declarado):")
    header = (f"{'RUN_ID':<28} {'MODO':<10} {'DERIVADO':<10} {'DECLARADO':<10} "
              f"{'APROVAÇÃO':<14} {'REVIEW':<22} ATUALIZADO")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['run_id'][:28]:<28} {r['mode'][:10]:<10} {r['outcome_derived'][:10]:<10} "
            f"{r['outcome_declared'][:10]:<10} {r['human_approval'][:14]:<14} "
            f"{r['review_status'][:22]:<22} {r['updated_at']}"
        )
    return 0


def _cmd_run(argv_list: list[str]) -> int:
    """Wrapper fino sobre `henke/run_state.py` (mesmos exit codes) + `list` nativo."""
    usage = (
        "Uso: henke run <init|record-gate|amend-scope|approve|verify|list> [args...]\n"
        "  init|record-gate|amend-scope|approve|verify  repassados a henke/run_state.py\n"
        "  list [--repo D] [--json]                     lista .henke/runs/ com outcome derivado"
    )
    if not argv_list:
        print(usage, file=sys.stderr)
        return 2
    if argv_list[0] in ("-h", "--help"):
        print(usage)
        return 0

    if argv_list[0] == "list":
        ap = argparse.ArgumentParser(
            prog="henke run list",
            description="Lista runs de .henke/runs/ com outcome DERIVADO.",
        )
        ap.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
        ap.add_argument("--json", action="store_true", help="Saída em JSON")
        try:
            ns = ap.parse_args(argv_list[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        return _cmd_list_runs(ns.repo.resolve(), as_json=ns.json)

    # Delegação: import limpo quando possível (run_state é stdlib pura);
    # senão, subprocess preservando os mesmos exit codes.
    try:
        from henke.run_state import main as run_state_main
    except ImportError:
        run_state_main = None
    if run_state_main is not None:
        return run_state_main(argv_list)
    res = subprocess.run([sys.executable, "-Es", str(SCRIPTS_DIR / "run_state.py"), *argv_list], env=_scrub_env())
    return res.returncode


def _resolve_run_ref(repo_root: Path, run_ref: str | None) -> Path | None:
    """Resolve `--run`/`<run_id>` para um caminho `.henke/runs/<id>.json` existente."""
    if not run_ref:
        return None
    runs_dir = (repo_root / ".henke" / "runs").resolve()
    cand = Path(run_ref)
    if cand.is_file():
        real = cand.resolve()
        try:
            real.relative_to(runs_dir)
        except ValueError:
            return None  # C3-banca: recusa caminho fora de .henke/runs (traversal)
        return real
    name = run_ref if run_ref.endswith(".json") else f"{run_ref}.json"
    if Path(name).name != name:  # '..' ou separador embutido → nunca vira path
        return None
    path = (runs_dir / name).resolve()
    try:
        path.relative_to(runs_dir)
    except ValueError:
        return None
    return path if path.is_file() else None


def _git_diff_snapshot(repo_root: Path) -> tuple[str | None, str]:
    """Captura o diff de trabalho para o envelope Goldfish (staged + unstaged vs HEAD).

    Retorna (texto_ou_None, origem_honesta): nunca inventa diff — quando o git
    falha ou não há HEAD, declara a origem exata do que foi capturado.
    """
    try:
        from henke.state_paths import sanitized_child_env
        _genv = sanitized_child_env(repo_root)
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             # A8 (banca v14): .gitattributes textconv executava comando do
             # repo ao materializar o envelope — RCE via clone+goldfish.
             "diff", "--no-ext-diff", "--no-textconv", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_genv, timeout=60,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout, "git diff HEAD"
        res2 = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_genv, timeout=60,
        )
        if res2.returncode == 0:
            if res2.stdout.strip():
                return res2.stdout, "git status --porcelain (diff indisponível: sem HEAD ou vazio)"
            return "", "git diff HEAD (vazio — nada alterado a revisar)"
        motivo = (res.stderr or res2.stderr or "").strip() or "git falhou"
        return None, f"indisponível: {motivo}"
    except OSError as exc:
        return None, f"indisponível: {exc}"


def _cmd_report(repo_root: Path, run_ref: str) -> int:
    """`henke report <run_id>`: renderiza o relatório markdown via henke/henke_report.py."""
    henke_report = None
    try:
        from henke import henke_report as _hr
        henke_report = _hr
    except ImportError:
        # Fallback: carrega o arquivo diretamente do diretório de scripts instalado.
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("henke_report", SCRIPTS_DIR / "henke_report.py")
            if spec is None or spec.loader is None:
                raise ImportError("spec indisponível")
            henke_report = _ilu.module_from_spec(spec)
            spec.loader.exec_module(henke_report)
        except Exception:
            henke_report = None
    if henke_report is None or not callable(getattr(henke_report, "render_run_report", None)):
        print(
            "ERRO: módulo 'henke/henke_report.py' indisponível — relatório não pode ser gerado.",
            file=sys.stderr,
        )
        return 2
    try:
        print(henke_report.render_run_report(repo_root, run_ref))
        return 0
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2
