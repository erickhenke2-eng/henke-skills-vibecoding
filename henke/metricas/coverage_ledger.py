#!/usr/bin/env python3
"""
coverage_ledger.py — Portão mecânico de Cobertura de Auditoria & Dossier (PROTOCOLO v3.4).

Sintetiza os mecanismos de:
  - `cloudflare/security-audit-skill` (`validate-coverage-ledger.cjs` + `validate-findings.cjs`)
  - `trailofbits/skills` (`fp-check` 6 gates + `audit-context-building` assumptions dossier)

Subcomandos:
  1. `init --root <dir> --out <ledger.json> [--profile quick|standard|deep]`
     Enumera deterministicamente todos os arquivos de código in-scope, calcula `sha256`
     e `lines` de cada arquivo, pontua a prioridade (`high|medium|low`) combinando
     `edges` (entry surfaces + fan-in), `metrics` (complexidade) e `hazard_scan` (hits),
     e grava `<ledger.json>` atomicamente (`.tmp` + `os.replace`).
  2. `verify <ledger.json> [--root <dir>] [--min-pct 90] [--require-critic]`
     Re-percorre o escopo para barrar arquivos `UNMAPPED` ou `STALE` (`sha256` alterado),
     valida invariantes por status (`covered` exige `checks[]` com `assumptions[]`;
     `finding_confirmed` exige `trace[]` com `file`+`line` reais no disco e gates `fp-check`;
     `needs_validation` proíbe `severity`; `deferred`/`out_of_scope` exigem `reason`),
     e reprova (`exit 1`) se houver unidades `pending` ou cobertura abaixo de `--min-pct`.
  3. `render-gaps <ledger.json>`
     Gera mecanicamente o resumo de contagens (incluindo `unenforced_assumptions`) e a
     seção Markdown `#### O que NÃO foi verificado` a partir de `deferred`, `needs_validation`
     e `out_of_scope`.

Códigos de saída:
  0  verde (ledger inicializado, verificado ou renderizado)
  1  veredito-vermelho (cobertura insuficiente, unidade pendente/stale/unmapped, achado inválido)
  2  entrada malformada / erro de I/O / stdout fechado
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")  # CWD-poisoning (banca v10 A2)
from pathlib import Path
import subprocess
import sys

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

_HERE = Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent
for _p in (str(_HERE), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import repo_signals as _rs  # noqa: E402
import hazard_scan as _hs   # noqa: E402

# A11 (banca v10): GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG_* plantados
# no ambiente redirecionariam o git dos vereditos para outro repo/index.
_GIT_ENV = {k: v for k, v in os.environ.items() if not (k.startswith("GIT_") or k.startswith("PYTHON")
                or k.startswith("LD_") or k.startswith("DYLD_")
                or k in ("BASH_ENV", "ENV", "ZDOTDIR", "NODE_OPTIONS",
                         "PERL5OPT", "PERL5LIB", "RUBYOPT",
                         "DOTNET_STARTUP_HOOKS"))}

VALID_PROFILES = ("quick", "standard", "deep")
VALID_UNIT_STATUS = ("pending", "covered", "finding_confirmed", "needs_validation", "deferred", "out_of_scope")
VALID_SEVERITIES = ("critical", "high", "medium", "low", "informational")
VALID_ASSUMPTION_STATUS = ("enforced", "unenforced", "unknown")

# banca-v19 (rev FV-1): veredito de check exige token POSITIVO explícito
# e nenhum token de falha — blocklist exata evadia "reprovado", "0", "❌".
_RES_POS = re.compile(
    r"\b(pass(ed|ou)?|ok|true|verified|verificado|verificada|success|"
    r"succeeded|sucesso|aprovado|aprovada|clean|limpo|confirmed|confirmado|"
    r"confirmada|válid|valid|green|yes|sim|conforme|satisfied)\b")
_RES_NEG = re.compile(
    r"\b(?:fail|falh|reprov|violat|inconclusive|denied|timeout|error|"
    r"não|not\b)|[❌✗✖×]")

# banca-v18 (residual 5): critic_passes teatral — `[{"agent":"a"}]`, `[""]`
# e `[{"note":"ok"}]` sem substância mintiam o gate --require-critic/deep.
# Cada entrada exige (agent|auditor) + round + (scope|findings OU
# rationale >= 20 chars); placeholders óbvios não valem como substância.
_CRITIC_PLACEHOLDERS = frozenset({
    "", "ok", "none", "teste", "test", "x", "n/a", "na", "-", "...",
    "todo", "wip", "lorem", "pass", "passed", "yes", "sim", "null",
    "placeholder", "tbd",
    # NOTA: dígitos ("0"/"1"/"2") NÃO são placeholder — `round: 1` é round
    # legítimo; quem quis evadir usa "ok"/"x", não numeração sequencial.
})
_CRITIC_AGENT_FIELDS = ("agent", "auditor", "author", "revisor")
_CRITIC_ROUND_FIELDS = ("round", "pass", "passo", "iteration", "rodada")
_CRITIC_SCOPE_FIELDS = ("scope", "findings", "summary", "result", "notes",
                        "escopo", "achados")
_CRITIC_RATIONALE_FIELDS = ("rationale", "justification", "justificativa",
                            "reason", "motivo", "analysis")


def _critic_field_values(cp: dict, names) -> list:
    """Valores não-vazios de qualquer alias do campo (str, lista, escalar)."""
    vals = []
    for n in names:
        v = cp.get(n)
        if isinstance(v, str) and v.strip():
            vals.append(v.strip())
        elif isinstance(v, (list, tuple, set)) and len(v) > 0:
            vals.append(v)
        elif (v is not None and not isinstance(v, (dict, bool, list, tuple, set))
              and str(v).strip()):
            # escalar não-string (round numérico etc.) conta como valor
            vals.append(str(v).strip())
    return vals


def _critic_substantive(vals) -> bool:
    """True se algum valor tem substância real (não-placeholder)."""
    for v in vals:
        if isinstance(v, str):
            if v.strip().lower() not in _CRITIC_PLACEHOLDERS:
                return True
        else:  # lista/tupla/set não-vazia é substância (findings[])
            return True
    return False


def _critic_entry_valid(cp) -> bool:
    """True quando a entrada de critic_passes tem substância mínima real."""
    if not isinstance(cp, dict):
        return False
    if not _critic_substantive(_critic_field_values(cp, _CRITIC_AGENT_FIELDS)):
        return False
    if not _critic_substantive(_critic_field_values(cp, _CRITIC_ROUND_FIELDS)):
        return False
    if _critic_substantive(_critic_field_values(cp, _CRITIC_SCOPE_FIELDS)):
        return True
    # alternativa: rationale textual com >=20 chars e não-placeholder
    for v in _critic_field_values(cp, _CRITIC_RATIONALE_FIELDS):
        if isinstance(v, str) and len(v.strip()) >= 20 \
                and v.strip().lower() not in _CRITIC_PLACEHOLDERS:
            return True
    return False


def _file_sha256_and_lines(path: Path) -> tuple[str, int]:
    raw = path.read_bytes()
    norm = raw.replace(b"\r\n", b"\n")
    h = hashlib.sha256(norm).hexdigest()
    lines = norm.count(b"\n") + (1 if norm and not norm.endswith(b"\n") else 0)
    return h, max(1, lines)


def _walk_scope_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in _rs.iter_files(root):
        if p.name.endswith(".d.ts"):
            continue
        rel = _rs.relposix(p, root)
        if _rs.pg.TEST_RE.search("/" + rel):
            continue
        out.append(p)
    return sorted(out, key=lambda x: _rs.relposix(x, root))


def _atomic_write_json(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def init_ledger(root: Path, out_path: Path, profile: str = "standard") -> dict:
    root = root.resolve()
    files = _walk_scope_files(root)
    if not files:
        raise ValueError(f"Nenhum arquivo de código elegível encontrado em {root}")

    # Sinais de risco para priorização (edges fan-in/entry + complexidade + hazards)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _rs.cmd_edges(argparse.Namespace(root=str(root)))
    edges = json.loads(buf.getvalue() or "[]")
    fan_in: dict[str, int] = {}
    fan_out: dict[str, int] = {}
    for e in edges:
        fan_out[e["from"]] = fan_out.get(e["from"], 0) + 1
        fan_in[e["to"]] = fan_in.get(e["to"], 0) + 1

    units = []
    tree_hasher = hashlib.sha256()
    for p in files:
        rel = _rs.relposix(p, root)
        sha, nlines = _file_sha256_and_lines(p)
        tree_hasher.update(f"{rel}:{sha}\n".encode("utf-8"))
        _, h_findings, _ = _hs.scan_file(p)
        h_count = len(h_findings)
        fin = fan_in.get(rel, 0)
        fout = fan_out.get(rel, 0)
        is_entry = (fin == 0 and fout > 0) or any(k in rel for k in ("main", "index", "handler", "ipc", "route", "api", "cli"))
        mcomp = max((comp for _, _, comp in _rs.iter_functions(p)), default=1)

        risk_score = (h_count * 10) + (5 if is_entry else 0) + min(fin * 2, 10) + min(mcomp, 15)
        if risk_score >= 12 or h_count > 0:
            priority = "high"
        elif risk_score >= 6 or is_entry:
            priority = "medium"
        else:
            priority = "low"

        units.append({
            "unit_id": f"unit:{rel}",
            "path": rel,
            "sha256": sha,
            "lines": nlines,
            "priority": priority,
            "risk_score": risk_score,
            "risk_signals": {
                "hazard_hits": h_count,
                "fan_in": fin,
                "max_complexity": mcomp,
                "entry_surface": is_entry,
            },
            "status": "pending",
            "reason": "",
            "checks": [],
            "attempts": [],
            "prior_status": None,
        })

    units.sort(key=lambda u: (-u["risk_score"], u["path"]))
    ledger = {
        "schema_version": "1.0",
        "metadata": {
            "root": str(root).replace("\\", "/"),
            "profile": profile,
            "tree_hash": tree_hasher.hexdigest(),
            "total_units": len(units),
            "run_status": "in_progress",
        },
        "critic_passes": [],
        "findings": [],
        "units": units,
    }
    _atomic_write_json(out_path, ledger)
    return ledger


def verify_ledger(
    ledger_path: Path,
    root_override: Path | None = None,
    min_pct: float = 90.0,
    require_critic: bool = False,
    max_out_of_scope_pct: float = 20.0,
    max_oos_per_dir_pct: float = 20.0,
    min_dir_units: int = 3,
    against: str | None = None,
) -> tuple[bool, list[str], dict]:
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, [f"SCHEMA-ERR: JSON inválido em {ledger_path}: {e}"], {}

    if not isinstance(data, dict) or "units" not in data or "metadata" not in data:
        return False, ["SCHEMA-ERR: campos obrigatórios 'metadata' e 'units' ausentes"], {}

    # H3 (banca v15): units null/lista e metadata não-dict explodiam em
    # traceback exit 1 (código de veredito-vermelho) — schema validado.
    meta = data["metadata"]
    if not isinstance(meta, dict) or not isinstance(data["units"], list):
        return False, ["SCHEMA-ERR: 'metadata' deve ser objeto e 'units' lista"], {}
    profile = meta.get("profile", "standard")
    if profile not in VALID_PROFILES:
        return False, [f"SCHEMA-ERR: profile inválido '{profile}'"], {}

    root = (root_override or Path(meta.get("root", "."))).resolve()
    if not root.is_dir():
        return False, [f"ROOT-ERR: diretório raiz inexistente: {root}"], {}

    errors: list[str] = []
    units = data.get("units", [])
    if not units:
        errors.append("EMPTY-LEDGER: 0 unidades no ledger")

    # 1. Mapeamento total arquivo <-> unidade e verificação de staleness por SHA-256
    disk_files = {p.relative_to(root).as_posix(): p for p in _walk_scope_files(root)}
    mapped_paths: set[str] = set()

    counts = {s: 0 for s in VALID_UNIT_STATUS}
    unenforced_assumptions = 0

    seen_paths: set = set()
    for u in units:
        if not isinstance(u, dict):
            # F35 (banca v11): entrada não-dict explodia com traceback exit 1
            # — crash veste código de veredito-vermelho; erro validado é 2.
            errors.append(f"INVALID-UNIT: entrada não-objeto {u!r:.60}")
            continue
        uid = u.get("unit_id", "<sem-id>")
        rel = u.get("path", "")
        status = u.get("status", "")
        mapped_paths.add(rel)
        # H3 (banca v14): unidades duplicadas do mesmo path inflavam o
        # denominador — 10 arquivos + 10 dups "covered" = 90% verde falso.
        if isinstance(rel, str) and rel:
            if rel in seen_paths:
                errors.append(f"DUPLICATE-PATH [{uid}]: '{rel}' repetida no ledger")
            seen_paths.add(rel)
        # M12 (banca v14): path não-string explodia/virava lixo no mapping.
        elif rel:
            errors.append(f"INVALID-UNIT [{uid}]: 'path' não-string {rel!r:.40}")

        if status not in VALID_UNIT_STATUS:
            errors.append(f"INVALID-STATUS [{uid}]: status '{status}' desconhecido")
            continue
        counts[status] += 1

        if rel not in disk_files:
            if status != "out_of_scope":
                errors.append(f"STALE-DELETED [{uid}]: arquivo '{rel}' não existe mais no disco")
        else:
            cur_sha, _ = _file_sha256_and_lines(disk_files[rel])
            # A10 v12 + H4 v14: sha checado p/ todo status E OBRIGATÓRIO —
            # omitir o campo desligava o detector de mutação.
            if u.get("sha256"):
                if cur_sha != u.get("sha256"):
                    errors.append(f"STALE-HASH [{uid}]: arquivo '{rel}' mudou após a auditoria (sha256 divergente)")
            else:
                errors.append(f"MISSING-SHA [{uid}]: unidade de arquivo existente exige 'sha256'")

        if status == "pending":
            errors.append(f"PENDING-UNIT [{uid}]: unidade '{rel}' permaneceu 'pending'")
        elif status in ("deferred", "out_of_scope"):
            if len((u.get("reason") or "").strip()) < 3:
                errors.append(f"MISSING-REASON [{uid}]: status '{status}' exige campo 'reason' não-vazio")
        elif status == "covered":
            checks = u.get("checks", [])
            if not isinstance(checks, list) or not checks:
                errors.append(f"EMPTY-CHECKS [{uid}]: unidade 'covered' exige pelo menos 1 check em 'checks[]'")
            for chk in checks if isinstance(checks, list) else []:
                if not isinstance(chk, dict):
                    # A13 (banca v12): chk não-dict explodia com traceback.
                    errors.append(f"INVALID-CHECK [{uid}]: check não-objeto")
                    continue
                if not chk.get("agent_id") or not chk.get("invariant") or not chk.get("result"):
                    errors.append(f"INCOMPLETE-CHECK [{uid}]: check exige 'agent_id', 'invariant' e 'result'")
                # banca-v18 (rev B-F7): `result` era validado só por
                # truthiness — "failed"/"violated" é truthy e mintava check
                # válido: a invariante REPROVANDO contava como cobertura.
                _res = str(chk.get("result", "")).strip().lower()
                # banca-v19 (rev FV-1): lista exata evadia "reprovado",
                # "failed — timeout", "0", "❌". Agora exige token
                # positivo E ausência de token de falha (fail-closed).
                if not (_RES_POS.search(_res) and not _RES_NEG.search(_res)):
                    errors.append(f"FAILED-RESULT [{uid}]: check com result "
                                  f"'{_res}' não expressa veredito positivo "
                                  "limpo — resultado não-afirmativo ou com "
                                  "marca de falha não é evidência de "
                                  "cobertura")
                if chk.get("method") not in ("source", "local"):
                    errors.append(f"INVALID-METHOD [{uid}]: check.method deve ser 'source' ou 'local'")
                assumptions = chk.get("assumptions", [])
                if not isinstance(assumptions, list):
                    errors.append(f"INVALID-ASSUMPTIONS [{uid}]: 'assumptions' deve ser lista")
                else:
                    for a in assumptions:
                        if not isinstance(a, dict) or a.get("status") not in VALID_ASSUMPTION_STATUS or not a.get("text"):
                            errors.append(f"INVALID-ASSUMPTION [{uid}]: cada assumption exige 'text' e status in {VALID_ASSUMPTION_STATUS}")
                        elif a.get("status") == "unenforced":
                            unenforced_assumptions += 1
        elif status in ("finding_confirmed", "needs_validation"):
            # F5 (banca v11): status que contam como "auditados" não podem
            # ser inventados sem evidência — exigem checks[] como 'covered'.
            checks = u.get("checks", [])
            if not isinstance(checks, list) or not checks:
                errors.append(
                    f"EMPTY-CHECKS [{uid}]: unidade '{status}' exige pelo menos "
                    "1 check em 'checks[]' — status auditado sem evidência é "
                    "verde fabricado")
            # BANCA-B2: lista não-vazia sem CONTEÚDO também era aceita —
            # [{}] contava como auditado. Valida o conteúdo como 'covered'.
            for chk in checks if isinstance(checks, list) else []:
                if not isinstance(chk, dict):
                    errors.append(f"INVALID-CHECK [{uid}]: check não-objeto")
                    continue
                if not chk.get("agent_id") or not chk.get("invariant") or not chk.get("result"):
                    errors.append(f"INCOMPLETE-CHECK [{uid}]: check exige 'agent_id', 'invariant' e 'result'")
                # banca-v19 (rev A-5): a regra de polaridade NÃO se aplica
                # aqui — em finding_confirmed/needs_validation, result
                # "violated"/"failed" É a evidência do finding (a
                # invariante reprovou). Exigir positivo travaria o
                # registro honesto de finding confirmado.
                if chk.get("method") not in ("source", "local"):
                    errors.append(f"INVALID-METHOD [{uid}]: check.method deve ser 'source' ou 'local'")
                assumptions = chk.get("assumptions", [])
                if not isinstance(assumptions, list):
                    errors.append(f"INVALID-ASSUMPTIONS [{uid}]: 'assumptions' deve ser lista")
                else:
                    for a in assumptions:
                        if not isinstance(a, dict) or a.get("status") not in VALID_ASSUMPTION_STATUS or not a.get("text"):
                            errors.append(f"INVALID-ASSUMPTION [{uid}]: cada assumption exige 'text' e status in {VALID_ASSUMPTION_STATUS}")
                        elif a.get("status") == "unenforced":
                            unenforced_assumptions += 1

    for disk_rel in sorted(set(disk_files.keys()) - mapped_paths):
        errors.append(f"UNMAPPED-FILE: arquivo in-scope '{disk_rel}' não pertence a nenhuma unidade do ledger")

    # 2. Validação dos findings (fp-check gates + checagem física de file:line)
    for idx, f in enumerate(data.get("findings", [])):
        if not isinstance(f, dict):
            errors.append(f"INVALID-FINDING: finding #{idx} não-objeto")
            continue
        fid = f.get("id", f"finding-{idx}")
        fstatus = f.get("status")
        if fstatus == "finding_confirmed":
            if f.get("severity") not in VALID_SEVERITIES:
                errors.append(f"FINDING-SEVERITY [{fid}]: finding_confirmed exige severity válida")
            for req_field in ("boundary_crossed", "real_impact", "reproduction_cmd_or_poc"):
                if len(str(f.get(req_field) or "").strip()) < 3:
                    errors.append(f"FINDING-FP-GATE [{fid}]: campo obrigatório '{req_field}' ausente/vazio")
            trace = f.get("trace", [])
            if not isinstance(trace, list) or not trace:
                errors.append(f"FINDING-NO-TRACE [{fid}]: finding_confirmed exige 'trace[]' (source -> sink)")
            else:
                for step in trace:
                    # H3 (banca v15): step não-dict/file não-str explodia.
                    if not isinstance(step, dict):
                        errors.append(f"INVALID-TRACE [{fid}]: step não-objeto")
                        continue
                    tf = step.get("file", "")
                    if not isinstance(tf, str):
                        errors.append(f"INVALID-TRACE [{fid}]: file não-string")
                        continue
                    tline = step.get("line", 0)
                    tpath = (root / tf).resolve()
                    root_res = root.resolve()
                    if root_res not in (tpath, *tpath.parents):
                        errors.append(
                            f"FABRICATED-FILE [{fid}]: arquivo '{tf}' do trace escapa a raiz do repo")
                    elif not tpath.is_file():
                        errors.append(f"FABRICATED-FILE [{fid}]: arquivo '{tf}' citado no trace não existe")
                    else:
                        _, max_ln = _file_sha256_and_lines(tpath)
                        if not isinstance(tline, int) or tline < 1 or tline > max_ln:
                            errors.append(f"FABRICATED-LINE [{fid}]: linha {tline} fora dos limites de '{tf}' (1..{max_ln})")
        elif fstatus == "needs_validation":
            if f.get("severity") is not None:
                errors.append(f"THEATRICAL-SEVERITY [{fid}]: 'needs_validation' NÃO pode receber severity")
            if not f.get("blockers") or not f.get("validation_plan"):
                errors.append(f"INCOMPLETE-NEEDS-VAL [{fid}]: exige 'blockers' e 'validation_plan'")
        elif fstatus == "rejected":
            if len(str(f.get("reason") or "").strip()) < 3:
                errors.append(f"REJECTED-NO-REASON [{fid}]: achado 'rejected' exige 'reason'")
        else:
            errors.append(f"INVALID-FINDING-STATUS [{fid}]: status '{fstatus}' inválido")

    # 3. Cálculo honesto de cobertura (exclui out_of_scope do denominador)
    total_units = len(units)
    if total_units > 0:
        out_pct = 100.0 * counts["out_of_scope"] / total_units
        if out_pct > max_out_of_scope_pct:
            errors.append(f"EXCESSIVE-OUT-OF-SCOPE: unidades fora de escopo {out_pct:.1f}% ({counts['out_of_scope']}/{total_units}) excede o teto permitido de {max_out_of_scope_pct:.1f}%")

    # 3b. Anti-diluição por diretório: teto de out_of_scope avaliado por pasta-pai
    # (impede concentrar código perigoso no core/ diluindo com DTOs auditáveis na ui/).
    # Diretórios com menos de `min_dir_units` unidades são isentos.
    # A12 (banca v12): unidades duplicadas do mesmo path diluíam o teto
    # out_of_scope — conta caminhos distintos, não unidades.
    dir_totals: dict[str, set] = {}
    dir_oos: dict[str, set] = {}
    for u in units:
        if u.get("status") not in VALID_UNIT_STATUS:
            continue
        parent = u.get("path", "").replace("\\", "/").rsplit("/", 1)[0] or "."
        dir_totals.setdefault(parent, set()).add(u.get("path", ""))
        if u.get("status") == "out_of_scope":
            dir_oos.setdefault(parent, set()).add(u.get("path", ""))
    for d in sorted(dir_totals):
        if len(dir_totals[d]) >= min_dir_units:
            d_pct = 100.0 * len(dir_oos.get(d, set())) / len(dir_totals[d])
            if d_pct > max_oos_per_dir_pct:
                errors.append(
                    f"DILUTED-OUT-OF-SCOPE: diretório '{d}/' tem {d_pct:.1f}% de unidades fora de escopo "
                    f"({len(dir_oos.get(d, set()))}/{len(dir_totals[d])}) excedendo o teto por-diretório de {max_oos_per_dir_pct:.1f}%")

    # 3c. Arquivo criado na própria PR não pode ser out_of_scope (diluição por nascença).
    if against:
        # F7: --against precisa resolver pra um commit — sem validação, uma ref
        # inválida/`--opt` falha o diff silenciosamente e o NEW-FILE-OUT-OF-SCOPE
        # nunca dispara (bypass do portão).
        if against.startswith("-") or not against.strip():
            errors.append(f"INVALID-AGAINST: ref '--against' inválida: {against!r}")
        else:
            _rc = subprocess.run(
                ["git", "rev-parse", "--verify", f"{against}^{{commit}}"],
                cwd=str(root), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                env=_GIT_ENV,
            )
            if _rc.returncode != 0:
                errors.append(f"INVALID-AGAINST: ref '{against}' não resolve para commit")
        res_added = subprocess.run(
            ["git", "-c", "core.quotePath=false",
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "diff", "--name-status", "-z", against, "--"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=_GIT_ENV,
        )
        # A14 (banca v12): diff falho caía silencioso — NEW-FILE-OUT-OF-SCOPE
        # nunca avaliado. Falha de enumeração é erro validado (fail-closed).
        if res_added.returncode != 0:
            errors.append(
                f"DIFF-FAILED: 'git diff --name-status {against}' retornou "
                f"rc={res_added.returncode} — checagem de arquivos novos "
                "indisponível")
        else:
            # F28 (banca v11): -z — nomes com tab/newline não quebram o parse.
            # A11 (banca v12): arquivo literalmente chamado 'A'/'R'/'C' era
            # confundido com status e dessincronizava o stream — status tem
            # forma ^[A-Z]\d{0,3}$, paths são consumidos por contagem fixa.
            toks = res_added.stdout.split("\0")
            added_files: set[str] = set()
            i_tok = 0
            while i_tok < len(toks) - 1:
                st = toks[i_tok]
                if not re.fullmatch(r"[A-Z]\d{0,3}", st or ""):
                    break  # dessincronizado — não inventa
                code = st[:1]
                if code in ("R", "C"):
                    i_tok += 3
                else:
                    if code == "A":
                        added_files.add(toks[i_tok + 1].strip().replace("\\", "/"))
                    i_tok += 2
            for u in units:
                rel_u = u.get("path", "").replace("\\", "/")
                if u.get("status") == "out_of_scope" and rel_u in added_files:
                    errors.append(
                        f"NEW-FILE-OUT-OF-SCOPE: arquivo '{rel_u}' criado nesta PR está marcado "
                        "out_of_scope — código novo não pode nascer fora de escopo")

    active_total = total_units - counts["out_of_scope"]
    audited_count = counts["covered"] + counts["finding_confirmed"] + counts["needs_validation"]
    if audited_count == 0:
        errors.append("ZERO-AUDITED: nenhuma unidade foi efetivamente auditada (audited_units == 0)")

    pct = (100.0 * audited_count / active_total) if active_total > 0 else 0.0
    if pct < min_pct:
        errors.append(f"LOW-COVERAGE: cobertura auditada {pct:.1f}% ({audited_count}/{active_total}) < mínimo {min_pct:.1f}%")

    # 4. Verificação do Coverage Critic (obrigatório em profile 'deep' ou --require-critic)
    critic_passes = data.get("critic_passes", [])
    # banca-v18 (rev B-F8): `isinstance(list) and len>0` aceitava [null],
    # [{}] e ["x"] — critic teatral satisfazia --require-critic/deep.
    # residual 5: agent+round sem substância ({"agent":"a","round":"r1"}) e
    # placeholders ("ok"/"x"/"none"/"teste") AINDA mintavam o gate — cada
    # entrada exige agent/auditor + round + (scope/findings OU rationale
    # >=20 chars) não-placeholder. Entrada teatral é evidência fabricada:
    # reprova por entrada, em qualquer profile.
    if not isinstance(critic_passes, list):
        errors.append("INVALID-CRITIC-PASSES: 'critic_passes' não é lista")
        critic_passes = []
    for _cpi, _cp in enumerate(critic_passes):
        if not _critic_entry_valid(_cp):
            errors.append(
                f"INVALID-CRITIC-PASS: critic_passes[{_cpi}] sem substância — "
                "exige agent/auditor + round + (scope/findings ou "
                "rationale>=20 chars) não-placeholder")
    has_critic = (len(critic_passes) > 0
                  and all(_critic_entry_valid(cp) for cp in critic_passes))
    outcome = "verified" if not errors else "failed"
    if not errors and (profile == "deep" or require_critic) and not has_critic:
        outcome = "partial"
        # profile=deep SEM critic completo é veredito incompleto — 'partial' não
        # pode vestir exit 0 como se fosse verde (M52-banca).
        errors.append("MISSING-CRITIC: profile/flag exige pelo menos 1 passagem em 'critic_passes[]'")

    summary = {
        "outcome": outcome,
        "coverage_pct": round(pct, 1),
        "audited_units": audited_count,
        "active_units": active_total,
        "counts": counts,
        "unenforced_assumptions": unenforced_assumptions,
        "critic_passes": len(critic_passes) if isinstance(critic_passes, list) else 0,
    }
    return len(errors) == 0, errors, summary


def render_gaps_markdown(ledger_path: Path) -> str:
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    # banca-v18 (rev B-F15): JSON não-dict (lista, escalar) caía em
    # AttributeError → traceback exit 1 em vez do diagnóstico padronizado.
    if not isinstance(data, dict):
        raise ValueError(f"ledger não é objeto JSON: {ledger_path}")
    ok, _errs, summary = verify_ledger(ledger_path)
    lines = [
        f"<!-- coverage_ledger outcome={summary.get('outcome', 'unknown')} "
        f"coverage={summary.get('coverage_pct', 0)}% "
        f"unenforced_assumptions={summary.get('unenforced_assumptions', 0)} -->",
        "#### O que NÃO foi verificado:",
    ]
    gaps: list[str] = []
    for u in data.get("units", []):
        st = u.get("status")
        if st in ("deferred", "out_of_scope", "pending", "needs_validation"):
            rsn = (u.get("reason") or "sem justificativa registrada").strip()
            gaps.append(f"- `{u.get('path')}` (`{st}`): {rsn}")
    for f in data.get("findings", []):
        if f.get("status") == "needs_validation":
            gaps.append(
                f"- Hipótese bloqueada `{f.get('id', 'finding')}` (`needs_validation`): "
                f"bloqueio={f.get('blockers')} | plano={f.get('validation_plan')}"
            )
    if not gaps:
        lines.append("- Escopo de código 100% coberto no ledger; superfícies dinâmicas/externas fora do repositório permanecem não verificadas.")
    else:
        lines.extend(gaps)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Portão de Cobertura de Auditoria & Dossier (Cloudflare + ToB).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Inicializa coverage-ledger determinístico com SHA-256 e risco")
    p_init.add_argument("--root", required=True, help="Diretório raiz a auditar")
    p_init.add_argument("--out", required=True, help="Arquivo JSON de saída (fora do repo auditado)")
    p_init.add_argument("--profile", default="standard", choices=VALID_PROFILES)

    p_ver = sub.add_parser("verify", help="Verifica integridade, SHA-256, cobertura e fp-check gates")
    p_ver.add_argument("ledger", help="Caminho do arquivo ledger.json")
    p_ver.add_argument("--root", default=None, help="Override do diretório raiz")
    p_ver.add_argument("--min-pct", type=float, default=90.0)
    p_ver.add_argument("--max-out-of-scope-pct", type=float, default=20.0, help="Teto máximo permitido de unidades fora de escopo (padrão 20.0%%)")
    p_ver.add_argument("--max-out-of-scope-per-dir-pct", dest="max_oos_per_dir_pct", type=float, default=20.0, help="Teto de out_of_scope por diretório-pai (padrão 20.0%%, dirs com >=3 unidades)")
    p_ver.add_argument("--against", type=str, default=None, help="Ref git base: arquivo criado na PR marcado out_of_scope -> NEW-FILE-OUT-OF-SCOPE")
    p_ver.add_argument("--require-critic", action="store_true")

    p_ren = sub.add_parser("render-gaps", help="Gera a seção Markdown 'O que NÃO foi verificado'")
    p_ren.add_argument("ledger", help="Caminho do arquivo ledger.json")

    try:
        args = ap.parse_args()
    except SystemExit:
        # --help sai 0 no argparse — um portão sem veredito nunca é verde.
        sys.exit(2)
    try:
        if args.cmd == "init":
            root = Path(args.root)
            if not root.is_dir():
                print(f"ERRO: diretório --root inexistente: {root}", file=sys.stderr)
                sys.exit(2)
            ledger = init_ledger(root, Path(args.out), profile=args.profile)
            print(f"OK: ledger inicializado com {ledger['metadata']['total_units']} unidade(s) "
                  f"(tree_hash={ledger['metadata']['tree_hash'][:12]}...).")
            sys.exit(0)

        elif args.cmd == "verify":
            lpath = Path(args.ledger)
            if not lpath.is_file():
                print(f"ERRO: ledger não encontrado: {lpath}", file=sys.stderr)
                sys.exit(2)
            import math
            # nan/inf desligam comparações (`pct < nan` é sempre False) — flag
            # numérica não-finita é configuração malformada, não threshold
            # válido: erro de uso → exit 2, nunca passa despercebida (A14-banca).
            for _flag_name, _flag_val in (
                ("--min-pct", args.min_pct),
                ("--max-out-of-scope-pct", args.max_out_of_scope_pct),
                ("--max-out-of-scope-per-dir-pct", args.max_oos_per_dir_pct),
            ):
                if not math.isfinite(_flag_val) or _flag_val < 0:
                    print(f"ERRO: {_flag_name}={_flag_val} não é percentual finito >= 0.", file=sys.stderr)
                    sys.exit(2)
            r_over = Path(args.root) if args.root else None
            ok, errors, summary = verify_ledger(
                lpath, root_override=r_over, min_pct=args.min_pct, require_critic=args.require_critic,
                max_out_of_scope_pct=args.max_out_of_scope_pct,
                max_oos_per_dir_pct=args.max_oos_per_dir_pct, against=args.against
            )
            if not summary and errors and errors[0].startswith(("SCHEMA-ERR", "ROOT-ERR")):
                for e in errors:
                    print(e, file=sys.stderr)
                sys.exit(2)
            for e in errors:
                print(f"FAIL: {e}")
            print(json.dumps(summary, ensure_ascii=False))
            sys.stdout.flush()
            sys.exit(0 if ok else 1)

        elif args.cmd == "render-gaps":
            lpath = Path(args.ledger)
            if not lpath.is_file():
                print(f"ERRO: ledger não encontrado: {lpath}", file=sys.stderr)
                sys.exit(2)
            print(render_gaps_markdown(lpath))
            sys.stdout.flush()
            sys.exit(0)
    except BrokenPipeError:
        sys.exit(2)
    except ValueError as e:
        print(f"ERRO: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    # ISO-BOOT canônico (henke/_isoboot.py) — sitecustomize/usercustomize/
    # .pth hostis executam no startup e patcham verificadores no pai; o
    # respawn -Es nasce sem esses canais (prova ESTRUTURAL: sys.flags
    # autenticado + os.sys — nunca variável de ambiente). Texto sincronizado
    # por tests/test_isoboot_guard.py — editar só alterando o canônico.
    import importlib.util as _ilu
    import os.path as _osp
    _ib_p = _osp.join(_osp.dirname(_osp.abspath(__file__)), "_isoboot.py")
    if not _osp.isfile(_ib_p):
        _ib_p = _osp.join(_osp.dirname(_osp.dirname(_osp.abspath(__file__))),
                          "_isoboot.py")
    _ib_spec = _ilu.spec_from_file_location("henke_isoboot", _ib_p)
    _ib_mod = _ilu.module_from_spec(_ib_spec)
    _ib_spec.loader.exec_module(_ib_mod)
    _ib_mod.ensure_isolated(__file__, sys.argv[1:])
    main()
