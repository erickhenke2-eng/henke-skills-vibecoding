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

Anti-TOCTOU: após cada scan o arquivo é re-hasheado e comparado ao snapshot
(sha256, size, mtime_ns). Divergência = corrida → a entrada é descartada e a
rodada falha fechada, sem gravar par (hash, findings) incoerente no manifesto.

Todo manifesto gravado carrega assinatura HMAC-SHA256 na raiz, calculada com a
mesma chave do gate cache — armazenada no perfil do usuário, FORA da árvore
auditada (DS-5): payload adulterado, HMAC ausente ou assinado com chave
plantada no repo → manifesto descartado e rebuild integral (`hmac_invalid`),
nunca findings zerados aceitos.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Literal

from scripts.state_paths import sanitized_child_env, state_dir
import scripts.winpath as winpath

_SCHEMA_VERSION = 1
_GRAPH_VERSION = 1

_SNAPSHOT_UNSTABLE = "SNAPSHOT_UNSTABLE"

# Raiz canônica da própria suíte (como SUITE_ROOT/GATE_MAP em henke_cli.py).
# Scanners, motor e configs SEMPRE resolvem daqui — jamais do repo auditado:
# `<repo>/scripts/metricas/*.py` não é código confiável e em repos externos
# nem sequer existe.
_SUITE_ROOT = Path(__file__).resolve().parents[2]


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


def _engine_sha256(spec: ScannerSpec) -> str:
    """Motor + helpers + scanner da suíte, em ordem de caminho POSIX."""
    parts = [
        "scripts/metricas/scan_manifest.py",
        "scripts/import_graph.py",
        "scripts/repo_signals.py",
        "scripts/metricas/polyglot.py",
        "scripts/winpath.py",
    ]
    try:
        rel_script = Path(spec.script_path).resolve().relative_to(_SUITE_ROOT).as_posix()
    except ValueError:
        rel_script = Path(spec.script_path).resolve().as_posix()
    parts.append(rel_script)
    agg = hashlib.sha256()
    for rel in sorted(parts):
        p = _SUITE_ROOT / rel
        try:
            agg.update(rel.encode("utf-8") + b"\x00" + _sha256_text(p).encode("ascii") + b"\x00")
        except OSError:
            agg.update(rel.encode("utf-8") + b"\x00MISSING\x00")
    return agg.hexdigest()


def _config_sha256(spec: ScannerSpec) -> str:
    """Configs declaradas pelo adapter — arquivos da suíte, não do repo alvo."""
    agg = hashlib.sha256()
    for rel in sorted(spec.config_paths):
        p = _SUITE_ROOT / rel
        try:
            data = p.read_bytes()
        except OSError:
            data = b""
        agg.update(rel.encode("utf-8") + b"\x00" + data + b"\x00")
    return agg.hexdigest()


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _metricas_dir() -> Path:
    """Diretório canônico dos scanners da suíte — nunca derivado do repo alvo."""
    return _SUITE_ROOT / "scripts" / "metricas"


def _load_scanner_module(spec: ScannerSpec):
    """Importa o módulo do scanner por caminho (stdlib only).

    `spec.script_path` aponta para a suíte quando vem de `scanner_specs`;
    specs injetadas por teste também são aceitas (o caminho é explícito).
    """
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


def scanner_specs() -> tuple[ScannerSpec, ...]:
    """Os três scanners com adapter nesta Wave — resolvidos na suíte Henke.

    NENHUM código executável vem do repo alvo: em repos externos
    `<repo>/scripts/metricas/` não existe e, se existisse, seria conteúdo
    auditado — não ferramenta.
    """
    mdir = _metricas_dir()
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


def _scan_one(repo_root: Path, spec: ScannerSpec, path: Path, rel: str, mod,
              *, content: str | None = None):
    """(status, findings) ou None em erro de leitura/parse (nunca findings vazios por erro).

    `content` (a11y) permite escanear exatamente os bytes já lidos pelo
    chamador — o par (hash, findings) gravado no manifesto corresponde ao
    mesmo conteúdo, sem re-leitura do disco dentro do scanner.
    """
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
        if content is None:
            fnd = mod.scan_a11y_file(path, root=Path(repo_root).resolve())
        else:
            fnd = mod.scan_a11y_content(content, rel)
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
    # DS-4: a11y sem arquivo de UI era PASS (0) — um repo só-backend ou com a
    # UI escondida atrás de junction passava no `henke check` sem a11y. O
    # protocolo exige n/a (3): veredito honesto, nunca verde por tautologia.
    return {"hazard": 2, "assertless": 3, "a11y": 3}[spec.scanner_id]


def _nonempty_exit(spec: ScannerSpec, findings: list[dict]) -> int:
    return 1 if findings else 0


# ---------------------------------------------------------------------------
# Núcleo transacional
# ---------------------------------------------------------------------------


def _manifest_dir(repo_root: Path) -> Path:
    return state_dir(Path(repo_root)) / "scan-manifests"


def _manifest_path(repo_root: Path, spec: ScannerSpec) -> Path:
    return _manifest_dir(repo_root) / f"{spec.scanner_id}.json"


# ---------------------------------------------------------------------------
# Assinatura HMAC-SHA256 do manifesto (anti-forja, mesma chave do gate cache)
# ---------------------------------------------------------------------------


def _secret_is_tracked(repo_root: Path, secret_file: Path) -> bool:
    """True se a chave HMAC está versionada — segredo público não assina nada."""
    try:
        rel = secret_file.resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except (ValueError, OSError):
        return False  # state_dir fora do repo (HENKE_STATE_DIR) não é rastreável
    try:
        res = subprocess.run(
            ["git", "-C", str(Path(repo_root).resolve()),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "ls-files", "--error-unmatch", "--", rel],
            capture_output=True, timeout=10,
            env=sanitized_child_env(repo_root))
    except (OSError, subprocess.SubprocessError):
        return False  # sem git, o arquivo não pode estar no índice
    return res.returncode == 0


def _manifest_secret(repo_root: Path) -> bytes | None:
    """Chave HMAC compartilhada com o gate cache; None se comprometida."""
    from scripts.gate_cache import _get_cache_secret
    from scripts.state_paths import secret_dir, secret_key_name
    # DS-5: a chave mora no perfil do usuário (fora da árvore auditada); o
    # check de "rastreada" ainda vale para overrides apontados ao repo.
    secret_file = secret_dir(Path(repo_root)) / secret_key_name(Path(repo_root))
    if _secret_is_tracked(repo_root, secret_file):
        return None
    return _get_cache_secret(Path(repo_root))


def _manifest_hmac(secret: bytes, manifest: dict) -> str:
    """HMAC-SHA256 sobre o payload canônico (tudo menos o próprio campo)."""
    clean = {k: v for k, v in manifest.items() if k != "hmac"}
    canonical = json.dumps(clean, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(secret, b"henke-scan-manifest-v1\x00" + canonical,
                    hashlib.sha256).hexdigest()


def _manifest_hmac_ok(secret: bytes | None, manifest: dict) -> bool:
    if secret is None:
        return False
    stored = manifest.get("hmac")
    if not isinstance(stored, str) or not stored.isascii():
        return False
    return hmac.compare_digest(stored, _manifest_hmac(secret, manifest))


def _load_manifest(repo_root: Path, spec: ScannerSpec,
                   secret: bytes | None) -> tuple[dict | None, str | None]:
    """(manifesto, rejeição). HMAC ausente/adulterado → 'hmac_invalid';
    arquivo ilegível/corrompido → 'corrupted_file' (ambos → full rebuild)."""
    path = _manifest_path(repo_root, spec)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, TypeError, UnicodeDecodeError, RecursionError):
        return None, "corrupted_file"
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        return None, "corrupted_file"
    if not _manifest_hmac_ok(secret, data):
        return None, "hmac_invalid"
    return data, None


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


def _fingerprint(spec: ScannerSpec, args_sha256: str) -> dict:
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {
        "id": spec.scanner_id,
        "version": spec.scanner_version,
        "script_sha256": _sha256_text(Path(spec.script_path)),
        "engine_sha256": _engine_sha256(spec),
        "python": py,
        "args_sha256": args_sha256,
        "config_sha256": _config_sha256(spec),
        "graph_version": _GRAPH_VERSION,
    }


def _graph_edges(repo_root: Path) -> dict[str, frozenset[str]]:
    try:
        from scripts.import_graph import build_import_graph
        g = build_import_graph(repo_root)
        return dict(g.direct), g.unresolved
    except Exception:
        return {}, {"__engine__": ("graph_build_failed",)}


def _manifest_fingerprint_ok(old: dict, fp: dict) -> bool:
    """Manifesto reusável somente se o fingerprint bater em todos os eixos."""
    scn = old.get("scanner") or {}
    return (
        scn.get("id") == fp["id"]
        and scn.get("version") == fp["version"]
        and scn.get("script_sha256") == fp["script_sha256"]
        and scn.get("engine_sha256") == fp["engine_sha256"]
        and scn.get("python") == fp["python"]
        and scn.get("args_sha256") == fp["args_sha256"]
        and scn.get("config_sha256") == fp["config_sha256"]
        and scn.get("graph_version") == fp["graph_version"]
    )


def _write_signed_manifest(repo_root: Path, spec: ScannerSpec,
                           secret: bytes | None, fp: dict,
                           new_entries: dict[str, dict],
                           reasons: list[str]) -> None:
    """Persiste manifesto assinado; sem chave confiável nada é gravado."""
    if secret is None:
        reasons.append("hmac_secret_compromised:manifest_write_skipped")
        return
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "scanner": fp,
        "files": {rel: new_entries[rel] for rel in sorted(new_entries)},
    }
    manifest["hmac"] = _manifest_hmac(secret, manifest)
    _write_manifest_atomic(repo_root, spec, manifest)


def run_incremental_scan(repo_root: Path, spec: ScannerSpec, *,
                         full: bool = False) -> IncrementalScanResult:
    """Executa scan incremental transacional para `spec`."""
    repo = Path(repo_root).resolve()
    reasons: list[str] = []
    mod = _load_scanner_module(spec)
    version = str(getattr(mod, "SCANNER_VERSION", spec.scanner_version))
    spec = ScannerSpec(spec.scanner_id, version, spec.script_path, spec.config_paths)
    args_sha256 = hashlib.sha256(f"{spec.scanner_id}|{spec.scanner_version}".encode()).hexdigest()
    fp = _fingerprint(spec, args_sha256)

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

        secret = _manifest_secret(repo)
        old, manifest_reject = (_load_manifest(repo, spec, secret)
                                if locked else (None, None))
        old_files: dict[str, dict] = {}
        manifest_valid = False
        if old and not full and not unresolved:
            manifest_valid = _manifest_fingerprint_ok(old, fp)
            if manifest_valid:
                old_files = old.get("files") or {}
            else:
                reasons.append("fingerprint_or_schema_diverged:full_rebuild")
        elif full:
            reasons.append("forced_full")
        if manifest_reject:
            reasons.append(f"{manifest_reject}:full_rebuild")

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
            fpath = repo / rel
            # Scanners com leitura em memória recebem exatamente os bytes
            # avaliados — o hash re-verificado abaixo fecha a janela TOCTOU.
            content = None
            if spec.scanner_id == "a11y":
                try:
                    content = fpath.read_bytes().decode("utf-8", errors="replace")
                except OSError as exc:
                    reasons.append(f"scan_error:{rel}:{type(exc).__name__}")
                    failed = True
                    break
            out = _scan_one(repo, spec, fpath, rel, mod, content=content)
            if out is None:
                reasons.append(f"scan_error:{rel}")
                failed = True
                break
            # Anti-TOCTOU: se (sha256, size, mtime_ns) divergir do snapshot
            # pré-scan, a entrada casaria hash de uma versão com findings de
            # outra — falso-verde permanente. A entrada é descartada e a
            # rodada falha fechada (manifesto anterior permanece intocado).
            try:
                post = winpath.sha256_file(fpath)
            except (winpath.UnstableFileError, winpath.PathSafetyError,
                    OSError) as exc:
                reasons.append(f"race:{rel}:{type(exc).__name__}")
                failed = True
                break
            snap = files_now[rel]
            if (post.sha256, post.size, post.mtime_ns) != (
                    snap["sha256"], snap["size"], snap["mtime_ns"]):
                reasons.append(f"race:{rel}")
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

        # 8. Escrita atômica única somente em sucesso total — e somente
        # assinada: sem chave confiável o manifesto não é persistido.
        if locked:
            _write_signed_manifest(repo, spec, secret, fp, new_entries,
                                   reasons)

        scanned_now = sorted(rel for rel in dirty_existing
                             if new_entries[rel]["status"] == "scanned"
                             and new_entries[rel].get("scanned_count"))
        # "Nada auditado" nunca é PASS: a soma real de scanned_count decide,
        # não a presença de arquivos reaproveitados (2ª rodada com reuse de
        # entradas com scanned_count=0 continuava verde por engano).
        total_scanned = sum(
            int(e.get("scanned_count") or 0) for e in new_entries.values()
        )
        if not new_entries or total_scanned == 0:
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
