#!/usr/bin/env python3
"""bench_wave7.py — mede o ganho da Wave 7 sem julgar.

Mede, no repositório atual:
  A) check sequencial (jobs=1) vs paralelo (jobs=4) para os 3 portões
     universais via processo supervisionado;
  B) scan a11y integral vs incremental (reuso de manifesto);
  C) TIA: custo de discovery+seleção vs suíte completa (reportado, não executa
     a suíte — mede somente a seleção e informa).

Imprime um relatório determinístico com tempos em ms e razões. Exit 0 sempre,
EXCETO com `--strict` (modo CI, equivalente ao bench_corpus): veredito real —
exit 1 se o paralelo degradar, os vereditos divergirem, o scanner incremental
errar, ou o grafo TIA vier vazio; exit 2 só em erro de uso/execução.

banca-v18 (rev C-N): antes `main()` retornava 0 incondicional — um step de CI
que não podia falhar.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


def bench_gates(repo: Path) -> dict:
    from henke.gate_parallel import GateSpec, run_gates_parallel
    mdir = ROOT / "henke" / "metricas"
    specs = [
        GateSpec("hazard_scan",
                 (sys.executable, str(mdir / "hazard_scan.py"), str(repo)),
                 repo, timeout_sec=180),
        GateSpec("assertless_scan",
                 (sys.executable, str(mdir / "assertless_scan.py"),
                  str(repo / "tests")), repo, timeout_sec=180),
        GateSpec("a11y_scan",
                 (sys.executable, str(mdir / "a11y_scan.py"), str(repo)),
                 repo, timeout_sec=180),
    ]
    t0 = time.perf_counter()
    seq = run_gates_parallel(specs, max_parallel=1)
    seq_ms = _ms(t0)
    t0 = time.perf_counter()
    par = run_gates_parallel(specs, max_parallel=4)
    par_ms = _ms(t0)
    return {
        "sequential_ms": seq_ms,
        "parallel4_ms": par_ms,
        "speedup": round(seq_ms / max(par_ms, 1), 2),
        "verdicts_equal": [r.exit_code for r in seq] == [r.exit_code for r in par],
        # banca-v18 (rev C-N): exit 2/125 ou output_error = gate não mediu —
        # a medição como um todo é inválida (veredito --strict depende disso).
        "gate_errors": [r.name for r in seq
                        if r.output_error or r.exit_code in (2, 125)],
    }


def bench_manifest(repo: Path) -> dict:
    from henke.metricas.scan_manifest import run_incremental_scan, scanner_specs
    spec = {s.scanner_id: s for s in scanner_specs()}["a11y"]
    import os
    old = os.environ.pop("HENKE_STATE_DIR", None)
    try:
        t0 = time.perf_counter()
        r1 = run_incremental_scan(repo, spec, full=True)
        full_ms = _ms(t0)
        t0 = time.perf_counter()
        r2 = run_incremental_scan(repo, spec)
        inc_ms = _ms(t0)
        return {
            "full_ms": full_ms,
            "incremental_ms": inc_ms,
            "speedup": round(full_ms / max(inc_ms, 1), 2),
            "reused": len(r2.reused_files),
            "scanned_again": len(r2.scanned_files),
            "exit_codes": [r1.exit_code, r2.exit_code],
        }
    finally:
        if old:
            os.environ["HENKE_STATE_DIR"] = old


def bench_tia(repo: Path) -> dict:
    from henke.import_graph import build_import_graph
    t0 = time.perf_counter()
    g = build_import_graph(repo)
    graph_ms = _ms(t0)
    return {
        "graph_build_ms": graph_ms,
        "nodes": len(g.direct),
        "unresolved": len(g.unresolved),
    }


# banca-v18 (rev C-N): checks de veredito do modo --strict. Cada um declara
# o invariante medido — o bench só reprova o que o número realmente violou.
_STRICT_CHECKS = (
    # (nome, descrição do invariante, avaliador(report)->bool)
    ("verdicts_equal",
     "gates paralelos divergiram do sequencial",
     lambda r: r["gates"]["verdicts_equal"] is True),
    ("gates_ran",
     "algum gate errou (exit 2/125/drain) — medição inválida",
     lambda r: not r["gates"]["gate_errors"]),
    ("parallel_nondegraded",
     "paralelo (jobs=4) degradou >25% vs sequencial",
     lambda r: r["gates"]["parallel4_ms"] <= r["gates"]["sequential_ms"] * 1.25),
    ("manifest_incremental_ok",
     "scan incremental a11y terminou em erro (exit 2)",
     lambda r: 2 not in r["manifest_a11y"]["exit_codes"]),
    ("incremental_nondegraded",
     "scan incremental degradou >50% vs full",
     lambda r: r["manifest_a11y"]["incremental_ms"]
     <= max(r["manifest_a11y"]["full_ms"] * 1.5, 1)),
    ("tia_graph_built",
     "grafo TIA veio vazio num repo não-vazio",
     # banca-v19 (rev FV-3): `or unresolved == 0` aprovava o grafo
     # totalmente vazio (0 nós, 0 não-resolvidos) — exatamente a falha
     # que o nome do invariante diz reprovar.
     lambda r: r["tia"]["nodes"] > 0),
)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="CI: exit 1 se algum invariante medido violar")
    args = ap.parse_args()
    repo = args.repo.resolve()

    report = {
        "repo": str(repo),
        "gates": bench_gates(repo),
        "manifest_a11y": bench_manifest(repo),
        "tia": bench_tia(repo),
    }
    if args.strict:
        # veredito real: cada violação sai nomeada no relatório (banca-v18).
        violations = [f"{name} — {why}" for name, why, chk in _STRICT_CHECKS
                      if not chk(report)]
        report["strict"] = {"verdict": "failed" if violations else "verified",
                            "violations": violations}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.strict:
        if violations:
            print("--strict: reprovado — " + "; ".join(violations), file=sys.stderr)
            return 1
        print("--strict: nenhum invariante violado — aprovado.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
