"""rs_io.py — Persistência atômica, lock interprocessos e higiene de paths de run_state."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
from pathlib import Path
import sys
import tempfile
import time

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # M12 (banca v13): write+replace sem fsync perdiam run/token/tip num
    # crash — flusha o arquivo E o diretório antes/depois do replace.
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    try:
        dfd = os.open(str(path.parent),
                      os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass  # alguns FS (FAT, alguns mounts) não permitem fsync de dir


def _pid_alive(pid: int) -> bool:
    """Dono do lock ainda existe? A1 (banca v14): `os.kill(pid,0)` no Windows
    mapeia sig 0 → GenerateConsoleCtrlEvent — injetava Ctrl+C num processo
    inocente com PID reciclado e sempre retornava vivo."""
    if os.name == "nt":
        try:
            # banca-v18 (rev B-F4): pid fora de faixa (10^20 de lock-lixo)
            # caía fora do except: ctypes.ArgumentError no Windows e
            # OverflowError no os.kill POSIX — crashava _file_lock inteiro.
            # PID válido ∈ [1, 2**32); fora disso o conteúdo é lixo → stale.
            _pid_i = int(pid)
            if not 0 < _pid_i < 2**32:
                return False
            import ctypes
            # BANCA-B5: ctypes.windll.kernel32 + ctypes.GetLastError() lia o
            # LAST-ERROR do thread errado (windll não seta use_last_error) —
            # dono vivo classificado como morto quebrava lock de vivo.
            _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = _k32.OpenProcess(0x1000, False, _pid_i)
            if h:
                _k32.CloseHandle(h)
                return True
            # M1 (banca v15): ERROR_ACCESS_DENIED(5) = processo existe mas
            # sem acesso — VIVO, não stale. 87 (param inválido) = morto.
            return ctypes.get_last_error() == 5
        except (AttributeError, OSError, TypeError, ValueError,
                OverflowError):
            return True  # indeterminado → não quebra lock de vivo
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # M1 (banca v15): EPERM = existe, só sem sinal
    except OSError:
        return False


@contextlib.contextmanager
def _file_lock(target: Path, timeout: float = 10.0, poll: float = 0.05):
    """Lock interprocessos por arquivo-alvo (`<target>.lock`).

    Stdlib pura e multi-plataforma: `os.open(O_CREAT|O_EXCL)` com retry até
    `timeout`. Cobre a transação read-modify-write inteira — sem ele, dois
    processos leem o mesmo JSON e um write é perdido.
    """
    # M2 (banca v13): spellings distintos do mesmo arquivo geravam locks
    # distintos → lost update concorrente.
    lock_path = Path(str(Path(target).resolve()) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # M10 v13 + A1 v14: lock órfão de crash wedgiava pra sempre; MAS
            # arquivo recém-criado (ainda sem pid, janela open→write) NÃO é
            # stale — só quebra com pid morto OU conteúdo inválido e velho.
            _stale = False
            _stat = None  # M11 (banca v15): stat falho deixava unbound
            _raw = ""     # BANCA-B5: read falho com ino=0 deixava unbound
            try:
                _stat = lock_path.stat()
                _raw = lock_path.read_text(encoding="utf-8", errors="replace")
                _pid = None
                if "=" in _raw:
                    try:
                        _pid = int(_raw.strip().split("=", 1)[-1])
                    except ValueError:
                        _pid = None
                elif _raw.strip().isdigit():
                    _pid = int(_raw.strip())
                if _pid is not None:
                    _stale = _pid != os.getpid() and not _pid_alive(_pid)
                else:
                    # vazio/imparseável jovem = lock recém-criado — espera
                    _stale = (time.time() - _stat.st_mtime) > 30
            except OSError:
                _stale = True
            if _stale:
                try:
                    _st2 = lock_path.stat()
                    # não deleta lock recriado por outro entre stat e unlink.
                    # M8 (banca v15): FS sem inode (FAT/WebDAV → st_ino=0)
                    # confundia locks distintos — compara conteúdo também.
                    # BANCA-B5: _stat None era dereferenciado ANTES do guard —
                    # stat falho + segundo stat ok = AttributeError no lock.
                    _same = (_stat is not None and (
                        (_st2.st_ino, _st2.st_ctime_ns, _st2.st_size)
                        == (_stat.st_ino, _stat.st_ctime_ns, _stat.st_size)))
                    if _same and _stat is not None and _stat.st_ino == 0:
                        _same = _same and (
                            lock_path.read_text(
                                encoding="utf-8", errors="replace") == _raw)
                    if _same:
                        lock_path.unlink()
                except OSError:
                    pass
                # banca-v18 (rev B-F2): `continue` aqui pulava o check de
                # deadline — lock stale irremovível (diretório, permissão)
                # girava o loop pra sempre ~100% CPU e TimeoutError nunca
                # disparava. O deadline precisa valer também neste caminho.
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timeout aguardando lock de escrita "
                        f"(stale irremovível): {lock_path}")
                time.sleep(poll)
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timeout aguardando lock de escrita: {lock_path}")
            time.sleep(poll)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        _our_ino = os.fstat(fd).st_ino
        yield
    finally:
        os.close(fd)
        try:
            # A1 v14 + M8 v15: unlink só se o inode é o nosso — em FS com
            # st_ino=0, compara o CONTEÚDO (pid=) como fallback.
            _cur = lock_path.stat().st_ino
            if _our_ino == 0:
                _mine = (lock_path.read_text(
                    encoding="utf-8", errors="replace").strip()
                    == f"pid={os.getpid()}")
            else:
                _mine = _cur == _our_ino
            if _mine:
                lock_path.unlink()
        except OSError:
            pass


def _repo_root_for_run(run_path: Path) -> Path | None:
    """Deriva a raiz do repo de `.henke/runs/<id>.json` (None fora desse layout)."""
    p = Path(run_path).resolve()
    try:
        if p.parent.name == "runs" and p.parent.parent.name == ".henke":
            return p.parent.parent.parent
    except (OSError, IndexError):
        pass
    return None


def _config_dir() -> Path:
    """Diretório de config LOCAL do usuário — fora do repo, fora do alcance
    de um driver/agente que só escreve na worktree. Aprovacões e âncoras de
    nascimento vivem aqui: um .henke/ plantado num clone não pode forjar o
    que nunca existiu na máquina da vítima."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    cfg = Path(base) / "henke"
    # M3 v13 + A7 v14: APPDATA=<repo>\.fake plantava âncoras/tokens DENTRO do
    # repo auditado. O check de CWD não bastava — rodando de subdir do repo,
    # o repo detectado (.git/.henke na ascendência do CWD) é a referência.
    try:
        cwd_r = Path.cwd().resolve()
        cfg_r = cfg.resolve()
        _repo = None
        for _up in (cwd_r, *cwd_r.parents):
            if (_up / ".git").exists() or (_up / ".henke").is_dir():
                _repo = _up
                break
        _poisoned = (
            cfg_r == cwd_r or cwd_r in cfg_r.parents
            or (_repo is not None and (cfg_r == _repo or _repo in cfg_r.parents))
        )
        if _poisoned:
            cfg = (Path.home() / "AppData" / "Roaming" / "henke") if os.name == "nt" else (Path.home() / ".config" / "henke")
            # M7 (banca v15): home é repo (dotfiles) ou HOME envenenado — o
            # "fallback" era o MESMO path dentro do repo. Re-valida.
            _cfg2 = cfg.resolve()
            if _repo is not None and (_cfg2 == _repo or _repo in _cfg2.parents):
                # fail-closed: usa tempdir do sistema, nunca dentro do repo
                # A2b (banca v16): getlogin() lança OSError sem TTY (CI/
                # container) — uid/env como fallback, nunca o cfg envenenado.
                try:
                    _who = os.getlogin()
                except OSError:
                    _who = (os.environ.get("USER") or os.environ.get("USERNAME")
                            or (str(os.getuid()) if hasattr(os, "getuid")
                                else str(os.getpid())))
                cfg = Path(tempfile.gettempdir()) / f"henke-{_who}"
    except OSError:
        pass
    return cfg


def _safe_run_id(run_id: str) -> str:
    """ASCII alfanumérico + `-_` — mesma forma que `init_run` grava.
    B2 (banca v13): `isalnum()` admitia Unicode (ſ/K/é/full-width) →
    homóglifos e colisões NTFS case-insensitive."""
    return "".join(c for c in str(run_id)
                   if c in "0123456789"
                   or "a" <= c <= "z" or "A" <= c <= "Z"
                   or c in ("-", "_")).strip()
