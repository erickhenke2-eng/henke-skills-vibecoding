"""cli_exec.py — `henke exec`: runner com custódia (hash-chain exec_log.jsonl, LEDGER.json e âncora de ponta)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

from henke.cli_exec_proc import (  # noqa: F401
    _audit_ledger_mod,
    _kill_process_tree,
    _resolve_exec_timeout,
    _run_capture_tree,
    is_test_command,
    parse_test_runner_output,
)
from henke.cli_kernel import (
    _atomic_write_json,
    _now_iso,
)
from henke.state_paths import sanitized_child_env


def _cmd_exec(
    repo_root: Path,
    ledger_path: Path,
    exec_log_path: Path,
    cmd_tokens: list[str],
    cli_timeout: float | None = None,
) -> int:
    if cmd_tokens and cmd_tokens[0] == "--":
        cmd_tokens = cmd_tokens[1:]
    if not cmd_tokens:
        print("ERRO: informe o comando a executar após 'henke exec -- <comando>'.", file=sys.stderr)
        return 2
    if cli_timeout is not None and cli_timeout <= 0:
        print("ERRO: --timeout deve ser um número positivo de segundos.", file=sys.stderr)
        return 2

    cmd_str = " ".join(cmd_tokens).strip()
    exec_tokens = list(cmd_tokens)
    # M5/H3 (banca v13) + Delta-F5: fonte única — sanitized_child_env cobre
    # GIT_*/loader-hooks de todos os toolchains (PYTHON*/NODE_*/JAVA_*/
    # DOTNET_*/GOFLAGS/RUST*/CARGO_/GEM_HOME/PERL5/PHP/BUNDLE_/PSModulePath/
    # ComSpec/PATHEXT/CDPATH...) e o PATH sem entradas do repo. A lista local
    # divergia do helper canônico (AUD-02: PSModulePath/ComSpec/CARGO_ etc.
    # escapavam por aqui).
    child_env = sanitized_child_env(repo_root)
    # Extras do exec — o que o helper genérico ainda mantém: PYTEST_*/
    # COVERAGE_* mintam evidência de runner; PWD/OLDPWD enganam tools que
    # leem cwd por env; RUBYLIB/CLASSPATH são loader-hooks de runtime.
    child_env = {
        k: v for k, v in child_env.items()
        if not (k.upper().startswith(("PYTEST_", "COVERAGE_"))
                or k.upper() in ("PWD", "OLDPWD", "RUBYLIB", "CLASSPATH"))
    }
    _rr: Path | None = None
    try:
        _rr = repo_root.resolve()
    except (OSError, ValueError):
        _rr = None
    if exec_tokens:
        # A2 (banca v14): recusa repo-resident era Windows-only — execvp no
        # POSIX resolvia o plantado por PATH. `which` roda em toda plataforma.
        resolved = shutil.which(exec_tokens[0], path=child_env.get("PATH"))
        if resolved:
            # H3 (banca v13): o resolvido dentro do repo é o impostor
            # plantado — recusar, nunca executar.
            try:
                _res = Path(resolved).resolve()
                if _rr is not None and (_res == _rr or _rr in _res.parents):
                    print(
                        f"ERRO: '{exec_tokens[0]}' resolve para binário dentro "
                        f"do repo ({_res}) — recusado (path poisoning).",
                        file=sys.stderr)
                    return 2
            except OSError:
                pass
            exec_tokens[0] = resolved
        elif (os.name == "nt" and not resolved
              and exec_tokens[0].lower() in ("echo", "dir", "type", "cls")):
            # A6 (banca v14): `cmd /c` re-parseia o rabo — `echo ok & calc`
            # executava o segundo comando mesmo com builtin "inocente".
            if any(re.search(r"[&|<>^%\"]", str(t)) for t in exec_tokens):
                print("ERRO: metacaractere de shell (&|<>) em comando builtin "
                      "— recusado (cmd /c re-parseia o rabo).", file=sys.stderr)
                return 2
            exec_tokens = ["cmd.exe", "/c"] + exec_tokens
        elif os.name == "nt" and not resolved:
            # A6 (banca v15): CreateProcess busca pelo PATH do PAI, não pelo
            # env sanitizado — which-miss executava o plantado via PATH pai.
            print(f"ERRO: '{exec_tokens[0]}' não resolve no PATH sanitizado "
                  "— recusado (binário poderia ser plantado no repo).",
                  file=sys.stderr)
            return 2
        elif not resolved:
            # BANCA-C1: which-miss no POSIX caía direto no execvpe — `./x.sh`
            # resolvia contra cwd=repo_root e executava o binário plantado,
            # ainda logado como comando auditado. Caminho explícito (ou
            # absoluto) para dentro do repo é recusado em toda plataforma.
            # +revisão cega: bare-name (`pytest` ausente) não é caminho —
            # só checa tokens com separador/ponto (fail-open do diagnóstico).
            _tok_s = exec_tokens[0]
            if (os.sep in _tok_s or "/" in _tok_s
                    or _tok_s.startswith(".") or Path(_tok_s).is_absolute()):
                _tok = Path(_tok_s)
                _cand = _tok if _tok.is_absolute() else (
                    (_rr / _tok) if _rr is not None else _tok)
                try:
                    _cr = _cand.resolve()
                    if (_cr.exists() and _rr is not None
                            and (_cr == _rr or _rr in _cr.parents)):
                        print(
                            f"ERRO: '{exec_tokens[0]}' resolve para caminho "
                            f"dentro do repo ({_cr}) — recusado "
                            "(repo-resident exec).",
                            file=sys.stderr)
                        return 2
                except OSError:
                    pass
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    # F-07 (banca v8): CWD poisoning — binário plantado no worktree não pode
    # ser resolvido como o comando pedido (`henke exec -- npm` num repo com
    # npm.cmd forjado executaria o plantado).
    child_env["NoDefaultCurrentDirectoryInExePath"] = "1"
    timeout_sec = _resolve_exec_timeout(repo_root, cli_timeout)

    try:
        # DS-12: subprocess.run(timeout) abandona netos — Popen + nova sessão
        # + kill de árvore; o timeout é registrado no ledger abaixo (exit 124),
        # não some num early-return sem rastro.
        rc, raw_out, raw_err, timed_out = _run_capture_tree(
            exec_tokens, cwd=repo_root, env=child_env, timeout_sec=timeout_sec)
    except OSError as exc:
        print(f"ERRO ao executar '{cmd_str}': {exc}", file=sys.stderr)
        return 2

    # B9 (banca v13): sha cobria texto com errors="replace" — byte-streams
    # distintos colidiam em U+FFFD. Hash sobre os BYTES capturados.
    out_text = raw_out.decode("utf-8", errors="replace")
    err_text = raw_err.decode("utf-8", errors="replace")
    if out_text:
        sys.stdout.write(out_text)
    if err_text:
        sys.stderr.write(err_text)

    stdout_sha256 = hashlib.sha256(raw_out + raw_err).hexdigest()
    # DS-1 + P0-02: contrato TIPADO de evidência (henke/exec_contract.py) —
    # `henke exec -- echo "10 passed"` nunca minta (is_test=False → zeros);
    # rc≠0 com placar perfeito zera (w7); contagens só existem p/ runner
    # reconhecido. `reason` fica no ledger p/ auditoria do PORQUÊ.
    try:
        from henke.exec_contract import evaluate_execution as _eval_exec
    except ImportError:
        try:
            from exec_contract import evaluate_execution as _eval_exec  # type: ignore[no-redef]
        except ImportError:
            _eval_exec = None  # type: ignore[assignment]
    if _eval_exec is None:
        # Contrato indisponível = nenhuma evidência mintada (fail-closed).
        print("ERRO: exec_contract indisponível — execução não registrada.",
              file=sys.stderr)
        return 2
    _verdict = _eval_exec(cmd_str, rc, out_text, err_text)
    discovered, passed = _verdict.discovered, _verdict.passed

    # Lê prev_hash + append da entrada + LEDGER.json sob o MESMO lock
    # (read-modify-write atômico — concorrência não pode ver estado intermediário).
    from henke import run_state as _rs
    # B9 (banca v9): a âncora local da ponta é chaveada por REPO — um
    # --exec-log fora de .henke/exec_log.jsonl não pode ancorar o tip do repo
    # (ancoraria a ponta de outro arquivo, corrompendo a custódia canônica).
    _canonical_log = (repo_root / ".henke" / "exec_log.jsonl").resolve()
    _is_canonical = exec_log_path.resolve() == _canonical_log
    try:
        with _rs._file_lock(exec_log_path):
            prev_hash = "0" * 64
            _last = None
            # A3 (banca v16): log DELETADO regenerava gênese e re-ancorava a
            # ponta — história inteira apagada sem vestígio. Se a âncora
            # diz que já existiu execução, gênese nova é forja.
            if _is_canonical and not (
                    exec_log_path.is_file() and exec_log_path.stat().st_size):
                _tip = _rs.load_exec_tip(repo_root)
                if _tip is not None:
                    print("ERRO: exec_log ausente/vazio mas âncora de ponta "
                          "existe — log foi apagado/truncado. Recusado; "
                          "restaure o log ou reset explícito.", file=sys.stderr)
                    return 2
            if exec_log_path.is_file():
                # A1 (banca v9): ANTES de encadear, a cadeia existente tem que
                # verificar — append sobre um log plantado lavaria a âncora da
                # ponta com uma execução legítima e tornaria a história
                # fabricada "ancorada" para sempre. Log fora do caminho
                # canônico não tem âncora: verifica-se só a cadeia interna.
                # M4 (banca v15): fallback não-qualificado resolvia
                # `audit_ledger.py` PLANTADO no sys.path → verify fake.
                # Carrega por path explícito (mesma regra do _rs_tip).
                try:
                    import importlib.util as _ilu
                    _al_p = Path(__file__).resolve().with_name("audit_ledger.py")
                    _spec = _ilu.spec_from_file_location(
                        "henke_audit_ledger", str(_al_p))
                    _al_mod = _ilu.module_from_spec(_spec) if _spec else None
                    if _al_mod is not None:
                        _spec.loader.exec_module(_al_mod)  # type: ignore[union-attr]
                    _vlc = getattr(_al_mod, "verify_log_chain", None)
                except Exception:
                    _vlc = None  # type: ignore[assignment]
                # A4 (banca v10): import falho no caminho CANÔNICO não pode
                # pular a checagem em silêncio — fail closed.
                if _is_canonical and _vlc is None:
                    print("ERRO: verify_log_chain indisponível — append no "
                          "log canônico recusado.", file=sys.stderr)
                    return 2
                if _vlc is not None and _is_canonical:
                    _chain_errors, _last = _vlc(exec_log_path)
                    if _chain_errors:
                        for _e in _chain_errors[:5]:
                            print(f"ERRO: {_e}", file=sys.stderr)
                        print(
                            "ERRO: exec_log existente falhou a verificação de "
                            "integridade — append recusado (remova o log "
                            "corrompido explicitamente se for recomeçar).",
                            file=sys.stderr,
                        )
                        return 2
                try:
                    lines = [
                        ln.strip()
                        for ln in exec_log_path.read_text(encoding="utf-8").splitlines()
                        if ln.strip()
                    ]
                    if lines:
                        last_obj = json.loads(lines[-1])
                        prev_hash = str(last_obj.get("entry_hash", prev_hash))
                    # A4 (banca v12): TOCTOU verify→append — driver truncava o
                    # log para prefixo válido entre _vlc() e a releitura, e o
                    # append 'lavava' entradas reais do rabo. A ponta relida
                    # DEVE ser a mesma que o verificador aprovou.
                    if (_is_canonical and _vlc is not None
                            and _last is not None and prev_hash != _last):
                        print("ERRO: exec_log mudou entre a verificação da "
                              "cadeia e o append (ponta divergente) — "
                              "append recusado.", file=sys.stderr)
                        return 2
                except Exception as exc:
                    print(f"ERRO: Log de execução corrompido em '{exec_log_path}': {exc}", file=sys.stderr)
                    return 2

            # BANCA-B1: hash e persistência devem cobrir o MESMO valor de exit —
            # o verificador recompõe a cadeia do campo persistido ('exit': 124
            # em timeout). Hashear o rc real enquanto grava 124 envenenava a
            # cadeia: todo exec seguinte era recusado (wedge permanente).
            _persisted_exit = rc if not timed_out else 124
            chain_payload = f"{prev_hash}|{cmd_str}|{_persisted_exit}|{discovered}|{stdout_sha256}"
            entry_hash = hashlib.sha256(chain_payload.encode("utf-8")).hexdigest()

            log_entry = {
                "timestamp": _now_iso(),
                "cmd": cmd_str,
                "exit": rc if not timed_out else 124,
                "timed_out": timed_out,
                "testes_descobertos": discovered,
                "testes_passados": passed,
                # P0-02: por que a evidência vale/não vale — auditável.
                "evidence_reason": _verdict.reason,
                "stdout_sha256": stdout_sha256,
                "prev_hash": prev_hash,
                "entry_hash": entry_hash,
            }
            # A1 (banca v11): valida/carrega o ledger ANTES do append —
            # `return 2` aqui não precisa de rollback porque a linha nova
            # ainda não existe. Antes, ledger corrompido wedgiava o exec_log
            # para sempre (linha extra sem tip-update).
            _ledger_pre: list[dict] | None = None
            if _verdict.is_test:
                _ledger_pre = []
                if ledger_path.is_file():
                    try:
                        loaded = json.loads(ledger_path.read_text(encoding="utf-8"))
                        if not isinstance(loaded, list):
                            print(f"ERRO: Formato inválido em '{ledger_path}' (esperava lista JSON).", file=sys.stderr)
                            return 2
                        _ledger_pre = loaded
                    except Exception as exc:
                        print(f"ERRO: Ledger corrompido em '{ledger_path}': {exc}", file=sys.stderr)
                        return 2

            try:
                exec_log_path.parent.mkdir(parents=True, exist_ok=True)
                _pre_size = exec_log_path.stat().st_size if exec_log_path.is_file() else 0
                with open(exec_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
                    # A10 (banca v14): append sem fsync — crash entre a linha e
                    # o tip wedgiava a cadeia pra sempre.
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                print(f"ERRO: falha ao escrever exec_log: {exc}", file=sys.stderr)
                return 2
            try:
                # Atualiza .henke/LEDGER.json apenas quando o COMANDO é de testes —
                # qualquer saída contendo "N tests" (ex.: um README impresso por cat)
                # não pode entrar no ledger sem ser um runner real (ALTO-6 banca).
                # M6 (banca v15): entrada {tests:0} era gravada ANTES do check
                # `discovered<=0` — run 0-testes envenenava o ledger pra sempre.
                if _ledger_pre is not None and discovered > 0:
                    ledger_item = {
                        "cmd": cmd_str,
                        "exit": rc,
                        "testes_descobertos": discovered,
                        "testes_passados": passed,
                        "stdout_sha256": stdout_sha256,
                        "entry_hash": entry_hash,
                    }
                    _atomic_write_json(ledger_path, _ledger_pre + [ledger_item])

                # Âncora LOCAL da ponta da cadeia (F-13): a cadeia prova só
                # consistência interna — truncar/reescrever o rabinho do jsonl
                # produzia cadeia "válida". Com a ponta fora do repo, qualquer
                # reescrita diverge do tip gravado nesta máquina. Só o caminho
                # canônico ancora (B9).
                if _is_canonical:
                    _rs.record_exec_tip(repo_root, entry_hash)
                # P1-03: espelha no event store v2 unificado (best-effort —
                # a custódia primária é o exec_log; falha de espelho avisa,
                # não reprova — verify() pega divergência depois).
                try:
                    from henke.event_store import append_event as _ev_app
                    _ev_app(repo_root, "exec", {"origin": "exec_log",
                                                "entry": log_entry})
                except Exception as _exc_ev:
                    print(f"AVISO: event_store mirror falhou: {_exc_ev}",
                          file=sys.stderr)
            except (OSError, ValueError) as exc:
                # A5 (banca v10): falha pós-append deixava rabo não-ancorado e
                # wedgiava o log para sempre — rollback trunca a linha nova
                # e restaura o ledger (senão a entrada fica órfã).
                try:
                    with open(exec_log_path, "r+b") as f:
                        f.truncate(_pre_size)
                except OSError:
                    pass
                if _ledger_pre is not None:
                    try:
                        _atomic_write_json(ledger_path, _ledger_pre)
                    except OSError:
                        pass
                print(f"ERRO: exec_log revertido após falha de custódia: {exc}",
                      file=sys.stderr)
                return 2
    except TimeoutError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    # Zero testes descobertos NUNCA pode ser tratado como verde — "Ran 0 tests" é
    # tão perigoso quanto suíte ausente (falso-verde do audit do Kimi).
    if is_test_command(cmd_tokens, cmd_str) and discovered <= 0:
        print(
            "ERRO: comando de teste executado mas 0 testes descobertos — "
            "a run não pode fechar sem teste real.",
            file=sys.stderr,
        )
        return 2

    if timed_out:
        # timeout registrado no exec_log (exit 124 + timed_out) acima —
        # infra: sai 2, não o rc do filho (que pode nem ter terminado).
        print(f"ERRO: Comando expirou após {timeout_sec}s: '{cmd_str}'",
              file=sys.stderr)
        return 2
    return rc
