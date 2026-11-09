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
from pathlib import Path
import subprocess
import sys
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
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


@contextlib.contextmanager
def _file_lock(target: Path, timeout: float = 10.0, poll: float = 0.05):
    """Lock interprocessos por arquivo-alvo (`<target>.lock`).

    Stdlib pura e multi-plataforma: `os.open(O_CREAT|O_EXCL)` com retry até
    `timeout`. Cobre a transação read-modify-write inteira — sem ele, dois
    processos leem o mesmo JSON e um write é perdido.
    """
    lock_path = Path(str(Path(target)) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timeout aguardando lock de escrita: {lock_path}")
            time.sleep(poll)
    try:
        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
        yield
    finally:
        os.close(fd)
        try:
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
        return Path(base) / "henke"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "henke"


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


def verify_approval_token(run_data: dict) -> tuple[bool, str]:
    """Confere se existe token LOCAL para o approval_binding desta run.
    Sem binding → nada a verificar (True). Com binding → exige o arquivo de
    aprovação local; sua ausência prova que `henke approve` nunca rodou aqui."""
    binding = run_data.get("approval_binding")
    if not isinstance(binding, dict):
        return True, ""
    token = _binding_token(binding)
    p = _config_dir() / "approvals" / f"{token}.json"
    if not p.is_file():
        return False, (
            "APPROVAL-TOKEN-MISSING: nenhuma aprovação local para este binding — "
            "a run pode ter sido plantada/forjada; rode `henke approve` nesta máquina"
        )
    try:
        stored = json.loads(p.read_text(encoding="utf-8")).get("binding") or {}
    except (OSError, ValueError):
        return False, "APPROVAL-TOKEN-CORRUPT: token local ilegível"
    want = {"head": binding.get("head"), "scope_sha256": binding.get("scope_sha256"),
            "driver": binding.get("driver"), "run_id": binding.get("run_id"),
            "repo": binding.get("repo")}
    if stored != want:
        return False, "APPROVAL-TOKEN-MISMATCH: token local pertence a outro binding"
    return True, ""


def _safe_run_id(run_id: str) -> str:
    """Alfanumérico + `-_` — mesma forma que `init_run` grava no arquivo."""
    return "".join(c for c in str(run_id) if c.isalnum() or c in ("-", "_")).strip()


# Teto de tamanho para evidência de portão (re-hash no verify lê em chunks
# mas não há razão legítima para uma saída de coletor passar disso).
_EVIDENCE_MAX_BYTES = 64 * 1024 * 1024


def _evidence_manifest_path(run_id: str) -> Path:
    safe = _safe_run_id(run_id) or "run"
    return _config_dir() / "evidence-manifest" / f"{safe}.json"


def _append_evidence_manifest(run_id: str, gate: str, evidence_file: str,
                              evidence_sha256: str, command: str) -> None:
    """Acrescenta a tríade ao manifesto LOCAL (~/.config/henke/evidence-manifest/).

    A tríade gravada na run prova "existiu um arquivo com este sha"; o manifesto
    prova "foi `record_gate` DESTA máquina que registrou". Uma run plantada por
    clone (evidência + JSON fabricados no worktree) não tem manifesto local —
    `verify_run_file` reprova cada tríade sem manifesto (EVIDENCE-UNMANIFESTED).
    """
    p = _evidence_manifest_path(run_id)
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
    entries[gate] = {
        "evidence_file": evidence_file,
        "evidence_sha256": evidence_sha256,
        "command": command,
        "recorded_at": _now_iso(),
    }
    _atomic_write_json(p, data)


def _load_evidence_manifest(run_id: str) -> dict | None:
    """Lê o manifesto LOCAL de evidência (None se ausente/inválido)."""
    p = _evidence_manifest_path(run_id)
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
                    "gates_required", "driver", "spec_path", "ignored_start")


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
        p = d / f"{safe}.json"
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


def load_anchor_sidecar(run_id: str, repo_root: Path | str | None = None) -> dict | None:
    """Lê o sidecar local de âncoras (None se ausente/inválido/de outro repo).
    `repo_root` fornecido + sidecar com `repo` divergente → None (replay
    cross-repo não vale como âncora)."""
    safe = _safe_run_id(run_id)
    if not safe:
        return None
    p = _config_dir() / "anchors" / f"{safe}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("run_id") != safe:
        return None
    if repo_root is not None and data.get("repo"):
        try:
            if str(Path(repo_root).resolve()) != str(data["repo"]):
                return None
        except OSError:
            return None
    return data


def _git_head(repo_root: Path) -> str | None:
    """HEAD atual do repo (None se git indisponível/sem commits — nunca inventa)."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root),
             "-c", "core.fsmonitor=false", "-c", "diff.external=",
             "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10,
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
            timeout=30,
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
                for g_name, g_info in physical_gates.items()
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
                    for g_name, g_info in physical_gates.items()
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
                for g_name, g_info in physical_gates.items()
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
    p = Path(run_path)
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
        _tampered_fields = [
            k for k, v in _sc_fields.items() if data.get(k) != v
        ]
        if _tampered_fields:
            return False, [
                "SIDECAR-FIELD-TAMPERED: campo(s) de decisão divergem do "
                "sidecar de nascimento (adulteração mid-run): "
                + ", ".join(sorted(_tampered_fields))
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
    # Manifesto LOCAL de tríades: prova que `record_gate` DESTA máquina
    # registrou a entrada — run plantada por clone não tem manifesto.
    manifest = _load_evidence_manifest(run_id)
    m_entries = (manifest or {}).get("entries") or {}
    seen_evidence: dict[str, str] = {}
    for g_name, g_info in (data.get("gates") or {}).items():
        if not isinstance(g_info, dict):
            continue
        ev = g_info.get("evidence_file")
        sha = g_info.get("evidence_sha256")
        if not ev or not sha:
            continue
        m = m_entries.get(g_name)
        if (
            not isinstance(m, dict)
            or m.get("evidence_sha256") != sha
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
        ok_tok, tok_msg = verify_approval_token(data)
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
        try:
            from scripts.spec_drift import compute_spec_hash
        except ImportError:
            try:
                from spec_drift import compute_spec_hash
            except ImportError:
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
                    confined.write_bytes(resolved.read_bytes())
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
        if evidence_sha is not None:
            entry["command"] = str(command)
            entry["evidence_file"] = str(evidence_path)
            entry["evidence_sha256"] = evidence_sha
            entry["evidence_source"] = str(source_resolved)
            if not manual:
                # Manifesto LOCAL: a tríade só é verificável se constar aqui —
                # uma run plantada não tem manifesto na máquina da vítima.
                # Registros manuais (CLI) NUNCA entram no manifesto: senão o
                # CLI mintava tríade + manifesto e bastava apagar "manual" do
                # JSON para forjar portão físico (F-03 banca v8).
                try:
                    _append_evidence_manifest(
                        run_id, gate_name, str(evidence_path), evidence_sha, str(command))
                except OSError as exc:
                    raise ValueError(
                        f"falha ao gravar manifesto de evidência local: {exc}"
                    ) from exc
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


def approve_run(run_path: Path, approver: str = "human") -> dict:
    """Registra aprovação humana explícita para destravar runs com requires_human_auth.

    Grava `approved_head` (git rev-parse HEAD do repo da run, ou None) como
    evidência informativa — o vínculo executável (`approval_binding`) é criado
    no nascimento da run e checado por `henke auto --resume`.
    """
    with _file_lock(run_path):
        data = _load_run(run_path)
        data["review_status"] = "approved"
        data["approved_by"] = approver.strip() or "human"
        data["approved_at"] = _now_iso()
        repo_root = _repo_root_for_run(run_path)
        approved_head = _git_head(repo_root) if repo_root is not None else None
        data["approved_head"] = approved_head
        # Re-ancora o vínculo executável ao HEAD que o humano está aprovando
        # AGORA — sem isso, um commit legítimo entre aprovação e --resume
        # tornava a run irrecuperável para sempre (approve→commit→resume→stale→loop).
        if isinstance(data.get("approval_binding"), dict):
            data["approval_binding"]["head"] = approved_head
            data["approval_binding"]["driver"] = data.get("driver")
        derived, reasons = derive_run_outcome(data)
        data["outcome"] = derived
        data["reasons"] = reasons
        data["updated_at"] = _now_iso()
        _atomic_write_json(run_path, data)
        # Comprovante LOCAL fora do repo: --resume exige este token — uma run
        # plantada via clone (binding forjado mas self-consistente) não tem
        # token na máquina da vítima e é recusada.
        if isinstance(data.get("approval_binding"), dict):
            write_approval_token(data["approval_binding"],
                                 data["approved_by"], run_path)
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
            has_cmd = bool(args.command and str(args.command).strip())
            has_ev = args.evidence_file is not None
            if has_cmd != has_ev:
                print(
                    "ERRO: evidência incompleta — informe --command E --evidence-file juntos. "
                    "Sem ambos, o veredito vira atestação manual (manual: true) e não conta "
                    "como portão físico.",
                    file=sys.stderr,
                )
                return 2
            # F-01: a superfície CLI NUNCA emite tríade física — evidência
            # física é domínio da pipeline (record_gate via API dentro de
            # run_gates/_run_auto_inner). Um forjador local produziria aqui
            # exatamente o artefato que verify valida.
            data = record_gate(
                args.run_file, args.gate, args.exit_code, args.summary,
                command=args.command, evidence_file=args.evidence_file,
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
            data = approve_run(args.run_file, args.approver)
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
