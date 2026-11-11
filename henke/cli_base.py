"""cli_base.py — comandos básicos: doctor, init, route."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
from henke.cli_kernel import (
    GATE_MAP,
    SCRIPTS_DIR,
    SUITE_ROOT,
    _scrub_env,
)



def _cmd_doctor(repo_root: Path, as_json: bool = False) -> int:
    py_ok = sys.version_info >= (3, 10)
    git_res = subprocess.run(
        ["git", "--version"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    env=_scrub_env(repo_root))
    git_ok = git_res.returncode == 0
    missing_scripts = [name for name, p in sorted(GATE_MAP.items()) if not p.is_file()]
    expected_skills = 58
    skills_count = len(list(SUITE_ROOT.glob("henke-*/SKILL.md")))
    henke_dir = (repo_root / ".henke").is_dir()

    from henke.mission_classifier import classify_repository
    classification = classify_repository(repo_root)
    stacks_detected = [s.name for s in classification.detected_stacks]
    stacks_configured = classification.configured_stacks
    stacks_unconfigured = [s.name for s in classification.unconfigured_stacks]
    active_gates = classification.active_gates

    report = {
        "status": "ok" if (py_ok and git_ok and not missing_scripts
                         and skills_count >= expected_skills) else "degraded",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_ok": py_ok,
        "git_available": git_ok,
        "suite_root": str(SUITE_ROOT),
        "repo_root": str(repo_root),
        "skills_count": skills_count,
        "skills_expected": expected_skills,
        "gates_available": len(GATE_MAP) - len(missing_scripts),
        "missing_gates": missing_scripts,
        "henke_initialized": henke_dir,
        "stacks_detected": stacks_detected,
        "stacks_configured": stacks_configured,
        "stacks_unconfigured": stacks_unconfigured,
        "active_gates": active_gates,
    }
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 1

def _cmd_init(repo_root: Path) -> int:
    henke_dir = repo_root / ".henke"
    (henke_dir / "runs").mkdir(parents=True, exist_ok=True)

    tpl_src = SUITE_ROOT / "docs" / "designs" / "TEMPLATE.md"
    tpl_dst = repo_root / "docs" / "designs" / "TEMPLATE.md"
    if tpl_src.is_file() and not tpl_dst.exists():
        tpl_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tpl_src, tpl_dst)

    if (repo_root / ".git").exists():
        subprocess.run(
            # -Es: filho já nasce isolado (ver henke/_isoboot.py BOOT_FLAG)
            [sys.executable, "-Es", str(SCRIPTS_DIR / "henke_gate.py"), "baseline", "--force"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        env=_scrub_env(repo_root))

    print(f"OK: Suíte Henke inicializada em {repo_root} (.henke/ e docs/designs/ prontos).")
    return 0


def _cmd_route(prompt: str, repo_root: Path, compact: bool = True) -> int:
    from henke.henke_router import route_request

    if not prompt or not prompt.strip():
        print("ERRO: prompt vazio.", file=sys.stderr)
        return 2
    res = route_request(prompt, repo_root=repo_root, suite_root=SUITE_ROOT)
    if compact:
        compact_payload = {
            "mode": res["mode"],
            "archetype": res["archetype"],
            "confidence": res["confidence"],
            "requires_human_auth": res["requires_human_auth"],
            "skills": [s["name"] for s in res.get("skills", [])],
            "gates_required": res.get("gates_required", []),
            "scope_paths": res.get("scope_paths", []),
        }
        print(json.dumps(compact_payload, ensure_ascii=False))
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0
