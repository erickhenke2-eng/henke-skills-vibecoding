#!/usr/bin/env python3
"""exec_contract.py — contrato TIPADO de evidência de execução (P0-02/P1-02).

Antes, o "mint" de crédito de teste era o resultado solto de
`parse_test_runner_output(stdout, stderr)` chamado em dois pontos
(henke_cli exec, henke_auto estágio tests) com regras de sanidade
espalhadas e inconsistentes (w7 rc≠0+placar perfeito só existia no cli;
auto nem aplicava). Qualquer drift entre os dois caminhos mintava
evidência divergente para o mesmo físico.

Agora UMA função canônica decide a evidência:

    verdict = evaluate_execution(cmd_tokens, cmd_str, exit_code, out, err)

`ExecVerdict` é o único objeto que pode alimentar exec_log/LEDGER/gate:
- `is_test` vem SEMPRE de audit_ledger.is_test_cmd (classificador único);
- contagens só existem para runner reconhecido — saída de comando
  arbitrário ("echo '10 passed'") produz zeros + reason 'not_test_command';
- `credit_eligible=False` em toda incoerência (rc≠0 com placar perfeito,
  passed>discovered, contagens negativas) — direção fail-closed;
- `reason` registra POR QUE a evidência vale ou não — auditável no ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import importlib.util


@dataclass(frozen=True)
class ExecVerdict:
    """Veredito imutável de UMA execução supervisionada."""
    is_test: bool
    discovered: int
    passed: int
    exit_code: int
    credit_eligible: bool
    reason: str
    evidence_source: str = "none"  # 'stdout_parse' quando o parse valeu


def _load_suite_module(name: str):
    """Carrega módulo da suíte por PATH explícito — nunca via sys.path
    (um módulo plantado no repo não pode forjar o classificador)."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        f"henke_{name}", str(here / f"{name}.py"))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _is_test_command(cmd_str: str) -> bool:
    al = _load_suite_module("audit_ledger")
    itc = getattr(al, "is_test_cmd", None) if al is not None else None
    if itc is None:
        # Classificador indisponível = não é teste (fail-closed).
        return False
    try:
        return bool(itc(cmd_str))
    except Exception:
        return False


def _parse_counts(out_text: str, err_text: str) -> tuple[int, int]:
    cli = _load_suite_module("henke_cli")
    parse = getattr(cli, "parse_test_runner_output", None) if cli else None
    if parse is None:
        return 0, 0
    try:
        d, p = parse(out_text, err_text)
        return int(d), int(p)
    except Exception:
        return 0, 0


def evaluate_execution(cmd_str: str, exit_code: int,
                       out_text: str, err_text: str) -> ExecVerdict:
    """Contrato único de crédito de teste — toda incoerência vira
    credit_eligible=False com reason auditável."""
    if not _is_test_command(cmd_str):
        return ExecVerdict(False, 0, 0, exit_code, False,
                           "not_test_command", "none")

    discovered, passed = _parse_counts(out_text, err_text)
    if discovered < 0 or passed < 0:
        return ExecVerdict(True, 0, 0, exit_code, False,
                           "negative_counts", "none")
    if passed > discovered:
        # Placar impossível — parse adulterado/corrompido.
        return ExecVerdict(True, discovered, 0, exit_code, False,
                           "passed_gt_discovered", "stdout_parse")
    if exit_code != 0 and discovered > 0 and passed == discovered:
        # w7: runner íntegro que falha reporta falhas — placar perfeito
        # com exit≠0 é forja (output plantado / runner mentindo).
        return ExecVerdict(True, discovered, 0, exit_code, False,
                           "rc_mismatch_zeroed", "stdout_parse")
    if discovered <= 0:
        # Runner reconhecido que "não viu" teste nenhum não minta nada —
        # 'Ran 0 tests' vale como ausência de evidência, não como verde.
        return ExecVerdict(True, 0, 0, exit_code, False,
                           "zero_tests_discovered", "stdout_parse")
    return ExecVerdict(True, discovered, passed, exit_code, True,
                       "ok", "stdout_parse")
