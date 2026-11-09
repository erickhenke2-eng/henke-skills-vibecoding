#!/usr/bin/env python3
"""
Portão Mecânico de Auditoria do Ledger Executável (PROTOCOLO v4.0, §6 e §10).
Inspeciona comandos executados contra denylist de risco, verifica resolução
de scripts efetivos, valida evidência criptográfica (`exec_log.jsonl`) e
barra vitória por tautologia (0 testes descobertos).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HAZARDOUS_PATTERNS = [
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b", "Tentativa de remoção recursiva forçada (rm -rf)"),
    (r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b", "Tentativa de remoção recursiva forçada (rm -fr)"),
    (r"\bgit\s+push\b.*(?:--force|-f\b)", "Comando 'git push --force' destrutivo proibido em ledger"),
    (r"\b(?:npm|pnpm|yarn)\s+publish\b", "Comando de publicação (publish) proibido em ledger"),
    (r"\b(?:pip|twine)\s+upload\b", "Comando de upload de pacote proibido em ledger"),
    (r"\bchmod\s+(?:-R\s+)?777\b", "Permissão insegura 777 detectada"),
    (r"(?:curl|wget)\b.*\|\s*(?:ba)?sh\b", "Execução remota cega via pipe (curl | sh) proibida"),
    (r"\bformat\b\s+[a-zA-Z]:", "Comando de formatação de disco detectado"),
    (r"\brm\b(?=[^|;&]*\s(?:-\w*r|--recursive)\b)(?=[^|;&]*\s(?:-\w*f|--force)\b)",
     "Remoção recursiva forçada (flags separadas) proibida"),
    (r"\bgit\s+reset\b[^|;&]*--hard\b", "git reset --hard destrutivo proibido em ledger"),
    (r"\bgit\s+clean\b[^|;&]*-\w*f", "git clean -f destrutivo proibido em ledger"),
    (r"\bgit\s+push\b[^|;&]*\+", "git push com refspec forçado (+ref) proibido"),
    (r"\bRemove-Item\b[^|;&]*(?:-Recurse|-Force)", "Remove-Item recursivo/forçado proibido"),
    (r"\b(?:rd|rmdir|del)\b[^|;&]*/s\b", "rd/del /s destrutivo proibido"),
    (r"\bgh\s+release\s+(?:create|upload)\b", "gh release (publish) proibido em ledger"),
    (r"\b(?:vercel|netlify|flyctl)\b[^|;&]*(?:--prod|deploy)\b", "Deploy para produção proibido em ledger"),
    (r"\bdocker\s+push\b", "docker push (publish) proibido em ledger"),
]

# Resolução real de lifecycle npm (anti-TOCTOU): `npm test` não é string cega —
# o pipeline executa pretest -> test -> posttest, e `npm run X` expande preX/X/postX.
_NPM_CMD_RE = re.compile(r"^\s*(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([A-Za-z0-9:_-]+)\s*$")
_NPM_RUN_RE = re.compile(r"(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?([A-Za-z0-9:_-]+)")
_TEST_CMD_RE = re.compile(
    r"\b(?:pytest|jest|vitest|mocha|unittest|cargo\s+test|go\s+test|npm\s+test|npm\s+t\b|bun\s+test|pnpm\s+test|yarn\s+test)\b",
    re.IGNORECASE,
)


def is_test_cmd(full_cmd: str) -> bool:
    """Detecta comando de teste com word-boundary (não confunde 'echo contest' com teste)."""
    return bool(_TEST_CMD_RE.search(full_cmd))


def resolve_npm_lifecycle(cmd: str, pkg: dict) -> list[tuple[str, str]]:
    """Resolve o grafo de hooks npm em ordem de execução: [(hook, corpo), ...].
    `npm test`/`pnpm test`/`yarn test` -> pretest/test/posttest;
    `npm run X` -> preX/X/postX. Expansão transitiva de `npm run Y` nos corpos,
    com guarda de ciclo e profundidade máxima 8."""
    scripts = (pkg or {}).get("scripts")
    m = _NPM_CMD_RE.match(cmd.strip())
    if not m or not isinstance(scripts, dict):
        return []
    chain: list[tuple[str, str]] = []
    visited: set[str] = set()

    def expand(name: str, depth: int) -> None:
        if name in visited or depth > 8:
            return
        visited.add(name)
        for hook in (f"pre{name}", name, f"post{name}"):
            body = scripts.get(hook)
            if not isinstance(body, str):
                continue
            chain.append((hook, body))
            for sub in _NPM_RUN_RE.findall(body):
                if sub != hook:
                    expand(sub, depth + 1)

    expand(m.group(1), 0)
    return chain


def _full_command_str(entry: dict) -> str:
    cmd = entry.get("cmd", "")
    args_list = entry.get("args", [])
    if isinstance(args_list, str) and args_list.strip():
        return f"{cmd} {args_list}".strip()
    if isinstance(args_list, list) and args_list:
        return f"{cmd} {' '.join(str(a) for a in args_list)}".strip()
    return str(cmd).strip()


def verify_log_chain(log_p: Path) -> tuple[list[str], str | None]:
    """Verifica a cadeia de hashes do exec_log e a âncora local da ponta.

    Retorna (erros, último entry_hash válido). Usado por verify_exec_log e por
    `henke exec` ANTES de fazer append — encadear sobre um log plantado
    lavaria a âncora da ponta com uma execução legítima (banca v9 — A1).
    """
    errors: list[str] = []
    log_p = Path(log_p)
    if not log_p.is_file():
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: arquivo de log '{log_p}' inexistente."], None

    try:
        raw_lines = [ln.strip() for ln in log_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as exc:
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: falha ao ler '{log_p}': {exc}"], None

    if not raw_lines:
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: '{log_p}' está vazio."], None

    verified_records: list[dict] = []
    last_ehash: str | None = None
    for idx, line in enumerate(raw_lines, 1):
        try:
            rec = json.loads(line)
        except Exception:
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: JSON malformado na linha {idx} de '{log_p.name}'.")
            continue
        if not isinstance(rec, dict):
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: linha {idx} de '{log_p.name}' não é objeto JSON.")
            continue

        cmd_r = str(rec.get("cmd", "")).strip()
        exit_r = rec.get("exit", rec.get("exit_code", 0))
        tests_r = rec.get("testes_descobertos", 0)
        sha_r = str(rec.get("stdout_sha256", "")).strip()
        prev_r = str(rec.get("prev_hash", "")).strip()
        ehash_r = str(rec.get("entry_hash", "")).strip()

        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha_r):
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: stdout_sha256 inválido na linha {idx} de '{log_p.name}'.")
            continue

        if not isinstance(exit_r, int) or isinstance(exit_r, bool):
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: exit não-inteiro na linha {idx} de '{log_p.name}'.")
            continue

        if idx == 1 and prev_r != "0" * 64:
            # Gênese: a primeira entrada deve nascer do hash-zero — um log
            # reescrito a partir do meio finge continuidade (F-13).
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: prev_hash de gênese inválido na linha 1 de '{log_p.name}'."
            )
            continue

        if idx > 1 and not prev_r:
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: prev_hash ausente na linha {idx} de '{log_p.name}'.")
            continue

        if idx > 1 and last_ehash and prev_r and prev_r != last_ehash:
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: quebra de encadeamento prev_hash na linha {idx} de '{log_p.name}'."
            )
            continue

        if not ehash_r:
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: entry_hash ausente na linha {idx} de '{log_p.name}'.")
            continue

        if ehash_r:
            cand_pipe = hashlib.sha256(
                f"{prev_r}|{cmd_r}|{exit_r}|{tests_r}|{sha_r}".encode("utf-8")
            ).hexdigest()
            body_no_ehash = {k: v for k, v in rec.items() if k != "entry_hash"}
            cand_json1 = hashlib.sha256(
                json.dumps(body_no_ehash, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            cand_json2 = hashlib.sha256(
                json.dumps(body_no_ehash, sort_keys=True).encode("utf-8")
            ).hexdigest()
            if ehash_r not in (cand_pipe, cand_json1, cand_json2):
                errors.append(
                    f"UNVERIFIED_EXECUTION_EVIDENCE: entry_hash adulterado na linha {idx} de '{log_p.name}'."
                )
                continue

        last_ehash = ehash_r
        verified_records.append({
            "cmd": cmd_r,
            "exit": exit_r,
            "testes_descobertos": tests_r,
            "stdout_sha256": sha_r,
        })

    # Âncora da ponta (F-13): a cadeia é consistente por construção até quando
    # reescrita do zero — o tip gravado LOCALMENTE por `henke exec` é a prova
    # de que a história termina onde a última execução real terminou.
    repo_root = log_p.parent.parent if log_p.parent.name == ".henke" else None
    if repo_root is None and last_ehash:
        # A6 (banca v8): log fora de <repo>/.henke/ pulava o check da ponta
        # em silêncio — sem âncora resolvível a cadeia é só auto-consistente.
        errors.append(
            "UNVERIFIED_EXECUTION_EVIDENCE: exec_log fora de '<repo>/.henke/' "
            "— âncora local da ponta não é resolvível, integridade não provada"
        )
    if repo_root is not None and last_ehash:
        try:
            from scripts import run_state as _rs_tip
        except ImportError:
            try:
                import run_state as _rs_tip
            except ImportError:
                _rs_tip = None
        if _rs_tip is not None:
            expected_tip = _rs_tip.load_exec_tip(repo_root)
            if expected_tip is None:
                errors.append(
                    f"UNVERIFIED_EXECUTION_EVIDENCE: exec_log sem âncora local de ponta "
                    f"(exec-tips ausente) — log possivelmente plantado."
                )
            elif expected_tip != last_ehash:
                errors.append(
                    f"UNVERIFIED_EXECUTION_EVIDENCE: ponta do exec_log diverge da "
                    f"âncora local — cadeia reescrita/truncada."
                )

    return errors, last_ehash


def verify_exec_log(entries: list[dict], exec_log_path: Path) -> list[str]:
    """Verifica a cadeia de hashes de `.henke/exec_log.jsonl` e cruza cada comando
    de teste do ledger contra os registros reais executados."""
    errors, _last = verify_log_chain(exec_log_path)
    log_p = Path(exec_log_path)
    if errors and not log_p.is_file():
        return errors
    try:
        raw_lines = [ln.strip() for ln in log_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        raw_lines = []
    verified_records: list[dict] = []
    for line in raw_lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            verified_records.append({
                "cmd": str(rec.get("cmd", "")).strip(),
                "exit": rec.get("exit", rec.get("exit_code", 0)),
                "testes_descobertos": rec.get("testes_descobertos", 0),
                "stdout_sha256": str(rec.get("stdout_sha256", "")).strip(),
            })

    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        full_cmd = _full_command_str(entry)
        raw_cmd = str(entry.get("cmd", "")).strip()
        if not (is_test_cmd(full_cmd) or "testes_descobertos" in entry or "stdout_sha256" in entry):
            continue
        e_exit = entry.get("exit", entry.get("exit_code"))
        e_tests = entry.get("testes_descobertos")
        e_sha = entry.get("stdout_sha256")

        matched = False
        for vrec in verified_records:
            if vrec["cmd"] not in (full_cmd, raw_cmd):
                continue
            if vrec["exit"] != e_exit or vrec["testes_descobertos"] != e_tests:
                continue
            if e_sha is not None and vrec["stdout_sha256"] != str(e_sha).strip():
                continue
            matched = True
            break
        if not matched:
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: entrada #{i} ('{full_cmd}', testes_descobertos={e_tests}) "
                f"sem registro correspondente íntegro em '{log_p.name}'."
            )

    return errors


def audit_ledger_content(ledger_data: list[dict], allow_zero_tests: bool = False,
                         pkg: dict | None = None, exec_log_path: Path | None = None,
                         require_exec_log: bool = False) -> tuple[bool, list[str], int]:
    errors = []
    total_tests_discovered = 0

    if not isinstance(ledger_data, list):
        return False, ["Formato do ledger inválido: esperado array/lista JSON de comandos."], 0

    if len(ledger_data) == 0:
        return False, ["Ledger vazio: nenhuma evidência de execução física foi gravada."], 0

    for i, entry in enumerate(ledger_data, 1):
        if not isinstance(entry, dict):
            errors.append(f"Entrada #{i} não é um objeto JSON válido.")
            continue

        cmd = entry.get("cmd", "")
        if not cmd:
            errors.append(f"Entrada #{i} sem comando ('cmd') especificado.")
            continue

        full_cmd = _full_command_str(entry)

        # Strings a inspecionar contra denylist (inclui script resolvido e a forma
        # normalizada do executável sem extensão Windows: git.exe -> git)
        head_exe, _, tail_args = full_cmd.partition(" ")
        exe_base = os.path.basename(head_exe).lower().replace(".exe", "").replace(".bat", "")
        normalized_cmd = f"{exe_base} {tail_args}".strip()
        strings_to_check = [full_cmd]
        if normalized_cmd != full_cmd:
            strings_to_check.append(normalized_cmd)
        if "executável_resolvido" in entry:
            strings_to_check.append(str(entry["executável_resolvido"]).strip())
        # Anti-TOCTOU: expande o grafo de hooks npm — corpos de pre*/post* entram
        # na denylist com o nome do hook (TOCTOU escondido em lifecycle).
        for hook, body in resolve_npm_lifecycle(normalized_cmd, pkg):
            strings_to_check.append(f"{hook}: {body}")

        # 1. Checagem contra padrões destrutivos (Heurística Denylist)
        for s in strings_to_check:
            for pattern, reason in HAZARDOUS_PATTERNS:
                if re.search(pattern, s, flags=re.IGNORECASE):
                    errors.append(f"Entrada #{i} bloqueada por denylist de segurança: '{s}' ({reason})")

        # 2. Verificação de integridade do snapshot de commit (se fornecido)
        if "snapshot" in entry:
            snap = str(entry["snapshot"]).strip()
            if not re.match(r"^[0-9a-fA-F]{64}$", snap):
                errors.append(f"Entrada #{i}: snapshot de commit/conteúdo inválido: '{snap}'. Esperado hash SHA-256 de 64 caracteres hex.")

        if "stdout_sha256" in entry:
            out_sha = str(entry["stdout_sha256"]).strip()
            if not re.match(r"^[0-9a-fA-F]{64}$", out_sha):
                errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: Entrada #{i} com stdout_sha256 inválido: '{out_sha}'.")

        # 3. Verificação de código de retorno
        exit_code = entry.get("exit", entry.get("exit_code"))
        if exit_code is None:
            errors.append(f"Entrada #{i} sem código de retorno ('exit') registrado.")
        elif isinstance(exit_code, bool) or not isinstance(exit_code, int):
            # LEDGER-EXIT-BOOL (banca v11 A6): false/0.0/"0" passavam na
            # comparação solta `!= 0` — LEDGER escrito à mão auditava verde.
            errors.append(f"Entrada #{i} com 'exit' não-inteiro: {exit_code!r} — veredito inválido.")
        elif exit_code != 0:
            errors.append(f"Entrada #{i} falhou com código de saída não-zero: {exit_code} (comando: '{full_cmd}').")

        # 4. Barramento de vitória por tautologia e verificação estrita de testes
        is_test_command = is_test_cmd(full_cmd)
        has_tests_decl = "testes_descobertos" in entry

        if has_tests_decl or is_test_command:
            tests_count = entry.get("testes_descobertos")
            if tests_count is None:
                errors.append(
                    f"Entrada #{i} ('{full_cmd}') é comando de teste mas não registrou contagem de 'testes_descobertos'."
                )
            elif isinstance(tests_count, bool) or not isinstance(tests_count, int):
                errors.append(f"Entrada #{i}: 'testes_descobertos' deve ser um inteiro válido, não booleano.")
            elif tests_count <= 0 and not allow_zero_tests:
                errors.append(
                    f"Entrada #{i} bloqueada: 0 testes descobertos no comando '{full_cmd}'. "
                    "Status 200/Exit 0 sem testes reais é vitória por tautologia migué."
                )
            else:
                total_tests_discovered += tests_count

        # 5. Validação de testes_passados contra testes_descobertos
        if "testes_passados" in entry:
            passed = entry["testes_passados"]
            if isinstance(passed, bool) or not isinstance(passed, int):
                errors.append(f"Entrada #{i}: 'testes_passados' deve ser um inteiro válido.")
            elif has_tests_decl and isinstance(entry.get("testes_descobertos"), int) and passed < entry["testes_descobertos"]:
                errors.append(f"Entrada #{i}: 'testes_passados' ({passed}) menor que 'testes_descobertos' ({entry['testes_descobertos']}).")

    if total_tests_discovered == 0 and not allow_zero_tests:
        errors.append(
            "Vitória por tautologia: 0 testes descobertos em todo o ledger "
            "(nenhuma evidência de execução real de testes)."
        )

    if require_exec_log or exec_log_path is not None:
        target_log = exec_log_path if exec_log_path is not None else Path(".henke/exec_log.jsonl")
        errors.extend(verify_exec_log(ledger_data, target_log))

    return len(errors) == 0, errors, total_tests_discovered


def _discover_package_json(ledger_path: Path) -> Path | None:
    candidates = [
        ledger_path.parent.parent / "package.json" if ledger_path.parent.name == ".henke" else ledger_path.parent / "package.json",
        ledger_path.parent / "package.json",
        Path.cwd() / "package.json",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=Path(".henke/LEDGER.json"), help="Caminho do LEDGER.json")
    parser.add_argument("--allow-zero-tests", action="store_true", help="Permite comandos de teste com 0 testes (não recomendado)")
    parser.add_argument("--package-json", type=Path, default=None,
                        help="package.json para resolver lifecycle npm (pretest/test/posttest) — anti-TOCTOU")
    parser.add_argument("--require-exec-log", action="store_true",
                        help="Exige verificação de integridade contra .henke/exec_log.jsonl")
    parser.add_argument("--exec-log", type=Path, default=None,
                        help="Caminho explícito do exec_log.jsonl (default: .henke/exec_log.jsonl)")
    args = parser.parse_args()

    ledger_path = args.ledger.resolve()
    if not ledger_path.is_file():
        sys.exit(f"ERRO: Arquivo de ledger não encontrado em: '{ledger_path}'")

    try:
        content = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERRO: Falha ao parsear JSON do ledger: {e}")

    pkg_path = args.package_json or _discover_package_json(ledger_path)
    pkg_data = None
    if pkg_path and Path(pkg_path).is_file():
        try:
            pkg_data = json.loads(Path(pkg_path).read_text(encoding="utf-8"))
        except Exception as e:
            if args.package_json:
                sys.exit(f"ERRO: Falha ao parsear package.json '{args.package_json}': {e}")

    has_sha_entries = isinstance(content, list) and any(
        isinstance(x, dict) and "stdout_sha256" in x for x in content
    )
    exec_log_p = None
    if args.exec_log:
        exec_log_p = args.exec_log.resolve()
    elif args.require_exec_log or has_sha_entries:
        cand_log = ledger_path.parent / "exec_log.jsonl"
        exec_log_p = cand_log if cand_log.is_file() else Path(".henke/exec_log.jsonl").resolve()

    ok, errors, total_tests = audit_ledger_content(
        content,
        allow_zero_tests=args.allow_zero_tests,
        pkg=pkg_data,
        exec_log_path=exec_log_p,
        require_exec_log=args.require_exec_log,
    )
    if not ok:
        print(f"FALHA NA AUDITORIA DO LEDGER ({len(errors)} violações):", file=sys.stderr)
        for err in errors:
            print(f"  [LEDGER ERROR] {err}", file=sys.stderr)
        sys.exit(1)

    if total_tests == 0 and not args.allow_zero_tests:
        print("FALHA NA AUDITORIA DO LEDGER: zero testes descobertos em toda a suíte (vitória por tautologia).", file=sys.stderr)
        sys.exit(1)

    print(
        f"OK: Ledger verificado com sucesso. {len(content)} comandos auditados, "
        f"{total_tests} testes descobertos, zero violações de denylist."
    )


if __name__ == "__main__":
    main()
