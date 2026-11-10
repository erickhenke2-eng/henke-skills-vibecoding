#!/usr/bin/env python3
"""
run_state.py — Máquina de Estados determinística de execução (.henke/runs/<run_id>.json).

Implementa o contrato mecânico do PROTOCOLO v4.0 (§2):
- `outcome` ('verified' | 'failed' | 'blocked') é SEMPRE derivado dos portões executados,
  das emendas de escopo justificadas e do status de aprovação humana.
- Rejeita `outcome: "verified"` declarado manualmente quando a derivação mecânica
  diverge (`STATE-DERIVED-CONFLICT`).
- Todas as mutações de estado usam escrita atômica (`.tmp` + `os.replace`).

Códigos de saída:
  0  operação concluída / run verificada com sucesso
  1  conflito de estado derivado, portão reprovado ou emenda sem justificativa
  2  erro de uso, arquivo inexistente ou JSON malformado
"""
from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
from pathlib import Path
import subprocess
import sys
import tempfile
import time

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass


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
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            # M1 (banca v15): ERROR_ACCESS_DENIED(5) = processo existe mas
            # sem acesso — VIVO, não stale. 87 (param inválido) = morto.
            return ctypes.GetLastError() == 5
        except (AttributeError, OSError, TypeError, ValueError):
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
                    _same = ((_st2.st_ino, _st2.st_ctime_ns, _st2.st_size)
                             == (_stat.st_ino, _stat.st_ctime_ns, _stat.st_size))
                    if _stat is not None and _stat.st_ino == 0:
                        _same = _same and (
                            lock_path.read_text(
                                encoding="utf-8", errors="replace") == _raw)
                    if _stat is not None and _same:
                        lock_path.unlink()
                except OSError:
                    pass
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
                cfg = Path(tempfile.gettempdir()) / f"henke-{os.getlogin() if os.name != 'nt' else 'u'}"
    except OSError:
        pass
    return cfg


def _binding_token(binding: dict) -> str:
    """Token determinístico do vínculo de aprovação: sha256 do binding
    canônico (head + scope_sha256 + driver + run_id). run_id impede que duas
    runs com binding idêntico compartilhem o mesmo comprovante local. O token
    é a CHAVE do arquivo de aprovação local — só existe se `henke approve`
    rodou naquela máquina sobre aquele exato binding."""
    canon = json.dumps(
        {
            "head": binding.get("head"),
            "scope_sha256": binding.get("scope_sha256"),
            "driver": binding.get("driver"),
            "run_id": binding.get("run_id"),
            "repo": binding.get("repo"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def write_approval_token(binding: dict, approver: str, run_path: Path) -> Path | None:
    """Grava o comprovante LOCAL de que um humano aprovou este binding nesta
    máquina (`~/.config/henke/approvals/<token>.json`). Uma run plantada por
    clone com binding forjado não encontra token local → --resume bloqueia."""
    try:
        d = _config_dir() / "approvals"
        d.mkdir(parents=True, exist_ok=True)
        token = _binding_token(binding)
        p = d / f"{token}.json"
        _atomic_write_json(p, {
            "binding": {
                "head": binding.get("head"),
                "scope_sha256": binding.get("scope_sha256"),
                "driver": binding.get("driver"),
                "run_id": binding.get("run_id"),
                "repo": binding.get("repo"),
            },
            "approved_by": approver,
            "approved_at": _now_iso(),
            "run_file": str(run_path),
        })
        return p
    except OSError:
        return None


def _exec_tip_path(repo_root: Path | str) -> Path:
    """Âncora LOCAL da ponta do exec_log: a cadeia de hashes prova só
    consistência interna — truncar/reescrever o rabinho produz cadeia válida.
    A ponta gravada fora do repo é a referência que a reescrita não alcança
    (F-13 banca v7)."""
    repo_key = hashlib.sha256(str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:32]
    return _config_dir() / "exec-tips" / f"{repo_key}.json"


def record_exec_tip(repo_root: Path | str, tip: str) -> None:
    p = _exec_tip_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(p, {
        "repo": str(Path(repo_root).resolve()),
        "tip": tip,
        "updated_at": _now_iso(),
    })


def load_exec_tip(repo_root: Path | str) -> str | None:
    p = _exec_tip_path(repo_root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return str(data.get("tip")) if data.get("tip") else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def verify_approval_token(run_data: dict, repo_root: Path | str | None = None) -> tuple[bool, str]:
    """Confere se existe token LOCAL para o approval_binding desta run.
    Sem binding → nada a verificar (True). Com binding → exige o arquivo de
    aprovação local; sua ausência prova que `henke approve` nunca rodou aqui."""
    binding = run_data.get("approval_binding")
    if not isinstance(binding, dict):
        return True, ""
    # M1 v13 + A9 v14: binding["repo"] deve ser ESTE repo — e é OBRIGATÓRIO:
    # binding sem repo mintava token global válido em qualquer worktree.
    if repo_root is not None:
        _b_repo = binding.get("repo")
        try:
            _mismatch = (not isinstance(_b_repo, str) or not _b_repo.strip()
                         or str(Path(repo_root).resolve()) != _b_repo)
        except OSError:
            _mismatch = True
        if _mismatch:
            return False, (
                "APPROVAL-BINDING-REPO-MISMATCH: binding pertence a outro "
                "repositório — aprovação não é transferível")
    # APPROVAL-BINDING-REPLAY (banca v11 A2): o token deriva do CONTEÚDO do
    # binding — copiar o dict inteiro de uma run aprovada carrega o run_id
    # junto e replaya a aprovação. O binding deve pertencer a ESTA run.
    _carrier = _safe_run_id(str(run_data.get("run_id") or ""))
    if binding.get("run_id") != _carrier or not _carrier:
        return False, (
            "APPROVAL-BINDING-RUN-MISMATCH: binding pertence a outra run — "
            "aprovação não é transferível"
        )
    token = _binding_token(binding)
    p = _config_dir() / "approvals" / f"{token}.json"
    if not p.is_file():
        return False, (
            "APPROVAL-TOKEN-MISSING: nenhuma aprovação local para este binding — "
            "a run pode ter sido plantada/forjada; rode `henke approve` nesta máquina"
        )
    try:
        _raw = json.loads(p.read_text(encoding="utf-8"))
        stored = _raw.get("binding") if isinstance(_raw, dict) else None
        stored = stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        return False, "APPROVAL-TOKEN-CORRUPT: token local ilegível"
    want = {"head": binding.get("head"), "scope_sha256": binding.get("scope_sha256"),
            "driver": binding.get("driver"), "run_id": binding.get("run_id"),
            "repo": binding.get("repo")}
    if stored != want:
        return False, "APPROVAL-TOKEN-MISMATCH: token local pertence a outro binding"
    return True, ""


def _safe_run_id(run_id: str) -> str:
    """ASCII alfanumérico + `-_` — mesma forma que `init_run` grava.
    B2 (banca v13): `isalnum()` admitia Unicode (ſ/K/é/full-width) →
    homóglifos e colisões NTFS case-insensitive."""
    return "".join(c for c in str(run_id)
                   if c in "0123456789"
                   or "a" <= c <= "z" or "A" <= c <= "Z"
                   or c in ("-", "_")).strip()


# Teto de tamanho para evidência de portão (re-hash no verify lê em chunks
# mas não há razão legítima para uma saída de coletor passar disso).
_EVIDENCE_MAX_BYTES = 64 * 1024 * 1024


def _evidence_manifest_path(run_id: str, run_path: Path | None = None) -> Path:
    safe = _safe_run_id(run_id) or "run"
    # A3 (banca v10): chavear só por run_id colidia cross-repo e deixava
    # `record-gate` num path arbitrário envenenar o manifesto da run
    # homônima — o arquivo agora também carrega o hash do repo que contém
    # a run (.henke/runs/x.json → repo = 3 níveis acima).
    if run_path is not None:
        # A7 (banca v11): path raso/symlink sem parents[2] NÃO pode cair na
        # chave nua (namespace global de manifesto) — deriva a tag do path
        # inteiro, mantendo o isolamento mesmo no caso degenerado.
        rp = Path(run_path)
        try:
            rp = rp.resolve()
            repo = rp.parents[2]
        except (IndexError, OSError):
            repo = None
        tag_src = str(repo) if repo is not None else str(rp)
        tag = hashlib.sha256(tag_src.encode("utf-8")).hexdigest()[:12]
        safe = f"{tag}-{safe}"
    return _config_dir() / "evidence-manifest" / f"{safe}.json"


def _append_evidence_manifest(run_id: str, gate: str,
                              evidence_file: str | None,
                              evidence_sha256: str | None, command: str | None,
                              exit_code: int, manual: bool,
                              external: bool = False,
                              run_path: Path | None = None) -> None:
    """Acrescenta o veredito ao manifesto LOCAL (~/.config/henke/evidence-manifest/).

    A tríade gravada na run prova "existiu um arquivo com este sha"; o manifesto
    prova "foi `record_gate` DESTA máquina que registrou" — e agora ancora
    também o `exit_code` (banca v9 — C1: o bit que decide o veredito era
    texto livre no JSON mutável) e cobre TODA gravação, inclusive manual
    (C2: deletar um portão vermelho do JSON era indetectável — o manifesto
    passa a exigir manifest ⊆ run.gates com campos iguais).
    """
    p = _evidence_manifest_path(run_id, run_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("run_id") != run_id:
            data = {"run_id": run_id, "entries": {}}
    except (OSError, ValueError):
        data = {"run_id": run_id, "entries": {}}
    entries = data.setdefault("entries", {})
    if not isinstance(entries, dict):
        entries = {}
        data["entries"] = entries
    entry: dict = {
        "exit_code": int(exit_code),
        "manual": bool(manual),
        "external": bool(external),
        "recorded_at": _now_iso(),
    }
    if not manual:
        entry["evidence_file"] = evidence_file
        entry["evidence_sha256"] = evidence_sha256
        entry["command"] = command
    entries[gate] = entry
    _atomic_write_json(p, data)


def _load_evidence_manifest(run_id: str, run_path: Path | None = None) -> dict | None:
    """Lê o manifesto LOCAL de evidência (None se ausente/inválido)."""
    p = _evidence_manifest_path(run_id, run_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("run_id") != run_id:
        return None
    entries = data.get("entries")
    return data if isinstance(entries, dict) else None


# Campos de decisão do outcome que o driver pode adulterar no arquivo da run
# para lavar o próprio veredito (banca v7 — F-03): o sidecar guarda o valor
# de nascimento e verify_run_file re-ancora antes de confiar.
_ANCHORED_FIELDS = ("mode", "mandatory_gates", "requires_human_auth",
                    "gates_required", "driver", "spec_path", "ignored_start",
                    "prompt", "route_snapshot")
# `approval_binding` fica de fora de propósito: approve_run o muta
# legitimamente (re-ancora head). O que o binding cobre (prompt/route/driver
# via scope_sha256) já está ancorado pelos campos acima — divergência neles é
# detectada antes do dispatch, não só no verify pós-execução.


def write_anchor_sidecar(run_id: str, spec_path: str | None,
                         spec_sha256: str | None, head_start: str | None,
                         repo_root: Path | str | None = None,
                         fields: dict | None = None) -> Path | None:
    """Cópia LOCAL das âncoras de nascimento da run (fora do repo). O driver
    pode editar .henke/runs/<id>.json e mover head_start/spec_sha256 — o
    sidecar fora do alcance dele é a fonte preferida de run_gates. `repo`
    impede replay cross-repo: sidecar de run_id idêntico em OUTRO repositório
    não é aceito como âncora desta (M2). `fields` ancora os campos de decisão
    (mode/mandatory_gates/requires_human_auth/gates_required) contra a
    adulteração mid-run do arquivo (F-03)."""
    safe = _safe_run_id(run_id)
    if not safe:
        return None
    try:
        d = _config_dir() / "anchors"
        d.mkdir(parents=True, exist_ok=True)
        # M1 (banca v13): sidecar chaveado só por run_id — init_run(X) num
        # clone B clobberava a âncora do repo A e replayava aprovação.
        p = d / _anchor_filename(safe, repo_root)
        payload = {
            "run_id": safe,
            "repo": str(Path(repo_root).resolve()) if repo_root is not None else None,
            "spec_path": spec_path,
            "spec_sha256": spec_sha256,
            "head_start": head_start,
            "created_at": _now_iso(),
        }
        if isinstance(fields, dict):
            payload["fields"] = {
                k: fields.get(k) for k in _ANCHORED_FIELDS if k in fields
            }
        _atomic_write_json(p, payload)
        return p
    except OSError:
        return None


def amend_anchor_fields(run_id: str, repo_root: Path | str | None,
                        patch: dict) -> bool:
    """Mescla chaves em `fields` do sidecar EXISTENTE (nunca cria um novo —
    sidecar ausente = init_run não rodou aqui; criar seria forja).
    MÉDIO-1 (banca v13): `mandatory_gates` é patchada na run no resume mas
    ficava fora da âncora — o driver a deletava do JSON e os portões da
    matriz deixavam de ser exigidos exatamente nas runs sensíveis."""
    safe = _safe_run_id(run_id)
    if not safe or not isinstance(patch, dict):
        return False
    p = _config_dir() / "anchors" / _anchor_filename(safe, repo_root)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
        return False
    data["fields"].update(patch)
    try:
        _atomic_write_json(p, data)
        return True
    except OSError:
        return False


def _anchor_filename(safe: str, repo_root: Path | str | None) -> str:
    """Nome do sidecar: tag do repo + run_id (isolamento cross-repo)."""
    if repo_root is not None:
        try:
            tag = hashlib.sha256(
                str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:12]
            return f"{tag}-{safe}.json"
        except OSError:
            pass
    return f"{safe}.json"


def load_anchor_sidecar(run_id: str, repo_root: Path | str | None = None) -> dict | None:
    """Lê o sidecar local de âncoras (None se ausente/inválido/de outro repo).
    `repo_root` fornecido + sidecar com `repo` divergente → None (replay
    cross-repo não vale como âncora)."""
    safe = _safe_run_id(run_id)
    if not safe:
        return None
    anchors_d = _config_dir() / "anchors"
    p = anchors_d / _anchor_filename(safe, repo_root)
    if repo_root is None and not p.is_file():
        # Legado/fallback sem repo: aceita <tag>-<run>.json ou <run>.json.
        for cand in sorted(anchors_d.glob(f"*-{safe}.json")) + [anchors_d / f"{safe}.json"]:
            if cand.is_file():
                p = cand
                break
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("run_id") != safe:
        return None
    # A10 (banca v12): sidecar sem 'repo' era âncora-curinga — aceita por
    # qualquer repo_root. Quando o verificador informa repo, o campo é exigido.
    if repo_root is not None:
        _sc_repo = data.get("repo")
        if not _sc_repo:
            return None
        try:
            if str(Path(repo_root).resolve()) != str(_sc_repo):
                return None
        except OSError:
            return None
    return data


# A11 (banca v10): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG_* plantados
# no ambiente redirecionariam o git das âncoras/vereditos para outro repo/index.
# A4 (banca v14): env higienizado nos subprocessos git internos —
# PYTHONPATH/loader hooks subvertem tooling que produz evidência.
_GIT_ENV = {k: v for k, v in os.environ.items()
            if not (k.startswith("GIT_") or k.startswith("PYTHON")
                    or k.startswith("LD_") or k.startswith("DYLD_")
                    or k in ("BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS",
                             "PERL5OPT", "PERL5LIB", "RUBYOPT",
                             "DOTNET_STARTUP_HOOKS"))}


def _git_head(repo_root: Path) -> str | None:
    """HEAD atual do repo (None se git indisponível/sem commits — nunca inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, env=_GIT_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = (res.stdout or "").strip()
    return sha if res.returncode == 0 and sha else None


def _git_ignored(repo_root: Path) -> list[str] | None:
    """Arquivos ignorados presentes no worktree (status --ignored → '!!').
    Snapshot ancorado no init: escrita em path ignorado é invisível a TODOS
    os portões de diff — só a diferença de snapshots a expõe (banca v8 F2).
    None = git indisponível (auditoria fica sem baseline, não inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "status", "--porcelain", "-uall", "--ignored", "-z"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_GIT_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None
    return sorted(
        ent[3:].strip() for ent in (res.stdout or "").split("\0")
        if ent.startswith("!! ") and ent[3:].strip()
    )


def derive_run_outcome(run_data: dict) -> tuple[str, list[str]]:
    """Deriva mecanicamente ('verified' | 'failed' | 'blocked', reasons) a partir de run_data."""
    if not isinstance(run_data, dict):
        return "failed", ["INVALID-RUN-DATA: estrutura de run não é um objeto JSON"]

    reasons: list[str] = []

    # 1. Valida emendas de escopo antes de tudo (emenda sem justificativa invalida a run)
    amendments = run_data.get("scope_amendments", [])
    if not isinstance(amendments, list):
        return "failed", ["UNJUSTIFIED-SCOPE-AMENDMENT: scope_amendments deve ser uma lista"]
    for idx, item in enumerate(amendments):
        if not isinstance(item, dict):
            reasons.append(f"UNJUSTIFIED-SCOPE-AMENDMENT: item #{idx + 1} inválido")
            continue
        f_rel = str(item.get("file", "")).strip()
        rsn = str(item.get("reason", "")).strip()
        if not f_rel or len(rsn) < 5:
            reasons.append(
                f"UNJUSTIFIED-SCOPE-AMENDMENT: arquivo '{f_rel or '<vazio>'}' sem motivo válido (mínimo 5 caracteres)"
            )
    if reasons:
        return "failed", reasons

    # 2. Verifica execução física dos portões
    gates = run_data.get("gates", {})
    if not isinstance(gates, dict):
        return "blocked", ["NO-GATES-EXECUTED: 'gates' não é um objeto na run"]

    # Portão FÍSICO = tríade completa de evidência (command + evidence_file +
    # evidence_sha256). `manual: true` é atestação declarada; entrada sem tríade
    # e sem manual é atestação implícita — NENHUMA das duas conta como execução
    # provada (um JSON escrito à mão com exit_code:0 não vira verde).
    physical_gates: dict = {}
    manual_failures: list[str] = []
    for gate_name, gate_info in gates.items():
        if not isinstance(gate_info, dict):
            physical_gates[gate_name] = gate_info  # formato inválido → falha abaixo
            continue
        ec = gate_info.get("exit_code")
        has_triad = bool(
            gate_info.get("command")
            and gate_info.get("evidence_file")
            and gate_info.get("evidence_sha256")
        )
        if has_triad and gate_info.get("manual") is not True:
            physical_gates[gate_name] = gate_info
            continue
        # Atestação (manual:true ou sem tríade): exit 3 é n/a honesto; qualquer
        # outro != 0 reprova; exit 0 declaratório não conta como execução real.
        if isinstance(ec, int) and not isinstance(ec, bool) and ec not in (0, 3):
            manual_failures.append(
                f"GATE-FAILED: {gate_name} (exit {ec} — atestação sem evidência física)"
            )
        elif not isinstance(ec, int) or isinstance(ec, bool):
            # A1 (banca v10): exit_code adulterado para "2"/2.0/true era
            # DESCARTADO do cômputo — atestação de tipo confuso lava o vermelho.
            manual_failures.append(
                f"GATE-FAILED: {gate_name} (exit_code de tipo inválido {ec!r})"
            )

    if not physical_gates:
        if manual_failures:
            return "failed", manual_failures
        if gates:
            return "blocked", [
                "NO-GATES-EXECUTED: apenas atestações manuais (manual: true) na run — "
                "nenhum portão físico registrado"
            ]
        return "blocked", ["NO-GATES-EXECUTED: nenhum portão registrado na run"]

    failed_gates: list[str] = list(manual_failures)
    for gate_name, gate_info in physical_gates.items():
        if not isinstance(gate_info, dict):
            failed_gates.append(f"GATE-FAILED: {gate_name} (formato inválido)")
            continue
        ec = gate_info.get("exit_code")
        # exit 3 = NÃO-APLICÁVEL: o portão rodou e reportou "nada a verificar"
        # (veredito honesto com evidência) — não reprova, mas também não é verde.
        if isinstance(ec, bool) or not isinstance(ec, int) or ec not in (0, 3):
            failed_gates.append(f"GATE-FAILED: {gate_name} (exit {ec})")
    if failed_gates:
        return "failed", failed_gates

    # Driver simulado: cap de outcome derivável (echo/stdin nunca executam
    # processo real — o resultado é digitado/ecoado). Vem DEPOIS das falhas
    # (um vermelho continua 'failed', que é pior) e ANTES dos bloqueios —
    # para um driver simulado, 'unverified' é o relato honesto de qualquer
    # cenário que não falhou explicitamente.
    if run_data.get("driver") in ("echo", "stdin"):
        return "unverified", [
            f"SIMULATED-DRIVER: driver '{run_data.get('driver')}' não executa processo real — "
            "outcome máximo é 'unverified'"
        ]

    # Anti-tautologia: se TODO portão físico devolveu n/a (exit 3), nenhum check
    # real aconteceu — verified por vacuidade é o falso-verde que este guarda mata.
    real_greens = [
        g for g, g_info in physical_gates.items()
        if isinstance(g_info, dict) and g_info.get("exit_code") == 0
    ]
    if not real_greens:
        return "blocked", [
            "ALL-GATES-N/A: todos os portões físicos reportaram não-aplicável "
            "(exit 3) — nenhuma verificação real executada"
        ]

    # 2b. Verifica portões obrigatórios declarados na run (ou gates_required)
    # Portões externos (script fora da suíte — opt-in --gates) executam e
    # reportam, mas NUNCA satisfazem cobertura obrigatória (banca v9 — A4:
    # `--gates C:\evil\hazard_scan.py` mintava stem de mandatory).
    coverage_gates = {
        g: i for g, i in physical_gates.items()
        if isinstance(i, dict) and not i.get("external")
    }
    mandatory_gates = run_data.get("mandatory_gates") or run_data.get("gates_required")
    if mandatory_gates is not None and not isinstance(mandatory_gates, list):
        return "blocked", [
            f"MANDATORY-GATE-MISSING: 'mandatory_gates' malformado ({type(mandatory_gates).__name__}) — "
            "malformação endurece, nunca relaxa"
        ]
    if not mandatory_gates and str(run_data.get("mode", "")).strip().upper() == "EXECUTAR":
        # Fallback de portões obrigatórios para o modo EXECUTAR:
        # Exige ao menos os portões fundamentais executados (hazard_scan, assertless_scan, a11y_scan)
        # com exit code 0, barrando runs compostas unicamente por portões fictícios/dummy.
        core_fundamental = ("hazard_scan", "assertless_scan", "a11y_scan")
        executed_core = [
            g for g in core_fundamental
            if any(
                (g_name == g or Path(g_name).stem == g)
                and isinstance(g_info, dict)
                and g_info.get("exit_code") == 0
                for g_name, g_info in coverage_gates.items()
            )
        ]
        if not executed_core:
            return "blocked", [
                "MANDATORY-GATE-MISSING: nenhum dos portões fundamentais "
                "('hazard_scan', 'assertless_scan', 'a11y_scan') foi executado com sucesso"
            ]
        if run_data.get("spec_path"):
            spec_gates = ["spec_drift", "contract_closure"]
            missing_spec = [
                req for req in spec_gates
                if not any(
                    (g_name == req or Path(g_name).stem == req)
                    and isinstance(g_info, dict)
                    and g_info.get("exit_code") == 0
                    for g_name, g_info in coverage_gates.items()
                )
            ]
            if missing_spec:
                return "blocked", [
                    f"MANDATORY-GATE-MISSING: portão obrigatório de spec '{req}' não executado com sucesso"
                    for req in missing_spec
                ]
    elif mandatory_gates and isinstance(mandatory_gates, list):
        missing_mandatory: list[str] = []
        for req in mandatory_gates:
            # Normaliza por stem dos DOIS lados: 'hazard', 'hazard_scan.py' e
            # 'scripts/metricas/hazard_scan.py' devem casar o mesmo portão.
            req_stem = Path(str(req)).stem
            # exit 3 (n/a) conta como veredito produzido: o portão obrigatório
            # rodou e concluiu honestamente "nada a verificar" neste contexto.
            found = any(
                (g_name == req or Path(g_name).stem == req_stem)
                and isinstance(g_info, dict)
                and g_info.get("exit_code") in (0, 3)
                for g_name, g_info in coverage_gates.items()
            )
            if not found:
                missing_mandatory.append(
                    f"MANDATORY-GATE-MISSING: portão obrigatório '{req}' não executado com sucesso"
                )
        if missing_mandatory:
            return "blocked", missing_mandatory

    # 3. Verifica trava de autorização humana para operações sensíveis/PUBLISH
    if run_data.get("requires_human_auth") is True and run_data.get("review_status") != "approved":
        return "blocked", ["HUMAN-AUTH-REQUIRED: review_status pendente de aprovação humana"]

    return "verified", []


def verify_run_file(run_path: Path) -> tuple[bool, list[str]]:
    """Verifica um arquivo .henke/runs/<run_id>.json contra adulteração de estado e falhas."""
    # F4 (opus v15): `p` não-resolvido divergia do evidence_root (.resolve())
    # — path relativo marcava TODA run como EVIDENCE-UNANCHORED.
    p = Path(run_path).resolve()
    if not p.is_file():
        return False, [f"RUN-FILE-MISSING: arquivo não encontrado em '{p}'"]
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"RUN-JSON-INVALID: {exc}"]

    if not isinstance(data, dict):
        return False, ["RUN-JSON-INVALID: raiz do JSON deve ser um objeto"]

    declared = data.get("outcome")
    derived, reasons = derive_run_outcome(data)

    # F-03: campos de decisão (mode/mandatory_gates/requires_human_auth/
    # gates_required) podem ser regravados pelo driver no arquivo da run —
    # a derivação sobre eles lavaria o veredito. Sidecar local re-ancora.
    _sc_run_id = _safe_run_id(str(data.get("run_id") or p.stem))
    _sc = load_anchor_sidecar(_sc_run_id, repo_root=p.parent.parent.parent)
    _sc_fields = (_sc or {}).get("fields")
    if not (isinstance(_sc_fields, dict) and _sc_fields):
        if any(k in data for k in _ANCHORED_FIELDS):
            # F-06 (banca v8): sidecar ausente/corrompido/de outro repo não
            # pode silenciar a re-âncora — campos de decisão presentes sem
            # âncora local são falha fechada (o sidecar é escrito no init).
            return False, [
                "ANCHOR-SIDECAR-MISSING: run declara campos de decisão "
                "(mode/mandatory_gates/requires_human_auth) mas o sidecar "
                "local de âncoras está ausente — campos não-verificáveis"
            ]
    if isinstance(_sc_fields, dict) and _sc_fields:
        # A1 (banca v12): igualdade solta — `1 == True` desarmava o
        # tamper-check mas `1 is True` desarmava a derivação de auth.
        # Campos ancorados exigem identidade de TIPO+VALOR.
        _tampered_fields = [
            k for k, v in _sc_fields.items()
            if type(data.get(k)) is not type(v) or data.get(k) != v
        ]
        if _tampered_fields:
            return False, [
                "SIDECAR-FIELD-TAMPERED: campo(s) de decisão divergem do "
                "sidecar de nascimento (adulteração mid-run): "
                + ", ".join(sorted(_tampered_fields))
            ]
    # VERIFY-ANCHOR-PARTIAL (banca v11 A5): head_start/spec_sha256 vivem
    # top-level no sidecar, fora de `fields` — verify e o resumo do approve
    # confiavam no JSON mutável. Divergência = adulteração.
    if isinstance(_sc, dict):
        for _af in ("spec_sha256", "head_start"):
            if _af in _sc and data.get(_af) != _sc[_af]:
                return False, [
                    f"SIDECAR-FIELD-TAMPERED: '{_af}' da run diverge do "
                    "sidecar de nascimento (âncora adulterada)"
                ]

    # CONFLICT é bidirecional: declared=verified com derived≠verified é a forja
    # clássica; mas declared≠derived com derived=verified também é estado
    # contraditório (ex.: JSON editado para 'failed' sobre mecânica verde) —
    # silenciar qualquer divergência aceita arquivo incoerente.
    known = {"verified", "failed", "blocked", "unverified"}
    if declared is not None and not (isinstance(declared, str) and declared in known):
        # F-12 (banca v8): outcome fora do vocabulário ("hacked") com mecânica
        # verde passava — declared desconhecido é estado incoerente.
        return False, [f"OUTCOME-UNKNOWN: outcome declarado '{declared}' não "
                       "pertence a verified|failed|blocked|unverified"]
    if isinstance(declared, str) and declared in known and declared != derived:
        conflict_msg = (
            f"STATE-DERIVED-CONFLICT: outcome declarado '{declared}' "
            f"mas derivação mecânica resultou em '{derived}'"
        )
        return False, [conflict_msg] + reasons

    if derived != "verified":
        return False, reasons

    # Integridade retroativa da evidência física: o sha256 gravado na run deve
    # bater com o arquivo em disco AGORA — evidência reescrita após o record
    # torna a tríade decorativa se ninguém re-conferir. E a evidência TEM que
    # viver sob .henke/evidence/: sem confinamento, `evidence_file: README.md`
    # com o sha real do arquivo verificaria — a tríade provaria "o arquivo
    # existe", não "um portão produziu esta saída" (banca v6).
    tampered: list[str] = []
    raw_run_id = str(data.get("run_id") or p.stem)
    run_id = _safe_run_id(raw_run_id)
    if run_id != raw_run_id:
        # run_id com separadores/traversal fura o confinamento de evidência:
        # `evidence_root` abaixo concatenaria ".."/drive absoluto para fora
        # de .henke/evidence/ (banca v7 — C3).
        return False, [
            f"RUN-ID-INVALID: run_id '{raw_run_id}' contém caracteres "
            "que violam a forma segura ([A-Za-z0-9_-]) — confinamento de "
            "evidência não pode ser estabelecido"
        ]
    if run_id != p.stem:
        # F-16 (banca v8): run_id declarado divergente do nome do arquivo
        # deixava a.json reivindicar evidência/manifesto da run b —
        # sabotagem cross-run e replay de token de aprovação.
        return False, [
            f"RUN-ID-MISMATCH: run_id declarado '{run_id}' diverge do "
            f"arquivo '{p.stem}' — custódia é chaveada pelo nome do arquivo"
        ]
    # Confinamento a evidence/<run_id>/: evidência de OUTRA run não prova esta
    # (reuso cross-run), e o mesmo arquivo não pode ser saída de dois portões.
    evidence_root = (p.parent.parent / "evidence" / run_id).resolve()
    # M10 (banca v15): '.henke/evidence' symlink commitável redirecionava a
    # cópia de custódia para fora do repo — o resolve dos DOIS lados casava
    # e a evidência vivia fora da custódia.
    try:
        _ev_parent = (p.parent.parent / "evidence")
        if _ev_parent.is_symlink() or evidence_root.parent.parent != p.parent.parent:
            tampered.append(
                "EVIDENCE-UNANCHORED: '.henke/evidence' é symlink/junction "
                "ou resolve fora do repo — custódia inválida")
    except OSError:
        tampered.append("EVIDENCE-UNANCHORED: evidence não resolvível")
    # Manifesto LOCAL de vereditos: prova que `record_gate` DESTA máquina
    # registrou CADA entrada — run plantada por clone não tem manifesto.
    # Bidirecional (banca v9 — C1/C2): exit_code e flag manual ancorados,
    # e órfãos no manifesto = veredito deletado da run.
    manifest = _load_evidence_manifest(run_id, p)
    m_entries = (manifest or {}).get("entries") or {}
    run_gates = {
        k: v for k, v in (data.get("gates") or {}).items()
        if isinstance(v, dict)
    }
    for g_name in sorted(set(m_entries) - set(run_gates)):
        tampered.append(
            f"EVIDENCE-ORPHANED: {g_name} — gravação consta no manifesto "
            "local mas o portão foi apagado da run (veredito deletado "
            "não é invisível)"
        )
    seen_evidence: dict[str, str] = {}
    for g_name, g_info in run_gates.items():
        m = m_entries.get(g_name)
        if not isinstance(m, dict):
            tampered.append(
                f"GATE-UNMANIFESTED: {g_name} — veredito não consta no "
                "manifesto local (plantado ou gravado fora de record_gate)"
            )
            continue
        # A1 (banca v10): comparação por coerção int() deixava "2"==2 passar
        # enquanto o derive ignorava o valor não-int — veredito vermelho
        # lavável por confusão de tipo. Exige identidade tipo+valor.
        m_exit, r_exit = m.get("exit_code"), g_info.get("exit_code")
        if not (type(m_exit) is type(r_exit) and m_exit == r_exit):
            tampered.append(
                f"GATE-VERDICT-TAMPERED: {g_name} — exit_code na run "
                f"({g_info.get('exit_code')}) diverge do manifesto local "
                f"({m.get('exit_code')})"
            )
            continue
        # A6 (banca v10): `command` é texto forense — divergência do manifesto
        # = trilha adulterada (manifesto legado sem o campo não bloqueia).
        if m.get("command") is not None and m.get("command") != g_info.get("command"):
            tampered.append(
                f"GATE-COMMAND-TAMPERED: {g_name} — 'command' na run diverge "
                "do manifesto local"
            )
            continue
        if bool(m.get("manual")) != bool(g_info.get("manual")):
            tampered.append(
                f"GATE-MANUAL-FLIPPED: {g_name} — flag 'manual' diverge "
                "do manifesto local (físico↔manual não é flippável)"
            )
            continue
        if bool(m.get("external")) != bool(g_info.get("external")):
            tampered.append(
                f"GATE-EXTERNAL-FLIPPED: {g_name} — flag 'external' diverge "
                "do manifesto local (removê-la mintaria portão obrigatório)"
            )
            continue
        ev = g_info.get("evidence_file")
        sha = g_info.get("evidence_sha256")
        if m.get("manual"):
            if ev or sha:
                tampered.append(
                    f"EVIDENCE-IN-MANUAL: {g_name} — tríade em entrada "
                    "atestada manual (tríade física é domínio da pipeline)"
                )
            continue
        if not ev or not sha:
            tampered.append(
                f"EVIDENCE-MISSING: {g_name} — entrada física sem tríade "
                "na run (evidence_file/evidence_sha256 apagados)"
            )
            continue
        if (
            m.get("evidence_sha256") != sha
            or str(m.get("evidence_file") or "") != str(ev)
        ):
            tampered.append(
                f"EVIDENCE-UNMANIFESTED: {g_name} — tríade não consta no "
                "manifesto local (run plantada/forjada ou gravada fora de record_gate)"
            )
            continue
        ev_path = Path(str(ev))
        if not ev_path.is_absolute():
            ev_path = p.parent.parent.parent / ev  # relativo à raiz do repo
        try:
            resolved = ev_path.resolve()
        except Exception:
            tampered.append(f"EVIDENCE-INVALID: {g_name} → '{ev}'")
            continue
        if not (resolved == evidence_root or evidence_root in resolved.parents):
            tampered.append(
                f"EVIDENCE-OUTSIDE-SCOPE: {g_name} → '{ev}' fora de .henke/evidence/{run_id}/"
            )
            continue
        prev = seen_evidence.get(str(resolved))
        if prev is not None:
            tampered.append(
                f"EVIDENCE-REUSED: {g_name} e {prev} apontam o mesmo arquivo '{ev}'"
            )
            continue
        seen_evidence[str(resolved)] = g_name
        if not resolved.is_file():
            tampered.append(f"EVIDENCE-MISSING: {g_name} → '{ev}'")
            continue
        try:
            if resolved.stat().st_size > _EVIDENCE_MAX_BYTES:
                tampered.append(
                    f"EVIDENCE-OVERSIZE: {g_name} → '{ev}' excede "
                    f"{_EVIDENCE_MAX_BYTES} bytes"
                )
                continue
            h = hashlib.sha256()
            with open(resolved, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            actual = h.hexdigest()
        except OSError:
            tampered.append(f"EVIDENCE-MISSING: {g_name} → '{ev}' ilegível")
            continue
        if actual != sha:
            tampered.append(f"EVIDENCE-TAMPERED: {g_name} → sha diverge do gravado")
    if tampered:
        return False, tampered

    # Aprovação exige o token LOCAL também no verify — sem ele, uma run
    # plantada com review_status/approved_head forjados verificaria sem
    # nenhuma aprovação real nesta máquina (banca v7 — A4). Só quando a run
    # DECLARA aprovação: approval_binding existe em toda run fresca como
    # forward-binding de --resume e não pode exigir token por si.
    if (data.get("review_status") == "approved"
            or data.get("approved_head") or data.get("approved_at")):
        if not isinstance(data.get("approval_binding"), dict):
            # F-01 (banca v8): aprovação declarada SEM binding é vacuosa —
            # `henke approve` sempre grava binding; aprovação sem ele é forja.
            return False, [
                "APPROVAL-BINDING-MISSING: run declara aprovação sem "
                "approval_binding — `henke approve` nunca rodou nesta máquina"
            ]
        ok_tok, tok_msg = verify_approval_token(
            data, repo_root=_repo_root_for_run(run_path))
        if not ok_tok:
            return False, [tok_msg]

    return True, []


def init_run(
    repo_root: Path,
    run_id: str,
    mode: str = "EXECUTAR",
    archetype: str = "CARPINTEIRO",
    spec_path: str | None = None,
    requires_human_auth: bool = False,
    mandatory_gates: list[str] | None = None,
) -> Path:
    """Inicializa .henke/runs/<run_id>.json com escrita atômica."""
    safe_id = _safe_run_id(run_id)
    if not safe_id:
        raise ValueError("run_id inválido")
    run_path = Path(repo_root).resolve() / ".henke" / "runs" / f"{safe_id}.json"
    if run_path.is_file():
        raise FileExistsError(f"run já existe (não sobrescrevo silenciosamente): {run_path}")
    now = _now_iso()
    # Âncoras gravadas no NASCIMENTO da run: o diff e o hash da spec comparam
    # contra o estado que existia quando a run começou — nunca contra um arquivo
    # vivo que o próprio driver pode adulterar durante a execução.
    head_start = _git_head(repo_root)
    spec_sha256 = None
    if spec_path:
        # M4 (banca v15): `from spec_drift import` resolvia módulo PLANTADO
        # no CWD → spec_sha forjado ancorado. Carrega por path explícito.
        try:
            import importlib.util as _ilu
            _sd_p = Path(__file__).resolve().with_name("spec_drift.py")
            _spec = _ilu.spec_from_file_location(
                "henke_spec_drift", str(_sd_p))
            _sd_mod = _ilu.module_from_spec(_spec) if _spec else None
            if _sd_mod is not None:
                _spec.loader.exec_module(_sd_mod)  # type: ignore[union-attr]
            compute_spec_hash = getattr(_sd_mod, "compute_spec_hash", None)
        except Exception:
            compute_spec_hash = None  # type: ignore[assignment]
        if compute_spec_hash is not None:  # type: ignore[truthy-function]
            try:
                spec_sha256 = compute_spec_hash(Path(spec_path))
            except (OSError, UnicodeDecodeError):
                spec_sha256 = None
    data: dict = {
        "version": 1,
        "run_id": safe_id,
        "mode": mode,
        "archetype": archetype,
        "spec_path": spec_path,
        "spec_sha256": spec_sha256,
        "head_start": head_start,
        "ignored_start": _git_ignored(repo_root),
        "created_at": now,
        "updated_at": now,
        "requires_human_auth": bool(requires_human_auth),
        "review_status": "pending" if requires_human_auth else "not_required",
        "gates": {},
        "scope_amendments": [],
    }
    if mandatory_gates is not None:
        data["mandatory_gates"] = list(mandatory_gates)
    derived, reasons = derive_run_outcome(data)
    data["outcome"] = derived
    data["reasons"] = reasons
    # A8 (banca v10): check→write sem lock — dois inits concorrentes do mesmo
    # run_id clobberavam a run (e portões já gravados viravam órfãos).
    with _file_lock(run_path):
        if run_path.is_file():
            raise FileExistsError(
                f"run já existe (não sobrescrevo silenciosamente): {run_path}")
        _atomic_write_json(run_path, data)
    # Sidecar de âncoras para TODA run — não só `henke auto`: sem ele, run_gates
    # cai no fallback de ler head_start/spec_sha256 do arquivo de run mutável
    # (banca v7 — A3). `fields` ancora os campos de decisão do outcome (F-03).
    write_anchor_sidecar(safe_id, spec_path, spec_sha256, head_start,
                         repo_root=repo_root, fields=data)
    return run_path


def _load_run(run_path: Path) -> dict:
    p = Path(run_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Arquivo de run não encontrado: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Conteúdo da run não é um objeto JSON")
    return data


def record_gate(
    run_path: Path,
    gate_name: str,
    exit_code: int,
    summary: str = "",
    *,
    command: str | None = None,
    evidence_file: str | Path | None = None,
    manual: bool = False,
    external: bool = False,
) -> dict:
    """Registra o veredito de um portão na run e recalcula o outcome derivado.

    Evidência física: `command` + `evidence_file` (arquivo de saída do comando)
    gravam `command`/`evidence_file`/`evidence_sha256` — é o que prova execução
    real. `manual=True` marca `manual: true`: atestação que NUNCA conta como
    portão físico em `derive_run_outcome`. Chamadas de API sem evidência e sem
    `manual=True` gravam atestação implícita — `derive_run_outcome` exige a
    tríade para contar como físico, então tais entradas também não contam. A
    superfície CLI (`record-gate`) força `manual: true` nesse caso para deixar
    a natureza da entrada explícita no JSON.

    Transação leitura→escrita coberta por `_file_lock(run_path)`.
    """
    has_cmd = bool(command and str(command).strip())
    has_ev = evidence_file is not None and str(evidence_file).strip() != ""
    if has_cmd != has_ev:
        raise ValueError(
            "evidência incompleta: informe command E evidence_file juntos "
            "(ou nenhum dos dois para atestação manual)"
        )
    evidence_path: Path | None = None
    evidence_sha: str | None = None
    if has_cmd and has_ev:
        evidence_path = Path(str(evidence_file))
        if not evidence_path.is_file():
            raise FileNotFoundError(f"evidence-file inexistente: {evidence_path}")
        evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()

    with _file_lock(run_path):
        data = _load_run(run_path)
        gates = data.setdefault("gates", {})
        if not isinstance(gates, dict):
            gates = {}
            data["gates"] = gates
        if evidence_path is not None:
            # Custódia no record-time: se a evidência nasce fora do confinamento
            # ela é COPIADA para .henke/evidence/<run_id>/<gate>__<nome> — depois
            # de gravada, nem o driver nem ninguém a altera sem invalidar o
            # sha256 (a verificação re-hasheia o arquivo confinado).
            # Custódia chaveada pelo NOME DO ARQUIVO — data["run_id"] editável
            # não pode redirecionar evidência/manifesto para outra run (F-16).
            run_id = run_path.stem
            ev_root = (run_path.parent.parent / "evidence" / run_id).resolve()
            try:
                resolved = evidence_path.resolve()
            except Exception as exc:
                raise ValueError(f"evidence-file inválido: {exc}") from exc
            source_resolved = resolved
            if not (resolved == ev_root or ev_root in resolved.parents):
                ev_root.mkdir(parents=True, exist_ok=True)
                safe_gate = "".join(c if c.isalnum() or c in "._-" else "_" for c in gate_name)
                confined = (ev_root / f"{safe_gate}__{resolved.name}").resolve()
                if not (confined == ev_root or ev_root in confined.parents):
                    raise ValueError(f"evidence-file fora do confinamento: {evidence_path}")
                try:
                    # A10 (banca v11): uma leitura só — hashear e copiar em
                    # leituras separadas abria janela onde sha gravado ≠
                    # bytes confinados (wedge EVIDENCE-TAMPERED permanente).
                    ev_bytes = resolved.read_bytes()
                    confined.write_bytes(ev_bytes)
                except OSError as exc:
                    raise ValueError(f"falha ao confinar evidência: {exc}") from exc
                evidence_path = confined
            # Dedupe por FONTE: o mesmo arquivo de saída não pode servir de
            # evidência para dois portões distintos (anti-forja cross-gate).
            for other, o_info in gates.items():
                if (
                    other != gate_name
                    and isinstance(o_info, dict)
                    and o_info.get("evidence_source")
                    and str(o_info["evidence_source"]) == str(source_resolved)
                ):
                    raise ValueError(
                        f"mesmo arquivo-fonte já registrado pelo portão '{other}'"
                    )
        entry: dict = {
            "exit_code": int(exit_code),
            "summary": summary,
            "recorded_at": _now_iso(),
        }
        if external:
            # A4 (banca v9): script fora da suíte/pacote (opt-in --gates)
            # executa, mas seu veredito NUNCA satisfaz portão obrigatório —
            # o flag é ancorado no manifesto (removê-lo do JSON = mismatch).
            entry["external"] = True
        if evidence_sha is not None:
            entry["command"] = str(command)
            entry["evidence_file"] = str(evidence_path)
            entry["evidence_sha256"] = evidence_sha
            entry["evidence_source"] = str(source_resolved)
        if manual:
            # manual=True marca atestação MESMO com tríade presente — sem isso
            # a superfície CLI emitiria evidência "física" verificável (F-01).
            entry["manual"] = True
            existing = gates.get(gate_name)
            if isinstance(existing, dict) and existing.get("evidence_sha256"):
                # F-02 (banca v8): veredito físico existente não pode ser
                # apagado/regravado via CLI — record-gate manual sobre tríade
                # física apagaria um portão VERMELHO do cômputo derivado.
                raise ValueError(
                    f"portão '{gate_name}' já possui tríade física — "
                    "registro manual não pode sobrescrevê-la"
                )
        # Manifesto LOCAL cobre TODA gravação (C1+C2 banca v9): entradas
        # físicas trazem a tríade; manuais trazem só exit_code+manual — o
        # veredito deixa de ser texto livre e deletar um portão do JSON
        # deixa órfão detectável no manifesto (EVIDENCE-ORPHANED).
        try:
            _append_evidence_manifest(
                run_path.stem, gate_name,
                str(evidence_path) if evidence_sha is not None else None,
                evidence_sha,
                str(command) if evidence_sha is not None else None,
                int(exit_code), manual, external, run_path,
            )
        except OSError as exc:
            raise ValueError(
                f"falha ao gravar manifesto de evidência local: {exc}"
            ) from exc
        gates[gate_name] = entry
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
    return data


def amend_scope(run_path: Path, file_rel: str, reason: str) -> dict:
    """Adiciona uma emenda de escopo auditada à run e recalcula o outcome."""
    with _file_lock(run_path):
        data = _load_run(run_path)
        amendments = data.setdefault("scope_amendments", [])
        if not isinstance(amendments, list):
            amendments = []
            data["scope_amendments"] = amendments
        amendments.append({
            "file": file_rel.replace("\\", "/").strip(),
            "reason": reason.strip(),
            "amended_at": _now_iso(),
        })
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
    return data


def approve_run(run_path: Path, approver: str = "human",
                expected: dict | None = None,
                expected_head: str | None = None) -> dict:
    """Registra aprovação humana explícita para destravar runs com requires_human_auth.

    Grava `approved_head` (git rev-parse HEAD do repo da run, ou None) como
    evidência informativa — o vínculo executável (`approval_binding`) é criado
    no nascimento da run e checado por `henke auto --resume`.
    """
    with _file_lock(run_path):
        data = _load_run(run_path)
        # A3 (banca v12): TOCTOU da cerimônia — o resumo exibido vinha de uma
        # leitura e a aprovação de outra. O driver trocava run+sidecar na
        # janela humana. Quando o chamador exibe um resumo, os bytes aprovados
        # DEVEM ser os mesmos bytes exibidos.
        if expected is not None and data != expected:
            raise ValueError(
                "APPROVE-TOCTOU: a run mudou entre o resumo exibido e a "
                "gravação da aprovação — aprovação recusada")
        # A1 (banca v10): approve sem checagem = token sobre binding adulterado.
        # O fingerprint é determinístico (recomputável por quem edita o JSON) —
        # a prova real é a âncora LOCAL: campos de decisão divergentes do
        # sidecar = run adulterada, aprovação recusada antes do dispatch.
        # A3 (banca v11): sidecar ausente tornava o drift-check vacuoso —
        # run plantada à mão (nunca passou por init_run → sem sidecar) era
        # aprovável com o humano vendo só o resumo. Falha fechada quando a
        # run declara campos ancorados sem âncora local, e confinamento:
        # run fora de .henke/runs/ não tem repo resolvível.
        _rr = _repo_root_for_run(run_path)
        if _rr is None:
            raise ValueError(
                "APPROVE-UNANCHORED: run fora de .henke/runs/ não tem repo "
                "resolvível — recusado aprovar run sem custódia")
        _sc = load_anchor_sidecar(
            _safe_run_id(run_path.stem), repo_root=_rr)
        _sc_fields = (_sc or {}).get("fields") or {}
        # A9 (banca v14): run mínima sem campos ancorados contornava o
        # requisito de sidecar (`any()` False) — aprovação exige âncora
        # local, sempre (init_run sempre grava fields).
        if not _sc_fields:
            raise ValueError(
                "APPROVE-SIDECAR-MISSING: run sem sidecar local de âncora — "
                "aprovação recusada (init_run não rodou aqui?)")
        _drift = [f for f in _ANCHORED_FIELDS
                  if f in _sc_fields and (
                      type(_sc_fields[f]) is not type(data.get(f))
                      or _sc_fields[f] != data.get(f))]
        if _drift:
            raise ValueError(
                "ANCHOR-DRIFT: campos da run divergem do sidecar local "
                f"({', '.join(_drift)}) — aprovação recusada")
        # A2 (banca v11): binding com run_id de outra run replayaria o token.
        _b = data.get("approval_binding")
        # B3 (banca v13): binding ausente/não-dict → approve "OK" sem token
        # mintado — cerimônia vacuosa que o verify reprovaria para sempre.
        if not isinstance(_b, dict):
            raise ValueError(
                "APPROVAL-BINDING-MISSING: run sem approval_binding — "
                "nada a vincular; aprovação recusada")
        if _b.get("run_id") != _safe_run_id(run_path.stem):
            raise ValueError(
                "APPROVAL-BINDING-RUN-MISMATCH: binding não pertence a esta "
                "run — aprovação recusada")
        # M1 v13 + A9 v14: binding["repo"] obrigatório e deve ser ESTE repo.
        if (not isinstance(_b.get("repo"), str) or not _b["repo"].strip()
                or str(Path(_rr).resolve()) != _b["repo"]):
            raise ValueError(
                "APPROVAL-BINDING-REPO-MISMATCH: binding pertence a outro "
                "repositório — aprovação recusada")
        # B5 (banca v13): chaves extras do binding não eram exibidas no
        # resumo nem cobertas por token/fingerprint — whitelist fecha o schema.
        _BINDING_KEYS = {"head", "scope_sha256", "driver", "run_id", "repo", "bound_at"}
        data["approval_binding"] = {k: v for k, v in _b.items() if k in _BINDING_KEYS}
        data["review_status"] = "approved"
        data["approved_by"] = approver.strip() or "human"
        data["approved_at"] = _now_iso()
        repo_root = _repo_root_for_run(run_path)
        approved_head = _git_head(repo_root) if repo_root is not None else None
        # M4 (banca v13): resumo exibia um HEAD e o token ancora outro —
        # commit plantado na janela humano→enter entrava aprovado.
        if expected_head is not None and approved_head != expected_head:
            raise ValueError(
                "APPROVE-HEAD-TOCTOU: HEAD mudou entre o resumo exibido e a "
                "aprovação — recusado; releia o resumo e tente de novo")
        data["approved_head"] = approved_head
        # Re-ancora o vínculo executável ao HEAD que o humano está aprovando
        # AGORA — sem isso, um commit legítimo entre aprovação e --resume
        # tornava a run irrecuperável para sempre (approve→commit→resume→stale→loop).
        # `driver` NÃO é re-ancorado: re-ancorar ao valor possivelmente
        # adulterado lavaria a troca de driver (banca v10 A1) — o valor de
        # nascimento já está no sidecar; divergência aborta acima.
        if isinstance(data.get("approval_binding"), dict):
            data["approval_binding"]["head"] = approved_head
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
        # Comprovante LOCAL fora do repo: --resume exige este token — uma run
        # plantada via clone (binding forjado mas self-consistente) não tem
        # token na máquina da vítima e é recusada.
        if isinstance(data.get("approval_binding"), dict):
            # A9 (banca v10): falha silenciosa deixava approve "OK" sem token —
            # --resume recusava depois sem explicação.
            if write_approval_token(data["approval_binding"],
                                    data["approved_by"], run_path) is None:
                raise OSError("falha ao gravar o token de aprovação local")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gerenciador de estado determinístico de runs (.henke/runs/).")
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_init = sub.add_parser("init", help="Inicializa um novo arquivo de run")
    p_init.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório")
    p_init.add_argument("--run-id", required=True, help="Identificador único da run")
    p_init.add_argument("--mode", default="EXECUTAR", choices=("ANALISAR", "PLANEJAR", "EXECUTAR"))
    p_init.add_argument("--archetype", default="CARPINTEIRO", choices=("SHERLOCK", "ESTADISTA", "CARPINTEIRO"))
    p_init.add_argument("--spec", default=None, help="Caminho opcional da spec")
    p_init.add_argument("--requires-human-auth", action="store_true")
    p_init.add_argument("--mandatory-gates", nargs="*", default=None, help="Portões obrigatórios declarados")

    p_gate = sub.add_parser("record-gate", help="Registra execução de um portão na run")
    p_gate.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_gate.add_argument("--gate", required=True, help="Nome do portão")
    p_gate.add_argument("--exit-code", type=int, required=True, help="Código de saída do portão")
    p_gate.add_argument("--summary", default="", help="Resumo curto da saída")
    p_gate.add_argument("--command", default=None,
                        help="Comando real executado (evidência física do veredito)")
    p_gate.add_argument("--evidence-file", dest="evidence_file", type=Path, default=None,
                        help="Arquivo de saída do comando; sha256 é registrado como evidência")

    p_amend = sub.add_parser("amend-scope", help="Registra emenda de escopo justificada")
    p_amend.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_amend.add_argument("--file", required=True, help="Arquivo relativo adicionado ao escopo")
    p_amend.add_argument("--reason", required=True, help="Justificativa técnica (>= 5 caracteres)")

    p_app = sub.add_parser("approve", help="Aprova manualmente uma run pendente de autorização humana")
    p_app.add_argument("run_file", type=Path, help="Caminho para .henke/runs/<id>.json")
    p_app.add_argument("--approver", default="human", help="Identificador do aprovador humano")
    p_app.add_argument("--yes", "-y", action="store_true",
                       help="Aprova sem confirmação interativa (obrigatório quando stdin não é TTY)")

    p_ver = sub.add_parser("verify", help="Verifica integridade e outcome derivado de uma run")
    p_ver.add_argument("run_file", nargs="?", type=Path, default=None, help="Caminho para .henke/runs/<id>.json")
    p_ver.add_argument("--run", dest="run_opt", type=Path, default=None, help="Caminho para .henke/runs/<id>.json (alternativo)")
    p_ver.add_argument("--json", action="store_true", help="Saída em JSON")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        if args.subcmd == "init":
            out_path = init_run(
                args.repo,
                args.run_id,
                mode=args.mode,
                archetype=args.archetype,
                spec_path=args.spec,
                requires_human_auth=args.requires_human_auth,
                mandatory_gates=args.mandatory_gates,
            )
            print(f"OK: Run inicializada em {out_path}")
            return 0

        if args.subcmd == "record-gate":
            import re as _re
            if _re.match(r"^(?:stage_.+|tests|dispatch)$", args.gate or ""):
                print(
                    f"ERRO: nome de portão reservado à pipeline interna: '{args.gate}' "
                    "(stage_*/tests/dispatch só a própria pipeline grava; um CLI externo "
                    "forjando esses nomes fabricaria a narrativa da run).",
                    file=sys.stderr,
                )
                return 2
            if args.command or args.evidence_file is not None:
                # F-01+M2: a superfície CLI NUNCA carrega tríade — atestação
                # manual com evidence_* envenenava a run permanentemente
                # (EVIDENCE-UNMANIFESTED sem caminho de reparo). Evidência
                # física é domínio exclusivo da pipeline (record_gate via API).
                print(
                    "ERRO: --command/--evidence-file não são aceitos no CLI — "
                    "atestação manual não carrega tríade; evidência física só "
                    "a pipeline emite (record_gate via API).",
                    file=sys.stderr,
                )
                return 2
            data = record_gate(
                args.run_file, args.gate, args.exit_code, args.summary,
                manual=True,
            )
            print(f"OK: Gate '{args.gate}' (exit {args.exit_code}) registrado "
                  f"(atestação manual — não conta como portão físico) -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "amend-scope":
            data = amend_scope(args.run_file, args.file, args.reason)
            if data["outcome"] == "failed" and any("UNJUSTIFIED-SCOPE-AMENDMENT" in r for r in data.get("reasons", [])):
                for r in data["reasons"]:
                    print(f"  [RUN-ERROR] {r}", file=sys.stderr)
                return 1
            print(f"OK: Escopo emendado com '{args.file}' -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "approve":
            # Aprovação nunca é cega (F-05): mostra exatamente o que está
            # sendo autorizado — um repo plantado com run "pendente" +
            # aprovação social transformaria `cmd:evil` em RCE via --resume.
            _prev = _load_run(args.run_file)
            _binding = _prev.get("approval_binding") if isinstance(_prev.get("approval_binding"), dict) else {}
            print("=== RESUMO DA APROVAÇÃO ===")
            print(f"  run_id          : {_prev.get('run_id')}")
            print(f"  mode            : {_prev.get('mode')}")
            print(f"  driver          : {_prev.get('driver') or _binding.get('driver') or '(não registrado)'}")
            print(f"  prompt          : {str(_prev.get('prompt') or '(não registrado)')[:200]!r}")
            print(f"  scope_paths     : {_prev.get('scope_paths')}")
            print(f"  mandatory_gates : {_prev.get('mandatory_gates')}")
            print(f"  requires_human  : {_prev.get('requires_human_auth')}")
            # A2 (banca v9): âncoras do diff também entram no resumo — o
            # humano precisa ver contra qual HEAD/spec a aprovação se vincula.
            print(f"  head_start      : {_prev.get('head_start')}")
            print(f"  spec_sha256     : {str(_prev.get('spec_sha256'))[:16]}…")
            print(f"  spec_path       : {_prev.get('spec_path')}")
            # A7 (banca v14): test_cmd/policy efetivos alimentam o
            # scope_sha256 e executam sob o selo da aprovação — o resumo
            # precisa mostrar O QUE será executado, não só o escopo.
            print(f"  test_cmd        : {_prev.get('test_cmd') or '(nenhum)'}")
            _pol = _prev.get("policy_efetiva") or _prev.get("policy") or {}
            if isinstance(_pol, dict) and _pol:
                _pl = {k: _pol.get(k) for k in
                       ("policy", "allowed_cmds", "source") if k in _pol}
                print(f"  policy_efetiva  : {_pl}")
            # A3 (banca v12): o HEAD que será vinculado também entra no
            # resumo — approve_run ancora ao HEAD VIVO, que o driver pode
            # ter acabado de commitar sem o humano ver.
            try:
                _rr = _repo_root_for_run(args.run_file)
                _live_head = _git_head(_rr) if _rr else "(sem repo)"
            except Exception:
                _live_head = "(indisponível)"
            print(f"  approved_head   : {_live_head}  (HEAD vivo no momento do approve)")
            if not args.yes:
                if not sys.stdin.isatty():
                    print(
                        "ERRO: aprovação não-interativa exige --yes explícito "
                        "(releia o resumo acima e aprove conscientemente).",
                        file=sys.stderr,
                    )
                    return 2
                resp = input("Aprovar esta run? [yes/NO] ").strip().lower()
                if resp not in ("yes", "y"):
                    print("Aprovação abortada pelo operador.")
                    return 2
            data = approve_run(
                args.run_file, args.approver, expected=_prev,
                # M4 (banca v13): o HEAD exibido é o HEAD vinculado.
                expected_head=_live_head if isinstance(_live_head, str) and "(" not in _live_head else None)
            print(f"OK: Run aprovada por '{args.approver}' -> outcome='{data['outcome']}'")
            return 0

        if args.subcmd == "verify":
            run_target = args.run_opt or args.run_file
            if run_target is None:
                print("ERRO: informe o arquivo de run (posicional ou --run).", file=sys.stderr)
                return 2
            if not run_target.is_file():
                print(f"ERRO: arquivo de run inexistente: {run_target}", file=sys.stderr)
                return 2
            try:
                raw_json = json.loads(run_target.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"ERRO: JSON inválido em {run_target}: {exc}", file=sys.stderr)
                return 2
            ok, reasons = verify_run_file(run_target)
            derived, _ = derive_run_outcome(raw_json) if isinstance(raw_json, dict) else ("failed", [])
            if args.json:
                print(json.dumps({
                    "run_file": str(run_target),
                    "verified": ok,
                    "derived_outcome": derived,
                    "reasons": reasons,
                }, ensure_ascii=False, indent=2))
            else:
                if ok:
                    print(f"OK: Run '{run_target}' verificada com sucesso (outcome='verified').")
                else:
                    print(f"FALHA NA VERIFICAÇÃO DA RUN '{run_target}' (derived='{derived}'):", file=sys.stderr)
                    for r in reasons:
                        print(f"  [RUN-ERROR] {r}", file=sys.stderr)
            return 0 if ok else 1

    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    return 2


if __name__ == "__main__":
    sys.exit(main())
