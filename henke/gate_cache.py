#!/usr/bin/env python3
"""
gate_cache.py — Smart Gate Cache with Windows 500 MB RAM Ceiling (PROTOCOLO v4.0).

Features:
  - Deterministic Merkle cache key generation based on gate identity, script hash,
    canonicalized argument list, and repository target files content (including CI configurations).
  - Disk storage in `.henke/cache/gates/<key>.json` and consolidated `.henke/cache/gate_cache.json`
    with atomic writes (.tmp + os.replace).
  - Resilient recovery against corrupted cache files on disk (treated as cache miss).
  - Physical RAM and Private Commit measurement on Windows via ctypes Win32 psapi with zero external dependencies.
  - LRU memory and disk eviction, garbage collection, and Win32 `EmptyWorkingSet` memory release when consumption reaches 500 MB.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from henke.state_paths import cache_root, secret_dir, secret_key_name, state_dir  # noqa: E402


def _henke_dir(repo_path: Path) -> Path:
    """Diretório de estado Henke — honra HENKE_STATE_DIR (Wave 7)."""
    return state_dir(Path(repo_path))

# ---------------------------------------------------------------------------
# Win32 Working Set & Private Memory Tracking (Zero External Dependencies)
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform.startswith("win")

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    try:
        ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        ctypes.windll.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        ctypes.windll.psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        ctypes.windll.psapi.EmptyWorkingSet.restype = wintypes.BOOL
    except Exception:
        pass


def get_current_ram_mb() -> float:
    """Returns the current process physical Working Set or Private Commit in megabytes (MB)."""
    if _IS_WINDOWS:
        try:
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                ws_mb = float(pmc.WorkingSetSize) / (1024.0 * 1024.0)
                pf_mb = float(pmc.PagefileUsage) / (1024.0 * 1024.0)
                return max(ws_mb, pf_mb)
        except Exception:
            pass
    else:
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss = usage.ru_maxrss
            if sys.platform.startswith("darwin"):
                return float(rss) / (1024.0 * 1024.0)
            return float(rss) / 1024.0
        except Exception:
            pass

    return 0.0


def trim_working_set() -> bool:
    """Calls EmptyWorkingSet on Windows to release non-essential physical memory."""
    if _IS_WINDOWS:
        try:
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            return bool(ctypes.windll.psapi.EmptyWorkingSet(handle))
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# In-Memory Cache and Synchronization
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.RLock()
_IN_MEMORY_CACHE: OrderedDict[str, Dict[str, Any]] = OrderedDict()
_LAST_REPO_ROOT: Optional[Path] = None

_IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
}

_IGNORED_EXTS = {
    ".tmp",
    ".pyc",
    ".pyo",
    ".swp",
}


def _discover_repo_root(repo_root: Optional[Path | str] = None) -> Path:
    """Resolves target repository root from argument, cached state, or cwd."""
    global _LAST_REPO_ROOT
    if repo_root is not None:
        p = Path(repo_root).resolve()
        _LAST_REPO_ROOT = p
        return p

    if _LAST_REPO_ROOT is not None and _LAST_REPO_ROOT.is_dir():
        return _LAST_REPO_ROOT

    cwd = Path.cwd().resolve()
    return cwd


def _safe_key_filename(key: str) -> str:
    """Produces a filesystem-safe filename for a cache key."""
    if not key:
        return "_empty_key_.json"
    safe = "".join(c if (c.isalnum() or c in ("-", "_")) else "_" for c in key)
    if len(safe) > 120:
        safe = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{safe}.json"


def _atomic_write_file(path: Path, content: str) -> None:
    """Writes content to target path atomically via a temporary file and os.replace."""
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _read_json_safe(path: Path) -> Optional[Dict[str, Any]]:
    """Reads JSON dictionary from path, safely recovering if file is corrupt."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ---------------------------------------------------------------------------
# Merkle Cache Key Generation
# ---------------------------------------------------------------------------

def _collect_files_to_hash(args: List[str], repo_root: Path) -> List[Tuple[str, str]]:
    """
    Identifies target files or directories referenced in command arguments.
    Computes sorted list of (canonical_relative_path, sha256_hex).
    """
    repo_root = Path(repo_root).resolve()
    explicit_targets: List[Path] = []

    for a in args:
        if not a or a.startswith("-"):
            continue
        p = Path(a)
        if not p.is_absolute():
            p = (repo_root / p).resolve()
        else:
            p = p.resolve()

        if p.exists():
            explicit_targets.append(p)
        elif " " in a:
            for token in a.split():
                if token.endswith(".py") or "/" in token or "\\" in token:
                    tp = Path(token)
                    if not tp.is_absolute():
                        tp = (repo_root / tp).resolve()
                    else:
                        tp = tp.resolve()
                    if tp.exists():
                        explicit_targets.append(tp)

    entries: Dict[str, str] = {}

    if explicit_targets:
        for target in explicit_targets:
            if not target.exists():
                rel = str(target)
                try:
                    rel = target.relative_to(repo_root).as_posix()
                except ValueError:
                    pass
                entries[rel] = "MISSING"
                continue

            if target.is_file():
                try:
                    rel = target.relative_to(repo_root).as_posix()
                except ValueError:
                    rel = target.name
                try:
                    h = hashlib.sha256(target.read_bytes()).hexdigest()
                    entries[rel] = h
                except OSError:
                    entries[rel] = "UNREADABLE"
            elif target.is_dir():
                _scan_dir_files(target, repo_root, entries)
    else:
        _scan_dir_files(repo_root, repo_root, entries)

    return sorted(entries.items(), key=lambda x: x[0])


def _scan_dir_files(dir_path: Path, repo_root: Path, out_entries: Dict[str, str]) -> None:
    """Recursively scans directory for source files, skipping ignored metadata."""
    try:
        for root, dirs, files in os.walk(str(dir_path)):
            # Prune only explicitly ignored directories (never prune .github, .gitlab, etc.)
            dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
            if Path(root).name == ".henke" and "cache" in dirs:
                dirs.remove("cache")

            for fname in files:
                p = Path(root) / fname
                if p.suffix.lower() in _IGNORED_EXTS:
                    continue
                if fname.endswith("~") or fname.startswith(".#"):
                    continue
                try:
                    rel = p.relative_to(repo_root).as_posix()
                except ValueError:
                    rel = p.name
                try:
                    file_bytes = p.read_bytes()
                    out_entries[rel] = hashlib.sha256(file_bytes).hexdigest()
                except OSError:
                    out_entries[rel] = "UNREADABLE"
    except OSError:
        pass


_SUITE_DIGEST_CACHE: Dict[str, str] = {}


def _suite_scripts_digest(suite_root: Path) -> str:
    """SHA-256 do fechamento de scripts da suíte (DS-19): a chave cobria só o
    script do portão — corrigir `polyglot.py`/helper compartilhado reusava
    veredito velho. Digest de TODOS os .py da suíte; cacheado por processo."""
    key = str(suite_root.resolve())
    cached = _SUITE_DIGEST_CACHE.get(key)
    if cached:
        return cached
    h = hashlib.sha256(b"suite-v1\x00")
    scripts_dir = Path(suite_root) / "henke"
    try:
        files = sorted(
            p for p in scripts_dir.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts)
    except OSError:
        files = []
    for p in files:
        try:
            h.update(p.relative_to(suite_root).as_posix().encode("utf-8"))
            h.update(b"\x00")
            h.update(hashlib.sha256(p.read_bytes()).digest())
            h.update(b"\x00")
        except OSError:
            h.update(b"UNREADABLE\x00")
    digest = h.hexdigest()
    _SUITE_DIGEST_CACHE[key] = digest
    return digest


def compute_cache_key(
    gate_name: str,
    script_path: Path | str,
    args: List[str],
    repo_root: Path | str,
) -> str:
    """
    Computes a deterministic 64-character SHA-256 Merkle cache key representing:
      1. gate_name
      2. SHA-256 hash of the gate script itself
      3. Canonicalized argument vector
      4. Merkle tree hash of target files or entire repository content
    """
    repo = Path(repo_root).resolve()
    script_p = Path(script_path)
    if not script_p.is_absolute():
        script_p = (repo / script_p).resolve()

    # 1. Script hash
    script_hash = "NOSCRIPT"
    if script_p.is_file():
        try:
            script_hash = hashlib.sha256(script_p.read_bytes()).hexdigest()
        except OSError:
            script_hash = "UNREADABLE_SCRIPT"

    # 2. Argument list canonical representation
    canon_args = [str(a) for a in args]

    # 3. Target files Merkle tree hash
    file_hashes = _collect_files_to_hash(args, repo)
    files_merkle = hashlib.sha256()
    for rel_path, f_hash in file_hashes:
        files_merkle.update(rel_path.encode("utf-8", errors="replace"))
        files_merkle.update(b":")
        files_merkle.update(f_hash.encode("utf-8", errors="replace"))
        files_merkle.update(b"\n")
    merkle_digest = files_merkle.hexdigest()

    # 4. Git state (HEAD commit + porcelain status hash)
    git_state = "NOGIT"
    if (repo / ".git").exists():
        try:
            import os as _os
            import subprocess
            # Merge-Audit 2: env cru + git sem -c — core.fsmonitor/diff.external
            # de .git/config executam código do repo auditado (RCE no check).
            from henke.state_paths import sanitized_child_env
            _ge = sanitized_child_env(repo)
            # Banca-A 1: textconv/diff.<drv>.command de .git/config executam
            # ao materializar o patch — --no-ext-diff E --no-textconv, mais
            # timeout contra diff gigante/lock (DoS).
            _gc = ["git", "-c", "core.fsmonitor=false", "-c", "diff.external="]
            res_head = subprocess.run([*_gc, "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, env=_ge, timeout=30)
            head_str = res_head.stdout.strip() if res_head.returncode == 0 else ""
            res_st = subprocess.run([*_gc, "status", "--porcelain"], cwd=str(repo), capture_output=True, text=True, env=_ge, timeout=30)
            st_str = res_st.stdout if res_st.returncode == 0 else ""
            res_diff = subprocess.run([*_gc, "diff", "--no-ext-diff", "--no-textconv", "HEAD"], cwd=str(repo), capture_output=True, text=True, env=_ge, timeout=60)
            diff_str = res_diff.stdout if res_diff.returncode == 0 else ""
            git_state = f"{head_str}:{hashlib.sha256((st_str + chr(10) + diff_str).encode('utf-8')).hexdigest()}"
        except Exception:
            pass

    # 5. Composite key hash — DS-19: + digest dos scripts da suíte (deps
    # compartilhadas mudam o veredito) + versão de schema da chave.
    composite = hashlib.sha256()
    composite.update(b"key-v2\x00")
    composite.update(gate_name.encode("utf-8"))
    composite.update(b"|")
    composite.update(script_hash.encode("utf-8"))
    composite.update(b"|")
    suite_root = Path(__file__).resolve().parent.parent
    composite.update(_suite_scripts_digest(suite_root).encode("utf-8"))
    composite.update(b"|")
    composite.update(json.dumps(canon_args, sort_keys=True).encode("utf-8"))
    composite.update(b"|")
    composite.update(merkle_digest.encode("utf-8"))
    composite.update(b"|")
    composite.update(git_state.encode("utf-8"))

    return composite.hexdigest()


# ---------------------------------------------------------------------------
# Cryptographic Cache Entry Authentication (HMAC-SHA256 Anti-Forgery)
# ---------------------------------------------------------------------------

import hmac
import secrets

def _get_cache_secret(repo_path: Path) -> bytes:
    """Recupera ou gera a chave secreta HMAC das entradas de cache.

    DS-5: a chave vive FORA da árvore auditada (perfil do usuário, chaveada
    pelo repo). Um `.cache_secret` dentro do repo — plantado por zip/tarball
    ou commit — nunca é consultado: quem controla o repo não controla a chave.
    """
    secret_file = secret_dir(Path(repo_path)) / secret_key_name(Path(repo_path))
    try:
        # Delta-F15: HENKE_SECRET_DIR dentro do repo auditado = chave
        # legível por quem controla o repo → entradas de cache forjáveis.
        # Fail-closed: sem chave, nenhuma entrada verifica (cache miss).
        secret_file.resolve().relative_to(Path(repo_path).resolve())
        return None
    except ValueError:
        pass
    if secret_file.is_file():
        try:
            raw = secret_file.read_bytes()
            if len(raw) == 32:
                return raw
            txt = raw.decode("utf-8", errors="ignore").strip()
            if len(txt) >= 64:
                return txt.encode("utf-8")
        except Exception:
            pass
    new_hex = secrets.token_hex(32)
    try:
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(new_hex, encoding="utf-8")
        if os.name != "nt":
            os.chmod(secret_file, 0o600)
    except Exception:
        pass
    return new_hex.encode("utf-8")


def _compute_entry_hmac(secret: bytes | None, key: str, entry: Dict[str, Any]) -> str:
    """Calcula a assinatura HMAC-SHA256 para impedir forja de vereditos no cache."""
    if secret is None:
        return ""  # Delta-F15: sem chave confiável, nada assina/verifica
    clean = {k: v for k, v in entry.items() if k not in ("hmac", "last_accessed", "access_count")}
    canonical = json.dumps(clean, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(secret, f"{key}:".encode("utf-8") + canonical, hashlib.sha256).hexdigest()


def _verify_entry_hmac(secret: bytes | None, key: str, entry: Dict[str, Any]) -> bool:
    """Verifica a integridade criptográfica da entrada do cache."""
    if secret is None:
        return False
    stored_hmac = entry.get("hmac")
    if not stored_hmac or not isinstance(stored_hmac, str):
        return False
    expected = _compute_entry_hmac(secret, key, entry)
    return bool(expected) and hmac.compare_digest(stored_hmac, expected)


# ---------------------------------------------------------------------------
# Cache Query and Storage Operations
# ---------------------------------------------------------------------------

def get_cached_gate(
    key: str,
    repo_root: Optional[Path | str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieves a cached gate execution verdict if available and verified."""
    repo_path = _discover_repo_root(Path(repo_root) if repo_root else None)
    secret = _get_cache_secret(repo_path)

    # Check in-memory LRU cache
    with _CACHE_LOCK:
        if key in _IN_MEMORY_CACHE:
            entry = _IN_MEMORY_CACHE[key]
            if _verify_entry_hmac(secret, key, entry):
                entry["last_accessed"] = datetime.now(timezone.utc).isoformat()
                entry["access_count"] = entry.get("access_count", 0) + 1
                _IN_MEMORY_CACHE.move_to_end(key)
                return dict(entry)
            else:
                del _IN_MEMORY_CACHE[key]

    # Check disk cache
    cache_dir = cache_root(repo_path)

    # 1. Check consolidated gate_cache.json if present
    consolidated_file = cache_dir / "gate_cache.json"
    consolidated = _read_json_safe(consolidated_file)
    if consolidated and key in consolidated:
        item = consolidated[key]
        if isinstance(item, dict) and _verify_entry_hmac(secret, key, item):
            with _CACHE_LOCK:
                _IN_MEMORY_CACHE[key] = item
                _IN_MEMORY_CACHE.move_to_end(key)
            return dict(item)

    # 2. Check individual key file .henke/cache/gates/<key>.json
    gate_file = cache_dir / "gates" / _safe_key_filename(key)
    gate_entry = _read_json_safe(gate_file)
    if gate_entry is not None and isinstance(gate_entry, dict) and _verify_entry_hmac(secret, key, gate_entry):
        gate_entry["last_accessed"] = datetime.now(timezone.utc).isoformat()
        gate_entry["access_count"] = gate_entry.get("access_count", 0) + 1
        with _CACHE_LOCK:
            _IN_MEMORY_CACHE[key] = gate_entry
            _IN_MEMORY_CACHE.move_to_end(key)
        return dict(gate_entry)

    return None


def store_cached_gate(
    key: str,
    result: Dict[str, Any],
    repo_root: Optional[Path | str] = None,
) -> None:
    """Stores a gate verdict in in-memory LRU cache and atomically persists to disk with HMAC signature."""
    now_iso = datetime.now(timezone.utc).isoformat()
    repo_path = _discover_repo_root(Path(repo_root) if repo_root else None)
    secret = _get_cache_secret(repo_path)

    entry: Dict[str, Any] = {
        "key": key,
        "gate_name": result.get("gate", result.get("gate_name", "")),
        "exit_code": result.get("exit_code", result.get("returncode", 0)),
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "created_at": result.get("created_at", now_iso),
        "last_accessed": now_iso,
        "access_count": result.get("access_count", 1),
    }
    entry.update(result)
    entry["hmac"] = _compute_entry_hmac(secret, key, entry)

    with _CACHE_LOCK:
        _IN_MEMORY_CACHE[key] = entry
        _IN_MEMORY_CACHE.move_to_end(key)

    try:
        repo_path = _discover_repo_root(Path(repo_root) if repo_root else None)
        cache_dir = cache_root(repo_path)

        # 1. Write individual gate entry atomically
        gate_file = cache_dir / "gates" / _safe_key_filename(key)
        _atomic_write_file(gate_file, json.dumps(entry, ensure_ascii=False, indent=2))

        # 2. Update consolidated gate_cache.json atomically
        consolidated_file = cache_dir / "gate_cache.json"
        consolidated = _read_json_safe(consolidated_file) or {}
        consolidated[key] = entry
        _atomic_write_file(consolidated_file, json.dumps(consolidated, ensure_ascii=False, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# RAM Ceiling Enforcement & LRU Eviction
# ---------------------------------------------------------------------------

def enforce_ram_limit(
    limit_mb: float = 500.0,
    force_flush: bool = False,
    repo_root: Optional[Path | str] = None,
) -> int:
    """
    Enforces maximum RAM ceiling (default 500 MB).
    When process Working Set exceeds limit_mb or force_flush is True:
      - Evicts least-recently-used (LRU) entries from in-memory cache and removes them from disk.
      - Calls garbage collection and Win32 EmptyWorkingSet to release memory.
    Returns the total count of evicted entries.
    """
    current_mb = get_current_ram_mb()
    evicted_count = 0

    if current_mb >= limit_mb or force_flush or limit_mb <= 0.0:
        repo_path = _discover_repo_root(Path(repo_root) if repo_root else None)
        cache_dir = cache_root(repo_path)
        consolidated_file = cache_dir / "gate_cache.json"
        consolidated = _read_json_safe(consolidated_file) or {}

        with _CACHE_LOCK:
            if limit_mb <= 0.0:
                evicted_keys = list(_IN_MEMORY_CACHE.keys())
                evicted_count = len(evicted_keys)
                _IN_MEMORY_CACHE.clear()
            else:
                evicted_keys = []
                while _IN_MEMORY_CACHE:
                    lru_key, _ = _IN_MEMORY_CACHE.popitem(last=False)
                    evicted_keys.append(lru_key)
                    evicted_count += 1
                    gc.collect()
                    trim_working_set()
                    if get_current_ram_mb() < limit_mb:
                        break

            # Evict corresponding entries from disk to keep storage and memory synchronized
            for k in evicted_keys:
                gate_file = cache_dir / "gates" / _safe_key_filename(k)
                try:
                    gate_file.unlink(missing_ok=True)
                except OSError:
                    pass
                if k in consolidated:
                    del consolidated[k]

            if evicted_keys and consolidated_file.is_file():
                try:
                    _atomic_write_file(consolidated_file, json.dumps(consolidated, ensure_ascii=False, indent=2))
                except Exception:
                    pass

        gc.collect()
        trim_working_set()

    return evicted_count


def clear_cache(repo_root: Optional[Path | str] = None) -> int:
    """Clears both in-memory and disk cache entries. Returns number of cleared entries."""
    global _IN_MEMORY_CACHE
    with _CACHE_LOCK:
        count = len(_IN_MEMORY_CACHE)
        _IN_MEMORY_CACHE.clear()

    try:
        repo_path = _discover_repo_root(Path(repo_root) if repo_root else None)
        # DS-14: cache vive fora do repo; limpa ambos (externo novo + legado
        # .henke/cache de runs antigas) — o legado pode conter entradas
        # pré-migração que seriam resgatadas depois.
        for cache_dir in (cache_root(repo_path),
                          repo_path / ".henke" / "cache"):
            gates_dir = cache_dir / "gates"
            if gates_dir.is_dir():
                for f in gates_dir.glob("*.json"):
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass

            consolidated_file = cache_dir / "gate_cache.json"
            if consolidated_file.is_file():
                try:
                    consolidated_file.unlink()
                except OSError:
                    pass
    except Exception:
        pass

    gc.collect()
    trim_working_set()
    return count
