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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# CWD-poisoning (banca v9 — A8): which()/exec não podem resolver binário
# plantado no diretório corrente do repo alvo no Windows.
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")

# `model` entra em argv de shim .cmd — \" escapado pelo list2cmdline não é
# compreendido pelo cmd.exe e & vira separador de comando (banca v9 A7).
_MODEL_RE = re.compile(r"[\w./:@+-]+")

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
    # MÉDIO-2 (banca v13): last-match no stdout INTEIRO — um `cat` de arquivo
    # do repo com {"files_changed": [...]} depois do JSON real spoofava a
    # declaração e contornava o cap NO-OP-UNVERIFIED. Contrato = última linha;
    # fallback: PRIMEIRO match (o real vem antes do conteúdo despejado).
    non_empty = [ln for ln in text.splitlines() if ln.strip()]
    if non_empty:
        try:
            obj = json.loads(non_empty[-1])
            if isinstance(obj, dict) and isinstance(obj.get("files_changed"), list):
                return obj
        except ValueError:
            pass
    # A10 (banca v14): último match, não primeiro — dump de arquivo com o
    # padrão ANTES do resultado real vencia o fallback.
    for match in list(re.finditer(r'\{[^{}]*"files_changed"[^{}]*\}', text))[::-1]:
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        if isinstance(obj.get("files_changed"), list):
            return obj  # último match — o resultado real é o último JSON
    return None


def _strip_untrusted_memory(envelope: dict) -> None:
    """Item6/P1-02: strip de campos de memória não confiável antes do
    envelope chegar a CLI full-auto. O contrato (_build_envelope) já não
    emite `memory` p/ driver não supervisionado, mas envelope montado à
    mão/outra versão podia contrabandeá-la — strip incondicional aqui é
    defesa em profundidade (in-place, idempotente)."""
    stripped = envelope.pop("memory", None)
    had_policy = "memory_policy" in envelope
    envelope.pop("memory_policy", None)
    if stripped is not None or had_policy:
        envelope.setdefault("_sanitized", {})["memory_stripped"] = True


def _safe_env() -> dict:
    """A4 (banca v14): adapters herdavam env cru (GIT_*/PYTHONPATH subvertem
    o CLI do agente)."""
    return {k: v for k, v in os.environ.items()
            if not (k.startswith("GIT_") or k.startswith("PYTHON")
                    or k.startswith("LD_") or k.startswith("DYLD_")
                    or k in ("BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS",
                             "PERL5OPT", "PERL5LIB", "RUBYOPT",
                             "DOTNET_STARTUP_HOOKS"))}


def main() -> int:
    cli = sys.argv[1] if len(sys.argv) > 1 else ""
    # B6 (banca v13): `--driver agent:cli:` (dois-pontos no fim) → model=""
    # → filtro removia o "" mas deixava "-m" solto engolindo --auto.
    model = (sys.argv[2] or _DEFAULT_MODELS.get(cli, "")) if len(sys.argv) > 2 else _DEFAULT_MODELS.get(cli, "")
    if cli not in PRESETS:
        print(json.dumps({
            "files_changed": [],
            "notes": f"cli '{cli}' sem preset. Disponíveis: {', '.join(sorted(PRESETS))}",
        }))
        return 2  # preset inválido = erro de uso (nunca verde)
    if model and not _MODEL_RE.fullmatch(model):
        print(json.dumps({"files_changed": [], "notes": f"modelo inválido: {model!r}"}))
        return 2

    try:
        envelope = json.load(sys.stdin)
    except (OSError, ValueError) as exc:
        print(json.dumps({"files_changed": [], "notes": f"envelope ilegível: {exc}"}))
        return 2  # envelope ilegível = erro de uso (nunca verde)

    repo = str(envelope.get("repo_root") or ".")
    if not Path(repo).is_dir():
        print(json.dumps({"files_changed": [], "notes": f"repo_root inexistente: {repo!r}"}))
        return 2
    # Item6 (driver sandbox — defesa em profundidade): TODA CLI preset é
    # full-auto (--trust-all-tools/--full-auto/-y/accept-edits — sem humano
    # no loop). Memória de repo é conteúdo NÃO CONFIÁVEL com potencial de
    # prompt-injection — o adapter STRIPA antes do env_file.
    _strip_untrusted_memory(envelope)
    # Envelope no TEMP do sistema — dentro do repo ele aparecia no git status
    # da própria run e sobrevivia a SIGKILL com o prompt inteiro (banca v9 M7).
    env_file = tempfile.NamedTemporaryFile(
        "w", suffix=".envelope.json", prefix="henke-", delete=False,
        encoding="utf-8",
    )
    try:
        json.dump(envelope, env_file, ensure_ascii=False)
        env_file.close()
        env_name = env_file.name
        prompt = _INSTR.format(env_name=env_name)
        argv = [
            a.format(prompt=prompt, env=env_file.name, model=model) if "{" in a else a
            for a in PRESETS[cli]
        ]
        argv = [a for a in argv if a != ""]  # modelo ausente remove token vazio
        # CLIs de agente no Windows são shims npm `.cmd` — CreateProcess não os
        # resolve por nome nem executa batch direto. `which` acha o path real;
        # `.cmd/.bat` precisam do cmd.exe na frente.
        exe = shutil.which(argv[0])
        if exe is None:
            print(json.dumps({"files_changed": [], "notes": f"cli '{cli}' indisponível (não está no PATH)"}))
            return 1  # CLI ausente = falha de driver (nunca verde)
        # A9 (banca v14): which() sem re-check — em py<3.12 procura no CWD;
        # shim `claude.cmd` plantado no repo virava "o driver".
        try:
            _exe_r = Path(exe).resolve()
            _repo_r = Path(repo).resolve()
            if _exe_r == _repo_r or _repo_r in _exe_r.parents:
                print(json.dumps({"files_changed": [], "notes": f"cli '{cli}' resolve dentro do repo — recusado (shim plantado)"}))
                return 2
        except OSError:
            pass
        argv[0] = exe
        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            # A7 (banca v16): COMSPEC envenenado executava binário arbitrário
            # no wrap .cmd/.bat — literal igual ao henke_cli.
            argv = ["cmd.exe", "/d", "/s", "/c", *argv]
        try:
            res = subprocess.run(
                argv, cwd=repo, capture_output=True, text=True,
                encoding="utf-8", errors="replace", env=_safe_env(),
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
    # ISO-BOOT canônico (scripts/_isoboot.py) — sitecustomize/usercustomize/
    # .pth hostis executam no startup e patcham verificadores no pai; o
    # respawn -Es nasce sem esses canais (prova ESTRUTURAL: sys.flags
    # autenticado + os.sys — nunca variável de ambiente). Texto sincronizado
    # por tests/test_isoboot_guard.py — editar só alterando o canônico.
    import importlib.util as _ilu
    import os.path as _osp
    _ib_p = _osp.join(_osp.dirname(_osp.abspath(__file__)), "_isoboot.py")
    if not _osp.isfile(_ib_p):
        _ib_p = _osp.join(_osp.dirname(_osp.dirname(_osp.abspath(__file__))),
                          "_isoboot.py")
    _ib_spec = _ilu.spec_from_file_location("henke_isoboot", _ib_p)
    _ib_mod = _ilu.module_from_spec(_ib_spec)
    _ib_spec.loader.exec_module(_ib_mod)
    _ib_mod.ensure_isolated(__file__, sys.argv[1:])
    sys.exit(main())
