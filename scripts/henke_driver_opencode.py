#!/usr/bin/env python3
"""henke_driver_opencode — adapter entre `henke auto --driver opencode` e o OpenCode.

Contrato cmd:: recebe o envelope JSON no stdin, executa `opencode run` no repo,
extrai o JSON de resultado {"files_changed": [...], "notes": "..."} impresso pelo
agente na última linha e devolve APENAS esse JSON no stdout.

Uso: henke_driver_opencode.py [provider/model]   (default: opencode/deepseek-v4.1-flash-free)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"


def _resolve_cli(name: str) -> str | None:
    """Localiza o executável — no Windows, CLIs npm são shims .cmd/.bat."""
    return shutil.which(name)


def _run_cli(argv: list[str], cwd: str) -> subprocess.CompletedProcess:
    exe = argv[0]
    if exe.lower().endswith((".cmd", ".bat")):
        argv = ["cmd", "/d", "/s", "/c", subprocess.list2cmdline(argv)]
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )

_INSTR = (
    "Voce e o executor de uma run da Suite Henke. Trabalhe APENAS dentro do "
    "repositorio atual. Execute a tarefa do campo `prompt` respeitando escopo e "
    "skills do envelope anexo (-f). Se houver `rework.findings`, corrija APENAS "
    "os achados listados. Ao terminar, imprima NA ULTIMA LINHA um unico objeto "
    'JSON: {"files_changed": ["caminho/relativo", ...], "notes": "resumo curto"}'
)


def _last_result_json(text: str) -> dict | None:
    """Último objeto JSON contendo 'files_changed' no stdout do agente."""
    found = None
    for match in re.finditer(r'\{[^{}]*"files_changed"[^{}]*\}', text):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(obj.get("files_changed"), list):
            found = obj
    return found


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    if model.startswith("-"):
        print(json.dumps({"files_changed": [], "notes": f"modelo inválido: {model!r}"}))
        return 1
    try:
        envelope = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        print(json.dumps({"files_changed": [], "notes": f"envelope ilegível: {exc}"}))
        return 1

    repo = str(envelope.get("repo_root") or ".")
    env_file = tempfile.NamedTemporaryFile(
        "w", suffix=".envelope.json", prefix="henke-", delete=False,
        dir=repo, encoding="utf-8",
    )
    try:
        json.dump(envelope, env_file, ensure_ascii=False)
        env_file.close()
        prompt = f"{_INSTR}\n\nEnvelope anexo: {Path(env_file.name).name}"
        exe = _resolve_cli("opencode")
        if exe is None:
            print(json.dumps({"files_changed": [], "notes": "opencode não encontrado no PATH"}))
            return 1
        try:
            res = _run_cli(
                [exe, "run", "-m", model, "--auto", "-f", env_file.name, prompt],
                cwd=repo,
            )
        except OSError as exc:
            print(json.dumps({"files_changed": [], "notes": f"opencode indisponível: {exc}"}))
            return 1

        obj = _last_result_json(res.stdout or "")
        if obj is None:
            # Sem JSON de resultado = driver não cumpriu o contrato → fracasso
            # real (exit != 0), nunca "sucesso com zero arquivos".
            obj = {
                "files_changed": [],
                "notes": f"opencode exit={res.returncode}; sem JSON de resultado "
                         f"(stdout_tail: {(res.stdout or '')[-300:]})",
            }
            print(json.dumps(obj, ensure_ascii=False))
            return 1
        else:
            obj = {
                "files_changed": [str(f) for f in obj["files_changed"]],
                "notes": str(obj.get("notes", ""))[:2000],
            }
            print(json.dumps(obj, ensure_ascii=False))
            return 0 if res.returncode == 0 else 1
    finally:
        try:
            os.unlink(env_file.name)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
