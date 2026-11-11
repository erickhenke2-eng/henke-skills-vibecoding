#!/usr/bin/env python3
"""event_store.py — event log v2 unificado (P1-03 / next-10-days item3).

ANTES: três stores separados sem um eixo comum —
  .henke/exec_log.jsonl      (execuções custodiadas, hash-chained)
  .henke/LEDGER.json         (créditos de teste — derivável do exec_log)
  .henke/cache/gate_cache*   (vereditos de portão, HMAC)
  .henke/cache/scans/*.json  (manifests incrementais)

AGORA (versão staged — decisão documentada):
  .henke/events.jsonl        = eixo ÚNICO append-only. Todo evento é um
    envelope {seq, ts, kind, payload, prev_hash, entry_hash} encadeado
    (mesmo esquema do exec_log), sob _file_lock, com âncora local de ponta
    — truncar o rabinho diverge da âncora fora do repo.
  O exec_log permanece o stream canônico de exec (já é hash-chained maduro
  com âncora própria); os eventos "exec" são ESPELHO dele — re-escrever o
  exec_log inteiro agora era risco de regressão maior que o ganho. Os
  demais arquivos (LEDGER/gate_cache/scan_manifest) passam a ser VIEWS
  derivadas: `rebuild_derived()` os regenera a partir de events.jsonl +
  exec_log, e `verify()` prova a cadeia.

API:
    append_event(repo, kind, payload) -> dict   # sequencia + hash-chain
    migrate(repo) -> dict                       # backfill idempotente
    rebuild_derived(repo) -> dict               # LEDGER + gate index
    verify(repo) -> list[str]                   # erros de cadeia

Kinds: "exec" (espelho exec_log), "gate_result", "scan_result",
"migrate_marker".
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_VALID_KINDS = frozenset(
    {"exec", "gate_result", "scan_result", "migrate_marker"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def events_path(repo_root: Path | str) -> Path:
    # state_dir honra HENKE_STATE_DIR/--state-dir: auditoria/testes mantêm o
    # event log fora do repo auditado; sem override cai em <repo>/.henke.
    try:
        from henke.state_paths import state_dir
    except ImportError:
        from state_paths import state_dir  # type: ignore[no-redef]
    return state_dir(Path(repo_root)) / "events.jsonl"


def _tip_path(repo_root: Path | str) -> Path:
    """Âncora local da ponta do events.jsonl — mesma família de
    exec-tips (fora do repo, chaveada por path do repo)."""
    try:
        from henke.run_state import _config_dir
    except ImportError:
        from run_state import _config_dir  # type: ignore[no-redef]
    repo_key = hashlib.sha256(
        str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:32]
    return _config_dir() / "events-tips" / f"{repo_key}.json"


def _record_tip(repo_root: Path | str, tip: str) -> None:
    from henke.run_state import _atomic_write_json
    p = _tip_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, {
        "repo": str(Path(repo_root).resolve()),
        "tip": tip,
        "updated_at": _now_iso(),
    })


def load_tip(repo_root: Path | str) -> str | None:
    p = _tip_path(repo_root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("tip")) if data.get("tip") else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _iter_events(path: Path):
    if not path.is_file():
        return
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            try:
                obj = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def append_event(repo_root: Path | str, kind: str,
                 payload: dict[str, Any]) -> dict:
    """Append canônico: sequencia monotônica + cadeia de hashes, sob
    _file_lock, com fsync e âncora de ponta. Falha → exceção (o chamador
    decide o rollback — nunca append silencioso)."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind inválido: {kind!r} (válidos: {sorted(_VALID_KINDS)})")
    repo = Path(repo_root).resolve()
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)

    from henke.run_state import _file_lock
    with _file_lock(path):
        prev_hash = "0" * 64
        seq = 0
        for ev in _iter_events(path):
            prev_hash = str(ev.get("entry_hash", prev_hash))
            seq = max(seq, int(ev.get("seq", 0)))
        seq += 1
        body = {
            "schema": SCHEMA_VERSION,
            "seq": seq,
            "ts": _now_iso(),
            "kind": kind,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        entry_hash = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False)
            .encode("utf-8")).hexdigest()
        body["entry_hash"] = entry_hash
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        _record_tip(repo, entry_hash)
    return body


def verify(repo_root: Path | str) -> list[str]:
    """Cadeia interna + âncora de ponta. Lista vazia = íntegro."""
    repo = Path(repo_root).resolve()
    path = events_path(repo)
    errors: list[str] = []
    prev = "0" * 64
    last_hash: str | None = None
    last_seq = 0
    for i, ev in enumerate(_iter_events(path) or [], 1):
        if ev.get("schema") != SCHEMA_VERSION:
            errors.append(f"evento #{i}: schema {ev.get('schema')} != {SCHEMA_VERSION}")
        if ev.get("prev_hash") != prev:
            errors.append(f"evento #{i}: prev_hash divergente da cadeia")
            break
        seq = int(ev.get("seq", 0))
        if seq != last_seq + 1:
            errors.append(f"evento #{i}: seq {seq} não monotônica (esperado {last_seq + 1})")
            break
        last_seq = seq
        recomputed = hashlib.sha256(
            json.dumps({k: v for k, v in ev.items() if k != "entry_hash"},
                        sort_keys=True, ensure_ascii=False)
            .encode("utf-8")).hexdigest()
        if recomputed != ev.get("entry_hash"):
            errors.append(f"evento #{i}: entry_hash incorreto (conteúdo adulterado)")
            break
        prev = str(ev["entry_hash"])
        last_hash = str(ev["entry_hash"])
    tip = load_tip(repo)
    if tip is not None and last_hash != tip:
        errors.append(
            "âncora de ponta diverge do log — reescrita/truncamento de rabo")
    elif tip is not None and last_hash is None:
        errors.append("âncora existe mas log está vazio — log apagado")
    return errors


def migrate(repo_root: Path | str) -> dict:
    """Backfill idempotente: importa exec_log.jsonl, gate_cache.json e
    manifests como eventos. Marker 'migrate_marker' evita re-import."""
    try:
        from henke.state_paths import state_dir
    except ImportError:
        from state_paths import state_dir  # type: ignore[no-redef]
    repo = Path(repo_root).resolve()
    sd = state_dir(repo)
    legacy_sd = repo / ".henke"
    already = any(ev.get("kind") == "migrate_marker"
                  for ev in (_iter_events(events_path(repo)) or []))
    stats = {"exec": 0, "gate_result": 0, "scan_result": 0,
             "skipped": already}
    if already:
        return stats

    exec_log = sd / "exec_log.jsonl" if (sd / "exec_log.jsonl").is_file() else legacy_sd / "exec_log.jsonl"
    for ln_path, kind in ((exec_log, "exec"),):
        for obj in _iter_events(ln_path) or []:
            append_event(repo, kind, {"origin": "exec_log", "entry": obj})
            stats[kind] += 1

    gate_index = (sd / "cache" / "gate_cache.json"
                  if (sd / "cache" / "gate_cache.json").is_file()
                  else legacy_sd / "cache" / "gate_cache.json")
    if gate_index.is_file():
        try:
            idx = json.loads(gate_index.read_text(encoding="utf-8"))
            entries = idx.get("entries", idx) if isinstance(idx, dict) else idx
            if isinstance(entries, dict):
                iterable = entries.values()
            elif isinstance(entries, list):
                iterable = entries
            else:
                iterable = []
            for entry in iterable:
                if isinstance(entry, dict):
                    append_event(repo, "gate_result",
                                 {"origin": "gate_cache", "entry": entry})
                    stats["gate_result"] += 1
        except (OSError, json.JSONDecodeError):
            pass

    scans_dir = (sd / "cache" / "scans"
                 if (sd / "cache" / "scans").is_dir()
                 else legacy_sd / "cache" / "scans")
    if scans_dir.is_dir():
        for mf in sorted(scans_dir.glob("*.json")):
            try:
                obj = json.loads(mf.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            append_event(repo, "scan_result",
                         {"origin": f"scan_manifest:{mf.name}", "entry": obj})
            stats["scan_result"] += 1

    append_event(repo, "migrate_marker",
                 {"counts": {k: v for k, v in stats.items() if k != "skipped"}})
    return stats


def rebuild_derived(repo_root: Path | str) -> dict:
    """Regera as views derivadas a partir dos eventos + exec_log.

    - LEDGER.json ← eventos "exec" com testes_descobertos>0 (mesma regra
      do exec: só runner reconhecido minta — a regra já vive no espelho
      pois veio do exec_log/verdict).
    - gate_cache consolidated ← último "gate_result" por key.
    Retorna contagens por view — prova de derivabilidade (P1-03)."""
    try:
        from henke.state_paths import state_dir
    except ImportError:
        from state_paths import state_dir  # type: ignore[no-redef]
    repo = Path(repo_root).resolve()
    sd = state_dir(repo)
    legacy_sd = repo / ".henke"
    ledger: list[dict] = []
    gate_index: dict[str, dict] = {}
    _seen_exec: set[str] = set()

    def _fold_exec(entry: dict) -> None:
        # Dedup: exec_log canônico e seu espelho "exec" em events.jsonl são
        # o mesmo físico — sem dedup a view reconstruía crédito dobrado.
        key = str(entry.get("entry_hash") or hashlib.sha256(
            json.dumps(entry, sort_keys=True, default=str,
                       ensure_ascii=False).encode("utf-8")).hexdigest())
        if key in _seen_exec:
            return
        _seen_exec.add(key)
        if (isinstance(entry, dict)
                and isinstance(entry.get("testes_descobertos"), int)
                and entry["testes_descobertos"] > 0):
            ledger.append({
                "cmd": entry.get("cmd", ""),
                "exit": entry.get("exit", 0),
                "testes_descobertos": entry["testes_descobertos"],
                "testes_passados": entry.get("testes_passados", 0),
                "stdout_sha256": entry.get("stdout_sha256", ""),
                "entry_hash": entry.get("entry_hash", ""),
            })

    # Fonte primária: exec_log canônico; eventos "exec" (espelho) como
    # complemento p/ entradas só presentes no v2.
    exec_log = sd / "exec_log.jsonl" if (sd / "exec_log.jsonl").is_file() else legacy_sd / "exec_log.jsonl"
    for obj in _iter_events(exec_log) or []:
        _fold_exec(obj)
    for ev in _iter_events(events_path(repo)) or []:
        if ev.get("kind") == "exec":
            pl = ev.get("payload", {})
            _fold_exec(pl.get("entry", pl))
        elif ev.get("kind") == "gate_result":
            pl = ev.get("payload", {})
            entry = pl.get("entry", pl)
            if isinstance(entry, dict) and entry.get("key"):
                gate_index[str(entry["key"])] = entry

    from henke.run_state import _atomic_write_json
    if ledger:
        _atomic_write_json(sd / "LEDGER.json", ledger)
    if gate_index:
        _atomic_write_json(
            sd / "cache" / "gate_cache.json",
            {"schema": SCHEMA_VERSION, "entries": gate_index})
    return {"ledger_entries": len(ledger), "gate_entries": len(gate_index)}
