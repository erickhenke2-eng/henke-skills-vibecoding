"""henke_watch.py — 'henke watch' foreground-only e interativo (Wave 7, R6).

Sem daemon, serviço, start/stop/status, arquivo de PID, socket, named pipe ou
thread persistente em background. O loop vive no thread principal da sessão;
Ctrl+C fecha o supervisor próprio e retorna 130.

Cada ciclo roda os portões como processo supervisionado (Job Object/process
group). `--quiet` suprime apenas repetições verdes idênticas; transições e
falhas sempre aparecem.
"""
from __future__ import annotations

import hashlib
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import scripts.winpath as winpath
from scripts.gate_parallel import GateResult, GateSpec, run_gates_parallel

_PRUNE = frozenset({
    ".git", ".hg", "node_modules", "dist", "build", "out", "target",
    "vendor", ".venv", "venv", "coverage", ".next", ".nuxt",
    "__pycache__", ".henke", ".pytest_cache", ".mypy_cache",
})
_MAX_SNAPSHOT_FILES = 20000
_SUMMARY_RE_LIMIT = 256


@dataclass(frozen=True)
class WatchConfig:
    repo_root: Path
    poll_interval_sec: float = 0.75
    debounce_sec: float = 0.30
    quiet: bool = False
    gate_timeout_sec: float = 120.0
    extra_gate_args: tuple[tuple[str, tuple[str, ...]], ...] = ()


def _snapshot(repo_root: Path) -> dict[str, tuple[int, int]] | None:
    """Mapa relpath→(size, mtime_ns) ou None quando a árvore está instável."""
    snap: dict[str, tuple[int, int]] = {}
    for entry in winpath.iter_tree(Path(repo_root), prune_names=_PRUNE):
        if entry.kind == "error":
            return None
        if entry.kind != "file":
            continue
        try:
            st = entry.path.stat(follow_symlinks=False)
        except OSError:
            return None
        snap[entry.relative_path] = (st.st_size, st.st_mtime_ns)
        if len(snap) > _MAX_SNAPSHOT_FILES:
            return None
    return snap


def _summary_sha256(text: str) -> str:
    return hashlib.sha256(
        text[-_SUMMARY_RE_LIMIT:].encode("utf-8", errors="replace")
    ).hexdigest()


class WatchController:
    """Loop foreground: poll → debounce → portões supervisionados → relatório."""

    def __init__(self, config: WatchConfig):
        self.config = config
        self._stop = threading.Event()

    def _gate_specs(self) -> list[GateSpec]:
        mdir = Path(__file__).resolve().parent / "metricas"
        repo = self.config.repo_root
        specs = [
            GateSpec("hazard_scan",
                     (sys.executable, "-Es", str(mdir / "hazard_scan.py"), str(repo)),
                     repo, timeout_sec=self.config.gate_timeout_sec),
            GateSpec("a11y_scan",
                     (sys.executable, "-Es", str(mdir / "a11y_scan.py"), str(repo)),
                     repo, timeout_sec=self.config.gate_timeout_sec),
        ]
        if (repo / "tests").is_dir():
            specs.insert(1, GateSpec(
                "assertless_scan",
                (sys.executable, "-Es", str(mdir / "assertless_scan.py"), str(repo / "tests")),
                repo, timeout_sec=self.config.gate_timeout_sec))
        for name, argv_tail in self.config.extra_gate_args:
            specs.append(GateSpec(
                name, (sys.executable, "-Es", *argv_tail), repo,
                timeout_sec=self.config.gate_timeout_sec))
        return specs

    def _print_result(self, results: list[GateResult], green_hash: str) -> None:
        all_pass = all(r.passed for r in results)
        this_hash = green_hash if all_pass else ""
        if all_pass and self.config.quiet and this_hash == self._last_green_hash:
            return
        if all_pass:
            self._last_green_hash = this_hash
        else:
            self._last_green_hash = ""
        ts = time.strftime("%H:%M:%S")
        if all_pass:
            print(f"[{ts}] watch: verde "
                  f"({', '.join(f'{r.name}={r.exit_code}' for r in results)})",
                  flush=True)
            return
        for r in results:
            status = "PASS" if r.passed else (
                "TIMEOUT" if r.timed_out else f"FAIL(exit {r.exit_code})")
            print(f"[{ts}] [{status}] {r.name}", flush=True)
            tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            for ln in tail[-10:]:
                print(f"    {ln}", flush=True)

    _last_green_hash: str = ""

    def run_foreground(self) -> int:
        repo = Path(self.config.repo_root)
        prev = _snapshot(repo)
        print("henke watch: foreground; Ctrl+C para sair.", flush=True)

        def _on_sigint(_sig, _frm):
            self._stop.set()

        old_handler = None
        try:
            old_handler = signal.signal(signal.SIGINT, _on_sigint)
        except (ValueError, OSError):
            old_handler = None

        try:
            specs = self._gate_specs()
            results = run_gates_parallel(specs, max_parallel=min(4, len(specs)))
            self._print_result(
                results,
                _summary_sha256("".join(r.stdout or "" for r in results)),
            )
            while not self._stop.is_set():
                time.sleep(self.config.poll_interval_sec)
                cur = _snapshot(repo)
                if cur is None or cur == prev:
                    continue
                # Debounce: aguarda a árvore estabilizar antes de disparar.
                deadline = time.monotonic() + self.config.debounce_sec
                while time.monotonic() < deadline:
                    time.sleep(min(0.05, self.config.debounce_sec))
                    nxt = _snapshot(repo)
                    if nxt is None or nxt == cur:
                        break
                    cur = nxt
                if cur is None or cur == prev or self._stop.is_set():
                    continue
                prev = cur
                results = run_gates_parallel(
                    specs, max_parallel=min(4, len(specs)))
                self._print_result(
                    results,
                    _summary_sha256("".join(r.stdout or "" for r in results)),
                )
        except KeyboardInterrupt:
            pass
        finally:
            if old_handler is not None:
                try:
                    signal.signal(signal.SIGINT, old_handler)
                except (ValueError, OSError):
                    pass
        print("henke watch: encerrado (130).", flush=True)
        return 130
