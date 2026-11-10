#!/usr/bin/env python3
"""
mission_classifier.py — Classificador de Missão e Pontes Multi-Framework (PROTOCOLO v4.0 / Wave 6 R1).

Detecta a stack tecnológica e linguagens do repositório inspecionado para disparar
portões mecânicos e testes exclusivamente para as tecnologias ativas, ignorando
stacks não configuradas e solicitando confirmação/configuração do usuário.

Suporta 10 linguagens principais:
  1) Python
  2) JavaScript / TypeScript
  3) Rust
  4) Go
  5) Java
  6) C / C++
  7) C# (.NET)
  8) PHP
  9) Ruby
  10) Kotlin / Swift

E 3 ferramentas essenciais de frontend/tooling:
  - Svelte
  - Vite
  - Tailwind CSS

Também suporta pontes customizadas registradas em `.henke/bridges/*.json`.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

PRUNE_DIRS = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    ".henke",
    ".idea",
    ".vscode",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "bin",
    "obj",
}

CPP_EXTENSIONS = {".c", ".cpp", ".cc", ".cxx", ".h", ".hpp"}


@dataclass
class StackInfo:
    """Metadados de uma stack tecnológica detectada no repositório."""
    name: str
    language: str
    category: str
    detected_files: list[str] = field(default_factory=list)
    test_command: Optional[str] = None
    gate_targets: list[str] = field(default_factory=list)


@dataclass
class MissionClassification:
    """Resultado da classificação do repositório comparando filesystem com CONFIG.json."""
    detected_stacks: list[StackInfo] = field(default_factory=list)
    configured_stacks: list[str] = field(default_factory=list)
    unconfigured_stacks: list[StackInfo] = field(default_factory=list)
    active_gates: list[str] = field(default_factory=list)
    active_test_commands: list[str] = field(default_factory=list)


DEFAULT_STACK_METADATA: dict[str, dict[str, Any]] = {
    "python": {
        "language": "python",
        "category": "language",
        "test_cmd": "pytest",
        "gates": ["assertless", "hazard", "mutant"],
    },
    "typescript": {
        "language": "typescript",
        "category": "language",
        "test_cmd": "npm test",
        "gates": ["assertless", "hazard", "a11y"],
    },
    "javascript": {
        "language": "javascript",
        "category": "language",
        "test_cmd": "npm test",
        "gates": ["assertless", "hazard", "a11y"],
    },
    "rust": {
        "language": "rust",
        "category": "language",
        "test_cmd": "cargo test",
        "gates": ["assertless", "hazard"],
    },
    "go": {
        "language": "go",
        "category": "language",
        "test_cmd": "go test ./...",
        "gates": ["assertless", "hazard"],
    },
    "java": {
        "language": "java",
        "category": "language",
        "test_cmd": "mvn test",
        "gates": ["assertless", "hazard"],
    },
    "cpp": {
        "language": "c/cpp",
        "category": "language",
        "test_cmd": "ctest --output-on-failure",
        "gates": ["hazard"],
    },
    "csharp": {
        "language": "csharp",
        "category": "language",
        "test_cmd": "dotnet test",
        "gates": ["assertless", "hazard"],
    },
    "php": {
        "language": "php",
        "category": "language",
        "test_cmd": "vendor/bin/phpunit",
        "gates": ["assertless", "hazard"],
    },
    "ruby": {
        "language": "ruby",
        "category": "language",
        "test_cmd": "bundle exec rspec",
        "gates": ["assertless", "hazard"],
    },
    "kotlin_swift": {
        "language": "kotlin/swift",
        "category": "language",
        "test_cmd": "./gradlew test",
        "gates": ["hazard"],
    },
    "svelte": {
        "language": "svelte",
        "category": "framework",
        "test_cmd": "npx vitest run",
        "gates": ["a11y"],
    },
    "vite": {
        "language": "javascript/typescript",
        "category": "tooling",
        "test_cmd": "npx vitest run",
        "gates": ["metaconfig", "hazard"],
    },
    "tailwind": {
        "language": "css",
        "category": "tooling",
        "test_cmd": None,
        "gates": ["a11y"],
    },
}


def _atomic_write_json(path: Path, data: object) -> None:
    """Grava JSON atomicamente em arquivo usando substituição atômica no Windows."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _scan_repo_files(repo_path: Path) -> list[str]:
    """Varre arquivos do repositório respeitando diretórios podados (PRUNE_DIRS) e limite de profundidade."""
    collected: list[str] = []
    if not repo_path.is_dir():
        return collected

    for root, dirs, files in os.walk(str(repo_path)):
        rel_root = Path(root).relative_to(repo_path)
        depth = len(rel_root.parts)

        # Prune excluded directories immediately
        dirs[:] = [
            d for d in dirs
            if d.lower() not in PRUNE_DIRS and not d.startswith(".git")
        ]

        # Limit search depth to 4 levels to keep scanning fast and avoid deep nested output folders
        if depth >= 4:
            dirs.clear()

        for f in files:
            rel_file = (rel_root / f).as_posix()
            collected.append(rel_file)

    return collected


def _load_custom_bridges(repo_path: Path) -> list[dict[str, Any]]:
    """Carrega pontes customizadas registradas em `.henke/bridges/*.json`."""
    bridges_dir = repo_path / ".henke" / "bridges"
    custom: list[dict[str, Any]] = []
    if not bridges_dir.is_dir():
        return custom

    for p in sorted(bridges_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "id" in data:
                custom.append(data)
        except Exception:
            continue
    return custom


def detect_stacks(repo_path: Path | str) -> list[StackInfo]:
    """Detecta heurística e deterministicamente as stacks presentes no repositório."""
    repo_p = Path(repo_path).resolve()
    if not repo_p.is_dir():
        return []

    rel_files = _scan_repo_files(repo_p)
    if not rel_files:
        return []

    # Map for fast lookup (lower-cased filenames and extensions)
    files_by_name: dict[str, list[str]] = {}
    files_by_ext: dict[str, list[str]] = {}

    for rf in rel_files:
        name = Path(rf).name.lower()
        ext = Path(rf).suffix.lower()
        files_by_name.setdefault(name, []).append(rf)
        if ext:
            files_by_ext.setdefault(ext, []).append(rf)

    detected: list[StackInfo] = []

    # 1. Python
    py_manifests = ["pyproject.toml", "requirements.txt", "setup.py", "pipfile", "environment.yml"]
    py_files: list[str] = []
    for m in py_manifests:
        py_files.extend(files_by_name.get(m, []))
    py_files.extend(files_by_ext.get(".py", []))
    py_files.extend(files_by_ext.get(".pyi", []))
    if py_files:
        detected.append(StackInfo(
            name="python",
            language="python",
            category="language",
            detected_files=py_files[:30],
            test_command=DEFAULT_STACK_METADATA["python"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["python"]["gates"],
        ))

    # 2. JavaScript / TypeScript
    ts_manifests = ["tsconfig.json"]
    ts_files: list[str] = []
    for m in ts_manifests:
        ts_files.extend(files_by_name.get(m, []))
    ts_files.extend(files_by_ext.get(".ts", []))
    ts_files.extend(files_by_ext.get(".tsx", []))

    js_manifests = ["package.json", "jsconfig.json", "deno.json", "bun.lockb"]
    js_files: list[str] = []
    for m in js_manifests:
        js_files.extend(files_by_name.get(m, []))
    js_files.extend(files_by_ext.get(".js", []))
    js_files.extend(files_by_ext.get(".jsx", []))
    js_files.extend(files_by_ext.get(".mjs", []))
    js_files.extend(files_by_ext.get(".cjs", []))

    if ts_files:
        # Repository has TypeScript elements
        detected.append(StackInfo(
            name="typescript",
            language="typescript",
            category="language",
            detected_files=(ts_files + js_files)[:30],
            test_command=DEFAULT_STACK_METADATA["typescript"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["typescript"]["gates"],
        ))
    elif js_files:
        # Repository has JavaScript but no TypeScript
        detected.append(StackInfo(
            name="javascript",
            language="javascript",
            category="language",
            detected_files=js_files[:30],
            test_command=DEFAULT_STACK_METADATA["javascript"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["javascript"]["gates"],
        ))

    # 3. Rust
    rust_manifests = ["cargo.toml", "cargo.lock"]
    rust_files: list[str] = []
    for m in rust_manifests:
        rust_files.extend(files_by_name.get(m, []))
    rust_files.extend(files_by_ext.get(".rs", []))
    if rust_files:
        detected.append(StackInfo(
            name="rust",
            language="rust",
            category="language",
            detected_files=rust_files[:30],
            test_command=DEFAULT_STACK_METADATA["rust"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["rust"]["gates"],
        ))

    # 4. Go
    go_manifests = ["go.mod", "go.sum", "go.work"]
    go_files: list[str] = []
    for m in go_manifests:
        go_files.extend(files_by_name.get(m, []))
    go_files.extend(files_by_ext.get(".go", []))
    if go_files:
        detected.append(StackInfo(
            name="go",
            language="go",
            category="language",
            detected_files=go_files[:30],
            test_command=DEFAULT_STACK_METADATA["go"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["go"]["gates"],
        ))

    # 5. Java
    java_manifests = ["pom.xml", "build.gradle", "settings.gradle"]
    java_files: list[str] = []
    for m in java_manifests:
        java_files.extend(files_by_name.get(m, []))
    java_files.extend(files_by_ext.get(".java", []))
    if java_files:
        detected.append(StackInfo(
            name="java",
            language="java",
            category="language",
            detected_files=java_files[:30],
            test_command=DEFAULT_STACK_METADATA["java"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["java"]["gates"],
        ))

    # 6. C / C++
    # Note: Makefile alone does not trigger C/C++ unless C/C++ files exist
    cpp_files: list[str] = []
    for ext in CPP_EXTENSIONS:
        cpp_files.extend(files_by_ext.get(ext, []))

    cpp_build_manifests = ["cmakelists.txt", "meson.build", "vcpkg.json", "conanfile.txt"]
    for m in cpp_build_manifests:
        cpp_files.extend(files_by_name.get(m, []))

    if "makefile" in files_by_name and cpp_files:
        cpp_files.extend(files_by_name.get("makefile", []))

    if cpp_files:
        detected.append(StackInfo(
            name="cpp",
            language="c/cpp",
            category="language",
            detected_files=cpp_files[:30],
            test_command=DEFAULT_STACK_METADATA["cpp"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["cpp"]["gates"],
        ))

    # 7. C# (.NET)
    csharp_files: list[str] = []
    for m in ["global.json", "directory.build.props"]:
        csharp_files.extend(files_by_name.get(m, []))
    for rf in rel_files:
        lower_name = Path(rf).name.lower()
        if lower_name.endswith(".csproj") or lower_name.endswith(".sln"):
            csharp_files.append(rf)
    csharp_files.extend(files_by_ext.get(".cs", []))
    if csharp_files:
        detected.append(StackInfo(
            name="csharp",
            language="csharp",
            category="language",
            detected_files=csharp_files[:30],
            test_command=DEFAULT_STACK_METADATA["csharp"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["csharp"]["gates"],
        ))

    # 8. PHP
    php_manifests = ["composer.json", "composer.lock", "phpunit.xml", "phpunit.xml.dist"]
    php_files: list[str] = []
    for m in php_manifests:
        php_files.extend(files_by_name.get(m, []))
    php_files.extend(files_by_ext.get(".php", []))
    if php_files:
        detected.append(StackInfo(
            name="php",
            language="php",
            category="language",
            detected_files=php_files[:30],
            test_command=DEFAULT_STACK_METADATA["php"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["php"]["gates"],
        ))

    # 9. Ruby
    ruby_manifests = ["gemfile", "gemfile.lock", "rakefile", ".rubocop.yml"]
    ruby_files: list[str] = []
    for m in ruby_manifests:
        ruby_files.extend(files_by_name.get(m, []))
    for rf in rel_files:
        if Path(rf).name.lower().endswith(".gemspec"):
            ruby_files.append(rf)
    ruby_files.extend(files_by_ext.get(".rb", []))
    if ruby_files:
        detected.append(StackInfo(
            name="ruby",
            language="ruby",
            category="language",
            detected_files=ruby_files[:30],
            test_command=DEFAULT_STACK_METADATA["ruby"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["ruby"]["gates"],
        ))

    # 10. Kotlin / Swift
    ks_files: list[str] = []
    for m in ["package.swift"]:
        ks_files.extend(files_by_name.get(m, []))
    for rf in rel_files:
        lower_name = Path(rf).name.lower()
        if lower_name.endswith(".xcodeproj") or lower_name.endswith(".xcworkspace"):
            ks_files.append(rf)
    ks_files.extend(files_by_ext.get(".kt", []))
    ks_files.extend(files_by_ext.get(".kts", []))
    ks_files.extend(files_by_ext.get(".swift", []))

    # Check build.gradle.kts for kotlin plugin
    for rf in files_by_name.get("build.gradle.kts", []):
        try:
            content = (repo_p / rf).read_text(encoding="utf-8", errors="replace")
            if "kotlin" in content.lower():
                ks_files.append(rf)
        except Exception:
            pass

    if ks_files:
        detected.append(StackInfo(
            name="kotlin_swift",
            language="kotlin/swift",
            category="language",
            detected_files=ks_files[:30],
            test_command=DEFAULT_STACK_METADATA["kotlin_swift"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["kotlin_swift"]["gates"],
        ))

    # 11. Svelte
    svelte_files: list[str] = []
    for m in ["svelte.config.js", "svelte.config.ts", "svelte.config.cjs", "svelte.config.mjs"]:
        svelte_files.extend(files_by_name.get(m, []))
    svelte_files.extend(files_by_ext.get(".svelte", []))
    if svelte_files:
        detected.append(StackInfo(
            name="svelte",
            language="svelte",
            category="framework",
            detected_files=svelte_files[:30],
            test_command=DEFAULT_STACK_METADATA["svelte"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["svelte"]["gates"],
        ))

    # 12. Vite
    vite_files: list[str] = []
    for m in ["vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs"]:
        vite_files.extend(files_by_name.get(m, []))
    if vite_files:
        detected.append(StackInfo(
            name="vite",
            language="javascript/typescript",
            category="tooling",
            detected_files=vite_files[:30],
            test_command=DEFAULT_STACK_METADATA["vite"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["vite"]["gates"],
        ))

    # 13. Tailwind
    tw_files: list[str] = []
    for m in ["tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs", "tailwind.config.mjs"]:
        tw_files.extend(files_by_name.get(m, []))
    if tw_files:
        detected.append(StackInfo(
            name="tailwind",
            language="css",
            category="tooling",
            detected_files=tw_files[:30],
            test_command=DEFAULT_STACK_METADATA["tailwind"]["test_cmd"],
            gate_targets=DEFAULT_STACK_METADATA["tailwind"]["gates"],
        ))

    # 14. Custom bridges in .henke/bridges/*.json
    for bridge in _load_custom_bridges(repo_p):
        b_id = bridge.get("id")
        if not b_id:
            continue
        detection = bridge.get("detection", {})
        det_files_cfg = [f.lower() for f in detection.get("files", [])]
        det_exts_cfg = [e.lower() for e in detection.get("extensions", [])]

        matched_bridge_files: list[str] = []
        for df in det_files_cfg:
            matched_bridge_files.extend(files_by_name.get(df, []))
        for de in det_exts_cfg:
            matched_bridge_files.extend(files_by_ext.get(de, []))

        if matched_bridge_files:
            test_runner = bridge.get("test_runner", {})
            test_cmd = test_runner.get("cmd") if isinstance(test_runner, dict) else None
            gates = bridge.get("gates", ["hazard"])
            detected.append(StackInfo(
                name=b_id,
                language=bridge.get("language", b_id),
                category=bridge.get("category", "custom"),
                detected_files=matched_bridge_files[:30],
                test_command=test_cmd,
                gate_targets=gates if isinstance(gates, list) else ["hazard"],
            ))

    return detected


def filter_gates_for_stacks(
    detected: list[StackInfo],
    configured: list[str],
) -> tuple[list[str], list[str]]:
    """Filtra portões e comandos para execução, retornando (active_gates, ignored_stacks)."""
    configured_set = set(configured)
    configured_lower = {c.lower() for c in configured_set}

    active_gates: list[str] = []
    ignored_stacks: list[str] = []

    for s in detected:
        s_name = s.name if hasattr(s, "name") else str(s)
        if s_name in configured_set or s_name.lower() in configured_lower:
            gate_targets = getattr(s, "gate_targets", [])
            for g in gate_targets:
                if g not in active_gates:
                    active_gates.append(g)
            test_cmd = getattr(s, "test_command", None)
            if test_cmd and test_cmd not in active_gates:
                active_gates.append(test_cmd)
        else:
            if s_name not in ignored_stacks:
                ignored_stacks.append(s_name)

    return active_gates, ignored_stacks


def prompt_unconfigured_stack(
    stack_name: str,
    config_path: Path | str,
    interactive: bool = True,
    input_stream=None,
    output_stream=None,
) -> bool:
    """Prompt interativo para configurar nova stack ou aviso de bypass em não-interativo."""
    config_p = Path(config_path).resolve()

    if not interactive:
        msg = f"[INFO] Stack não configurada ignorada: {stack_name} (use 'henke configure' para habilitar).\n"
        if output_stream is not None:
            output_stream.write(msg)
            if hasattr(output_stream, "flush"):
                output_stream.flush()
        else:
            sys.stderr.write(msg)
        return False

    prompt_msg = f"Deseja configurar a nova stack '{stack_name}' antes de continuar? [s/N]: "

    try:
        if input_stream is not None:
            if output_stream is not None:
                output_stream.write(prompt_msg)
                if hasattr(output_stream, "flush"):
                    output_stream.flush()
            line = input_stream.readline()
            ans = line.strip() if line else ""
        else:
            ans = input(prompt_msg).strip()
    except (EOFError, KeyboardInterrupt):
        ans = ""

    confirmed = ans.lower() in ("s", "sim", "y", "yes")

    # Load or initialize CONFIG.json
    cfg_data: dict[str, Any] = {"versao_protocolo": "4.0", "stacks": {}}
    if config_p.is_file():
        try:
            loaded = json.loads(config_p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                cfg_data = loaded
        except Exception:
            pass

    if "stacks" not in cfg_data or not isinstance(cfg_data["stacks"], dict):
        cfg_data["stacks"] = {}

    default_info = DEFAULT_STACK_METADATA.get(stack_name, {
        "test_cmd": None,
        "gates": ["hazard"],
    })

    if confirmed:
        cfg_data["stacks"][stack_name] = {
            "enabled": True,
            "configured": True,
            "test_cmd": default_info.get("test_cmd"),
            "gates": default_info.get("gates", ["hazard"]),
        }
        _atomic_write_json(config_p, cfg_data)
        if output_stream is not None:
            output_stream.write(f"[OK] Stack '{stack_name}' configurada e habilitada.\n")
            if hasattr(output_stream, "flush"):
                output_stream.flush()
        return True
    else:
        cfg_data["stacks"][stack_name] = {
            "enabled": False,
            "configured": False,
            "reason": "user_declined",
        }
        _atomic_write_json(config_p, cfg_data)
        msg = f"[INFO] Stack '{stack_name}' ignorada nesta sessão.\n"
        if output_stream is not None:
            output_stream.write(msg)
            if hasattr(output_stream, "flush"):
                output_stream.flush()
        else:
            sys.stderr.write(msg)
        return False


def classify_repository(
    repo_path: Path | str,
    config_path: Path | str | None = None,
) -> MissionClassification:
    """Classifica o repositório determinando stacks ativas, configuradas e ignoradas."""
    repo_p = Path(repo_path).resolve()
    detected = detect_stacks(repo_p)

    if config_path is None:
        cfg_p = repo_p / ".henke" / "CONFIG.json"
    else:
        cfg_p = Path(config_path).resolve()

    configured_stacks: list[str] = []
    if cfg_p.is_file():
        try:
            cfg_data = json.loads(cfg_p.read_text(encoding="utf-8"))
            if isinstance(cfg_data, dict):
                # 1. Stacks dictionary
                stacks_dict = cfg_data.get("stacks")
                if isinstance(stacks_dict, dict):
                    for st_name, st_info in stacks_dict.items():
                        if isinstance(st_info, dict):
                            if st_info.get("enabled", True) and st_info.get("configured", True):
                                configured_stacks.append(st_name)
                        elif st_info is True:
                            configured_stacks.append(st_name)
                # 2. Legacy/string format in "stack" field
                stack_str = cfg_data.get("stack")
                if isinstance(stack_str, str) and not configured_stacks:
                    for known in DEFAULT_STACK_METADATA:
                        if known in stack_str.lower():
                            if known not in configured_stacks:
                                configured_stacks.append(known)
        except Exception:
            pass

    configured_lower = {c.lower() for c in configured_stacks}
    unconfigured_stacks = [
        s for s in detected
        if s.name not in configured_stacks and s.name.lower() not in configured_lower
    ]

    active_gates, _ = filter_gates_for_stacks(detected, configured_stacks)
    active_test_commands = [
        s.test_command for s in detected
        if (s.name in configured_stacks or s.name.lower() in configured_lower)
        and s.test_command is not None
    ]

    return MissionClassification(
        detected_stacks=detected,
        configured_stacks=configured_stacks,
        unconfigured_stacks=unconfigured_stacks,
        active_gates=active_gates,
        active_test_commands=active_test_commands,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI para diagnóstico direto do classificador de missão."""
    parser = argparse.ArgumentParser(description="Classificador de Missão e Pontes Multi-Framework.")
    parser.add_argument("--repo", type=Path, default=Path("."), help="Caminho do repositório a classificar")
    parser.add_argument("--config", type=Path, default=None, help="Caminho alternativo para CONFIG.json")
    parser.add_argument("--json", action="store_true", help="Saída em formato JSON")
    args = parser.parse_args(argv)

    repo_root = args.repo.resolve()
    if not repo_root.exists():
        print(f"ERRO: Repositório '{repo_root}' não existe.", file=sys.stderr)
        return 2

    res = classify_repository(repo_root, config_path=args.config)
    if args.json:
        payload = {
            "repo": str(repo_root),
            "detected_stacks": [
                {
                    "name": s.name,
                    "language": s.language,
                    "category": s.category,
                    "detected_files": s.detected_files,
                    "test_command": s.test_command,
                    "gate_targets": s.gate_targets,
                }
                for s in res.detected_stacks
            ],
            "configured_stacks": res.configured_stacks,
            "unconfigured_stacks": [s.name for s in res.unconfigured_stacks],
            "active_gates": res.active_gates,
            "active_test_commands": res.active_test_commands,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[HENKE] Classificação de Missão para {repo_root}:")
        if res.detected_stacks:
            print("  Stacks detectadas:")
            for s in res.detected_stacks:
                status = "CONFIGURADA" if s.name in res.configured_stacks else "NÃO CONFIGURADA"
                print(f"    - {s.name} ({s.language}, {s.category}) [{status}]")
                if s.detected_files:
                    print(f"      Arquivos: {', '.join(s.detected_files[:5])}")
        else:
            print("  Nenhuma stack de desenvolvimento identificada.")

        if res.unconfigured_stacks:
            print(f"  Aviso: {len(res.unconfigured_stacks)} stack(s) não configurada(s).")
        print(f"  Portões ativos: {', '.join(res.active_gates) if res.active_gates else 'Apenas universais'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
