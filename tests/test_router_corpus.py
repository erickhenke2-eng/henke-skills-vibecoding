#!/usr/bin/env python3
"""
test_router_corpus.py — corpus de regressão do roteador determinístico.

Cada linha de `evals/router_corpus.jsonl` é {"prompt", "expect_top1",
"expect_mode", "expect_auth"} e é roteada via `route_request` — a mesma
função que tests/test_metric_gates.py usa para exercitar o router.

Semântica de expect_top1:
- string : skills[0].name deve ser exatamente ela
- lista  : skills[0].name deve pertencer à lista
- null   : skills deve sair vazio (fora de escopo / sem match útil)

Réguas: modo e auth são exatos por linha (subTest). Casos CRÍTICOS —
destrutivos/publish (expect_auth=True) e ambíguos (top1 null ou lista
disjuntiva) — exigem top1 exato por linha: nenhum erro crítico passa,
diferente da régua anterior de 85% agregado (que tolerava ~20 erros em 147).
O % agregado permanece como métrica informativa + piso de regressão.
"""
from pathlib import Path
import json
import sys
import unittest

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from scripts.henke_router import route_request

CORPUS = repo_root / "evals" / "router_corpus.jsonl"
MIN_TOP1_RATIO = 0.85
MIN_CORPUS_LINES = 30


def _load_corpus() -> list[dict]:
    rows = []
    for raw in CORPUS.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _is_critical(row: dict) -> bool:
    """Crítico = destrutivo/publish (auth) ou ambíguo (top1 null / disjuntivo)."""
    expected = row["expect_top1"]
    return row["expect_auth"] is True or expected is None or isinstance(expected, list)


def _top1_ok(expected, top1) -> bool:
    if expected is None:
        return top1 is None
    if isinstance(expected, str):
        return top1 == expected
    return top1 in expected


class TestRouterCorpus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.rows = _load_corpus()
        cls.results = [(row, route_request(row["prompt"])) for row in cls.rows]

    def test_corpus_size(self):
        self.assertGreaterEqual(
            len(self.rows), MIN_CORPUS_LINES,
            f"corpus com {len(self.rows)} linhas; mínimo {MIN_CORPUS_LINES}")

    def test_mode_and_auth_per_line(self):
        for row, res in self.results:
            with self.subTest(prompt=row["prompt"]):
                self.assertEqual(
                    res["mode"], row["expect_mode"],
                    f"skills={[s['name'] for s in res['skills']]}")
                self.assertEqual(
                    res["requires_human_auth"], row["expect_auth"],
                    f"skills={[s['name'] for s in res['skills']]}")

    def test_top1_critical_lines_exact(self):
        """Destrutivos, publish e ambíguos: acerto exato, sem margem agregada."""
        critical = [(row, res) for row, res in self.results if _is_critical(row)]
        self.assertTrue(critical, "corpus sem casos críticos marcados")
        for row, res in critical:
            with self.subTest(prompt=row["prompt"]):
                names = [s["name"] for s in res["skills"]]
                top1 = names[0] if names else None
                expected = row["expect_top1"]
                self.assertTrue(
                    _top1_ok(expected, top1),
                    f"esperado top1={expected} | obtido top1={top1} "
                    f"skills={names} mode={res['mode']}")

    def test_top1_accuracy_at_least_85pct(self):
        """% agregada: métrica informativa + piso de regressão (não absolve críticos)."""
        misses = []
        for row, res in self.results:
            names = [s["name"] for s in res["skills"]]
            top1 = names[0] if names else None
            if not _top1_ok(row["expect_top1"], top1):
                misses.append(
                    f"prompt={row['prompt']!r} | esperado top1={row['expect_top1']} | "
                    f"obtido top1={top1} skills={names} mode={res['mode']}")
        ratio = 1.0 - (len(misses) / len(self.results) if self.results else 1)
        print(f"\n[router-corpus] top-1 agregado: {ratio:.1%} "
              f"({len(self.results) - len(misses)}/{len(self.results)})")
        self.assertGreaterEqual(
            ratio, MIN_TOP1_RATIO,
            f"top-1 {ratio:.0%} < {MIN_TOP1_RATIO:.0%}:\n" + "\n".join(misses))


if __name__ == "__main__":
    unittest.main(verbosity=2)
