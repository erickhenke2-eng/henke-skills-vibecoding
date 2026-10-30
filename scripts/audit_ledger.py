#!/usr/bin/env python3
"""
Portão Mecânico de Auditoria do Ledger Executável (PROTOCOLO v3.1.1, §6 e §10).
Inspeciona comandos executados contra denylist de risco, verifica resolução
de scripts efetivos e barra vitória por tautologia (0 testes descobertos).
"""
import argparse
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
]


def audit_ledger_content(ledger_data: list[dict], allow_zero_tests: bool = False) -> tuple[bool, list[str], int]:
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

        # Monta a linha de comando completa combinando cmd e args (lista ou string)
        args_list = entry.get("args", [])
        if isinstance(args_list, str):
            full_cmd = f"{cmd} {args_list}".strip()
        elif isinstance(args_list, list) and args_list:
            full_cmd = f"{cmd} {' '.join(str(a) for a in args_list)}".strip()
        else:
            full_cmd = str(cmd).strip()

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

        # 3. Verificação de código de retorno
        exit_code = entry.get("exit", entry.get("exit_code"))
        if exit_code is None:
            errors.append(f"Entrada #{i} sem código de retorno ('exit') registrado.")
        elif exit_code != 0:
            errors.append(f"Entrada #{i} falhou com código de saída não-zero: {exit_code} (comando: '{full_cmd}').")

        # 4. Barramento de vitória por tautologia e verificação estrita de testes
        is_test_command = any(k in full_cmd.lower() for k in ("test", "pytest", "jest", "vitest", "cargo test", "npm t"))
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

    return len(errors) == 0, errors, total_tests_discovered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=Path(".henke/LEDGER.json"), help="Caminho do LEDGER.json")
    parser.add_argument("--allow-zero-tests", action="store_true", help="Permite comandos de teste com 0 testes (não recomendado)")
    args = parser.parse_args()

    ledger_path = args.ledger.resolve()
    if not ledger_path.is_file():
        sys.exit(f"ERRO: Arquivo de ledger não encontrado em: '{ledger_path}'")

    try:
        content = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as e:
        sys.exit(f"ERRO: Falha ao parsear JSON do ledger: {e}")

    ok, errors, total_tests = audit_ledger_content(content, allow_zero_tests=args.allow_zero_tests)
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
