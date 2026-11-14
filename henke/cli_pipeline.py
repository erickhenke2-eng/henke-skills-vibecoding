"""cli_pipeline.py — pipeline auto: goldfish (revisão isolada), setup, auto, trigger."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from henke.cli_kernel import (
    _atomic_write_json,
    _now_iso,
)
from henke.cli_runs import _git_diff_snapshot, _resolve_run_ref



# Conteúdo EXATO do stub .henke/POLICY.json criado por `henke setup`
_POLICY_STUB_JSON = (
    '{"policy":"balanced","always_ask":["publish","destructive","metaconfig"],'
    '"auto_approve":["non-publish"],"allowed_cmds":[]}'
)


def _cmd_goldfish(
    repo_root: Path,
    spec_path: str | None = None,
    driver: str | None = None,
    run_ref: str | None = None,
    as_json: bool = False,
) -> int:
    """Certificação Goldfish honesta (PROTOCOLO §3.2 — revisão isolada em contexto zerado).

    - COM `--driver <cmd>`: grava envelope de revisão isolada (spec + diff, SEM
      transcript) em `.henke/runs/<id>.goldfish.json` e retorna 0. O envelope é
      o dossiê a ser submetido ao revisor externo; este comando NÃO executa a
      revisão em si.
    - SEM `--driver`: HARD STOP honesto — registra `review_status=goldfish_unavailable`
      na run (quando `--run` informado) e retorna 2.
    """
    run_path = _resolve_run_ref(repo_root, run_ref) if run_ref else None
    if run_ref and run_path is None:
        print(f"ERRO: run '{run_ref}' não encontrada em {repo_root / '.henke' / 'runs'}.", file=sys.stderr)
        return 2

    if not driver:
        if run_path is not None:
            try:
                from henke.run_state import _file_lock, derive_run_outcome
                with _file_lock(run_path):
                    data = json.loads(run_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("raiz do JSON da run não é um objeto")
                    data["review_status"] = "goldfish_unavailable"
                    data["updated_at"] = _now_iso()
                    try:
                        derived, reasons = derive_run_outcome(data)
                        data["outcome"] = derived
                        data["reasons"] = reasons
                    except (ImportError, ValueError, OSError) as exc:
                        # V-02: engolir Exception apagava o porquê — registra a
                        # falha de derivação no próprio registro da run.
                        data["outcome_derivation_error"] = (
                            f"{type(exc).__name__}: {exc}")
                        print(f"AVISO: derive_run_outcome falhou "
                              f"({exc}) — outcome anterior mantido.",
                              file=sys.stderr)
                    _atomic_write_json(run_path, data)
                print(f"Registrado review_status='goldfish_unavailable' em {run_path}")
            except Exception as exc:
                print(f"ERRO: não foi possível atualizar a run '{run_path}': {exc}", file=sys.stderr)
                return 2
        print(
            "INDISPONÍVEL: certificação Goldfish não executada — nenhum --driver informado.\n"
            "A revisão Goldfish exige um revisor externo em contexto zerado (sem transcript).\n"
            "Para certificar: henke goldfish --driver \"<comando do revisor>\" [--spec <spec>] [--run <id>]\n"
            "Sem driver externo, resta apenas revisão same-context preliminar — "
            "e ela NÃO é certificação Goldfish.",
            file=sys.stderr,
        )
        return 2

    diff_text, diff_origem = _git_diff_snapshot(repo_root)
    spec_text: str | None = None
    spec_status = "não informado"
    if spec_path:
        sp = Path(spec_path)
        if not sp.is_absolute():
            sp = repo_root / spec_path
        # B8 (banca v13): --spec ../../etc/passwd embutia arquivo arbitrário
        # no envelope destinado ao revisor externo.
        try:
            sp = sp.resolve()
            sp.relative_to(repo_root.resolve())
        except (OSError, ValueError):
            spec_status = "fora do confinamento do repo — recusado"
            sp = None
        if sp is not None and sp.is_file():
            try:
                spec_text = sp.read_text(encoding="utf-8", errors="replace")
                spec_status = "lido"
            except OSError as exc:
                spec_status = f"ilegível: {exc}"
        elif sp is not None:
            spec_status = f"não encontrado: {spec_path}"

    run_id = run_path.stem if run_path else f"goldfish-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    envelope = {
        "version": 1,
        "tipo": "goldfish_review_envelope",
        "run_id": run_id,
        "created_at": _now_iso(),
        "repo": str(repo_root),
        "driver": driver,
        "spec_path": spec_path,
        "spec_status": spec_status,
        "spec": spec_text,
        "diff": diff_text,
        "diff_origem": diff_origem,
        "transcript": "omitido intencionalmente — a revisão Goldfish exige contexto zerado (PROTOCOLO §3.2)",
        "instrucoes": (
            "Submeta este envelope a um revisor externo em sessão zerada via o driver informado. "
            "NÃO inclua o transcript desta sessão. Registre o veredito com "
            "'henke run record-gate <run> --gate goldfish_review --exit-code <0|1>'."
        ),
    }
    env_path = repo_root / ".henke" / "runs" / f"{run_id}.goldfish.json"
    if run_path is not None:
        from henke.run_state import _file_lock
        with _file_lock(run_path):
            _atomic_write_json(env_path, envelope)
    else:
        _atomic_write_json(env_path, envelope)

    if as_json:
        print(json.dumps({
            "envelope": str(env_path),
            "run_id": run_id,
            "driver": driver,
            "revisao_executada": False,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"OK: envelope Goldfish gravado em {env_path}")
        print(f"Driver registrado: {driver}")
        print(
            "NOTA: este comando gerou o envelope (spec + diff, sem transcript); "
            "a revisão isolada em si NÃO foi executada aqui."
        )
    return 0


def _cmd_setup(target_dir: Path) -> int:
    """`henke setup [dir]`: detecção via henke_detect + stubs .henke/ sem sobrescrever."""
    target_dir = target_dir.resolve()
    if not target_dir.is_dir():
        print(f"ERRO: diretório inexistente: {target_dir}", file=sys.stderr)
        return 2
    try:
        from henke import henke_detect
    except ImportError:
        print(
            "ERRO: módulo 'henke/henke_detect.py' indisponível — a detecção de projeto\n"
            "(detect_project/write_project) não está instalada neste pacote.\n"
            "Setup abortado sem escrever nada.",
            file=sys.stderr,
        )
        return 2
    detect_project = getattr(henke_detect, "detect_project", None)
    write_project = getattr(henke_detect, "write_project", None)
    if not callable(detect_project) or not callable(write_project):
        print(
            "ERRO: 'henke/henke_detect.py' não expõe detect_project(root)/write_project(root, data).",
            file=sys.stderr,
        )
        return 2
    try:
        data = detect_project(str(target_dir))
        project_path = write_project(str(target_dir), data)
    except Exception as exc:
        print(f"ERRO na detecção do projeto: {exc}", file=sys.stderr)
        return 2

    henke_dir = target_dir / ".henke"
    (henke_dir / "runs").mkdir(parents=True, exist_ok=True)

    linguagens = data.get("linguagens") if isinstance(data, dict) else None
    if isinstance(linguagens, list) and linguagens:
        stack_txt = ", ".join(map(str, linguagens))
    elif isinstance(data, dict) and data.get("framework"):
        stack_txt = str(data.get("framework"))
    else:
        stack_txt = "detectada automaticamente"
    nome = target_dir.name or "PROJETO"

    stubs: dict[str, str] = {
        "DIARIO.md": (
            f"# DIÁRIO DE BORDO VIVO — {nome}\n\n"
            f"## [{_now_iso()}] — Inicialização via `henke setup`\n"
            "- **Autor:** Suíte Henke (`henke setup`)\n"
            "- **Ações Realizadas:**\n"
            "  1. Estrutura `.henke/` inicializada; detecção gravada em `.henke/PROJECT.json`.\n"
            "- **Próximos Passos:**\n"
            "  - Registrar aqui a jornada de desenvolvimento.\n"
        ),
        "DECISOES.md": (
            f"# COFRE DE DECISÕES ARQUITETURAIS — {nome}\n\n"
            "Este documento registra as decisões técnicas fundamentais para impedir "
            "que intervenções futuras de agentes desfaçam escolhas aprovadas.\n\n"
            "| ID | Data | Contexto / Decisão | Justificativa Técnica | Status |\n"
            "|---|---|---|---|---|\n"
        ),
        "CONFIG.json": json.dumps({
            "versao_protocolo": "5.0",
            "stack": stack_txt,
            "economia_tokens": {
                "caveman": "auto",
                "ponytail": "auto",
                "perguntar_novamente": True,
            },
            "auditoria_adversarial": {
                "exigir_ledger_comandos": True,
                "proibir_sr_only_hacks": True,
            },
        }, ensure_ascii=False, indent=2) + "\n",
        "POLICY.json": _POLICY_STUB_JSON + "\n",
    }

    criados: list[str] = []
    preservados: list[str] = []
    for stub_name, conteudo in stubs.items():
        destino = henke_dir / stub_name
        if destino.exists():
            preservados.append(stub_name)
            continue
        destino.write_text(conteudo, encoding="utf-8")
        criados.append(stub_name)

    print(f"OK: detecção gravada em {project_path}")
    if isinstance(data, dict):
        partes = []
        for campo in ("linguagens", "framework", "test_cmd", "build_cmd", "lint_cmd", "source"):
            val = data.get(campo)
            if val:
                if isinstance(val, list):
                    val = ", ".join(map(str, val))
                partes.append(f"{campo}={val}")
        if partes:
            print("Detectado: " + "; ".join(partes))
    print("Criados: " + (", ".join(criados) if criados else "nenhum"))
    print("Preservados (já existiam): " + (", ".join(preservados) if preservados else "nenhum"))
    return 0


def _cmd_auto(
    prompt: str,
    repo: Path,
    driver: str | None,
    dry_run: bool,
    yes: bool,
    policy: str,
    timeout: float | None,
    resume: str | None = None,
) -> int:
    """`henke auto`: despacha para `henke/henke_auto.py:run_auto` (import lazy)."""
    try:
        from henke import henke_auto
    except ImportError:
        print(
            "ERRO: módulo 'henke/henke_auto.py' indisponível — o modo automático\n"
            "não está instalado neste pacote. Nada foi executado.",
            file=sys.stderr,
        )
        return 2
    run_auto = getattr(henke_auto, "run_auto", None)
    if not callable(run_auto):
        print("ERRO: 'henke/henke_auto.py' não expõe run_auto(args).", file=sys.stderr)
        return 2
    ns = argparse.Namespace(
        prompt=prompt,
        repo=str(repo.resolve()),
        driver=driver,
        dry_run=bool(dry_run),
        yes=bool(yes),
        policy=policy,
        timeout=timeout,
        resume=resume,
    )
    # Onda 2+: o single-writer vive dentro de run_auto — cobre `henke auto`,
    # `trigger` e a execução direta de henke/henke_auto.py.
    return int(run_auto(ns))


def _cmd_trigger(kind: str, target: str | None, repo: Path, **kw) -> int:
    """`henke trigger`: evento → prompt → mesma máquina `auto` (entrada não-interativa).

    Onda 2 (Kimi): triggers não-interativos nunca rodam policy=yolo e o
    alvo de `file` é normalizado para um relpath dentro do repositório —
    o caminho cru do evento nunca é interpolado no prompt."""
    repo_root = Path(repo).resolve()
    if kind == "commit":
        prompt = (
            "revisa o último commit (HEAD) e corrige qualquer problema real "
            "encontrado nas mudanças"
        )
    elif kind == "ci":
        prompt = "investiga e corrige a falha do CI/testes"
    elif kind == "file":
        if not target:
            print("ERRO: `henke trigger file` exige um caminho alvo.", file=sys.stderr)
            return 2
        tp = Path(target)
        cand = tp.resolve() if tp.is_absolute() else (repo_root / tp).resolve()
        try:
            rel = cand.relative_to(repo_root).as_posix()
        except ValueError:
            print(f"ERRO: alvo do trigger fora do repositório: {target}", file=sys.stderr)
            return 2
        prompt = f"revisa o arquivo '{rel}' e corrige problemas reais encontrados"
    else:
        return 2
    if kw.get("policy") != "strict":
        if kw.get("policy") == "yolo":
            print("AVISO: trigger não-interativo não aceita policy=yolo — usando 'balanced'.", file=sys.stderr)
        kw["policy"] = "balanced"
    return _cmd_auto(prompt, repo_root, dry_run=False, **kw)
