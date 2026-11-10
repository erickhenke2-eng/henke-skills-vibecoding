"""
test_mission_classifier.py — Testes Unitários e de Integração do Classificador de Missão (Wave 6 R1).

Cobre:
  - Detecção isolada de todas as 10 linguagens
  - Detecção de ferramentas frontend (Svelte, Vite, Tailwind)
  - Repositórios poliglotas
  - Pruning de diretórios ignorados (node_modules, target, dist, .venv, .git)
  - Repositórios vazios e apenas com documentação
  - Arquivos corrompidos ou malformados
  - Desambiguação de Makefile sem fontes C/C++
  - Case-insensitivity no Windows
  - Pontes customizadas (.henke/bridges/*.json)
  - Filtragem seletiva de portões mecânicos e test runners
  - Classificação de repositório com CONFIG.json (formato novo e legado)
  - Prompt interativo e bypass não-interativo
  - Execução via CLI

100% determinístico e isolado em tempfile.TemporaryDirectory().
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.mission_classifier import (
    StackInfo,
    MissionClassification,
    detect_stacks,
    classify_repository,
    filter_gates_for_stacks,
    prompt_unconfigured_stack,
    main as classifier_main,
)


class TestMissionClassifier(unittest.TestCase):
    """Bateria de testes unitários do classificador de missão e pontes multi-framework."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name).resolve()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_detect_python_manifests(self):
        """Detecta stack Python a partir de pyproject.toml, requirements.txt e arquivos .py."""
        (self.repo_path / "pyproject.toml").write_text('[project]\nname = "test-py"\n', encoding="utf-8")
        (self.repo_path / "requirements.txt").write_text("pytest>=7.0.0\n", encoding="utf-8")
        src = self.repo_path / "src"
        src.mkdir()
        (src / "main.py").write_text("print('hello')\n", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("python", stack_names)
        py_stack = next(s for s in stacks if s.name == "python")
        self.assertEqual(py_stack.language, "python")
        self.assertEqual(py_stack.category, "language")
        self.assertIn("assertless", py_stack.gate_targets)
        self.assertIn("hazard", py_stack.gate_targets)
        self.assertIn("mutant", py_stack.gate_targets)

    def test_detect_javascript_and_typescript(self):
        """Distingue repositório JavaScript puro de repositório TypeScript."""
        # 1. Repositório JS puro
        (self.repo_path / "package.json").write_text('{"name": "pure-js"}', encoding="utf-8")
        (self.repo_path / "index.js").write_text("console.log(42);", encoding="utf-8")
        stacks_js = detect_stacks(self.repo_path)
        names_js = [s.name for s in stacks_js]
        self.assertIn("javascript", names_js)
        self.assertNotIn("typescript", names_js)

        # 2. Adiciona tsconfig.json -> vira typescript
        (self.repo_path / "tsconfig.json").write_text('{"compilerOptions": {}}', encoding="utf-8")
        (self.repo_path / "index.ts").write_text("const x: number = 42;", encoding="utf-8")
        stacks_ts = detect_stacks(self.repo_path)
        names_ts = [s.name for s in stacks_ts]
        self.assertIn("typescript", names_ts)
        self.assertNotIn("javascript", names_ts)

    def test_detect_rust(self):
        """Detecta stack Rust a partir de Cargo.toml e fontes .rs."""
        (self.repo_path / "Cargo.toml").write_text('[package]\nname = "test-rs"\nversion = "0.1.0"', encoding="utf-8")
        src = self.repo_path / "src"
        src.mkdir()
        (src / "main.rs").write_text("fn main() {}", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("rust", stack_names)
        rust_stack = next(s for s in stacks if s.name == "rust")
        self.assertEqual(rust_stack.test_command, "cargo test")
        self.assertIn("assertless", rust_stack.gate_targets)

    def test_detect_go(self):
        """Detecta stack Go a partir de go.mod e arquivos .go."""
        (self.repo_path / "go.mod").write_text("module testgo\n\ngo 1.22\n", encoding="utf-8")
        (self.repo_path / "main.go").write_text("package main\n\nfunc main() {}\n", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("go", stack_names)
        go_stack = next(s for s in stacks if s.name == "go")
        self.assertEqual(go_stack.test_command, "go test ./...")
        self.assertIn("assertless", go_stack.gate_targets)

    def test_detect_java(self):
        """Detecta stack Java a partir de pom.xml e fontes .java."""
        (self.repo_path / "pom.xml").write_text("<project></project>", encoding="utf-8")
        pkg = self.repo_path / "src" / "main" / "java"
        pkg.mkdir(parents=True)
        (pkg / "App.java").write_text("public class App {}", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("java", stack_names)
        java_stack = next(s for s in stacks if s.name == "java")
        self.assertEqual(java_stack.test_command, "mvn test")

    def test_detect_cpp(self):
        """Detecta stack C/C++ a partir de CMakeLists.txt e fontes .cpp."""
        (self.repo_path / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)", encoding="utf-8")
        (self.repo_path / "main.cpp").write_text("int main() { return 0; }", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("cpp", stack_names)
        cpp_stack = next(s for s in stacks if s.name == "cpp")
        self.assertEqual(cpp_stack.test_command, "ctest --output-on-failure")

    def test_detect_csharp(self):
        """Detecta stack C# (.NET) a partir de .csproj e fontes .cs."""
        (self.repo_path / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"/>', encoding="utf-8")
        (self.repo_path / "Program.cs").write_text("System.Console.WriteLine();", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("csharp", stack_names)
        cs_stack = next(s for s in stacks if s.name == "csharp")
        self.assertEqual(cs_stack.test_command, "dotnet test")

    def test_detect_php(self):
        """Detecta stack PHP a partir de composer.json e fontes .php."""
        (self.repo_path / "composer.json").write_text('{"name": "test/php"}', encoding="utf-8")
        (self.repo_path / "index.php").write_text("<?php echo 1;", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("php", stack_names)
        php_stack = next(s for s in stacks if s.name == "php")
        self.assertEqual(php_stack.test_command, "vendor/bin/phpunit")

    def test_detect_ruby(self):
        """Detecta stack Ruby a partir de Gemfile e fontes .rb."""
        (self.repo_path / "Gemfile").write_text("source 'https://rubygems.org'", encoding="utf-8")
        (self.repo_path / "app.rb").write_text("puts 'ok'", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("ruby", stack_names)
        ruby_stack = next(s for s in stacks if s.name == "ruby")
        self.assertEqual(ruby_stack.test_command, "bundle exec rspec")

    def test_detect_kotlin_swift(self):
        """Detecta stack Kotlin/Swift a partir de build.gradle.kts ou Package.swift."""
        (self.repo_path / "Package.swift").write_text('// swift-tools-version:5.5\nimport PackageDescription', encoding="utf-8")
        (self.repo_path / "main.swift").write_text("print(42)", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("kotlin_swift", stack_names)

    def test_detect_frontend_tools(self):
        """Detecta ferramentas de frontend Svelte, Vite e Tailwind."""
        (self.repo_path / "svelte.config.js").write_text("export default {};", encoding="utf-8")
        (self.repo_path / "vite.config.ts").write_text("export default {};", encoding="utf-8")
        (self.repo_path / "tailwind.config.js").write_text("module.exports = {};", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        stack_names = [s.name for s in stacks]
        self.assertIn("svelte", stack_names)
        self.assertIn("vite", stack_names)
        self.assertIn("tailwind", stack_names)

        svelte_stack = next(s for s in stacks if s.name == "svelte")
        self.assertEqual(svelte_stack.category, "framework")
        self.assertIn("a11y", svelte_stack.gate_targets)

        tailwind_stack = next(s for s in stacks if s.name == "tailwind")
        self.assertEqual(tailwind_stack.category, "tooling")
        self.assertIn("a11y", tailwind_stack.gate_targets)

    def test_polyglot_repository(self):
        """Detecta múltiplas stacks simultaneamente em repositório híbrido poliglota."""
        # Backend Python
        (self.repo_path / "pyproject.toml").write_text('[project]\nname="backend"', encoding="utf-8")
        (self.repo_path / "server.py").write_text("import os", encoding="utf-8")

        # Frontend Vite + Tailwind
        (self.repo_path / "package.json").write_text('{"name": "web"}', encoding="utf-8")
        (self.repo_path / "vite.config.js").write_text("export default {}", encoding="utf-8")
        (self.repo_path / "tailwind.config.js").write_text("module.exports = {}", encoding="utf-8")

        # Native Rust crate
        native = self.repo_path / "native"
        native.mkdir()
        (native / "Cargo.toml").write_text('[package]\nname="core"', encoding="utf-8")
        (native / "lib.rs").write_text("pub fn run() {}", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        names = {s.name for s in stacks}
        self.assertIn("python", names)
        self.assertIn("vite", names)
        self.assertIn("tailwind", names)
        self.assertIn("rust", names)

    def test_directory_pruning(self):
        """Garante que manifestos em diretórios podados (node_modules, target, dist, .venv, .git) são ignorados."""
        for pruned in ["node_modules", "target", "dist", ".venv", ".git", "build", "__pycache__"]:
            p_dir = self.repo_path / pruned / "nested"
            p_dir.mkdir(parents=True)
            (p_dir / "package.json").write_text('{"name": "ignored"}', encoding="utf-8")
            (p_dir / "Cargo.toml").write_text('[package]\nname="ignored"', encoding="utf-8")
            (p_dir / "app.py").write_text("ignored = True", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        self.assertEqual(len(stacks), 0, "Nenhum arquivo em diretórios de build/dependências deve ser detectado")

    def test_empty_and_docs_only_repository(self):
        """Repositório vazio ou apenas com documentação Markdown/texto retorna lista vazia."""
        (self.repo_path / "README.md").write_text("# Documentation Only", encoding="utf-8")
        (self.repo_path / "LICENSE").write_text("MIT", encoding="utf-8")
        (self.repo_path / "notes.txt").write_text("Some notes", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        self.assertEqual(len(stacks), 0)

    def test_corrupted_manifests(self):
        """Arquivos com conteúdo malformado ou corrompido não provocam exceções não tratadas."""
        (self.repo_path / "package.json").write_text("{ unclosed: true, ", encoding="utf-8")
        (self.repo_path / "Cargo.toml").write_text("[[[ broken toml", encoding="utf-8")
        (self.repo_path / "build.gradle.kts").write_text("\x00\xff invalid utf-8 binary simulation", encoding="utf-8", errors="replace")

        try:
            stacks = detect_stacks(self.repo_path)
            self.assertIsInstance(stacks, list)
        except Exception as exc:
            self.fail(f"detect_stacks não deve lançar exceção em manifestos corrompidos: {exc}")

    def test_ambiguous_makefile_without_c_sources(self):
        """Makefile em projeto puro Go ou Python não classifica erradamente como C/C++."""
        (self.repo_path / "go.mod").write_text("module app", encoding="utf-8")
        (self.repo_path / "main.go").write_text("package main", encoding="utf-8")
        (self.repo_path / "Makefile").write_text("all:\n\tgo build .\n", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        names = [s.name for s in stacks]
        self.assertIn("go", names)
        self.assertNotIn("cpp", names, "Makefile sem fontes C/C++ não deve ativar a stack cpp")

    def test_case_insensitivity_windows(self):
        """Manipulação segura de case-insensitivity em nomes de manifestos no Windows NTFS."""
        (self.repo_path / "Package.JSON").write_text('{"name": "case-test"}', encoding="utf-8")
        (self.repo_path / "Index.JS").write_text("console.log(1);", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        names = [s.name for s in stacks]
        self.assertIn("javascript", names)

    def test_custom_bridge_loading(self):
        """Carrega e detecta ponte customizada registrada em .henke/bridges/*.json."""
        bridges_dir = self.repo_path / ".henke" / "bridges"
        bridges_dir.mkdir(parents=True)
        (bridges_dir / "elixir.json").write_text(json.dumps({
            "id": "elixir",
            "name": "Elixir / Phoenix",
            "language": "elixir",
            "category": "language",
            "detection": {
                "files": ["mix.exs", "mix.lock"],
                "extensions": [".ex", ".exs"]
            },
            "test_runner": {
                "cmd": "mix test"
            },
            "gates": ["hazard", "assertless"]
        }), encoding="utf-8")

        # Repositório com arquivo Elixir
        (self.repo_path / "mix.exs").write_text("defmodule App.MixProject do", encoding="utf-8")

        stacks = detect_stacks(self.repo_path)
        names = [s.name for s in stacks]
        self.assertIn("elixir", names)
        elixir_stack = next(s for s in stacks if s.name == "elixir")
        self.assertEqual(elixir_stack.test_command, "mix test")
        self.assertIn("assertless", elixir_stack.gate_targets)

    def test_filter_gates_selective(self):
        """filter_gates_for_stacks retorna apenas os portões das stacks configuradas."""
        py_stack = StackInfo(name="python", language="python", category="language", gate_targets=["assertless", "hazard"], test_command="pytest")
        rs_stack = StackInfo(name="rust", language="rust", category="language", gate_targets=["assertless", "hazard"], test_command="cargo test")

        # Configurado apenas Python
        active, ignored = filter_gates_for_stacks([py_stack, rs_stack], configured=["python"])
        self.assertIn("pytest", active)
        self.assertNotIn("cargo test", active)
        self.assertIn("rust", ignored)
        self.assertNotIn("python", ignored)

        # Configurado ambos
        active_both, ignored_both = filter_gates_for_stacks([py_stack, rs_stack], configured=["python", "rust"])
        self.assertIn("pytest", active_both)
        self.assertIn("cargo test", active_both)
        self.assertEqual(len(ignored_both), 0)

    def test_classify_repository_with_existing_config(self):
        """classify_repository identifica stacks configuradas e unconfigured."""
        (self.repo_path / "pyproject.toml").write_text('[project]\nname="py"', encoding="utf-8")
        (self.repo_path / "Cargo.toml").write_text('[package]\nname="rs"', encoding="utf-8")

        cfg_dir = self.repo_path / ".henke"
        cfg_dir.mkdir()
        (cfg_dir / "CONFIG.json").write_text(json.dumps({
            "stacks": {
                "python": {"enabled": True, "configured": True}
            }
        }), encoding="utf-8")

        classification = classify_repository(self.repo_path)
        detected_names = [s.name for s in classification.detected_stacks]
        self.assertIn("python", detected_names)
        self.assertIn("rust", detected_names)
        self.assertIn("python", classification.configured_stacks)
        self.assertNotIn("rust", classification.configured_stacks)

        unconfigured_names = [s.name for s in classification.unconfigured_stacks]
        self.assertIn("rust", unconfigured_names)
        self.assertNotIn("python", unconfigured_names)

    def test_classify_repository_legacy_string_config(self):
        """classify_repository suporta formato legado com string descritiva em 'stack'."""
        (self.repo_path / "pyproject.toml").write_text('[project]\nname="py"', encoding="utf-8")
        cfg_dir = self.repo_path / ".henke"
        cfg_dir.mkdir()
        (cfg_dir / "CONFIG.json").write_text(json.dumps({
            "stack": "Python 3.12 (unittest) + Shell Scripts"
        }), encoding="utf-8")

        classification = classify_repository(self.repo_path)
        self.assertIn("python", classification.configured_stacks)
        self.assertEqual(len(classification.unconfigured_stacks), 0)

    def test_prompt_unconfigured_stack_interactive_accept(self):
        """Aceite no prompt interativo atualiza CONFIG.json com a stack habilitada."""
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir()
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        with patch("builtins.input", return_value="s"):
            res = prompt_unconfigured_stack("rust", cfg_file, interactive=True)
            self.assertTrue(res)

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertTrue(data["stacks"]["rust"]["enabled"])
        self.assertTrue(data["stacks"]["rust"]["configured"])

    def test_prompt_unconfigured_stack_interactive_decline(self):
        """PT-11: recusa interativa NÃO persiste no repo — CONFIG.json fica
        intocado (sujeira permanente no alvo auditado era o achado)."""
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir()
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        with patch("builtins.input", return_value="N"):
            res = prompt_unconfigured_stack("rust", cfg_file, interactive=True)
            self.assertFalse(res)

        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        self.assertNotIn("rust", data["stacks"])

    def test_prompt_unconfigured_stack_non_interactive(self):
        """Em modo não-interativo, bypassa a stack e retorna False sem travar."""
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir()
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        res = prompt_unconfigured_stack("rust", cfg_file, interactive=False)
        self.assertFalse(res)

    def test_prompt_with_custom_streams(self):
        """Suporta injeção de input_stream e output_stream para testes sem patch global."""
        cfg_file = self.repo_path / ".henke" / "CONFIG.json"
        cfg_file.parent.mkdir()
        cfg_file.write_text('{"stacks": {}}', encoding="utf-8")

        in_stream = io.StringIO("sim\n")
        out_stream = io.StringIO()

        res = prompt_unconfigured_stack("go", cfg_file, interactive=True, input_stream=in_stream, output_stream=out_stream)
        self.assertTrue(res)
        out_val = out_stream.getvalue()
        self.assertIn("Deseja configurar a nova stack 'go'", out_val)
        self.assertIn("[OK] Stack 'go' configurada e habilitada", out_val)

    def test_mission_classifier_cli(self):
        """Testa invocação CLI direta com flag --json."""
        (self.repo_path / "pyproject.toml").write_text('[project]\nname="cli-test"', encoding="utf-8")
        out_buf = io.StringIO()

        with patch("sys.stdout", out_buf):
            ret = classifier_main(["--repo", str(self.repo_path), "--json"])
            self.assertEqual(ret, 0)

        output = out_buf.getvalue()
        payload = json.loads(output)
        self.assertIn("detected_stacks", payload)
        stack_names = [s["name"] for s in payload["detected_stacks"]]
        self.assertIn("python", stack_names)


if __name__ == "__main__":
    unittest.main()
