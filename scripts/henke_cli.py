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

SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = SCRIPTS_DIR.parent
METRICAS_DIR = SCRIPTS_DIR / "metricas"

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from scripts._paths import suite_root as _suite_root  # noqa: E402

SUITE_ROOT = _suite_root()  # checkout OU <prefix>/henke_data (wheel)

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

# Arquivo staged que "parece" de teste (para restringir o assertless_scan em --staged)
_TEST_FILE_RE = re.compile(
    r"(?i)(^test_.*\.py$|.*_test\.py$|.*[.\-_](test|spec)\.[jt]sx?$|.*\.(test|spec)\.[jt]sx?$)"
)

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
    expected_skills = 58
    skills_count = len(list(SUITE_ROOT.glob("henke-*/SKILL.md")))
    henke_dir = (repo_root / ".henke").is_dir()

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


def _resolve_exec_timeout(repo_root: Path, cli_timeout: float | None) -> float:
    """Resolve o teto de timeout do `henke exec`.

    Ordem de precedência honesta:
      1. flag `--timeout N` (quando > 0);
      2. campo `timeout_sec` de `.henke/PROJECT.json` (quando numérico e > 0);
      3. fallback fixo de 180s.
    """
    if cli_timeout is not None and cli_timeout > 0:
        return float(cli_timeout)
    project_json = repo_root / ".henke" / "PROJECT.json"
    try:
        pdata = json.loads(project_json.read_text(encoding="utf-8"))
        val = pdata.get("timeout_sec") if isinstance(pdata, dict) else None
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            return float(val)
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
    if exec_tokens and sys.platform.startswith("win"):
        resolved = shutil.which(exec_tokens[0])
        if resolved:
            exec_tokens[0] = resolved
        elif exec_tokens[0].lower() in ("echo", "dir", "type", "del", "copy", "move", "md", "rd", "cls"):
            exec_tokens = ["cmd.exe", "/c"] + exec_tokens
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    timeout_sec = _resolve_exec_timeout(repo_root, cli_timeout)

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

    # Lê prev_hash + append da entrada + LEDGER.json sob o MESMO lock
    # (read-modify-write atômico — concorrência não pode ver estado intermediário).
    from scripts import run_state as _rs
    try:
        with _rs._file_lock(exec_log_path):
            prev_hash = "0" * 64
            if exec_log_path.is_file():
                try:
                    lines = [
                        ln.strip()
                        for ln in exec_log_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]
                    if lines:
                        last_obj = json.loads(lines[-1])
                        prev_hash = str(last_obj.get("entry_hash", prev_hash))
                except Exception as exc:
                    print(f"ERRO: Log de execução corrompido em '{exec_log_path}': {exc}", file=sys.stderr)
                    return 2

            chain_payload = f"{prev_hash}|{cmd_str}|{res.returncode}|{discovered}|{stdout_sha256}"
            entry_hash = hashlib.sha256(chain_payload.encode("utf-8")).hexdigest()

            log_entry = {
                "timestamp": _now_iso(),
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

            # Atualiza .henke/LEDGER.json apenas quando o COMANDO é de testes —
            # qualquer saída contendo "N tests" (ex.: um README impresso por cat)
            # não pode entrar no ledger sem ser um runner real (ALTO-6 banca).
            if is_test_command(cmd_tokens, cmd_str):
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

            # Âncora LOCAL da ponta da cadeia (F-13): a cadeia prova só
            # consistência interna — truncar/reescrever o rabinho do jsonl
            # produzia cadeia "válida". Com a ponta fora do repo, qualquer
            # reescrita diverge do tip gravado nesta máquina.
            try:
                _rs.record_exec_tip(repo_root, entry_hash)
            except OSError as exc:
                print(f"ERRO: falha ao ancorar ponta do exec_log localmente: {exc}", file=sys.stderr)
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
    res = subprocess.run([sys.executable, str(script_path), *args],
                         cwd=str(repo_root))
    return res.returncode


def _git_staged_files(repo_root: Path) -> list[str] | None:
    """Retorna os caminhos staged (`git diff --cached --name-only`) ou None se indisponível.

    Usa `-z` para não quebrar em nomes com espaço/acentos; bytes decodificados em utf-8.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "diff", "--cached", "--name-only", "-z"],
            capture_output=True,
        )
    except OSError:
        return None
    if res.returncode != 0:
        return None
    raw = res.stdout if isinstance(res.stdout, bytes) else str(res.stdout).encode("utf-8")
    text = raw.decode("utf-8", errors="replace")
    return [p for p in text.split("\0") if p.strip()]


def _is_test_file(path: Path) -> bool:
    """Heurística honesta de 'arquivo de teste' para o escopo --staged do assertless_scan."""
    if _TEST_FILE_RE.match(path.name):
        return True
    return "tests" in path.parts or "test" in path.parts


def _cmd_check(
    repo_root: Path,
    spec_path: str | None = None,
    as_json: bool = False,
    staged: bool = False,
) -> int:
    """Bateria agregada de portões.

    Com `staged=True`, hazard_scan e assertless_scan são restritos aos arquivos
    listados por `git diff --cached --name-only` (honesto: nada staged -> portão
    marcado como SKIP, nunca varredura silenciosa do repo inteiro). O a11y_scan
    segue no repo inteiro, pois seu escopo é a árvore de UI e não o diff.
    """
    checks_to_run: list[tuple[str, list[str]]] = []
    skipped: list[tuple[str, str]] = []

    if staged:
        staged_rel = _git_staged_files(repo_root)
        if staged_rel is None:
            print(
                "ERRO: '--staged' requer um repositório Git legível "
                "(falha em 'git diff --cached --name-only').",
                file=sys.stderr,
            )
            return 2
        staged_abs = [
            str(repo_root / rel) for rel in staged_rel
            if (repo_root / rel).is_file()
        ]
        if staged_abs:
            checks_to_run.append(("hazard_scan", [sys.executable, str(GATE_MAP["hazard"]), *staged_abs]))
        else:
            skipped.append(("hazard_scan", "nenhum arquivo staged existente em disco"))
        staged_tests = [p for p in staged_abs if _is_test_file(Path(p))]
        if staged_tests:
            checks_to_run.append(("assertless_scan", [sys.executable, str(GATE_MAP["assertless"]), *staged_tests]))
        else:
            skipped.append(("assertless_scan", "nenhum arquivo de teste staged"))
        # a11y_scan: escopo é a árvore de UI do repo, não o diff — permanece integral.
        checks_to_run.append(("a11y_scan", [sys.executable, str(GATE_MAP["a11y"]), str(repo_root)]))
    else:
        checks_to_run.append(("hazard_scan", [sys.executable, str(GATE_MAP["hazard"]), str(repo_root)]))
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
    for gate_name, motivo in skipped:
        results.append({
            "gate": gate_name,
            "exit_code": None,
            "passed": False,
            "skipped": True,
            "output": f"pulado: {motivo}",
        })

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
        # exit 3 = n/a honesto (portão rodou, nada a verificar) — não é falha,
        # mesma semântica de run_state/henke_gates; nunca conta como verde.
        passed = res.returncode in (0, 3)
        if not passed:
            overall_ok = False
        results.append({
            "gate": name,
            "exit_code": res.returncode,
            "passed": passed,
            "skipped": False,
            "output": ((res.stdout or "") + (res.stderr or "")).strip(),
        })

    if as_json:
        print(json.dumps({
            "repo": str(repo_root),
            "staged": staged,
            "passed": overall_ok,
            "gates": results,
        }, ensure_ascii=False, indent=2))
    else:
        for item in results:
            badge = "SKIP" if item.get("skipped") else ("PASS" if item["passed"] else "FAIL")
            exit_txt = "-" if item.get("skipped") else str(item["exit_code"])
            print(f"[{badge}] {item['gate']} (exit {exit_txt})")
            if item["output"]:
                for ln in item["output"].splitlines():
                    print(f"  {ln}")

    return 0 if overall_ok else 1


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
        )
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
    res = subprocess.run([sys.executable, str(SCRIPTS_DIR / "run_state.py"), *argv_list])
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
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "diff", "--no-ext-diff", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout, "git diff HEAD"
        res2 = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
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
        if sp.is_file():
            try:
                spec_text = sp.read_text(encoding="utf-8", errors="replace")
                spec_status = "lido"
            except OSError as exc:
                spec_status = f"ilegível: {exc}"
        else:
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
        help="Restringe hazard_scan/assertless_scan aos arquivos de 'git diff --cached' "
             "(a11y_scan segue no repo inteiro)",
    )
    p_check.add_argument("--json", action="store_true", help="Saída em JSON")

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
        return _cmd_check(args.repo.resolve(), spec_path=args.spec, as_json=args.json, staged=args.staged)

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

    if args.subcmd == "status":
        return _cmd_list_runs(args.repo.resolve(), as_json=args.json)

    if args.subcmd == "setup":
        return _cmd_setup(args.dir)

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

    return 2


if __name__ == "__main__":
    sys.exit(main())
