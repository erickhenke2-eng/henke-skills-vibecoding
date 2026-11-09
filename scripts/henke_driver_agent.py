#!/usr/bin/env python3
"""henke_driver_agent — adapter genérico `henke auto --driver agent:<cli>[:model]`.

Contrato cmd:: recebe o envelope JSON no stdin, invoca o CLI do agente no repo
(o envelope vai para um arquivo que o agente lê), extrai da saída o objeto
{"files_changed": [...], "notes": "..."} e devolve APENAS esse JSON no stdout.

Cada preset declara como invocar o CLI: `{prompt}` = instrução + caminho do
envelope; `{env}` = caminho do arquivo de envelope; `{model}` = modelo (opcional).

Ex.: henke_driver_agent.py claude            # claude -p ... --permission-mode acceptEdits
     henke_driver_agent.py opencode beam-free
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_DEFAULT_MODELS = {
    "opencode": "opencode/deepseek-v4.1-flash-free",
}

# {prompt} obrigatório; {env} e {model} opcionais (expandem só se presentes).
# Marcado ✓verificado = sintaxe conferida contra o --help do CLI instalado.
PRESETS: dict[str, list[str]] = {
    "opencode": ["opencode", "run", "-m", "{model}", "--auto", "-f", "{env}", "{prompt}"],
    "claude":   ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"],
    "codex":    ["codex", "exec", "--full-auto", "{prompt}"],
    "gemini":   ["gemini", "-p", "{prompt}", "-y"],
    "aider":    ["aider", "--yes", "--no-auto-commits", "--message", "{prompt}"],
    # ✓verificado: devin [OPTIONS] [PATH] [-- <PROMPT>]; 'accept-edits' aprova edições no workspace.
    "devin":    ["devin", "--permission-mode", "accept-edits", "--", "{prompt}"],
    # ✓verificado: agy -p (print mode não-interativo); accept-edits + skip-permissions p/ rodar sem prompt.
    "agy":      ["agy", "-p", "{prompt}", "--mode", "accept-edits", "--dangerously-skip-permissions"],
    # ✓binário existe: kiro-cli chat aceita mensagem posicional; --trust-all-tools p/ não promptar.
    "kiro":     ["kiro-cli", "chat", "--no-interactive", "--trust-all-tools", "{prompt}"],
    # não-verificado: sem CLI instalada; preset best-effort (falha honesto se inexistente).
    "qoder":    ["qoder", "-p", "{prompt}"],
}

_INSTR = (
    "Voce e o executor de uma run da Suite Henke. Trabalhe APENAS dentro do "
    "repositorio atual. Execute a tarefa do campo `prompt` do envelope no "
    "arquivo {env_name}, respeitando escopo e skills. Se houver "
    "`rework.findings`, corrija APENAS os achados listados. Ao terminar, "
    "imprima NA ULTIMA LINHA um unico objeto JSON: "
    '{{"files_changed": ["caminho/relativo", ...], "notes": "resumo curto"}}'
)


def _last_result_json(text: str) -> dict | None:
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
    cli = sys.argv[1] if len(sys.argv) > 1 else ""
    model = sys.argv[2] if len(sys.argv) > 2 else _DEFAULT_MODELS.get(cli, "")
    if cli not in PRESETS:
        print(json.dumps({
            "files_changed": [],
            "notes": f"cli '{cli}' sem preset. Disponíveis: {', '.join(sorted(PRESETS))}",
        }))
        return 2  # preset inválido = erro de uso (nunca verde)

    try:
        envelope = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        print(json.dumps({"files_changed": [], "notes": f"envelope ilegível: {exc}"}))
        return 2  # envelope ilegível = erro de uso (nunca verde)

    repo = str(envelope.get("repo_root") or ".")
    env_file = tempfile.NamedTemporaryFile(
        "w", suffix=".envelope.json", prefix="henke-", delete=False,
        dir=repo, encoding="utf-8",
    )
    try:
        json.dump(envelope, env_file, ensure_ascii=False)
        env_file.close()
        env_name = Path(env_file.name).name
        prompt = _INSTR.format(env_name=env_name)
        argv = [
            a.format(prompt=prompt, env=env_file.name, model=model) if "{" in a else a
            for a in PRESETS[cli]
        ]
        argv = [a for a in argv if a != ""]  # modelo ausente remove token vazio
        try:
            res = subprocess.run(
                argv, cwd=repo, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            print(json.dumps({"files_changed": [], "notes": f"cli '{cli}' indisponível: {exc}"}))
            return 1  # CLI ausente = falha de driver (nunca verde)

        obj = _last_result_json(res.stdout or "")
        if obj is None:
            print(json.dumps({
                "files_changed": [],
                "notes": f"{cli} exit={res.returncode}; sem JSON de resultado "
                         f"(stdout_tail: {(res.stdout or '')[-300:]})",
            }, ensure_ascii=False))
            return 1  # sem resultado = driver falhou (nunca verde)
        obj = {
            "files_changed": [str(f) for f in obj["files_changed"]],
            "notes": str(obj.get("notes", ""))[:2000],
        }
        if res.returncode != 0:
            obj["notes"] = (obj["notes"] + f" [cli exit {res.returncode}]").strip()
            print(json.dumps(obj, ensure_ascii=False))
            return 1  # CLI saiu com erro — propaga (nunca verde)
        print(json.dumps(obj, ensure_ascii=False))
        return 0
    finally:
        try:
            os.unlink(env_file.name)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
