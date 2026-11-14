"""rs_custody.py — Custódia local fora do repo (~/.config/henke/): tokens, tips, manifestos e sidecars."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from henke.rs_io import (
    _atomic_write_json,
    _config_dir,
    _now_iso,
    _safe_run_id,
)

# Teto de tamanho para evidência de portão (re-hash no verify lê em chunks
# mas não há razão legítima para uma saída de coletor passar disso).
_EVIDENCE_MAX_BYTES = 64 * 1024 * 1024

# Campos de decisão do outcome que o driver pode adulterar no arquivo da run
# para lavar o próprio veredito (banca v7 — F-03): o sidecar guarda o valor
# de nascimento e verify_run_file re-ancora antes de confiar.
_ANCHORED_FIELDS = ("mode", "mandatory_gates", "requires_human_auth",
                    "gates_required", "driver", "spec_path", "ignored_start",
                    "untracked_start", "ignored_start_hashes",
                    "prompt", "route_snapshot")
# `approval_binding` fica de fora de propósito: approve_run o muta
# legitimamente (re-ancora head). O que o binding cobre (prompt/route/driver
# via scope_sha256) já está ancorado pelos campos acima — divergência neles é
# detectada antes do dispatch, não só no verify pós-execução.


# Eixo 1 (aprovação humana) usa o campo `human_approval`; o eixo 2 (revisão
# independente Goldfish) usa `review_status`. Runs legadas guardavam a
# aprovação em `review_status` — o fallback preserva a leitura delas.
_APPROVAL_VOCAB = frozenset({"pending", "approved", "not_required"})


def human_approval_of(run_data: dict) -> str:
    """Estado do eixo de aprovação humana da run.

    `human_approval` prevalece; na ausência dele, aceita `review_status`
    somente quando o valor pertence ao vocabulário de aprovação (legado).
    Vocabulário Goldfish nunca vaza para o eixo humano."""
    if isinstance(run_data, dict):
        ha = run_data.get("human_approval")
        if isinstance(ha, str) and ha in _APPROVAL_VOCAB:
            return ha
        rs = run_data.get("review_status")
        if isinstance(rs, str) and rs in _APPROVAL_VOCAB:
            return rs
    return "not_required"


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


# Cache por processo: o root commit não muda dentro de uma execução, e cada
# lookup de âncora spawnaria um `git rev-list` — O(1) depois do primeiro.
_REPO_IDENTITY_CACHE: dict[str, str | None] = {}


def repo_identity(repo_root: Path | str | None) -> str | None:
    """Identidade de custódia por CONTEÚDO (F-06), não por caminho absoluto.

    Ordem: (1) root commit git — uma cópia isolada congelada (PROTOCOLO §5/§6)
    compartilha histórico e herda as âncoras locais legítimas; (2) sha256 de
    `.henke/CONFIG.json` (repo sem git já inicializado); (3) None — o caller
    cai no caminho resolvido como último recurso. O caminho era apenas proxy:
    duas árvores com o mesmo root commit são a mesma linhagem de projeto."""
    if repo_root is None:
        return None
    try:
        root = Path(repo_root).resolve()
    except (OSError, TypeError, ValueError):
        return None
    _cache_key = str(root)
    if _cache_key in _REPO_IDENTITY_CACHE:
        return _REPO_IDENTITY_CACHE[_cache_key]

    def _compute() -> str | None:
        try:
            from henke.rs_git import _git_root_commit
        except ImportError:
            try:
                from rs_git import _git_root_commit  # type: ignore[no-redef]
            except ImportError:
                _git_root_commit = None  # type: ignore[assignment]
        if _git_root_commit is not None:
            rc = _git_root_commit(root)
            if rc:
                return f"git:{rc}"
        try:
            cfg = root / ".henke" / "CONFIG.json"
            if cfg.is_file():
                # identidade_repo (nonce gravado no init) é estável a edições
                # de stack e único por repo — templates idênticos deixam de
                # colidir; fallback legado: hash do arquivo (instável a edição,
                # colide entre clones do mesmo stub — residual documentado).
                try:
                    _cfg_d = json.loads(cfg.read_text(encoding="utf-8"))
                    _nonce = _cfg_d.get("identidade_repo") if isinstance(_cfg_d, dict) else None
                    if isinstance(_nonce, str) and len(_nonce) >= 8:
                        return "cfg:" + _nonce
                except (ValueError, OSError):
                    pass
                return "cfg:" + hashlib.sha256(cfg.read_bytes()).hexdigest()
        except OSError:
            pass
        return None

    ident = _compute()
    # Só cacheia identidade git: (imutável — root commit nunca muda). cfg:/None
    # dependem de arquivo que pode surgir ou mudar dentro do processo (primeiro
    # commit, init de .henke): cachear tornaria o resultado stale.
    if ident and ident.startswith("git:"):
        _REPO_IDENTITY_CACHE[_cache_key] = ident
    return ident


def _repo_anchor_key(repo_root: Path | str) -> str:
    """Chave de âncora: identidade de conteúdo quando derivável; caminho
    resolvido como último recurso (diretório sem git e sem CONFIG)."""
    ident = repo_identity(repo_root)
    if ident is not None:
        return hashlib.sha256(ident.encode("utf-8")).hexdigest()[:32]
    return _legacy_repo_key(repo_root)


def _legacy_repo_key(repo_root: Path | str) -> str:
    """Chave histórica (caminho absoluto) — usada só para LER âncoras
    gravadas antes do F-06 e para dual-write de migração."""
    return hashlib.sha256(str(Path(repo_root).resolve()).encode("utf-8")).hexdigest()[:32]


def _exec_tip_path(repo_root: Path | str) -> Path:
    """Âncora LOCAL da ponta do exec_log: a cadeia de hashes prova só
    consistência interna — truncar/reescrever o rabinho produz cadeia válida.
    A ponta gravada fora do repo é a referência que a reescrita não alcança
    (F-13 banca v7). Chaveada por identidade de conteúdo (F-06)."""
    return _config_dir() / "exec-tips" / f"{_repo_anchor_key(repo_root)}.json"


def _exec_tip_path_legacy(repo_root: Path | str) -> Path:
    return _config_dir() / "exec-tips" / f"{_legacy_repo_key(repo_root)}.json"


def record_exec_tip(repo_root: Path | str, tip: str) -> None:
    # F-06: dual-write durante a migração — a chave por identidade serve
    # cópias isoladas; a chave legada por caminho mantém leitores antigos
    # consistentes até a transição completar.
    payload = {
        "repo": str(Path(repo_root).resolve()),
        "repo_identity": repo_identity(repo_root),
        "tip": tip,
        "updated_at": _now_iso(),
    }
    for p in {_exec_tip_path(repo_root), _exec_tip_path_legacy(repo_root)}:
        p.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(p, dict(payload))


def load_exec_tip(repo_root: Path | str) -> str | None:
    for p in (_exec_tip_path(repo_root), _exec_tip_path_legacy(repo_root)):
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


def _anchor_filename_candidates(safe: str, repo_root: Path | str | None) -> list[str]:
    """Nomes candidatos do sidecar: identidade de conteúdo primeiro (F-06),
    tag legada por caminho depois (migração), run puro por último."""
    names: list[str] = []
    if repo_root is not None:
        try:
            ident = repo_identity(repo_root)
            if ident is not None:
                names.append(
                    f"{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:12]}-{safe}.json")
            names.append(
                f"{hashlib.sha256(str(Path(repo_root).resolve()).encode('utf-8')).hexdigest()[:12]}"
                f"-{safe}.json")
        except OSError:
            pass
    names.append(f"{safe}.json")
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def _anchor_filename(safe: str, repo_root: Path | str | None) -> str:
    """Nome preferido do sidecar (identidade de conteúdo + run_id)."""
    return _anchor_filename_candidates(safe, repo_root)[0]


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
            "repo_identity": repo_identity(repo_root),
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
    p = next(
        (anchors_p for anchors_p in
         (_config_dir() / "anchors" / n for n in _anchor_filename_candidates(safe, repo_root))
         if anchors_p.is_file()),
        _config_dir() / "anchors" / _anchor_filename(safe, repo_root),
    )
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


def load_anchor_sidecar(run_id: str, repo_root: Path | str | None = None) -> dict | None:
    """Lê o sidecar local de âncoras (None se ausente/inválido/de outro repo).
    `repo_root` fornecido + sidecar com `repo` divergente → None (replay
    cross-repo não vale como âncora)."""
    safe = _safe_run_id(run_id)
    if not safe:
        return None
    anchors_d = _config_dir() / "anchors"
    p = next(
        (cand for cand in
         (anchors_d / n for n in _anchor_filename_candidates(safe, repo_root))
         if cand.is_file()),
        anchors_d / _anchor_filename(safe, repo_root),
    )
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
        _sc_ident = data.get("repo_identity")
        if _sc_ident:
            # F-06: identidade de conteúdo decide — cópia isolada congelada
            # da mesma linhagem compartilha o root commit e herda a âncora.
            if _sc_ident != repo_identity(repo_root):
                return None
        else:
            # Legado (sem repo_identity): caminho idêntico, ou caminho gravado
            # ainda acessível com identidade idêntica, provam o mesmo repo.
            _sc_repo = data.get("repo")
            if not _sc_repo:
                return None
            try:
                if str(Path(repo_root).resolve()) != str(_sc_repo):
                    _alt = repo_identity(Path(_sc_repo))
                    if _alt is None or _alt != repo_identity(repo_root):
                        return None
            except (OSError, TypeError, ValueError):
                # TypeError/ValueError: sidecar com "repo" truthy não-string
                # (123, [], {}) — inválido, nunca prova de custódia.
                return None
    return data
