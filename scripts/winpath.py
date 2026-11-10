r"""Caminhos canônicos, contenção e varredura segura de árvores (Wave 7 — R7).

Primitivas de higiene de caminhos usadas por scanners, manifestos, quarentena
e watch, sem dependências fora da stdlib:

* ``canonical_path``: absolutização lexical idempotente (``abspath`` +
  ``normpath``), sem seguir symlinks/junctions/reparse points. No Windows,
  aplica prefixo estendido ``\\?\`` (drive ``C:\`` → ``\\?\C:\``, UNC
  ``\\srv\shr`` → ``\\?\UNC\srv\shr``) sem duplicar prefixo existente.
* ``is_reserved_component`` / ``assert_safe_relative``: rejeição de nomes
  reservados Win32 e de caminhos relativos inseguros.
* ``is_within``: contenção lexical com ``os.path.normcase``.
* ``iter_tree``: varredura determinística por ``os.scandir`` + fila BFS
  explícita (sem ``os.walk``/``rglob``), nunca atravessando reparse points.
* ``sha256_file``: hash em blocos com detecção de arquivo instável e
  rejeição de reparse points.

Decisões conservadoras além do mínimo do §3.2 (todas fail-closed):

* ``assert_safe_relative`` também rejeita ``:`` (drive/ADS), componentes
  ``.``, componentes em branco e caracteres de controle.
* ``canonical_path`` rejeita ``\\.\`` em qualquer plataforma e ``..`` que
  escape de ``base``; ``..`` que permanece contido é normalizado.
* ``is_reserved_component`` também limpa espaços/pontos antes do primeiro
  ponto (``"com1 .txt"`` é tratado como reservado, como o Win32 faz).
* ``iter_tree`` emite diretórios em ``max_depth`` como ``skipped`` com
  reason ``"max_depth"`` em vez de fingir que estão vazios, e recusa raiz
  que seja reparse point via ``PathSafetyError``.
* ``sha256_file`` recusa não-regulares (diretório/fifo) e usa ``O_NOFOLLOW``
  quando disponível como defesa anti-TOCTOU.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePath
import stat as stat_mod
import sys
from typing import Iterator, Literal

__all__ = [
    "PathSafetyError",
    "UnstableFileError",
    "TreeEntry",
    "FileDigest",
    "canonical_path",
    "is_reserved_component",
    "assert_safe_relative",
    "is_within",
    "iter_tree",
    "sha256_file",
]

_IS_WIN = sys.platform == "win32"

# FILE_ATTRIBUTE_REPARSE_POINT (Win32): marca symlinks, junctions e demais
# pontos de reanálise em DirEntry.stat(follow_symlinks=False).st_file_attributes.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_EXT_PREFIX = "\\\\?\\"  # \\?\
_EXT_UNC_PREFIX = "\\\\?\\UNC\\"  # \\?\UNC\


class PathSafetyError(ValueError):
    """Caminho viola a política de segurança (contenção, namespace, reservado)."""


class UnstableFileError(OSError):
    """Arquivo mudou (size/mtime_ns ou tipo) entre as medições do hash."""


@dataclass(frozen=True)
class TreeEntry:
    path: Path
    relative_path: str
    kind: Literal["file", "directory", "skipped", "error"]
    size: int
    mtime_ns: int
    reason: str = ""


@dataclass(frozen=True)
class FileDigest:
    sha256: str
    size: int
    mtime_ns: int


def _lstat(path):
    """Wrapper injetável de ``os.lstat`` (fault injection determinística)."""
    return os.lstat(path)


# --------------------------------------------------------------------------
# Nomes reservados Win32
# --------------------------------------------------------------------------

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def is_reserved_component(name: str) -> bool:
    """True se ``name`` é um nome reservado de dispositivo Win32.

    Comparação case-insensitive após remover espaços e pontos finais e
    considerar o texto antes do primeiro ponto (superset conservador: também
    limpa espaços/pontos imediatamente antes desse ponto, como o Win32 faz).
    """
    if not isinstance(name, str):
        name = os.fspath(name)
    stem = name.rstrip(" .").split(".", 1)[0].rstrip(" .")
    if not stem:
        return False
    return stem.upper() in _RESERVED_NAMES


# --------------------------------------------------------------------------
# Caminho canônico
# --------------------------------------------------------------------------


def _is_abs_win32(s: str) -> bool:
    """True para ``C:\\x``, ``\\srv\\shr[\\x]`` e ``\\?\\...`` já despojado."""
    if s.startswith("\\\\"):
        return True
    drive, rest = os.path.splitdrive(s)
    return bool(drive) and rest.startswith("\\")


def _strip_extended(s: str) -> str:
    """Remove ``\\?\\`` / ``\\?\\UNC\\`` já presentes (idempotência)."""
    if s[:8].upper() == _EXT_UNC_PREFIX:
        return "\\\\" + s[8:]
    if s[:4] == _EXT_PREFIX:
        return s[4:]
    return s


def _add_extended(s: str) -> str:
    """Aplica ``\\?\\`` (drive) ou ``\\?\\UNC\\`` (UNC) a caminho absoluto."""
    if s.startswith("\\\\"):
        return _EXT_UNC_PREFIX + s[2:]
    return _EXT_PREFIX + s


def _within_str(root_s: str, cand_s: str, sep: str) -> bool:
    """Contenção lexical com normcase; raiz e candidato já normalizados."""
    root_n = os.path.normcase(root_s).rstrip(sep)
    cand_n = os.path.normcase(cand_s)
    return cand_n == root_n or cand_n.startswith(root_n + sep)


def _check_not_device_ns(raw: str) -> None:
    """Rejeita o namespace de dispositivo ``\\.\\`` (todas as plataformas)."""
    probe = raw.replace("/", "\\")
    if probe == "\\\\." or probe.startswith("\\\\.\\"):
        raise PathSafetyError(f"namespace de dispositivo '\\\\.\\' rejeitado: {raw!r}")


def _canonical_win32(raw: str, base_raw: "str | None", extended: bool) -> str:
    s = _strip_extended(raw.replace("/", "\\"))
    if base_raw is not None:
        b = _strip_extended(base_raw.replace("/", "\\"))
        if not _is_abs_win32(b):
            b = os.path.abspath(b)
        b = os.path.normpath(b)
        joined = os.path.normpath(s if _is_abs_win32(s) else os.path.join(b, s))
        if not _within_str(b, joined, "\\"):
            raise PathSafetyError(
                f"{raw!r} escapa da base {base_raw!r} ('..' fora da contenção)"
            )
        out = joined
    else:
        if not _is_abs_win32(s):
            raise PathSafetyError(f"caminho relativo exige 'base': {raw!r}")
        out = os.path.normpath(s)
    if extended:
        out = _add_extended(out)
    return out


def _canonical_posix(raw: str, base_raw: "str | None") -> str:
    if base_raw is not None:
        b = base_raw if os.path.isabs(base_raw) else os.path.abspath(base_raw)
        b = os.path.normpath(b)
        joined = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(b, raw))
        if not _within_str(b, joined, "/"):
            raise PathSafetyError(
                f"{raw!r} escapa da base {base_raw!r} ('..' fora da contenção)"
            )
        return joined
    if not os.path.isabs(raw):
        raise PathSafetyError(f"caminho relativo exige 'base': {raw!r}")
    return os.path.normpath(raw)


def canonical_path(
    path: Path | str, *, base: Path | None = None, extended: bool = True
) -> Path:
    """Absolutiza ``path`` lexicalmente, sem seguir links e sem exigir existência.

    - Com ``base``: resolve ``base / path`` e exige contenção (``..`` que
      escapa da base gera ``PathSafetyError``); ``path`` absoluto também é
      aceito, desde que permaneça dentro de ``base``.
    - Sem ``base``: exige ``path`` absoluto.
    - Windows: devolve ``\\?\\C:\\...`` ou ``\\?\\UNC\\...`` (idempotente;
      ``extended=False`` devolve a forma simples). Rejeita ``\\.\\``.
    - POSIX: devolve absoluto normalizado, sem prefixos Win32.
    """
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw:
        raise PathSafetyError("caminho vazio ou inválido")
    if "\x00" in raw:
        raise PathSafetyError("caminho contém byte NUL")
    base_raw: "str | None" = None
    if base is not None:
        base_raw = os.fspath(base)
        if not isinstance(base_raw, str) or not base_raw or "\x00" in base_raw:
            raise PathSafetyError("base vazia, inválida ou com NUL")
        _check_not_device_ns(base_raw)
    _check_not_device_ns(raw)
    if _IS_WIN:
        return Path(_canonical_win32(raw, base_raw, extended))
    return Path(_canonical_posix(raw, base_raw))


def assert_safe_relative(path: PurePath | str) -> str:
    """Valida um caminho relativo persistível e devolve a forma ``a/b/c``.

    Rejeita (``PathSafetyError``): caminhos absolutos (POSIX, drive Win32,
    UNC), ``..``, componentes vazios/``.``/em branco, ``:`` (drive/ADS),
    caracteres de controle e componentes reservados Win32. ``\\`` é
    normalizado para ``/`` antes da validação.
    """
    if isinstance(path, PurePath):
        text = path.as_posix()
    else:
        text = os.fspath(path)
        if not isinstance(text, str):
            raise PathSafetyError(f"tipo inválido: {type(path).__name__}")
        text = text.replace("\\", "/")
    if not text:
        raise PathSafetyError("caminho vazio")
    segs = text.split("/")
    for seg in segs:
        if seg == "" or seg == ".":
            raise PathSafetyError(
                f"componente vazio, '.' ou caminho absoluto em {text!r}"
            )
        if seg == "..":
            raise PathSafetyError(f"componente '..' em {text!r}")
        if seg.rstrip(" .") == "":
            raise PathSafetyError(f"componente em branco em {text!r}")
        if ":" in seg:
            raise PathSafetyError(f"':' (drive/ADS) em {text!r}")
        if any(ord(c) < 0x20 for c in seg):
            raise PathSafetyError(f"caractere de controle em {text!r}")
        if is_reserved_component(seg):
            raise PathSafetyError(f"componente reservado Win32 {seg!r} em {text!r}")
    return "/".join(segs)


def is_within(root: Path, candidate: Path) -> bool:
    """True se ``candidate`` está lexicalmente contido em ``root`` (inclusive).

    Ambos passam por ``canonical_path``; a comparação usa ``os.path.normcase``
    (case-insensitive no Windows). ``PathSafetyError`` propaga.
    """
    sep = "\\" if _IS_WIN else "/"
    root_s = str(canonical_path(root))
    cand_s = str(canonical_path(candidate))
    return _within_str(root_s, cand_s, sep)


# --------------------------------------------------------------------------
# Varredura segura de árvore
# --------------------------------------------------------------------------


def _st_is_reparse(st) -> bool:
    """True se o stat (sem seguir links) indica symlink/junction/reparse."""
    if _IS_WIN:
        return bool(
            getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        )
    return stat_mod.S_ISLNK(st.st_mode)


def _err_reason(exc: OSError) -> str:
    return f"{type(exc).__name__}: {exc}"


def is_reparse_path(path: Path) -> bool:
    """True se symlink/junction/reparse point — portável py3.11+
    (``Path.is_junction`` só existe em 3.12+). Em dúvida, True."""
    try:
        if path.is_symlink():
            return True
        st = path.lstat()
    except OSError:
        return True
    return bool(
        getattr(st, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def iter_tree(
    root: Path,
    *,
    prune_names: frozenset[str] = frozenset(),
    max_depth: int = 64,
) -> Iterator[TreeEntry]:
    """Varre ``root`` em BFS determinístico com ``os.scandir``, sem seguir links.

    - Ordem: filhos de cada diretório ordenados por ``name.casefold()`` e
      depois ``name``; diretórios visitados na ordem de descoberta.
    - Reparse points (symlink/junction) viram ``skipped``/``reparse_point``;
      nunca entram na fila — ciclos de junction terminam aqui.
    - Diretórios com nome em ``prune_names`` (case-insensitive) viram
      ``skipped``/``pruned`` e não são atravessados.
    - Profundidade: filhos do root têm depth 1; um diretório é atravessado
      somente se seu depth < ``max_depth``; no limite vira
      ``skipped``/``max_depth`` (nunca finge diretório vazio).
    - Falhas de leitura/desaparecimento viram ``error`` (consumidores de
      segurança tratam como exit 2, nunca como ausência de arquivo).
    - ``relative_path`` usa ``/`` e é relativo a ``root`` (``.`` na raiz em
      caso de erro). O próprio root não é emitido em caso de sucesso.
    """
    if max_depth < 1:
        raise ValueError("max_depth deve ser >= 1")
    root_s = os.fspath(root)
    try:
        st_root = _lstat(root_s)
    except OSError as exc:
        yield TreeEntry(Path(root_s), ".", "error", 0, 0, _err_reason(exc))
        return
    if _st_is_reparse(st_root):
        raise PathSafetyError(f"root é reparse point: {root_s!r}")
    if not stat_mod.S_ISDIR(st_root.st_mode):
        raise PathSafetyError(f"root não é diretório: {root_s!r}")

    prune_fold = frozenset(n.casefold() for n in prune_names)
    queue: deque[tuple[str, str, int]] = deque([(root_s, "", 0)])
    while queue:
        dir_s, rel_prefix, depth = queue.popleft()
        try:
            with os.scandir(dir_s) as it:
                entries = sorted(it, key=lambda e: (e.name.casefold(), e.name))
        except OSError as exc:
            yield TreeEntry(
                Path(dir_s), rel_prefix or ".", "error", 0, 0, _err_reason(exc)
            )
            continue
        for entry in entries:
            rel = entry.name if not rel_prefix else f"{rel_prefix}/{entry.name}"
            epath = Path(entry.path)
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                yield TreeEntry(epath, rel, "error", 0, 0, _err_reason(exc))
                continue
            if _st_is_reparse(st):
                yield TreeEntry(
                    epath, rel, "skipped", st.st_size, st.st_mtime_ns,
                    "reparse_point",
                )
                continue
            if stat_mod.S_ISDIR(st.st_mode):
                if entry.name in prune_names or entry.name.casefold() in prune_fold:
                    yield TreeEntry(
                        epath, rel, "skipped", 0, st.st_mtime_ns, "pruned"
                    )
                    continue
                child_depth = depth + 1
                if child_depth >= max_depth:
                    yield TreeEntry(
                        epath, rel, "skipped", 0, st.st_mtime_ns, "max_depth"
                    )
                    continue
                yield TreeEntry(epath, rel, "directory", 0, st.st_mtime_ns)
                queue.append((entry.path, rel, child_depth))
            elif stat_mod.S_ISREG(st.st_mode):
                yield TreeEntry(
                    epath, rel, "file", st.st_size, st.st_mtime_ns
                )
            else:
                yield TreeEntry(
                    epath, rel, "skipped", st.st_size, st.st_mtime_ns,
                    "special_file",
                )


# --------------------------------------------------------------------------
# Hash estável de arquivo
# --------------------------------------------------------------------------


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> FileDigest:
    """SHA-256 em blocos com verificação de estabilidade (size, mtime_ns).

    - Rejeita reparse points (``PathSafetyError``) e não-regulares.
    - Abre com ``O_RDONLY`` (+``O_BINARY`` no Windows, +``O_NOFOLLOW`` quando
      existir) e nunca devolve hash parcial: se ``(size, mtime_ns)`` ou o
      tipo mudar entre as medições, lança ``UnstableFileError``.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size deve ser > 0")
    path_s = os.fspath(path)
    st_before = _lstat(path_s)
    if _st_is_reparse(st_before):
        raise PathSafetyError(f"reparse point não é hasheado: {path_s!r}")
    if not stat_mod.S_ISREG(st_before.st_mode):
        raise PathSafetyError(f"não é arquivo regular: {path_s!r}")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    fd = os.open(path_s, flags)
    try:
        while True:
            chunk = os.read(fd, chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)

    st_after = _lstat(path_s)
    if _st_is_reparse(st_after) or not stat_mod.S_ISREG(st_after.st_mode):
        raise UnstableFileError(
            f"arquivo mudou de tipo durante a leitura: {path_s!r}"
        )
    if (st_after.st_size, st_after.st_mtime_ns) != (
        st_before.st_size,
        st_before.st_mtime_ns,
    ):
        raise UnstableFileError(
            f"arquivo instável durante a leitura: {path_s!r} "
            f"(size {st_before.st_size}->{st_after.st_size}, "
            f"mtime_ns {st_before.st_mtime_ns}->{st_after.st_mtime_ns})"
        )
    return FileDigest(
        sha256=digest.hexdigest(),
        size=st_before.st_size,
        mtime_ns=st_before.st_mtime_ns,
    )
