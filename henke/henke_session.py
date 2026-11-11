"""henke_session.py — sentinel de sessão com rastreamento de artefatos (Wave 7, R7).

O sentinel vive em `<state_dir>/sessions/<session-id>.sentinel.json`.
`owner_pid` é informativo para diagnóstico; NUNCA é autoridade de morte —
inclusive para evitar PID reuse. `session_gc` só marca `reaped` um sentinel
`active` cuja janela de heartbeat expirou e delega a limpeza a `ephemeral_gc`,
que ainda exige a expiração individual de cada workspace.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from henke.ephemeral import _MARKER_NAME, _parse_iso, _utcnow, _iso, _validate_marker, ephemeral_gc
from henke.state_paths import state_dir
import henke.winpath as winpath

_SCHEMA_VERSION = 1
_HEARTBEAT_WINDOW = timedelta(hours=1)


@dataclass(frozen=True)
class SessionSentinel:
    schema_version: int
    session_id: str
    run_id: str
    agent: str
    workspace: str
    owner_pid: int
    started_at: str
    heartbeat_at: str
    heartbeat_expires_at: str
    state: Literal["active", "closed", "reaped"]
    ephemeral_workspaces: tuple[str, ...]


def _sessions_dir(workspace: Path | None = None) -> Path:
    if workspace is None:
        env = os.environ.get("HENKE_WORKSPACE")
        workspace = Path(env) if env else Path.cwd()
    return state_dir(Path(workspace)) / "sessions"


def _sentinel_path(session_id: str, workspace: Path | None = None) -> Path:
    """session_id deve ser um único componente seguro contido em sessions/.

    `assert_safe_relative` rejeita absoluto, `..`, drive/ADS, nomes
    reservados Win32 e caracteres de controle; `canonical_path` confirma a
    contenção lexical do sentinel dentro do diretório de sessões.
    """
    if not session_id:
        raise ValueError(f"session_id inválido: {session_id!r}")
    rel = winpath.assert_safe_relative(session_id)
    if "/" in rel:
        raise ValueError(f"session_id inválido: {session_id!r}")
    sdir = winpath.canonical_path(_sessions_dir(workspace))
    path = winpath.canonical_path(f"{rel}.sentinel.json", base=sdir)
    if path.parent != sdir or not winpath.is_within(sdir, path):
        raise ValueError(f"session_id escapa do diretório de sessões: {session_id!r}")
    return path


def _load(session_id: str, workspace: Path | None = None) -> dict:
    path = _sentinel_path(session_id, workspace)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA_VERSION:
        raise ValueError(f"sentinel inválido: {path}")
    return data


def _save(session_id: str, data: dict, workspace: Path | None = None) -> None:
    path = _sentinel_path(session_id, workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _to_sentinel(data: dict) -> SessionSentinel:
    return SessionSentinel(
        schema_version=data["schema_version"],
        session_id=data["session_id"],
        run_id=data["run_id"],
        agent=data["agent"],
        workspace=data["workspace"],
        owner_pid=data["owner_pid"],
        started_at=data["started_at"],
        heartbeat_at=data["heartbeat_at"],
        heartbeat_expires_at=data["heartbeat_expires_at"],
        state=data["state"],
        ephemeral_workspaces=tuple(data.get("ephemeral_workspaces", ())),
    )


def start_session(agent: str, workspace: Path, run_id: str) -> SessionSentinel:
    """Cria sentinel `active`; heartbeat expira em 1h até o primeiro beat."""
    ws = Path(workspace).resolve()
    session_id = f"ses-{_uuid.uuid4().hex[:12]}"
    now = _utcnow()
    data = {
        "schema_version": _SCHEMA_VERSION,
        "session_id": session_id,
        "run_id": run_id,
        "agent": agent,
        "workspace": str(ws),
        "owner_pid": os.getpid(),
        "started_at": _iso(now),
        "heartbeat_at": _iso(now),
        "heartbeat_expires_at": _iso(now + _HEARTBEAT_WINDOW),
        "state": "active",
        "ephemeral_workspaces": [],
    }
    _save(session_id, data, ws)
    return _to_sentinel(data)


def load_session(session_id: str, workspace: Path | None = None) -> SessionSentinel:
    return _to_sentinel(_load(session_id, workspace))


def register_ephemeral(session_id: str, workspace: Path) -> None:
    """Registra workspace efêmera: exige marker válido e contenção no repo."""
    data = _load(session_id)
    ws = Path(workspace)
    sentinel_repo = Path(data["workspace"]).resolve()
    if _validate_marker(ws / _MARKER_NAME, ws.resolve(), repo_root=sentinel_repo) is None:
        raise ValueError(f"marker efêmero inválido ou fora do repo da sessão: {ws}")
    entry = str(ws.resolve())
    current = list(data.get("ephemeral_workspaces", []))
    if entry not in current:
        current.append(entry)
        data["ephemeral_workspaces"] = current
        _save(session_id, data)


def heartbeat(session_id: str) -> None:
    """Renova a janela de heartbeat para +1h."""
    data = _load(session_id)
    if data["state"] != "active":
        raise RuntimeError(f"sessão {session_id} não está ativa (state={data['state']})")
    now = _utcnow()
    data["heartbeat_at"] = _iso(now)
    data["heartbeat_expires_at"] = _iso(now + _HEARTBEAT_WINDOW)
    _save(session_id, data)


def end_session(session_id: str) -> None:
    data = _load(session_id)
    data["state"] = "closed"
    _save(session_id, data)


def session_gc(workspace: Path, *, now: datetime) -> int:
    """Marca `reaped` sentinels ativos expirados e roda `ephemeral_gc`.

    Não varre processos, não usa owner_pid para matar, não apaga caminhos
    arbitrários e não remove workspace efêmera dentro do TTL.
    Retorna a quantidade de sentinels marcados como `reaped`.
    """
    ws = Path(workspace).resolve()
    sdir = _sessions_dir(ws)
    reaped = 0
    if sdir.is_dir():
        for path in sorted(sdir.glob("*.sentinel.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("schema_version") != _SCHEMA_VERSION:
                continue
            if data.get("state") != "active":
                continue
            try:
                expires = _parse_iso(data["heartbeat_expires_at"])
            except (KeyError, ValueError):
                continue
            if now < expires:
                continue
            data["state"] = "reaped"
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            reaped += 1
    ephemeral_gc(ws, now=now)
    return reaped
