"""scan_manifest.py — scan incremental transacional por scanner (Wave 7, R5).

Um manifesto INDEPENDENTE por scanner em `<state_dir>/scan-manifests/<id>.json`;
não existe manifesto global compartilhado. Cada entrada guarda hash de conteúdo,
metadados, dependências e findings. O fingerprint agrega versão, SHA-256 do
script, `engine_sha256` (motor + helpers + scanner), Python major.minor,
argumentos e configs — mudança em qualquer um invalida o manifesto inteiro.

Algoritmo (§3.7.3): lock não bloqueante → snapshot via winpath → validação do
manifesto → `dirty = reverse_closure(changed)` com arestas antigas ∪ novas →
scan só dos dirty existentes + checagens globais sempre → escrita atômica só
em sucesso total. Corrupção, `.tmp` incompleto ou divergência → full rebuild,
nunca "cache vazio com zero findings".
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from dataclasses import dataclass
from typing import Literal

from scripts.state_paths import state_dir
import scripts.winpath as winpath

_SCHEMA_VERSION = 1
_GRAPH_VERSION = 1

_SNAPSHOT_UNSTABLE = "SNAPSHOT_UNSTABLE"


@dataclass(frozen=True)
class ScannerSpec:
    scanner_id: Literal["hazard", "assertless", "a11y"]
    scanner_version: str
    script_path: Path
    config_paths: tuple[str, ...]


@dataclass(frozen=True)
class IncrementalScanResult:
    scanner_id: str
    exit_code: int
    findings: tuple[dict[str, object], ...]
    scanned_files: tuple[str, ...]
    reused_files: tuple[str, ...]
    invalidated_files: tuple[str, ...]
    full_scan: bool
    reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# Lock de manifesto (não bloqueante, por scanner)
# ---------------------------------------------------------------------------


class _ManifestLock:
    """Lock não bloqueante: msvcrt.locking no Windows, fcntl.flock no POSIX."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        try:
            self._fh.close()
        finally:
            self._fh = None


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def _sha256_text(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _engine_sha256(repo_root: Path, spec: ScannerSpec) -> str:
    """Motor + helpers + scanner, em ordem de caminho POSIX."""
    repo = Path(repo_root).resolve()
    parts = [
        "scripts/metricas/scan_manifest.py",
        "scripts/import_graph.py",
        "scripts/repo_signals.py",
        "scripts/metricas/polyglot.py",
        "scripts/winpath.py",
    ]
    try:
        rel_script = Path(spec.script_path).resolve().relative_to(repo).as_posix()
    except ValueError:
        rel_script = Path(spec.script_path).as_posix()
    parts.append(rel_script)
    agg = hashlib.sha256()
    for rel in sorted(parts):
        p = repo / rel
        try:
            agg.update(rel.encode("utf-8") + b"\x00" + _sha256_text(p).encode("ascii") + b"\x00")
        except OSError:
            agg.update(rel.encode("utf-8") + b"\x00MISSING\x00")
    return agg.hexdigest()


def _config_sha256(repo_root: Path, spec: ScannerSpec) -> str:
    agg = hashlib.sha256()
    repo = Path(repo_root).resolve()
    for rel in sorted(spec.config_paths):
        p = repo / rel
        try:
            data = p.read_bytes()
        except OSError:
            data = b""
        agg.update(rel.encode("utf-8") + b"\x00" + data + b"\x00")
    return agg.hexdigest()


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _metricas_dir(repo_root: Path) -> Path:
    return Path(repo_root).resolve() / "scripts" / "metricas"


def _load_scanner_module(repo_root: Path, spec: ScannerSpec):
    """Importa o módulo do scanner por caminho (stdlib only)."""
    import importlib.util
    path = Path(spec.script_path).resolve()
    mod_name = f"_henke_scan_{spec.scanner_id}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    imp = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(imp)
    sys.path.insert(0, str(path.parent))
    try:
        imp.loader.exec_module(mod)
    finally:
        sys.path.remove(str(path.parent))
    sys.modules[mod_name] = mod
    return mod


def scanner_specs(repo_root: Path) -> tuple[ScannerSpec, ...]:
    """Os três scanners com adapter nesta Wave."""
    mdir = _metricas_dir(repo_root)
    return (
        ScannerSpec("hazard", "wave7.1", mdir / "hazard_scan.py",
                    ("evals/hazard_fixtures_manifest.json",)),
        ScannerSpec("assertless", "wave7.1", mdir / "assertless_scan.py", ()),
        ScannerSpec("a11y", "wave7.1", mdir / "a11y_scan.py", ()),
    )


def _eligible(spec: ScannerSpec, rel: str, mod) -> bool:
    """Elegibilidade lida do módulo do scanner — zero drift de constantes."""
    name = rel.rsplit("/", 1)[-1]
    if spec.scanner_id == "hazard":
        p = Path(rel)
        return p.suffix.lower() in mod.SCAN_EXTS or mod._is_config_file(p)
    if spec.scanner_id == "assertless":
        return bool(mod.TEST_FILE.match(name)) or (
            name.endswith(".rs") and "tests" in rel.split("/")[:-1]
        )
    if spec.scanner_id == "a11y":
        return Path(rel).suffix.lower() in mod.A11Y_EXTS
    raise ValueError(f"scanner desconhecido: {spec.scanner_id}")


def _scan_one(repo_root: Path, spec: ScannerSpec, path: Path, rel: str, mod):
    """(status, findings) ou None em erro de leitura/parse (nunca findings vazios por erro)."""
    if spec.scanner_id == "hazard":
        sc, fnd, ov = mod.scan_file(path)
        findings = [{"rule": r, "location": loc, "scope": sc_, "message": msg}
                    for r, loc, sc_, msg in fnd]
        status = "skipped_oversize" if ov else "scanned"
        return status, sc, findings
    if spec.scanner_id == "assertless":
        out = mod.scan_test_file(path, set())
        if out is None:
            return None
        n, fnd = out
        findings = [{"rule": v, "location": f"{rel}:{fn.lineno}", "fn": fn.name,
                     "message": why} for v, _p, fn, why in fnd]
        return "scanned", n, findings
    if spec.scanner_id == "a11y":
        fnd = mod.scan_a11y_file(path, root=Path(repo_root).resolve())
        findings = [{"rule": r, "location": loc, "message": msg} for r, loc, msg in fnd]
        return "scanned", 1 if path.suffix.lower() in mod.A11Y_EXTS else 0, findings
    raise ValueError(f"scanner desconhecido: {spec.scanner_id}")


def _global_findings(repo_root: Path, spec: ScannerSpec, mod) -> list[dict]:
    """Checagens globais do adapter — executadas em TODA rodada, nunca cacheadas."""
    if spec.scanner_id == "hazard":
        fnd = mod._check_env_committed(Path(repo_root).resolve())
        return [{"rule": r, "location": loc, "scope": sc, "message": msg}
                for r, loc, sc, msg in fnd]
    return []


def _empty_exit(spec: ScannerSpec) -> int:
    """Código auxiliar para 'nada escaneado': ausência de veredito, nunca sucesso."""
    return {"hazard": 2, "assertless": 3, "a11y": 0}[spec.scanner_id]


def _nonempty_exit(spec: ScannerSpec, findings: list[dict]) -> int:
    return 1 if findings else 0


# ---------------------------------------------------------------------------
# Núcleo transacional
# ---------------------------------------------------------------------------


def _manifest_dir(repo_root: Path) -> Path:
    return state_dir(Path(repo_root)) / "scan-manifests"


def _manifest_path(repo_root: Path, spec: ScannerSpec) -> Path:
    return _manifest_dir(repo_root) / f"{spec.scanner_id}.json"


def _load_manifest(repo_root: Path, spec: ScannerSpec) -> dict | None:
    path = _manifest_path(repo_root, spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        return None
    return data


def _write_manifest_atomic(repo_root: Path, spec: ScannerSpec, manifest: dict) -> None:
    path = _manifest_path(repo_root, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{spec.scanner_id}.",
                             suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _fingerprint(repo_root: Path, spec: ScannerSpec, args_sha256: str) -> dict:
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "id": spec.scanner_id,
        "version": spec.scanner_version,
        "script_sha256": _sha256_text(Path(spec.script_path)),
        "engine_sha256": _engine_sha256(repo_root, spec),
        "python": py,
        "args_sha256": args_sha256,
        "config_sha256": _config_sha256(repo_root, spec),
        "graph_version": _GRAPH_VERSION,
    }


def _graph_edges(repo_root: Path) -> dict[str, frozenset[str]]:
    try:
        from scripts.import_graph import build_import_graph
        g = build_import_graph(repo_root)
        return dict(g.direct), g.unresolved
    except Exception:
        return {}, {"__engine__": ("graph_build_failed",)}


def run_incremental_scan(repo_root: Path, spec: ScannerSpec, *,
                         full: bool = False) -> IncrementalScanResult:
    """Executa scan incremental transacional para `spec`."""
    repo = Path(repo_root).resolve()
    reasons: list[str] = []
    mod = _load_scanner_module(repo, spec)
    version = str(getattr(mod, "SCANNER_VERSION", spec.scanner_version))
    spec = ScannerSpec(spec.scanner_id, version, spec.script_path, spec.config_paths)
    args_sha256 = hashlib.sha256(f"{spec.scanner_id}|{spec.scanner_version}".encode()).hexdigest()
    fp = _fingerprint(repo, spec, args_sha256)

    lock = _ManifestLock(_manifest_dir(repo) / f"{spec.scanner_id}.lock")
    locked = lock.acquire()
    if not locked:
        reasons.append("manifest_locked")

    try:
        # 2. Snapshot: enumeração + hashes estáveis (winpath, sem links).
        files_now: dict[str, dict] = {}
        unstable = False
        for entry in winpath.iter_tree(repo, prune_names=frozenset({
            ".git", ".hg", "node_modules", "dist", "build", "out", "target",
            "vendor", ".venv", "venv", "coverage", ".next", ".nuxt",
            ".svelte-kit", ".turbo", "__pycache__", ".pytest_cache",
            ".mypy_cache", ".ruff_cache", ".henke",
        })):
            if entry.kind == "error":
                unstable = True
                reasons.append(f"tree_error:{entry.relative_path}:{entry.reason}")
                continue
            if entry.kind != "file" or not _eligible(spec, entry.relative_path, mod):
                continue
            try:
                dig = winpath.sha256_file(entry.path)
            except (winpath.UnstableFileError, winpath.PathSafetyError, OSError) as exc:
                unstable = True
                reasons.append(f"hash_error:{entry.relative_path}:{type(exc).__name__}")
                continue
            files_now[entry.relative_path] = {
                "sha256": dig.sha256, "size": dig.size, "mtime_ns": dig.mtime_ns,
            }
        if unstable:
            return IncrementalScanResult(
                spec.scanner_id, 2, (), (), (), (), True,
                tuple([*_clean_reasons(reasons), _SNAPSHOT_UNSTABLE]),
            )

        # Grafo: arestas atuais + (depois) arestas antigas do manifesto.
        direct_now, unresolved = _graph_edges(repo)
        if unresolved:
            reasons.append("graph_unresolved:full_rebuild")

        old = _load_manifest(repo, spec) if locked else None
        old_files: dict[str, dict] = {}
        manifest_valid = False
        if old and not full and not unresolved:
            scn = old.get("scanner") or {}
            manifest_valid = (
                scn.get("id") == fp["id"]
                and scn.get("version") == fp["version"]
                and scn.get("script_sha256") == fp["script_sha256"]
                and scn.get("engine_sha256") == fp["engine_sha256"]
                and scn.get("python") == fp["python"]
                and scn.get("args_sha256") == fp["args_sha256"]
                and scn.get("config_sha256") == fp["config_sha256"]
                and scn.get("graph_version") == fp["graph_version"]
            )
            if manifest_valid:
                old_files = old.get("files") or {}
            else:
                reasons.append("fingerprint_or_schema_diverged:full_rebuild")
        elif full:
            reasons.append("forced_full")

        # 4-5. changed + dirty = reverse_closure(changed) com arestas antigas∪novas.
        removed = sorted(set(old_files) - set(files_now))
        changed = sorted(
            rel for rel in files_now
            if rel not in old_files
            or old_files[rel].get("sha256") != files_now[rel]["sha256"]
            or old_files[rel].get("status") not in ("scanned", "skipped_oversize")
        )
        merged: dict[str, set[str]] = {}
        for src, deps in direct_now.items():
            merged.setdefault(src, set()).update(deps)
        for rel, ent in old_files.items():
            for dep in ent.get("dependencies") or ():
                merged.setdefault(rel, set()).add(dep)
        dirty = _reverse_closure(merged, set(changed) | set(removed))
        dirty_existing = sorted(r for r in dirty if r in files_now)
        reused = sorted(r for r in files_now if r not in dirty)

        # 6-7. Scan dos dirty existentes + checagens globais em toda rodada.
        new_entries: dict[str, dict] = {}
        failed = False
        for rel in dirty_existing:
            out = _scan_one(repo, spec, repo / rel, rel, mod)
            if out is None:
                reasons.append(f"scan_error:{rel}")
                failed = True
                break
            status, sc_count, findings = out
            deps = sorted(direct_now.get(rel, ()))
            new_entries[rel] = {
                **files_now[rel],
                "status": status,
                "scanned_count": sc_count,
                "dependencies": deps,
                "findings": findings,
            }
        if failed:
            # Falha parcial: manifesto anterior intocado, veredito impossível.
            return IncrementalScanResult(
                spec.scanner_id, 2, (),
                tuple(dirty_existing), (), tuple(sorted(dirty)), True,
                tuple(_clean_reasons(reasons)),
            )

        for rel in reused:
            new_entries[rel] = dict(old_files[rel])
            new_entries[rel].update(files_now[rel])

        all_findings: list[dict] = []
        for rel in sorted(new_entries):
            all_findings.extend(new_entries[rel].get("findings") or [])
        all_findings.extend(_global_findings(repo, spec, mod))

        # 8. Escrita atômica única somente em sucesso total.
        if locked:
            manifest = {
                "schema_version": _SCHEMA_VERSION,
                "scanner": fp,
                "files": {rel: new_entries[rel] for rel in sorted(new_entries)},
            }
            _write_manifest_atomic(repo, spec, manifest)

        scanned_now = sorted(rel for rel in dirty_existing
                             if new_entries[rel]["status"] == "scanned"
                             and new_entries[rel].get("scanned_count"))
        total_scanned = scanned_now or reused
        if not new_entries or not total_scanned:
            exit_code = _empty_exit(spec)
        else:
            exit_code = _nonempty_exit(spec, all_findings)
        return IncrementalScanResult(
            spec.scanner_id, exit_code, tuple(all_findings),
            tuple(scanned_now), tuple(reused), tuple(sorted(dirty)),
            full or not manifest_valid, tuple(_clean_reasons(reasons)),
        )
    finally:
        lock.release()


def _reverse_closure(merged: dict[str, set[str]], changed: set[str]) -> set[str]:
    rev: dict[str, set[str]] = {}
    for src, deps in merged.items():
        for dep in deps:
            rev.setdefault(dep, set()).add(src)
    seen = set(changed)
    stack = list(seen)
    while stack:
        node = stack.pop()
        for parent in rev.get(node, ()):
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _clean_reasons(reasons: list[str]) -> list[str]:
    out = []
    for r in reasons:
        if r not in out:
            out.append(r)
    return out
