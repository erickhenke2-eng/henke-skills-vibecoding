#!/usr/bin/env python3
"""
Portão Mecânico de Auditoria do Ledger Executável (PROTOCOLO v4.0, §6 e §10).
Inspeciona comandos executados contra denylist de risco, verifica resolução
de scripts efetivos, valida evidência criptográfica (`exec_log.jsonl`) e
barra vitória por tautologia (0 testes descobertos).
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

HAZARDOUS_PATTERNS = [
    (r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b", "Tentativa de remoção recursiva forçada (rm -rf)"),
    (r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b", "Tentativa de remoção recursiva forçada (rm -fr)"),
    (r"\bgit\s+push\b.*(?:--force|-f\b)", "Comando 'git push --force' destrutivo proibido em ledger"),
    # M3 (banca v15): args interpostos entre binário e subcomando evadiam
    # `git\s+push` — `git -C . push -f`, `git -c x=y push`, `push -fu` bundle.
    (r"\bgit\b[^|;&]*\bpush\b[^|;&]*(?:--force|-\w*f\b|-f\w*\b|\+\S*)",
     "git push forçado (args interpostos/bundle/refspec +) proibido"),
    (r"\b(?:npm|pnpm|yarn|bun)\b[^|;&]*\bpublish\b|\bnpx\b[^|;&]*\bpublish\b",
     "Comando de publicação (publish, args interpostos) proibido em ledger"),
    (r"\b(?:cargo|poetry|uv)\s+publish\b|\bgem\s+push\b|\bdotnet\s+nuget\s+push\b",
     "Publicação de pacote (cargo/poetry/uv/gem/dotnet) proibida"),
    (r"\bfind\b[^|;&]*-delete\b|\b(?:mkfs|fdisk|wipefs|shred|sfdisk)\b|"
     r"\brobocopy\b[^|;&]*/mir\b|\bcipher\b[^|;&]*/w\b",
     "Comando destrutivo de disco/arquivos proibido"),
    (r"\b(?:certutil|bitsadmin|msbuild|msiexec|installutil|regsvcs)\b[^|;&]*"
     r"(?:-urlcache|/transfer|/i\b[^|;&]*http|http|-decode|-encode)",
     "LOLBin de download/execução proibido"),
    # A11 (banca v16): powershell -c/-Command/-File/-i não são '-e*';
    # rm isolado com -r OU -f; install/exec de pacote remoto = code-exec.
    (r"\b(?:powershell|pwsh)\b[^|;&]*-(?:c|co|com|comm|command|f|fi|fil|file|i|inputformat)\b",
     "powershell com comando/arquivo (-c/-f) proibido"),
    (r"\brm\b[^|;&]*(?:-\w*r\b|--recursive\b|-\w*f\b|--force\b)",
     "rm com flag destrutiva (-r/-f isolado) proibido"),
    (r"\bnpx\b|\b(?:npm|pnpm|yarn)\s+(?:i|install|add|ci|dlx|exec)\b|"
     r"\bpip[\d.]*\s+install\b|\bcargo\s+install\b|\bgem\s+install\b",
     "Instalação/execução de pacote remoto (postinstall/dlx) proibida"),
    (r"\b(?:npm|pnpm|yarn)\s+publish\b", "Comando de publicação (publish) proibido em ledger"),
    (r"\b(?:pip|twine)\s+upload\b", "Comando de upload de pacote proibido em ledger"),
    (r"\bchmod\s+(?:-R\s+)?777\b", "Permissão insegura 777 detectada"),
    # M9 (banca v13): sinks de pipe além de sh/bash (zsh/fish/dash/python/
    # node/iex) passavam; formas curtas PS e verbos destrutivos extra.
    (r"(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b.*"
     r"\|\s*(?:(?:ba|z|fi|da)?sh|pwsh|powershell|iex|python[\d.]*|node|perl|ruby)\b",
     "Execução remota cega via pipe proibida"),
    (r"\bRemove-Item\b[^|;&]*-(?:r|re|rec|recurse|f|fo|force)\b",
     "Remove-Item recursivo/forçado proibido"),
    (r"\b(?:ri|erase)\b[^|;&]*(?:-Recurse|/s\b)", "Apagamento recursivo proibido"),
    (r"\b(?:Format-Volume|Clear-Disk|vssadmin\s+delete|reg\s+delete|dd\s+\S*of=/dev/)\b",
     "Comando destrutivo de sistema proibido"),
    # A11 (banca v14): powershell -enc/-e e LOLBins de execução remota.
    (r"\b(?:powershell|pwsh)\b[^|;&]*-(?:e|en|ec|ep|enc|encodedcommand)\b",
     "powershell com comando codificado (-enc) proibido"),
    (r"\b(?:mshta|wscript|cscript)\b|\bregsvr32\b[^|;&]*/i:http|\brundll32\b[^|;&]*javascript:",
     "LOLBin de execução remota proibido"),
    (r"\bformat\b\s+[a-zA-Z]:", "Comando de formatação de disco detectado"),
    (r"\brm\b(?=[^|;&]*\s(?:-\w*r|--recursive)\b)(?=[^|;&]*\s(?:-\w*f|--force)\b)",
     "Remoção recursiva forçada (flags separadas) proibida"),
    (r"\bgit\s+reset\b[^|;&]*--hard\b", "git reset --hard destrutivo proibido em ledger"),
    (r"\bgit\s+clean\b[^|;&]*-\w*f", "git clean -f destrutivo proibido em ledger"),
    (r"\bgit\s+push\b[^|;&]*\+", "git push com refspec forçado (+ref) proibido"),
    (r"\bRemove-Item\b[^|;&]*(?:-Recurse|-Force)", "Remove-Item recursivo/forçado proibido"),
    (r"\b(?:rd|rmdir|del)\b[^|;&]*/s\b", "rd/del /s destrutivo proibido"),
    (r"\bgh\s+release\s+(?:create|upload)\b", "gh release (publish) proibido em ledger"),
    (r"\b(?:vercel|netlify|flyctl)\b[^|;&]*(?:--prod|deploy)\b", "Deploy para produção proibido em ledger"),
    (r"\bdocker\s+push\b", "docker push (publish) proibido em ledger"),
]

# Resolução real de lifecycle npm (anti-TOCTOU): `npm test` não é string cega —
# o pipeline executa pretest -> test -> posttest, e `npm run X` expande preX/X/postX.
# A5 (banca v12): args/flags após o script NÃO podem pular a expansão —
# `npm test -- --runInBand`/`npm run test -s` rodam pretest/posttest de verdade.
# H4 (banca v13): flags ANTES do script (`npm -s test`, `npm --prefix x test`)
# viravam o nome capturado e a expansão sumia; `npm run build.prod` falhava o
# regex inteiro; `npm.cmd` não era reconhecido como runner.
_NPM_CMD_RE = re.compile(
    r"^\s*(?:npm|pnpm|yarn|bun)(?:\.(?:cmd|exe|bat|com|ps1))?"
    r"\s+(?:-\S+\s+)*(?:run\s+)?([^\s]+)(?:\s|$)", re.IGNORECASE)
_NPM_RUN_RE = re.compile(
    r"(?:npm|pnpm|yarn|bun)(?:\.(?:cmd|exe|bat|com|ps1))?\s+"
    r"(?:-\S+\s+)*(?:run\s+)?([^\s]+)", re.IGNORECASE)
# A5 (banca v14): flag COM argumento separado (`--prefix . test`,
# `-w pkg test`) — regex comia o flag e capturava o ARGUMENTO como script,
# escondendo o lifecycle da denylist.
_NPM_FLAGS_TAKING_ARG = {
    "--prefix", "-w", "--workspace", "--cache", "--userconfig",
    "--location", "--loglevel", "--registry", "-C", "--cwd",
}


def _npm_script_name(cmd: str) -> str | None:
    """Extrai o nome do script npm via tokenizer — pula runner, flags e os
    argumentos das flags que tomam valor separado."""
    toks = cmd.strip().split()
    # A5 (banca v16): wrappers escondiam o runner — `env npm test`,
    # `sudo npm install`, `nice -n5 npm t`, `timeout 10 npm run x`,
    # `cmd /c npm t` devolviam None e os hooks ficavam invisíveis.
    _WRAPPERS = {"env", "sudo", "doas", "nice", "nohup", "timeout", "time"}
    while toks and toks[0].lower() in _WRAPPERS:
        toks.pop(0)
        while toks and (toks[0].startswith("-") or "=" in toks[0]):
            toks.pop(0)  # flags e VAR=val do wrapper
    if len(toks) >= 2 and toks[0].lower() in ("cmd", "cmd.exe") and toks[1] == "/c":
        toks = toks[2:]
    if not toks or not re.fullmatch(
            r"(?:npm|pnpm|yarn|bun)(?:\.(?:cmd|exe|bat|com|ps1))?",
            toks[0], re.IGNORECASE):
        return None
    toks = toks[1:]
    def _skip_flags(t: list) -> None:
        while t and t[0].startswith("-"):
            flag = t.pop(0)
            if flag in _NPM_FLAGS_TAKING_ARG and t:
                t.pop(0)  # argumento separado da flag
    _skip_flags(toks)
    if toks and toks[0] in ("run", "run-script", "rum"):
        toks.pop(0)
        # M2 (banca v15): `npm run -s build` — flags também depois de 'run'.
        _skip_flags(toks)
    script = toks[0] if toks else None
    # A5 (banca v16): aliases executam o lifecycle do nome REAL —
    # `npm t` roda pretest/test/posttest, `npm i`/`npm ci`/`npm add` rodam
    # preinstall/install/postinstall. Devolve o nome canônico.
    _ALIAS = {"t": "test", "tst": "test",
              "i": "install", "add": "install", "in": "install",
              "ins": "install", "inst": "install", "insta": "install",
              "instal": "install", "isntall": "install", "ci": "install",
              "cit": "install", "it": "install"}
    if script is not None:
        script = _ALIAS.get(script.lower(), script)
    return script
# M8 (banca v13): shapes de teste fora do regex declaravam testes sem nenhum
# registro exigido — cobre `npm run test`, runners via npx/deno/dotnet/ctest.
_TEST_CMD_RE = re.compile(
    r"\b(?:pytest|py\.test|tox|nox|jest|vitest|mocha|unittest|playwright|"
    r"cargo\s+test|go\s+test|ctest|dotnet\s+test|deno\s+test|"
    # DS-1: runners que o ledger aceitava como saída mas o classificador
    # não reconhecia — `make test`/`rspec`/`phpunit`/`mvn`/`gradle`/`sbt`/`mix`.
    r"make\s+(?:test|tests|check)\b|rspec\b|phpunit\b|"
    r"mvn(?:w)?\s+(?:test|verify)\b|gradle(?:w)?\s+test\b|sbt\s+test\b|"
    r"mix\s+test\b|"
    r"(?:npm|pnpm|yarn|bun)(?:\.(?:cmd|exe|bat|com|ps1))?\s+"
    # F1 (opus v15): `\S*test\S*` casava 'latest'/'attest'/'pretest' — script
    # token exige prefixo 'test'/'t' (mesma borda do is_test_cmd).
    r"(?:(?:-\S+\s+)*(?:run\s+)?)(?:t|test(?:[.:_/-]\S*)?|teste\w*)\b|"
    r"npx\s+(?:jest|vitest|mocha|playwright|cypress))\b",
    re.IGNORECASE,
)


# PT-01: flags cujo argumento é CÓDIGO AVALIADO — flag, payload e argv nunca
# são um comando executável; um `pytest` ali dentro mintava crédito falso.
_EVAL_FLAG = {"python": {"-c"}, "py": {"-c"},
              "node": {"-e", "-p", "--eval"},
              "ruby": {"-e"}, "perl": {"-e"}, "php": {"-r"}}
_INTERP_RE = re.compile(
    r"(python[\d.]*|py(?:-\d(?:\.\d+)?)?|node(?:js)?|ruby|perl|php)(?:\.(?:exe|cmd|bat|com))?",
    re.IGNORECASE)
# Shells (`sh -c`, `cmd /c`, `pwsh -Command`) NÃO estão aqui de propósito:
# a string deles É o comando executado — mascará-la apagaria `sh -c 'pytest'`.
# Passthrough de saída: `echo "pytest; 10 passed"` nunca EXECUTA nada — os
# argumentos são só texto; mascarados até o próximo separador.
_PASSTHRU_RE = re.compile(
    r"(?:echo|printf|print|write-output)(?:\.(?:exe|cmd|bat|com))?",
    re.IGNORECASE)


def _interp_kind(tok: str) -> str | None:
    m = _INTERP_RE.fullmatch(tok)
    if not m:
        return None
    base = m.group(1).lower()
    if base == "nodejs":
        return "node"
    return "py" if base.startswith("py") and not base.startswith("python") else base.rstrip("0123456789.")


def _strip_interp_argv(full_cmd: str) -> str:
    """Remove payload/argv pós-entry-point de invocações de interpretador.

    `python -c "print('1 passed')" -m pytest` minta crédito porque o detector
    via o token `pytest` — mas após `-c <code>` (ou um script path, ou uma
    flag eval de node/ruby/perl/php) tudo é sys.argv, NUNCA executado.
    `-m <mod>` é exceção: o módulo É o runner real e fica visível. Tokens de
    um segundo comando (`&& pytest`) continuam visíveis."""
    try:
        toks = shlex.split(full_cmd)
    except ValueError:
        toks = full_cmd.split()
    if "\\" in full_cmd:
        # shlex POSIX devora '\' fora de aspas: `C:\Python312\python.exe`
        # virava `C:Python312python.exe` e o basename não casava — re-tokeniza
        # preservando barras e remove as aspas literais que posix=False
        # deixa presas aos tokens.
        try:
            toks = [
                t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'"
                else t
                for t in shlex.split(full_cmd, posix=False)]
        except ValueError:
            pass
    out: list[str] = []
    i = 0
    while i < len(toks):
        # R2-PT-01: `henke exec` normaliza o interpretador para caminho
        # absoluto (C:\...\python.exe) — o fullmatch no token inteiro não o
        # reconhecia e o `-m pytest` pós-`-c` voltava a mintar crédito.
        # Compara sempre pelo basename (sep `\` e `/` — o exec_log pode ter
        # sido gravado em outro SO).
        base_tok = toks[i].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        kind = _interp_kind(base_tok)
        if kind is None:
            if _PASSTHRU_RE.fullmatch(base_tok):
                i += 1
                while (i < len(toks)
                       and toks[i] not in ("&&", "||", ";", "|")):
                    i += 1
                continue
            out.append(toks[i])
            i += 1
            continue
        j = i + 1
        keep_until = j  # tokens toks[i:keep_until] permanecem visíveis
        while j < len(toks):
            f = toks[j]
            if f in _EVAL_FLAG.get(kind, set()):
                j += 2  # flag eval + payload: some tudo (flag, código, argv)
                keep_until = i + 1
                break
            if kind in ("python", "py") and f == "-m":
                j += 2  # -m <mod>: o módulo É o runner — permanece visível
                keep_until = j
                break
            if f in ("-W", "-X"):
                j += 2
                continue
            if f.startswith("-"):
                j += 1
                continue
            j += 1  # script path permanece (padrão `python tests/x.py`)
            keep_until = j
            break
        out.extend(toks[i:keep_until])
        k = j
        while k < len(toks) and toks[k] not in ("&&", "||", ";", "|"):
            k += 1
        i = k
    return " ".join(out)


def is_test_cmd(full_cmd: str) -> bool:
    """Detecta comando de teste com word-boundary (não confunde 'echo contest' com teste)."""
    full_cmd = _strip_interp_argv(full_cmd)
    if _TEST_CMD_RE.search(full_cmd):
        return True
    # A5 (banca v14): `npm -w pkg test`/`npm t` — script resolvido via
    # tokenizer (regex não cobria flag+arg nem o alias `t`).
    # A1 (banca v15): 'latest'/'attest'/'pretest' casavam como script-teste —
    # borda estrita: t|test|test:NOME|teste* prefixo de token.
    script = _npm_script_name(full_cmd)
    if script is not None:
        return bool(re.fullmatch(r"t|test(?:[.:_/-].*)?|teste\w*", script))
    # ava/tape como EXECUTÁVEL (1º token) + `python tests/x.py`/`test_*.py`/
    # `*_test.py` (henke_cli aceita — A2 v15: divergência = wedge).
    toks = full_cmd.strip().split()
    if toks and toks[0].lower() in ("ava", "tape"):
        return True
    return bool(re.search(
        r"\bpython[\d.]*\s+(?:tests?[\\/]|test_\w+\.py|\w+_test\.py)",
        full_cmd, re.IGNORECASE))


def resolve_npm_lifecycle(cmd: str, pkg: dict) -> list[tuple[str, str]]:
    """Resolve o grafo de hooks npm em ordem de execução: [(hook, corpo), ...].
    `npm test`/`pnpm test`/`yarn test` -> pretest/test/posttest;
    `npm run X` -> preX/X/postX. Expansão transitiva de `npm run Y` nos corpos,
    com guarda de ciclo e profundidade máxima 8."""
    scripts = (pkg or {}).get("scripts")
    script_name = _npm_script_name(cmd)
    if script_name is None or not isinstance(scripts, dict):
        return []
    chain: list[tuple[str, str]] = []
    visited: set[str] = set()

    def expand(name: str, depth: int) -> None:
        if name in visited or depth > 8:
            return
        visited.add(name)
        for hook in (f"pre{name}", name, f"post{name}"):
            body = scripts.get(hook)
            if not isinstance(body, str):
                continue
            chain.append((hook, body))
            for sub in _NPM_RUN_RE.findall(body):
                if sub != hook:
                    expand(sub, depth + 1)

    expand(script_name, 0)
    return chain


def _full_command_str(entry: dict) -> str:
    cmd = entry.get("cmd", "")
    args_list = entry.get("args", [])
    if isinstance(args_list, str) and args_list.strip():
        return f"{cmd} {args_list}".strip()
    if isinstance(args_list, list) and args_list:
        return f"{cmd} {' '.join(str(a) for a in args_list)}".strip()
    if args_list:
        # A6 (banca v14): args dict/int/escondido caía silencioso — o
        # comando perigoso ficava invisível à denylist. Serializa qualquer
        # tipo pra nunca ocultar conteúdo.
        return f"{cmd} {json.dumps(args_list, ensure_ascii=False, default=str)}".strip()
    return str(cmd).strip()


def verify_log_chain(log_p: Path) -> tuple[list[str], str | None]:
    """Verifica a cadeia de hashes do exec_log e a âncora local da ponta.

    Retorna (erros, último entry_hash válido). Usado por verify_exec_log e por
    `henke exec` ANTES de fazer append — encadear sobre um log plantado
    lavaria a âncora da ponta com uma execução legítima (banca v9 — A1).
    """
    log_p = Path(log_p)
    if not log_p.is_file():
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: arquivo de log '{log_p}' inexistente."], None
    raw_lines = _read_log_lines(log_p)
    if raw_lines is None:
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: falha ao ler '{log_p}'."], None
    if not raw_lines:
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: '{log_p}' está vazio."], None
    errors, last, _recs = _verify_log_chain_core(raw_lines, log_p)
    return errors, last


def _read_log_lines(log_p: Path) -> list[str] | None:
    try:
        return [ln.strip() for ln in log_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception:
        return None


def _verify_log_chain_core(raw_lines: list[str], log_p: Path) -> tuple[list[str], str | None, list[dict]]:
    """Núcleo compartilhado: cadeia+âncora+registros válidos sobre UMA leitura.
    M6 (banca v13): verify_exec_log relia o arquivo depois de verify_log_chain
    — swap na janela entre as duas leituras casava o ledger com um arquivo
    nunca validado. Uma leitura, uma verdade."""
    errors: list[str] = []
    verified_records: list[dict] = []
    last_ehash: str | None = None
    for idx, line in enumerate(raw_lines, 1):
        try:
            rec = json.loads(line)
        except Exception:
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: JSON malformado na linha {idx} de '{log_p.name}'.")
            continue
        if not isinstance(rec, dict):
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: linha {idx} de '{log_p.name}' não é objeto JSON.")
            continue

        cmd_r = str(rec.get("cmd", "")).strip()
        exit_r = rec.get("exit", rec.get("exit_code", 0))
        tests_r = rec.get("testes_descobertos", 0)
        sha_r = str(rec.get("stdout_sha256", "")).strip()
        prev_r = str(rec.get("prev_hash", "")).strip()
        ehash_r = str(rec.get("entry_hash", "")).strip()

        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha_r):
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: stdout_sha256 inválido na linha {idx} de '{log_p.name}'.")
            continue

        if not isinstance(exit_r, int) or isinstance(exit_r, bool):
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: exit não-inteiro na linha {idx} de '{log_p.name}'.")
            continue

        if idx == 1 and prev_r != "0" * 64:
            # Gênese: a primeira entrada deve nascer do hash-zero — um log
            # reescrito a partir do meio finge continuidade (F-13).
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: prev_hash de gênese inválido na linha 1 de '{log_p.name}'."
            )
            continue

        if idx > 1 and not prev_r:
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: prev_hash ausente na linha {idx} de '{log_p.name}'.")
            continue

        if idx > 1 and last_ehash and prev_r and prev_r != last_ehash:
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: quebra de encadeamento prev_hash na linha {idx} de '{log_p.name}'."
            )
            continue

        if not ehash_r:
            errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: entry_hash ausente na linha {idx} de '{log_p.name}'.")
            continue

        cand_pipe = hashlib.sha256(
            f"{prev_r}|{cmd_r}|{exit_r}|{tests_r}|{sha_r}".encode("utf-8")
        ).hexdigest()
        body_no_ehash = {k: v for k, v in rec.items() if k != "entry_hash"}
        cand_json1 = hashlib.sha256(
            json.dumps(body_no_ehash, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        cand_json2 = hashlib.sha256(
            json.dumps(body_no_ehash, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if ehash_r not in (cand_pipe, cand_json1, cand_json2):
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: entry_hash adulterado na linha {idx} de '{log_p.name}'."
            )
            continue

        last_ehash = ehash_r
        verified_records.append({
            "cmd": cmd_r,
            "exit": exit_r,
            "testes_descobertos": tests_r,
            "stdout_sha256": sha_r,
        })

    # Âncora da ponta (F-13): a cadeia é consistente por construção até quando
    # reescrita do zero — o tip gravado LOCALMENTE por `henke exec` é a prova
    # de que a história termina onde a última execução real terminou.
    repo_root = log_p.parent.parent if log_p.parent.name == ".henke" else None
    if repo_root is None and last_ehash:
        # A6 (banca v8): log fora de <repo>/.henke/ pulava o check da ponta
        # em silêncio — sem âncora resolvível a cadeia é só auto-consistente.
        errors.append(
            "UNVERIFIED_EXECUTION_EVIDENCE: exec_log fora de '<repo>/.henke/' "
            "— âncora local da ponta não é resolvível, integridade não provada"
        )
    if repo_root is not None and last_ehash:
        # A6 (banca v12): 'from scripts import' resolve por PYTHONPATH —
        # scripts/run_state.py falso era importado e spoofava o tip.
        # Carrega SEMPRE por path explícito ao lado deste arquivo.
        _rs_tip = None
        try:
            import importlib.util as _ilu2
            _rs_spec = _ilu2.spec_from_file_location(
                "run_state", Path(__file__).resolve().parent / "run_state.py")
            if _rs_spec and _rs_spec.loader:
                _rs_tip = _ilu2.module_from_spec(_rs_spec)
                _rs_spec.loader.exec_module(_rs_tip)
        except Exception:
            _rs_tip = None
        if _rs_tip is None:
            # H1 (banca v13): falha de import silenciava a ÚNICA âncora
            # anti-rewrite — log inteiramente forjado auditava verde.
            errors.append(
                "UNVERIFIED_EXECUTION_EVIDENCE: módulo run_state "
                "indisponível — âncora de ponta não verificada"
            )
        else:
            expected_tip = _rs_tip.load_exec_tip(repo_root)
            if expected_tip is None:
                errors.append(
                    f"UNVERIFIED_EXECUTION_EVIDENCE: exec_log sem âncora local de ponta "
                    f"(exec-tips ausente) — log possivelmente plantado."
                )
            elif expected_tip != last_ehash:
                errors.append(
                    f"UNVERIFIED_EXECUTION_EVIDENCE: ponta do exec_log diverge da "
                    f"âncora local — cadeia reescrita/truncada."
                )

    return errors, last_ehash, verified_records


def verify_exec_log(entries: list[dict], exec_log_path: Path) -> list[str]:
    """Verifica a cadeia de hashes de `.henke/exec_log.jsonl` e cruza cada comando
    de teste do ledger contra os registros reais executados."""
    log_p = Path(exec_log_path)
    if not log_p.is_file():
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: arquivo de log '{log_p}' inexistente."]
    raw_lines = _read_log_lines(log_p)
    if raw_lines is None:
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: falha ao ler '{log_p}'."]
    if not raw_lines:
        return [f"UNVERIFIED_EXECUTION_EVIDENCE: '{log_p}' está vazio."]
    errors, _last, verified_records = _verify_log_chain_core(raw_lines, log_p)

    # A9 (banca v16): denylist nunca tocava os records do exec_log —
    # `henke exec -- cmd /c "del /s /q x"` ficava custodiado e nunca sinalizado.
    for j, vrec in enumerate(verified_records, 1):
        vcmd = str(vrec.get("cmd", ""))
        for pattern, reason in HAZARDOUS_PATTERNS:
            if re.search(pattern, vcmd, flags=re.IGNORECASE):
                errors.append(
                    f"EXEC-LOG-HAZARDOUS: comando #{j} no exec_log viola "
                    f"denylist: '{vcmd[:120]}' ({reason})")
                break

    for i, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        full_cmd = _full_command_str(entry)
        raw_cmd = str(entry.get("cmd", "")).strip()
        if not (is_test_cmd(full_cmd) or "testes_descobertos" in entry or "stdout_sha256" in entry):
            continue
        if not is_test_cmd(full_cmd):
            # H2 (banca v13): `echo "5 passed"` minta registro de log honesto
            # que casava com entrada de ledger editada à mão — declaração de
            # teste exige que o comando seja um runner reconhecido.
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: entrada #{i} declara testes "
                f"mas '{full_cmd}' não é runner de teste reconhecido."
            )
            continue
        e_exit = entry.get("exit", entry.get("exit_code"))
        e_tests = entry.get("testes_descobertos")
        e_sha = entry.get("stdout_sha256")

        matched = False
        for vrec in verified_records:
            if vrec["cmd"] not in (full_cmd, raw_cmd):
                continue
            if vrec["exit"] != e_exit or vrec["testes_descobertos"] != e_tests:
                continue
            if e_sha is not None and vrec["stdout_sha256"] != str(e_sha).strip():
                continue
            matched = True
            # A7 (banca v12): consumo 1:1 — um registro do log não pode
            # validar N entradas duplicadas do ledger.
            verified_records.remove(vrec)
            break
        if not matched:
            errors.append(
                f"UNVERIFIED_EXECUTION_EVIDENCE: entrada #{i} ('{full_cmd}', testes_descobertos={e_tests}) "
                f"sem registro correspondente íntegro em '{log_p.name}'."
            )

    return errors


def audit_ledger_content(ledger_data: list[dict], allow_zero_tests: bool = False,
                         pkg: dict | None = None, exec_log_path: Path | None = None,
                         require_exec_log: bool = False) -> tuple[bool, list[str], int]:
    errors = []
    total_tests_discovered = 0

    if not isinstance(ledger_data, list):
        return False, ["Formato do ledger inválido: esperado array/lista JSON de comandos."], 0

    if len(ledger_data) == 0:
        return False, ["Ledger vazio: nenhuma evidência de execução física foi gravada."], 0

    for i, entry in enumerate(ledger_data, 1):
        if not isinstance(entry, dict):
            errors.append(f"Entrada #{i} não é um objeto JSON válido.")
            continue

        cmd = entry.get("cmd", "")
        if not cmd:
            errors.append(f"Entrada #{i} sem comando ('cmd') especificado.")
            continue
        # A10 (banca v16): cmd lista/dict virava repr ("['rm', '-rf']") que
        # nenhum padrão casava — schema exige string, fail-closed.
        if not isinstance(cmd, str):
            errors.append(
                f"Entrada #{i} com 'cmd' não-string ({type(cmd).__name__}) "
                "— formato inválido, recusado.")
            continue

        full_cmd = _full_command_str(entry)

        # Strings a inspecionar contra denylist (inclui script resolvido e a forma
        # normalizada do executável sem extensão Windows: git.exe -> git)
        head_exe, _, tail_args = full_cmd.partition(" ")
        exe_base = os.path.basename(head_exe).lower().replace(".exe", "").replace(".bat", "")
        normalized_cmd = f"{exe_base} {tail_args}".strip()
        strings_to_check = [full_cmd]
        if normalized_cmd != full_cmd:
            strings_to_check.append(normalized_cmd)
        if "executável_resolvido" in entry:
            strings_to_check.append(str(entry["executável_resolvido"]).strip())
        # Anti-TOCTOU: expande o grafo de hooks npm — corpos de pre*/post* entram
        # na denylist com o nome do hook (TOCTOU escondido em lifecycle).
        for hook, body in resolve_npm_lifecycle(normalized_cmd, pkg):
            strings_to_check.append(f"{hook}: {body}")

        # 1. Checagem contra padrões destrutivos (Heurística Denylist)
        for s in strings_to_check:
            for pattern, reason in HAZARDOUS_PATTERNS:
                if re.search(pattern, s, flags=re.IGNORECASE):
                    errors.append(f"Entrada #{i} bloqueada por denylist de segurança: '{s}' ({reason})")

        # 2. Verificação de integridade do snapshot de commit (se fornecido)
        if "snapshot" in entry:
            snap = str(entry["snapshot"]).strip()
            if not re.match(r"^[0-9a-fA-F]{64}$", snap):
                errors.append(f"Entrada #{i}: snapshot de commit/conteúdo inválido: '{snap}'. Esperado hash SHA-256 de 64 caracteres hex.")

        if "stdout_sha256" in entry:
            out_sha = str(entry["stdout_sha256"]).strip()
            if not re.match(r"^[0-9a-fA-F]{64}$", out_sha):
                errors.append(f"UNVERIFIED_EXECUTION_EVIDENCE: Entrada #{i} com stdout_sha256 inválido: '{out_sha}'.")

        # 3. Verificação de código de retorno
        exit_code = entry.get("exit", entry.get("exit_code"))
        if exit_code is None:
            errors.append(f"Entrada #{i} sem código de retorno ('exit') registrado.")
        elif isinstance(exit_code, bool) or not isinstance(exit_code, int):
            # LEDGER-EXIT-BOOL (banca v11 A6): false/0.0/"0" passavam na
            # comparação solta `!= 0` — LEDGER escrito à mão auditava verde.
            errors.append(f"Entrada #{i} com 'exit' não-inteiro: {exit_code!r} — veredito inválido.")
        elif exit_code != 0:
            errors.append(f"Entrada #{i} falhou com código de saída não-zero: {exit_code} (comando: '{full_cmd}').")

        # 4. Barramento de vitória por tautologia e verificação estrita de testes
        is_test_command = is_test_cmd(full_cmd)
        has_tests_decl = "testes_descobertos" in entry

        if has_tests_decl or is_test_command:
            tests_count = entry.get("testes_descobertos")
            if tests_count is None:
                errors.append(
                    f"Entrada #{i} ('{full_cmd}') é comando de teste mas não registrou contagem de 'testes_descobertos'."
                )
            elif isinstance(tests_count, bool) or not isinstance(tests_count, int):
                errors.append(f"Entrada #{i}: 'testes_descobertos' deve ser um inteiro válido, não booleano.")
            elif tests_count <= 0 and not allow_zero_tests:
                errors.append(
                    f"Entrada #{i} bloqueada: 0 testes descobertos no comando '{full_cmd}'. "
                    "Status 200/Exit 0 sem testes reais é vitória por tautologia migué."
                )
            else:
                total_tests_discovered += tests_count

        # 5. Validação de testes_passados contra testes_descobertos
        if "testes_passados" in entry:
            passed = entry["testes_passados"]
            if isinstance(passed, bool) or not isinstance(passed, int):
                errors.append(f"Entrada #{i}: 'testes_passados' deve ser um inteiro válido.")
            elif has_tests_decl and isinstance(entry.get("testes_descobertos"), int) and passed < entry["testes_descobertos"]:
                errors.append(f"Entrada #{i}: 'testes_passados' ({passed}) menor que 'testes_descobertos' ({entry['testes_descobertos']}).")

    if total_tests_discovered == 0 and not allow_zero_tests:
        errors.append(
            "Vitória por tautologia: 0 testes descobertos em todo o ledger "
            "(nenhuma evidência de execução real de testes)."
        )

    if require_exec_log or exec_log_path is not None:
        target_log = exec_log_path if exec_log_path is not None else Path(".henke/exec_log.jsonl")
        errors.extend(verify_exec_log(ledger_data, target_log))

    return len(errors) == 0, errors, total_tests_discovered


def _discover_package_json(ledger_path: Path) -> Path | None:
    candidates = [
        ledger_path.parent.parent / "package.json" if ledger_path.parent.name == ".henke" else ledger_path.parent / "package.json",
        ledger_path.parent / "package.json",
        Path.cwd() / "package.json",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=Path(".henke/LEDGER.json"), help="Caminho do LEDGER.json")
    parser.add_argument("--allow-zero-tests", action="store_true", help="Permite comandos de teste com 0 testes (não recomendado)")
    parser.add_argument("--package-json", type=Path, default=None,
                        help="package.json para resolver lifecycle npm (pretest/test/posttest) — anti-TOCTOU")
    parser.add_argument("--require-exec-log", action="store_true",
                        help="Exige verificação de integridade contra .henke/exec_log.jsonl")
    parser.add_argument("--exec-log", type=Path, default=None,
                        help="Caminho explícito do exec_log.jsonl (default: .henke/exec_log.jsonl)")
    args = parser.parse_args()

    ledger_path = args.ledger.resolve()
    if not ledger_path.is_file():
        # B10 (banca v13): malformado/uso sai exit 2, não 1.
        print(f"ERRO: Arquivo de ledger não encontrado em: '{ledger_path}'",
              file=sys.stderr)
        sys.exit(2)

    try:
        content = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"ERRO: Falha ao parsear JSON do ledger: {e}", file=sys.stderr)
        sys.exit(2)

    pkg_path = args.package_json or _discover_package_json(ledger_path)
    if args.package_json and not Path(args.package_json).is_file():
        # A12 (banca v14): --package-json explícito inexistente caía
        # silencioso (lifecycle invisível) — caminho explícito é fail-closed.
        print(f"ERRO: package.json não encontrado: '{args.package_json}'",
              file=sys.stderr)
        sys.exit(2)
    pkg_data = None
    if pkg_path and Path(pkg_path).is_file():
        try:
            pkg_data = json.loads(Path(pkg_path).read_text(encoding="utf-8"))
        except Exception as e:
            # M7 (banca v13): package.json descoberto e ilegível → lifecycle
            # invisível silencioso (pretest hostil não expandido). Fail closed.
            print(f"ERRO: Falha ao parsear package.json '{pkg_path}': {e}",
                  file=sys.stderr)
            sys.exit(2)

    has_sha_entries = isinstance(content, list) and any(
        isinstance(x, dict) and "stdout_sha256" in x for x in content
    )
    # A2 (banca v12): a custódia era opt-in chaveada por dado controlado pelo
    # atacante — ledger sem 'stdout_sha256' pulava verify_exec_log. Qualquer
    # entrada de teste/testes_descobertos dispara a verificação cruzada.
    has_test_entries = isinstance(content, list) and any(
        isinstance(x, dict) and (
            "testes_descobertos" in x or is_test_cmd(str(x.get("cmd", "")))
        ) for x in content
    )
    exec_log_p = None
    if args.exec_log:
        exec_log_p = args.exec_log.resolve()
    elif args.require_exec_log or has_sha_entries or has_test_entries:
        cand_log = ledger_path.parent / "exec_log.jsonl"
        exec_log_p = cand_log if cand_log.is_file() else Path(".henke/exec_log.jsonl").resolve()

    ok, errors, total_tests = audit_ledger_content(
        content,
        allow_zero_tests=args.allow_zero_tests,
        pkg=pkg_data,
        exec_log_path=exec_log_p,
        require_exec_log=args.require_exec_log,
    )
    if not ok:
        print(f"FALHA NA AUDITORIA DO LEDGER ({len(errors)} violações):", file=sys.stderr)
        for err in errors:
            print(f"  [LEDGER ERROR] {err}", file=sys.stderr)
        sys.exit(1)

    if total_tests == 0 and not args.allow_zero_tests:
        print("FALHA NA AUDITORIA DO LEDGER: zero testes descobertos em toda a suíte (vitória por tautologia).", file=sys.stderr)
        sys.exit(1)

    print(
        f"OK: Ledger verificado com sucesso. {len(content)} comandos auditados, "
        f"{total_tests} testes descobertos, zero violações de denylist."
    )


if __name__ == "__main__":
    # R2-PT-06: sitecustomize via PYTHONPATH roda antes de qualquer
    # mitigação em-processo e poderia substituir is_test_cmd/verificadores
    # no pai. Respawna isolado (-I: ignora PYTHONPATH e user site); o filho
    # sai com sys.flags.isolated — auto-limite, sem sentinel (R3-PT-06).
    if not sys.flags.isolated:
        import subprocess as _sp
        sys.exit(_sp.run(
            [sys.executable, "-I",
             str(Path(__file__).resolve()), *sys.argv[1:]]).returncode)
    main()
