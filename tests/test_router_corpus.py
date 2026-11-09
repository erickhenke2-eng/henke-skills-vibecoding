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

Réguas: modo e auth são exatos por linha; top-1 exige >= 85% de acerto.
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
        fails = []
        for row, res in self.results:
            if res["mode"] != row["expect_mode"] or res["requires_human_auth"] != row["expect_auth"]:
                fails.append(
                    f"prompt={row['prompt']!r} | esperado mode={row['expect_mode']} "
                    f"auth={row['expect_auth']} | obtido mode={res['mode']} "
                    f"auth={res['requires_human_auth']} skills={[s['name'] for s in res['skills']]}")
        self.assertEqual(fails, [], "\n" + "\n".join(fails))

    def test_top1_accuracy_at_least_85pct(self):
        misses = []
        for row, res in self.results:
            expected = row["expect_top1"]
            names = [s["name"] for s in res["skills"]]
            top1 = names[0] if names else None
            if expected is None:
                ok = top1 is None
            elif isinstance(expected, str):
                ok = top1 == expected
            else:
                ok = top1 in expected
            if not ok:
                misses.append(
                    f"prompt={row['prompt']!r} | esperado top1={expected} | "
                    f"obtido top1={top1} skills={names} mode={res['mode']}")
        ratio = 1.0 - (len(misses) / len(self.results) if self.results else 1)
        self.assertGreaterEqual(
            ratio, MIN_TOP1_RATIO,
            f"top-1 {ratio:.0%} < {MIN_TOP1_RATIO:.0%}:\n" + "\n".join(misses))


if __name__ == "__main__":
    unittest.main(verbosity=2)
