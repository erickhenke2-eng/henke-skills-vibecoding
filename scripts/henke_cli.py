#!/usr/bin/env python3
"""
henke_cli.py — CLI Global Unificada da Suíte Henke (PROTOCOLO v4.0).

Resolve `SUITE_ROOT` automaticamente a partir da própria localização física do script,
eliminando a dependência de CWD dos 14 portões mecânicos e fornecendo:
  - `henke doctor`   : diagnóstico do ambiente, scripts e catálogo de skills
  - `henke init`     : inicialização de `.henke/`, `docs/designs/TEMPLATE.md` e baseline Git
  - `henke route`    : roteamento determinístico de prompt (`--compact` por padrão)
  - `henke exec`     : runner wrapper que executa testes reais e grava `.henke/exec_log.jsonl`
                       (hash-chain SHA-256) e `.henke/LEDGER.json` com `stdout_sha256`
  - `henke gate`     : despacho direto para qualquer portão mecânico pelo nome curto
  - `henke check`    : bateria agregada de portões sem abortar no primeiro erro
  - `henke approve`  : aprovação humana auditada de uma run (`.henke/runs/<id>.json`)
  - `henke scope`    : emenda de escopo justificada em uma run ativa

Códigos de saída:
  0  sucesso / todos os portões aprovados
  1  falha de portão ou comando executado
  2  erro de uso ou argumento inválido
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SUITE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SUITE_ROOT / "scripts"
METRICAS_DIR = SCRIPTS_DIR / "metricas"

if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

GATE_MAP: dict[str, Path] = {
    "hazard": METRICAS_DIR / "hazard_scan.py",
    "assertless": METRICAS_DIR / "assertless_scan.py",
    "contracts": METRICAS_DIR / "contract_closure.py",
    "drift": SCRIPTS_DIR / "spec_drift.py",
    "a11y": METRICAS_DIR / "a11y_scan.py",
    "metaconfig": METRICAS_DIR / "metaconfig_diff.py",
    "coverage": METRICAS_DIR / "coverage_ledger.py",
    "ledger": SCRIPTS_DIR / "audit_ledger.py",
    "mutant": METRICAS_DIR / "mutant_hunt.py",
    "custody": SCRIPTS_DIR / "henke_gate.py",
    "fence": METRICAS_DIR / "fence_check.py",
    "ratchet": METRICAS_DIR / "ratchet.py",
    "crap": METRICAS_DIR / "crap_score.py",
    "signals": SCRIPTS_DIR / "repo_signals.py",
}

_PYTEST_RE = re.compile(r"(?i)\b(\d+)\s+passed\b")
_PYTEST_FAIL_RE = re.compile(r"(?i)\b(\d+)\s+(?:failed|errors?)\b")
_UNITTEST_RAN_RE = re.compile(r"Ran\s+(\d+)\s+tests?\s+in\s+", flags=re.I)
_UNITTEST_FAIL_RE = re.compile(r"FAILED\s*\(([^)]+)\)", flags=re.I)
_JEST_VITEST_RE = re.compile(
    r"Tests:\s*(?:(\d+)\s+failed\s*[|,]\s*)?(?:(\d+)\s+skipped\s*[|,]\s*)?(\d+)\s+passed",
    flags=re.I,
)
_CARGO_TEST_RE = re.compile(
    r"test result:\s+\w+\.\s+(\d+)\s+passed;\s+(\d+)\s+failed",
    flags=re.I,
)
_GO_TEST_OK_RE = re.compile(r"(?m)^ok\s+\S+")
_GO_TEST_FAIL_RE = re.compile(r"(?m)^FAIL\s+\S+")
_CANARY_RE = re.compile(r"(\d+)/(\d+)\s+can[aá]rios\s+aprovados", flags=re.I)


def parse_test_runner_output(stdout: str, stderr: str) -> tuple[int, int]:
    """Extrai deterministicamente (testes_descobertos, testes_passados) da saída de um test runner."""
    combined = (stdout or "") + "\n" + (stderr or "")

    # 1. Jest / Vitest ("Tests: 1 failed, 4 passed, 5 total" ou "Tests: 5 passed")
    m_jv = _JEST_VITEST_RE.search(combined)
    if m_jv:
        failed = int(m_jv.group(1) or 0)
        passed = int(m_jv.group(3) or 0)
        return passed + failed, passed

    # 2. Cargo test ("test result: ok. 12 passed; 0 failed")
    m_cargo = list(_CARGO_TEST_RE.finditer(combined))
    if m_cargo:
        passed = sum(int(m.group(1)) for m in m_cargo)
        failed = sum(int(m.group(2)) for m in m_cargo)
        return passed + failed, passed

    # 3. Python unittest ("Ran N tests in X.XXXs")
    m_ut = list(_UNITTEST_RAN_RE.finditer(combined))
    if m_ut:
        ran = sum(int(m.group(1)) for m in m_ut)
        failed_count = 0
        for m_f in _UNITTEST_FAIL_RE.finditer(combined):
            for num in re.findall(r"(?:failures|errors)=(\d+)", m_f.group(1), flags=re.I):
                failed_count += int(num)
        passed = max(0, ran - failed_count)
        return ran, passed

    # 4. Canários Henke ("5/5 canários aprovados")
    m_can = _CANARY_RE.search(combined)
    if m_can:
        passed = int(m_can.group(1))
        total = int(m_can.group(2))
        return total, passed

    # 5. Pytest ("12 passed, 1 failed")
    m_py = _PYTEST_RE.search(combined)
    if m_py:
        passed = int(m_py.group(1))
        failed = sum(int(m.group(1)) for m in _PYTEST_FAIL_RE.finditer(combined))
        return passed + failed, passed

    # 6. Go test ("ok   pkg/foo 0.012s")
    go_ok = len(_GO_TEST_OK_RE.findall(combined))
    go_fail = len(_GO_TEST_FAIL_RE.findall(combined))
    if go_ok > 0 or go_fail > 0:
        return go_ok + go_fail, go_ok

    return 0, 0


def _atomic_write_json(path: Path, data: object) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _cmd_doctor(repo_root: Path, as_json: bool = False) -> int:
    py_ok = sys.version_info >= (3, 10)
    git_res = subprocess.run(
        ["git", "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    git_ok = git_res.returncode == 0
    missing_scripts = [name for name, p in sorted(GATE_MAP.items()) if not p.is_file()]
    skills_count = len(list(SUITE_ROOT.glob("henke-*/SKILL.md")))
    henke_dir = (repo_root / ".henke").is_dir()

    report = {
        "status": "ok" if (py_ok and git_ok and not missing_scripts) else "degraded",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_ok": py_ok,
        "git_available": git_ok,
        "suite_root": str(SUITE_ROOT),
        "repo_root": str(repo_root),
        "skills_count": skills_count,
        "gates_available": len(GATE_MAP) - len(missing_scripts),
        "missing_gates": missing_scripts,
        "henke_initialized": henke_dir,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1


def _cmd_init(repo_root: Path) -> int:
    henke_dir = repo_root / ".henke"
    (henke_dir / "runs").mkdir(parents=True, exist_ok=True)

    tpl_src = SUITE_ROOT / "docs" / "designs" / "TEMPLATE.md"
    tpl_dst = repo_root / "docs" / "designs" / "TEMPLATE.md"
    if tpl_src.is_file() and not tpl_dst.exists():
        tpl_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tpl_src, tpl_dst)

    if (repo_root / ".git").exists():
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "henke_gate.py"), "baseline", "--force"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    print(f"OK: Suíte Henke inicializada em {repo_root} (.henke/ e docs/designs/ prontos).")
    return 0


def _cmd_route(prompt: str, repo_root: Path, compact: bool = True) -> int:
    from scripts.henke_router import route_request

    if not prompt or not prompt.strip():
        print("ERRO: prompt vazio.", file=sys.stderr)
        return 2
    res = route_request(prompt, repo_root=repo_root, suite_root=SUITE_ROOT)
    if compact:
        compact_payload = {
            "mode": res["mode"],
            "archetype": res["archetype"],
            "confidence": res["confidence"],
            "requires_human_auth": res["requires_human_auth"],
            "skills": [s["name"] for s in res.get("skills", [])],
            "gates_required": res.get("gates_required", []),
            "scope_paths": res.get("scope_paths", []),
        }
        print(json.dumps(compact_payload, ensure_ascii=False))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


_TEST_CMD_RE = re.compile(
    r"\b(pytest|jest|vitest|mocha|cargo\s+test|go\s+test|unittest|npm\s+test|bun\s+test|pnpm\s+test|yarn\s+test)\b",
    re.IGNORECASE,
)

_NON_TEST_CMDS = {
    "echo", "print", "printf", "cat", "type", "git", "dir", "ls", "find", "grep",
    "head", "tail", "curl", "wget", "touch", "rm", "del", "mkdir", "cd", "cp",
    "mv", "copy", "move", "tar", "zip", "unzip", "sed", "awk",
}


def is_test_command(cmd_tokens: list[str], cmd_str: str) -> bool:
    """Verifica se o comando executado é de fato um runner de testes legítimo."""
    tokens = list(cmd_tokens)
    # Desenrola wrappers comuns de shell como cmd.exe /c ou bash -c
    while tokens:
        first_stem = Path(tokens[0]).stem.lower()
        if first_stem == "cmd" and len(tokens) > 2 and tokens[1].lower() in ("/c", "/k"):
            tokens = tokens[2:]
            continue
        if first_stem in ("bash", "sh", "zsh") and len(tokens) > 2 and tokens[1] in ("-c", "-ec"):
            tokens = tokens[2:]
            continue
        break

    if not tokens:
        return False

    exe = Path(tokens[0]).name.lower()
    stem = Path(tokens[0]).stem.lower()

    # Comandos utilitários comuns que recebem 'test' como argumento nunca são test runners
    if stem in _NON_TEST_CMDS or exe in _NON_TEST_CMDS:
        return False

    # Runners diretos
    if stem in ("pytest", "jest", "vitest", "mocha", "ctest", "ava", "tape"):
        return True

    # Python executando unittest / pytest ou script de testes
    if stem in ("python", "python3", "py"):
        toks_lower = [t.lower() for t in tokens[1:]]
        for idx, t in enumerate(toks_lower):
            if t == "-m" and idx + 1 < len(toks_lower):
                mod = toks_lower[idx + 1]
                if mod in ("unittest", "pytest"):
                    return True
            if Path(t).stem in ("pytest", "unittest"):
                return True
            if (
                t.startswith("tests/")
                or t.startswith("tests\\")
                or t.startswith("test/")
                or t.startswith("test\\")
                or t.endswith("_test.py")
                or Path(t).name.startswith("test_")
            ):
                return True

    # Subcomandos específicos: cargo test, go test, dotnet test
    if stem == "cargo" and any(t.lower() == "test" for t in tokens[1:]):
        return True
    if stem == "go" and any(t.lower() == "test" for t in tokens[1:]):
        return True
    if stem == "dotnet" and any(t.lower() == "test" for t in tokens[1:]):
        return True

    # Gerenciadores de pacote: npm test, yarn test, pnpm test, bun test
    if stem in ("npm", "pnpm", "yarn", "bun") and any(t.lower() in ("test", "t") for t in tokens[1:]):
        return True

    # NPX com test runner
    if stem == "npx" and any(t.lower() in ("jest", "vitest", "mocha", "playwright", "cypress") for t in tokens[1:]):
        return True

    # Fallback via regex de comando caso o executável não seja comando utilitário
    sub_cmd_str = " ".join(tokens)
    if _TEST_CMD_RE.search(sub_cmd_str):
        return True

    return False


def _cmd_exec(repo_root: Path, ledger_path: Path, exec_log_path: Path, cmd_tokens: list[str]) -> int:
    if cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]
    if not cmd_tokens:
        print("ERRO: informe o comando a executar após 'henke exec -- <comando>'.", file=sys.stderr)
        return 2

    cmd_str = " ".join(cmd_tokens).strip()
    exec_tokens = list(cmd_tokens)
    if exec_tokens and sys.platform.startswith("win"):
        resolved = shutil.which(exec_tokens[0])
        if resolved:
            exec_tokens[0] = resolved
        elif exec_tokens[0].lower() in ("echo", "dir", "type", "del", "copy", "move", "md", "rd", "cls"):
            exec_tokens = ["cmd.exe", "/c"] + exec_tokens
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    timeout_sec = 180.0

    try:
        res = subprocess.run(
            exec_tokens,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        print(f"ERRO: Comando expirou após {timeout_sec}s: '{cmd_str}'", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERRO ao executar '{cmd_str}': {exc}", file=sys.stderr)
        return 2

    out_text = res.stdout or ""
    err_text = res.stderr or ""
    if out_text:
        sys.stdout.write(out_text)
    if err_text:
        sys.stderr.write(err_text)

    stdout_sha256 = hashlib.sha256((out_text + err_text).encode("utf-8")).hexdigest()
    discovered, passed = parse_test_runner_output(out_text, err_text)

    # Lê prev_hash da última entrada em exec_log.jsonl
    prev_hash = "0" * 64
    if exec_log_path.is_file():
        try:
            lines = [ln.strip() for ln in exec_log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                last_obj = json.loads(lines[-1])
                prev_hash = str(last_obj.get("entry_hash", prev_hash))
        except Exception as exc:
            print(f"ERRO: Log de execução corrompido em '{exec_log_path}': {exc}", file=sys.stderr)
            return 2

    chain_payload = f"{prev_hash}|{cmd_str}|{res.returncode}|{discovered}|{stdout_sha256}"
    entry_hash = hashlib.sha256(chain_payload.encode("utf-8")).hexdigest()

    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cmd": cmd_str,
        "exit": res.returncode,
        "testes_descobertos": discovered,
        "testes_passados": passed,
        "stdout_sha256": stdout_sha256,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
    }
    exec_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(exec_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

    # Atualiza .henke/LEDGER.json atomicamente apenas para runners de testes
    if is_test_command(cmd_tokens, cmd_str) or discovered > 0:
        existing_ledger: list[dict] = []
        if ledger_path.is_file():
            try:
                loaded = json.loads(ledger_path.read_text(encoding="utf-8"))
                if not isinstance(loaded, list):
                    print(f"ERRO: Formato inválido em '{ledger_path}' (esperava lista JSON).", file=sys.stderr)
                    return 2
                existing_ledger = loaded
            except Exception as exc:
                print(f"ERRO: Ledger corrompido em '{ledger_path}': {exc}", file=sys.stderr)
                return 2

        ledger_item = {
            "cmd": cmd_str,
            "exit": res.returncode,
            "testes_descobertos": discovered,
            "testes_passados": passed,
            "stdout_sha256": stdout_sha256,
            "entry_hash": entry_hash,
        }
        existing_ledger.append(ledger_item)
        _atomic_write_json(ledger_path, existing_ledger)

    return res.returncode


def _cmd_gate(gate_name: str, extra_args: list[str], repo_root: Path = Path(".")) -> int:
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]
    script_path = GATE_MAP.get(gate_name)
    if not script_path or not script_path.is_file():
        print(
            f"ERRO: portão desconhecido '{gate_name}'. Disponíveis: {', '.join(sorted(GATE_MAP))}",
            file=sys.stderr,
        )
        return 2
    args = list(extra_args)
    if not args and gate_name in ("hazard", "a11y", "assertless"):
        if gate_name == "assertless" and (repo_root / "tests").is_dir():
            args = [str(repo_root / "tests")]
        else:
            args = [str(repo_root)]
    res = subprocess.run([sys.executable, str(script_path), *args])
    return res.returncode


def _cmd_check(repo_root: Path, spec_path: str | None = None, as_json: bool = False) -> int:
    checks_to_run: list[tuple[str, list[str]]] = [
        ("hazard_scan", [sys.executable, str(GATE_MAP["hazard"]), str(repo_root)]),
    ]
    tests_dir = repo_root / "tests"
    if tests_dir.is_dir():
        checks_to_run.append(("assertless_scan", [sys.executable, str(GATE_MAP["assertless"]), str(tests_dir)]))
    checks_to_run.append(("a11y_scan", [sys.executable, str(GATE_MAP["a11y"]), str(repo_root)]))
    if spec_path:
        checks_to_run.append((
            "contract_closure",
            [sys.executable, str(GATE_MAP["contracts"]), "--repo", str(repo_root), "--spec", spec_path],
        ))

    results: list[dict] = []
    overall_ok = True
    for name, cmd in checks_to_run:
        res = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        passed = res.returncode == 0
        if not passed:
            overall_ok = False
        results.append({
            "gate": name,
            "exit_code": res.returncode,
            "passed": passed,
            "output": ((res.stdout or "") + (res.stderr or "")).strip(),
        })

    if as_json:
        print(json.dumps({
            "repo": str(repo_root),
            "passed": overall_ok,
            "gates": results,
        }, ensure_ascii=False, indent=2))
    else:
        for item in results:
            badge = "PASS" if item["passed"] else "FAIL"
            print(f"[{badge}] {item['gate']} (exit {item['exit_code']})")
            if item["output"]:
                for ln in item["output"].splitlines():
                    print(f"  {ln}")

    return 0 if overall_ok else 1


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["--help"]

    # Subcomandos que encaminham argumentos livres (exec, gate)
    head = raw_argv[0]
    if head == "gate":
        if len(raw_argv) < 2 or raw_argv[1] in ("-h", "--help"):
            print(f"Uso: henke gate <{'|'.join(sorted(GATE_MAP))}> [args...]")
            return 0 if len(raw_argv) >= 2 else 2
        return _cmd_gate(raw_argv[1], raw_argv[2:])

    if head == "exec":
        ap_exec = argparse.ArgumentParser(prog="henke exec", description="Executa comando registrando evidência no ledger.")
        ap_exec.add_argument("--repo", type=Path, default=Path("."), help="Diretório do repositório")
        ap_exec.add_argument("--ledger", type=Path, default=None, help="Caminho para LEDGER.json")
        ap_exec.add_argument("--exec-log", type=Path, default=None, help="Caminho para exec_log.jsonl")
        ap_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="Comando a executar (precedido por --)")
        try:
            ns = ap_exec.parse_args(raw_argv[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        repo_root = ns.repo.resolve()
        ledger_p = ns.ledger.resolve() if ns.ledger else (repo_root / ".henke" / "LEDGER.json")
        exec_log_p = ns.exec_log.resolve() if ns.exec_log else (repo_root / ".henke" / "exec_log.jsonl")
        return _cmd_exec(repo_root, ledger_p, exec_log_p, ns.cmd)

    parser = argparse.ArgumentParser(prog="henke", description="CLI Global Unificada da Suíte Henke (v4.0).")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_doc = sub.add_parser("doctor", help="Verifica ambiente, scripts e skills instaladas")
    p_doc.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_doc.add_argument("--json", action="store_true", help="Saída JSON")

    p_init = sub.add_parser("init", help="Inicializa .henke/, TEMPLATE.md e baseline no repositório")
    p_init.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")

    p_route = sub.add_parser("route", help="Roteia prompt para modo, arquétipo, skills e portões")
    p_route.add_argument("prompt_pos", nargs="?", default=None, help="Prompt do usuário")
    p_route.add_argument("--prompt", dest="prompt_opt", default=None, help="Prompt do usuário (alternativo)")
    p_route.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_route.add_argument("--compact", action="store_true", default=False, help="Saída compacta em 1 linha")
    p_route.add_argument("--full", action="store_true", default=False, help="Saída JSON detalhada")

    p_check = sub.add_parser("check", help="Executa bateria unificada de portões sem abortar no primeiro erro")
    p_check.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_check.add_argument("--spec", default=None, help="Spec de referência para contract_closure")
    p_check.add_argument("--staged", action="store_true", help="Inspeciona estado atual/staged")
    p_check.add_argument("--json", action="store_true", help="Saída em JSON")

    p_app = sub.add_parser("approve", help="Aprova uma run pendente de autorização humana")
    p_app.add_argument("run_file", type=Path, help="Arquivo .henke/runs/<id>.json")
    p_app.add_argument("--approver", default="human", help="Nome do aprovador")

    p_scope = sub.add_parser("scope", help="Gerencia emendas de escopo em uma run")
    scope_sub = p_scope.add_subparsers(dest="scope_cmd", required=True)
    p_amend = scope_sub.add_parser("amend", help="Adiciona arquivo ao escopo da run com justificativa")
    p_amend.add_argument("run_file", type=Path, help="Arquivo .henke/runs/<id>.json")
    p_amend.add_argument("file_rel", help="Caminho relativo do arquivo")
    p_amend.add_argument("--reason", required=True, help="Justificativa técnica (>= 5 chars)")

    p_gf = sub.add_parser("goldfish", help="Executa verificação adversarial em contexto limpo")
    p_gf.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_gf.add_argument("--spec", default=None, help="Spec de referência")
    p_gf.add_argument("--check", action="store_true", help="Executa bateria completa de portões em contexto limpo")
    p_gf.add_argument("--json", action="store_true", help="Saída em JSON")

    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.subcmd == "doctor":
        return _cmd_doctor(args.repo.resolve(), as_json=args.json)

    if args.subcmd == "init":
        return _cmd_init(args.repo.resolve())

    if args.subcmd == "route":
        prompt_val = args.prompt_opt if args.prompt_opt is not None else args.prompt_pos
        if not prompt_val:
            print("ERRO: informe o prompt em 'henke route \"<prompt>\"'.", file=sys.stderr)
            return 2
        is_compact = args.compact or not args.full
        return _cmd_route(prompt_val, args.repo.resolve(), compact=is_compact)

    if args.subcmd == "check" or args.subcmd == "goldfish":
        return _cmd_check(args.repo.resolve(), spec_path=args.spec, as_json=args.json)

    if args.subcmd == "approve":
        from scripts.run_state import main as run_state_main
        return run_state_main(["approve", str(args.run_file), "--approver", args.approver])

    if args.subcmd == "scope" and args.scope_cmd == "amend":
        from scripts.run_state import main as run_state_main
        return run_state_main([
            "amend-scope",
            str(args.run_file),
            "--file",
            args.file_rel,
            "--reason",
            args.reason,
        ])

    return 2


if __name__ == "__main__":
    sys.exit(main())
