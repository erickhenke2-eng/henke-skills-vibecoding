"""rs_mutations.py — Transações de estado de run: init_run, record_gate, amend_scope, approve_run."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from henke.rs_custody import (
    _ANCHORED_FIELDS,
    _append_evidence_manifest,
    load_anchor_sidecar,
    write_anchor_sidecar,
    write_approval_token,
)
from henke.rs_git import (
    _git_head,
    _git_ignored,
    _git_ignored_hashes,
    _git_untracked,
)
from henke.rs_io import (
    _atomic_write_json,
    _file_lock,
    _now_iso,
    _repo_root_for_run,
    _safe_run_id,
)
from henke.rs_verify import derive_run_outcome


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
        except (ImportError, AttributeError, OSError) as _sd_exc:
            compute_spec_hash = None  # type: ignore[assignment]
            # P2: spec_sha256=None é ancoragem degradada — antes o swallow
            # silencioso deixava spec trocável entre init e approve sem
            # nenhum rastro. Avisa (o drift gate ainda cobre o conteúdo).
            if spec_path:
                print(f"AVISO: spec_drift indisponível ({_sd_exc}) — "
                      f"spec_sha256 não ancorado para '{spec_path}'.",
                      file=sys.stderr)
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
        "ignored_start_hashes": _git_ignored_hashes(repo_root),
        "untracked_start": _git_untracked(repo_root),
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
        # A12 (banca v16): hash aqui + read_bytes() na cópia = DUAS leituras
        # (wedge EVIDENCE-TAMPERED por TOCTOU). O sha sai do MESMO buffer
        # confinado — computado dentro do lock.
        evidence_sha = None

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
                    evidence_sha = hashlib.sha256(ev_bytes).hexdigest()
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
            if evidence_sha is None:
                # Já confinada (sem cópia): hash dos bytes atuais, uma leitura.
                evidence_sha = hashlib.sha256(
                    evidence_path.read_bytes()).hexdigest()
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
        _BINDING_KEYS = {"head", "scope_sha256", "driver", "run_id", "repo",
                         "bound_at", "dirty_start"}
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
