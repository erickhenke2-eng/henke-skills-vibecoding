#!/usr/bin/env bash
set -e

echo "=== Configurando Suíte Henke no GitHub Codespaces ==="
python -m pip install --upgrade pip
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
fi

echo "=== Validando Skills & Canários ==="
python henke/validate_skills.py
python henke/metricas/hazard_scan.py .

echo "=== Pronto! Digite: python evals/canaries_metricas.py para rodar a suíte completa ==="
