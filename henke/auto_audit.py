"""auto_audit.py — Auditoria forense pós-driver e pós-rework (files_changed vs diff/ignored/untracked)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import posixpath as _pp
import subprocess

from henke.auto_kernel import (
    _child_env,
    load_anchor_sidecar,
    record_gate,
)

# A3 (banca v10): excluir TODO .henke/ deixava o driver regravar
# ratchet-baseline/fence/coverage-ledger mid-run sem o audit ver — só o estado
# MUTÁVEL da máquina é isento.
_HENKE_MUTABLE = (
    ".henke/runs/", ".henke/evidence/", ".henke/exec_log.jsonl",
    ".henke/events.jsonl",
    ".henke/gate-baseline.json", ".henke/install-manifest.json",
)

# A8 (banca v11) + MÉDIO-3 (banca v15): POLICY/PROJECT/baselines e memória são
# inputs de veredito/envelope — o driver NUNCA pode "declarar" esses arquivos.
_NEVER_DECLARED = {
    ".henke/policy.json", ".henke/project.json",
    ".henke/ratchet-baseline.tsv", ".henke/fence.json",
    ".henke/coverage-ledger.json",
    ".henke/diario.md", ".henke/decisoes.md",
}


def _audit_driver_changes(repo_root: Path, run_path: Path, declared_changes: list[str]) -> None:
    """Auditoria forense files_changed↔diff pós-despacho do driver."""
    touched: set[str] | None = None
    declared: set[str] = set()
    _audit_fail = False
    try:
        _anchor = None
        _rd = None
        try:
            _sc = load_anchor_sidecar(run_path.stem, repo_root=repo_root)
            if isinstance(_sc, dict):
                _anchor = _sc.get("head_start") or None
        except Exception:
            _sc = None
        if _anchor is None:
            _rd = json.loads(run_path.read_text(encoding="utf-8"))
            _anchor = _rd.get("head_start") or None
        from henke.spec_drift import get_touched_files as _gtf
        touched = set(_gtf(repo_root, against=_anchor))
        # Normalização literal: NÃO usar lstrip("./") — devora o ponto de
        # dotfiles (.env → env) e casa declarações erradas.
        declared = {
            str(f).replace("\\", "/").strip().removeprefix("./")
            for f in declared_changes
        }
        undeclared: list[str] | None = sorted(
            t for t in touched
            if t not in declared and not (
                t.startswith(".henke/")
                and any(t.startswith(m) or t == m for m in _HENKE_MUTABLE))
        )
        undeclared = sorted(set(undeclared) | {
            t for t in touched if t.lower() in _NEVER_DECLARED})
        # F2 (banca v8): escrita em path IGNORADO é invisível a todo
        # portão de diff — só o snapshot de nascimento (ancorado no
        # sidecar) vs. o atual revela arquivo escondido.
        new_ignored: list[str] = []
        _ign_start = None
        if isinstance(_sc, dict):
            _ign_start = (_sc.get("fields") or {}).get("ignored_start")
        if _ign_start is None and isinstance(_rd, dict):
            _ign_start = _rd.get("ignored_start")
        if _ign_start is not None:
            _si = subprocess.run(
                ["git", "-c", "core.fsmonitor=false",
                 "-c", "diff.external=", "status", "--porcelain",
                 "-uall", "--ignored", "-z"],
                cwd=str(repo_root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                env=_child_env(repo_root),
            )
            if _si.returncode == 0:
                cur_ig = {
                    e[3:].strip() for e in _si.stdout.split("\0")
                    if e.startswith("!! ") and e[3:].strip()
                }
                _gone_ut: list[str] = []
                _ut_start = None
                if isinstance(_sc, dict):
                    _ut_start = (_sc.get("fields") or {}).get("untracked_start")
                if _ut_start is None and isinstance(_rd, dict):
                    _ut_start = _rd.get("untracked_start")
                if _ut_start is not None:
                    cur_ut = {
                        e[3:].strip() for e in _si.stdout.split("\0")
                        if e.startswith("?? ") and e[3:].strip()
                    }
                    _gone_ut = [
                        f"{p} (untracked deletado)"
                        for p in set(_ut_start) - cur_ut
                        if (not p.startswith(".henke/")
                            or p.lower() in _NEVER_DECLARED)
                        and p not in declared]
                new_ignored = sorted(
                    p for p in cur_ig - set(_ign_start or [])
                    if (not p.startswith(".henke/")
                        or p.lower() in _NEVER_DECLARED)
                    and p not in declared
                )
                gone_ignored = sorted(
                    p for p in set(_ign_start or []) - cur_ig
                    if (not p.startswith(".henke/")
                        or p.lower() in _NEVER_DECLARED)
                    and p not in declared
                )
                new_ignored += [f"{p} (deletado)" for p in gone_ignored]
                new_ignored += _gone_ut
                _ig_hash = None
                if isinstance(_sc, dict):
                    _ig_hash = (_sc.get("fields") or {}).get("ignored_start_hashes")
                if _ig_hash is None and isinstance(_rd, dict):
                    _ig_hash = _rd.get("ignored_start_hashes")
                if isinstance(_ig_hash, dict):
                    for p in set(_ig_hash) & cur_ig:
                        if (p.startswith(".henke/")
                                and p.lower() not in _NEVER_DECLARED) \
                                or p in declared:
                            continue
                        want = _ig_hash.get(p) or {}
                        want_sha = want.get("sha256")
                        if not want_sha:
                            continue
                        cur_p = Path(repo_root) / p
                        try:
                            if cur_p.is_file() and not cur_p.is_symlink():
                                cur_sha = hashlib.sha256(cur_p.read_bytes()).hexdigest()
                                if cur_sha != want_sha or cur_p.stat().st_size != want.get("size"):
                                    new_ignored.append(f"{p} (conteúdo modificado)")
                            else:
                                new_ignored.append(f"{p} (tipo alterado)")
                        except OSError:
                            new_ignored.append(f"{p} (ilegível)")
            elif _si.returncode != 0:
                _audit_fail = True
        ignored_declared: list[str] = []
        if declared:
            _ci = subprocess.run(
                ["git", "-c", "core.fsmonitor=false",
                 "-c", "diff.external=", "check-ignore", "--stdin"],
                cwd=str(repo_root), capture_output=True, text=True,
                input="\n".join(sorted(declared)) + "\n",
                encoding="utf-8", errors="replace", timeout=30,
                env=_child_env(repo_root),
            )
            if _ci.returncode == 0:
                ignored_declared = [
                    l.strip() for l in _ci.stdout.splitlines()
                    if l.strip() and l.strip() not in touched
                ]
            elif _ci.returncode not in (0, 1):
                _audit_fail = True
    except Exception:
        undeclared = None
        ignored_declared = []
        new_ignored = []

    audit_findings: list[tuple[int, str]] = []
    if new_ignored:
        audit_findings.append((2,
            "IGNORED-WRITE-UNDECLARED: arquivo novo em path ignorado "
            "(invisível a todos os portões de diff): "
            + ", ".join(new_ignored[:10])))
    if ignored_declared:
        audit_findings.append((2,
            "FILES-CHANGED-IGNORED: declarados gitignored invisíveis "
            "ao diff — nenhum portão consegue verificá-los: "
            + ", ".join(ignored_declared[:10])))
    if undeclared is None or _audit_fail:
        audit_findings.append((2,
            "AUDIT-UNAVAILABLE: git falhou ao enumerar o diff real — "
            "files_changed não auditado (inconclusivo ≠ verde)"))
    elif undeclared:
        audit_findings.append((1,
            "FILES-CHANGED-INCOMPLETE: driver editou sem declarar: "
            + ", ".join(undeclared[:20])))
    try:
        _touched_set = {str(t).replace("\\", "/").strip() for t in (touched or [])}
        phantom = []
        invalid_declared = []
        for d in (declared or []):
            nd = _pp.normpath(str(d).replace("\\", "/").strip())
            if nd.startswith("../") or nd == ".." or nd.startswith("/") \
                    or _pp.isabs(nd) or (len(nd) > 1 and nd[1] == ":"):
                invalid_declared.append(str(d))
                continue
            if nd not in _touched_set and not nd.startswith(
                    (".henke/runs/", ".henke/evidence/")):
                phantom.append(str(d))
    except Exception:
        phantom = []
        invalid_declared = []
    if invalid_declared:
        audit_findings.append((2,
            "FILES-CHANGED-INVALID: declarados com '..' ou caminho "
            "absoluto — declaração malformada é evasão, não escopo: "
            + ", ".join(sorted(invalid_declared)[:15])))
    if phantom:
        audit_findings.append((2,
            "FILES-CHANGED-PHANTOM: declarados sem contraparte no "
            "diff real — declaração do driver não confere: "
            + ", ".join(sorted(phantom)[:15])))
    if audit_findings:
        record_gate(
            run_path, "files_changed_audit",
            max(c for c, _ in audit_findings),
            " | ".join(m for _, m in audit_findings),
            manual=True,
        )
    else:
        record_gate(
            run_path, "files_changed_audit", 0,
            "files_changed declarado cobre o diff real (fora .henke/)",
            manual=True,
        )


def _audit_rework_changes(repo_root: Path, run_path: Path, driver_result: dict | None) -> None:
    """A4 (banca v11): re-audita files_changed↔diff após ciclos de rework."""
    try:
        from henke.spec_drift import get_touched_files as _gtf2
        _anchor2 = _ign2 = None
        _sc2 = load_anchor_sidecar(run_path.stem, repo_root=repo_root)
        if isinstance(_sc2, dict):
            _anchor2 = _sc2.get("head_start")
            _ign2 = (_sc2.get("fields") or {}).get("ignored_start")
        _decl2 = {
            str(f).replace("\\", "/").strip().removeprefix("./")
            for f in ((driver_result or {}).get("files_changed") or [])
        }
        _und2 = sorted(
            t for t in set(_gtf2(repo_root, against=_anchor2))
            if t not in _decl2 and not (
                t.startswith(".henke/")
                and any(t.startswith(m) or t == m for m in _HENKE_MUTABLE)))
        _newig2: list[str] = []
        if _ign2 is not None:
            _si2 = subprocess.run(
                ["git", "-c", "core.fsmonitor=false",
                 "-c", "diff.external=", "status", "--porcelain",
                 "-uall", "--ignored", "-z"],
                cwd=str(repo_root), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
                env=_child_env(repo_root),
            )
            if _si2.returncode == 0:
                _cur2 = {
                    e[3:].strip() for e in _si2.stdout.split("\0")
                    if e.startswith("!! ") and e[3:].strip()
                }
                _newig2 = sorted(
                    p for p in _cur2 - set(_ign2)
                    if (not p.startswith(".henke/") or p.lower() in _NEVER_DECLARED)
                    and p not in _decl2)
                _newig2 += [
                    f"{p} (deletado)" for p in set(_ign2) - _cur2
                    if (not p.startswith(".henke/") or p.lower() in _NEVER_DECLARED)
                    and p not in _decl2]
        _und2 = sorted(set(_und2) | {
            t for t in set(_gtf2(repo_root, against=_anchor2))
            if t.lower() in _NEVER_DECLARED})
        if _und2 or _newig2:
            record_gate(
                run_path, "files_changed_audit", 1,
                "REWORK-UNDECLARED: edição do rework fora da declaração/"
                f"invisível: {', '.join((_und2 + _newig2)[:20])}")
        else:
            record_gate(
                run_path, "files_changed_audit", 0,
                "rework audit: diff ⊆ declaração, ignorados intactos")
    except Exception as _ae:
        record_gate(run_path, "files_changed_audit", 2,
                    f"rework audit indisponível: {_ae}")
