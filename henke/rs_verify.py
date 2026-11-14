"""rs_verify.py — Derivação mecânica de outcome e verificação retroativa de custódia."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from henke.rs_custody import (
    _ANCHORED_FIELDS,
    _EVIDENCE_MAX_BYTES,
    _load_evidence_manifest,
    human_approval_of,
    load_anchor_sidecar,
    verify_approval_token,
)
from henke.rs_io import _repo_root_for_run, _safe_run_id


_NA_CAPABLE_GATES = frozenset({"a11y_scan", "mutant_hunt"})


def derive_run_outcome(run_data: dict, repo_root: Path | None = None) -> tuple[str, list[str]]:
    """Deriva mecanicamente ('verified' | 'failed' | 'blocked', reasons) a partir de run_data."""
    if not isinstance(run_data, dict):
        return "failed", ["INVALID-RUN-DATA: estrutura de run não é um objeto JSON"]

    root_dir = Path(repo_root).resolve() if repo_root is not None else Path.cwd().resolve()
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

    # 2. Verifica existência física da spec se declarada
    spec_path_str = run_data.get("spec_path")
    if spec_path_str:
        spec_p = Path(spec_path_str)
        if not spec_p.is_absolute():
            spec_p = (root_dir / spec_p).resolve()
        if not spec_p.is_file():
            return "blocked", [f"INVALID-SPEC-PATH: spec declarada '{spec_path_str}' não existe fisicamente no disco"]

    # 3. Verifica execução física dos portões
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
        # DS-4 (auditoria consolidada): `if not executed_core` liberava a run
        # com UM único portão verde — hazard_scan sozinho satisfazia uma run
        # EXECUTAR sem testes. Exige o conjunto completo: hazard e assertless
        # verdes (0), a11y verde ou n/a (0|3 — sem arquivos de UI elegíveis).
        def _exit_of(gate: str) -> int | None:
            for g_name, g_info in coverage_gates.items():
                if (g_name == gate or Path(g_name).stem == gate) \
                        and isinstance(g_info, dict):
                    ec = g_info.get("exit_code")
                    return ec if isinstance(ec, int) else None
            return None
        missing_core = [
            g for g in ("hazard_scan", "assertless_scan")
            if _exit_of(g) != 0
        ]
        a11y_exit = _exit_of("a11y_scan")
        if a11y_exit not in (0, 3):
            missing_core.append("a11y_scan")
        if missing_core:
            return "blocked", [
                "MANDATORY-GATE-MISSING: portões fundamentais ausentes ou não "
                f"verdes — exige hazard_scan==0, assertless_scan==0 e "
                f"a11y_scan∈{{0,3}}; faltando/falhos: {', '.join(missing_core)}"
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
            # 'henke/metricas/hazard_scan.py' devem casar o mesmo portão.
            req_stem = Path(str(req)).stem
            # banca-v20 (F6): exit 3 (n/a) só vale veredito para portões que
            # PODEM ser n/a por spec (a11y_scan, mutant_hunt) — para os demais,
            # obrigatório exige exit 0, alinhando com o fallback EXECUTAR.
            want = (0, 3) if req_stem in _NA_CAPABLE_GATES else (0,)
            found = any(
                (g_name == req or Path(g_name).stem == req_stem)
                and isinstance(g_info, dict)
                and g_info.get("exit_code") in want
                for g_name, g_info in coverage_gates.items()
            )
            if not found:
                missing_mandatory.append(
                    f"MANDATORY-GATE-MISSING: portão obrigatório '{req}' não executado com sucesso"
                )
        if missing_mandatory:
            return "blocked", missing_mandatory

    # 3. Verifica trava de autorização humana para operações sensíveis/PUBLISH
    if run_data.get("requires_human_auth") is True and human_approval_of(run_data) != "approved":
        return "blocked", ["HUMAN-AUTH-REQUIRED: human_approval pendente de aprovação humana"]

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

    # Dedução de repo_root a partir do caminho do arquivo se dentro de .henke/runs
    repo_root = p.parent.parent.parent if (len(p.parents) >= 2 and p.parent.name == "runs" and p.parent.parent.name == ".henke") else Path.cwd().resolve()

    declared = data.get("outcome")
    derived, reasons = derive_run_outcome(data, repo_root=repo_root)

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
        # A14 (banca v16): junction Windows não é symlink — is_junction.
        _linked = _ev_parent.is_symlink() or (
            getattr(_ev_parent, "is_junction", lambda: False)())
        if _linked or evidence_root.parent.parent != p.parent.parent:
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
    if (human_approval_of(data) == "approved"
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
