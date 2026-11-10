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


class _HashMismatchError(ValueError):
    """Divergência de hash pós-movimentação — log result='hash_mismatch'."""


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
        # PT-02: poda junction/reparse descendente — sem isso o tamanho
        # incluía a árvore EXTERNA do alvo dentro de um candidato.
        dirnames[:] = [
            d for d in dirnames if not _is_reparse_dir(Path(dirpath) / d)
        ]
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                pass
    return total


def _tree_sha256(root: Path) -> str:
    """Hash agregado determinístico: relpath + sha256 de cada arquivo, ordenado."""
    # Um arquivo isolado não é árvore: os.walk devolve zero entradas e o
    # hash da "árvore vazia" colidiria para QUALQUER arquivo — um manifesto
    # forjado com sha256(vazio) validava qualquer payload externo (DS-1).
    root = Path(root)
    if root.is_file():
        return hashlib.sha256(b"file\x00" + root.read_bytes()).hexdigest()
    agg = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # PT-02: poda junction/reparse descendente — um `pkg/__pycache__/leak
        # -> C:\segredos` fazia o hash engolir a árvore EXTERNA do alvo e o
        # copytree de fallback materializava segredo dentro da quarentena.
        dirnames[:] = sorted(
            d for d in dirnames
            if not _is_reparse_dir(Path(dirpath) / d))
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


def _is_reparse_dir(p: Path) -> bool:
    """Junction/symlink/reparse em UM caminho — portável py3.11+
    (``Path.is_junction`` só existe em 3.12+). Em dúvida, True (fail-closed):
    diretório que não conseguimos classificar não é atravessado."""
    try:
        if p.is_symlink():
            return True
        st = p.lstat()
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _has_reparse_descendant(root: Path) -> bool:
    """PT-02: True se QUALQUER descendente (dir ou arquivo) for reparse/symlink.

    os.walk(followlinks=False) não desce em symlinks de diretório, mas
    junctions Windows NÃO são ``is_symlink`` — sem o check por
    ``st_file_attributes`` o walk desceria a árvore externa. O retorno
    antecipado sai antes do próximo yield: o alvo nunca é atravessado.
    """
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames:
            if _is_reparse_dir(Path(dirpath) / name):
                return True
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.is_symlink():
                    return True
                st = p.lstat()
                if getattr(st, "st_file_attributes", 0) & 0x400:
                    return True
            except OSError:
                return True  # ilegível: em dúvida, recusa o candidato
    return False


def _ignore_reparse(dirpath: str, names: list) -> set:
    """``ignore`` do copytree: descarta todo entry reparse/symlink (PT-02).

    Defesa anti-TOCTOU do fallback cross-volume: se um junction/symlink for
    plantado entre o check e a cópia, ele nunca é materializado na quarentena.
    Entrada ilegível também é descartada — copiar cego seria pior que pular.
    """
    skipped: set = set()
    for name in names:
        p = Path(dirpath) / name
        try:
            if p.is_symlink():
                skipped.add(name)
                continue
            st = p.lstat()
            if getattr(st, "st_file_attributes", 0) & 0x400:
                skipped.add(name)
        except OSError:
            skipped.add(name)
    return skipped


def _iter_repo_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        # Delta-F13: poda junction/reparse — sem isso o plano descia a
        # árvore externa do alvo (IO ilimitado + hash de conteúdo fora do
        # repo no _tree_sha256).
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS
            and _no_reparse(Path(dirpath) / d))
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


def _manifest_hmac(repo_root: Path, manifest: dict) -> str | None:
    """HMAC-SHA256 do manifesto de quarentena com a chave do perfil —
    Banca-A 3: manifesto unsigned em .henke/ era forjável (dest+sha256
    arbitrários → restore exfiltrava arquivo externo via junction)."""
    import hashlib
    import hmac as _hmac
    from scripts.metricas.scan_manifest import _manifest_secret
    secret = _manifest_secret(Path(repo_root))
    if secret is None:
        return None
    body = {k: v for k, v in manifest.items() if k != "hmac"}
    canon = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    # Delta-F14: separador de domínio — a chave é compartilhada com o
    # scan-manifest; um HMAC de um protocolo nunca valida no outro.
    return _hmac.new(secret, b"henke-quarantine-v1\x00" + canon,
                     hashlib.sha256).hexdigest()


def _quarantine_tree_safe(repo: Path, qroot: Path, batch_dir: Path) -> None:
    """Recusa operar sob junction/reparse: qroot, batch_dir e payload
    precisam ser diretórios reais — senão restore/purge age FORA do repo."""
    qroot_c = qroot.resolve()
    for cand in (qroot, batch_dir, batch_dir / "payload"):
        if cand.is_symlink() or (cand.exists() and not _no_reparse(cand)):
            raise ValueError(
                f"reparse point na árvore de quarentena: {cand} — "
                "operação recusada (possível redirecionamento)")
    if not winpath.is_within(qroot_c, batch_dir.resolve()):
        raise ValueError("batch_dir resolve fora da raiz de quarentena")


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


def _move_transacted(src: Path, staged: Path, dest: Path,
                     tree_hash: str) -> None:
    """Move src -> staging -> payload sem janela de perda.

    Falha na 2ª perna restaura staged -> src quando seguro; se a origem
    reapareceu (corrida) ou a restauração falhou, dá raise preservando
    staged e src — o staged nunca é apagado (pode ser a única cópia).
    """
    try:
        os.replace(src, staged)
        os.replace(staged, dest)
    except OSError:
        # Se a 2ª perna falhou com o item já só em staging, restaura
        # staged -> src ANTES de qualquer remoção (nunca perde dados).
        if staged.exists() and not src.exists():
            os.replace(staged, src)
        if staged.exists():
            # Origem já existe (corrida) ou restauração impossível:
            # nunca apaga staged — falha explícita preserva os dois.
            raise
        if not src.exists():
            raise  # origem perdida sem cópia local: falha explícita
        # Entre unidades: copia para staging, verifica, renomeia, remove origem.
        shutil.copytree(src, staged, ignore=_ignore_reparse,
                        ignore_dangling_symlinks=True)
        if _tree_sha256(staged) != tree_hash:
            raise _HashMismatchError("hash divergente após cópia cross-volume")
        if dest.exists():
            shutil.rmtree(dest)  # falha propaga: nunca esconde dest estranho
        os.replace(staged, dest)
        shutil.rmtree(src, ignore_errors=True)
    if _tree_sha256(dest) != tree_hash:
        raise _HashMismatchError("hash divergente após movimentação")


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
        # DS-3: checar reparse ANTES do resolve — um junction `pkg/__pycache__
        # -> src` resolvia para o ALVO e o `_no_reparse` do alvo passava:
        # a limpeza movia src/ inteiro para a quarentena.
        if not _no_reparse(cand.source):
            _log(repo, "quarantine_item", batch=batch_id, item=str(cand.source),
                 hash="", result="reparse_skip")
            continue
        # PT-02: junction/symlink DENTRO do candidato aponta para fora da
        # árvore — o hash incluiria conteúdo externo e o copytree de fallback
        # materializaria segredo na quarentena (e de volta no restore).
        # Candidato com reparse descendente é recusado inteiro (fail-closed).
        if _has_reparse_descendant(cand.source):
            _log(repo, "quarantine_item", batch=batch_id, item=str(cand.source),
                 hash="", result="reparse_descendant")
            continue
        src = cand.source.resolve()
        dest = payload / f"{idx:04d}-{src.name}"
        root = _allowed_root(repo, cand)
        rel = ""
        tree_hash = ""
        try:
            if root is None or not src.exists() or not _no_reparse(src):
                raise ValueError("origem fora da raiz permitida ou reparse point")
            rel = src.name if cand.kind == "ephemeral-expired" else src.relative_to(root).as_posix()
            tree_hash = _tree_sha256(src)
            staged = staging / dest.name
            _move_transacted(src, staged, dest, tree_hash)
            items.append({
                "original_path": str(src),
                "allowed_root": "repo" if cand.kind == "repo-cache" else "ephemeral",
                "relative": rel,
                "dest": dest.name,
                "kind": cand.kind,
                "size_bytes": cand.size_bytes,
                "sha256": tree_hash,
                "verified": True,
                "created_at": _iso(created),
                "expires_at": _iso(expires),
            })
            _log(repo, "quarantine_item", batch=batch_id, item=str(src), hash=tree_hash, result="moved")
        except Exception as exc:
            # Divergência de hash com o item já no payload: o conteúdo
            # movido existe e precisa ficar rastreado — registra com
            # verified=False e o hash REAL do que está em quarentena
            # (nunca o hash esperado, que corresponderia a outro conteúdo).
            if isinstance(exc, _HashMismatchError) and dest.exists():
                items.append({
                    "original_path": str(src),
                    "allowed_root": "repo" if cand.kind == "repo-cache" else "ephemeral",
                    "relative": rel,
                    "dest": dest.name,
                    "kind": cand.kind,
                    "size_bytes": cand.size_bytes,
                    "sha256": _tree_sha256(dest),
                    "expected_sha256": tree_hash,
                    "verified": False,
                    "created_at": _iso(created),
                    "expires_at": _iso(expires),
                })
            _log(repo, "quarantine_item", batch=batch_id, item=str(src), hash="",
                 result="hash_mismatch" if isinstance(exc, _HashMismatchError)
                        else f"failed:{exc}")

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
    manifest["hmac"] = _manifest_hmac(repo, manifest)
    (batch_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    _log(repo, "quarantine_batch", batch=batch_id, item=str(len(items)), hash="", result="created")
    return QuarantineBatch(batch_id, _iso(created), _iso(expires), tuple(items))


def _load_manifest(repo: Path, batch_dir: Path) -> dict:
    manifest_path = batch_dir / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError("manifesto de quarentena inválido ou de schema desconhecido")
    import hmac as _hmac
    expected = _manifest_hmac(repo, data)
    have = data.get("hmac")
    if expected is None or not isinstance(have, str) or             not _hmac.compare_digest(expected, have):
        raise ValueError(
            "manifesto de quarentena sem assinatura HMAC válida — "
            "recusado (fail-closed): restore/purge só operam manifestos "
            "gerados por esta instalação")
    return data


def restore_batch(repo_root: Path, batch_id: str) -> int:
    """Restaura itens do batch à origem; exige destino ausente e hash fiel."""
    repo = Path(repo_root).resolve()
    qroot = _quarantine_root(repo)
    batch_dir = _validate_batch_dir(qroot, batch_id)
    _quarantine_tree_safe(repo, qroot, batch_dir)
    manifest = _load_manifest(repo, batch_dir)
    restored = 0
    qroot_c = qroot.resolve()
    payload_root = (batch_dir / "payload").resolve()
    for item in manifest["items"]:
        # DS-1: `dest` vem do manifesto — caminho absoluto ou `..` faria o
        # restore mover um arquivo de FORA da quarentena para dentro do repo.
        # Aceita só nome relativo simples confinado ao payload/.
        raw_dest = item.get("dest")
        if not isinstance(raw_dest, str) or not raw_dest:
            _log(repo, "restore_item", batch=batch_id,
                 item=str(item.get("original_path")), hash="",
                 result="bad_dest")
            continue
        dest = (payload_root / raw_dest).resolve()
        # Banca-A 3: contenção dupla — payload_root JÁ RESOLVIDO pode estar
        # fora do qroot (junction); exige dentro de ambos.
        if not winpath.is_within(qroot_c, dest) or                 not winpath.is_within(payload_root, dest):
            _log(repo, "restore_item", batch=batch_id,
                 item=str(item.get("original_path")), hash="",
                 result="dest_escapes_payload")
            continue
        orig_raw = item.get("original_path")
        if not isinstance(orig_raw, str) or not orig_raw:
            _log(repo, "restore_item", batch=batch_id,
                 item="", hash="", result="bad_original")
            continue
        original = Path(orig_raw)
        if item.get("verified") is False:
            _log(repo, "restore_item", batch=batch_id, item=orig_raw,
                 hash=item.get("sha256", ""), result="unverified_skip")
            continue
        if original.exists():
            _log(repo, "restore_item", batch=batch_id, item=orig_raw,
                 hash=item.get("sha256", ""), result="dest_exists")
            continue
        resolved = original.resolve().parent
        if item["allowed_root"] == "repo":
            contained = winpath.is_within(repo, resolved)
        else:
            # efêmero: a workspace sempre vive diretamente sob o temp do sistema
            contained = resolved == Path(tempfile.gettempdir()).resolve()
        if not contained:
            _log(repo, "restore_item", batch=batch_id, item=orig_raw,
                 hash=item.get("sha256", ""), result="outside_root")
            continue
        if _tree_sha256(dest) != item.get("sha256"):
            _log(repo, "restore_item", batch=batch_id, item=orig_raw,
                 hash=item.get("sha256", ""), result="hash_mismatch")
            continue
        original.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(dest, original)
        except OSError:
            # dest pode ser arquivo único — copytree falharia com traceback.
            try:
                if dest.is_dir():
                    shutil.copytree(dest, original, ignore=_ignore_reparse,
                                    ignore_dangling_symlinks=True)
                else:
                    shutil.copy2(dest, original)
                copy_ok = _tree_sha256(original) == item.get("sha256")
            except OSError:
                copy_ok = False
            if not copy_ok:
                if original.is_dir():
                    shutil.rmtree(original, ignore_errors=True)
                else:
                    original.unlink(missing_ok=True)
                _log(repo, "restore_item", batch=batch_id, item=orig_raw,
                     hash=item.get("sha256", ""), result="copy_verify_failed")
                continue
            if dest.is_dir():
                shutil.rmtree(dest, ignore_errors=True)
            else:
                dest.unlink(missing_ok=True)
        restored += 1
        _log(repo, "restore_item", batch=batch_id, item=orig_raw,
             hash=item.get("sha256", ""), result="restored")
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
    _quarantine_tree_safe(repo, qroot, batch_dir)
    manifest = _load_manifest(repo, batch_dir)
    if now < _parse_iso(manifest["expires_at"]):
        raise ValueError(f"batch {batch_id} ainda não expirou (expires_at={manifest['expires_at']})")
    # DS-2: item não verificado nunca é restaurado, mas entrava no manifesto
    # e a purga apagava a ÚNICA cópia. Recusa até resolução manual.
    unverified = sorted(str(i.get("original_path")) for i in manifest["items"]
                        if i.get("verified") is False)
    if unverified:
        raise ValueError(
            f"batch {batch_id} possui {len(unverified)} item(ns) não verificados "
            "— a purga apagaria a única cópia; resolva manualmente")
    staging_dir = batch_dir / "staging"
    if staging_dir.is_dir() and any(staging_dir.iterdir()):
        raise ValueError(
            f"batch {batch_id} possui staging não vazio; purga recusada "
            "até o leftover ser resolvido manualmente")
    # Todo payload precisa estar registrado no manifesto — um diretório
    # estranho não rastreado nunca é apagado em massa.
    payload_dir = batch_dir / "payload"
    registered = {item.get("dest") for item in manifest["items"]}
    if payload_dir.is_dir():
        strays = sorted(e.name for e in payload_dir.iterdir()
                        if e.name not in registered)
        if strays:
            raise ValueError(
                f"batch {batch_id} possui payload não registrado no manifesto "
                f"({', '.join(strays)}); purga recusada")
    shutil.rmtree(batch_dir)
    _log(repo, "purge_batch", batch=batch_id, item="", hash="", result="purged")
    return 1
