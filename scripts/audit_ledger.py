#!/usr/bin/env python3
"""
Portão Mecânico de Auditoria do Ledger Executável (PROTOCOLO v3.1.1, §6 e §10).
Inspeciona comandos executados contra denylist de risco, verifica resolução
de scripts efetivos e barra vitória por tautologia (0 testes descobertos).
"""
import argparse
import json
from pathlib import Path
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HAZARDOUS_PATTERNS = [
    (r"\brm\s+-(?:r|f|rf|fr)\b\s+[/~]", "Tentativa de remoção recursiva em diretório raiz ou home"),
    (r"\bgit\s+push\b.*(?:--force|-f\b)", "Comando 'git push --force' destrutivo proibido em ledger"),
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

        # 1. Checagem contra padrões destrutivos (Heurística Denylist)
        for pattern, reason in HAZARDOUS_PATTERNS:
            if re.search(pattern, cmd, flags=re.IGNORECASE):
                errors.append(f"Entrada #{i} bloqueada por denylist de segurança: '{cmd}' ({reason})")

        # 2. Verificação de código de retorno
        exit_code = entry.get("exit", entry.get("exit_code"))
        if exit_code is None:
            errors.append(f"Entrada #{i} sem código de retorno ('exit') registrado.")
        elif exit_code != 0:
            errors.append(f"Entrada #{i} falhou com código de saída não-zero: {exit_code} (comando: '{cmd}').")

        # 3. Barramento de vitória por tautologia (0 testes descobertos em comandos de teste)
        is_test_command = any(k in cmd.lower() for k in ("test", "pytest", "jest", "vitest", "cargo test", "npm t"))
        tests_count = entry.get("testes_descobertos")

        if is_test_command:
            if tests_count is None:
                errors.append(
                    f"Entrada #{i} ('{cmd}') é comando de teste mas não registrou contagem de 'testes_descobertos'."
                )
            elif not isinstance(tests_count, int):
                errors.append(f"Entrada #{i}: 'testes_descobertos' deve ser um número inteiro.")
            elif tests_count <= 0 and not allow_zero_tests:
                errors.append(
                    f"Entrada #{i} bloqueada: 0 testes descobertos no comando '{cmd}'. "
                    "Status 200/Exit 0 sem testes reais é vitória por tautologia migué."
                )
            else:
                total_tests_discovered += tests_count

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

    print(
        f"OK: Ledger verificado com sucesso. {len(content)} comandos auditados, "
        f"{total_tests} testes descobertos, zero violações de denylist."
    )


if __name__ == "__main__":
    main()
