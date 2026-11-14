#!/usr/bin/env python3
"""
run_state.py — Máquina de Estados determinística de execução (.henke/runs/<run_id>.json).

Implementa o contrato mecânico do PROTOCOLO v5.0 (§2):
- `outcome` ('verified' | 'failed' | 'blocked') é SEMPRE derivado dos portões executados,
  das emendas de escopo justificadas e do status de aprovação humana.
- Rejeita `outcome: "verified"` declarado manualmente quando a derivação mecânica
  diverge (`STATE-DERIVED-CONFLICT`).
- Todas as mutações de estado usam escrita atômica (`.tmp` + `os.replace`).

Desmembrado por responsabilidade em:
  - `rs_io.py`        : escrita atômica, lock interprocessos e higiene de paths
  - `rs_custody.py`   : custódia local fora do repo (tokens, tips, manifestos, sidecars)
  - `rs_git.py`       : snapshots git higienizados (HEAD, ignored, untracked)
  - `rs_verify.py`    : derivação mecânica de outcome e verificação retroativa
  - `rs_mutations.py` : init_run, record_gate, amend_scope, approve_run

Códigos de saída:
  0  operação concluída / run verificada com sucesso
  1  conflito de estado derivado, portão reprovado ou emenda sem justificativa
  2  erro de uso, arquivo inexistente ou JSON malformado
"""
from __future__ import annotations

import argparse
import importlib.util as _ilu_rs
import json
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")
from pathlib import Path
import sys

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

# A6 (banca v12): audit_ledger carrega run_state por path explícito para barrar
# PYTHONPATH hostil; ancora também cada submódulo rs_* ao diretório físico da suíte.
for _sib in ("rs_io", "rs_custody", "rs_git", "rs_verify", "rs_mutations"):
    _full = f"henke.{_sib}"
    _target = _HERE / f"{_sib}.py"
    _cur = sys.modules.get(_full)
    if _cur is None or Path(getattr(_cur, "__file__", "")).resolve() != _target:
        _sp = _ilu_rs.spec_from_file_location(_full, str(_target))
        if _sp is not None and _sp.loader is not None:
            _mod = _ilu_rs.module_from_spec(_sp)
            sys.modules[_full] = _mod
            _sp.loader.exec_module(_mod)

from henke.rs_custody import (  # noqa: E402
    _ANCHORED_FIELDS,
    _EVIDENCE_MAX_BYTES,
    _anchor_filename,
    _append_evidence_manifest,
    _binding_token,
    _evidence_manifest_path,
    _exec_tip_path,
    _load_evidence_manifest,
    amend_anchor_fields,
    human_approval_of,
    load_anchor_sidecar,
    load_exec_tip,
    record_exec_tip,
    verify_approval_token,
    write_anchor_sidecar,
    write_approval_token,
)
from henke.rs_git import (  # noqa: E402
    _GIT_ENV,
    _git_env_for,
    _git_head,
    _git_ignored,
    _git_ignored_hashes,
    _git_untracked,
)
from henke.rs_io import (  # noqa: E402
    _atomic_write_json,
    _config_dir,
    _file_lock,
    _now_iso,
    _pid_alive,
    _repo_root_for_run,
    _safe_run_id,
)
from henke.rs_mutations import (  # noqa: E402
    _load_run,
    amend_scope,
    approve_run,
    init_run,
    record_gate,
)
from henke.rs_verify import (  # noqa: E402
    derive_run_outcome,
    verify_run_file,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gerenciador de estado determinístico de runs (.henke/runs/).")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_init = sub.add_parser("init", help="Inicializa um novo arquivo de run")
    p_init.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório")
    p_init.add_argument("--run-id", required=True, help="Identificador único da run")
    p_init.add_argument("--mode", default="EXECUTAR", choices=("ANALISAR", "PLANEJAR", "EXECUTAR"))
    p_init.add_argument("--archetype", default="CARPINTEIRO", choices=("SHERLOCK", "ESTADISTA", "CARPINTEIRO"))
    p_init.add_argument("--spec", default=None, help="Caminho opcional da spec")
    p_init.add_argument("--requires-human-auth", action="store_true")
    p_init.add_argument("--mandatory-gates", nargs="*", default=None, help="Portões obrigatórios declarados")

    p_gate = sub.add_parser("record-gate", help="Registra execução de um portão na run")
    p_gate.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_gate.add_argument("--gate", required=True, help="Nome do portão")
    p_gate.add_argument("--exit-code", type=int, required=True, help="Código de saída do portão")
    p_gate.add_argument("--summary", default="", help="Resumo curto da saída")
    p_gate.add_argument("--command", default=None,
                        help="Comando real executado (evidência física do veredito)")
    p_gate.add_argument("--evidence-file", dest="evidence_file", type=Path, default=None,
                        help="Arquivo de saída do comando; sha256 é registrado como evidência")

    p_amend = sub.add_parser("amend-scope", help="Registra emenda de escopo justificada")
    p_amend.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_amend.add_argument("--file", required=True, help="Arquivo relativo adicionado ao escopo")
    p_amend.add_argument("--reason", required=True, help="Justificativa técnica (>= 5 caracteres)")

    p_app = sub.add_parser("approve", help="Aprova manualmente uma run pendente de autorização humana")
    p_app.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_app.add_argument("--approver", default="human", help="Identificador do aprovador humano")
    p_app.add_argument("--yes", "-y", action="store_true",
                       help="Aprova sem confirmação interativa (obrigatório quando stdin não é TTY)")

    p_ver = sub.add_parser("verify", help="Verifica integridade e outcome derivado de uma run")
    p_ver.add_argument("run_file", nargs="?", type=Path, default=None, help="Caminho para .henke/runs/<id>.json")
    p_ver.add_argument("--run", dest="run_opt", type=Path, default=None, help="Caminho para .henke/runs/<id>.json (alternativo)")
    p_ver.add_argument("--json", action="store_true", help="Saída em JSON")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        if args.subcmd == "init":
            out_path = init_run(
                args.repo,
                args.run_id,
                mode=args.mode,
                archetype=args.archetype,
                spec_path=args.spec,
                requires_human_auth=args.requires_human_auth,
                mandatory_gates=args.mandatory_gates,
            )
            print(f"OK: Run inicializada em {out_path}")
            return 0

        if args.subcmd == "record-gate":
            import re as _re
            if _re.match(r"^(?:stage_.+|tests|dispatch)$", args.gate or ""):
                print(
                    f"ERRO: nome de portão reservado à pipeline interna: '{args.gate}' "
                    "(stage_*/tests/dispatch só a própria pipeline grava; um CLI externo "
                    "forjando esses nomes fabricaria a narrativa da run).",
                    file=sys.stderr,
                )
                return 2
            if args.command or args.evidence_file is not None:
                # F-01+M2: a superfície CLI NUNCA carrega tríade — atestação
                # manual com evidence_* envenenava a run permanentemente
                # (EVIDENCE-UNMANIFESTED sem caminho de reparo). Evidência
                # física é domínio exclusivo da pipeline (record_gate via API).
                print(
                    "ERRO: --command/--evidence-file não são aceitos no CLI — "
                    "atestação manual não carrega tríade; evidência física só "
                    "a pipeline emite (record_gate via API).",
                    file=sys.stderr,
                )
                return 2
            data = record_gate(
                args.run_file, args.gate, args.exit_code, args.summary,
                manual=True,
            )
            print(f"OK: Gate '{args.gate}' (exit {args.exit_code}) registrado "
                  f"(atestação manual — não conta como portão físico) -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "amend-scope":
            data = amend_scope(args.run_file, args.file, args.reason)
            if data["outcome"] == "failed" and any("UNJUSTIFIED-SCOPE-AMENDMENT" in r for r in data.get("reasons", [])):
                for r in data["reasons"]:
                    print(f"  [RUN-ERROR] {r}", file=sys.stderr)
                return 1
            print(f"OK: Escopo emendado com '{args.file}' -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "approve":
            # Aprovação nunca é cega (F-05): mostra exatamente o que está
            # sendo autorizado — um repo plantado com run "pendente" +
            # aprovação social transformaria `cmd:evil` em RCE via --resume.
            _prev = _load_run(args.run_file)
            _binding = _prev.get("approval_binding") if isinstance(_prev.get("approval_binding"), dict) else {}
            print("=== RESUMO DA APROVAÇÃO ===")
            print(f"  run_id          : {_prev.get('run_id')}")
            print(f"  mode            : {_prev.get('mode')}")
            print(f"  driver          : {_prev.get('driver') or _binding.get('driver') or '(não registrado)'}")
            print(f"  prompt          : {str(_prev.get('prompt') or '(não registrado)')[:200]!r}")
            print(f"  scope_paths     : {_prev.get('scope_paths')}")
            print(f"  mandatory_gates : {_prev.get('mandatory_gates')}")
            print(f"  requires_human  : {_prev.get('requires_human_auth')}")
            # A2 (banca v9): âncoras do diff também entram no resumo — o
            # humano precisa ver contra qual HEAD/spec a aprovação se vincula.
            print(f"  head_start      : {_prev.get('head_start')}")
            print(f"  spec_sha256     : {str(_prev.get('spec_sha256'))[:16]}…")
            print(f"  spec_path       : {_prev.get('spec_path')}")
            # A7 (banca v14): test_cmd/policy efetivos alimentam o
            # scope_sha256 e executam sob o selo da aprovação — o resumo
            # precisa mostrar O QUE será executado, não só o escopo.
            print(f"  test_cmd        : {_prev.get('test_cmd') or '(nenhum)'}")
            _pol = _prev.get("policy_efetiva") or _prev.get("policy") or {}
            if isinstance(_pol, dict) and _pol:
                _pl = {k: _pol.get(k) for k in
                       ("policy", "allowed_cmds", "source") if k in _pol}
                print(f"  policy_efetiva  : {_pl}")
            # A3 (banca v12): o HEAD que será vinculado também entra no
            # resumo — approve_run ancora ao HEAD VIVO, que o driver pode
            # ter acabado de commitar sem o humano ver.
            try:
                _rr = _repo_root_for_run(args.run_file)
                _live_head = _git_head(_rr) if _rr else "(sem repo)"
            except Exception:
                _live_head = "(indisponível)"
            print(f"  approved_head   : {_live_head}  (HEAD vivo no momento do approve)")
            if not args.yes:
                if not sys.stdin.isatty():
                    print(
                        "ERRO: aprovação não-interativa exige --yes explícito "
                        "(releia o resumo acima e aprove conscientemente).",
                        file=sys.stderr,
                    )
                    return 2
                resp = input("Aprovar esta run? [yes/NO] ").strip().lower()
                if resp not in ("yes", "y"):
                    print("Aprovação abortada pelo operador.")
                    return 2
            data = approve_run(
                args.run_file, args.approver, expected=_prev,
                # M4 (banca v13): o HEAD exibido é o HEAD vinculado.
                expected_head=_live_head if isinstance(_live_head, str) and "(" not in _live_head else None)
            print(f"OK: Run aprovada por '{args.approver}' -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "verify":
            run_target = args.run_opt or args.run_file
            if run_target is None:
                print("ERRO: informe o arquivo de run (posicional ou --run).", file=sys.stderr)
                return 2
            if not run_target.is_file():
                print(f"ERRO: arquivo de run inexistente: {run_target}", file=sys.stderr)
                return 2
            try:
                raw_json = json.loads(run_target.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"ERRO: JSON inválido em {run_target}: {exc}", file=sys.stderr)
                return 2
            ok, reasons = verify_run_file(run_target)
            derived, _ = derive_run_outcome(raw_json) if isinstance(raw_json, dict) else ("failed", [])
            if args.json:
                print(json.dumps({
                    "run_file": str(run_target),
                    "verified": ok,
                    "derived_outcome": derived,
                    "reasons": reasons,
                }, ensure_ascii=False, indent=2))
            else:
                if ok:
                    print(f"OK: Run '{run_target}' verificada com sucesso (outcome='verified').")
                else:
                    print(f"FALHA NA VERIFICAÇÃO DA RUN '{run_target}' (derived='{derived}'):", file=sys.stderr)
                    for r in reasons:
                        print(f"  [RUN-ERROR] {r}", file=sys.stderr)
            return 0 if ok else 1

    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

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
