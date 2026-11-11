"""`henke banca` — Banca Adversarial como feature de primeira classe.

Orquestra as fases MECÂNICAS do Protocolo Goldfish Todos-contra-Todos
(a skill `henke-audit-banca` é a fonte processual; este comando é a
implementação física):

  dossier     Gera o dossiê físico determinístico fora do repo auditado
              (metrics.tsv + hazards.txt + gitlog.txt + ledger.json).
  envelope    Imprime o brief isolado de um auditor (Rodada 1 ou 2) —
              o texto vai por stdin para `agy`, `opencode`, subagentes
              ou qualquer driver externo em contexto limpo.
  consolidate Registra os passes de crítica cruzada no ledger
              (critic_passes[]) e opcionalmente roda
              `coverage_ledger verify --require-critic`.

O despacho dos auditores em si (processos/modelos) fica com o chamador —
este comando nunca executa um driver (mesma disciplina do goldfish).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

_PKG = Path(__file__).resolve().parent
_METRICAS = _PKG / "metricas"

_FRONTS = {
    "seguranca": (
        "Frente SEGURANÇA: cace sinks de injeção (shell/cmd re-parse, "
        "subprocess com dado não-confiável), desserialização insegura, "
        "filesystem/TOCTOU, segredos hardcoded, carregamento dinâmico e "
        "path poisoning. Prove file:line ou declare needs_validation."
    ),
    "corretude": (
        "Frente CORRETUDE: cace bugs de lock/atomicidade, máquina de "
        "estados, hash-chain/log, timeouts, concorrência e invariantes "
        "dos portões determinísticos. Prove file:line ou declare "
        "needs_validation."
    ),
    "falso-verde": (
        "Frente FALSO-VERDE: cace portões burláveis, testes que não podem "
        "falhar, supressões injustificadas, métricas fabricadas, passos "
        "de CI cosméticos e qualquer verde sem evidência física. Prove "
        "file:line ou declare needs_validation."
    ),
}


def _run_gate(script: Path, args: list[str], out_file: Path | None) -> int:
    res = subprocess.run(
        [sys.executable, "-Es", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    text = res.stdout + res.stderr
    if out_file is not None:
        out_file.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return res.returncode


def _cmd_dossier(repo: Path, out_dir: Path) -> int:
    repo = repo.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rc_metrics = _run_gate(
        _PKG / "repo_signals.py", ["metrics", "--root", str(repo)],
        out_dir / "metrics.tsv")
    _run_gate(_METRICAS / "hazard_scan.py", [str(repo)],
              out_dir / "hazards.txt")
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline", "-20"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    (out_dir / "gitlog.txt").write_text(
        log.stdout or "(sem histórico git)", encoding="utf-8")
    _run_gate(
        _METRICAS / "coverage_ledger.py",
        ["init", "--root", str(repo), "--out", str(out_dir / "ledger.json")],
        None)
    print(json.dumps({
        "dossier": str(out_dir), "repo": str(repo),
        "artifacts": sorted(p.name for p in out_dir.iterdir()),
        "metrics_rc": rc_metrics,
    }, indent=1))
    return 0


def _cmd_envelope(dossier: Path, auditor: str, front: str,
                  reports_dir: Path | None) -> int:
    front_txt = _FRONTS.get(front)
    if front_txt is None:
        print(f"ERRO: front '{front}' inválido — "
              f"válidos: {sorted(_FRONTS)}", file=sys.stderr)
        return 2
    others = ""
    if reports_dir is not None and reports_dir.is_dir():
        others = ("\n\nRODADA 2 — relatórios rivais a atacar (confirme, "
                  "refute ou rebaixe CADA achado com file:line):\n" +
                  "\n---\n".join(
                      p.read_text(encoding="utf-8", errors="replace")
                      for p in sorted(reports_dir.glob("*.md"))))
    brief = f"""Você é o auditor '{auditor}' numa banca adversarial
(Todos-contra-Todos, contexto limpo — NUNCA viu este trabalho antes).

DOSSIÊ FÍSICO (dados congelados, fora do repo auditado):
  {dossier}
  - ledger.json  (coverage_ledger init — unidades e hashes)
  - metrics.tsv  (repo_signals metrics — LOC/fan-in/complexidade/cov)
  - hazards.txt  (hazard_scan do repo auditado)
  - gitlog.txt   (histórico recente)

{front_txt}

Regras: máx 5 achados; cada um com file, boundary_crossed, real_impact,
reproduction e confidence. Sem prova física file:line = needs_validation
(nunca severity). Classifique também o que verificou LIMPO.{others}
"""
    sys.stdout.write(brief)
    return 0


def _cmd_consolidate(ledger_path: Path, critic_passes: list[str],
                     verify: bool, min_pct: int) -> int:
    if not ledger_path.is_file():
        print(f"ERRO: ledger '{ledger_path}' inexistente.", file=sys.stderr)
        return 2
    if not critic_passes:
        print("ERRO: --critic <nome> obrigatório (>=1 passe adversarial).",
              file=sys.stderr)
        return 2
    data = json.loads(ledger_path.read_text(encoding="utf-8"))
    passes = data.setdefault("critic_passes", [])
    for name in critic_passes:
        passes.append({"agent": name, "round": "cross-exam",
                       "scope": "banca adversarial"})
    ledger_path.write_text(
        json.dumps(data, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"critic_passes registrados: {len(critic_passes)} "
          f"(total {len(passes)})")
    if verify:
        res = subprocess.run(
            [sys.executable, "-Es",
             str(_METRICAS / "coverage_ledger.py"), "verify",
             str(ledger_path), "--min-pct", str(min_pct), "--require-critic"],
            text=True)
        return res.returncode
    return 0


def cmd_banca(args: argparse.Namespace) -> int:
    if args.banca_cmd == "dossier":
        out = args.out or (Path(tempfile.gettempdir()) / "banca")
        return _cmd_dossier(args.repo, out)
    if args.banca_cmd == "envelope":
        return _cmd_envelope(args.dossier, args.auditor, args.front,
                             args.reports_dir)
    if args.banca_cmd == "consolidate":
        return _cmd_consolidate(args.ledger, args.critic, args.verify,
                                args.min_pct)
    print("ERRO: sub-ação de banca ausente.", file=sys.stderr)
    return 2


def register(sub) -> None:
    p_b = sub.add_parser(
        "banca",
        help="Banca adversarial: dossier físico, envelopes de auditor e "
             "consolidação critic_passes (Protocolo Goldfish T-c-T)")
    bsub = p_b.add_subparsers(dest="banca_cmd")
    b_d = bsub.add_parser("dossier", help="Gera dossiê físico fora do repo")
    b_d.add_argument("--repo", type=Path, default=Path("."))
    b_d.add_argument("--out", type=Path, default=None)
    b_e = bsub.add_parser("envelope", help="Imprime o brief isolado de um auditor")
    b_e.add_argument("--dossier", type=Path, required=True)
    b_e.add_argument("--auditor", required=True)
    b_e.add_argument("--front", default="falso-verde",
                     choices=sorted(_FRONTS))
    b_e.add_argument("--reports-dir", dest="reports_dir", type=Path,
                     default=None)
    b_c = bsub.add_parser("consolidate",
                          help="Registra critic_passes e roda verify")
    b_c.add_argument("--ledger", type=Path, required=True)
    b_c.add_argument("--critic", action="append", default=[])
    b_c.add_argument("--verify", action="store_true")
    b_c.add_argument("--min-pct", type=int, default=90)
