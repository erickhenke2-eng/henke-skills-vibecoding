#!/usr/bin/env python3
"""
henke_cli.py — CLI Global Unificada da Suíte Henke (PROTOCOLO v4.0).

Resolve `SUITE_ROOT` automaticamente a partir da própria localização física do script,
eliminando a dependência de CWD dos 14 portões mecânicos e fornecendo:
  - `henke doctor`   : diagnóstico do ambiente, scripts e catálogo de skills
  - `henke init`     : inicialização de `.henke/`, `docs/designs/TEMPLATE.md` e baseline Git
  - `henke route`    : roteamento determinístico de prompt (`--compact` por padrão)
  - `henke exec`     : runner wrapper que executa testes reais e grava `.henke/exec_log.jsonl`
                       (hash-chain SHA-256) e `.henke/LEDGER.json` com `stdout_sha256`;
                       `--timeout N` > `.henke/PROJECT.json:timeout_sec` > fallback 180s
  - `henke gate`     : despacho direto para qualquer portão mecânico pelo nome curto
  - `henke check`    : bateria agregada de portões sem abortar no primeiro erro;
                       `--staged` restringe hazard_scan/assertless_scan aos arquivos de
                       `git diff --cached --name-only` (a11y_scan segue no repo inteiro)
  - `henke run`      : wrapper fino sobre `scripts/run_state.py`
                       (init|record-gate|amend-scope|approve|verify) + `list` nativo
  - `henke report`   : relatório markdown rastreável de uma run (`scripts/henke_report.py`)
  - `henke status`   : lista runs de `.henke/runs/` com outcome DERIVADO (não declarado)
  - `henke setup`    : detecção de projeto (`scripts/henke_detect.py`) + stubs `.henke/`
                       criados sem sobrescrever os existentes
  - `henke auto`     : modo automático via `scripts/henke_auto.py` (import lazy; exit 2
                       honesto quando o módulo não está instalado)
  - `henke goldfish` : com `--driver <cmd>` gera envelope de revisão isolada (spec+diff,
                       SEM transcript) em `.henke/runs/<id>.goldfish.json` e sai 0;
                       sem `--driver` grava `review_status=goldfish_unavailable` na run
                       (quando `--run` informado) e sai 2 — nunca despacha para `check`
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
import math
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

# EXEC-WHICH-CWD (banca v11 A4): shutil.which() no processo PAI resolve o
# nome com CWD no Windows (py<3.12 sempre; py>=3.12 só se a var estiver no
# ambiente). Sem isso, `henke exec -- pytest` num repo com pytest.cmd
# plantado executa o impostor e minta evidência verde "honesta" na cadeia.
if sys.platform.startswith("win"):
    os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")

SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = SCRIPTS_DIR.parent
METRICAS_DIR = SCRIPTS_DIR / "metricas"

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from scripts._paths import suite_root as _suite_root  # noqa: E402

SUITE_ROOT = _suite_root()  # checkout OU <prefix>/henke_data (wheel)

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


# Conteúdo EXATO do stub .henke/POLICY.json criado por `henke setup`
_POLICY_STUB_JSON = (
    '{"policy":"balanced","always_ask":["publish","destructive","metaconfig"],'
    '"auto_approve":["non-publish"],"allowed_cmds":[]}'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    env=_scrub_env(repo_root))
    git_ok = git_res.returncode == 0
    missing_scripts = [name for name, p in sorted(GATE_MAP.items()) if not p.is_file()]
    expected_skills = 58
    skills_count = len(list(SUITE_ROOT.glob("henke-*/SKILL.md")))
    henke_dir = (repo_root / ".henke").is_dir()

    from scripts.mission_classifier import classify_repository
    classification = classify_repository(repo_root)
    stacks_detected = [s.name for s in classification.detected_stacks]
    stacks_configured = classification.configured_stacks
    stacks_unconfigured = [s.name for s in classification.unconfigured_stacks]
    active_gates = classification.active_gates

    report = {
        "status": "ok" if (py_ok and git_ok and not missing_scripts
                         and skills_count >= expected_skills) else "degraded",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_ok": py_ok,
        "git_available": git_ok,
        "suite_root": str(SUITE_ROOT),
        "repo_root": str(repo_root),
        "skills_count": skills_count,
        "skills_expected": expected_skills,
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


def _scrub_env(repo: Path | None = None) -> dict:
    """Env sanitizado para subprocessos de custódia (A6 banca v16):
    _cmd_check/_cmd_init/_derive_outcome/doctor herdavam PYTHONPATH/GIT_*/
    PATH integral — sitecustomize plantado fabricava vereditos.
    `repo` ancora o filtro de PATH (Delta-F4) — sem ele, entradas de PATH
    dentro do repo auditado sobrevivem e shims mintam vereditos."""
    from scripts.state_paths import sanitized_child_env
    e = sanitized_child_env(repo)
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e

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
        env=_scrub_env(repo_root))

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


_AL_MOD_CACHE = None


def _audit_ledger_mod():
    """audit_ledger por path explícito (fonte única de is_test_cmd — A13 v16)."""
    global _AL_MOD_CACHE
    if _AL_MOD_CACHE is not None:
        return _AL_MOD_CACHE
    try:
        import importlib.util as _ilu
        _p = Path(__file__).resolve().with_name("audit_ledger.py")
        _sp = _ilu.spec_from_file_location("henke_audit_ledger2", str(_p))
        _m = _ilu.module_from_spec(_sp) if _sp else None
        if _m is not None:
            _sp.loader.exec_module(_m)  # type: ignore[union-attr]
        _AL_MOD_CACHE = _m
    except Exception:
        _AL_MOD_CACHE = None
    return _AL_MOD_CACHE


def is_test_command(cmd_tokens: list[str], cmd_str: str) -> bool:
    # A13 (banca v16): mint×audit divergentes wedgiavam o LEDGER —
    # audit_ledger.is_test_cmd é a fonte única de verdade.
    _al = _audit_ledger_mod()
    if _al is not None and hasattr(_al, "is_test_cmd"):
        return bool(_al.is_test_cmd(cmd_str))
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

    # Gerenciadores de pacote: npm test, yarn test, pnpm test, bun test.
    # Mesma borda do audit_ledger (A2 v15): 'npm run test:unit' é teste,
    # 'npm run latest'/'attest' NÃO é — wedge nas duas direções eliminado.
    if stem in ("npm", "pnpm", "yarn", "bun") and any(
            re.fullmatch(r"t|test(?:[.:_/-].*)?|teste\w*", t.lower())
            for t in tokens[1:]):
        return True

    # NPX com test runner
    if stem == "npx" and any(t.lower() in ("jest", "vitest", "mocha", "playwright", "cypress") for t in tokens[1:]):
        return True

    # Fallback via regex de comando caso o executável não seja comando utilitário
    sub_cmd_str = " ".join(tokens)
    if _TEST_CMD_RE.search(sub_cmd_str):
        return True

    return False


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Derruba `proc` e todos os descendentes — subprocess.run(timeout)
    mata só o filho direto e abandona netos vivos (DS-12)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.kill()
            except OSError:
                pass
    else:
        # start_new_session: o filho é líder do próprio grupo — killpg só
        # atinge a árvore dele, nunca o nosso (DS-15 do relatório wave6).
        try:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass


def _run_capture_tree(argv: list[str], *, cwd: Path, env: dict,
                      timeout_sec: float) -> tuple[int, bytes, bytes, bool]:
    """Popen com kill de árvore no timeout. Retorna (rc, stdout, stderr,
    timed_out) — o chamador registra o timeout no ledger em vez de sair
    sem rastro (DS-12)."""
    if os.name == "nt":
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    else:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_tree(proc)
        out, err = proc.communicate()
    return proc.returncode, out or b"", err or b"", timed_out


def _resolve_exec_timeout(repo_root: Path, cli_timeout: float | None) -> float:
    """Resolve o teto de timeout do `henke exec`.

    Ordem de precedência honesta:
      1. flag `--timeout N` (quando > 0);
      2. campo `timeout_sec` de `.henke/PROJECT.json` (quando numérico e > 0);
      3. fallback fixo de 180s.
    """
    # B1 (banca v13): --timeout nan/inf passava nas checagens e o subprocesso
    # nunca expirava — finito+positivo em ambas as fontes.
    # A13 (banca v14): timeout controlado pelo repo podia ser 1e9 — o stage
    # pendurava ~31 anos. Teto honesto de 1h.
    _CAP = 3600.0
    if (cli_timeout is not None and math.isfinite(cli_timeout)
            and cli_timeout > 0):
        return min(float(cli_timeout), _CAP)
    project_json = repo_root / ".henke" / "PROJECT.json"
    try:
        pdata = json.loads(project_json.read_text(encoding="utf-8"))
        val = pdata.get("timeout_sec") if isinstance(pdata, dict) else None
        if (isinstance(val, (int, float)) and not isinstance(val, bool)
                and math.isfinite(val) and val > 0):
            return min(float(val), _CAP)
    except Exception:
        pass  # PROJECT.json ausente/inválido -> fallback silencioso para 180s
    return 180.0


def _cmd_exec(
    repo_root: Path,
    ledger_path: Path,
    exec_log_path: Path,
    cmd_tokens: list[str],
    cli_timeout: float | None = None,
) -> int:
    if cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]
    if not cmd_tokens:
        print("ERRO: informe o comando a executar após 'henke exec -- <comando>'.", file=sys.stderr)
        return 2
    if cli_timeout is not None and cli_timeout <= 0:
        print("ERRO: --timeout deve ser um número positivo de segundos.", file=sys.stderr)
        return 2

    cmd_str = " ".join(cmd_tokens).strip()
    exec_tokens = list(cmd_tokens)
    # M5/H3 (banca v13): PYTHONPATH/PYTEST_*/NODE_OPTIONS/GIT_* hereditários
    # subvertiam o tooling que produz evidência; PATH com '.', entrada do repo
    # ou CWD implícito (which() em py<3.12) resolvia binário plantado.
    # A3 (banca v14): Windows entrega a chave como 'Path' — escrever 'PATH'
    # criava duplicata e o CreateProcess lia a ORIGINAL envenenada.
    _raw_env = (os.environ.items() if os.name != "nt"
                else ((k.upper(), v) for k, v in os.environ.items()))
    # A3 (banca v15): loader hooks de OUTROS toolchains também mintavam
    # saída "honesta" — NODE_PATH/GOFLAGS/RUSTC_*/JAVA_*/PHPRC/etc.
    child_env = {k: v for k, v in _raw_env
                 if not (k.startswith("GIT_") or k.startswith("PYTEST_")
                         or k.startswith("COVERAGE_") or k.startswith("PYTHON")
                         or k.startswith("LD_") or k.startswith("DYLD_")
                         or k.startswith("NPM_CONFIG_") or k.startswith("npm_config_")
                         or k in ("NODE_OPTIONS", "NODE_PATH", "VIRTUAL_ENV",
                                  "BASH_ENV", "ENV", "ZDOTDIR", "PERL5OPT",
                                  "PERL5LIB", "PERL5DB", "RUBYOPT", "RUBYLIB",
                                  "BUNDLE_GEMFILE", "DOTNET_STARTUP_HOOKS",
                                  "DOTNET_ADDITIONAL_DEPS", "GOFLAGS",
                                  "RUSTC_WRAPPER", "RUSTFLAGS", "JAVA_TOOL_OPTIONS",
                                  "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "CLASSPATH",
                                  "MAVEN_OPTS", "GRADLE_OPTS", "SBT_OPTS",
                                  "PHPRC", "PHP_INI_SCAN_DIR", "PWD", "OLDPWD"))}
    _rr: Path | None = None
    try:
        _rr = repo_root.resolve()
        _safe_path = os.pathsep.join(
            d for d in os.environ.get("PATH", "").split(os.pathsep)
            if d.strip() and not (
                lambda _rd: _rd == _rr or _rr in _rd.parents
            )((repo_root / d).resolve() if not Path(d).is_absolute() else Path(d).resolve()))
        # A3 (banca v14): PATH vazio de efeito = herda envenenado — sempre
        # escreve (vazio é melhor que envenenado).
        child_env["PATH"] = _safe_path
    except (OSError, ValueError):
        pass
    if exec_tokens:
        # A2 (banca v14): recusa repo-resident era Windows-only — execvp no
        # POSIX resolvia o plantado por PATH. `which` roda em toda plataforma.
        resolved = shutil.which(exec_tokens[0], path=child_env.get("PATH"))
        if resolved:
            # H3 (banca v13): o resolvido dentro do repo é o impostor
            # plantado — recusar, nunca executar.
            try:
                _res = Path(resolved).resolve()
                if _rr is not None and (_res == _rr or _rr in _res.parents):
                    print(
                        f"ERRO: '{exec_tokens[0]}' resolve para binário dentro "
                        f"do repo ({_res}) — recusado (path poisoning).",
                        file=sys.stderr)
                    return 2
            except OSError:
                pass
            exec_tokens[0] = resolved
        elif (os.name == "nt" and not resolved
              and exec_tokens[0].lower() in ("echo", "dir", "type", "cls")):
            # A6 (banca v14): `cmd /c` re-parseia o rabo — `echo ok & calc`
            # executava o segundo comando mesmo com builtin "inocente".
            if any(re.search(r"[&|<>^%\"]", str(t)) for t in exec_tokens):
                print("ERRO: metacaractere de shell (&|<>) em comando builtin "
                      "— recusado (cmd /c re-parseia o rabo).", file=sys.stderr)
                return 2
            exec_tokens = ["cmd.exe", "/c"] + exec_tokens
        elif os.name == "nt" and not resolved:
            # A6 (banca v15): CreateProcess busca pelo PATH do PAI, não pelo
            # env sanitizado — which-miss executava o plantado via PATH pai.
            print(f"ERRO: '{exec_tokens[0]}' não resolve no PATH sanitizado "
                  "— recusado (binário poderia ser plantado no repo).",
                  file=sys.stderr)
            return 2
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    # F-07 (banca v8): CWD poisoning — binário plantado no worktree não pode
    # ser resolvido como o comando pedido (`henke exec -- npm` num repo com
    # npm.cmd forjado executaria o plantado).
    child_env["NoDefaultCurrentDirectoryInExePath"] = "1"
    timeout_sec = _resolve_exec_timeout(repo_root, cli_timeout)

    try:
        # DS-12: subprocess.run(timeout) abandona netos — Popen + nova sessão
        # + kill de árvore; o timeout é registrado no ledger abaixo (exit 124),
        # não some num early-return sem rastro.
        rc, raw_out, raw_err, timed_out = _run_capture_tree(
            exec_tokens, cwd=repo_root, env=child_env, timeout_sec=timeout_sec)
    except OSError as exc:
        print(f"ERRO ao executar '{cmd_str}': {exc}", file=sys.stderr)
        return 2

    # B9 (banca v13): sha cobria texto com errors="replace" — byte-streams
    # distintos colidiam em U+FFFD. Hash sobre os BYTES capturados.
    out_text = raw_out.decode("utf-8", errors="replace")
    err_text = raw_err.decode("utf-8", errors="replace")
    if out_text:
        sys.stdout.write(out_text)
    if err_text:
        sys.stderr.write(err_text)

    stdout_sha256 = hashlib.sha256(raw_out + raw_err).hexdigest()
    # DS-1: `henke exec -- echo "10 passed"` gravava 10/10 no exec_log —
    # contagem só vale quando o COMANDO é um runner de teste reconhecido;
    # saída de comando arbitrário nunca vira evidência de teste.
    discovered = passed = 0
    if is_test_command(cmd_tokens, cmd_str):
        discovered, passed = parse_test_runner_output(out_text, err_text)
        # w7: rc!=0 com "todos passaram" é suspeito — runner íntegro que falha
        # reporta falhas; zera em vez de creditar um placar incoerente.
        if rc != 0 and discovered > 0 and passed == discovered:
            passed = 0

    # Lê prev_hash + append da entrada + LEDGER.json sob o MESMO lock
    # (read-modify-write atômico — concorrência não pode ver estado intermediário).
    from scripts import run_state as _rs
    # B9 (banca v9): a âncora local da ponta é chaveada por REPO — um
    # --exec-log fora de .henke/exec_log.jsonl não pode ancorar o tip do repo
    # (ancoraria a ponta de outro arquivo, corrompendo a custódia canônica).
    _canonical_log = (repo_root / ".henke" / "exec_log.jsonl").resolve()
    _is_canonical = exec_log_path.resolve() == _canonical_log
    try:
        with _rs._file_lock(exec_log_path):
            prev_hash = "0" * 64
            _last = None
            # A3 (banca v16): log DELETADO regenerava gênese e re-ancorava a
            # ponta — história inteira apagada sem vestígio. Se a âncora
            # diz que já existiu execução, gênese nova é forja.
            if _is_canonical and not (
                    exec_log_path.is_file() and exec_log_path.stat().st_size):
                _tip = _rs.load_exec_tip(repo_root)
                if _tip is not None:
                    print("ERRO: exec_log ausente/vazio mas âncora de ponta "
                          "existe — log foi apagado/truncado. Recusado; "
                          "restaure o log ou reset explícito.", file=sys.stderr)
                    return 2
            if exec_log_path.is_file():
                # A1 (banca v9): ANTES de encadear, a cadeia existente tem que
                # verificar — append sobre um log plantado lavaria a âncora da
                # ponta com uma execução legítima e tornaria a história
                # fabricada "ancorada" para sempre. Log fora do caminho
                # canônico não tem âncora: verifica-se só a cadeia interna.
                # M4 (banca v15): fallback não-qualificado resolvia
                # `audit_ledger.py` PLANTADO no sys.path → verify fake.
                # Carrega por path explícito (mesma regra do _rs_tip).
                try:
                    import importlib.util as _ilu
                    _al_p = Path(__file__).resolve().with_name("audit_ledger.py")
                    _spec = _ilu.spec_from_file_location(
                        "henke_audit_ledger", str(_al_p))
                    _al_mod = _ilu.module_from_spec(_spec) if _spec else None
                    if _al_mod is not None:
                        _spec.loader.exec_module(_al_mod)  # type: ignore[union-attr]
                    _vlc = getattr(_al_mod, "verify_log_chain", None)
                except Exception:
                    _vlc = None  # type: ignore[assignment]
                # A4 (banca v10): import falho no caminho CANÔNICO não pode
                # pular a checagem em silêncio — fail closed.
                if _is_canonical and _vlc is None:
                    print("ERRO: verify_log_chain indisponível — append no "
                          "log canônico recusado.", file=sys.stderr)
                    return 2
                if _vlc is not None and _is_canonical:
                    _chain_errors, _last = _vlc(exec_log_path)
                    if _chain_errors:
                        for _e in _chain_errors[:5]:
                            print(f"ERRO: {_e}", file=sys.stderr)
                        print(
                            "ERRO: exec_log existente falhou a verificação de "
                            "integridade — append recusado (remova o log "
                            "corrompido explicitamente se for recomeçar).",
                            file=sys.stderr,
                        )
                        return 2
                try:
                    lines = [
                        ln.strip()
                        for ln in exec_log_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]
                    if lines:
                        last_obj = json.loads(lines[-1])
                        prev_hash = str(last_obj.get("entry_hash", prev_hash))
                    # A4 (banca v12): TOCTOU verify→append — driver truncava o
                    # log para prefixo válido entre _vlc() e a releitura, e o
                    # append 'lavava' entradas reais do rabo. A ponta relida
                    # DEVE ser a mesma que o verificador aprovou.
                    if (_is_canonical and _vlc is not None
                            and _last is not None and prev_hash != _last):
                        print("ERRO: exec_log mudou entre a verificação da "
                              "cadeia e o append (ponta divergente) — "
                              "append recusado.", file=sys.stderr)
                        return 2
                except Exception as exc:
                    print(f"ERRO: Log de execução corrompido em '{exec_log_path}': {exc}", file=sys.stderr)
                    return 2

            chain_payload = f"{prev_hash}|{cmd_str}|{rc}|{discovered}|{stdout_sha256}"
            entry_hash = hashlib.sha256(chain_payload.encode("utf-8")).hexdigest()

            log_entry = {
                "timestamp": _now_iso(),
                "cmd": cmd_str,
                "exit": rc if not timed_out else 124,
                "timed_out": timed_out,
                "testes_descobertos": discovered,
                "testes_passados": passed,
                "stdout_sha256": stdout_sha256,
                "prev_hash": prev_hash,
                "entry_hash": entry_hash,
            }
            # A1 (banca v11): valida/carrega o ledger ANTES do append —
            # `return 2` aqui não precisa de rollback porque a linha nova
            # ainda não existe. Antes, ledger corrompido wedgiava o exec_log
            # para sempre (linha extra sem tip-update).
            _ledger_pre: list[dict] | None = None
            if is_test_command(cmd_tokens, cmd_str):
                _ledger_pre = []
                if ledger_path.is_file():
                    try:
                        loaded = json.loads(ledger_path.read_text(encoding="utf-8"))
                        if not isinstance(loaded, list):
                            print(f"ERRO: Formato inválido em '{ledger_path}' (esperava lista JSON).", file=sys.stderr)
                            return 2
                        _ledger_pre = loaded
                    except Exception as exc:
                        print(f"ERRO: Ledger corrompido em '{ledger_path}': {exc}", file=sys.stderr)
                        return 2

            try:
                exec_log_path.parent.mkdir(parents=True, exist_ok=True)
                _pre_size = exec_log_path.stat().st_size if exec_log_path.is_file() else 0
                with open(exec_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    # A10 (banca v14): append sem fsync — crash entre a linha e
                    # o tip wedgiava a cadeia pra sempre.
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                print(f"ERRO: falha ao escrever exec_log: {exc}", file=sys.stderr)
                return 2
            try:
                # Atualiza .henke/LEDGER.json apenas quando o COMANDO é de testes —
                # qualquer saída contendo "N tests" (ex.: um README impresso por cat)
                # não pode entrar no ledger sem ser um runner real (ALTO-6 banca).
                # M6 (banca v15): entrada {tests:0} era gravada ANTES do check
                # `discovered<=0` — run 0-testes envenenava o ledger pra sempre.
                if _ledger_pre is not None and discovered > 0:
                    ledger_item = {
                        "cmd": cmd_str,
                        "exit": rc,
                        "testes_descobertos": discovered,
                        "testes_passados": passed,
                        "stdout_sha256": stdout_sha256,
                        "entry_hash": entry_hash,
                    }
                    _atomic_write_json(ledger_path, _ledger_pre + [ledger_item])

                # Âncora LOCAL da ponta da cadeia (F-13): a cadeia prova só
                # consistência interna — truncar/reescrever o rabinho do jsonl
                # produzia cadeia "válida". Com a ponta fora do repo, qualquer
                # reescrita diverge do tip gravado nesta máquina. Só o caminho
                # canônico ancora (B9).
                if _is_canonical:
                    _rs.record_exec_tip(repo_root, entry_hash)
            except (OSError, ValueError) as exc:
                # A5 (banca v10): falha pós-append deixava rabo não-ancorado e
                # wedgiava o log para sempre — rollback trunca a linha nova
                # e restaura o ledger (senão a entrada fica órfã).
                try:
                    with open(exec_log_path, "r+b") as f:
                        f.truncate(_pre_size)
                except OSError:
                    pass
                if _ledger_pre is not None:
                    try:
                        _atomic_write_json(ledger_path, _ledger_pre)
                    except OSError:
                        pass
                print(f"ERRO: exec_log revertido após falha de custódia: {exc}",
                      file=sys.stderr)
                return 2
    except TimeoutError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    # Zero testes descobertos NUNCA pode ser tratado como verde — "Ran 0 tests" é
    # tão perigoso quanto suíte ausente (falso-verde do audit do Kimi).
    if is_test_command(cmd_tokens, cmd_str) and discovered <= 0:
        print(
            "ERRO: comando de teste executado mas 0 testes descobertos — "
            "a run não pode fechar sem teste real.",
            file=sys.stderr,
        )
        return 2

    if timed_out:
        # timeout registrado no exec_log (exit 124 + timed_out) acima —
        # infra: sai 2, não o rc do filho (que pode nem ter terminado).
        print(f"ERRO: Comando expirou após {timeout_sec}s: '{cmd_str}'",
              file=sys.stderr)
        return 2
    return rc


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
            from scripts.state_paths import sanitized_child_env
            _git_env = sanitized_child_env(repo_root)
            git_check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                env=_git_env,
            )
            if git_check.returncode == 0 and git_check.stdout.strip() == "true":
                # Merge-Audit 3: checkout-index roda filter.*.smudge de .git/config
                # (RCE no pre-commit). write-tree + archive não passam pelo
                # machinery de filtros de conteúdo.
                staged_temp_ctx = tempfile.TemporaryDirectory()
                # Banca-A 6: write-tree escreveria objetos novos em
                # .git/objects do repo auditado — redireciona para tempdir
                # com os objetos existentes como alternates.
                obj_dir = Path(staged_temp_ctx.name) / "objects"
                obj_dir.mkdir()
                _wt_env = dict(_git_env)
                _wt_env["GIT_OBJECT_DIRECTORY"] = str(obj_dir)
                # Delta-F16: `.git` pode ser arquivo (linked worktree/submodule)
                # — o dir real de objetos vem do git, não do path presumido.
                _gdir = subprocess.run(
                    ["git", "rev-parse", "--git-common-dir"],
                    cwd=str(repo_root), capture_output=True, text=True,
                    env=_git_env)
                if _gdir.returncode == 0 and _gdir.stdout.strip():
                    _gdir_p = Path(_gdir.stdout.strip())
                    if not _gdir_p.is_absolute():
                        _gdir_p = (repo_root / _gdir_p).resolve()
                    _objects = (_gdir_p / "objects").resolve()
                else:
                    _objects = (repo_root / ".git" / "objects").resolve()
                _wt_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(_objects)
                wt_res = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false",
                     "write-tree"],
                    cwd=str(repo_root),
                    capture_output=True,
                    text=True,
                    env=_wt_env,
                )
                co_res = wt_res
                if wt_res.returncode == 0:
                    tree = wt_res.stdout.strip()
                    archive_path = Path(staged_temp_ctx.name) / "index.zip"
                    ar_res = subprocess.run(
                        ["git", "archive", "--format=zip",
                         "-o", str(archive_path), tree],
                        cwd=str(repo_root),
                        capture_output=True,
                        text=True,
                        # Delta-F1: archive precisa enxergar os objetos
                        # escritos no tempdir pelo write-tree — mesmo env.
                        env=_wt_env,
                    )
                    if ar_res.returncode == 0:
                        import zipfile
                        extract_dir = Path(staged_temp_ctx.name) / "tree"
                        extract_dir.mkdir()
                        extracted: set[str] = set()
                        with zipfile.ZipFile(archive_path) as zf:
                            for info in zf.infolist():
                                rel = info.filename.lstrip("/")
                                if ".." in rel.split("/"):
                                    continue  # path traversal não extraído
                                zf.extract(info, extract_dir)
                                extracted.add(rel)
                        archive_path.unlink()
                        # Delta-F2: `export-ignore`/`export-subst` de
                        # .gitattributes omitiriam/reescreveriam arquivos
                        # staged — o conjunto extraído TEM que bater com o
                        # ls-tree do índice (fail-closed em divergência).
                        lt_res = subprocess.run(
                            ["git", "ls-tree", "-r", "--name-only", tree],
                            cwd=str(repo_root), capture_output=True,
                            text=True, env=_wt_env)
                        listed = (set(lt_res.stdout.split())
                                  if lt_res.returncode == 0 else set())
                        missing = sorted(p for p in listed
                                         if p + "/" not in extracted
                                         and p not in extracted)
                        if lt_res.returncode != 0 or missing:
                            print("ERRO: snapshot do índice diverge de "
                                  "ls-tree (export-ignore/export-subst em "
                                  f".gitattributes? omitidos={missing[:5]})",
                                  file=sys.stderr)
                            return 2
                        target_root = extract_dir
                    else:
                        co_res = ar_res
                if co_res.returncode == 0 and target_root == repo_root:
                    print("ERRO: Falha ao extrair snapshot do index Git via git archive.", file=sys.stderr)
                    return 2
                if co_res.returncode != 0:
                    print(f"ERRO: Falha ao extrair snapshot do index Git (exit {co_res.returncode}): {co_res.stderr}", file=sys.stderr)
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
                GateSpec, run_gates_parallel,
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
            par_results = run_gates_parallel(
                specs, max_parallel=jobs, filter_root=repo_root)
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




def _cmd_report_html(repo_root: Path, out_path: Path | None = None) -> int:
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

    Tenta `scripts.run_state.derive_run_outcome` via import; se o módulo não
    estiver importável, cai para subprocess `run_state.py verify --json`.
    """
    try:
        from scripts.run_state import derive_run_outcome
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
            [sys.executable, str(SCRIPTS_DIR / "run_state.py"), "verify", "--json", str(run_path)],
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
    header = f"{'RUN_ID':<28} {'MODO':<10} {'DERIVADO':<10} {'DECLARADO':<10} {'REVIEW':<22} ATUALIZADO"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['run_id'][:28]:<28} {r['mode'][:10]:<10} {r['outcome_derived'][:10]:<10} "
            f"{r['outcome_declared'][:10]:<10} {r['review_status'][:22]:<22} {r['updated_at']}"
        )
    return 0


def _cmd_run(argv_list: list[str]) -> int:
    """Wrapper fino sobre `scripts/run_state.py` (mesmos exit codes) + `list` nativo."""
    usage = (
        "Uso: henke run <init|record-gate|amend-scope|approve|verify|list> [args...]\n"
        "  init|record-gate|amend-scope|approve|verify  repassados a scripts/run_state.py\n"
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
        from scripts.run_state import main as run_state_main
    except ImportError:
        run_state_main = None
    if run_state_main is not None:
        return run_state_main(argv_list)
    res = subprocess.run([sys.executable, str(SCRIPTS_DIR / "run_state.py"), *argv_list], env=_scrub_env())
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
        from scripts.state_paths import sanitized_child_env
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


def _cmd_goldfish(
    repo_root: Path,
    spec_path: str | None = None,
    driver: str | None = None,
    run_ref: str | None = None,
    as_json: bool = False,
) -> int:
    """Certificação Goldfish honesta (PROTOCOLO §3.2 — revisão isolada em contexto zerado).

    - COM `--driver <cmd>`: grava envelope de revisão isolada (spec + diff, SEM
      transcript) em `.henke/runs/<id>.goldfish.json` e retorna 0. O envelope é
      o dossiê a ser submetido ao revisor externo; este comando NÃO executa a
      revisão em si.
    - SEM `--driver`: HARD STOP honesto — registra `review_status=goldfish_unavailable`
      na run (quando `--run` informado) e retorna 2.
    """
    run_path = _resolve_run_ref(repo_root, run_ref) if run_ref else None
    if run_ref and run_path is None:
        print(f"ERRO: run '{run_ref}' não encontrada em {repo_root / '.henke' / 'runs'}.", file=sys.stderr)
        return 2

    if not driver:
        if run_path is not None:
            try:
                from scripts.run_state import _file_lock, derive_run_outcome
                with _file_lock(run_path):
                    data = json.loads(run_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("raiz do JSON da run não é um objeto")
                    data["review_status"] = "goldfish_unavailable"
                    data["updated_at"] = _now_iso()
                    try:
                        derived, reasons = derive_run_outcome(data)
                        data["outcome"] = derived
                        data["reasons"] = reasons
                    except Exception:
                        pass  # mantém outcome anterior se a derivação estiver indisponível
                    _atomic_write_json(run_path, data)
                print(f"Registrado review_status='goldfish_unavailable' em {run_path}")
            except Exception as exc:
                print(f"ERRO: não foi possível atualizar a run '{run_path}': {exc}", file=sys.stderr)
                return 2
        print(
            "INDISPONÍVEL: certificação Goldfish não executada — nenhum --driver informado.\n"
            "A revisão Goldfish exige um revisor externo em contexto zerado (sem transcript).\n"
            "Para certificar: henke goldfish --driver \"<comando do revisor>\" [--spec <spec>] [--run <id>]\n"
            "Sem driver externo, resta apenas revisão same-context preliminar — "
            "e ela NÃO é certificação Goldfish.",
            file=sys.stderr,
        )
        return 2

    diff_text, diff_origem = _git_diff_snapshot(repo_root)
    spec_text: str | None = None
    spec_status = "não informado"
    if spec_path:
        sp = Path(spec_path)
        if not sp.is_absolute():
            sp = repo_root / spec_path
        # B8 (banca v13): --spec ../../etc/passwd embutia arquivo arbitrário
        # no envelope destinado ao revisor externo.
        try:
            sp = sp.resolve()
            sp.relative_to(repo_root.resolve())
        except (OSError, ValueError):
            spec_status = "fora do confinamento do repo — recusado"
            sp = None
        if sp is not None and sp.is_file():
            try:
                spec_text = sp.read_text(encoding="utf-8", errors="replace")
                spec_status = "lido"
            except OSError as exc:
                spec_status = f"ilegível: {exc}"
        elif sp is not None:
            spec_status = f"não encontrado: {spec_path}"

    run_id = run_path.stem if run_path else f"goldfish-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    envelope = {
        "version": 1,
        "tipo": "goldfish_review_envelope",
        "run_id": run_id,
        "created_at": _now_iso(),
        "repo": str(repo_root),
        "driver": driver,
        "spec_path": spec_path,
        "spec_status": spec_status,
        "spec": spec_text,
        "diff": diff_text,
        "diff_origem": diff_origem,
        "transcript": "omitido intencionalmente — a revisão Goldfish exige contexto zerado (PROTOCOLO §3.2)",
        "instrucoes": (
            "Submeta este envelope a um revisor externo em sessão zerada via o driver informado. "
            "NÃO inclua o transcript desta sessão. Registre o veredito com "
            "'henke run record-gate <run> --gate goldfish_review --exit-code <0|1>'."
        ),
    }
    env_path = repo_root / ".henke" / "runs" / f"{run_id}.goldfish.json"
    if run_path is not None:
        from scripts.run_state import _file_lock
        with _file_lock(run_path):
            _atomic_write_json(env_path, envelope)
    else:
        _atomic_write_json(env_path, envelope)

    if as_json:
        print(json.dumps({
            "envelope": str(env_path),
            "run_id": run_id,
            "driver": driver,
            "revisao_executada": False,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"OK: envelope Goldfish gravado em {env_path}")
        print(f"Driver registrado: {driver}")
        print(
            "NOTA: este comando gerou o envelope (spec + diff, sem transcript); "
            "a revisão isolada em si NÃO foi executada aqui."
        )
    return 0


def _cmd_setup(target_dir: Path) -> int:
    """`henke setup [dir]`: detecção via henke_detect + stubs .henke/ sem sobrescrever."""
    target_dir = target_dir.resolve()
    if not target_dir.is_dir():
        print(f"ERRO: diretório inexistente: {target_dir}", file=sys.stderr)
        return 2
    try:
        from scripts import henke_detect
    except ImportError:
        print(
            "ERRO: módulo 'scripts/henke_detect.py' indisponível — a detecção de projeto\n"
            "(detect_project/write_project) não está instalada neste pacote.\n"
            "Setup abortado sem escrever nada.",
            file=sys.stderr,
        )
        return 2
    detect_project = getattr(henke_detect, "detect_project", None)
    write_project = getattr(henke_detect, "write_project", None)
    if not callable(detect_project) or not callable(write_project):
        print(
            "ERRO: 'scripts/henke_detect.py' não expõe detect_project(root)/write_project(root, data).",
            file=sys.stderr,
        )
        return 2
    try:
        data = detect_project(str(target_dir))
        project_path = write_project(str(target_dir), data)
    except Exception as exc:
        print(f"ERRO na detecção do projeto: {exc}", file=sys.stderr)
        return 2

    henke_dir = target_dir / ".henke"
    (henke_dir / "runs").mkdir(parents=True, exist_ok=True)

    linguagens = data.get("linguagens") if isinstance(data, dict) else None
    if isinstance(linguagens, list) and linguagens:
        stack_txt = ", ".join(map(str, linguagens))
    elif isinstance(data, dict) and data.get("framework"):
        stack_txt = str(data.get("framework"))
    else:
        stack_txt = "detectada automaticamente"
    nome = target_dir.name or "PROJETO"

    stubs: dict[str, str] = {
        "DIARIO.md": (
            f"# DIÁRIO DE BORDO VIVO — {nome}\n\n"
            f"## [{_now_iso()}] — Inicialização via `henke setup`\n"
            "- **Autor:** Suíte Henke (`henke setup`)\n"
            "- **Ações Realizadas:**\n"
            "  1. Estrutura `.henke/` inicializada; detecção gravada em `.henke/PROJECT.json`.\n"
            "- **Próximos Passos:**\n"
            "  - Registrar aqui a jornada de desenvolvimento.\n"
        ),
        "DECISOES.md": (
            f"# COFRE DE DECISÕES ARQUITETURAIS — {nome}\n\n"
            "Este documento registra as decisões técnicas fundamentais para impedir "
            "que intervenções futuras de agentes desfaçam escolhas aprovadas.\n\n"
            "| ID | Data | Contexto / Decisão | Justificativa Técnica | Status |\n"
            "|---|---|---|---|---|\n"
        ),
        "CONFIG.json": json.dumps({
            "versao_protocolo": "4.0",
            "stack": stack_txt,
            "economia_tokens": {
                "caveman": "auto",
                "ponytail": "auto",
                "perguntar_novamente": True,
            },
            "auditoria_adversarial": {
                "exigir_ledger_comandos": True,
                "proibir_sr_only_hacks": True,
            },
        }, ensure_ascii=False, indent=2) + "\n",
        "POLICY.json": _POLICY_STUB_JSON + "\n",
    }

    criados: list[str] = []
    preservados: list[str] = []
    for stub_name, conteudo in stubs.items():
        destino = henke_dir / stub_name
        if destino.exists():
            preservados.append(stub_name)
            continue
        destino.write_text(conteudo, encoding="utf-8")
        criados.append(stub_name)

    print(f"OK: detecção gravada em {project_path}")
    if isinstance(data, dict):
        partes = []
        for campo in ("linguagens", "framework", "test_cmd", "build_cmd", "lint_cmd", "source"):
            val = data.get(campo)
            if val:
                if isinstance(val, list):
                    val = ", ".join(map(str, val))
                partes.append(f"{campo}={val}")
        if partes:
            print("Detectado: " + "; ".join(partes))
    print("Criados: " + (", ".join(criados) if criados else "nenhum"))
    print("Preservados (já existiam): " + (", ".join(preservados) if preservados else "nenhum"))
    return 0


def _cmd_auto(
    prompt: str,
    repo: Path,
    driver: str | None,
    dry_run: bool,
    yes: bool,
    policy: str,
    timeout: float | None,
    resume: str | None = None,
) -> int:
    """`henke auto`: despacha para `scripts/henke_auto.py:run_auto` (import lazy)."""
    try:
        from scripts import henke_auto
    except ImportError:
        print(
            "ERRO: módulo 'scripts/henke_auto.py' indisponível — o modo automático\n"
            "não está instalado neste pacote. Nada foi executado.",
            file=sys.stderr,
        )
        return 2
    run_auto = getattr(henke_auto, "run_auto", None)
    if not callable(run_auto):
        print("ERRO: 'scripts/henke_auto.py' não expõe run_auto(args).", file=sys.stderr)
        return 2
    ns = argparse.Namespace(
        prompt=prompt,
        repo=str(repo.resolve()),
        driver=driver,
        dry_run=bool(dry_run),
        yes=bool(yes),
        policy=policy,
        timeout=timeout,
        resume=resume,
    )
    # Onda 2+: o single-writer vive dentro de run_auto — cobre `henke auto`,
    # `trigger` e a execução direta de scripts/henke_auto.py.
    return int(run_auto(ns))


def _cmd_trigger(kind: str, target: str | None, repo: Path, **kw) -> int:
    """`henke trigger`: evento → prompt → mesma máquina `auto` (entrada não-interativa).

    Onda 2 (Kimi): triggers não-interativos nunca rodam policy=yolo e o
    alvo de `file` é normalizado para um relpath dentro do repositório —
    o caminho cru do evento nunca é interpolado no prompt."""
    repo_root = Path(repo).resolve()
    if kind == "commit":
        prompt = (
            "revisa o último commit (HEAD) e corrige qualquer problema real "
            "encontrado nas mudanças"
        )
    elif kind == "ci":
        prompt = "investiga e corrige a falha do CI/testes"
    elif kind == "file":
        if not target:
            print("ERRO: `henke trigger file` exige um caminho alvo.", file=sys.stderr)
            return 2
        tp = Path(target)
        cand = tp.resolve() if tp.is_absolute() else (repo_root / tp).resolve()
        try:
            rel = cand.relative_to(repo_root).as_posix()
        except ValueError:
            print(f"ERRO: alvo do trigger fora do repositório: {target}", file=sys.stderr)
            return 2
        prompt = f"revisa o arquivo '{rel}' e corrige problemas reais encontrados"
    else:
        return 2
    if kw.get("policy") != "strict":
        if kw.get("policy") == "yolo":
            print("AVISO: trigger não-interativo não aceita policy=yolo — usando 'balanced'.", file=sys.stderr)
        kw["policy"] = "balanced"
    return _cmd_auto(prompt, repo_root, dry_run=False, **kw)


def _cmd_report(repo_root: Path, run_ref: str) -> int:
    """`henke report <run_id>`: renderiza o relatório markdown via scripts/henke_report.py."""
    henke_report = None
    try:
        from scripts import henke_report as _hr
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
            "ERRO: módulo 'scripts/henke_report.py' indisponível — relatório não pode ser gerado.",
            file=sys.stderr,
        )
        return 2
    try:
        print(henke_report.render_run_report(repo_root, run_ref))
        return 0
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["--help"]

    # Subcomandos que encaminham argumentos livres (exec, gate, run)
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
        ap_exec.add_argument(
            "--timeout", type=float, default=None, metavar="N",
            help="Teto em segundos (default: timeout_sec de .henke/PROJECT.json, senão 180)",
        )
        ap_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="Comando a executar (precedido por --)")
        try:
            ns = ap_exec.parse_args(raw_argv[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        repo_root = ns.repo.resolve()
        ledger_p = ns.ledger.resolve() if ns.ledger else (repo_root / ".henke" / "LEDGER.json")
        exec_log_p = ns.exec_log.resolve() if ns.exec_log else (repo_root / ".henke" / "exec_log.jsonl")
        return _cmd_exec(repo_root, ledger_p, exec_log_p, ns.cmd, cli_timeout=ns.timeout)

    if head == "run":
        return _cmd_run(raw_argv[1:])

    parser = argparse.ArgumentParser(
        prog="henke",
        description="CLI Global Unificada da Suíte Henke (v4.0).",
        epilog="Também disponíveis: 'henke exec [--timeout N] -- <cmd>', 'henke gate <portão>', "
               "'henke run <init|record-gate|amend-scope|approve|verify|list>'.",
    )
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
    p_route.add_argument("--json", action="store_true", default=False, help="Alias de --full (contrato consumido por 'henke auto')")

    p_check = sub.add_parser("check", help="Executa bateria unificada de portões sem abortar no primeiro erro")
    p_check.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_check.add_argument("--spec", default=None, help="Spec de referência para contract_closure")
    p_check.add_argument(
        "--staged", action="store_true",
        help="Avalia um snapshot do índice Git (conteúdo staged) " "em vez do worktree (a11y_scan segue no repo inteiro)",
    )
    p_check.add_argument("--json", action="store_true", help="Saída em JSON")
    p_check.add_argument("--no-cache", action="store_true", help="Ignora cache e força re-execução dos portões")
    p_check.add_argument("--clear-cache", action="store_true", help="Limpa o cache de portões antes de executar")
    p_check.add_argument("--full", action="store_true", help="Força scan integral (regrava manifesto após sucesso)")
    p_check.add_argument("--jobs", type=int, default=1, choices=(1, 2, 3, 4), help="Portões de processo em paralelo (1-4)")
    p_check.add_argument("--gate-timeout", type=float, default=120.0, help="Timeout por portão em segundos")
    p_check.add_argument("--state-dir", type=Path, default=None, help="Diretório de estado absoluto (equivale a HENKE_STATE_DIR)")

    p_app = sub.add_parser("approve", help="Aprova uma run pendente de autorização humana")
    p_app.add_argument("run_file", type=Path, help="Arquivo .henke/runs/<id>.json")
    p_app.add_argument("--approver", default="human", help="Nome do aprovador")
    p_app.add_argument("--yes", "-y", action="store_true",
                       help="Aprova sem confirmação interativa (obrigatório quando stdin não é TTY)")

    p_scope = sub.add_parser("scope", help="Gerencia emendas de escopo em uma run")
    scope_sub = p_scope.add_subparsers(dest="scope_cmd", required=True)
    p_amend = scope_sub.add_parser("amend", help="Adiciona arquivo ao escopo da run com justificativa")
    p_amend.add_argument("run_file", type=Path, help="Arquivo .henke/runs/<id>.json")
    p_amend.add_argument("file_rel", help="Caminho relativo do arquivo")
    p_amend.add_argument("--reason", required=True, help="Justificativa técnica (>= 5 chars)")

    p_gf = sub.add_parser("goldfish", help="Certificação Goldfish: envelope isolado (--driver) ou HARD STOP honesto")
    p_gf.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_gf.add_argument("--spec", default=None, help="Spec de referência embutida no envelope")
    p_gf.add_argument("--driver", default=None, help="Comando do revisor externo (sessão zerada)")
    p_gf.add_argument("--run", dest="run_ref", default=None, help="Run alvo (id ou caminho .henke/runs/<id>.json)")
    p_gf.add_argument("--json", action="store_true", help="Saída em JSON")

    p_install = sub.add_parser("install-hook", help="Instala o pre-commit hook nativo no repositório Git")
    p_install.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_install.add_argument("--force", action="store_true", help="Sobrescreve hook pré-existente sem criar backup .legacy")

    
    p_report_html = sub.add_parser("report-html", help="Gera o dashboard visual local em HTML autocontido")
    p_report_html.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_report_html.add_argument("--out", type=Path, default=None, help="Caminho do arquivo HTML de saída (padrão: .henke/report.html)")

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

    p_report = sub.add_parser("report", help="Relatório markdown rastreável de uma run (5 seções fixas)")
    p_report.add_argument("run_id", help="ID da run ou caminho do arquivo .henke/runs/<id>.json")
    p_report.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")

    p_status = sub.add_parser("status", help="Lista runs de .henke/runs/ com outcome DERIVADO")
    p_status.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_status.add_argument("--json", action="store_true", help="Saída em JSON")

    p_setup = sub.add_parser("setup", help="Detecta o projeto e cria stubs .henke/ sem sobrescrever existentes")
    p_setup.add_argument("dir", nargs="?", type=Path, default=Path("."), help="Diretório do projeto (default: cwd)")

    p_auto = sub.add_parser("auto", help="Modo automático (scripts/henke_auto.py) com política de aprovação")
    p_auto.add_argument("prompt", nargs="+", help="Prompt da tarefa a executar")
    p_auto.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_auto.add_argument("--driver", default=None, help="Driver/executor externo")
    p_auto.add_argument("--dry-run", action="store_true", help="Simula sem escrever nada")
    p_auto.add_argument("--yes", action="store_true", help="Aprova automaticamente o permitido pela policy")
    p_auto.add_argument("--policy", choices=("strict", "balanced", "yolo"), default=None, help="Política de aprovação (default: a da POLICY.json, ou balanced)")
    p_auto.add_argument("--timeout", type=float, default=None, metavar="N", help="Teto em segundos")
    p_auto.add_argument("--resume", default=None, metavar="RUN", help="Retoma run bloqueada após `henke approve`")

    p_trig = sub.add_parser("trigger", help="Dispara `auto` a partir de um evento (commit, falha de CI, arquivo)")
    p_trig.add_argument("kind", choices=("commit", "ci", "file"), help="Tipo de evento")
    p_trig.add_argument("target", nargs="?", default=None, help="Alvo (ex.: path para kind=file)")
    p_trig.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_trig.add_argument("--driver", default=None, help="Driver de agente")
    p_trig.add_argument("--yes", action="store_true", help="Confirma escopos comuns")
    p_trig.add_argument("--policy", choices=("strict", "balanced", "yolo"), default=None)
    p_trig.add_argument("--timeout", type=float, default=None, metavar="N")

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
        is_compact = args.compact or not (args.full or args.json)
        return _cmd_route(prompt_val, args.repo.resolve(), compact=is_compact)

    if args.subcmd == "check":
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

    if args.subcmd == "goldfish":
        return _cmd_goldfish(
            args.repo.resolve(),
            spec_path=args.spec,
            driver=args.driver,
            run_ref=args.run_ref,
            as_json=args.json,
        )

    if args.subcmd == "report":
        return _cmd_report(args.repo.resolve(), args.run_id)

    if args.subcmd == "report-html":
        return _cmd_report_html(args.repo.resolve(), out_path=args.out)

    if args.subcmd == "status":
        return _cmd_list_runs(args.repo.resolve(), as_json=args.json)

    if args.subcmd == "setup":
        return _cmd_setup(args.dir)

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

    if args.subcmd == "auto":
        return _cmd_auto(
            " ".join(args.prompt),
            args.repo,
            driver=args.driver,
            dry_run=args.dry_run,
            yes=args.yes,
            policy=args.policy,
            timeout=args.timeout,
            resume=getattr(args, "resume", None),
        )

    if args.subcmd == "trigger":
        return _cmd_trigger(
            args.kind, getattr(args, "target", None), args.repo,
            driver=args.driver, yes=args.yes, policy=args.policy, timeout=args.timeout,
        )

    if args.subcmd == "approve":
        from scripts.run_state import main as run_state_main
        argv = ["approve", str(args.run_file), "--approver", args.approver]
        if args.yes:
            argv.append("--yes")
        return run_state_main(argv)

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

    
    return 2


if __name__ == "__main__":
    sys.exit(main())
