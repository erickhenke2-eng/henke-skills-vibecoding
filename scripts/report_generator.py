#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/report_generator.py — Dashboard Visual Local em HTML Autocontido.

Gera relatórios de auditoria e qualidade totalmente offline e autocontidos (zero CDNs externas),
com painéis visuais para desenvolvedores e uma seção obrigatória de resumo em linguagem simples
para stakeholders não técnicos.

Parte integrante da Suíte Henke v4.0 (Wave 6 - R4).
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional


def _safe_read_json(path: Path) -> Optional[Any]:
    """Lê um arquivo JSON de forma segura, ignorando arquivos corrompidos."""
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
        return json.loads(content)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def _format_size(num_bytes: int) -> str:
    """Formata bytes em unidade legível."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def generate_report(repo_path: Path, output_file: Optional[Path] = None) -> Path:
    """Gera um relatório HTML autocontido a partir dos artefatos da pasta .henke.

    Args:
        repo_path: Caminho raiz do repositório auditado.
        output_file: Destino do arquivo HTML gerado. Se None, utiliza .henke/report.html.

    Returns:
        Path do arquivo HTML gerado.
    """
    repo = Path(repo_path).resolve()
    henke_dir = repo / ".henke"

    if output_file is None:
        target_path = henke_dir / "report.html"
    else:
        target_path = Path(output_file).resolve()

    target_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Carrega LEDGER.json (suporta formato dict ou lista)
    raw_ledger = _safe_read_json(henke_dir / "LEDGER.json")
    ledger_data: Dict[str, Any] = {}
    if isinstance(raw_ledger, dict):
        ledger_data = raw_ledger
    elif isinstance(raw_ledger, list):
        passed_cnt = sum(
            1 for item in raw_ledger
            if isinstance(item, dict) and item.get("exit") == 0 and item.get("passed", True) is True
        )
        failed_cnt = sum(
            1 for item in raw_ledger
            if isinstance(item, dict) and (item.get("exit") not in (0, None) or item.get("passed") is False)
        )
        ledger_data = {
            "findings": [item for item in raw_ledger if isinstance(item, dict) and (item.get("exit", 0) != 0 or item.get("passed") is False)],
            "gates_passed": passed_cnt,
            "gates_failed": failed_cnt,
            "verdict": "REPROVADO" if failed_cnt > 0 else ("APROVADO" if passed_cnt > 0 else "PENDENTE"),
        }

    # 2. Carrega execuções recentes de .henke/runs/*.json
    runs: List[Dict[str, Any]] = []
    runs_dir = henke_dir / "runs"
    if runs_dir.is_dir():
        for r_path in sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            r_json = _safe_read_json(r_path)
            if isinstance(r_json, dict):
                runs.append(r_json)

    # 3. Detecta stacks/linguagens se o classificador estiver disponível
    detected_stacks: List[str] = []
    try:
        from scripts.mission_classifier import classify_repository
        classification = classify_repository(repo)
        detected_stacks = [s.name for s in classification.detected_stacks if getattr(s, "name", None)]
    except ImportError:
        pass
    except Exception as exc:
        sys.stderr.write(f"[AVISO] Erro ao classificar stacks para o relatório: {exc}\n")

    # Stacks adicionais presentes no ledger
    ledger_stacks = ledger_data.get("stacks", [])
    if isinstance(ledger_stacks, list):
        for s in ledger_stacks:
            s_str = str(s)
            if s_str and s_str not in detected_stacks:
                detected_stacks.append(s_str)

    # 4. Compila métricas principais
    raw_verdict = ledger_data.get("verdict")
    gates_passed = int(ledger_data.get("gates_passed", 0))
    gates_failed = int(ledger_data.get("gates_failed", 0))
    findings = ledger_data.get("findings", [])
    if not isinstance(findings, list):
        findings = []

    if raw_verdict is not None:
        v_upper = str(raw_verdict).strip().upper()
        if gates_failed > 0 or len(findings) > 0:
            verdict = "REPROVADO"
        elif v_upper in ("APROVADO", "PASS", "OK"):
            verdict = "APROVADO"
        elif v_upper in ("REPROVADO", "FAIL", "FAILED", "NAO APROVADO", "NÃO APROVADO"):
            verdict = "REPROVADO"
        else:
            verdict = str(raw_verdict)
    else:
        if gates_failed > 0 or len(findings) > 0:
            verdict = "REPROVADO"
        elif gates_passed > 0:
            verdict = "APROVADO"
        else:
            verdict = "PENDENTE"

    # Extrai data de auditoria
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    audit_date = str(ledger_data.get("audit_date", now_iso))

    # Resumo em linguagem simples (leigo)
    custom_summary = ledger_data.get("plain_language_summary")
    if not custom_summary and findings:
        custom_summary = (
            f"Foram identificados {len(findings)} apontamento(s) mecânicos que requerem atenção da equipe técnica. "
            "Recomenda-se revisar os itens da tabela detalhada antes de publicar qualquer alteração."
        )
    elif not custom_summary and verdict in ("APROVADO", "PASS", "OK"):
        custom_summary = (
            "O sistema foi auditado pelos portões de qualidade mecânicos e está 100% aprovado. "
            "Nenhum risco de segurança, vulnerabilidade de código ou teste teatral foi encontrado."
        )
    elif not custom_summary:
        custom_summary = (
            "Repositório inicializado. Ainda não foram registradas execuções formais de auditoria no ledger. "
            "Execute 'henke check' para gerar a primeira avaliação completa."
        )

    # Cores e classes do veredito (comparação exata de string, NUNCA substring 'in')
    is_approved = verdict in ("APROVADO", "PASS", "OK")
    is_failed = verdict in ("REPROVADO", "FAIL", "FAILED", "NAO APROVADO", "NÃO APROVADO")
    if is_approved:
        verdict_badge_class = "badge-success"
    elif is_failed:
        verdict_badge_class = "badge-danger"
    else:
        verdict_badge_class = "badge-warning"

    # Monta HTML 100% autocontido (Zero CDNs, zero URLs externas)
    html_lines: List[str] = [
        "<!doctype html>",
        "<html lang=\"pt-BR\">",
        "<head>",
        "  <meta charset=\"utf-8\">",
        "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "  <title>Henke Toolkit — Relatório de Auditoria e Qualidade</title>",
        "  <style>",
        "    :root {",
        "      --bg: #0f172a;",
        "      --card-bg: #1e293b;",
        "      --border: #334155;",
        "      --text: #f8fafc;",
        "      --text-muted: #94a3b8;",
        "      --primary: #38bdf8;",
        "      --success: #22c55e;",
        "      --danger: #ef4444;",
        "      --warning: #f59e0b;",
        "      --badge-bg: #0f172a;",
        "    }",
        "    * { box-sizing: border-box; margin: 0; padding: 0; }",
        "    body {",
        "      font-family: system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;",
        "      background-color: var(--bg);",
        "      color: var(--text);",
        "      line-height: 1.6;",
        "      padding: 2rem;",
        "    }",
        "    .container { max-width: 1200px; margin: 0 auto; }",
        "    header {",
        "      display: flex;",
        "      justify-content: space-between;",
        "      align-items: center;",
        "      padding-bottom: 1.5rem;",
        "      border-bottom: 1px solid var(--border);",
        "      margin-bottom: 2rem;",
        "    }",
        "    h1 { font-size: 1.8rem; font-weight: 700; color: var(--primary); }",
        "    .meta-date { font-size: 0.9rem; color: var(--text-muted); }",
        "    .section {",
        "      background: var(--card-bg);",
        "      border: 1px solid var(--border);",
        "      border-radius: 8px;",
        "      padding: 1.5rem;",
        "      margin-bottom: 1.5rem;",
        "    }",
        "    .section-title {",
        "      font-size: 1.25rem;",
        "      font-weight: 600;",
        "      margin-bottom: 1rem;",
        "      color: var(--text);",
        "      display: flex;",
        "      align-items: center;",
        "      gap: 0.5rem;",
        "    }",
        "    .plain-language-box {",
        "      background: rgba(56, 189, 248, 0.08);",
        "      border-left: 4px solid var(--primary);",
        "      border-radius: 4px;",
        "      padding: 1.25rem;",
        "      margin-top: 0.5rem;",
        "      font-size: 1.05rem;",
        "    }",
        "    .plain-item { margin-bottom: 0.75rem; }",
        "    .plain-item strong { color: var(--primary); }",
        "    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 1.5rem; }",
        "    .card {",
        "      background: var(--card-bg);",
        "      border: 1px solid var(--border);",
        "      border-radius: 8px;",
        "      padding: 1.25rem;",
        "      text-align: center;",
        "    }",
        "    .card-num { font-size: 2rem; font-weight: 700; color: var(--text); }",
        "    .card-label { font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; margin-top: 0.25rem; }",
        "    .badge {",
        "      display: inline-block;",
        "      padding: 0.25rem 0.75rem;",
        "      border-radius: 9999px;",
        "      font-weight: 600;",
        "      font-size: 0.9rem;",
        "      text-transform: uppercase;",
        "    }",
        "    .badge-success { background: rgba(34, 197, 94, 0.2); color: var(--success); border: 1px solid var(--success); }",
        "    .badge-danger { background: rgba(239, 68, 68, 0.2); color: var(--danger); border: 1px solid var(--danger); }",
        "    .badge-warning { background: rgba(245, 158, 11, 0.2); color: var(--warning); border: 1px solid var(--warning); }",
        "    .stack-tag {",
        "      display: inline-block;",
        "      background: #334155;",
        "      color: #38bdf8;",
        "      padding: 0.2rem 0.6rem;",
        "      border-radius: 4px;",
        "      font-size: 0.85rem;",
        "      margin-right: 0.4rem;",
        "      margin-bottom: 0.4rem;",
        "    }",
        "    table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.95rem; }",
        "    th, td { padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid var(--border); }",
        "    th { background: rgba(15, 23, 42, 0.6); color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 0.8rem; }",
        "    tr:hover td { background: rgba(255, 255, 255, 0.02); }",
        "    .code-cell { font-family: monospace; font-size: 0.85rem; color: #cbd5e1; }",
        "    footer { text-align: center; margin-top: 3rem; font-size: 0.85rem; color: var(--text-muted); }",
        "  </style>",
        "</head>",
        "<body>",
        "  <div class=\"container\">",
        "    <header>",
        "      <div>",
        "        <h1>Henke Toolkit — Relatório de Auditoria</h1>",
        f"        <div class=\"meta-date\">Repositório: {html.escape(repo.name)} • Gerado em: {html.escape(audit_date)}</div>",
        "      </div>",
        f"      <div><span class=\"badge {verdict_badge_class}\">{html.escape(verdict)}</span></div>",
        "    </header>",
        "",
        "    <!-- Resumo Executivo em Linguagem Simples (Mandatory) -->",
        "    <div class=\"section\">",
        "      <div class=\"section-title\">📋 Resumo Executivo em Linguagem Simples</div>",
        "      <div class=\"plain-language-box\">",
        f"        <div class=\"plain-item\"><strong>Visão Geral:</strong> {html.escape(str(custom_summary))}</div>",
        f"        <div class=\"plain-item\"><strong>O que isso significa?</strong> "
        + ("Todos os critérios de qualidade e segurança foram atendidos rigorosamente. O código está pronto para ser publicado com confiança."
           if is_approved else
           "Foram detectadas pendências que precisam ser corrigidas antes do deploy para evitar falhas ou vulnerabilidades em produção.")
        + "</div>",
        "        <div class=\"plain-item\"><strong>Tecnologias Auditadas:</strong> "
        + (", ".join(html.escape(s.capitalize()) for s in detected_stacks) if detected_stacks else "Padrão Henke / Poliglota")
        + "</div>",
        "      </div>",
        "    </div>",
        "",
        "    <!-- Métricas em Cartões Visuais -->",
        "    <div class=\"grid\">",
        "      <div class=\"card\">",
        f"        <div class=\"card-num\" style=\"color: {'#22c55e' if is_approved else '#ef4444'};\">{html.escape(verdict)}</div>",
        "        <div class=\"card-label\">Veredito Final</div>",
        "      </div>",
        "      <div class=\"card\">",
        f"        <div class=\"card-num\">{gates_passed}</div>",
        "        <div class=\"card-label\">Portões Aprovados</div>",
        "      </div>",
        "      <div class=\"card\">",
        f"        <div class=\"card-num\">{gates_failed}</div>",
        "        <div class=\"card-label\">Portões Reprovados</div>",
        "      </div>",
        "      <div class=\"card\">",
        f"        <div class=\"card-num\">{len(findings)}</div>",
        "        <div class=\"card-label\">Total de Achados</div>",
        "      </div>",
        "    </div>",
        "",
        "    <!-- Tecnologias e Stacks Detectadas -->",
        "    <div class=\"section\">",
        "      <div class=\"section-title\">🛠️ Tecnologias e Frameworks Identificados</div>",
        "      <div>",
    ]

    if detected_stacks:
        for st in detected_stacks:
            html_lines.append(f"        <span class=\"stack-tag\">{html.escape(st.capitalize())}</span>")
    else:
        html_lines.append("        <span class=\"meta-date\">Nenhuma tecnologia específica detectada ou repositório limpo.</span>")

    html_lines.extend([
        "      </div>",
        "    </div>",
        "",
        "    <!-- Tabela Detalhada de Achados da Auditoria -->",
        "    <div class=\"section\">",
        f"      <div class=\"section-title\">🔍 Detalhes dos Portões e Achados ({len(findings)})</div>",
    ])

    if findings:
        html_lines.extend([
            "      <table>",
            "        <thead>",
            "          <tr>",
            "            <th>Portão / Regra</th>",
            "            <th>Arquivo Alvo</th>",
            "            <th>Severidade / Detalhes</th>",
            "          </tr>",
            "        </thead>",
            "        <tbody>",
        ])
        for f in findings:
            if isinstance(f, dict):
                gate_name = html.escape(str(f.get("gate", f.get("rule", "Geral"))))
                file_name = html.escape(str(f.get("file", f.get("target", "N/A"))))
                msg = html.escape(str(f.get("msg", f.get("message", "Sem detalhes adicionais"))))
            else:
                gate_name = "Auditoria"
                file_name = "N/A"
                msg = html.escape(str(f))

            html_lines.append(
                f"          <tr>"
                f"<td class=\"code-cell\">{gate_name}</td>"
                f"<td class=\"code-cell\">{file_name}</td>"
                f"<td>{msg}</td>"
                f"</tr>"
            )
        html_lines.extend([
            "        </tbody>",
            "      </table>",
        ])
    else:
        html_lines.append("      <p style=\"color: #22c55e;\">✨ Nenhum achado adverso ou vulnerabilidade detectada nos portões executados.</p>")

    html_lines.extend([
        "    </div>",
        "",
        "    <!-- Histórico de Execuções Recentes -->",
    ])

    if runs:
        html_lines.extend([
            "    <div class=\"section\">",
            f"      <div class=\"section-title\">⏱️ Execuções Recentes ({len(runs)})</div>",
            "      <table>",
            "        <thead>",
            "          <tr>",
            "            <th>ID da Execução</th>",
            "            <th>Modo</th>",
            "            <th>Status</th>",
            "            <th>Criado em</th>",
            "          </tr>",
            "        </thead>",
            "        <tbody>",
        ])
        for r in runs[:10]:
            r_id = html.escape(str(r.get("run_id", "N/A")))
            r_mode = html.escape(str(r.get("mode", "N/A")))
            r_status = html.escape(str(r.get("status") or r.get("outcome") or "N/A"))
            r_created = html.escape(str(r.get("created_at", "N/A")))
            html_lines.append(
                f"          <tr>"
                f"<td class=\"code-cell\">{r_id}</td>"
                f"<td>{r_mode}</td>"
                f"<td>{r_status}</td>"
                f"<td class=\"meta-date\">{r_created}</td>"
                f"</tr>"
            )
        html_lines.extend([
            "        </tbody>",
            "      </table>",
            "    </div>",
        ])

    html_lines.extend([
        "    <!-- Seção Obrigatória: O que NÃO foi verificado (AGENTS.md §1.3) -->",
        "    <div class=\"section\">",
        "      <div class=\"section-title\">⚠️ O que NÃO foi verificado</div>",
        "      <div class=\"plain-language-box\" style=\"border-left-color: var(--warning);\">",
        "        <ul style=\"padding-left: 1.25rem; font-size: 0.95rem; color: #cbd5e1;\">",
        "          <li>Endpoints externos de rede, APIs de terceiros e serviços em nuvem (inspeção 100% offline).</li>",
        "          <li>Variáveis de ambiente de produção e secrets de infraestrutura remota.</li>",
        "          <li>Runtimes não suportados nativamente nesta máquina (ex: validação com GPU proprietária ou distros Linux externas).</li>",
        "        </ul>",
        "      </div>",
        "    </div>",
        "",
        "    <footer>",
        "      Suíte Henke v4.0 • Protocolo de Verificação Determinística • 100% Offline & Autocontido",
        "    </footer>",
        "  </div>",
        "</body>",
        "</html>",
        "",
    ])

    # Escrita atômica usando arquivo temporário adjacente e os.replace
    tmp_target = target_path.with_name(f"{target_path.name}.tmp.{os.getpid()}")
    try:
        tmp_target.write_text("\n".join(html_lines), encoding="utf-8")
        os.replace(tmp_target, target_path)
    finally:
        if tmp_target.exists():
            try:
                tmp_target.unlink()
            except OSError:
                pass
    return target_path


def main(argv: Optional[List[str]] = None) -> int:
    """Ponto de entrada CLI para geração do relatório HTML."""
    parser = argparse.ArgumentParser(description="Gera o dashboard visual local em HTML autocontido")
    parser.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório")
    parser.add_argument("--out", type=Path, default=None, help="Caminho do arquivo HTML gerado")

    args = parser.parse_args(argv)
    try:
        res = generate_report(args.repo, output_file=args.out)
        print(f"[OK] Relatório gerado com sucesso em: {res}")
        return 0
    except Exception as exc:
        print(f"[ERRO] Falha ao gerar relatório: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
