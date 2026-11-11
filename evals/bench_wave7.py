#!/usr/bin/env python3
"""bench_wave7.py — mede o ganho da Wave 7 sem julgar.

Mede, no repositório atual:
  A) check sequencial (jobs=1) vs paralelo (jobs=4) para os 3 portões
     universais via processo supervisionado;
  B) scan a11y integral vs incremental (reuso de manifesto);
  C) TIA: custo de discovery+seleção vs suíte completa (reportado, não executa
     a suíte — mede somente a seleção e informa).

Imprime um relatório determinístico com tempos em ms e razões. Exit 0 sempre.
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


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, default=ROOT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    repo = args.repo.resolve()

    report = {
        "repo": str(repo),
        "gates": bench_gates(repo),
        "manifest_a11y": bench_manifest(repo),
        "tia": bench_tia(repo),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
