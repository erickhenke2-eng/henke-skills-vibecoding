#!/usr/bin/env python3
"""henke_detect.py — detecção de projeto e gravação de .henke/PROJECT.json (proposta M4).

Lê os fingerprints conhecidos na raiz do repositório e devolve um dicionário
estável (contrato consumido por `henke setup` e futuros `henke exec/auto`):

    {
      "linguagens": ["javascript", "python", ...],   # nomes simples, ordem da evidência
      "framework":  "react" | "django" | None,       # melhor palpite ou null
      "test_cmd":   "npm test" | "python -m pytest" | None,
      "build_cmd":  "npm run build" | "cargo build" | None,
      "lint_cmd":   "npm run lint" | "make lint" | None,
      "source":     ["package.json", "pytest.ini", ...]   # evidências (paths relativos)
    }

Repositório vazio ou irreconhecível NÃO é erro: campos null e `linguagens`/`source`
vazios.

Fingerprints (em ordem; cada arquivo que contribui entra em `source`):
  1. package.json     scripts.test/build/lint; `packageManager` (npm|yarn|pnpm);
                      vitest/jest (arquivo de config, chave "jest" ou dependência)
                      quando não há script "test" -> npx/yarn/pnpm equivalente.
                      tsconfig.json ou dep "typescript" -> linguagem typescript.
  2. pyproject.toml   menção a pytest ou [tool.pytest] -> `python -m pytest`;
                      [tool.poetry]/[tool.hatch]/[build-system] -> build_cmd;
                      framework django/flask/fastapi por heurística de texto
                      (tomllib não existe no Python 3.10 — parse é por regex).
  3. requirements*.txt  linguagem python; linha `pytest` -> pytest; framework por dep.
  4. pytest.ini / tox.ini / setup.cfg  pytest.ini e [tool:pytest] -> pytest;
                      tox.ini só vota em pytest se a palavra aparecer no arquivo.
  5. Cargo.toml       -> `cargo test` / `cargo build`.
  6. go.mod           -> `go test ./...` / `go build ./...`.
  7. Makefile         alvos `test:`/`build:`/`lint:` -> `make <alvo>`
                      (só preenche campos ainda vagos; prioridade mais baixa).
  8. Dockerfile       evidência de container — entra em `source`, sem comandos.

Resolução de comandos (primeiro candidato vence): JS > Python > Cargo > Go > Make.
Fallback python: com "python" detectado (ou tests/*.py presentes) e diretório
tests/ sem pytest -> `python -m unittest discover -s tests`.

`linguagens`: derivadas dos manifests; complementadas por censo de extensões
(.py/.js/.ts/.rs/.go/.sh), em que a linguagem só entra se seus arquivos somarem
>=50% do grupo majoritário (arquivo isolado não conta). Diretórios de ruído
(.git, node_modules, .venv, dist, build, target, vendor, __pycache__ etc.) são
ignorados no censo.

CLI:
  python scripts/henke_detect.py <root>            imprime o JSON (utf-8) no stdout
  python scripts/henke_detect.py <root> --json     idem (flag explícita)
  python scripts/henke_detect.py <root> --write    grava .henke/PROJECT.json e
                                                   imprime o caminho no stdout
  --write --json   grava, imprime o JSON no stdout e confirma o path no stderr

API pública (não alterar — consumidores dependem dela):
  detect_project(root: str) -> dict    levanta NotADirectoryError se root inválido
  write_project(root: str, data: dict) -> str
      grava <root>/.henke/PROJECT.json atomicamente (tmp + os.replace), cria
      .henke/ quando ausente e retorna o caminho absoluto gravado.

Exit: 0 ok · 2 root inválido/IO.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_JS_MANAGERS = ("npm", "yarn", "pnpm")
_JS_FRAMEWORKS = (
    ("next", "next"), ("nuxt", "nuxt"), ("@nestjs/core", "nestjs"),
    ("@angular/core", "angular"), ("react", "react"), ("vue", "vue"),
    ("svelte", "svelte"), ("express", "express"), ("fastify", "fastify"),
)
_PY_FRAMEWORKS = ("django", "flask", "fastapi")
_ECO_ORDER = ("js", "py", "cargo", "go", "make")

_EXT_LANG = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript",
    ".cts": "typescript",
    ".py": "python", ".rs": "rust", ".go": "go",
    ".sh": "shell", ".bash": "shell",
}
_SKIP_DIRS = {
    "node_modules", "venv", "env", "dist", "build", "target", "vendor",
    "coverage", "__pycache__",
}
_MAKE_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?![:=])", re.M)


def _empty() -> dict:
    return {"linguagens": [], "framework": None, "test_cmd": None,
            "build_cmd": None, "lint_cmd": None, "source": []}


def _add_lang(res: dict, name: str) -> None:
    if name not in res["linguagens"]:
        res["linguagens"].append(name)


def _add_src(res: dict, rel: str) -> None:
    if rel not in res["source"]:
        res["source"].append(rel)


def _set(res: dict, key: str, value) -> None:
    if value and res[key] is None:
        res[key] = value


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"WARN: {path}: {e} (ignorado)", file=sys.stderr)
        return None


def _find_any(root: Path, patterns) -> list[str]:
    hits = []
    for pat in patterns:
        for p in sorted(root.glob(pat)):
            if p.is_file():
                hits.append(p.name)
    return hits


def _js_manager(pkg: dict) -> str:
    pm = str(pkg.get("packageManager") or "")
    name = pm.split("@")[0].strip() if pm else ""
    return name if name in _JS_MANAGERS else "npm"


def _js_run_cmd(pm: str, script: str) -> str:
    return f"npm run {script}" if pm == "npm" else f"{pm} {script}"


def _js_exec_cmd(pm: str, tool: str) -> str:
    return {"npm": f"npx {tool}",
            "yarn": f"yarn {tool}",
            "pnpm": f"pnpm {tool}"}[pm]


def _fp_package_json(root: Path, res: dict, cand: dict) -> None:
    p = root / "package.json"
    if not p.is_file():
        return
    _add_src(res, "package.json")
    _add_lang(res, "javascript")
    pkg = {}
    text = _read(p)
    if text is not None:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                pkg = data
        except json.JSONDecodeError:
            print(f"WARN: package.json malformado (conteúdo ignorado)",
                  file=sys.stderr)
    scripts = pkg.get("scripts")
    scripts = scripts if isinstance(scripts, dict) else {}
    pm = _js_manager(pkg)

    deps = {}
    for k in ("dependencies", "devDependencies"):
        if isinstance(pkg.get(k), dict):
            deps.update(pkg[k])

    tsconfig = root / "tsconfig.json"
    if "typescript" in deps or tsconfig.is_file():
        _add_lang(res, "typescript")
        if tsconfig.is_file():
            _add_src(res, "tsconfig.json")

    for key, name in _JS_FRAMEWORKS:
        if key in deps:
            _set(res, "framework", name)
            break

    vitest_cfg = _find_any(root, ("vitest.config.*", "vitest.workspace.*"))
    jest_cfg = _find_any(root, ("jest.config.*",))
    for cfg in vitest_cfg + jest_cfg:
        _add_src(res, cfg)
    has_vitest = bool(vitest_cfg) or "vitest" in deps
    has_jest = bool(jest_cfg) or "jest" in deps or isinstance(pkg.get("jest"), dict)

    test_script = str(scripts.get("test") or "").strip()
    # "test": "echo \"Error: no test specified\" && exit 1" é o scaffold do npm —
    # não é runner de verdade; cai para a evidência de vitest/jest.
    if test_script and "no test specified" not in test_script:
        cand["js"]["test"] = f"{pm} test"
    elif has_vitest:
        cand["js"]["test"] = _js_exec_cmd(pm, "vitest run")
    elif has_jest:
        cand["js"]["test"] = _js_exec_cmd(pm, "jest")
    if str(scripts.get("build") or "").strip():
        cand["js"]["build"] = _js_run_cmd(pm, "build")
    if str(scripts.get("lint") or "").strip():
        cand["js"]["lint"] = _js_run_cmd(pm, "lint")


def _fp_pyproject(root: Path, res: dict, cand: dict) -> None:
    p = root / "pyproject.toml"
    if not p.is_file():
        return
    _add_src(res, "pyproject.toml")
    _add_lang(res, "python")
    text = _read(p) or ""
    if re.search(r"(?i)\bpytest\b", text):
        cand["py"]["test"] = "python -m pytest"
    if re.search(r"(?im)^\s*\[\s*tool\.poetry\b", text):
        cand["py"]["build"] = "poetry build"
    elif re.search(r"(?im)^\s*\[\s*tool\.hatch\b", text):
        cand["py"]["build"] = "hatch build"
    elif re.search(r"(?im)^\s*\[\s*build-system\b", text):
        cand["py"]["build"] = "python -m build"
    for fw in _PY_FRAMEWORKS:
        if re.search(rf"(?i)\b{fw}\b", text):
            _set(res, "framework", fw)
            break


def _fp_requirements(root: Path, res: dict, cand: dict) -> None:
    for p in sorted(root.glob("requirements*.txt")):
        if not p.is_file():
            continue
        _add_src(res, p.name)
        _add_lang(res, "python")
        text = _read(p) or ""
        if re.search(r"(?im)^\s*pytest\b", text):
            cand["py"]["test"] = "python -m pytest"
        for fw in _PY_FRAMEWORKS:
            if re.search(rf"(?im)^\s*{fw}\b", text):
                _set(res, "framework", fw)
                break


def _fp_pytest_cfg(root: Path, res: dict, cand: dict) -> None:
    if (root / "pytest.ini").is_file():
        _add_src(res, "pytest.ini")
        _add_lang(res, "python")
        cand["py"]["test"] = "python -m pytest"
    tox = root / "tox.ini"
    if tox.is_file():
        _add_src(res, "tox.ini")
        _add_lang(res, "python")
        if re.search(r"(?i)\bpytest\b", _read(tox) or ""):
            cand["py"]["test"] = "python -m pytest"
    sc = root / "setup.cfg"
    if sc.is_file():
        _add_src(res, "setup.cfg")
        _add_lang(res, "python")
        if re.search(r"(?im)^\s*\[tool:pytest\]", _read(sc) or ""):
            cand["py"]["test"] = "python -m pytest"


def _fp_cargo(root: Path, res: dict, cand: dict) -> None:
    if (root / "Cargo.toml").is_file():
        _add_src(res, "Cargo.toml")
        _add_lang(res, "rust")
        cand["cargo"]["test"] = "cargo test"
        cand["cargo"]["build"] = "cargo build"


def _fp_go(root: Path, res: dict, cand: dict) -> None:
    if (root / "go.mod").is_file():
        _add_src(res, "go.mod")
        _add_lang(res, "go")
        cand["go"]["test"] = "go test ./..."
        cand["go"]["build"] = "go build ./..."


def _fp_makefile(root: Path, res: dict, cand: dict) -> None:
    p = root / "Makefile"
    if not p.is_file():
        return
    text = _read(p)
    if text is None:
        return
    targets = {t for t in _MAKE_TARGET_RE.findall(text) if not t.startswith(".")}
    used = False
    for alvo in ("test", "build", "lint"):
        if alvo in targets:
            cand["make"][alvo] = f"make {alvo}"
            used = True
    if used:
        _add_src(res, "Makefile")


def _fp_dockerfile(root: Path, res: dict, cand: dict) -> None:
    if (root / "Dockerfile").is_file():
        _add_src(res, "Dockerfile")


def _ext_census(root: Path) -> dict:
    """Conta arquivos-fonte por linguagem, ignorando diretórios de ruído."""
    counts = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in _SKIP_DIRS and not d.startswith(".")]
        dirnames[:] = [d for d in dirnames
                       if not (Path(dirpath) / d).is_symlink()
                       and not (Path(dirpath) / d).is_junction()]
        for fn in filenames:
            lang = _EXT_LANG.get(Path(fn).suffix.lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
    return counts


def _apply_ext_census(res: dict, counts: dict) -> None:
    if not counts:
        return
    top = max(counts.values())
    for lang, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if 2 * n >= top:  # >=50% do grupo majoritário
            _add_lang(res, lang)


def _py_unittest_fallback(root: Path, res: dict, cand: dict) -> None:
    if cand["py"].get("test"):
        return
    tests = root / "tests"
    if not tests.is_dir():
        return
    if "python" in res["linguagens"] or any(tests.glob("*.py")):
        _add_lang(res, "python")
        _add_src(res, "tests")
        cand["py"]["test"] = "python -m unittest discover -s tests"


def detect_project(root: str) -> dict:
    """Inspeciona <root> e devolve o dicionário de detecção (ver docstring do módulo).

    Levanta NotADirectoryError quando root não é um diretório existente.
    """
    rp = Path(root)
    if not rp.is_dir():
        raise NotADirectoryError(f"diretório inválido: {root}")
    res = _empty()
    cand = {eco: {} for eco in _ECO_ORDER}
    _fp_package_json(rp, res, cand)
    _fp_pyproject(rp, res, cand)
    _fp_requirements(rp, res, cand)
    _fp_pytest_cfg(rp, res, cand)
    _fp_cargo(rp, res, cand)
    _fp_go(rp, res, cand)
    _fp_makefile(rp, res, cand)
    _fp_dockerfile(rp, res, cand)
    _apply_ext_census(res, _ext_census(rp))
    _py_unittest_fallback(rp, res, cand)
    for field, key in (("test_cmd", "test"), ("build_cmd", "build"),
                       ("lint_cmd", "lint")):
        for eco in _ECO_ORDER:
            v = cand[eco].get(key)
            if v:
                res[field] = v
                break
    return res


def write_project(root: str, data: dict) -> str:
    """Grava <root>/.henke/PROJECT.json atomicamente (tmp + os.replace).

    Cria .henke/ quando ausente. Retorna o caminho absoluto gravado.
    """
    path = Path(root).resolve() / ".henke" / "PROJECT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Detecta linguagens/framework/comandos do projeto; com "
                    "--write grava .henke/PROJECT.json.")
    ap.add_argument("root", help="diretório do projeto a inspecionar")
    ap.add_argument("--write", action="store_true",
                    help="grava .henke/PROJECT.json e imprime o caminho")
    ap.add_argument("--json", action="store_true",
                    help="imprime o JSON de detecção (padrão sem --write)")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"ERRO: root inválido ou inexistente: {args.root}",
              file=sys.stderr)
        return 2
    try:
        res = detect_project(str(root))
    except OSError as e:
        print(f"ERRO: falha ao varrer '{root}': {e}", file=sys.stderr)
        return 2

    if args.write:
        try:
            path = write_project(str(root), res)
        except OSError as e:
            print(f"ERRO: não foi possível gravar PROJECT.json: {e}",
                  file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            print(f"OK: PROJECT.json gravado em {path}", file=sys.stderr)
        else:
            print(path)
        return 0

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
