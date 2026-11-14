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

import contextlib
import hashlib
import json
import os
import threading
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
    exec-tips (fora do repo, chaveada por identidade de conteúdo do repo:
    root commit / CONFIG.json, com caminho só como último recurso — F-06)."""
    try:
        from henke.run_state import _config_dir
        from henke.rs_custody import _legacy_repo_key, _repo_anchor_key
    except ImportError:
        from run_state import _config_dir  # type: ignore[no-redef]
        from rs_custody import _legacy_repo_key, _repo_anchor_key  # type: ignore[no-redef]
    return _config_dir() / "events-tips" / f"{_repo_anchor_key(repo_root)}.json"


def _tip_path_legacy(repo_root: Path | str) -> Path:
    try:
        from henke.run_state import _config_dir
        from henke.rs_custody import _legacy_repo_key
    except ImportError:
        from run_state import _config_dir  # type: ignore[no-redef]
        from rs_custody import _legacy_repo_key  # type: ignore[no-redef]
    return _config_dir() / "events-tips" / f"{_legacy_repo_key(repo_root)}.json"


def _record_tip(repo_root: Path | str, tip: str) -> None:
    from henke.run_state import _atomic_write_json
    try:
        from henke.rs_custody import repo_identity
    except ImportError:
        try:
            from rs_custody import repo_identity  # type: ignore[no-redef]
        except ImportError:
            repo_identity = None  # type: ignore[assignment]
    payload = {
        "repo": str(Path(repo_root).resolve()),
        "repo_identity": repo_identity(repo_root) if repo_identity else None,
        "tip": tip,
        "updated_at": _now_iso(),
    }
    # F-06: dual-write — identidade serve cópias isoladas; caminho legado
    # mantém leitores antigos consistentes durante a migração.
    for p in {_tip_path(repo_root), _tip_path_legacy(repo_root)}:
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(p, dict(payload))


def load_tip(repo_root: Path | str) -> str | None:
    for p in (_tip_path(repo_root), _tip_path_legacy(repo_root)):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            tip = str(data.get("tip")) if data.get("tip") else None
            if tip:
                return tip
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
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


# banca-v18 (residual 6, rev seguinte): mutex intra-processo cobrindo o
# MESMO arquivo que _file_lock protege entre processos. Dois efeitos:
#  1) serializa threads irmãs sem a loteria do O_EXCL (append/migrate já
#     eram serializados pelo file lock — o mutex só ordena a fila);
#  2) permite provar órfão same-pid: segurando o mutex, NENHUM irmão pode
#     estar dentro/criando `<path>.lock`; se o arquivo existe com
#     `pid=<o nosso>`, é órfão de release abortado (Windows: unlink
#     transitório sob AV/indexer falha e o `finally` do _file_lock engole
#     OSError) — caso contrário o próximo esperante via `pid` vivo do
#     mesmo processo NUNCA seria "stale" e morreria de TimeoutError.
# Sem o mutex, `pid==meu` poderia ser o lock VIVO de um irmão — remover
# quebrava a exclusão mútua que o residual 6 conserta.
_PROCESS_APPEND_LOCK = threading.Lock()


def _reap_self_lock_orphan(path: Path) -> None:
    """Remove `<path>.lock` se for órfão com o NOSSO pid (seguro apenas sob
    `_PROCESS_APPEND_LOCK`): ninguém escreve nosso pid além de nós — um
    irmão vivo estaria sob o mutex; um processo morto não segura nada."""
    lock_path = Path(str(Path(path).resolve()) + ".lock")
    try:
        if lock_path.read_text(
                encoding="utf-8", errors="replace").strip() == (
                f"pid={os.getpid()}"):
            lock_path.unlink()
    except OSError:
        pass


@contextlib.contextmanager
def _events_file_lock(path: Path, timeout: float):
    """`_file_lock` do events.jsonl + mutex intra-processo + reapa de
    órfão same-pid — a unidade de exclusão para append/migrate."""
    from henke.run_state import _file_lock
    with _PROCESS_APPEND_LOCK:
        _reap_self_lock_orphan(path)
        try:
            with _file_lock(path, timeout=timeout):
                yield
        finally:
            # Se o unlink do release falhou (OSError transitório engolido
            # pelo _file_lock), o órfão same-pid wedgiava o próximo
            # esperante por `timeout` inteiro — reapa enquanto o mutex
            # ainda prova que nenhum irmão o segura.
            _reap_self_lock_orphan(path)


def _append_event_locked(repo: Path, path: Path, kind: str,
                         payload: dict[str, Any]) -> dict:
    """Núcleo do append — exige `_file_lock(path)` JÁ adquirido pelo chamador.

    banca-v18 (residual 6): migrate() precisa do backfill inteiro sob UM
    lock, e `_file_lock` (O_EXCL por arquivo) não é reentrante — readquirir
    dentro do lock preso é TimeoutError. O núcleo compartilhado deixa
    append_event e migrate executarem a mesma transação atômica (ambos
    entram por `_events_file_lock`, que também segura o mutex do processo
    e reapa órfão same-pid do release)."""
    # BANCA-B4 (+revisão cega): append não pode estender cadeia corrompida.
    # O gate anterior só verificava arquivo existente-não-vazio — deletar/
    # zerar o log com âncora viva lavava a reescrita. Verifica SEMPRE que
    # âncora existe ou o log tem conteúdo.
    _has_tip = load_tip(repo) is not None
    _has_log = path.is_file() and path.stat().st_size > 0
    if _has_tip or _has_log:
        _pre_errors = verify(repo)
        if _pre_errors:
            raise ValueError(
                "events.jsonl falhou a verificação de integridade — "
                f"append recusado: {_pre_errors[0]}")
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

    with _events_file_lock(path, timeout=10.0):
        return _append_event_locked(repo, path, kind, payload)


def verify(repo_root: Path | str) -> list[str]:
    """Cadeia interna + âncora de ponta. Lista vazia = íntegro."""
    repo = Path(repo_root).resolve()
    path = events_path(repo)
    errors: list[str] = []
    prev = "0" * 64
    last_hash: str | None = None
    last_seq = 0
    # banca-v18 (rev B-F13): _iter_events pulava linhas não-JSON/não-dict
    # em silêncio — lixo injetado no meio do stream era tolerado (o
    # exec_log flaga linha malformada; o event store não). verify lê as
    # linhas cruas e reprova na primeira malformada.
    _events: list[dict] = []
    if path.is_file():
        for _ln_no, _ln in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            _ln = _ln.strip()
            if not _ln:
                # Linha vazia/whitespace no meio ou no fim do stream não
                # participa da hash-chain: antes passava como íntegra.
                errors.append(f"linha {_ln_no}: linha vazia fora da cadeia de eventos")
                break
            try:
                _obj = json.loads(_ln)
            except json.JSONDecodeError:
                errors.append(
                    f"linha {_ln_no}: JSON malformado — conteúdo injetado "
                    "no stream de eventos")
                break
            if not isinstance(_obj, dict):
                errors.append(f"linha {_ln_no}: evento não-objeto no stream")
                break
            _events.append(_obj)
    for i, ev in enumerate(_events, 1):
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
    if tip is not None and last_hash is None:
        errors.append("âncora existe mas log está vazio — log apagado")
    elif tip is not None and last_hash != tip:
        errors.append(
            "âncora de ponta diverge do log — reescrita/truncamento de rabo")
    elif tip is None and last_hash is not None:
        # BANCA-B4: log com eventos mas sem âncora — âncora apagada permite
        # reescrever o histórico inteiro como cadeia auto-consistente.
        errors.append("log existe mas âncora de ponta ausente — "
                      "histórico pode ter sido reescrito")
    return errors


def migrate(repo_root: Path | str) -> dict:
    """Backfill idempotente: importa exec_log.jsonl, gate_cache.json e
    manifests como eventos. Marker 'migrate_marker' evita re-import.

    banca-v18 (residual 6): a transação INTEIRA (check do marker + todos os
    appends + marker final) roda sob UM `_file_lock` — o mesmo lock do
    append. Antes, cada append pegava o lock individualmente: dois migrates
    concorrentes liam `already=False` e duplicavam a importação, e sob
    contenção morriam com TimeoutError deixando migração PARCIAL sem marker
    (a próxima rodada re-importava tudo de novo). Um writer externo também
    não pode intercalar no meio do backfill."""
    try:
        from henke.state_paths import state_dir
    except ImportError:
        from state_paths import state_dir  # type: ignore[no-redef]
    repo = Path(repo_root).resolve()
    sd = state_dir(repo)
    legacy_sd = repo / ".henke"
    path = events_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=60: um backfill grande legítimo pode segurar o lock além dos
    # 10s default — quem espera tem TimeoutError honesto, nunca corrupção.
    # `_events_file_lock` adiciona o mutex intra-processo (serializa irmãos
    # sem loteria O_EXCL) e a reapa de órfão same-pid do release.
    with _events_file_lock(path, timeout=60.0):
        already = any(ev.get("kind") == "migrate_marker"
                      for ev in (_iter_events(path) or []))
        stats = {"exec": 0, "gate_result": 0, "scan_result": 0,
                 "skipped": already}
        if already:
            return stats

        exec_log = sd / "exec_log.jsonl" if (sd / "exec_log.jsonl").is_file() else legacy_sd / "exec_log.jsonl"
        for ln_path, kind in ((exec_log, "exec"),):
            for obj in _iter_events(ln_path) or []:
                _append_event_locked(repo, path, kind,
                                     {"origin": "exec_log", "entry": obj})
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
                        _append_event_locked(repo, path, "gate_result",
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
                _append_event_locked(repo, path, "scan_result",
                                     {"origin": f"scan_manifest:{mf.name}", "entry": obj})
                stats["scan_result"] += 1

        _append_event_locked(repo, path, "migrate_marker",
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
