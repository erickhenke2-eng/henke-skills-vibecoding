"""cli_ops.py — ops de tooling: install-hook, report-html, test-impact, watch, clean."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys
from henke.cli_exec import _cmd_exec


def _cmd_audit_copy(repo_root: Path, out_dir: Path) -> int:
    """Materializa a cópia isolada congelada de PROTOCOLO §2/§6: copia a
    árvore (incluindo .git — o root commit ancora a identidade F-06) e grava
    `<out>.manifest.json` com contagem de arquivos e `tree_sha256` — o hash
    registrado no ato da cópia que prova congelamento antes/depois."""
    out_dir = out_dir.resolve()
    if out_dir.exists():
        print(f"ERRO: destino já existe: {out_dir} (cópia isolada exige diretório novo).",
              file=sys.stderr)
        return 2
    try:
        out_dir.relative_to(repo_root)
        print("ERRO: a cópia isolada não pode ficar dentro do repo auditado.",
              file=sys.stderr)
        return 2
    except ValueError:
        pass

    def _ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in ("__pycache__", ".pytest_cache",
                                          ".workbuddy-ai", "node_modules")}

    try:
        shutil.copytree(repo_root, out_dir, ignore=_ignore, symlinks=True)
    except (OSError, shutil.Error) as exc:
        print(f"ERRO: falha ao materializar cópia isolada: {exc}", file=sys.stderr)
        return 2

    h = hashlib.sha256()
    count = 0
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(out_dir).as_posix()
        h.update(rel.encode("utf-8") + b"\0" + str(p.stat().st_size).encode() + b"\0")
        h.update(p.read_bytes())
        count += 1
    tree_sha = h.hexdigest()
    manifest = {
        "source": str(repo_root),
        "copy": str(out_dir),
        "files": count,
        "tree_sha256": tree_sha,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": "cópia isolada congelada — compare tree_sha256 antes/depois da auditoria",
    }
    manifest_p = out_dir.parent / f"{out_dir.name}.manifest.json"
    try:
        manifest_p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except OSError as exc:
        print(f"AVISO: cópia criada mas manifesto falhou: {exc}", file=sys.stderr)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0






def _cmd_install_hook(repo_root: Path, force: bool = False) -> int:
    from henke.install_hook import install_hook

    ok, msg = install_hook(repo_root, force=force)
    if ok:
        print(f"[OK] {msg}")
        return 0
    else:
        print(f"ERRO: {msg}", file=sys.stderr)
        return 2 if "não é um repositório git" in msg.lower() else 1




def _cmd_report_html(repo_root: Path, out_path: Path | None = None) -> int:
    from henke.report_generator import generate_report

    try:
        res = generate_report(repo_root, output_file=out_path)
        print(f"[OK] Relatório gerado em: {res}")
        return 0
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


def _cmd_test_impact(repo_root: Path, base: str, *, full: bool = False,
                     run: bool = False, as_json: bool = False) -> int:
    from henke.import_graph import build_import_graph
    from henke.test_impact import (
        GitDiscoveryError, ImpactSelection, build_unittest_command, select_tests,
    )
    try:
        if full:
            from henke.test_impact import discover_changed_files
            changed = discover_changed_files(repo_root, base)
            sel = ImpactSelection("full", tuple(changed), (), ("forced_full",), ())
        else:
            graph = build_import_graph(repo_root)
            sel = select_tests(repo_root, base, graph.direct,
                               unresolved=graph.unresolved)
    except GitDiscoveryError as exc:
        print(f"ERRO: descoberta Git falhou: {exc}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps({
            "mode": sel.mode,
            "changed_files": list(sel.changed_files),
            "test_files": list(sel.test_files),
            "reasons": list(sel.reasons),
            "unresolved_files": list(sel.unresolved_files),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"mode={sel.mode} tests={len(sel.test_files)} "
              f"changed={len(sel.changed_files)} reasons={','.join(sel.reasons) or '-'}")
        for t in sel.test_files:
            print(f"  selected: {t}")

    if not run:
        return 0
    if sel.mode == "selected" and not sel.test_files:
        print("no_changes: nenhum teste a executar.")
        return 0
    cmd = build_unittest_command(sel, repo_root)
    ledger = repo_root / ".henke" / "LEDGER.json"
    exec_log = repo_root / ".henke" / "exec_log.jsonl"
    return _cmd_exec(repo_root, ledger, exec_log, ["--"] + cmd)


def _cmd_watch(repo_root: Path, *, quiet: bool = False,
               poll: float = 0.75, debounce: float = 0.30) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("ERRO: 'henke watch' é exclusivamente foreground/interativo (TTY exigido).",
              file=sys.stderr)
        return 2
    from henke.henke_watch import WatchConfig, WatchController
    cfg = WatchConfig(repo_root=repo_root, poll_interval_sec=poll,
                      debounce_sec=debounce, quiet=quiet)
    return WatchController(cfg).run_foreground()


def _cmd_clean(repo_root: Path, *, execute: bool = False,
               restore: str | None = None, purge_expired: bool = False,
               confirm_batch: str | None = None, as_json: bool = False) -> int:
    from henke.henke_clean import (
        plan_clean, purge_expired_batch, quarantine, restore_batch,
        staging_leftovers,
    )
    if restore is not None:
        if not execute:
            print("ERRO: --restore exige --execute.", file=sys.stderr)
            return 2
        n = restore_batch(repo_root, restore)
        if n == 0:
            # DS-2: "0 restaurados" não é sucesso — todos os itens foram
            # pulados (não-verificados, destino ocupado ou fora da raiz).
            print(f"[FALHA] 0 item(ns) restaurado(s) do batch {restore} "
                  "(veja clean_log.jsonl).", file=sys.stderr)
            return 1
        print(f"[OK] {n} item(ns) restaurado(s) do batch {restore}.")
        return 0
    if purge_expired:
        if not execute or not confirm_batch:
            print("ERRO: --purge-expired exige --execute e --confirm-batch <id>.",
                  file=sys.stderr)
            return 2
        try:
            n = purge_expired_batch(repo_root, confirm_batch,
                                    now=datetime.now(timezone.utc))
        except ValueError as exc:
            print(f"ERRO: {exc}", file=sys.stderr)
            return 2
        print(f"[OK] batch {confirm_batch} purgado ({n}).")
        return 0
    candidates = plan_clean(repo_root)
    leftovers = staging_leftovers(repo_root)
    if as_json:
        print(json.dumps({
            "candidates": [
                {"source": str(c.source), "kind": c.kind, "size_bytes": c.size_bytes}
                for c in candidates
            ],
            "staging_leftovers": [str(p) for p in leftovers],
            "dry_run": not execute,
        }, ensure_ascii=False, indent=2))
    else:
        for c in candidates:
            print(f"  [{c.kind}] {c.source} ({c.size_bytes} bytes)")
        for p in leftovers:
            print(f"  [staging-leftover] {p}")
        print(f"{len(candidates)} candidato(s); "
              f"{'EXECUTANDO quarentena' if execute else 'dry-run (use --execute para mover)'}")
    if not execute:
        return 0
    if not candidates:
        return 0
    batch = quarantine(repo_root, candidates)
    print(f"[OK] batch {batch.batch_id}: {len(batch.items)} item(ns) em quarentena "
          f"(expira {batch.expires_at}).")
    return 0
