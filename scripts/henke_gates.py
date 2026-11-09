#!/usr/bin/env python3
"""
henke_gates.py — Executor mecânico de portões da Suíte Henke (PROTOCOLO v4.0, proposta M3).

Hoje cada portão roda isolado via `henke gate <nome>` e o loop de reparo é apenas
prosa. Este módulo executa uma LISTA de portões em ordem, coleta evidência mecânica
de cada execução (exit, duração, sha256 do stdout, tail do stderr) e, opcionalmente,
grava o veredito na run ativa (`.henke/runs/<run_id>.json`) via `run_state.record_gate`.

Despacho de cada item de `gates`:
  - nome curto do GATE_MAP ("hazard", "assertless", ...) → mesmo despacho de
    `henke gate`: defaults só para hazard/a11y (`<repo>`) e assertless
    (`<repo>/tests` quando existe, senão `<repo>`); demais rodam sem args;
  - path "scripts/...py" (formato que `route` emite em `gates_required`) → resolve
    contra SUITE_ROOT primeiro e depois contra o repo; se o path pertence ao
    GATE_MAP usa os args default daquele portão, senão roda `python <path> <repo>`;
  - desconhecido → verdict com exit 2 (nunca exceção).

Não para no primeiro vermelho: produz relatório completo na ordem pedida. Aborta
com FileNotFoundError apenas quando o registro falha por run inexistente — o CLI
converte em exit 2. `all_green` exige exit==0 em TODOS (exit 2 também reprova).

Códigos de saída (CLI):
  0  todos os portões aprovados (exit 0)
  1  algum portão reprovado (exit != 0, inclui 2)
  2  erro de uso, route.json inválido ou run inexistente
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SUITE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SUITE_ROOT / "scripts"

if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

try:
    # Tabela única da verdade — import real (scripts/ é namespace package PEP 420).
    from scripts.henke_cli import GATE_MAP
except Exception:  # pragma: no cover - fallback defensivo se o import falhar
    _MET = SCRIPTS_DIR / "metricas"
    GATE_MAP = {
        "hazard": _MET / "hazard_scan.py",
        "assertless": _MET / "assertless_scan.py",
        "contracts": _MET / "contract_closure.py",
        "drift": SCRIPTS_DIR / "spec_drift.py",
        "a11y": _MET / "a11y_scan.py",
        "metaconfig": _MET / "metaconfig_diff.py",
        "coverage": _MET / "coverage_ledger.py",
        "ledger": SCRIPTS_DIR / "audit_ledger.py",
        "mutant": _MET / "mutant_hunt.py",
        "custody": SCRIPTS_DIR / "henke_gate.py",
        "fence": _MET / "fence_check.py",
        "ratchet": _MET / "ratchet.py",
        "crap": _MET / "crap_score.py",
        "signals": SCRIPTS_DIR / "repo_signals.py",
    }

try:
    # Função limpa: escrita atômica + FileNotFoundError para run inexistente.
    from scripts.run_state import record_gate as _rs_record_gate
except Exception:  # pragma: no cover - fallback subprocess
    _rs_record_gate = None

MAX_REWORK = 2  # máximo de ciclos de reparo do loop M3 (contrato público)

_STDERR_TAIL_CHARS = 4000
_GATES_COM_REPO_ARG = ("hazard", "a11y")


def _safe_run_id(run_id: str) -> str:
    """Mesma sanitização de run_state.init_run: alfanumérico, '-' e '_'."""
    return "".join(c for c in str(run_id) if c.isalnum() or c in ("-", "_")).strip()


def _looks_like_path(token: str) -> bool:
    return "/" in token or "\\" in token or token.lower().endswith(".py")


def _git_has_head(repo_root: Path) -> bool:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=str(repo_root),
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_SKIP_REASONS = {
    "ratchet": "exige --baseline/--current (TSVs de referência) — não autoexecutável sem baseline registrado",
    "fence_check": "exige JSONs de contrato gerados pelo pipeline — não autoexecutável fora dele",
    "coverage_ledger": "exige ledger de cobertura — rode `coverage_ledger.py init` antes",
    "spec_drift": "exige spec de referência — a run não declarou `spec_path`",
    "contract_closure": "exige spec de referência — a run não declarou `spec_path`",
    "metaconfig_diff": "exige commit base (HEAD) — repositório sem commits",
}


def _default_gate_args(name: str | None, repo_root: Path, spec_path: Path | None = None) -> list[str] | None:
    """Args por portão, espelhando `henke gate`/check. `None` = não autoexecutável
    neste contexto — o veredito vira `skipped` (não-veredito), nunca verde."""
    if name in _GATES_COM_REPO_ARG:
        return [str(repo_root)]
    if name == "assertless":
        tests_dir = repo_root / "tests"
        return [str(tests_dir)] if tests_dir.is_dir() else [str(repo_root)]
    if name == "metaconfig":
        return ["--against", "HEAD"] if _git_has_head(repo_root) else None
    if name == "signals":
        return ["metrics", "--root", str(repo_root)]
    if name == "drift":
        if spec_path and spec_path.is_file():
            return ["--repo", str(repo_root), "--spec", str(spec_path),
                    "--expected-hash", hashlib.sha256(spec_path.read_bytes()).hexdigest()]
        return None
    if name == "contracts":
        return ["--repo", str(repo_root), "--spec", str(spec_path)] if spec_path and spec_path.is_file() else None
    if name == "coverage":
        led = repo_root / ".henke" / "coverage-ledger.json"
        return ["verify", str(led)] if led.is_file() else None
    if name in ("ratchet", "fence"):
        return None
    return []


def _resolve_gate(token: str, repo_root: Path, spec_path: Path | None = None) -> tuple[str, list[str] | None] | None:
    """Resolve um item de `gates` para (gate_id_canônico, cmd_ou_None).
    None = desconhecido; cmd=None = conhecido mas não autoexecutável aqui."""
    if not token:
        return None
    if token in GATE_MAP:
        script = Path(GATE_MAP[token])
        if not script.is_file():
            return None
        args = _default_gate_args(token, repo_root, spec_path)
        return script.stem, ([sys.executable, str(script), *args] if args is not None else None)
    if not _looks_like_path(token):
        return None
    cand = Path(token)
    if not cand.is_absolute():
        suite_cand = SUITE_ROOT / cand
        repo_cand = repo_root / cand
        cand = suite_cand if suite_cand.is_file() else (repo_cand if repo_cand.is_file() else suite_cand)
    if not cand.is_file():
        return None
    resolved = cand.resolve()
    path_to_name = {os.path.normcase(str(Path(p).resolve())): n for n, p in GATE_MAP.items()}
    name = path_to_name.get(os.path.normcase(str(resolved)))
    args = _default_gate_args(name, repo_root, spec_path) if name else [str(repo_root)]
    if args is None:
        return resolved.stem, None
    return resolved.stem, [sys.executable, str(resolved), *args]


def _gate_id_of(token: str) -> str:
    """Identificador canônico do portão (stem do script; o próprio token se nome)."""
    return Path(token).stem if _looks_like_path(token) else token


def _to_text(chunk) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def _record_gate_verdict(run_path: Path, gate_id: str, exit_code: int, sha: str, ms: int) -> None:
    """Grava o veredito na run. Levanta FileNotFoundError se a run não existe."""
    if not run_path.is_file():
        raise FileNotFoundError(f"run inexistente para record-gate: {run_path}")
    summary = f"stdout_sha256={sha};duration_ms={ms}"
    if _rs_record_gate is not None:
        _rs_record_gate(run_path, gate_id, int(exit_code), summary)
        return
    res = subprocess.run(  # fallback subprocess (mesma CLI real de run_state.py)
        [
            sys.executable, str(SCRIPTS_DIR / "run_state.py"), "record-gate",
            str(run_path), "--gate", gate_id, "--exit-code", str(int(exit_code)),
            "--summary", summary,
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"record-gate falhou (exit {res.returncode}): {(res.stderr or '').strip()}")


def run_gates(repo_root: str, gates: list[str], run_id: str | None = None,
              record: bool = True, timeout_sec: float = 180.0) -> dict:
    """Executa `gates` em ordem e devolve o relatório mecânico.

    Retorna {"verdicts": [{"gate", "cmd", "exit", "duration_ms", "stdout_sha256",
    "stderr_tail"}], "all_green": bool, "ran": int}. `verdicts["gate"]` é o stem
    canônico do script do portão (ex. `hazard_scan`), igual para entrada por nome
    ou por path, e é a mesma chave gravada na run.

    Levanta FileNotFoundError (run inexistente), ValueError (run_id inválido) —
    nunca por portão desconhecido (esse vira verdict exit 2).
    """
    root = Path(repo_root).resolve()
    run_path: Path | None = None
    if record and run_id:
        safe = _safe_run_id(run_id)
        if not safe:
            raise ValueError(f"run_id inválido: {run_id!r}")
        run_path = root / ".henke" / "runs" / f"{safe}.json"
        if not run_path.is_file():
            raise FileNotFoundError(f"run inexistente para record-gate: {run_path}")

    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"

    spec_path: Path | None = None
    if run_path is not None:
        try:
            spec_path = Path(json.loads(run_path.read_text(encoding="utf-8")).get("spec_path") or "")
        except (OSError, ValueError):
            spec_path = None

    verdicts: list[dict] = []
    skipped: list[dict] = []
    for raw in gates:
        token = str(raw).strip()
        resolved = _resolve_gate(token, root, spec_path)
        if resolved is None:
            gate_id = _gate_id_of(token)
            cmd_str = ""
            exit_code, out, err, ms = 2, "", f"ERRO: portão desconhecido '{token}'", 0
        elif resolved[1] is None:
            gate_id = resolved[0]
            skipped.append({"gate": gate_id, "skipped": True,
                            "reason": _SKIP_REASONS.get(gate_id, "sem args autoexecutáveis neste contexto")})
            continue
        else:
            gate_id, cmd = resolved
            cmd_str = shlex.join(cmd)
            t0 = time.monotonic()
            try:
                res = subprocess.run(
                    cmd, cwd=str(root), capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=child_env,
                    timeout=timeout_sec,
                )
                ms = int(round((time.monotonic() - t0) * 1000))
                exit_code = res.returncode
                out = res.stdout or ""
                err = res.stderr or ""
            except subprocess.TimeoutExpired as exc:
                ms = int(round((time.monotonic() - t0) * 1000))
                exit_code = 2
                out = _to_text(exc.stdout)
                err = (_to_text(exc.stderr) + f"\n[TIMEOUT] expirou após {timeout_sec}s").strip()
            except OSError as exc:
                ms = int(round((time.monotonic() - t0) * 1000))
                exit_code, out, err = 2, "", f"ERRO ao lançar processo: {exc}"

        sha = hashlib.sha256(out.encode("utf-8")).hexdigest()
        if run_path is not None:
            _record_gate_verdict(run_path, gate_id, exit_code, sha, ms)
        verdicts.append({
            "gate": gate_id,
            "cmd": cmd_str,
            "exit": exit_code,
            "duration_ms": ms,
            "stdout_sha256": sha,
            "stderr_tail": (err or "")[-_STDERR_TAIL_CHARS:],
        })

    return {
        "verdicts": verdicts,
        "skipped": skipped,
        "all_green": all(v["exit"] == 0 for v in verdicts),
        "ran": len(verdicts),
    }


def _load_route_gates(route_path: Path) -> list[str]:
    """Lê `gates_required` de um JSON de saída do route (compact ou full)."""
    data = json.loads(Path(route_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"route.json inválido: raiz não é objeto em {route_path}")
    req = data.get("gates_required")
    if not isinstance(req, list) or any(not isinstance(g, str) for g in req):
        raise ValueError(f"route.json inválido: 'gates_required' ausente ou malformado em {route_path}")
    return [g.strip() for g in req if g.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="henke_gates.py",
        description="Executor mecânico de portões: roda uma lista de portões, "
                    "coleta evidência e registra vereditos na run ativa.",
    )
    parser.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório alvo")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--gates", default=None,
                     help="Portões separados por vírgula (nomes do GATE_MAP ou paths scripts/...py)")
    src.add_argument("--from-route", dest="from_route", type=Path, default=None,
                     help="Arquivo JSON de saída do route (usa 'gates_required')")
    parser.add_argument("--run", dest="run_id", default=None,
                        help="run_id para gravar vereditos em .henke/runs/<id>.json")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="Timeout por portão em segundos (default: 180)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Emite o relatório completo em JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.gates is None and args.from_route is None:
        print("ERRO: informe --gates <lista> ou --from-route <route.json>.", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("ERRO: --timeout deve ser > 0.", file=sys.stderr)
        return 2

    try:
        if args.from_route is not None:
            gates = _load_route_gates(args.from_route)
            if not gates:
                raise ValueError(f"'gates_required' vazio em {args.from_route}")
        else:
            gates = [t.strip() for t in args.gates.split(",") if t.strip()]
            if not gates:
                raise ValueError("lista --gates vazia")
        result = run_gates(
            args.repo.resolve(), gates,
            run_id=args.run_id, record=bool(args.run_id), timeout_sec=args.timeout,
        )
    except (FileNotFoundError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for v in result["verdicts"]:
            badge = "PASS" if v["exit"] == 0 else "FAIL"
            print(f"[{badge}] {v['gate']} (exit {v['exit']}, {v['duration_ms']} ms)")
            for ln in (v["stderr_tail"] or "").splitlines()[:6]:
                print(f"  {ln}")
        n_ok = sum(1 for v in result["verdicts"] if v["exit"] == 0)
        estado = "VERDE" if result["all_green"] else "VERMELHO"
        extra = f" | run '{args.run_id}' atualizada" if args.run_id else ""
        print(f"RESULTADO: {estado} — {n_ok}/{result['ran']} portões aprovados{extra}.")
    return 0 if result["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
