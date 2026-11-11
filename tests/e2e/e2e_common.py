"""
e2e_common.py — Shared test infrastructure, fixtures, and utilities for Wave 6 E2E tests.
Strictly compatible with Windows OS, Git for Windows, and Python 3.10+.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Optional

SUITE_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = SUITE_ROOT / "henke"
CLI_PATH = SCRIPTS_DIR / "henke_cli.py"


def load_module_from_path(module_name: str, path: Path):
    """Dynamically imports a module from a specific file path."""
    if not path.is_file():
        raise FileNotFoundError(f"Target module file does not exist: {path}")
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module from {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def get_mission_classifier_module():
    return load_module_from_path("mission_classifier", SCRIPTS_DIR / "mission_classifier.py")


def get_process_runner_module():
    return load_module_from_path("process_runner", SCRIPTS_DIR / "process_runner.py")


def get_install_hook_module():
    return load_module_from_path("install_hook", SCRIPTS_DIR / "install_hook.py")


def get_gate_cache_module():
    return load_module_from_path("gate_cache", SCRIPTS_DIR / "gate_cache.py")


def get_report_generator_module():
    return load_module_from_path("report_generator", SCRIPTS_DIR / "report_generator.py")


def run_cli(
    args: list[str],
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: float = 60.0,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    """Runs a henke CLI command using python executable with UTF-8 encoding."""
    target_env = os.environ.copy()
    if env:
        target_env.update(env)
    target_env["PYTHONUTF8"] = "1"
    target_env["PYTHONIOENCODING"] = "utf-8"

    cmd = [sys.executable, str(CLI_PATH)] + list(args)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else str(SUITE_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        input=input_text,
        env=target_env,
    )
    return proc


def git_init(repo_path: Path) -> None:
    """Initializes a clean git repository in the target path with an initial commit."""
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Henke E2E Test",
        GIT_AUTHOR_EMAIL="test@henke.e2e",
        GIT_COMMITTER_NAME="Henke E2E Test",
        GIT_COMMITTER_EMAIL="test@henke.e2e",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_path), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.name", "Henke E2E Test"], cwd=str(repo_path), check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@henke.e2e"], cwd=str(repo_path), check=True, capture_output=True, env=env)
    (repo_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo_path), check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo_path), check=True, capture_output=True, env=env)


def setup_mock_stack(repo_path: Path, stack: str) -> list[Path]:
    """Populates a repository with canonical manifests and files for the specified stack."""
    created_files: list[Path] = []
    
    if stack == "python":
        f1 = repo_path / "pyproject.toml"
        f1.write_text('[project]\nname = "mock-python"\nversion = "0.1.0"\n', encoding="utf-8")
        f2 = repo_path / "requirements.txt"
        f2.write_text("pytest>=7.0.0\n", encoding="utf-8")
        src_dir = repo_path / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f3 = src_dir / "app.py"
        f3.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        created_files.extend([f1, f2, f3])

    elif stack in ("javascript", "typescript"):
        f1 = repo_path / "package.json"
        f1.write_text('{\n  "name": "mock-js",\n  "scripts": {"test": "echo ok"}\n}\n', encoding="utf-8")
        created_files.append(f1)
        if stack == "typescript":
            f2 = repo_path / "tsconfig.json"
            f2.write_text('{"compilerOptions": {"target": "es2022"}}\n', encoding="utf-8")
            src_dir = repo_path / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            f3 = src_dir / "index.ts"
            f3.write_text("export const answer: number = 42;\n", encoding="utf-8")
            created_files.extend([f2, f3])
        else:
            f2 = repo_path / "index.js"
            f2.write_text("module.exports = { answer: 42 };\n", encoding="utf-8")
            created_files.append(f2)

    elif stack == "rust":
        f1 = repo_path / "Cargo.toml"
        f1.write_text('[package]\nname = "mock-rust"\nversion = "0.1.0"\nedition = "2021"\n', encoding="utf-8")
        src_dir = repo_path / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f2 = src_dir / "lib.rs"
        f2.write_text("pub fn add(a: i32, b: i32) -> i32 { a + b }\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "go":
        f1 = repo_path / "go.mod"
        f1.write_text("module mockgo\n\ngo 1.21\n", encoding="utf-8")
        f2 = repo_path / "main.go"
        f2.write_text('package main\n\nfunc main() {}\n', encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "java":
        f1 = repo_path / "pom.xml"
        f1.write_text('<project><modelVersion>4.0.0</modelVersion><groupId>com.mock</groupId><artifactId>app</artifactId><version>1.0</version></project>', encoding="utf-8")
        src_dir = repo_path / "src" / "main" / "java"
        src_dir.mkdir(parents=True, exist_ok=True)
        f2 = src_dir / "App.java"
        f2.write_text("public class App { public static void main(String[] args) {} }\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "cpp":
        f1 = repo_path / "CMakeLists.txt"
        f1.write_text("cmake_minimum_required(VERSION 3.10)\nproject(MockCpp)\nadd_executable(app main.cpp)\n", encoding="utf-8")
        f2 = repo_path / "main.cpp"
        f2.write_text("int main() { return 0; }\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "csharp":
        f1 = repo_path / "App.csproj"
        f1.write_text('<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n</Project>\n', encoding="utf-8")
        f2 = repo_path / "Program.cs"
        f2.write_text("System.Console.WriteLine(42);\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "php":
        f1 = repo_path / "composer.json"
        f1.write_text('{\n  "name": "mock/php",\n  "require": {}\n}\n', encoding="utf-8")
        f2 = repo_path / "index.php"
        f2.write_text("<?php echo 'ok';\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "ruby":
        f1 = repo_path / "Gemfile"
        f1.write_text("source 'https://rubygems.org'\n", encoding="utf-8")
        f2 = repo_path / "app.rb"
        f2.write_text("puts 'hello'\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "kotlin_swift":
        f1 = repo_path / "build.gradle.kts"
        f1.write_text('plugins { kotlin("jvm") version "1.9.0" }\n', encoding="utf-8")
        f2 = repo_path / "Package.swift"
        f2.write_text('// swift-tools-version:5.5\nimport PackageDescription\nlet package = Package(name: "MockSwift")\n', encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "svelte":
        f1 = repo_path / "svelte.config.js"
        f1.write_text("export default {};\n", encoding="utf-8")
        src_dir = repo_path / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        f2 = src_dir / "App.svelte"
        f2.write_text("<h1>Hello Svelte</h1>\n", encoding="utf-8")
        created_files.extend([f1, f2])

    elif stack == "vite":
        f1 = repo_path / "vite.config.ts"
        f1.write_text("import { defineConfig } from 'vite';\nexport default defineConfig({});\n", encoding="utf-8")
        created_files.append(f1)

    elif stack == "tailwind":
        f1 = repo_path / "tailwind.config.js"
        f1.write_text("module.exports = { content: ['./src/**/*.{html,js}'] };\n", encoding="utf-8")
        created_files.append(f1)

    return created_files


# Windows Working Set memory inspection via Win32 ctypes API
class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def get_process_working_set_bytes() -> int:
    """Returns the current process's Working Set Size (physical RAM) in bytes on Windows."""
    if sys.platform.startswith("win"):
        try:
            counters = PROCESS_MEMORY_COUNTERS()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            pass
    return 0


def is_pid_alive_win32(pid: int) -> bool:
    """Verifies whether a process ID is actively running on Windows."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000, SYNCHRONIZE = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            exit_code = wintypes.DWORD()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            ctypes.windll.kernel32.CloseHandle(handle)
            # STILL_ACTIVE is 259
            return exit_code.value == 259
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
