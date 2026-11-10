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
  - `henke install-hook`: instala o pre-commit hook nativo de 1 comando no repositório Git

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

from scripts.gate_cache import (
    clear_cache,
    compute_cache_key,
    enforce_ram_limit,
    get_cached_gate,
    store_cached_gate,
)
from scripts.process_runner import run_process

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

    # 3. Python unittest ("Ran N tests in X.XXXs" — usa a última ocorrência)
    m_ut = list(_UNITTEST_RAN_RE.finditer(combined))
    if m_ut:
        ran = int(m_ut[-1].group(1))
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

    from scripts.mission_classifier import classify_repository
    classification = classify_repository(repo_root)
    stacks_detected = [s.name for s in classification.detected_stacks]
    stacks_configured = classification.configured_stacks
    stacks_unconfigured = [s.name for s in classification.unconfigured_stacks]
    active_gates = classification.active_gates

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
        "stacks_detected": stacks_detected,
        "stacks_configured": stacks_configured,
        "stacks_unconfigured": stacks_unconfigured,
        "active_gates": active_gates,
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
        res = run_process(
            exec_tokens,
            cwd=str(repo_root),
            timeout_sec=timeout_sec,
            env=child_env,
        )
    except OSError as exc:
        print(f"ERRO ao executar '{cmd_str}': {exc}", file=sys.stderr)
        return 2

    if res.timed_out:
        print(f"ERRO: Comando expirou após {timeout_sec}s: '{cmd_str}'", file=sys.stderr)
        return 2

    out_text = res.stdout or ""
    err_text = res.stderr or ""
    if out_text:
        sys.stdout.write(out_text)
    if err_text:
        sys.stderr.write(err_text)

    stdout_sha256 = hashlib.sha256((out_text + err_text).encode("utf-8")).hexdigest()
    discovered, passed = parse_test_runner_output(out_text, err_text)
    if res.returncode != 0 and discovered > 0 and passed == discovered:
        passed = 0

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

    no_cache = False
    args: list[str] = []
    for a in extra_args:
        if a == "--no-cache":
            no_cache = True
        elif a == "--clear-cache":
            clear_cache(repo_root)
            # DS-19: limpar e sair 0 SEM executar o portão parecia verde —
            # o operador pedia veredito fresco e recebia nada. Limpa e SEGUE
            # para a execução (equivalente a --no-cache nesta rodada).
            no_cache = True
            print("[OK] Cache de portões limpo — executando portão.")
        else:
            args.append(a)

    if not args and gate_name in ("hazard", "a11y", "assertless"):
        if gate_name == "assertless" and (repo_root / "tests").is_dir():
            args = [str(repo_root / "tests")]
        else:
            args = [str(repo_root)]

    cache_key = compute_cache_key(gate_name, script_path, args, repo_root)
    if not no_cache:
        cached = get_cached_gate(cache_key, repo_root=repo_root)
        if cached is not None:
            code = cached.get("exit_code", cached.get("returncode", 0))
            print(f"[CACHE HIT] {gate_name} (exit {code})")
            if cached.get("stdout"):
                sys.stdout.write(cached["stdout"])
                if not cached["stdout"].endswith("\n"):
                    sys.stdout.write("\n")
            if cached.get("stderr"):
                sys.stderr.write(cached["stderr"])
                if not cached["stderr"].endswith("\n"):
                    sys.stderr.write("\n")
            return code

    res = run_process([sys.executable, str(script_path), *args], cwd=str(repo_root), timeout_sec=120.0)
    out_text = res.stdout or ""
    err_text = res.stderr or ""
    if out_text:
        sys.stdout.write(out_text)
    if err_text:
        sys.stderr.write(err_text)

    store_cached_gate(
        cache_key,
        {
            "gate": gate_name,
            "exit_code": res.returncode,
            "returncode": res.returncode,
            "stdout": out_text,
            "stderr": err_text,
            "passed": res.returncode == 0 and not res.timed_out,
        },
        repo_root=repo_root,
    )
    enforce_ram_limit(500.0, repo_root=repo_root)
    return res.returncode


def _format_manifest_output(scanner_id: str, res) -> str:
    lines = []
    for f in res.findings:
        loc = f.get("location", "?")
        scope = f.get("scope", "")
        lines.append(f"{f.get('rule','?'):<26} {loc}  [{scope}]  {f.get('message','')}")
    lines.append(
        f"{len(res.scanned_files)} file(s) scanned this round, "
        f"{len(res.reused_files)} reused, {len(res.findings)} finding(s)"
    )
    return "\n".join(lines)


def _cmd_check(
    repo_root: Path,
    spec_path: str | None = None,
    as_json: bool = False,
    no_cache: bool = False,
    clear_cache_flag: bool = False,
    staged: bool = False,
    full: bool = False,
    jobs: int = 1,
    gate_timeout: float = 120.0,
) -> int:
    if clear_cache_flag:
        clear_cache(repo_root)
        print("[OK] Cache de portões limpo.")

    import tempfile
    staged_temp_ctx = None
    target_root = repo_root

    if staged:
        # --staged implica --no-cache --full: manifestos, watch e cache desligados.
        no_cache = True
        full = True
        try:
            git_check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if git_check.returncode == 0:
                staged_temp_ctx = tempfile.TemporaryDirectory()
                prefix = (Path(staged_temp_ctx.name).resolve().as_posix()) + "/"
                co_res = subprocess.run(
                    ["git", "checkout-index", "--prefix=" + prefix, "-a", "-f"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                )
                if co_res.returncode == 0:
                    target_root = Path(staged_temp_ctx.name)
                else:
                    print(f"ERRO: Falha ao extrair snapshot do index Git via git checkout-index (exit {co_res.returncode}): {co_res.stderr}", file=sys.stderr)
                    return 2
            else:
                print("ERRO: --staged exige repositório Git.", file=sys.stderr)
                return 2
        except Exception as exc:
            print(f"ERRO: Falha ao extrair index Git para --staged: {exc}", file=sys.stderr)
            return 2

    try:
        from scripts.mission_classifier import classify_repository, prompt_unconfigured_stack

        classification = classify_repository(target_root)

        # In interactive session, prompt user for any unconfigured stacks (only when checking working tree)
        if classification.unconfigured_stacks and not staged:
            is_interactive = sys.stdin.isatty()
            cfg_path = repo_root / ".henke" / "CONFIG.json"
            for unconf in list(classification.unconfigured_stacks):
                prompt_unconfigured_stack(unconf.name, config_path=cfg_path, interactive=is_interactive)
            # Re-classify in case user enabled a stack
            classification = classify_repository(target_root)

        checks_to_run: list[tuple[str, Path, list[str]]] = []
        manifest_scanners: list[str] = []

        # 1. Universal baseline gate: hazard_scan
        checks_to_run.append(("hazard_scan", GATE_MAP["hazard"], [str(target_root)]))
        manifest_scanners.append("hazard")

        # 2. Universal assertless scan: se diretório tests/ existir no snapshot inspecionado
        tests_dir = target_root / "tests"
        if tests_dir.is_dir():
            checks_to_run.append(("assertless_scan", GATE_MAP["assertless"], [str(tests_dir)]))
            manifest_scanners.append("assertless")

        # 3. Universal a11y scan: executado incondicionalmente para evitar regressões
        checks_to_run.append(("a11y_scan", GATE_MAP["a11y"], [str(target_root)]))
        manifest_scanners.append("a11y")

        if spec_path:
            checks_to_run.append((
                "contract_closure",
                GATE_MAP["contracts"],
                ["--repo", str(target_root), "--spec", spec_path],
            ))

        use_manifests = not staged
        effective_no_cache = no_cache or staged
        results: list[dict] = []
        overall_ok = True

        if use_manifests:
            from scripts.metricas.scan_manifest import (
                run_incremental_scan, scanner_specs,
            )
            spec_by_id = {s.scanner_id: s for s in scanner_specs()}
            manifest_results: dict[str, dict] = {}
            for name, _script, _args in checks_to_run:
                scanner_id = name.replace("_scan", "")
                if scanner_id not in manifest_scanners:
                    continue
                res = run_incremental_scan(
                    repo_root, spec_by_id[scanner_id],
                    full=full or effective_no_cache,
                )
                # DS-4: exit 3 = n/a (nada elegível a verificar) é veredito
                # físico honesto — não reprova, mas também não é PASS.
                passed = res.exit_code in (0, 3)
                if not passed:
                    overall_ok = False
                manifest_results[name] = {
                    "gate": name,
                    "exit_code": res.exit_code,
                    "passed": passed,
                    "output": _format_manifest_output(scanner_id, res),
                    "cached": False,
                    "manifest": {
                        "full_scan": res.full_scan,
                        "scanned": list(res.scanned_files),
                        "reused": len(res.reused_files),
                        "invalidated": len(res.invalidated_files),
                        "reasons": list(res.reasons),
                    },
                }
            results.extend(manifest_results[n] for n, *_ in checks_to_run
                           if n in manifest_results)

        # Portões sem adapter (ou staged) executam como processos supervisionados.
        script_gates = [(n, p, a) for n, p, a in checks_to_run
                        if n.replace("_scan", "") not in manifest_scanners
                        or not use_manifests]
        if script_gates:
            from scripts.gate_parallel import (
                GateSpec, aggregate_exit_code, run_gates_parallel,
            )
            uncached: list[tuple[int, str, Path, list[str]]] = []
            cached_results: dict[int, dict] = {}
            for idx, (name, script_path, gate_args) in enumerate(script_gates):
                key = compute_cache_key(name, script_path, gate_args, repo_root)
                cached = None if effective_no_cache else get_cached_gate(key, repo_root=repo_root)
                if cached is not None:
                    exit_code = cached.get("exit_code", cached.get("returncode", 0))
                    passed = exit_code in (0, 3)
                    if not passed:
                        overall_ok = False
                    cached_results[idx] = {
                        "gate": name,
                        "exit_code": exit_code,
                        "passed": passed,
                        "output": cached.get("output", cached.get("stdout", "")).strip(),
                        "cached": True,
                    }
                else:
                    uncached.append((idx, name, script_path, gate_args))
            specs = [
                GateSpec(name, (sys.executable, str(script_path), *gate_args),
                         target_root, timeout_sec=float(gate_timeout))
                for _idx, name, script_path, gate_args in uncached
            ]
            par_results = run_gates_parallel(specs, max_parallel=jobs)
            for (idx, name, script_path, gate_args), gres in zip(uncached, par_results):
                passed = gres.passed
                if not passed:
                    overall_ok = False
                out_text = ((gres.stdout or "") + (gres.stderr or "")).strip()
                if not staged:
                    store_cached_gate(
                        compute_cache_key(name, script_path, gate_args, repo_root),
                        {
                            "gate": name,
                            "exit_code": gres.exit_code,
                            "returncode": gres.exit_code,
                            "stdout": gres.stdout or "",
                            "stderr": gres.stderr or "",
                            "passed": passed,
                            "output": out_text,
                        },
                        repo_root=repo_root,
                    )
                    enforce_ram_limit(500.0, repo_root=repo_root)
                cached_results[idx] = {
                    "gate": name,
                    "exit_code": gres.exit_code,
                    "passed": passed,
                    "output": out_text,
                    "cached": False,
                }
            for idx, (name, _p, _a) in enumerate(script_gates):
                results.append(cached_results[idx])

        if as_json:
            print(json.dumps({
                "repo": str(repo_root),
                "passed": overall_ok,
                "staged": staged,
                "gates": results,
            }, ensure_ascii=False, indent=2))
        else:
            for item in results:
                if item["exit_code"] == 3:
                    badge = "N/A (CACHED)" if item.get("cached") else "N/A"
                elif item.get("cached"):
                    badge = "CACHE HIT" if item["passed"] else "FAIL (CACHED)"
                else:
                    badge = "PASS" if item["passed"] else "FAIL"
                print(f"[{badge}] {item['gate']} (exit {item['exit_code']})")
                if item["output"]:
                    for ln in item["output"].splitlines():
                        print(f"  {ln}")

        if not staged:
            enforce_ram_limit(500.0, repo_root=repo_root)
        return 0 if overall_ok else 1
    finally:
        if staged_temp_ctx is not None:
            try:
                staged_temp_ctx.cleanup()
            except Exception:
                pass


def _cmd_install_hook(repo_root: Path, force: bool = False) -> int:
    from scripts.install_hook import install_hook

    ok, msg = install_hook(repo_root, force=force)
    if ok:
        print(f"[OK] {msg}")
        return 0
    else:
        print(f"ERRO: {msg}", file=sys.stderr)
        return 2 if "não é um repositório git" in msg.lower() else 1




def _cmd_report(repo_root: Path, out_path: Path | None = None) -> int:
    from scripts.report_generator import generate_report

    try:
        res = generate_report(repo_root, output_file=out_path)
        print(f"[OK] Relatório gerado em: {res}")
        return 0
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


def _cmd_test_impact(repo_root: Path, base: str, *, full: bool = False,
                     run: bool = False, as_json: bool = False) -> int:
    from scripts.import_graph import build_import_graph
    from scripts.test_impact import (
        GitDiscoveryError, ImpactSelection, build_unittest_command, select_tests,
    )
    try:
        if full:
            from scripts.test_impact import discover_changed_files
            changed = discover_changed_files(repo_root, base)
            sel = ImpactSelection("full", tuple(changed), (), ("forced_full",), ())
        else:
            graph = build_import_graph(repo_root)
            sel = select_tests(repo_root, base, graph.direct,
                               unresolved=graph.unresolved)
    except GitDiscoveryError as exc:
        print(f"ERRO: descoberta Git falhou: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps({
            "mode": sel.mode,
            "changed_files": list(sel.changed_files),
            "test_files": list(sel.test_files),
            "reasons": list(sel.reasons),
            "unresolved_files": list(sel.unresolved_files),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"mode={sel.mode} tests={len(sel.test_files)} "
              f"changed={len(sel.changed_files)} reasons={','.join(sel.reasons) or '-'}")
        for t in sel.test_files:
            print(f"  selected: {t}")

    if not run:
        return 0
    if sel.mode == "selected" and not sel.test_files:
        print("no_changes: nenhum teste a executar.")
        return 0
    cmd = build_unittest_command(sel, repo_root)
    env = os.environ.copy()
    env["HENKE_TEST_IMPACT"] = "1"
    ledger = repo_root / ".henke" / "LEDGER.json"
    exec_log = repo_root / ".henke" / "exec_log.jsonl"
    return _cmd_exec(repo_root, ledger, exec_log, ["--"] + cmd)


def _cmd_watch(repo_root: Path, *, quiet: bool = False,
               poll: float = 0.75, debounce: float = 0.30) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("ERRO: 'henke watch' é exclusivamente foreground/interativo (TTY exigido).",
              file=sys.stderr)
        return 2
    from scripts.henke_watch import WatchConfig, WatchController
    cfg = WatchConfig(repo_root=repo_root, poll_interval_sec=poll,
                      debounce_sec=debounce, quiet=quiet)
    return WatchController(cfg).run_foreground()


def _cmd_clean(repo_root: Path, *, execute: bool = False,
               restore: str | None = None, purge_expired: bool = False,
               confirm_batch: str | None = None, as_json: bool = False) -> int:
    from scripts.henke_clean import (
        plan_clean, purge_expired_batch, quarantine, restore_batch,
        staging_leftovers,
    )
    if restore is not None:
        if not execute:
            print("ERRO: --restore exige --execute.", file=sys.stderr)
            return 2
        n = restore_batch(repo_root, restore)
        if n == 0:
            # DS-2: "0 restaurados" não é sucesso — todos os itens foram
            # pulados (não-verificados, destino ocupado ou fora da raiz).
            print(f"[FALHA] 0 item(ns) restaurado(s) do batch {restore} "
                  "(veja clean_log.jsonl).", file=sys.stderr)
            return 1
        print(f"[OK] {n} item(ns) restaurado(s) do batch {restore}.")
        return 0
    if purge_expired:
        if not execute or not confirm_batch:
            print("ERRO: --purge-expired exige --execute e --confirm-batch <id>.",
                  file=sys.stderr)
            return 2
        try:
            n = purge_expired_batch(repo_root, confirm_batch,
                                    now=datetime.now(timezone.utc))
        except ValueError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        print(f"[OK] batch {confirm_batch} purgado ({n}).")
        return 0
    candidates = plan_clean(repo_root)
    leftovers = staging_leftovers(repo_root)
    if as_json:
        print(json.dumps({
            "candidates": [
                {"source": str(c.source), "kind": c.kind, "size_bytes": c.size_bytes}
                for c in candidates
            ],
            "staging_leftovers": [str(p) for p in leftovers],
            "dry_run": not execute,
        }, ensure_ascii=False, indent=2))
    else:
        for c in candidates:
            print(f"  [{c.kind}] {c.source} ({c.size_bytes} bytes)")
        for p in leftovers:
            print(f"  [staging-leftover] {p}")
        print(f"{len(candidates)} candidato(s); "
              f"{'EXECUTANDO quarentena' if execute else 'dry-run (use --execute para mover)'}")
    if not execute:
        return 0
    if not candidates:
        return 0
    batch = quarantine(repo_root, candidates)
    print(f"[OK] batch {batch.batch_id}: {len(batch.items)} item(ns) em quarentena "
          f"(expira {batch.expires_at}).")
    return 0


def _cmd_session(args) -> int:
    import scripts.henke_session as hs
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
    p_check.add_argument("--no-cache", action="store_true", help="Ignora cache e força re-execução dos portões")
    p_check.add_argument("--clear-cache", action="store_true", help="Limpa o cache de portões antes de executar")
    p_check.add_argument("--full", action="store_true", help="Força scan integral (regrava manifesto após sucesso)")
    p_check.add_argument("--jobs", type=int, default=1, help="Portões de processo em paralelo (1-4)")
    p_check.add_argument("--gate-timeout", type=float, default=120.0, help="Timeout por portão em segundos")
    p_check.add_argument("--state-dir", type=Path, default=None, help="Diretório de estado absoluto (equivale a HENKE_STATE_DIR)")

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
    p_gf.add_argument("--no-cache", action="store_true", help="Ignora cache e força re-execução dos portões")

    p_install = sub.add_parser("install-hook", help="Instala o pre-commit hook nativo no repositório Git")
    p_install.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_install.add_argument("--force", action="store_true", help="Sobrescreve hook pré-existente sem criar backup .legacy")

    
    p_report = sub.add_parser("report", help="Gera o dashboard visual local em HTML autocontido")
    p_report.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_report.add_argument("--out", type=Path, default=None, help="Caminho do arquivo HTML de saída (padrão: .henke/report.html)")

    p_ti = sub.add_parser("test-impact", help="Seleciona arquivos de teste pelo diff (TIA conservativa)")
    p_ti.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_ti.add_argument("--base", required=True, help="Ref Git base para o diff")
    p_ti.add_argument("--full", action="store_true", help="Sempre suíte completa (CI/pré-merge)")
    p_ti.add_argument("--run", action="store_true", help="Executa o comando selecionado via supervisor")
    p_ti.add_argument("--json", action="store_true", help="Saída em JSON")

    p_watch = sub.add_parser("watch", help="Modo watch foreground interativo (exige TTY)")
    p_watch.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_watch.add_argument("--quiet", action="store_true", help="Suprime repetições verdes idênticas")
    p_watch.add_argument("--poll-interval", type=float, default=0.75, help="Intervalo de polling (s)")
    p_watch.add_argument("--debounce", type=float, default=0.30, help="Janela de debounce (s)")

    p_clean = sub.add_parser("clean", help="Faxina allowlist: dry-run padrão; --execute move para quarentena")
    p_clean.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_clean.add_argument("--execute", action="store_true", help="Executa a movimentação para quarentena")
    p_clean.add_argument("--restore", default=None, help="Batch-id para restaurar da quarentena")
    p_clean.add_argument("--purge-expired", action="store_true", help="Purgar batch expirado (exige --confirm-batch)")
    p_clean.add_argument("--confirm-batch", default=None, help="Confirmação humana do batch-id a purgar")
    p_clean.add_argument("--json", action="store_true", help="Saída em JSON")

    p_session = sub.add_parser("session", help="Sentinel de sessão com rastreamento de artefatos")
    session_sub = p_session.add_subparsers(dest="session_cmd", required=True)
    p_sstart = session_sub.add_parser("start", help="Inicia sessão e imprime session_id")
    p_sstart.add_argument("--agent", required=True, help="Nome do agente")
    p_sstart.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")
    p_sstart.add_argument("--run-id", required=True, help="ID da run associada")
    p_shb = session_sub.add_parser("heartbeat", help="Renova janela de heartbeat (+1h)")
    p_shb.add_argument("session_id", help="ID da sessão")
    p_shb.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")
    p_send = session_sub.add_parser("end", help="Encerra a sessão (state=closed)")
    p_send.add_argument("session_id", help="ID da sessão")
    p_send.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")
    p_sgc = session_sub.add_parser("gc", help="Reapa sentinels expirados e roda ephemeral_gc")
    p_sgc.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")

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
        state_dir_arg = getattr(args, "state_dir", None)
        if state_dir_arg is not None:
            if not Path(state_dir_arg).is_absolute():
                print("ERRO: --state-dir deve ser caminho absoluto.", file=sys.stderr)
                return 2
            os.environ["HENKE_STATE_DIR"] = str(Path(state_dir_arg).resolve())
        return _cmd_check(
            args.repo.resolve(),
            spec_path=args.spec,
            as_json=args.json,
            no_cache=getattr(args, "no_cache", False),
            clear_cache_flag=getattr(args, "clear_cache", False),
            staged=getattr(args, "staged", False),
            full=getattr(args, "full", False),
            jobs=getattr(args, "jobs", 1),
            gate_timeout=getattr(args, "gate_timeout", 120.0),
        )

    if args.subcmd == "test-impact":
        return _cmd_test_impact(args.repo.resolve(), args.base,
                                full=args.full, run=args.run, as_json=args.json)

    if args.subcmd == "watch":
        return _cmd_watch(args.repo.resolve(), quiet=args.quiet,
                          poll=args.poll_interval, debounce=args.debounce)

    if args.subcmd == "clean":
        return _cmd_clean(args.repo.resolve(), execute=args.execute,
                          restore=args.restore, purge_expired=args.purge_expired,
                          confirm_batch=args.confirm_batch, as_json=args.json)

    if args.subcmd == "session":
        return _cmd_session(args)

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

    if args.subcmd == "install-hook":
        return _cmd_install_hook(args.repo.resolve(), force=args.force)

    
    if args.subcmd == "report":
        return _cmd_report(args.repo.resolve(), out_path=args.out.resolve() if args.out else None)

    return 2


if __name__ == "__main__":
    sys.exit(main())
