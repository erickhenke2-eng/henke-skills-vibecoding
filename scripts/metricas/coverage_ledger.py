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

VALID_PROFILES = ("quick", "standard", "deep")
VALID_UNIT_STATUS = ("pending", "covered", "finding_confirmed", "needs_validation", "deferred", "out_of_scope")
VALID_SEVERITIES = ("critical", "high", "medium", "low", "informational")
VALID_ASSUMPTION_STATUS = ("enforced", "unenforced", "unknown")


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

    meta = data["metadata"]
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

    for u in units:
        uid = u.get("unit_id", "<sem-id>")
        rel = u.get("path", "")
        status = u.get("status", "")
        mapped_paths.add(rel)

        if status not in VALID_UNIT_STATUS:
            errors.append(f"INVALID-STATUS [{uid}]: status '{status}' desconhecido")
            continue
        counts[status] += 1

        if rel not in disk_files:
            if status != "out_of_scope":
                errors.append(f"STALE-DELETED [{uid}]: arquivo '{rel}' não existe mais no disco")
        else:
            cur_sha, _ = _file_sha256_and_lines(disk_files[rel])
            if cur_sha != u.get("sha256") and status in ("covered", "finding_confirmed", "needs_validation"):
                errors.append(f"STALE-HASH [{uid}]: arquivo '{rel}' mudou após a auditoria (sha256 divergente)")

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
                if not chk.get("agent_id") or not chk.get("invariant") or not chk.get("result"):
                    errors.append(f"INCOMPLETE-CHECK [{uid}]: check exige 'agent_id', 'invariant' e 'result'")
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
                    tf = step.get("file", "")
                    tline = step.get("line", 0)
                    tpath = (root / tf).resolve()
                    if not tpath.is_file():
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
    dir_totals: dict[str, int] = {}
    dir_oos: dict[str, int] = {}
    for u in units:
        if u.get("status") not in VALID_UNIT_STATUS:
            continue
        parent = u.get("path", "").replace("\\", "/").rsplit("/", 1)[0] or "."
        dir_totals[parent] = dir_totals.get(parent, 0) + 1
        if u.get("status") == "out_of_scope":
            dir_oos[parent] = dir_oos.get(parent, 0) + 1
    for d in sorted(dir_totals):
        if dir_totals[d] >= min_dir_units:
            d_pct = 100.0 * dir_oos.get(d, 0) / dir_totals[d]
            if d_pct > max_oos_per_dir_pct:
                errors.append(
                    f"DILUTED-OUT-OF-SCOPE: diretório '{d}/' tem {d_pct:.1f}% de unidades fora de escopo "
                    f"({dir_oos.get(d, 0)}/{dir_totals[d]}) excedendo o teto por-diretório de {max_oos_per_dir_pct:.1f}%")

    # 3c. Arquivo criado na própria PR não pode ser out_of_scope (diluição por nascença).
    if against:
        res_added = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-status", against, "--"],
            cwd=str(root), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if res_added.returncode == 0:
            added_files = {
                ln.split("\t")[-1].strip().replace("\\", "/")
                for ln in res_added.stdout.splitlines() if ln.startswith("A\t")
            }
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
    has_critic = isinstance(critic_passes, list) and len(critic_passes) > 0
    outcome = "verified" if not errors else "failed"
    if not errors and (profile == "deep" or require_critic) and not has_critic:
        outcome = "partial"
        if require_critic:
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
        if st in ("deferred", "out_of_scope", "pending"):
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
    p_ver.add_argument("--max-out-of-scope-pct", type=float, default=20.0, help="Teto máximo permitido de unidades fora de escopo (padrão 20.0%)")
    p_ver.add_argument("--max-out-of-scope-per-dir-pct", dest="max_oos_per_dir_pct", type=float, default=20.0, help="Teto de out_of_scope por diretório-pai (padrão 20.0%, dirs com >=3 unidades)")
    p_ver.add_argument("--against", type=str, default=None, help="Ref git base: arquivo criado na PR marcado out_of_scope -> NEW-FILE-OUT-OF-SCOPE")
    p_ver.add_argument("--require-critic", action="store_true")

    p_ren = sub.add_parser("render-gaps", help="Gera a seção Markdown 'O que NÃO foi verificado'")
    p_ren.add_argument("ledger", help="Caminho do arquivo ledger.json")

    args = ap.parse_args()
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
    main()
