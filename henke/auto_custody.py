"""auto_custody.py — Confinamento de custódia (.henke/), validação de --resume e nascimento/vínculo da run."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import uuid

from henke.auto_kernel import (
    _ANCHORED_FIELDS,
    _approval_extra,
    _approval_fingerprint,
    _git_head,
    _now_iso,
    _patch_run,
    _record_stage,
    _tracked_dirty_snapshot,
    amend_anchor_fields,
    init_run,
    load_anchor_sidecar,
    verify_approval_token,
    write_anchor_sidecar,
)


def _validate_resume(
    repo_root: Path,
    resume_ref: object,
    driver: str | None,
    project: dict,
    policy_efetiva: dict,
) -> tuple[Path | None, dict | None, int | None]:
    """Valida `--resume`: confinamento, aprovação, HEAD, fingerprint, dirty_start e token local.
    Retorna (cand, resumed, err_rc) — err_rc != None aborta a run."""
    # A5 (banca v14): .henke/.henke/runs junction fazia o cand.resolve()
    # casar com runs_dir resolvido pelo MESMO alvo — a run fora do repo
    # era lida/mutada antes dos checks de confinamento do estágio 2.
    _hd = repo_root / ".henke"
    if _hd.is_symlink() or (_hd / "runs").is_symlink():
        print("ERRO: '.henke'/'.henke/runs' é symlink/junction — resume "
              "recusado (custódia fora do repo).", file=sys.stderr)
        return None, None, 2
    runs_dir = (repo_root / ".henke" / "runs").resolve()
    cand = Path(str(resume_ref))
    cand = cand if cand.is_absolute() else runs_dir / (
        cand.name if cand.name.endswith(".json") else f"{cand.name}.json"
    )
    try:
        cand = cand.resolve()
        cand.relative_to(runs_dir)
    except (ValueError, OSError):
        print("ERRO: --resume aceita apenas runs dentro de .henke/runs/.", file=sys.stderr)
        return None, None, 2
    if not cand.is_file():
        print(f"ERRO: run inexistente: {cand}", file=sys.stderr)
        return None, None, 2
    try:
        resumed = json.loads(cand.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("ERRO: run ilegível/corrompida.", file=sys.stderr)
        return None, None, 2
    if resumed.get("review_status") != "approved":
        print("ERRO: run não aprovada. Rode primeiro:", file=sys.stderr)
        print(f"  henke approve {cand}", file=sys.stderr)
        return None, None, 2
    # Vínculo da aprovação: HEAD + escopo (prompt + route_snapshot) não
    # podem ter divergido desde a aprovação — divergência exige nova aprovação.
    snap = resumed.get("route_snapshot")
    binding = resumed.get("approval_binding")
    stale: list[str] = []
    fp_now: str | None = None
    if not isinstance(binding, dict):
        stale.append("run sem approval_binding (criada antes do vínculo de aprovação)")
    else:
        head_then, head_now = binding.get("head"), _git_head(repo_root)
        if head_then != head_now:
            stale.append(
                f"HEAD divergiu: aprovado em {head_then or '<sem-git>'}, "
                f"agora {head_now or '<sem-git>'}"
            )
        fp_now = _approval_fingerprint(
            str(resumed.get("prompt") or ""), snap if isinstance(snap, dict) else None,
            str(driver or resumed.get("driver") or "") or None,
            extra=_approval_extra(project, policy_efetiva),
        )
        if fp_now != binding.get("scope_sha256"):
            stale.append("escopo/prompt da run divergiu do snapshot aprovado")
        # DS-2: worktree rastreada na aprovação vs. agora — editar arquivo
        # tracked sem commit mantinha o HEAD e escapava de todas as âncoras.
        dirty_then = binding.get("dirty_start")
        if isinstance(dirty_then, dict):
            dirty_now = _tracked_dirty_snapshot(repo_root)
            if dirty_now != dirty_then:
                changed = sorted(set(dirty_now) ^ set(dirty_then))
                for k in set(dirty_now) & set(dirty_then):
                    if dirty_now[k] != dirty_then[k]:
                        changed.append(k)
                stale.append(
                    "worktree rastreada divergiu da aprovada (dirty_start): "
                    + ", ".join(sorted(set(changed))[:10]))
    # Comprovante LOCAL de aprovação (fora do repo): o binding é self-signed
    # dentro do próprio arquivo — uma run plantada por clone com binding
    # recomputado passaria em TODAS as checagens acima e executaria o
    # driver arbitrário gravado nela. O token só existe se `henke approve`
    # rodou de verdade nesta máquina sobre este binding.
    tok_ok, tok_err = verify_approval_token(resumed, repo_root=repo_root)
    if not tok_ok:
        stale.append(tok_err)
    if stale:
        _record_stage(cand, "resume_bind", "RETOMADA RECUSADA: " + "; ".join(stale))

        def _unstale(d: dict) -> None:
            d["review_status"] = "pending"
            # MÉDIO-3 (banca v13): drift de test_cmd/policy entre init e
            # approve virava wedge — approve re-ancorava só head, e o
            # escopo divergido recusava para sempre. O fingerprint do
            # ambiente ATUAL é o que o novo approve vai vincular.
            _b = d.get("approval_binding")
            if fp_now and isinstance(_b, dict):
                _b["scope_sha256"] = fp_now

        _patch_run(cand, _unstale)
        print("RETOMADA RECUSADA — HEAD/escopo divergiu desde a aprovação:", file=sys.stderr)
        for r in stale:
            print(f"  - {r}", file=sys.stderr)
        print(f"Nova aprovação exigida: henke approve {cand}", file=sys.stderr)
        return None, None, 2
    return cand, resumed, None


def _check_henke_confinement(repo_root: Path) -> int | None:
    """Garante `.henke/`, `.henke/runs` e `.henke/evidence` confinados à worktree (sem symlink/junction)."""
    henke_dir = repo_root / ".henke"
    # ALTO-1 (banca v13): `.henke` symlink/junction (clone hostil) fazia
    # runs/evidence serem escritos FORA do repo e POLICY/PROJECT lidos do
    # alvo — confinado à worktree real.
    try:
        if henke_dir.is_symlink() or (
                henke_dir.exists()
                and repo_root.resolve() not in henke_dir.resolve().parents):
            print("ERRO: '.henke' é symlink/junction ou resolve fora do repo "
                  "— custódia não é executável; remova o link.", file=sys.stderr)
            return 2
    except OSError:
        print("ERRO: '.henke' não resolvível — recusado.", file=sys.stderr)
        return 2
    if henke_dir.exists() and not henke_dir.is_dir():
        # A15 (banca v14): .henke como ARQUIVO explodia em NotADirectoryError.
        print("ERRO: '.henke' existe mas não é diretório — recusado.",
              file=sys.stderr)
        return 2
    (henke_dir / "runs").mkdir(parents=True, exist_ok=True)
    try:
        runs_r = (henke_dir / "runs").resolve()
        if (henke_dir / "runs").is_symlink() or repo_root.resolve() not in runs_r.parents:
            print("ERRO: '.henke/runs' é symlink/junction ou resolve fora do "
                  "repo — recusado.", file=sys.stderr)
            return 2
    except OSError:
        print("ERRO: '.henke/runs' não resolvível — recusado.", file=sys.stderr)
        return 2
    # A5 (banca v14): .henke/evidence nunca era conferido — junction redi-
    # recionava <gate>.out/tests.out/artifacts pra fora do repo e o
    # EVIDENCE-OUTSIDE-SCOPE nunca disparava (dois lados resolvem igual).
    ev_dir = henke_dir / "evidence"
    if ev_dir.exists() or ev_dir.is_symlink():
        try:
            ev_r = ev_dir.resolve()
            if ev_dir.is_symlink() or repo_root.resolve() not in ev_r.parents:
                print("ERRO: '.henke/evidence' é symlink/junction ou resolve "
                      "fora do repo — recusado.", file=sys.stderr)
                return 2
        except OSError:
            print("ERRO: '.henke/evidence' não resolvível — recusado.",
                  file=sys.stderr)
            return 2
    return None


def _init_or_bind_run(
    repo_root: Path,
    prompt: str,
    route: dict,
    project: dict | None,
    policy_efetiva: dict,
    driver: str | None,
    spec_path: Path | None,
    sensitive_hit: bool,
    resumed: dict | None,
    cand: Path | None,
) -> tuple[str, Path, int | None]:
    """Inicializa uma run fresca ou vincula uma run retomada + ancora sidecar."""
    if resumed is not None and cand is not None:
        run_id = cand.stem
        run_path = cand
        _mg = list(route.get("gates_required") or [])
        _patch_run(run_path, lambda d: d.update({
            "mandatory_gates": _mg,
            "driver": driver,
        }))
        # MÉDIO-1 (banca v13): a run sensível nasce SEM mandatory_gates —
        # patchada no resume ela ficava fora da âncora e o driver a deletava
        # do JSON (portões da matriz deixavam de ser exigidos).
        amend_anchor_fields(run_id, repo_root, {"mandatory_gates": _mg})
        _record_stage(run_path, "authorize", "RETOMADA pós-approve humana — escopo vinculado à run aprovada")
    else:
        run_id = f"auto-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
        run_path = init_run(
            repo_root,
            run_id,
            mode=str(route.get("mode", "ANALISAR")),
            archetype=str(route.get("archetype", "SHERLOCK")),
            spec_path=str(spec_path) if spec_path else None,
            requires_human_auth=sensitive_hit,
            # run bloqueada por auth não declara mandatory_gates (nunca rodarão)
            mandatory_gates=None if sensitive_hit else list(route.get("gates_required") or []),
        )
    route_snapshot = {
        "mode": route.get("mode"),
        "archetype": route.get("archetype"),
        "confidence": route.get("confidence"),
        "skills": route.get("skills"),
        "gates_required": route.get("gates_required"),
        "scope_paths": route.get("scope_paths"),
        "requires_human_auth": route.get("requires_human_auth"),
    }
    run_patch: dict = {
        "prompt": prompt,
        "scope_paths": list(route.get("scope_paths") or []),
        "policy_efetiva": policy_efetiva,
        "driver": driver,
        "rework_attempts": 0,
        # snapshot do route p/ retomada futura (--resume) — escopo vinculado à run
        "route_snapshot": route_snapshot,
        # ALTO-2 (banca v15): o resumo do approve lia 'test_cmd' e sempre
        # mostrava '(nenhum)' — o humano aprovava sem ver o comando que o
        # fingerprint ancora e o stage 7 executa.
        "test_cmd": (project or {}).get("test_cmd") if isinstance(project, dict) else None,
    }
    if resumed is None:
        # Vínculo da aprovação (--resume): HEAD + sha256(prompt+route_snapshot).
        run_patch["approval_binding"] = {
            "head": _git_head(repo_root),
            "scope_sha256": _approval_fingerprint(
                prompt, route_snapshot, driver,
                extra=_approval_extra(project, policy_efetiva)),
            "driver": driver,
            "run_id": run_id,
            "repo": str(repo_root.resolve()),
            # DS-2: snapshot dos tracked-sujos na aprovação — editar arquivo
            # tracked entre approve e --resume diverge o binding e recusa.
            "dirty_start": _tracked_dirty_snapshot(repo_root),
            "bound_at": _now_iso(),
        }
    _patch_run(run_path, lambda d: d.update(run_patch))
    # Sidecar LOCAL das âncoras de nascimento (fora do repo): o driver pode
    # editar .henke/runs/<id>.json e mover head_start/spec_sha256 para esconder
    # o próprio trabalho do diff — run_gates prefere esta cópia intocável.
    try:
        _run_now = json.loads(run_path.read_text(encoding="utf-8"))
        if resumed is None:
            write_anchor_sidecar(
                str(_run_now.get("run_id") or run_id),
                _run_now.get("spec_path"),
                _run_now.get("spec_sha256"),
                _run_now.get("head_start"),
                repo_root=repo_root,
                fields=_run_now,
            )
        else:
            # A2 (banca v9): resume NUNCA regrava o sidecar a partir do JSON
            # adulterável — regravar lavaria head_start/spec_sha256 movidos
            # pelo atacante entre a criação e o approve. Divergência = falha.
            _sc = load_anchor_sidecar(str(_run_now.get("run_id") or run_id), repo_root=repo_root)
            _drift = ["sidecar ausente"] if not isinstance(_sc, dict) else [
                f for f in ("spec_path", "spec_sha256", "head_start")
                if _sc.get(f) != _run_now.get(f)
            ] + [
                f for f in _ANCHORED_FIELDS
                if f in ((_sc or {}).get("fields") or {})
                and (_sc["fields"][f] != _run_now.get(f))
            ]
            if _drift:
                # B2 (banca v11): RuntimeError escapava do except em traceback
                # feio — erro limpo + aborto; NÃO pode ser engolido pelo
                # except abaixo (drift ignorado = retomar run adulterada).
                raise ValueError(
                    "ANCHOR-DRIFT: âncoras da run divergem do sidecar local "
                    f"({', '.join(_drift)}) — retomada recusada"
                )
    except (OSError, ValueError) as _ae:
        if "ANCHOR-DRIFT" in str(_ae):
            print(f"ERRO: {_ae}", file=sys.stderr)
            return run_id, run_path, 2
    return run_id, run_path, None
