#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_report.py — Testes Unitários para o Gerador de Relatório HTML Autocontido.

Testa geração padrão, caminhos customizados, ausência estrita de recursos externos HTTP/HTTPS,
resumo em linguagem simples, sanitização contra XSS e resiliência contra JSON corrompido.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.report_generator import generate_report, main as report_main


class TestReportGenerator(unittest.TestCase):
    """Bateria de testes unitários do report_generator."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_output_location(self):
        """Gera o relatório no local padrão .henke/report.html."""
        out = generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        self.assertEqual(out.name, "report.html")
        self.assertEqual(out.parent.name, ".henke")

    def test_custom_output_location(self):
        """Gera o relatório no caminho customizado especificado."""
        custom_out = self.repo_path / "custom_audit.html"
        out = generate_report(self.repo_path, output_file=custom_out)
        self.assertEqual(out.resolve(), custom_out.resolve())
        self.assertTrue(custom_out.is_file())

    def test_nested_parent_directory_creation(self):
        """Cria automaticamente diretórios pais inexistentes para o arquivo de saída."""
        deep_out = self.repo_path / "deep" / "nested" / "reports" / "summary.html"
        out = generate_report(self.repo_path, output_file=deep_out)
        self.assertTrue(deep_out.is_file())
        self.assertEqual(out.resolve(), deep_out.resolve())

    def test_zero_external_http_https_dependencies(self):
        """Garante HTML 100% offline, sem links externos http:// ou https://."""
        out = generate_report(self.repo_path)
        html_text = out.read_text(encoding="utf-8")

        self.assertNotIn("http://", html_text)
        self.assertNotIn("https://", html_text)
        self.assertNotIn("fonts.googleapis.com", html_text)
        self.assertNotIn("cdnjs.cloudflare.com", html_text)

    def test_mandatory_plain_language_section_present(self):
        """Verifica a presença obrigatória da seção em linguagem simples."""
        out = generate_report(self.repo_path)
        html_lower = out.read_text(encoding="utf-8").lower()
        self.assertTrue(
            "linguagem simples" in html_lower or "resumo executivo" in html_lower,
            "Seção obrigatória de linguagem simples ausente no relatório",
        )

    def test_custom_plain_language_summary_rendered(self):
        """Renderiza resumo customizado em linguagem simples presente no LEDGER.json."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        custom_summary = "Este projeto está impecável e pronto para homologação."
        (henke_dir / "LEDGER.json").write_text(
            json.dumps({"plain_language_summary": custom_summary, "verdict": "APROVADO"}),
            encoding="utf-8",
        )

        out = generate_report(self.repo_path)
        html_text = out.read_text(encoding="utf-8")
        self.assertIn(custom_summary, html_text)

    def test_visual_audit_status_cards_and_html_structure(self):
        """Verifica estrutura básica do HTML e cartões visuais."""
        out = generate_report(self.repo_path)
        content = out.read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", content.lower())
        self.assertIn("</html>", content.lower())
        self.assertIn("<style>", content.lower())
        self.assertIn("veredito final", content.lower())

    def test_html_escaping_and_xss_protection(self):
        """Garante escape de caracteres especiais e proteção contra XSS."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        malicious = "<script>alert('xss')</script>"
        (henke_dir / "LEDGER.json").write_text(
            json.dumps({"findings": [{"gate": malicious, "file": malicious, "msg": malicious}], "verdict": malicious}),
            encoding="utf-8",
        )

        out = generate_report(self.repo_path)
        content = out.read_text(encoding="utf-8")
        self.assertNotIn("<script>alert('xss')</script>", content)
        self.assertIn("&lt;script&gt;alert(&#x27;xss&#x27;)&lt;/script&gt;", content)

    def test_resilience_with_corrupted_runs_json(self):
        """Garante que arquivos JSON corrompidos em .henke/runs não quebrem o gerador."""
        runs_dir = self.repo_path / ".henke" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        (runs_dir / "corrupted_run.json").write_text("{ unclosed invalid json !!!", encoding="utf-8")

        out = generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 100)

    def test_large_volume_of_findings(self):
        """Renderiza grande volume de achados (500+) sem estouro de memória ou falha."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        findings = [
            {"gate": f"gate_{i}", "file": f"src/module_{i}.py", "msg": f"Análise estática {i}"}
            for i in range(500)
        ]
        (henke_dir / "LEDGER.json").write_text(
            json.dumps({"findings": findings, "verdict": "REPROVADO", "gates_passed": 2, "gates_failed": 1}),
            encoding="utf-8",
        )

        out = generate_report(self.repo_path)
        self.assertTrue(out.is_file())
        self.assertGreater(out.stat().st_size, 5000)

    def test_stacks_rendering(self):
        """Renderiza tags de linguagens/stacks identificadas."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(
            json.dumps({"stacks": ["python", "rust", "svelte", "tailwind"], "verdict": "APROVADO"}),
            encoding="utf-8",
        )

        out = generate_report(self.repo_path)
        content = out.read_text(encoding="utf-8")
        self.assertIn("Python", content)
        self.assertIn("Rust", content)
        self.assertIn("Svelte", content)
        self.assertIn("Tailwind", content)

    def test_cli_entry_point(self):
        """Testa invocação CLI do módulo report_generator."""
        ret = report_main(["--repo", str(self.repo_path)])
        self.assertEqual(ret, 0)
        expected_report = self.repo_path / ".henke" / "report.html"
        self.assertTrue(expected_report.is_file())

    def test_nao_aprovado_verdict_renders_as_danger_badge(self):
        """Veredito 'NAO APROVADO' deve renderizar badge de perigo e NUNCA badge de sucesso."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(
            json.dumps({"verdict": "NAO APROVADO", "gates_passed": 0, "gates_failed": 1}),
            encoding="utf-8",
        )
        out = generate_report(self.repo_path)
        content = out.read_text(encoding="utf-8")
        self.assertIn('<span class="badge badge-danger">REPROVADO</span>', content)
        self.assertNotIn('<span class="badge badge-success">', content)

    def test_list_ledger_without_exit_does_not_count_as_passed(self):
        """Em ledger no formato de lista, itens sem 'exit: 0' explícito não contam como aprovados."""
        henke_dir = self.repo_path / ".henke"
        henke_dir.mkdir(parents=True, exist_ok=True)
        (henke_dir / "LEDGER.json").write_text(
            json.dumps([{"cmd": "run_test"}]),
            encoding="utf-8",
        )
        out = generate_report(self.repo_path)
        content = out.read_text(encoding="utf-8")
        self.assertIn("0</div>\n        <div class=\"card-label\">Portões Aprovados</div>", content)


if __name__ == "__main__":
    unittest.main()
