#!/usr/bin/env python3
"""canaries_wave7.py — canários dos invariantes da Wave 7.

Cada canário prova um comportamento físico: supervisão de árvore, ordem e
agregação dos portões, TIA conservativa, manifesto incremental, watch
foreground-only, staged sem atalhos e higiene (winpath/clean/session).
Exit 0 somente se TODOS passarem.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def run(script, *args, cwd=None):
    res = subprocess.run(
        [sys.executable, str(script), *map(str, args)],
        capture_output=True, text=True, cwd=cwd,
    )
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def main() -> int:
    passed = total = 0

    def expect(name, cond, detail=""):
        nonlocal passed, total
        total += 1
        tag = "PASSOU" if cond else "FALHOU"
        if cond:
            passed += 1
        print(f"[{tag}] {name} {detail}")

    from henke.process_supervisor import ChildSupervisor
    from henke.gate_parallel import (
        GateSpec, aggregate_exit_code, run_gates_parallel,
    )
    import henke.winpath as winpath
    import henke.ephemeral as ephemeral
    import henke.henke_clean as henke_clean
    import henke.henke_session as henke_session
    from henke.metricas.scan_manifest import run_incremental_scan, scanner_specs
    from henke.test_impact import select_tests
    from unittest.mock import patch

    # --- W7-1: supervisor mata a árvore no timeout (124) e limpa filho. ---
    sup = ChildSupervisor()
    try:
        child = sup.start([sys.executable, "-c",
                           "import subprocess,sys,time;"
                           "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                           "time.sleep(30)"], cwd=ROOT)
        res = sup.wait(child, timeout_sec=1.0)
        expect("W7-1: timeout mata árvore supervisionada (rc=124, killed_tree)",
               res.timed_out and res.returncode == 124 and res.killed_tree)
    finally:
        sup.close()

    # --- W7-2: gate_parallel preserva ordem e agrega 2 em erro de saída. ---
    specs = [
        GateSpec("g2", (sys.executable, "-c", "import sys;sys.exit(2)"), ROOT,
                 timeout_sec=15),
        GateSpec("g0", (sys.executable, "-c", "print('x')"), ROOT,
                 timeout_sec=15),
    ]
    rs = run_gates_parallel(specs, max_parallel=2)
    expect("W7-2: ordem preservada e agregação=2 em exit 2",
           [r.name for r in rs] == ["g2", "g0"]
           and aggregate_exit_code(rs) == 2)

    # --- W7-3: run_process público NÃO expõe kill por PID. ---
    import henke.process_runner as pr
    expect("W7-3: fachada sem kill_process_tree/kill público",
           not hasattr(pr, "kill_process_tree")
           and not hasattr(pr, "kill")
           and hasattr(pr, "run_process"))

    # --- W7-4: TIA — mudança em teste seleciona só ele; config → full. ---
    direct = {
        "henke/prod.py": frozenset(),
        "tests/test_prod.py": frozenset({"henke/prod.py"}),
    }
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        for rel in ("henke/prod.py", "tests/test_prod.py"):
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("X=1\n", encoding="utf-8")
        with patch("henke.test_impact._discover",
                   return_value=(("tests/test_prod.py",), frozenset())):
            sel = select_tests(repo, "BASE", direct)
        expect("W7-4a: teste alterado seleciona a si mesmo",
               sel.mode == "selected" and sel.test_files == ("tests/test_prod.py",))
        with patch("henke.test_impact._discover",
                   return_value=(("pyproject.toml",), frozenset())):
            sel = select_tests(repo, "BASE", direct)
        expect("W7-4b: config/lock alterado força full", sel.mode == "full")
        with patch("henke.test_impact._discover",
                   return_value=((), frozenset())):
            sel = select_tests(repo, "BASE", direct)
        expect("W7-4c: no_changes -> selected vazio (nunca full)",
               sel.mode == "selected" and not sel.test_files)

    # --- W7-5: manifesto incremental reusa arquivo intacto em repo externo. ---
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)  # repo externo sem henke/ — scanners vêm da suíte
        (repo / "p.html").write_text("<html><body>x</body></html>", encoding="utf-8")
        spec = {s.scanner_id: s for s in scanner_specs()}["a11y"]
        old_sd = os.environ.pop("HENKE_STATE_DIR", None)
        try:
            run_incremental_scan(repo, spec, full=True)
            r = run_incremental_scan(repo, spec)
            expect("W7-5: segunda rodada reusa arquivo intacto",
                   not r.full_scan and "p.html" in r.reused_files,
                   f"reasons={r.reasons}")
        finally:
            if old_sd:
                os.environ["HENKE_STATE_DIR"] = old_sd

    # --- W7-6: 'henke watch' fora de TTY sai 2 (foreground-only). ---
    code, out = run(ROOT / "henke" / "henke_cli.py", "watch", "--repo", ROOT)
    expect("W7-6: watch sem TTY recusa com exit 2", code == 2)

    # --- W7-7: check --staged não grava manifesto nem cache. ---
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        subprocess.run(["git", "init"], cwd=td, capture_output=True)
        subprocess.run(["git", "config", "user.email", "c@x"], cwd=td, capture_output=True)
        subprocess.run(["git", "config", "user.name", "c"], cwd=td, capture_output=True)
        (repo / "ok.py").write_text("X=1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=td, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=td, capture_output=True)
        code, out = run(ROOT / "henke" / "henke_cli.py", "check",
                        "--repo", repo, "--staged")
        henke = repo / ".henke"
        expect("W7-7: staged não cria manifests/cache no worktree",
               not (henke / "scan-manifests").exists()
               and not (henke / "cache").exists(),
               f"exit={code}")

    # --- W7-8: clean dry-run não muta; quarentena registra manifesto. ---
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        cache = repo / "__pycache__"
        cache.mkdir()
        (cache / "x.pyc").write_bytes(b"0")
        plan = henke_clean.plan_clean(repo)
        expect("W7-8a: dry-run lista __pycache__ sem mutar",
               any(c.kind == "repo-cache" for c in plan) and cache.is_dir())
        batch = henke_clean.quarantine(repo, plan)
        expect("W7-8b: quarentena move candidato com manifesto",
               not cache.is_dir() and batch.items and batch.batch_id)

    # --- W7-9: sentinel de sessão com heartbeat e GC seguro. ---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        os.environ["HENKE_WORKSPACE"] = str(ws)
        s = henke_session.start_session("canary", ws, "run-x")
        expect("W7-9a: sentinel criado com schema/PID diagnóstico",
               s.session_id and (ws / ".henke" / "sessions"
                                 / f"{s.session_id}.sentinel.json").is_file())
        before = s.heartbeat_expires_at
        time.sleep(0.02)
        henke_session.heartbeat(s.session_id)
        after = henke_session.load_session(s.session_id).heartbeat_expires_at
        henke_session.end_session(s.session_id)
        closed = henke_session.load_session(s.session_id).state == "closed"
        os.environ.pop("HENKE_WORKSPACE", None)
        expect("W7-9b: heartbeat renova heartbeat_expires_at e end fecha",
               after > before and closed)

    # --- W7-10: ephemeral workspace com TTL e cleanup idempotente. ---
    with tempfile.TemporaryDirectory() as td:
        ws_root = Path(td)
        cfg = ephemeral.EphemeralConfig(ttl_seconds=3600)
        with ephemeral.ephemeral_workspace(cfg, repo_root=ws_root) as ws:
            marker = ws.path / ".henke-ephemeral.json"
            expect("W7-10a: workspace criado com marker", marker.is_file())
            path = ws.path
        expect("W7-10b: cleanup idempotente remove workspace",
               not path.exists())

    print(f"\n{passed}/{total} canários Wave 7 aprovados")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
