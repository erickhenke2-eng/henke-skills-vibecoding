#!/usr/bin/env python3
"""gate_pipeline.py — coletor → portão para gates que consomem artefatos gerados.

`ratchet` e `fence_check` não leem o repositório diretamente: consomem TSV/JSON
produzidos por `repo_signals.py`. Este wrapper é o que os torna autoexecutáveis
quando o projeto declara seus insumos:

  .henke/ratchet-baseline.tsv  →  gate_pipeline.py ratchet --repo R --baseline B
  .henke/fence.json            →  gate_pipeline.py fence   --repo R --fence F

Os artefatos frescos do coletor vão para `.henke/evidence/pipeline/` (runtime
metadata, mesmo status dos demais artefatos de execução — excluídos do
spec_drift, presentes para auditoria).

Códigos de saída:
  0/1  exit do portão final — o veredito é sempre do portão, nunca do coletor
  2    uso indevido, coletor falhou ou artefato vazio — sem veredito, sem verde
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_SIGNALS = SCRIPTS_DIR / "repo_signals.py"
METRICAS_DIR = SCRIPTS_DIR / "metricas"
GATES = {
    "ratchet": METRICAS_DIR / "ratchet.py",
    "fence": METRICAS_DIR / "fence_check.py",
}
COLLECTORS = {
    # ratchet consome TSV por-arquivo (`--per-file`), não por-função
    "ratchet": ["metrics", "--per-file"],
    "fence": ["edges"],
}
EVIDENCE_NAME = {
    "ratchet": "ratchet-current.tsv",
    "fence": "edges-current.json",
}


def _run(argv: list[str], cwd: Path | None = None,
         timeout: float = 300.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            # F-8 (banca v12): GIT_* herdado redirecionava o git dos coletores.
            env={k: v for k, v in os.environ.items()
                 if not (k.startswith("GIT_") or k.startswith("PYTHON")
                        or k.startswith("LD_") or k.startswith("DYLD_")
                        or k in ("BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS",
                                 "PERL5OPT", "PERL5LIB", "RUBYOPT",
                                 "DOTNET_STARTUP_HOOKS"))
                 } | {"NoDefaultCurrentDirectoryInExePath": "1"},
        )
    except subprocess.TimeoutExpired:
        # A15 (banca v14): timeout propagava traceback exit 1 — infra é 2.
        return subprocess.CompletedProcess(
            argv, returncode=2, stdout="",
            stderr=f"gate_pipeline: timeout ({timeout}s) em {argv[:2]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gate", choices=sorted(GATES))
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--baseline", type=Path, default=None, help="TSV baseline (ratchet)")
    ap.add_argument("--fence", type=Path, default=None, help="fence.json de camadas (fence)")
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        # --help sai 0 no argparse — um portão sem veredito nunca é verde.
        sys.exit(2)

    repo = args.repo.resolve()
    gate = args.gate
    if gate == "ratchet" and (args.baseline is None or not args.baseline.is_file()):
        print("ERRO: ratchet exige --baseline <tsv> existente (.henke/ratchet-baseline.tsv)", file=sys.stderr)
        return 2
    if gate == "fence" and (args.fence is None or not args.fence.is_file()):
        print("ERRO: fence exige --fence <json> existente (.henke/fence.json)", file=sys.stderr)
        return 2

    # 1) Coletor: produz o artefato fresco que o portão consome.
    try:
        collector = _run([sys.executable, "-Es", str(REPO_SIGNALS), *COLLECTORS[gate], "--root", str(repo)])
    except subprocess.TimeoutExpired:
        # B8 (banca v13): TimeoutExpired virava traceback exit 1.
        print(f"gate_pipeline: coletor '{COLLECTORS[gate]}' expirou", file=sys.stderr)
        return 2
    if collector.returncode != 0 or not (collector.stdout or "").strip():
        print(
            f"gate_pipeline: coletor '{COLLECTORS[gate]}' falhou "
            f"(exit {collector.returncode}): {(collector.stderr or '')[-400:]}",
            file=sys.stderr,
        )
        return 2
    # Coletor degradado: arquivos que não parsearam saem do grafo em silêncio —
    # fence/ratchet verdes sobre evidência incompleta é falso-verde. O portão
    # exige grafo completo (M6 banca v6).
    skipped = [
        ln[12:].strip() for ln in (collector.stderr or "").splitlines()
        if ln.startswith("SKIP-PARSE:")
    ]
    if skipped:
        print(
            "gate_pipeline: coletor incompleto — arquivos não parseados "
            f"({len(skipped)}): {', '.join(skipped[:10])}",
            file=sys.stderr,
        )
        return 2

    # fence com zero arestas internas não tem o que verificar: exit 3 (n/a
    # honesto) — não é verde (não conta para all_green) nem vermelho. Repo de
    # arquivo único ou sem imports internos não pode ser punido nem premiado.
    # Distinto de coletor quebrado (exit 2 acima): aqui o coletor rodou e o
    # resultado legítimo foi "sem arestas".
    if gate == "fence":
        try:
            edges = json.loads(collector.stdout)
        except Exception:
            edges = None
        if isinstance(edges, list) and not edges:
            print("N/A: nenhuma aresta interna no repo — fence não tem o que verificar")
            return 3

    ev_dir = repo / ".henke" / "evidence" / "pipeline"
    try:
        ev_dir.mkdir(parents=True, exist_ok=True)
        artifact = ev_dir / EVIDENCE_NAME[gate]
        # >4MB NÃO trunca: um artefato parcial alimentaria o portão com
        # evidência incompleta (falso-verde banca v8 — F6). E escrita é
        # tmp+replace: crash no meio não deixa artefato truncado servindo o
        # portão seguinte nem outro run concorrente (A5).
        if len(collector.stdout) > 4 * 1024 * 1024:
            print("gate_pipeline: saída do coletor excede 4MB — recusada", file=sys.stderr)
            return 2
        tmp = artifact.with_suffix(artifact.suffix + f".{os.getpid()}.tmp")
        tmp.write_text(collector.stdout, encoding="utf-8")
        os.replace(tmp, artifact)
    except OSError as exc:
        print(f"gate_pipeline: falha ao gravar artefato: {exc}", file=sys.stderr)
        return 2

    # 2) Portão real sobre o artefato.
    if gate == "ratchet":
        gate_argv = [sys.executable, "-Es", str(GATES[gate]), "--baseline", str(args.baseline), "--current", str(artifact)]
    else:
        gate_argv = [sys.executable, "-Es", str(GATES[gate]), str(args.fence), str(artifact)]
    res = _run(gate_argv, cwd=repo)
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    return res.returncode


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
