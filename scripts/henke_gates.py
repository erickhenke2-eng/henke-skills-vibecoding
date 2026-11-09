#!/usr/bin/env python3
"""
henke_gates.py — Executor mecânico de portões da Suíte Henke (PROTOCOLO v4.0, proposta M3).

Hoje cada portão roda isolado via `henke gate <nome>` e o loop de reparo é apenas
prosa. Este módulo executa uma LISTA de portões em ordem, coleta evidência mecânica
de cada execução (exit, duração, sha256 do stdout, tail do stderr) e, opcionalmente,
grava o veredito na run ativa (`.henke/runs/<run_id>.json`) via `run_state.record_gate`.

Despacho de cada item de `gates`:
  - nome curto do GATE_MAP ("hazard", "assertless", ...) → mesmo despacho de
    `henke gate`: defaults só para hazard/a11y (`<repo>`) e assertless
    (`<repo>/tests` quando existe, senão `<repo>`); demais rodam sem args;
  - path "scripts/...py" (formato que `route` emite em `gates_required`) → resolve
    contra SUITE_ROOT primeiro e depois contra o repo; se o path pertence ao
    GATE_MAP usa os args default daquele portão, senão roda `python <path> <repo>`;
  - desconhecido → verdict com exit 2 (nunca exceção).

Não para no primeiro vermelho: produz relatório completo na ordem pedida. Aborta
com FileNotFoundError apenas quando o registro falha por run inexistente — o CLI
converte em exit 2. `all_green` exige todo exit em (0, 3), zero skipped e ≥1
exit 0 real — exit 2 reprova; exit 3 é "não aplicável" (portão rodou e declarou
honestamente "nada a verificar": a11y sem UI, mutant sem .py no diff).

Códigos de saída (CLI):
  0  todos os portões aprovados (exit 0) ou n/a (exit 3), com ≥1 verde real
  1  algum portão reprovado (exit ∉ {0,3}) ou zero verde real
  2  erro de uso, route.json inválido ou run inexistente
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = SCRIPTS_DIR.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from scripts._paths import suite_root as _suite_root  # noqa: E402

SUITE_ROOT = _suite_root()  # checkout OU <prefix>/henke_data (wheel)

try:
    # Tabela única da verdade — import real (scripts/ é namespace package PEP 420).
    from scripts.henke_cli import GATE_MAP
except Exception:  # pragma: no cover - fallback defensivo se o import falhar
    _MET = SCRIPTS_DIR / "metricas"
    GATE_MAP = {
        "hazard": _MET / "hazard_scan.py",
        "assertless": _MET / "assertless_scan.py",
        "contracts": _MET / "contract_closure.py",
        "drift": SCRIPTS_DIR / "spec_drift.py",
        "a11y": _MET / "a11y_scan.py",
        "metaconfig": _MET / "metaconfig_diff.py",
        "coverage": _MET / "coverage_ledger.py",
        "ledger": SCRIPTS_DIR / "audit_ledger.py",
        "mutant": _MET / "mutant_hunt.py",
        "custody": SCRIPTS_DIR / "henke_gate.py",
        "fence": _MET / "fence_check.py",
        "ratchet": _MET / "ratchet.py",
        "crap": _MET / "crap_score.py",
        "signals": SCRIPTS_DIR / "repo_signals.py",
    }

try:
    # Função limpa: escrita atômica + FileNotFoundError para run inexistente.
    from scripts.run_state import record_gate as _rs_record_gate
except Exception:  # pragma: no cover - fallback subprocess
    _rs_record_gate = None

MAX_REWORK = 2  # máximo de ciclos de reparo do loop M3 (contrato público)

_STDERR_TAIL_CHARS = 4000
_GATES_COM_REPO_ARG = ("hazard", "a11y")
# Portões que consomem artefatos gerados (ratchet←metrics TSV, fence←edges.json):
# o cmd resolvido aponta para gate_pipeline.py (coletor→portão), mas o id
# canônico continua sendo o do portão real — mandatory_gates casa com a matriz.
# A7 (banca v11): o ramo GATE_MAP compara `script.stem` ('fence_check') e o
# ramo path compara `name` ('fence') — o set precisa casar os dois ou o
# portão caía no despacho errado (exit 2 pra sempre pelo nome).
_PIPELINE_GATES = {"ratchet", "fence", "fence_check"}
_PIPELINE_SCRIPT = SCRIPTS_DIR / "gate_pipeline.py"


def _safe_run_id(run_id: str) -> str:
    """Mesma sanitização de run_state.init_run: alfanumérico, '-' e '_'."""
    return "".join(c for c in str(run_id) if c.isalnum() or c in ("-", "_")).strip()


# Tree vazio universal do git — âncora honesta para diffs quando a run nasce
# num repo sem commits (banca v9 — A3): HEAD vivo é mutável pelo driver.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _looks_like_path(token: str) -> bool:
    return "/" in token or "\\" in token or token.lower().endswith(".py")


def _git_has_head(repo_root: Path) -> bool:
    try:
        return subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "rev-parse", "--verify", "HEAD"], cwd=str(repo_root),
            capture_output=True, timeout=10,
            # F-8 (banca v12): GIT_* herdado redirecionava a consulta para
            # repo estranho — strip consistente com spec_drift/henke_auto.
            env={k: v for k, v in os.environ.items()
                 if not k.startswith("GIT_")
                 } | {"NoDefaultCurrentDirectoryInExePath": "1"},
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_SKIP_REASONS = {
    "ratchet": "exige .henke/ratchet-baseline.tsv — capture o baseline de métricas do projeto uma vez (repo_signals.py metrics --root .)",
    "fence_check": "exige .henke/fence.json — declare o modelo de camadas/módulos do projeto",
    "coverage_ledger": "exige ledger de cobertura — rode `coverage_ledger.py init` antes",
    "spec_drift": "exige spec de referência — a run não declarou `spec_path`",
    "contract_closure": "exige spec de referência — a run não declarou `spec_path`",
    "metaconfig_diff": "exige commit base (HEAD) — repositório sem commits",
}


def _spec_hash(spec_path: Path) -> str:
    """sha256 da spec na forma NORMALIZADA (a mesma que spec_drift verifica) —
    hashear read_bytes cru quebraria em spec CRLF (Windows) com falso-vermelho."""
    try:
        from scripts.spec_drift import compute_spec_hash
    except ImportError:
        try:
            from spec_drift import compute_spec_hash
        except ImportError:
            return hashlib.sha256(spec_path.read_bytes()).hexdigest()
    return compute_spec_hash(spec_path)


def _default_gate_args(name: str | None, repo_root: Path, spec_path: Path | None = None,
                       test_cmd: str | None = None, spec_hash: str | None = None,
                       head_start: str | None = None) -> list[str] | None:
    """Args por portão, espelhando `henke gate`/check. `None` = não autoexecutável
    neste contexto — o veredito vira `skipped` (não-veredito), nunca verde."""
    if name in _GATES_COM_REPO_ARG:
        return [str(repo_root)]
    if name == "mutant":
        # Modo matriz: o próprio portão enumera os .py do diff vs a âncora de
        # nascimento. HEAD vivo deixaria um commit do driver esvaziar o portão
        # (diff vazio → n/a → mandatory satisfeito sem verificar nada).
        if not _git_has_head(repo_root):
            return None
        return ["--auto-diff", "--repo", str(repo_root),
                "--diff-against", head_start or "HEAD",
                "--min-kill-rate", "100", "--test-cmd", test_cmd or ""]
    if name == "assertless":
        tests_dir = repo_root / "tests"
        return [str(tests_dir)] if tests_dir.is_dir() else [str(repo_root)]
    if name == "metaconfig":
        # Âncora de nascimento vence HEAD vivo: um commit feito pelo driver
        # durante a execução não pode cegar o portão sobre o próprio trabalho.
        base = head_start or "HEAD"
        return ["--repo", str(repo_root), "--against", base] if _git_has_head(repo_root) else None
    if name == "signals":
        return ["metrics", "--root", str(repo_root)]
    if name == "drift":
        if spec_path and spec_path.is_file():
            args = ["--repo", str(repo_root), "--spec", str(spec_path),
                    "--expected-hash", spec_hash or _spec_hash(spec_path)]
            # Âncora do diff: head_start gravado no nascimento da run vence HEAD
            # vivo — commits feitos pelo driver durante a execução não escondem
            # o próprio trabalho do diff.
            if head_start:
                args += ["--against", head_start]
            return args
        return None
    if name == "contracts":
        return ["--repo", str(repo_root), "--spec", str(spec_path)] if spec_path and spec_path.is_file() else None
    if name == "coverage":
        led = repo_root / ".henke" / "coverage-ledger.json"
        # --root explícito: sem ele, `verify` adota o `metadata.root` gravado
        # dentro do próprio ledger — autoridade auto-declarada (banca v7).
        return ["verify", str(led), "--root", str(repo_root)] if led.is_file() else None
    if name == "ratchet":
        base = repo_root / ".henke" / "ratchet-baseline.tsv"
        return ["ratchet", "--repo", str(repo_root), "--baseline", str(base)] if base.is_file() else None
    if name == "fence":
        fenc = repo_root / ".henke" / "fence.json"
        return ["fence", "--repo", str(repo_root), "--fence", str(fenc)] if fenc.is_file() else None
    return []


def _resolve_gate(token: str, repo_root: Path, spec_path: Path | None = None,
                  test_cmd: str | None = None, spec_hash: str | None = None,
                  head_start: str | None = None,
                  allow_external: bool = False) -> tuple[str, list[str] | None] | None:
    """Resolve um item de `gates` para (gate_id_canônico, cmd_ou_None, external).
    None = desconhecido; cmd=None = conhecido mas não autoexecutável aqui.
    external=True = script fora da suíte/pacote (opt-in --gates): executa mas
    NUNCA satisfaz portões obrigatórios (banca v9 — A4)."""
    if not token:
        return None
    if token in GATE_MAP:
        script = Path(GATE_MAP[token])
        if not script.is_file():
            return None
        args = _default_gate_args(token, repo_root, spec_path, test_cmd, spec_hash, head_start)
        if args is None:
            return script.stem, None, False
        if script.stem in _PIPELINE_GATES:
            return script.stem, [sys.executable, str(_PIPELINE_SCRIPT), *args], False
        return script.stem, [sys.executable, str(script), *args], False
    if not _looks_like_path(token):
        return None
    cand = Path(token)
    if not cand.is_absolute():
        suite_cand = SUITE_ROOT / cand
        pkg_cand = _PKG_ROOT / cand  # wheel: portões vivem no pacote scripts/, não em henke_data
        # Sem fallback para <repo>/<path>: num repo hostil qualquer .py
        # plantado seria executado "como portão" (banca v7 — F-14). Portão
        # só roda do suite (henke_data) ou do pacote instalado.
        cand = (suite_cand if suite_cand.is_file()
                else pkg_cand if pkg_cand.is_file() else suite_cand)
    if not cand.is_file():
        return None
    resolved = cand.resolve()
    # F-04 (banca v8): o resolvido TEM que viver sob a suíte ou o pacote —
    # `../../evil.py` ou path absoluto escapavam da resolução suite/pkg e
    # executavam script arbitrário "como portão". `allow_external` só vem do
    # `--gates` explícito do CLI (escolha do usuário); --from-route/pipeline
    # são plantáveis e ficam confinados.
    _suite_res = SUITE_ROOT.resolve()
    # M1 (banca v9): em wheel, _PKG_ROOT = site-packages INTEIRO — confinar a
    # ele admitia "requests/models.py" como portão. O sandbox real é scripts/
    # do pacote, não o prefixo.
    _pkg_res = (_PKG_ROOT / "scripts").resolve()
    _external = not (
        resolved == _suite_res or _suite_res in resolved.parents
        or resolved == _pkg_res or _pkg_res in resolved.parents)
    if _external and not allow_external:
        return None
    path_to_name = {os.path.normcase(str(Path(p).resolve())): n for n, p in GATE_MAP.items()}
    name = path_to_name.get(os.path.normcase(str(resolved)))
    args = _default_gate_args(name, repo_root, spec_path, test_cmd, spec_hash, head_start) if name else [str(repo_root)]
    if args is None:
        return resolved.stem, None, _external
    if name in _PIPELINE_GATES:
        return resolved.stem, [sys.executable, str(_PIPELINE_SCRIPT), *args], _external
    return resolved.stem, [sys.executable, str(resolved), *args], _external


def _gate_id_of(token: str) -> str:
    """Identificador canônico do portão (stem do script; o próprio token se nome)."""
    return Path(token).stem if _looks_like_path(token) else token


def _to_text(chunk) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def _record_gate_verdict(
    run_path: Path,
    gate_id: str,
    exit_code: int,
    sha: str,
    ms: int,
    *,
    command: str | None = None,
    evidence_file: str | Path | None = None,
    manual: bool = False,
    external: bool = False,
) -> None:
    """Grava o veredito na run. Levanta FileNotFoundError se a run não existe."""
    if not run_path.is_file():
        raise FileNotFoundError(f"run inexistente para record-gate: {run_path}")
    summary = f"stdout_sha256={sha};duration_ms={ms}"
    if _rs_record_gate is not None:
        _rs_record_gate(
            run_path, gate_id, int(exit_code), summary,
            command=command, evidence_file=evidence_file, manual=manual,
            external=external,
        )
        return
    res = subprocess.run(  # fallback subprocess (mesma CLI real de run_state.py)
        [
            sys.executable, str(SCRIPTS_DIR / "run_state.py"), "record-gate",
            str(run_path), "--gate", gate_id, "--exit-code", str(int(exit_code)),
            "--summary", summary,
            # F-9 (banca v12): o CLI recusa --command/--evidence-file (M2 v9)
            # — passá-los tornava o fallback permanentemente quebrado.
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"record-gate falhou (exit {res.returncode}): {(res.stderr or '').strip()}")


def run_gates(repo_root: str, gates: list[str], run_id: str | None = None,
              record: bool = True, timeout_sec: float = 180.0,
              test_cmd: str | None = None,
              allow_external_gates: bool = False) -> dict:
    """Executa `gates` em ordem e devolve o relatório mecânico.

    Retorna {"verdicts": [{"gate", "cmd", "exit", "duration_ms", "stdout_sha256",
    "stderr_tail"}], "all_green": bool, "ran": int}. `verdicts["gate"]` é o stem
    canônico do script do portão (ex. `hazard_scan`), igual para entrada por nome
    ou por path, e é a mesma chave gravada na run.

    exit 3 = NÃO-APLICÁVEL (n/a): o portão rodou e reportou honestamente "nada a
    verificar" (ex.: a11y sem arquivos de UI, mutant sem .py no diff). Conta como
    veredito físico com evidência, não reprova — mas `all_green` continua exigindo
    ≥1 portão com exit 0 real (n/a nunca vira verde por tautologia).

    Levanta FileNotFoundError (run inexistente), ValueError (run_id inválido) —
    nunca por portão desconhecido (esse vira verdict exit 2).

    `all_green` só é True quando ran>0, zero portões pulados (skipped) e todos os
    exits == 0 — "skip não é verde" (falso-verde do audit do Kimi).
    Com `record=True`, a saída (stdout+stderr) de cada portão é persistida em
    `.henke/evidence/<run_id>/<gate>.out` e registrada como evidência física.
    """
    root = Path(repo_root).resolve()
    run_path: Path | None = None
    if record and run_id:
        safe = _safe_run_id(run_id)
        if not safe:
            raise ValueError(f"run_id inválido: {run_id!r}")
        run_path = root / ".henke" / "runs" / f"{safe}.json"
        if not run_path.is_file():
            raise FileNotFoundError(f"run inexistente para record-gate: {run_path}")

    evidence_dir: Path | None = None
    if run_path is not None:
        evidence_dir = run_path.parent.parent / "evidence" / run_path.stem

    # F-8 (banca v12): portões-filho herdam env — GIT_* do chamador
    # redirecionava o git interno (repo_signals/ratchet/fence) para outro repo.
    child_env = {k: v for k, v in os.environ.items()
                 if not k.startswith("GIT_")}
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    # Windows: sem isso, CreateProcess procura o binário no CWD do processo
    # pai primeiro — um `python.exe` plantado no worktree hostil sequestraria
    # o subprocesso do portão (F-06 banca v7).
    child_env["NoDefaultCurrentDirectoryInExePath"] = "1"

    spec_path: Path | None = None
    spec_hash: str | None = None
    head_start: str | None = None
    if run_path is not None:
        # Âncoras de nascimento: o sidecar LOCAL (fora do repo, intocável pelo
        # driver) vence o arquivo da run — editá-lo é o ataque que esconde o
        # próprio trabalho do diff (A1-banca).
        sidecar = None
        try:
            from scripts.run_state import load_anchor_sidecar
        except ImportError:
            try:
                from run_state import load_anchor_sidecar  # type: ignore
            except ImportError:
                load_anchor_sidecar = None  # type: ignore[assignment]
        if load_anchor_sidecar is not None:  # type: ignore[truthy-function]
            sidecar = load_anchor_sidecar(run_path.stem, repo_root=root)
        try:
            run_meta = json.loads(run_path.read_text(encoding="utf-8"))
            anchor = sidecar if isinstance(sidecar, dict) else run_meta
            spec_raw = anchor.get("spec_path") or ""
            spec_path = Path(spec_raw) if spec_raw else None
            # Âncoras de nascimento: vencem qualquer estado vivo do repo.
            spec_hash = anchor.get("spec_sha256") or None
            head_start = anchor.get("head_start") or None
            if run_path is not None and not head_start:
                # A3 (banca v9): sob custódia, âncora ausente (repo sem
                # commits) NUNCA cai para HEAD vivo nem para
                # gate-baseline.json mutável — a empty-tree do git é a
                # âncora honesta (diff mostra TUDO; portões que exigem
                # commit falham fechado).
                head_start = _EMPTY_TREE
        except (OSError, ValueError, TypeError):
            spec_path = None

    verdicts: list[dict] = []
    skipped: list[dict] = []
    _seen_ids: set[str] = set()
    for raw in gates:
        token = str(raw).strip()
        resolved = _resolve_gate(token, root, spec_path, test_cmd, spec_hash, head_start,
                                 allow_external=allow_external_gates)
        external = False
        # B4 (banca v11): dois tokens com o mesmo gate_id compartilham a chave
        # em run.gates, o .out e o manifesto — last-write-wins silencioso.
        # Duplicata vira não-veredito (exit 2) em vez de sobrescrever.
        _gid = resolved[0] if resolved is not None else _gate_id_of(token)
        # Só colide sob custódia (run_path): fora de run, re-executar o mesmo
        # token é inócuo (modo dry/check record=False).
        _dup = run_path is not None and _gid in _seen_ids
        _seen_ids.add(_gid)
        if _dup:
            resolved = None
        if resolved is None:
            gate_id = _gid if _dup else _gate_id_of(token)
            cmd_str = ""
            exit_code, out, err, ms = 2, "", (
                f"ERRO: portão duplicado '{gate_id}' — mesma chave de "
                "veredito/evidência (last-write-wins recusado)"
                if _dup else f"ERRO: portão desconhecido '{token}'"), 0
        elif resolved[1] is None:
            gate_id = resolved[0]
            skipped.append({"gate": gate_id, "skipped": True,
                            "reason": _SKIP_REASONS.get(gate_id, "sem args autoexecutáveis neste contexto")})
            continue
        else:
            gate_id, cmd, external = resolved
            cmd_str = shlex.join(cmd)
            t0 = time.monotonic()
            # Popen + árvore: timeout mata o processo E seus filhos (pipeline
            # spawna repo_signals + portão real) — subprocess.run só mata o filho
            # direto e deixaria netos órfãos escrevendo evidência.
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(root), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                    errors="replace", env=child_env,
                    start_new_session=(os.name != "nt"),
                )
                try:
                    out, err = proc.communicate(timeout=timeout_sec)
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    _kill_tree(proc)
                    try:
                        # Neto órfão segurando o pipe herdado bloquearia para
                        # sempre — segundo communicate com teto pequeno (B2).
                        out, err = proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        out, err = "", ""
                    exit_code = 2
                    err = (err or "") + f"\n[TIMEOUT] expirou após {timeout_sec}s (árvore encerrada)"
                ms = int(round((time.monotonic() - t0) * 1000))
                out = out or ""
                err = (err or "").strip()
                # Cap de evidência: portão ruidoso não pode encher memória/disco
                # (communicate já bufferizou; o arquivo é o que fica para a
                # auditoria). 4 MiB cobrem qualquer relatório legítimo.
                _CAP = 4 * 1024 * 1024
                if len(out) + len(err) > _CAP:
                    out = out[: _CAP // 2]
                    err = err[: _CAP // 2] + "\n[EVIDENCE-TRUNCATED: saída excedeu 4 MiB]"
            except OSError as exc:
                ms = int(round((time.monotonic() - t0) * 1000))
                exit_code, out, err = 2, "", f"ERRO ao lançar processo: {exc}"

        sha = hashlib.sha256(out.encode("utf-8")).hexdigest()
        evidence_file: Path | None = None
        if evidence_dir is not None:
            safe_gate = "".join(
                c if c.isalnum() or c in ("-", "_", ".") else "_"
                for c in str(gate_id)
            ).strip("._") or "gate"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_file = evidence_dir / f"{safe_gate}.out"
            try:
                evidence_file.write_text((out or "") + (err or ""), encoding="utf-8")
            except OSError:
                evidence_file = None
        if run_path is not None:
            if cmd_str and evidence_file is None:
                # Evidência não pôde ser escrita → não existe tríade → não-veredito
                # honesto (atestação manual exit 2), nunca crash nem verde.
                _record_gate_verdict(
                    run_path, gate_id, 2, sha, ms,
                    command=None, evidence_file=None, manual=True,
                )
                exit_code, cmd_str = 2, f"{cmd_str} [EVIDENCE-WRITE-FAILED]"
            else:
                _record_gate_verdict(
                    run_path, gate_id, exit_code, sha, ms,
                    command=cmd_str or f"gate-token:{token}", evidence_file=evidence_file,
                    external=external,
                )
        verdicts.append({
            "gate": gate_id,
            "cmd": cmd_str,
            "exit": exit_code,
            "na": exit_code == 3,
            "duration_ms": ms,
            "stdout_sha256": sha,
            "stderr_tail": (err or "")[-_STDERR_TAIL_CHARS:],
        })

    return {
        "verdicts": verdicts,
        "skipped": skipped,
        "all_green": (
            bool(verdicts) and not skipped
            and all(v["exit"] in (0, 3) for v in verdicts)
            and any(v["exit"] == 0 for v in verdicts)
        ),
        "ran": len(verdicts),
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    """Mata o processo e TODA a árvore (netos inclusos) — taskkill /T no Windows,
    killpg no POSIX."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
                env={**{k: v for k, v in os.environ.items()
                        if not k.startswith("GIT_")},
                     "NoDefaultCurrentDirectoryInExePath": "1"},
            )
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _load_route_gates(route_path: Path) -> list[str]:
    """Lê `gates_required` de um JSON de saída do route (compact ou full)."""
    data = json.loads(Path(route_path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"route.json inválido: raiz não é objeto em {route_path}")
    req = data.get("gates_required")
    if not isinstance(req, list) or any(not isinstance(g, str) for g in req):
        raise ValueError(f"route.json inválido: 'gates_required' ausente ou malformado em {route_path}")
    return [g.strip() for g in req if g.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="henke_gates.py",
        description="Executor mecânico de portões: roda uma lista de portões, "
                    "coleta evidência e registra vereditos na run ativa.",
    )
    parser.add_argument("--repo", type=Path, default=Path("."), help="Raiz do repositório alvo")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--gates", default=None,
                     help="Portões separados por vírgula (nomes do GATE_MAP ou paths scripts/...py)")
    src.add_argument("--from-route", dest="from_route", type=Path, default=None,
                     help="Arquivo JSON de saída do route (usa 'gates_required')")
    parser.add_argument("--run", dest="run_id", default=None,
                        help="run_id para gravar vereditos em .henke/runs/<id>.json")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="Timeout por portão em segundos (default: 180)")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Emite o relatório completo em JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if args.gates is None and args.from_route is None:
        print("ERRO: informe --gates <lista> ou --from-route <route.json>.", file=sys.stderr)
        return 2
    # F-6 (banca v12): nan/inf passavam por '<= 0' e quebravam communicate().
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        print("ERRO: --timeout deve ser um número finito > 0.", file=sys.stderr)
        return 2

    try:
        if args.from_route is not None:
            gates = _load_route_gates(args.from_route)
            if not gates:
                raise ValueError(f"'gates_required' vazio em {args.from_route}")
        else:
            gates = [t.strip() for t in args.gates.split(",") if t.strip()]
            if not gates:
                raise ValueError("lista --gates vazia")
        result = run_gates(
            args.repo.resolve(), gates,
            run_id=args.run_id, record=bool(args.run_id), timeout_sec=args.timeout,
            # `--gates` é escolha explícita do usuário (scripts fora da suíte
            # permitidos); `--from-route` é plantável e fica confinado.
            allow_external_gates=args.from_route is None,
        )
    except (FileNotFoundError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for v in result["verdicts"]:
            badge = "N/A" if v["exit"] == 3 else ("PASS" if v["exit"] == 0 else "FAIL")
            print(f"[{badge}] {v['gate']} (exit {v['exit']}, {v['duration_ms']} ms)")
            for ln in (v["stderr_tail"] or "").splitlines()[:6]:
                print(f"  {ln}")
        n_ok = sum(1 for v in result["verdicts"] if v["exit"] == 0)
        estado = "VERDE" if result["all_green"] else "VERMELHO"
        extra = f" | run '{args.run_id}' atualizada" if args.run_id else ""
        print(f"RESULTADO: {estado} — {n_ok}/{result['ran']} portões aprovados{extra}.")
    return 0 if result["all_green"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
