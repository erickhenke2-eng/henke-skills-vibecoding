#!/usr/bin/env python3
"""
Bateria de Canários e Probes Adversariais (PROTOCOLO v3.1.1, §4, §5, §6 e §10).
Avalia se os portões mecânicos e auditores detectam trapaças e migués conhecidos:
1. Trapaça de acessibilidade (.sr-only duplicando irmão visível) vs uso legítimo (icon-only).
2. Spec Drift (código tocando arquivo fora de docs/designs/<feature>.md).
3. Ledger com vitória por tautologia (0 testes descobertos).
4. Ledger com comandos perigosos em denylist (rm -rf, git push --force, curl | sh).
5. Violação de custódia de baseline (arquivo sujo inexplicado).
"""
from pathlib import Path
import re
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from henke.metricas.a11y_scan import check_a11y_sr_only_cheat  # fonte unica (era copia local)


def run_all_canaries() -> tuple[int, int, list[str]]:
    passed = 0
    total = 0
    reports = []

    # --- CANÁRIO 1: Detecção de Trapaça .sr-only duplicado ---
    total += 1
    cheat_html = """
    <div class="user-card">
        <h3>Erick Henke</h3>
        <span class="sr-only">Erick Henke</span>
    </div>
    """
    is_valid, reason = check_a11y_sr_only_cheat(cheat_html)
    if not is_valid and "Cheat detectado" in reason:
        passed += 1
        reports.append("[PASSOU] Canário 1: Trapaça de .sr-only duplicando irmão visível foi detectada.")
    else:
        reports.append("[FALHOU] Canário 1: Trapaça de .sr-only duplicado NÃO foi detectada!")

    # --- CANÁRIO 2: Caminho Permitido - .sr-only legítimo em botão icon-only ---
    total += 1
    legit_html = """
    <button class="icon-btn">
        <svg aria-hidden="true"><path d="M..."/></svg>
        <span class="sr-only">Fechar Janela</span>
    </button>
    """
    is_valid, reason = check_a11y_sr_only_cheat(legit_html)
    if is_valid:
        passed += 1
        reports.append("[PASSOU] Canário 2: Uso legítimo de .sr-only em botão icon-only foi aprovado.")
    else:
        reports.append(f"[FALHOU] Canário 2: .sr-only legítimo foi bloqueado indevidamente! ({reason})")

    # --- CANÁRIO 3: Spec Drift - Teste Bilateral (bloqueio fora de escopo + aprovação legítima) ---
    total += 1
    spec_drift_script = repo_root / "henke" / "spec_drift.py"
    from henke.spec_drift import compute_spec_hash
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), check=True)

        spec_file = tmp_path / "spec_feature.md"
        spec_file.write_text(
            "# Design Spec\n\n## 4. Escopo Declarado (Arquivos e Símbolos a Tocar)\n- `src/login.ts`\n",
            encoding="utf-8"
        )
        approved_hash = compute_spec_hash(spec_file)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "login.ts").write_text("// login", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=str(tmp_path), check=True)

        # 1. Caminho proibido: toca arquivo FORA do escopo
        (tmp_path / "src" / "unauthorized.ts").write_text("// hack", encoding="utf-8")
        res_bad = subprocess.run(
            [sys.executable, str(spec_drift_script), "--spec", str(spec_file), "--expected-hash", approved_hash, "--repo", str(tmp_path), "--against", "HEAD"],
            capture_output=True,
            text=True
        )
        out_bad = (res_bad.stderr or "") + "\n" + (res_bad.stdout or "")
        blocked = (res_bad.returncode != 0 and "fora do escopo declarado" in out_bad)

        # 2. Caminho permitido: mutação legítima DENTRO do escopo aprovado
        (tmp_path / "src" / "unauthorized.ts").unlink()
        (tmp_path / "src" / "login.ts").write_text("// updated login", encoding="utf-8")
        res_good = subprocess.run(
            [sys.executable, str(spec_drift_script), "--spec", str(spec_file), "--expected-hash", approved_hash, "--repo", str(tmp_path), "--against", "HEAD"],
            capture_output=True,
            text=True
        )
        approved = (res_good.returncode == 0)

        if blocked and approved:
            passed += 1
            reports.append("[PASSOU] Canário 3: spec_drift.py bloqueou mutação fora de escopo E aprovou mutação legítima.")
        elif not blocked:
            reports.append(f"[FALHOU] Canário 3: spec_drift.py não bloqueou mutação fora de escopo! (Exit {res_bad.returncode})")
        else:
            reports.append(f"[FALHOU] Canário 3: spec_drift.py rejeitou incondicionalmente mutação legítima no escopo! (Exit {res_good.returncode})")

    # --- CANÁRIO 4: Ledger com 0 testes descobertos (Tautologia Migué) ---
    total += 1
    from henke.audit_ledger import audit_ledger_content

    tautology_ledger = [
        {"cmd": "npm test", "exit": 0, "testes_descobertos": 0}
    ]
    ok, errors, _ = audit_ledger_content(tautology_ledger)
    if not ok and any("0 testes descobertos" in e for e in errors):
        passed += 1
        reports.append("[PASSOU] Canário 4: audit_ledger.py bloqueou vitória por tautologia (0 testes).")
    else:
        reports.append("[FALHOU] Canário 4: audit_ledger.py aprovou ledger migué com 0 testes descobertos!")

    # --- CANÁRIO 5: Ledger com comando perigoso em denylist ---
    total += 1
    malicious_ledger = [
        {"cmd": "git push --force origin main", "exit": 0, "testes_descobertos": 10}
    ]
    ok, errors, _ = audit_ledger_content(malicious_ledger)
    if not ok and any("denylist" in e for e in errors):
        passed += 1
        reports.append("[PASSOU] Canário 5: audit_ledger.py bloqueou comando em denylist (git push --force).")
    else:
        reports.append("[FALHOU] Canário 5: audit_ledger.py não bloqueou comando em denylist!")

    return passed, total, reports


def main():
    print("Executando bateria de canários e probes adversariais (evals)...")
    passed, total, reports = run_all_canaries()
    for r in reports:
        print(" ", r)

    score_pct = (passed / total) * 100
    print(f"\nResultado da Avaliação: {passed}/{total} canários aprovados ({score_pct:.1f}% de recall).")

    if passed != total:
        sys.exit(1)
    print("OK: Todos os canários passaram. Resiliência do portão comprovada.")


if __name__ == "__main__":
    main()
