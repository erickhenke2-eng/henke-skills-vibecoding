"""ephemeral.py — workspaces efêmeros com TTL e propriedade (Wave 7, R7).

Cada workspace é um diretório único no temp do sistema
(`henke-eph-<session>-<uuid>`) com marker `.henke-ephemeral.json`. Filhos
executados por `ws.run` pertencem ao `ChildSupervisor` da workspace; o Job
Object (Windows) ou process group (POSIX) é a garantia de morte quando os
handles são fechados — inclusive em término abrupto do interpretador.

`threading.Timer` é apenas o gatilho em processo vivo. Diretórios remanescentes
só são removidos por `ephemeral_gc` após expiração e validação do marker.
Nenhum PID é autoridade para limpar ou matar.
"""
from __future__ import annotations

import functools
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, ContextManager, Mapping, Sequence

from henke.process_supervisor import ChildSupervisor, ProcessExecutionResult

_MARKER_NAME = ".henke-ephemeral.json"
_DIR_PREFIX = "henke-eph-"


@dataclass(frozen=True)
class EphemeralConfig:
    ttl_seconds: float = 300.0
    session_id: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in text)[:40] or "anon"


def _validate_marker(marker_path: Path, workspace: Path, *, repo_root: Path | None = None) -> dict | None:
    """Valida marker efêmero; retorna o payload ou None se inválido."""
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("kind") != "henke-ephemeral":
        return None
    ws_id = data.get("workspace_id")
    if not isinstance(ws_id, str) or not ws_id or not workspace.name.endswith(ws_id):
        return None
    if repo_root is not None and data.get("repo") != str(repo_root.resolve()):
        return None
    return data


def _safe_relative(workspace_root: Path, relative: str) -> Path:
    """Resolve caminho relativo exigindo contenção estrita dentro do workspace."""
    rel = Path(relative)
    if rel.is_absolute():
        raise ValueError(f"caminho absoluto fora do workspace: {relative!r}")
    target = (workspace_root / rel).resolve()
    root = workspace_root.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"caminho escapa do workspace: {relative!r}")
    return target


class EphemeralWorkspace:
    """Workspace efêmero supervisionado; `close()` é idempotente."""

    def __init__(self, root: Path, workspace_id: str, config: EphemeralConfig,
                 expires_at: datetime) -> None:
        self._root = root
        self._id = workspace_id
        self._config = config
        self._expires_at = expires_at
        self._supervisor = ChildSupervisor()
        self._closed = False
        self._lock = threading.Lock()
        self._timer = threading.Timer(config.ttl_seconds, self.close)
        self._timer.daemon = True
        self._timer.start()

    @property
    def path(self) -> Path:
        return self._root

    @property
    def expires_at(self) -> datetime:
        return self._expires_at

    def _remaining_ttl(self) -> float:
        return max(0.0, (self._expires_at - _utcnow()).total_seconds())

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("workspace efêmero já fechado")

    def path_for(self, relative: str) -> Path:
        self._require_open()
        return _safe_relative(self._root, relative)

    def write_bytes(self, relative: str, data: bytes) -> Path:
        target = self.path_for(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def write_text(self, relative: str, text: str) -> Path:
        return self.write_bytes(relative, text.encode("utf-8"))

    def run(self, argv: Sequence[str], *, timeout_sec: float | None = None,
            env: Mapping[str, str] | None = None) -> ProcessExecutionResult:
        """Executa filho dentro do workspace sob o supervisor próprio.

        O timeout efetivo é `min(timeout_sec, tempo restante do TTL)`.
        """
        self._require_open()
        remaining = self._remaining_ttl()
        effective = remaining if timeout_sec is None else min(float(timeout_sec), remaining)
        with self._lock:
            if self._closed:
                raise RuntimeError("workspace efêmero já fechado")
            child = self._supervisor.start(
                [str(a) for a in argv],
                cwd=self._root,
                env=env,
                max_output_bytes=1024 * 1024,
            )
        # O wait fica FORA do lock: close() e o timer de TTL precisam conseguir
        # encerrar o supervisor enquanto um run bloqueia aqui.
        return self._supervisor.wait(child, timeout_sec=effective)

    def close(self) -> None:
        """Idempotente: cancela timer, fecha supervisor e remove só o próprio dir."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._timer.cancel()
        except Exception:
            pass
        try:
            self._supervisor.close()
        finally:
            self._remove_owned_dir()

    def _remove_owned_dir(self) -> None:
        """Remove o diretório apenas se o marker confirmar propriedade."""
        marker = self._root / _MARKER_NAME
        if _validate_marker(marker, self._root) is None:
            return  # marker ausente/adulterado: não é autoridade para remover
        if _validate_marker(marker, self._root).get("workspace_id") != self._id:
            return
        shutil.rmtree(self._root, ignore_errors=True)

    def __enter__(self) -> "EphemeralWorkspace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
        return None


def _create_workspace(repo_root: Path, config: EphemeralConfig) -> EphemeralWorkspace:
    repo = Path(repo_root).resolve()
    session = config.session_id or os.environ.get("HENKE_SESSION_ID") or "nosession"
    ws_id = _uuid.uuid4().hex[:12]
    root = Path(tempfile.gettempdir()) / f"{_DIR_PREFIX}{_slug(session)}-{ws_id}"
    root.mkdir(parents=True, exist_ok=False)
    expires_at = _utcnow() + timedelta(seconds=config.ttl_seconds)
    marker = {
        "kind": "henke-ephemeral",
        "workspace_id": ws_id,
        "session_id": session,
        "repo": str(repo),
        "created_at": _iso(_utcnow()),
        "expires_at": _iso(expires_at),
    }
    (root / _MARKER_NAME).write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return EphemeralWorkspace(root, ws_id, config, expires_at)


def ephemeral_workspace(config: EphemeralConfig | None = None,
                        *, repo_root: Path) -> ContextManager[EphemeralWorkspace]:
    """Context manager: cria workspace único com TTL e cleanup garantido."""
    cfg = config or EphemeralConfig()
    return _create_workspace(Path(repo_root), cfg)


def ephemeral_test(*, ttl_seconds: float = 120.0) -> Callable:
    """Decorator: injeta `ws` por keyword e sempre fecha em `finally`."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            cfg = EphemeralConfig(ttl_seconds=ttl_seconds)
            with ephemeral_workspace(cfg, repo_root=Path.cwd()) as ws:
                kwargs["ws"] = ws
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def ephemeral_gc(repo_root: Path, *, now: datetime) -> int:
    """Remove workspaces efêmeros expirados e validados do repo. Retorna a contagem."""
    repo = Path(repo_root).resolve()
    tmp = Path(tempfile.gettempdir())
    removed = 0
    try:
        entries = list(tmp.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_dir() or not entry.name.startswith(_DIR_PREFIX):
            continue
        marker = entry / _MARKER_NAME
        data = _validate_marker(marker, entry, repo_root=repo)
        if data is None:
            continue
        try:
            expires = _parse_iso(data["expires_at"])
        except (KeyError, ValueError):
            continue
        if now < expires:
            continue  # dentro do TTL: jamais remove
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed += 1
    return removed
