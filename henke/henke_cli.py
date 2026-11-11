#!/usr/bin/env python3
"""
henke_cli.py — CLI Global Unificada da Suíte Henke (PROTOCOLO v4.0).

Resolve `SUITE_ROOT` automaticamente a partir da própria localização física do script,
eliminando a dependência de CWD dos 14 portões mecânicos e fornecendo:
  - `henke doctor`   : diagnóstico do ambiente, scripts e catálogo de skills
  - `henke init`     : inicialização de `.henke/`, `docs/designs/TEMPLATE.md` e baseline Git
  - `henke route`    : roteamento determinístico de prompt (`--compact` por padrão)
  - `henke exec`     : runner wrapper que executa testes reais e grava `.henke/exec_log.jsonl`
                       (hash-chain SHA-256) e `.henke/LEDGER.json` com `stdout_sha256`;
                       `--timeout N` > `.henke/PROJECT.json:timeout_sec` > fallback 180s
  - `henke gate`     : despacho direto para qualquer portão mecânico pelo nome curto
  - `henke check`    : bateria agregada de portões sem abortar no primeiro erro;
                       `--staged` restringe hazard_scan/assertless_scan aos arquivos de
                       `git diff --cached --name-only` (a11y_scan segue no repo inteiro)
  - `henke run`      : wrapper fino sobre `henke/run_state.py`
                       (init|record-gate|amend-scope|approve|verify) + `list` nativo
  - `henke report`   : relatório markdown rastreável de uma run (`henke/henke_report.py`)
  - `henke status`   : lista runs de `.henke/runs/` com outcome DERIVADO (não declarado)
  - `henke setup`    : detecção de projeto (`henke/henke_detect.py`) + stubs `.henke/`
                       criados sem sobrescrever os existentes
  - `henke auto`     : modo automático via `henke/henke_auto.py` (import lazy; exit 2
                       honesto quando o módulo não está instalado)
  - `henke goldfish` : com `--driver <cmd>` gera envelope de revisão isolada (spec+diff,
                       SEM transcript) em `.henke/runs/<id>.goldfish.json` e sai 0;
                       sem `--driver` grava `review_status=goldfish_unavailable` na run
                       (quando `--run` informado) e sai 2 — nunca despacha para `check`
  - `henke approve`  : aprovação humana auditada de uma run (`.henke/runs/<id>.json`)
  - `henke scope`    : emenda de escopo justificada em uma run ativa
  - `henke install-hook`: instala o pre-commit hook nativo de 1 comando no repositório Git

Códigos de saída:
  0  sucesso / todos os portões aprovados
  1  falha de portão ou comando executado
  2  erro de uso ou argumento inválido
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from henke.cli_kernel import (
    GATE_MAP,
    METRICAS_DIR,
    SCRIPTS_DIR,
    SUITE_ROOT,
    _atomic_write_json,
    _now_iso,
    _scrub_env,
    _suite_root,
)
from henke.cli_base import (
    _cmd_doctor,
    _cmd_init,
    _cmd_route,
)
from henke.cli_exec import (
    parse_test_runner_output,
    _audit_ledger_mod,
    is_test_command,
    _kill_process_tree,
    _run_capture_tree,
    _resolve_exec_timeout,
    _cmd_exec,
)
from henke.cli_check import (
    _cmd_gate,
    _format_manifest_output,
    _drop_repo_syspath,
    _cmd_check,
)
from henke.cli_runs import (
    _cmd_session,
    _iter_run_files,
    _derive_outcome_safe,
    _cmd_list_runs,
    _cmd_run,
    _resolve_run_ref,
    _git_diff_snapshot,
    _cmd_report,
)
from henke.cli_ops import (
    _cmd_install_hook,
    _cmd_report_html,
    _cmd_test_impact,
    _cmd_watch,
    _cmd_clean,
)
from henke.cli_pipeline import (
    _cmd_goldfish,
    _cmd_setup,
    _cmd_auto,
    _cmd_trigger,
)
import henke.cli_banca as _cli_banca


def _ensure_isolated_main() -> None:
    """Bootstrap de isolamento no nível de main() — ver henke/_isoboot.py.

    Carrega _isoboot por path explícito (meta_path finder plantado não
    consegue servir um falso) e respawna `python -Es <este arquivo> argv`
    quando o runtime não prova isolamento. No-op quando já isolado."""
    import importlib.util as _ilu
    _ib_p = Path(__file__).resolve().parent / "_isoboot.py"
    _ib_spec = _ilu.spec_from_file_location("henke_isoboot", str(_ib_p))
    _ib_mod = _ilu.module_from_spec(_ib_spec)
    _ib_spec.loader.exec_module(_ib_mod)
    _ib_mod.ensure_isolated(__file__, sys.argv[1:])


def main(argv: list[str] | None = None) -> int:
    # Console-script `henke` (pyproject): o shim gerado pelo pip importa
    # henke.henke_cli e chama main() DIRETO — o guarda do __main__ nunca
    # roda nesse caminho. Repete a verificação aqui (no-op se já isolado).
    # main(argv=[...]) programático em testes NÃO respawna (argv explícito =
    # uso em-processo, não entrypoint de processo).
    if argv is None:
        _ensure_isolated_main()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if not raw_argv:
        raw_argv = ["--help"]

    # Subcomandos que encaminham argumentos livres (exec, gate, run)
    head = raw_argv[0]
    if head == "gate":
        if len(raw_argv) < 2 or raw_argv[1] in ("-h", "--help"):
            print(f"Uso: henke gate <{'|'.join(sorted(GATE_MAP))}> [args...]")
            return 0 if len(raw_argv) >= 2 else 2
        return _cmd_gate(raw_argv[1], raw_argv[2:])

    if head == "exec":
        ap_exec = argparse.ArgumentParser(prog="henke exec", description="Executa comando registrando evidência no ledger.")
        ap_exec.add_argument("--repo", type=Path, default=Path("."), help="Diretório do repositório")
        ap_exec.add_argument("--ledger", type=Path, default=None, help="Caminho para LEDGER.json")
        ap_exec.add_argument("--exec-log", type=Path, default=None, help="Caminho para exec_log.jsonl")
        ap_exec.add_argument(
            "--timeout", type=float, default=None, metavar="N",
            help="Teto em segundos (default: timeout_sec de .henke/PROJECT.json, senão 180)",
        )
        ap_exec.add_argument("cmd", nargs=argparse.REMAINDER, help="Comando a executar (precedido por --)")
        try:
            ns = ap_exec.parse_args(raw_argv[1:])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        repo_root = ns.repo.resolve()
        ledger_p = ns.ledger.resolve() if ns.ledger else (repo_root / ".henke" / "LEDGER.json")
        exec_log_p = ns.exec_log.resolve() if ns.exec_log else (repo_root / ".henke" / "exec_log.jsonl")
        return _cmd_exec(repo_root, ledger_p, exec_log_p, ns.cmd, cli_timeout=ns.timeout)

    if head == "run":
        return _cmd_run(raw_argv[1:])

    parser = argparse.ArgumentParser(
        prog="henke",
        description="CLI Global Unificada da Suíte Henke (v4.0).",
        epilog="Também disponíveis: 'henke exec [--timeout N] -- <cmd>', 'henke gate <portão>', "
               "'henke run <init|record-gate|amend-scope|approve|verify|list>'.",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_doc = sub.add_parser("doctor", help="Verifica ambiente, scripts e skills instaladas")
    p_doc.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_doc.add_argument("--json", action="store_true", help="Saída JSON")

    p_init = sub.add_parser("init", help="Inicializa .henke/, TEMPLATE.md e baseline no repositório")
    p_init.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")

    p_route = sub.add_parser("route", help="Roteia prompt para modo, arquétipo, skills e portões")
    p_route.add_argument("prompt_pos", nargs="?", default=None, help="Prompt do usuário")
    p_route.add_argument("--prompt", dest="prompt_opt", default=None, help="Prompt do usuário (alternativo)")
    p_route.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_route.add_argument("--compact", action="store_true", default=False, help="Saída compacta em 1 linha")
    p_route.add_argument("--full", action="store_true", default=False, help="Saída JSON detalhada")
    p_route.add_argument("--json", action="store_true", default=False, help="Alias de --full (contrato consumido por 'henke auto')")

    p_check = sub.add_parser("check", help="Executa bateria unificada de portões sem abortar no primeiro erro")
    p_check.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_check.add_argument("--spec", default=None, help="Spec de referência para contract_closure")
    p_check.add_argument("--expected-hash", default=None, help="Hash SHA256 da spec aprovada (spec_drift; default: âncora da run que declarou a spec)")
    p_check.add_argument(
        "--staged", action="store_true",
        help="Avalia um snapshot do índice Git (conteúdo staged) " "em vez do worktree (a11y_scan segue no repo inteiro)",
    )
    p_check.add_argument("--json", action="store_true", help="Saída em JSON")
    p_check.add_argument("--no-cache", action="store_true", help="Ignora cache e força re-execução dos portões")
    p_check.add_argument("--clear-cache", action="store_true", help="Limpa o cache de portões antes de executar")
    p_check.add_argument("--full", action="store_true", help="Força scan integral (regrava manifesto após sucesso)")
    p_check.add_argument("--jobs", type=int, default=1, choices=(1, 2, 3, 4), help="Portões de processo em paralelo (1-4)")
    p_check.add_argument("--gate-timeout", type=float, default=120.0, help="Timeout por portão em segundos")
    p_check.add_argument("--state-dir", type=Path, default=None, help="Diretório de estado absoluto (equivale a HENKE_STATE_DIR)")

    p_app = sub.add_parser("approve", help="Aprova uma run pendente de autorização humana")
    p_app.add_argument("run_file", type=Path, help="Arquivo .henke/runs/<id>.json")
    p_app.add_argument("--approver", default="human", help="Nome do aprovador")
    p_app.add_argument("--yes", "-y", action="store_true",
                       help="Aprova sem confirmação interativa (obrigatório quando stdin não é TTY)")

    p_scope = sub.add_parser("scope", help="Gerencia emendas de escopo em uma run")
    scope_sub = p_scope.add_subparsers(dest="scope_cmd", required=True)
    p_amend = scope_sub.add_parser("amend", help="Adiciona arquivo ao escopo da run com justificativa")
    p_amend.add_argument("run_file", type=Path, help="Arquivo .henke/runs/<id>.json")
    p_amend.add_argument("file_rel", help="Caminho relativo do arquivo")
    p_amend.add_argument("--reason", required=True, help="Justificativa técnica (>= 5 chars)")

    p_gf = sub.add_parser("goldfish", help="Certificação Goldfish: envelope isolado (--driver) ou HARD STOP honesto")
    p_gf.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_gf.add_argument("--spec", default=None, help="Spec de referência embutida no envelope")
    p_gf.add_argument("--driver", default=None, help="Comando do revisor externo (sessão zerada)")
    p_gf.add_argument("--run", dest="run_ref", default=None, help="Run alvo (id ou caminho .henke/runs/<id>.json)")
    p_gf.add_argument("--json", action="store_true", help="Saída em JSON")

    _cli_banca.register(sub)

    p_install = sub.add_parser("install-hook", help="Instala o pre-commit hook nativo no repositório Git")
    p_install.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_install.add_argument("--force", action="store_true", help="Sobrescreve hook pré-existente sem criar backup .legacy")

    
    p_report_html = sub.add_parser("report-html", help="Gera o dashboard visual local em HTML autocontido")
    p_report_html.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_report_html.add_argument("--out", type=Path, default=None, help="Caminho do arquivo HTML de saída (padrão: .henke/report.html)")

    p_ti = sub.add_parser("test-impact", help="Seleciona arquivos de teste pelo diff (TIA conservativa)")
    p_ti.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_ti.add_argument("--base", required=True, help="Ref Git base para o diff")
    p_ti.add_argument("--full", action="store_true", help="Sempre suíte completa (CI/pré-merge)")
    p_ti.add_argument("--run", action="store_true", help="Executa o comando selecionado via supervisor")
    p_ti.add_argument("--json", action="store_true", help="Saída em JSON")

    p_watch = sub.add_parser("watch", help="Modo watch foreground interativo (exige TTY)")
    p_watch.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_watch.add_argument("--quiet", action="store_true", help="Suprime repetições verdes idênticas")
    p_watch.add_argument("--poll-interval", type=float, default=0.75, help="Intervalo de polling (s)")
    p_watch.add_argument("--debounce", type=float, default=0.30, help="Janela de debounce (s)")

    p_clean = sub.add_parser("clean", help="Faxina allowlist: dry-run padrão; --execute move para quarentena")
    p_clean.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_clean.add_argument("--execute", action="store_true", help="Executa a movimentação para quarentena")
    p_clean.add_argument("--restore", default=None, help="Batch-id para restaurar da quarentena")
    p_clean.add_argument("--purge-expired", action="store_true", help="Purgar batch expirado (exige --confirm-batch)")
    p_clean.add_argument("--confirm-batch", default=None, help="Confirmação humana do batch-id a purgar")
    p_clean.add_argument("--json", action="store_true", help="Saída em JSON")

    p_session = sub.add_parser("session", help="Sentinel de sessão com rastreamento de artefatos")
    session_sub = p_session.add_subparsers(dest="session_cmd", required=True)
    p_sstart = session_sub.add_parser("start", help="Inicia sessão e imprime session_id")
    p_sstart.add_argument("--agent", required=True, help="Nome do agente")
    p_sstart.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")
    p_sstart.add_argument("--run-id", required=True, help="ID da run associada")
    p_shb = session_sub.add_parser("heartbeat", help="Renova janela de heartbeat (+1h)")
    p_shb.add_argument("session_id", help="ID da sessão")
    p_shb.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")
    p_send = session_sub.add_parser("end", help="Encerra a sessão (state=closed)")
    p_send.add_argument("session_id", help="ID da sessão")
    p_send.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")
    p_sgc = session_sub.add_parser("gc", help="Reapa sentinels expirados e roda ephemeral_gc")
    p_sgc.add_argument("--workspace", type=Path, default=Path("."), help="Worktree alvo")

    p_report = sub.add_parser("report", help="Relatório markdown rastreável de uma run (5 seções fixas)")
    p_report.add_argument("run_id", help="ID da run ou caminho do arquivo .henke/runs/<id>.json")
    p_report.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")

    p_status = sub.add_parser("status", help="Lista runs de .henke/runs/ com outcome DERIVADO")
    p_status.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_status.add_argument("--json", action="store_true", help="Saída em JSON")

    p_setup = sub.add_parser("setup", help="Detecta o projeto e cria stubs .henke/ sem sobrescrever existentes")
    p_setup.add_argument("dir", nargs="?", type=Path, default=Path("."), help="Diretório do projeto (default: cwd)")

    p_auto = sub.add_parser("auto", help="Modo automático (henke/henke_auto.py) com política de aprovação")
    p_auto.add_argument("prompt", nargs="+", help="Prompt da tarefa a executar")
    p_auto.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_auto.add_argument("--driver", default=None, help="Driver/executor externo")
    p_auto.add_argument("--dry-run", action="store_true", help="Simula sem escrever nada")
    p_auto.add_argument("--yes", action="store_true", help="Aprova automaticamente o permitido pela policy")
    p_auto.add_argument("--policy", choices=("strict", "balanced", "yolo"), default=None, help="Política de aprovação (default: a da POLICY.json, ou balanced)")
    p_auto.add_argument("--timeout", type=float, default=None, metavar="N", help="Teto em segundos")
    p_auto.add_argument("--resume", default=None, metavar="RUN", help="Retoma run bloqueada após `henke approve`")

    p_trig = sub.add_parser("trigger", help="Dispara `auto` a partir de um evento (commit, falha de CI, arquivo)")
    p_trig.add_argument("kind", choices=("commit", "ci", "file"), help="Tipo de evento")
    p_trig.add_argument("target", nargs="?", default=None, help="Alvo (ex.: path para kind=file)")
    p_trig.add_argument("--repo", type=Path, default=Path("."), help="Repositório alvo")
    p_trig.add_argument("--driver", default=None, help="Driver de agente")
    p_trig.add_argument("--yes", action="store_true", help="Confirma escopos comuns")
    p_trig.add_argument("--policy", choices=("strict", "balanced", "yolo"), default=None)
    p_trig.add_argument("--timeout", type=float, default=None, metavar="N")

    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.subcmd == "doctor":
        return _cmd_doctor(args.repo.resolve(), as_json=args.json)

    if args.subcmd == "init":
        return _cmd_init(args.repo.resolve())

    if args.subcmd == "route":
        prompt_val = args.prompt_opt if args.prompt_opt is not None else args.prompt_pos
        if not prompt_val:
            print("ERRO: informe o prompt em 'henke route \"<prompt>\"'.", file=sys.stderr)
            return 2
        is_compact = args.compact or not (args.full or args.json)
        return _cmd_route(prompt_val, args.repo.resolve(), compact=is_compact)

    if args.subcmd == "check":
        state_dir_arg = getattr(args, "state_dir", None)
        prev_state_dir = os.environ.get("HENKE_STATE_DIR")
        if state_dir_arg is not None:
            if not Path(state_dir_arg).is_absolute():
                print("ERRO: --state-dir deve ser caminho absoluto.", file=sys.stderr)
                return 2
            os.environ["HENKE_STATE_DIR"] = str(Path(state_dir_arg).resolve())
        try:
            return _cmd_check(
                args.repo.resolve(),
                spec_path=args.spec,
                as_json=args.json,
                no_cache=getattr(args, "no_cache", False),
                clear_cache_flag=getattr(args, "clear_cache", False),
                staged=getattr(args, "staged", False),
                full=getattr(args, "full", False),
                jobs=getattr(args, "jobs", 1),
                gate_timeout=getattr(args, "gate_timeout", 120.0),
                expected_hash=getattr(args, "expected_hash", None),
            )
        finally:
            if state_dir_arg is not None:
                if prev_state_dir is None:
                    os.environ.pop("HENKE_STATE_DIR", None)
                else:
                    os.environ["HENKE_STATE_DIR"] = prev_state_dir

    if args.subcmd == "goldfish":
        return _cmd_goldfish(
            args.repo.resolve(),
            spec_path=args.spec,
            driver=args.driver,
            run_ref=args.run_ref,
            as_json=args.json,
        )

    if args.subcmd == "banca":
        return _cli_banca.cmd_banca(args)

    if args.subcmd == "report":
        return _cmd_report(args.repo.resolve(), args.run_id)

    if args.subcmd == "report-html":
        return _cmd_report_html(args.repo.resolve(), out_path=args.out)

    if args.subcmd == "status":
        return _cmd_list_runs(args.repo.resolve(), as_json=args.json)

    if args.subcmd == "setup":
        return _cmd_setup(args.dir)

    if args.subcmd == "test-impact":
        return _cmd_test_impact(args.repo.resolve(), args.base,
                                full=args.full, run=args.run, as_json=args.json)

    if args.subcmd == "watch":
        return _cmd_watch(args.repo.resolve(), quiet=args.quiet,
                          poll=args.poll_interval, debounce=args.debounce)

    if args.subcmd == "clean":
        return _cmd_clean(args.repo.resolve(), execute=args.execute,
                          restore=args.restore, purge_expired=args.purge_expired,
                          confirm_batch=args.confirm_batch, as_json=args.json)

    if args.subcmd == "session":
        return _cmd_session(args)

    if args.subcmd == "auto":
        return _cmd_auto(
            " ".join(args.prompt),
            args.repo,
            driver=args.driver,
            dry_run=args.dry_run,
            yes=args.yes,
            policy=args.policy,
            timeout=args.timeout,
            resume=getattr(args, "resume", None),
        )

    if args.subcmd == "trigger":
        return _cmd_trigger(
            args.kind, getattr(args, "target", None), args.repo,
            driver=args.driver, yes=args.yes, policy=args.policy, timeout=args.timeout,
        )

    if args.subcmd == "approve":
        from henke.run_state import main as run_state_main
        argv = ["approve", str(args.run_file), "--approver", args.approver]
        if args.yes:
            argv.append("--yes")
        return run_state_main(argv)

    if args.subcmd == "scope" and args.scope_cmd == "amend":
        from henke.run_state import main as run_state_main
        return run_state_main([
            "amend-scope",
            str(args.run_file),
            "--file",
            args.file_rel,
            "--reason",
            args.reason,
        ])

    if args.subcmd == "install-hook":
        return _cmd_install_hook(args.repo.resolve(), force=args.force)

    
    return 2


if __name__ == "__main__":
    # ISO-BOOT canônico (henke/_isoboot.py) — sitecustomize/usercustomize/
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
