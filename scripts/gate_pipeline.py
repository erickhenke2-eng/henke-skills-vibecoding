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


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("gate", choices=sorted(GATES))
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--baseline", type=Path, default=None, help="TSV baseline (ratchet)")
    ap.add_argument("--fence", type=Path, default=None, help="fence.json de camadas (fence)")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    gate = args.gate
    if gate == "ratchet" and (args.baseline is None or not args.baseline.is_file()):
        print("ERRO: ratchet exige --baseline <tsv> existente (.henke/ratchet-baseline.tsv)", file=sys.stderr)
        return 2
    if gate == "fence" and (args.fence is None or not args.fence.is_file()):
        print("ERRO: fence exige --fence <json> existente (.henke/fence.json)", file=sys.stderr)
        return 2

    # 1) Coletor: produz o artefato fresco que o portão consome.
    collector = _run([sys.executable, str(REPO_SIGNALS), *COLLECTORS[gate], "--root", str(repo)])
    if collector.returncode != 0 or not (collector.stdout or "").strip():
        print(
            f"gate_pipeline: coletor '{COLLECTORS[gate]}' falhou "
            f"(exit {collector.returncode}): {(collector.stderr or '')[-400:]}",
            file=sys.stderr,
        )
        return 2
    ev_dir = repo / ".henke" / "evidence" / "pipeline"
    ev_dir.mkdir(parents=True, exist_ok=True)
    artifact = ev_dir / EVIDENCE_NAME[gate]
    artifact.write_text(collector.stdout, encoding="utf-8")

    # 2) Portão real sobre o artefato.
    if gate == "ratchet":
        gate_argv = [sys.executable, str(GATES[gate]), "--baseline", str(args.baseline), "--current", str(artifact)]
    else:
        gate_argv = [sys.executable, str(GATES[gate]), str(args.fence), str(artifact)]
    res = _run(gate_argv, cwd=repo)
    if res.stdout:
        sys.stdout.write(res.stdout)
    if res.stderr:
        sys.stderr.write(res.stderr)
    return res.returncode


if __name__ == "__main__":
    sys.exit(main())
