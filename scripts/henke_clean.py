"""henke_clean.py — faxineiro com quarentena e retenção (Wave 7, R7).

Alvos permitidos: caches dentro do repo (`__pycache__`, `.pytest_cache`,
`.mypy_cache`, `.henke/cache`) e workspaces efêmeros expirados com marker e
registro Henke válidos. Nada de padrões globais em %TEMP%, home, `dist`,
`build`, `node_modules` ou dados de conversas.

Quarentena: `<state_dir>/quarantine/<batch-id>/` com `manifest.json` e payload.
Cada item registra origem canônica, raiz permitida, hash agregado e expiração.
Restore exige destino ausente, contenção e hash igual. Purga exige expiração,
ID exato e confirmação humana — nunca automática.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Sequence

from scripts.ephemeral import _MARKER_NAME, _parse_iso, _utcnow, _iso, _validate_marker
from scripts.state_paths import state_dir
import scripts.winpath as winpath

_SCHEMA_VERSION = 1
_CACHE_NAMES = ("__pycache__", ".pytest_cache", ".mypy_cache")
_SKIP_DIRS = {".git", "node_modules", ".henke"}


@dataclass(frozen=True)
class CleanCandidate:
    source: Path
    kind: Literal["repo-cache", "ephemeral-expired"]
    size_bytes: int


@dataclass(frozen=True)
class QuarantineBatch:
    batch_id: str
    created_at: str
    expires_at: str
    items: tuple[dict[str, object], ...]


def _quarantine_root(repo_root: Path) -> Path:
    return state_dir(Path(repo_root)) / "quarantine"


def _log(repo_root: Path, action: str, **fields: object) -> None:
    try:
        log = state_dir(Path(repo_root)) / "clean_log.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": _iso(_utcnow()), "action": action, **fields}
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _tree_size(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames]
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return total


def _tree_sha256(root: Path) -> str:
    """Hash agregado determinístico: relpath + sha256 de cada arquivo, ordenado."""
    agg = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            try:
                h = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                h = "UNREADABLE"
            rel = p.relative_to(root).as_posix()
            entries.append((rel, h))
    for rel, h in sorted(entries):
        agg.update(rel.encode("utf-8") + b"\x00" + h.encode("ascii") + b"\x00")
    return agg.hexdigest()


def _allowed_root(repo_root: Path, candidate: CleanCandidate) -> Path | None:
    """Raiz permitida do candidato; None se a origem não for elegível."""
    if candidate.kind == "repo-cache":
        repo = Path(repo_root).resolve()
        src = candidate.source.resolve()
        if src == repo or not winpath.is_within(repo, src):
            return None
        return repo
    if candidate.kind == "ephemeral-expired":
        src = candidate.source.resolve()
        if _validate_marker(src / _MARKER_NAME, src, repo_root=repo_root) is None:
            return None
        return src
    return None


def _no_reparse(path: Path) -> bool:
    """Nenhum ancestral até a raiz nem o próprio caminho pode ser reparse/symlink."""
    p = Path(path)
    for anc in [p, *p.parents]:
        try:
            if anc.is_symlink():
                return False
            st = anc.stat(follow_symlinks=False)
            if os.name == "nt" and (st.st_file_attributes & 0x400):  # REPARSE_POINT
                return False
        except OSError:
            return False
        if anc.parent == anc:
            break
    return True


def _iter_repo_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        yield Path(dirpath), dirnames, filenames


def _expired_ephemeral_candidates(repo_root: Path, now: datetime) -> list[CleanCandidate]:
    tmp = Path(tempfile.gettempdir())
    found: list[CleanCandidate] = []
    try:
        entries = list(tmp.iterdir())
    except OSError:
        return found
    for entry in sorted(entries, key=lambda e: e.name):
        if not entry.is_dir() or not entry.name.startswith("henke-eph-"):
            continue
        data = _validate_marker(entry / _MARKER_NAME, entry, repo_root=repo_root)
        if data is None:
            continue
        try:
            if now < _parse_iso(data["expires_at"]):
                continue
        except (KeyError, ValueError):
            continue
        found.append(CleanCandidate(
            source=entry, kind="ephemeral-expired", size_bytes=_tree_size(entry),
        ))
    return found


def plan_clean(repo_root: Path, *, now: datetime | None = None) -> tuple[CleanCandidate, ...]:
    """Planeja candidatos; dry-run puro, sem mutações."""
    repo = Path(repo_root).resolve()
    now = now or _utcnow()
    candidates: list[CleanCandidate] = []

    for dirpath, dirnames, _files in _iter_repo_files(repo):
        for d in list(dirnames):
            if d in _CACHE_NAMES:
                src = dirpath / d
                candidates.append(CleanCandidate(
                    source=src, kind="repo-cache", size_bytes=_tree_size(src),
                ))
                dirnames.remove(d)  # não desce dentro do cache

    henke_cache = repo / ".henke" / "cache"
    if henke_cache.is_dir():
        candidates.append(CleanCandidate(
            source=henke_cache, kind="repo-cache", size_bytes=_tree_size(henke_cache),
        ))

    candidates.extend(_expired_ephemeral_candidates(repo, now))
    return tuple(candidates)


def staging_leftovers(repo_root: Path) -> tuple[Path, ...]:
    """Staging remanescente de crash: reportado, nunca purgado automaticamente."""
    qroot = _quarantine_root(repo_root)
    leftovers: list[Path] = []
    if not qroot.is_dir():
        return ()
    for batch_dir in sorted(qroot.iterdir()):
        st = batch_dir / "staging"
        if st.is_dir():
            leftovers.extend(sorted(st.iterdir()))
    return tuple(leftovers)


def _validate_batch_dir(qroot: Path, batch_id: str) -> Path:
    """batch_id deve ser um único componente seguro contido em `qroot`.

    `assert_safe_relative` rejeita absoluto, `..`, drive/ADS, nomes
    reservados Win32 e caracteres de controle; `canonical_path` garante a
    contenção lexical final dentro da raiz de quarentena.
    """
    if not batch_id:
        raise ValueError(f"batch_id inválido: {batch_id!r}")
    rel = winpath.assert_safe_relative(batch_id)
    if "/" in rel:
        raise ValueError(f"batch_id inválido: {batch_id!r}")
    qroot_c = winpath.canonical_path(qroot)
    batch_dir = winpath.canonical_path(rel, base=qroot_c)
    if batch_dir.parent != qroot_c or not winpath.is_within(qroot_c, batch_dir):
        raise ValueError(f"batch_id escapa da quarentena: {batch_id!r}")
    return batch_dir


def quarantine(repo_root: Path, candidates: Sequence[CleanCandidate],
               *, retention_days: int = 7) -> QuarantineBatch:
    """Move candidatos para quarentena com manifesto transacional.

    Falha parcial mantém manifesto apenas dos itens concluídos; nenhum item é
    dado como movido antes da verificação.
    """
    if retention_days < 1:
        raise ValueError("retention_days deve ser >= 1")
    repo = Path(repo_root).resolve()
    qroot = _quarantine_root(repo)
    batch_id = _uuid.uuid4().hex[:16]
    batch_dir = qroot / batch_id
    payload = batch_dir / "payload"
    staging = batch_dir / "staging"
    created = _utcnow()
    expires = created + timedelta(days=retention_days)
    items: list[dict[str, object]] = []

    payload.mkdir(parents=True, exist_ok=False)
    staging.mkdir(exist_ok=True)

    for idx, cand in enumerate(candidates):
        src = cand.source.resolve()
        root = _allowed_root(repo, cand)
        try:
            if root is None or not src.exists() or not _no_reparse(src):
                raise ValueError("origem fora da raiz permitida ou reparse point")
            rel = src.name if cand.kind == "ephemeral-expired" else src.relative_to(root).as_posix()
            tree_hash = _tree_sha256(src)
            dest = payload / f"{idx:04d}-{src.name}"
            staged = staging / dest.name
            try:
                os.replace(src, staged)
                os.replace(staged, dest)
            except OSError:
                # Se a 2ª perna falhou com o item já só em staging, restaura
                # staged -> src ANTES de qualquer remoção (nunca perde dados).
                if staged.exists() and not src.exists():
                    os.replace(staged, src)
                if staged.exists():
                    shutil.rmtree(staged, ignore_errors=True)
                if not src.exists():
                    raise  # origem perdida sem cópia local: falha explícita
                # Entre unidades: copia para staging, verifica, renomeia, remove origem.
                shutil.copytree(src, staged)
                if _tree_sha256(staged) != tree_hash:
                    raise ValueError("hash divergente após cópia cross-volume")
                if dest.exists():
                    shutil.rmtree(dest)  # falha propaga: nunca esconde dest estranho
                os.replace(staged, dest)
                shutil.rmtree(src, ignore_errors=True)
            if _tree_sha256(dest) != tree_hash:
                raise ValueError("hash divergente após movimentação")
            items.append({
                "original_path": str(src),
                "allowed_root": "repo" if cand.kind == "repo-cache" else "ephemeral",
                "relative": rel,
                "dest": dest.name,
                "kind": cand.kind,
                "size_bytes": cand.size_bytes,
                "sha256": tree_hash,
                "created_at": _iso(created),
                "expires_at": _iso(expires),
            })
            _log(repo, "quarantine_item", batch=batch_id, item=str(src), hash=tree_hash, result="moved")
        except Exception as exc:
            _log(repo, "quarantine_item", batch=batch_id, item=str(src), hash="", result=f"failed:{exc}")

    if staging.is_dir() and not any(staging.iterdir()):
        staging.rmdir()
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "batch_id": batch_id,
        "created_at": _iso(created),
        "expires_at": _iso(expires),
        "retention_days": retention_days,
        "items": items,
    }
    (batch_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _log(repo, "quarantine_batch", batch=batch_id, item=str(len(items)), hash="", result="created")
    return QuarantineBatch(batch_id, _iso(created), _iso(expires), tuple(items))


def _load_manifest(batch_dir: Path) -> dict:
    manifest_path = batch_dir / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("manifesto de quarentena inválido ou de schema desconhecido")
    return data


def restore_batch(repo_root: Path, batch_id: str) -> int:
    """Restaura itens do batch à origem; exige destino ausente e hash fiel."""
    repo = Path(repo_root).resolve()
    qroot = _quarantine_root(repo)
    batch_dir = _validate_batch_dir(qroot, batch_id)
    manifest = _load_manifest(batch_dir)
    restored = 0
    for item in manifest["items"]:
        dest = batch_dir / "payload" / item["dest"]
        original = Path(item["original_path"])
        if item["allowed_root"] == "repo":
            root = repo
        else:
            root = original  # efêmero: a própria workspace era a raiz
        if original.exists():
            _log(repo, "restore_item", batch=batch_id, item=item["original_path"],
                 hash=item["sha256"], result="dest_exists")
            continue
        resolved = original.resolve().parent
        if item["allowed_root"] == "repo":
            contained = winpath.is_within(repo, resolved)
        else:
            # efêmero: a workspace sempre vive diretamente sob o temp do sistema
            contained = resolved == Path(tempfile.gettempdir()).resolve()
        if not contained:
            _log(repo, "restore_item", batch=batch_id, item=item["original_path"],
                 hash=item["sha256"], result="outside_root")
            continue
        if _tree_sha256(dest) != item["sha256"]:
            _log(repo, "restore_item", batch=batch_id, item=item["original_path"],
                 hash=item["sha256"], result="hash_mismatch")
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(dest, original)
        except OSError:
            shutil.copytree(dest, original)
            if _tree_sha256(original) != item["sha256"]:
                shutil.rmtree(original, ignore_errors=True)
                _log(repo, "restore_item", batch=batch_id, item=item["original_path"],
                     hash=item["sha256"], result="copy_verify_failed")
                continue
            shutil.rmtree(dest, ignore_errors=True)
        restored += 1
        _log(repo, "restore_item", batch=batch_id, item=item["original_path"],
             hash=item["sha256"], result="restored")
    _log(repo, "restore_batch", batch=batch_id, item=str(restored), hash="", result="done")
    return restored


def purge_expired_batch(repo_root: Path, batch_id: str, *, now: datetime) -> int:
    """Purga um único batch expirado. Exige `now >= expires_at` e ID exato.

    Sem `--all`, sem purga por idade de arquivo, sem automação em check/watch/
    hook/session/atexit.
    """
    repo = Path(repo_root).resolve()
    qroot = _quarantine_root(repo)
    batch_dir = _validate_batch_dir(qroot, batch_id)
    if not batch_dir.is_dir():
        raise ValueError(f"batch inexistente: {batch_id}")
    manifest = _load_manifest(batch_dir)
    if now < _parse_iso(manifest["expires_at"]):
        raise ValueError(f"batch {batch_id} ainda não expirou (expires_at={manifest['expires_at']})")
    shutil.rmtree(batch_dir)
    _log(repo, "purge_batch", batch=batch_id, item="", hash="", result="purged")
    return 1
