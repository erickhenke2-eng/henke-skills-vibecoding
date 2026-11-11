"""cli_kernel.py — núcleo compartilhado da CLI: constantes, GATE_MAP, helpers de env/IO."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import sys


for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

# EXEC-WHICH-CWD (banca v11 A4): shutil.which() no processo PAI resolve o
# nome com CWD no Windows (py<3.12 sempre; py>=3.12 só se a var estiver no
# ambiente). Sem isso, `henke exec -- pytest` num repo com pytest.cmd
# plantado executa o impostor e minta evidência verde "honesta" na cadeia.
if sys.platform.startswith("win"):
    os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")

SCRIPTS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = SCRIPTS_DIR.parent
METRICAS_DIR = SCRIPTS_DIR / "metricas"

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from henke._paths import suite_root as _suite_root  # noqa: E402

SUITE_ROOT = _suite_root()  # checkout OU <prefix>/henke_data (wheel)

GATE_MAP: dict[str, Path] = {
    "hazard": METRICAS_DIR / "hazard_scan.py",
    "assertless": METRICAS_DIR / "assertless_scan.py",
    "contracts": METRICAS_DIR / "contract_closure.py",
    "drift": SCRIPTS_DIR / "spec_drift.py",
    "a11y": METRICAS_DIR / "a11y_scan.py",
    "metaconfig": METRICAS_DIR / "metaconfig_diff.py",
    "coverage": METRICAS_DIR / "coverage_ledger.py",
    "ledger": SCRIPTS_DIR / "audit_ledger.py",
    "mutant": METRICAS_DIR / "mutant_hunt.py",
    "custody": SCRIPTS_DIR / "henke_gate.py",
    "fence": METRICAS_DIR / "fence_check.py",
    "ratchet": METRICAS_DIR / "ratchet.py",
    "crap": METRICAS_DIR / "crap_score.py",
    "signals": SCRIPTS_DIR / "repo_signals.py",
}
_PYTEST_FAIL_RE = re.compile(r"(?i)\b(\d+)\s+(?:failed|errors?)\b")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: object) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)


def _scrub_env(repo: Path | None = None) -> dict:
    """Env sanitizado para subprocessos de custódia (A6 banca v16):
    _cmd_check/_cmd_init/_derive_outcome/doctor herdavam PYTHONPATH/GIT_*/
    PATH integral — sitecustomize plantado fabricava vereditos.
    `repo` ancora o filtro de PATH (Delta-F4) — sem ele, entradas de PATH
    dentro do repo auditado sobrevivem e shims mintam vereditos."""
    from henke.state_paths import sanitized_child_env
    e = sanitized_child_env(repo)
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e
