#!/usr/bin/env python3
# Portado de Island-Dev-Crew/uncle-bob-skills (MIT) — skills/coverage-gaming-audit/henke/assertless-scan.py
"""assertless-scan.py — find tests that execute code without asserting on it.

Coverage counts execution, not assertion, so an assertion-free suite can drive a
coverage report (and a CRAP score) green while proving nothing. This scanner
parses test files with `ast` — it never imports or executes them — and flags
three gaming patterns per test function:

  NO-ASSERTION  the test's own body contains no assertion at all
  MOCK-ONLY     every assertion only observes a double — `assert_called*()`, or
                an assertion whose every value chain ends in `.called`,
                `.call_count`, `.call_args`, `.call_args_list`, `.mock_calls`,
                `.await_count`, `.await_args` — never the code under test
  SWALLOWED     a broad `except` whose body cannot propagate (no raise, no
                non-tautological assert, no fail, no declared helper), or a
                broad `contextlib.suppress(...)`, so a failure cannot go red

Assertions inside a nested def/lambda are NOT counted — blanket, whether the
test calls that inner function or not, since the parser cannot tell a live
callback from decoration. Delegating assertions to a named helper (nested or
top-level) is legitimate — declare it with --assert-helper, which is a written
excusal, echoed on the run's own output line so a captured artifact carries the
record. Nothing is allow-listed by default: pytest's `raises`/`warns`/
`deprecated_call` count only through a real pytest binding (`pytest.raises`, or
a name imported `from pytest import raises`) AND only as the `context_expr` of
a `with` — a bare `pytest.raises(E)` statement creates and discards the
context, so a `.raises()` method on an arbitrary object is not an assertion,
and neither is `.suppress()`. banca-v18 (rev C-N): `except AssertionError`
counts as a swallower (it catches exactly the assert's failure); `assert m` /
`assert m.foo` / `assert hasattr(m, 'x')` over a Mock-bound name are truthy
tautologies, not assertions; dead branches fold via the literal evaluator
(`if 1 == 2`, `if __name__ == "__main__"`, `while 2-2`, `for _ in range(0)`,
`match <const>: case <const diferente>` — non-literal patterns stay residual);
and `@skip`/`@xfail`/`@expectedFailure`/`@skipif(<const true>)` decorated
functions are never collected, so they are not tests at all.

Watched names are resolved by IMPORT BINDING, not by spelling, so renaming one
at import does not defeat a rule: `from contextlib import suppress as quiet`,
`import contextlib as ctx` and `from builtins import Exception as Boom` land in
the same nets as the plain spellings. Test functions are collected from every
statement body at module scope — inside a version guard, a `try/except
ImportError`, or a nested class — because pytest collects those too and a
partial scan must never read as clean. Filenames are matched case-insensitively
for the same reason: over-collecting a `Test_Billing.py` is a false red, while
skipping it is the false green this scanner exists to catch.

HOISTING A NAME CHANGES NOTHING, in either direction. A name bound only to a
LITERAL contributes exactly what the inline literal contributed: nothing — in
the test, in its class body (`self.EXPECTED_CALLS`), or at module scope. A name
bound only to a MOCK OBSERVATION carries that observation's tips, so `actual =
gw.charge.call_args; assert actual == ((1200,), {})` is the same mock-only claim
as the inline form. A handler body that cannot fail does not rescue a broad
handler, whether it is written `assert True`, `ok = True; assert ok`,
`assert not False`, `assert 1 + 1` or `self.assertTrue(True)` — constant-folding
is done here, over bounded literal operands, never by executing the file.

Files are keyed by `(st_dev, st_ino)`, so the same file reached through `./`,
`//`, a trailing slash, a symlink, a relative and an absolute spelling, a
different Unicode form, or once directly and once through its directory is
scanned once — an inflated `scanned` count is a false evidence line. Source is
decoded as `utf-8-sig`, so a BOM written by a Windows editor is content, not a
parse error.

Exit codes — distinct meanings get distinct codes, and no error path may borrow
a verdict's code:
  0  clean verdict  — every scanned test asserts on something
  1  dirty verdict  — at least one finding
  2  usage / IO / parse error, including `--help`, an unwritable stdout and any
     unhandled internal error — never a verdict
  3  nothing to audit — no test functions found (fail closed; an empty scan
     cannot certify a suite)

Those four are the only codes that leave this process. CPython flushes the std
streams at interpreter shutdown and REPLACES the exit status with 120 when that
flush raises, so a `--help` into a dead stdout pipe — or argparse's own usage
error into a dead stderr — would otherwise report a code this script never
chose. The seal at the bottom of the file flushes both streams itself, downgrades
a verdict whose output never landed to 2, and points the dead fd at /dev/null so
the shutdown flush cannot raise.

This is a screen, not a proof: `assert result is not None` on a function that
can only return that object passes it. A surviving mutant is the proof that an
assertion is missing.
"""
import argparse
import ast
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple

# [HENKE-PATCH] dispatch poliglota: testes TS/JS/Rust/Go via lexer compartilhado.
try:
    from . import polyglot as _pg
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import polyglot as _pg

# [HENKE-PATCH] streams UTF-8 no Windows (cp1252 não derruba o portão)
for _s in (sys.stdout, sys.stderr, sys.stdin):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

TEST_FUNC = re.compile(r"^test[A-Za-z0-9_]*$")
# `\w` rather than [A-Za-z0-9_]: for str patterns Python matches \w against Unicode,
# so `test_café.py` is collected. An ASCII-only class silently skipped any test module
# with a non-ASCII name that pytest itself collects and runs - the scan reported clean
# over tests it never read.
TEST_FILE = re.compile(
    r"^(?:test_\w*|\w+_test)\.py$|.*\.(?:test|spec)\.(?:ts|tsx|js|mjs|cjs|mts|cts|jsx)$|.*_test\.(?:go|rs)$",
    re.IGNORECASE)
UNITTEST_ASSERT = re.compile(r"^(?:assert[A-Z][A-Za-z0-9]*|fail)$")
MOCK_ASSERT = re.compile(r"^assert_[a-z0-9_]+$")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PYTEST_ASSERT = frozenset({"raises", "warns", "deprecated_call"})
MOCK_OBS = frozenset({"called", "call_count", "call_args", "call_args_list",
                      "await_count", "await_args", "mock_calls"})
# banca-v18 (rev C-N): AssertionError entra na rede de engolidores —
# `except AssertionError: pass` captura exatamente a falha do assert, logo um
# try cujo assert é a única "verificação" vira teste que não pode ir vermelho
# (a mesma fome de falso-verde do `except Exception`, só que cirúrgica).
BROAD_EXC = frozenset({"Exception", "BaseException", "AssertionError"})
SELF_NAMES = frozenset({"self", "cls"})

# [HENKE-PATCH] padrões léxicos para testes não-Python (corpo já mascarado).
_JS_EXPECT = re.compile(r"\bexpect\s*\(")
_JS_MATCHER = re.compile(
    r"\)\s*\.\s*(?:not\s*\.\s*)?(?:resolves\s*\.\s*|rejects\s*\.\s*)?[A-Za-z_]\w*\s*\(")
_JS_MOCK_ONLY = re.compile(
    r"toHaveBeenCalled|toBeCalled|toHaveReturned|toHaveBeenCalledWith|"
    r"toHaveBeenCalledTimes|calledTimes|calledWith|\.mock\b")
_JS_OTHER_ASSERT = re.compile(
    r"\bassert(?:\.\w+)?\s*\(|\b(?:assertThat|assertThrows|assertRejects|assertEquals|"
    r"assertMatch|assertNodeContains)\s*\(|\b(?:assert|expect)\.fail\s*\(|"
    r"\bfail\s*\(|\.should\s*\(|chai\b|\bthrow\b")
_JS_CATCH = re.compile(r"\bcatch\s*\(\s*([A-Za-z_$][\w$]*)?\s*[^)]*\)\s*\{|\bcatch\s*\{")
_JS_PROMISE_CATCH = re.compile(r"\.catch\s*\(\s*(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")
_RS_ASSERT = re.compile(
    r"\bassert(?:_eq|_ne|_matches)?\s*!|\bpanic!|should_panic|insta::|assert_fs")
_GO_ASSERT = re.compile(
    r"\bt\.(?:Error|Fatal|Fail)\w*\s*\(|\b(?:assert|require)\.[A-Z]\w+\s*\(|"
    r"\bpanic\s*\("
    # t.Helper() NÃO é asserção — é marcador de stack-frame (A3-banca).
)


class _Fn(NamedTuple):
    lineno: int
    name: str


def scan_polyglot(path: Path, helpers):
    """Scanner léxico para .ts/.tsx/.js/.mjs/.rs/.go (corpo mascarado, O(N)).

    Aproximação declarada (spec v2 §7): conta it()/test()/#[test]/TestX e suas
    asserções por padrão léxico, não por AST. MOCK-ONLY e SWALLOWED só se aplicam
    a JS/TS (idioma de mocks/promises); Rust/Go flagam NO-ASSERTION.
    Retorna (scanned, findings[(veredito, path, _Fn, why)]).
    """
    ext = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"error: cannot read {path}: {e} ({_RECORD})", file=sys.stderr)
        return None, None
    scanned, findings = 0, []
    helper_re = (re.compile(r"\b(?:" + "|".join(re.escape(h) for h in helpers)
                            + r")\s*\(") if helpers else None)
    for name, sl, _el, body in _pg.find_test_blocks(text, ext):
        scanned += 1
        fn = _Fn(sl, name)
        has_helper = bool(helper_re and helper_re.search(body))
        if ext in _pg.JS_EXTS:
            real_expect = mock_expect = 0
            for m in _JS_EXPECT.finditer(body):
                open_p = body.find("(", m.start())
                pe = _pg._paren_end(body, open_p) if open_p != -1 else -1
                chain = body[pe:pe + 400] if pe != -1 else ""
                mm = _JS_MATCHER.search(chain)
                if not mm:
                    continue  # expect(x) solto, sem matcher → não é asserção
                matcher_call = chain[:mm.end()]
                full_expr = body[m.start():pe] + matcher_call
                if _JS_MOCK_ONLY.search(full_expr):
                    mock_expect += 1
                else:
                    real_expect += 1
            other = bool(_JS_OTHER_ASSERT.search(body)) or has_helper
            if real_expect == 0 and not other:
                if mock_expect:
                    findings.append(("MOCK-ONLY", path, fn,
                                     f"{mock_expect} expect(s) on the double, none on the code under test"))
                else:
                    findings.append(("NO-ASSERTION", path, fn,
                                     "executes code, asserts nothing"))
            for cm in _JS_CATCH.finditer(body):
                b = body.find("{", cm.end() - 1)
                e = _pg._brace_end(body, b) if b != -1 else -1
                inner = body[b:e + 1] if e != -1 else ""
                if not inner:
                    continue
                # banca-v18 (rev C-N): `return` NÃO é propagação — encerra o
                # teste cedo com a falha engolida (`catch(e){ return; }`).
                if re.search(r"\b(throw|expect|assert|fail|rejects)\b", inner):
                    continue
                # isenção: catch(e){ x = e } com 'x' inspecionada fora do catch
                # (expect(x), f(x) asserido, ou qualquer uso posterior)
                param = cm.group(1)
                exempt = False
                if param:
                    am = re.search(r"\b([A-Za-z_$][\w$]*)\s*=[^;}\n]*\b" + re.escape(param) + r"\b", inner)
                    if am:
                        var = re.escape(am.group(1))
                        outside = body[:b] + body[e + 1:] if e != -1 else ""
                        if re.search(r"\b" + var + r"\b", outside):
                            exempt = True
                    # banca-v19: catch(e){ return e } (ou return expr-com-e)
                    # NÃO engole — o erro sai como valor de retorno para o
                    # caller do teste inspecionar/asserir (error-codes.test.ts:
                    # `return err` -> `caught` asserido como ZodError depois).
                    # `return`/`return false`/`return null` seguem flagados:
                    # descartam o erro de verdade.
                    if re.search(r"\breturn\b[^;}\n]*\b" + re.escape(param) + r"\b", inner):
                        exempt = True
                if not exempt:
                    findings.append(("SWALLOWED", path, fn,
                                     "catch block that cannot propagate — the test cannot go red"))
                    break  # 1 achado por teste — não duplicar
            for pm in _JS_PROMISE_CATCH.finditer(body):
                tail = body[pm.end():]
                if re.match(r"\s*\{", tail):
                    b2 = body.find("{", pm.end() - 1)
                    e2 = _pg._brace_end(body, b2) if b2 != -1 else -1
                    inner2 = body[b2:e2 + 1] if e2 != -1 else ""
                else:  # forma expressão: () => expr
                    inner2 = tail[:200]
                if not re.search(r"\b(throw|expect|assert|fail|rejects)\b", inner2):
                    findings.append(("HIT-AND-SWALLOW", path, fn,
                                     ".catch(() => ...) without rethrow/assertion — rejection swallowed, test cannot go red"))
                    break
        elif ext == ".rs":
            if not _RS_ASSERT.search(body) and not has_helper:
                findings.append(("NO-ASSERTION", path, fn,
                                 "executes code, asserts nothing"))
        elif ext == ".go":
            if not _GO_ASSERT.search(body) and not has_helper:
                findings.append(("NO-ASSERTION", path, fn,
                                 "executes code, asserts nothing"))
    return scanned, findings

_UNKNOWN = object()          # the literal evaluator could not fold this expression
_MAX_INT, _MAX_LEN, _MAX_POW = 10 ** 18, 4096, 64
_RECORD = "helpers declared: none"   # the excusal record every `error:` line carries


def own_scope(body):
    """Yield every node in this statement list's OWN scope, skipping nested scopes."""
    stack = list(body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


class Bindings(NamedTuple):
    """What the file's own imports bound each watched name to."""
    pytest_mods: frozenset    # module aliases for pytest
    pytest_bare: frozenset    # names imported from pytest
    ctx_mods: frozenset       # module aliases for contextlib
    broad: frozenset          # names bound to Exception / BaseException
    suppress: frozenset       # names bound to contextlib.suppress


class Scope(NamedTuple):
    """What names mean inside one test function."""
    consts: dict         # name -> literal node: contributes what the literal did, nothing
    assigned: dict       # name -> every value bound to it here (None = not an expression)
    class_consts: dict   # enclosing class body's literals, read as `self.NAME`


def bindings(tree):
    """Resolve the watched names by import binding, not by spelling.

    Renaming an import is an idiom swap, not a change to what a test proves, so
    `from contextlib import suppress as quiet` and `from builtins import
    Exception as Boom` must land in the same nets as the plain spellings — and
    `import contextlib as ctx` must carry `ctx.suppress` into the suppress net
    while leaving some arbitrary object's `.suppress()` method outside it.
    """
    mods, bare, ctx = set(), set(), set()
    broad, supp = set(BROAD_EXC), {"suppress"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "pytest":
                    mods.add(a.asname or a.name)
                elif a.name == "contextlib":
                    ctx.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in BROAD_EXC:
                    broad.add(a.asname or a.name)
                elif a.name == "suppress" and node.module == "contextlib":
                    supp.add(a.asname or a.name)
                elif a.name in PYTEST_ASSERT and node.module == "pytest":
                    bare.add(a.asname or a.name)
    return Bindings(frozenset(mods), frozenset(bare), frozenset(ctx),
                    frozenset(broad), frozenset(supp))


def is_literal(node):
    """True when this expression is built only from literals.

    A literal cannot carry a value the code under test produced, so hoisting one
    into a name must not change what an assertion proves. Boolean and comparison
    operators over literals are literal too: `not False` and `1 == 1` vary at
    runtime exactly as much as `True` does.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return (all(k is not None and is_literal(k) for k in node.keys)
                and all(is_literal(v) for v in node.values))
    if isinstance(node, ast.UnaryOp):
        return is_literal(node.operand)
    if isinstance(node, ast.BinOp):
        return is_literal(node.left) and is_literal(node.right)
    if isinstance(node, ast.BoolOp):
        return all(is_literal(v) for v in node.values)
    if isinstance(node, ast.Compare):
        return is_literal(node.left) and all(is_literal(c) for c in node.comparators)
    return False


def _small(v):
    """Bound what the literal evaluator will carry, so no fold can blow up."""
    if v is None or isinstance(v, (bool, float, complex)):
        return True
    if isinstance(v, int):
        return abs(v) < _MAX_INT
    if isinstance(v, (str, bytes, bytearray, list, tuple, set, frozenset, dict)):
        return len(v) < _MAX_LEN
    return False


_UNARY = {ast.Not: lambda a: not a, ast.USub: lambda a: -a,
          ast.UAdd: lambda a: +a, ast.Invert: lambda a: ~a}
_BINARY = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
           ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
           ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
           ast.Pow: lambda a, b: a ** b}
_COMPARE = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
            ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
            ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
            ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}


def literal_value(node, consts, depth=0):
    """Fold an expression built only from literals to its value, else `_UNKNOWN`.

    Nothing is executed and nothing is `eval`'d: the operators are applied here,
    over bounded literal operands, so `not False`, `1 + 1`, `-1`, `1 == 1` and
    `"x" in "xyz"` are recognised as the constant-true asserts they are — each
    one token off `assert True` and just as unable to turn a test red. `is` /
    `is not` are left unfolded because literal identity is not a language
    guarantee, and `**` is folded only for small exponents.
    """
    if depth > 16:
        return _UNKNOWN
    if isinstance(node, ast.Name):
        # Resolução por-ponto: último binding ≤ a linha de USO do nome.
        # Literal → fold; binding não-literal ou inexistente antes do uso →
        # _UNKNOWN. Bindings POSTERIORES ao uso não contaminam (F4).
        hist = consts.get(node.id)
        if not hist:
            return _UNKNOWN
        ln_use = getattr(node, "lineno", -1)
        chosen = None
        for bln, bval in hist:
            if bln <= ln_use:
                chosen = bval
            else:
                break
        node = chosen
        if node is None:
            return _UNKNOWN
    if isinstance(node, ast.Constant):
        return node.value if _small(node.value) else _UNKNOWN
    if isinstance(node, ast.NamedExpr):
        # `assert (ok := True)` — NamedExpr é binding + uso no mesmo nó (B3).
        return literal_value(node.value, consts, depth + 1)
    if isinstance(node, ast.Attribute):
        # Laundering por atributo: `self.ok = True; assert self.ok` — o
        # atributo é binding rastreado com chave pontilhada (B4).
        dotted = _dotted_name(node)
        if dotted is None:
            return _UNKNOWN
        hist = consts.get(dotted)
        if not hist:
            return _UNKNOWN
        ln_use = getattr(node, "lineno", -1)
        chosen = None
        for bln, bval in hist:
            if bln <= ln_use:
                chosen = bval
            else:
                break
        node = chosen
        if node is None:
            return _UNKNOWN
        if isinstance(node, ast.Constant):
            return node.value if _small(node.value) else _UNKNOWN
        return _UNKNOWN
    if isinstance(node, (ast.Tuple, ast.List, ast.Set, ast.Dict)):
        if not is_literal(node):
            return _UNKNOWN
        try:
            value = ast.literal_eval(node)
        except Exception:
            return _UNKNOWN
        return value if _small(value) else _UNKNOWN
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.Or):
            # `X or True` é sempre truthy mesmo com X desconhecido — o or só
            # pode ser falsy se TODOS os operandos forem falsy (B2).
            saw_unknown = False
            for v in node.values:
                value = literal_value(v, consts, depth + 1)
                if value is _UNKNOWN:
                    saw_unknown = True
                    continue
                if bool(value):
                    return value
            return _UNKNOWN if saw_unknown else False
        saw_unknown = False
        for v in node.values:
            value = literal_value(v, consts, depth + 1)
            if value is _UNKNOWN:
                saw_unknown = True
                continue
            if not bool(value):
                return value
        return _UNKNOWN if saw_unknown else True
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        # `assert x == x` / `x is x` / `x != x` — mesmo operando estrutural
        # (Name/atributo/literal, nunca Call não-determinístico) decide sem
        # conhecer o valor (B1).
        l, r = node.left, node.comparators[0]
        if ast.dump(l) == ast.dump(r) and not _contains_call(l):
            same = isinstance(node.ops[0], (ast.Eq, ast.Is))
            diff = isinstance(node.ops[0], (ast.NotEq, ast.IsNot))
            if same or diff:
                return same
    operands, apply = [], None
    if isinstance(node, ast.UnaryOp):
        operands, apply = [node.operand], _UNARY.get(type(node.op))
    elif isinstance(node, ast.BinOp):
        operands, apply = [node.left, node.right], _BINARY.get(type(node.op))
    elif isinstance(node, ast.Compare) and len(node.ops) == 1:
        operands, apply = [node.left, node.comparators[0]], _COMPARE.get(type(node.ops[0]))
    if apply is None:
        return _UNKNOWN
    values = [literal_value(o, consts, depth + 1) for o in operands]
    if any(v is _UNKNOWN or not _small(v) for v in values):
        return _UNKNOWN
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        if not isinstance(values[1], int) or abs(values[1]) > _MAX_POW:
            return _UNKNOWN
    try:
        result = apply(*values)
    except Exception:
        return _UNKNOWN
    return result if _small(result) else _UNKNOWN


def _dotted_name(node) -> str | None:
    """`self.ok.flag` → 'self.ok.flag'; nada que não seja Name.Attribute.*."""
    parts = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return None


def _contains_call(node) -> bool:
    """True se a subárvore contém Call/Subscript — operandos não-determinísticos
    não são tautologia estrutural (`f() == f()` pode divergir)."""
    return any(isinstance(n, (ast.Call, ast.Subscript, ast.Await, ast.Yield))
               for n in ast.walk(node))


def _bound_names(target):
    """Every binding key this assignment target binds — nome puro E chave
    pontilhada de atributo (`self.ok` → 'self.ok'; B4 laundering)."""
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, ast.Attribute):
        dotted = _dotted_name(target)
        if dotted is not None:
            yield dotted
    elif isinstance(target, (ast.Tuple, ast.List)):
        for e in target.elts:
            yield from _bound_names(e)
    elif isinstance(target, ast.Starred):
        yield from _bound_names(target.value)


def _each_binding(body):
    """Yield (targets, value) for every name binding in this scope; value None = not an expression."""
    for node in own_scope(body):
        if isinstance(node, ast.Assign):
            yield node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)):
            yield [node.target], node.value
        elif isinstance(node, ast.AugAssign):
            yield [node.target], None
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            yield [node.target], None
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            yield [node.optional_vars], None


def constant_bindings(body, inherited=None, shadowed=()):
    """Map each bound name to its binding HISTORY: [(lineno, literal|None), ...]
    em ordem de aparição. `None` = binding não-literal naquele ponto.

    Resolução é POR-PONTO (F4 banca v6): `ok = True; assert ok; ok = compute()`
    — a rebind tardia em 12 não lava a tautologia do assert em 8. O estado do
    nome numa linha é o ÚLTIMO binding ≤ essa linha: literal → constante;
    não-literal ou inexistente → não-constante. `shadowed` (parâmetros do
    teste) entram como binding não-literal na linha -1 — um valor de fixture
    nunca lê como literal.
    """
    const: dict[str, list[tuple[int, object]]] = {}
    if inherited:
        for n, hist in inherited.items():
            const[n] = list(hist)
    for n in shadowed:
        const[n] = [(-1, None)]
    for targets, value in _each_binding(body):
        literal = value is not None and is_literal(value)
        ln = getattr(targets[0] if targets else value, "lineno", -1)
        for t in targets:
            for n in _bound_names(t):
                const.setdefault(n, []).append((ln, value if literal else None))
    # _each_binding não garante ordem de fonte — o "último binding ≤ uso" só
    # é correto com histórico ordenado por linha.
    for hist in const.values():
        hist.sort(key=lambda e: e[0])
    return const


def assigned_values(body):
    """Map each name bound in this scope to every value bound to it.

    Read by `observation_tips` to carry a hoisted mock observation through the
    name that holds it — the mirror image of dropping a hoisted literal.
    """
    out = {}
    for targets, value in _each_binding(body):
        for t in targets:
            for n in _bound_names(t):
                out.setdefault(n, []).append(value)
    return out


def const_truth(node, scope):
    """True/False when this expression is a constant of known truthiness, else None.

    `assert True`, `assert 1`, `ok = True; assert ok`, `assert not False` and
    `assert 1 + 1` cannot turn a test red; `assert False, "must not raise"`
    always can, and is a legitimate handler.
    """
    value = literal_value(node, scope.consts)
    return None if value is _UNKNOWN else bool(value)


def is_broad(exc, b):
    """Bare handler, or one naming a broad class — plain, dotted, or renamed at import."""
    if exc is None:
        return True
    parts = exc.elts if isinstance(exc, ast.Tuple) else [exc]
    return any(
        (isinstance(p, ast.Name) and p.id in b.broad)
        or (isinstance(p, ast.Attribute) and p.attr in BROAD_EXC)
        for p in parts
    )


def suppresses_broadly(expr, b):
    """`with suppress(Exception):` eats every failure inside it — no handler node exists.

    Resolved by binding: a bare name bound to `contextlib.suppress` however
    aliased, or `<contextlib-alias>.suppress`. Some arbitrary object's
    `.suppress()` method is not contextlib's, exactly as `.raises()` on an
    arbitrary object is not pytest's.
    """
    if not isinstance(expr, ast.Call):
        return False
    f = expr.func
    if isinstance(f, ast.Attribute):
        hit = f.attr == "suppress" and isinstance(f.value, ast.Name) and f.value.id in b.ctx_mods
    else:
        hit = getattr(f, "id", None) in b.suppress
    return hit and any(is_broad(a, b) for a in expr.args)


def observation_tips(nodes, scope):
    """Terminal name of every value chain in these expressions.

    `gateway.charge.call_args == ((1200,), {})` -> ['call_args']
    `gateway.charge.call_args == expected`      -> ['call_args']  (expected is a literal)
    `actual == ((1200,), {})`                   -> ['call_args']  (actual holds the observation)
    `gateway.charge.call_count == self.N`       -> ['call_count'] (N is a class-body literal)
    `receipt.total == 5`                        -> ['total']
    `assert True`                               -> []          (no chain at all)

    A callee is not a value being asserted, so a Call's func is never a tip, and
    neither is a name — or a `self.NAME` — that only ever held a literal. A name
    bound only to mock observations reports those observations instead of
    itself, resolved transitively with a visited set so `a = gw.charge.called;
    b = a` carries; a name holding anything else is a real value however it is
    spelled.
    """
    tips = []

    def hoisted_mock_tips(name, seen):
        values = scope.assigned.get(name)
        if not values or name in seen:
            return None
        seen = seen | {name}
        carried = []
        for v in values:
            if v is None:
                return None
            got = []
            visit(v, True, got, seen)
            if not got or not all(t in MOCK_OBS for t in got):
                return None
            carried.extend(got)
        return carried

    def visit(node, record, out, seen):
        if isinstance(node, ast.Attribute):
            base = node.value
            if (isinstance(base, ast.Name) and base.id in SELF_NAMES
                    and node.attr in scope.class_consts):
                return
            if record:
                out.append(node.attr)
            visit(base, False, out, seen)
        elif isinstance(node, ast.Name):
            if not record:
                return
            carried = hoisted_mock_tips(node.id, seen)
            if carried is not None:
                out.extend(carried)
            elif node.id not in scope.consts:
                out.append(node.id)
        elif isinstance(node, ast.Call):
            visit(node.func, False, out, seen)
            for arg in node.args:
                visit(arg, True, out, seen)
            for kw in node.keywords:
                visit(kw.value, True, out, seen)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child, True, out, seen)

    for n in nodes:
        visit(n, True, tips, frozenset())
    return tips


def observes_only_mock(nodes, scope):
    """True when the assertion looks only at the double, never at a real value."""
    tips = observation_tips(nodes, scope)
    return bool(tips) and all(t in MOCK_OBS for t in tips)


def call_cannot_fail(name, args, scope):
    """True when a unittest-style assert call is a tautology no run can turn red.

    `self.assertTrue(True)` is the `assert True` of the unittest idiom, and
    `self.assertEqual(1, 1)` is the same claim spelled longer. Only calls whose
    arguments fold to literals are judged; `self.fail(...)` always raises, so it
    is never a tautology.
    """
    if name in ("assertTrue", "assertFalse") and args:
        return const_truth(args[0], scope) is (name == "assertTrue")
    _EQ_POS = {"assertEqual", "assertEquals", "assertIs", "assertDictEqual",
               "assertListEqual", "assertTupleEqual", "assertSetEqual",
               "assertMultiLineEqual", "assertSequenceEqual", "assertCountEqual",
               "assertAlmostEqual"}
    _EQ_NEG = {"assertNotEqual", "assertIsNot", "assertNotAlmostEqual"}
    if name in _EQ_POS | _EQ_NEG and len(args) >= 2:
        # assertEqual(x, x) / assertIs(obj, obj) — tautologia por identidade,
        # sem literal nenhum: mesma expressão dos dois lados nunca falha.
        if ast.dump(args[0]) == ast.dump(args[1]):
            return name in _EQ_POS
        if name in ("assertEqual", "assertEquals", "assertNotEqual"):
            left, right = (literal_value(a, scope.consts) for a in args[:2])
            if left is _UNKNOWN or right is _UNKNOWN:
                return False
            try:
                same = bool(left == right)
            except Exception:
                return False
            return same if name != "assertNotEqual" else not same
    return False


def can_propagate(body, helpers, b, scope, exc_name=None):
    """True when a broad handler body can still turn the test red.

    An `assert` counts only when it could fail: a constant-true assert is the
    one-token evasion off `except Exception: pass`, and it eats the failure
    identically — as does the same tautology written `self.assertTrue(True)`.
    `exc_name` é o nome de `except ... as e`: a exceção capturada é sempre
    truthy, logo `assert e` é tautologia que não propaga (B5). Um
    `try: raise; except: pass` interno também não propaga — a raise está
    contida se todos os handlers internos são broad-e-engolem.
    """
    if exc_name:
        scope = scope._replace(
            consts={**scope.consts,
                    exc_name: [(-1, ast.Constant(value=True))]})
    nodes = list(own_scope(body))
    # Spans de corpo de try cujos handlers TODOS engolem — raises/asserts
    # dentro desses spans estão contidos e não propagam.
    swallowed = []
    for node in nodes:
        if isinstance(node, ast.Try) and node.handlers and node.body:
            if all(is_broad(h.type, b)
                   and not can_propagate(h.body, helpers, b, scope, h.name)
                   for h in node.handlers):
                swallowed.append((node.body[0].lineno,
                                  getattr(node.body[-1], "end_lineno", -1)))

    def _inside_swallowed(n):
        ln = getattr(n, "lineno", -1)
        return any(a <= ln <= bb for a, bb in swallowed)

    for node in nodes:
        if _inside_swallowed(node):
            continue
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Assert):
            if const_truth(node.test, scope) is not True:
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                base = f.value.id if isinstance(f.value, ast.Name) else None
                if f.attr in helpers:
                    return True
                if (UNITTEST_ASSERT.match(f.attr) and (base in SELF_NAMES or base in b.pytest_mods)
                        and not call_cannot_fail(f.attr, node.args, scope)):
                    return True
            elif isinstance(f, ast.Name):
                if f.id in helpers:
                    return True
                if UNITTEST_ASSERT.match(f.id) and not call_cannot_fail(f.id, node.args, scope):
                    return True
    return False


def is_constant_tautology(node):
    """Verifica se a expressão de teste é uma tautologia constante óbvia (ex: assert True, assert 1 == 1, assert not False)."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return True
    if isinstance(node, ast.Compare):
        if isinstance(node.left, ast.Constant) and all(isinstance(c, ast.Constant) for c in node.comparators):
            return True
    if isinstance(node, ast.BoolOp) and all(isinstance(v, ast.Constant) for v in node.values):
        return True
    return False


# banca-v18 (rev C-N): dobragem de ramos mortos NÃO-literais — reutiliza o
# avaliador de literais (literal_value) com `__name__` preso a uma string que
# nunca é "__main__" (sob pytest o módulo é importado pelo nome, então a
# guarda `if __name__ == "__main__":` é inalcançável no collection run).
_DEAD_SCOPE_CONSTS = {"__name__": [(-1, ast.Constant(value="<test-module>"))]}


def _fold_dead(t, consts=None):
    """Dobra expressões constantes de guarda — Compare/BinOp/BoolOp/UnaryOp
    sobre literais e o compare-string de `__name__` (banca-v18 rev C-N).
    `consts` opcional: bindings por-ponto do escopo analisado (nome local
    preso a literal — `done = False; if done:` é ramo morto)."""
    return literal_value(t, consts if consts is not None else _DEAD_SCOPE_CONSTS)


def _dead_false(t, consts=None):
    """True quando o teste do ramo dobra para falsy — o corpo nunca executa."""
    v = _fold_dead(t, consts)
    if v is not _UNKNOWN:
        return not bool(v)
    # +revisão cega: TYPE_CHECKING é False em runtime — bloco só existe
    # pro type-checker; assert/def dentro nunca executa.
    if isinstance(t, ast.Name) and t.id == "TYPE_CHECKING":
        return True
    if (isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"
            and isinstance(t.value, ast.Name) and t.value.id == "typing"):
        return True
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
        return _dead_true(t.operand, consts)
    return False


def _dead_true(t, consts=None):
    """True quando o teste do ramo dobra para truthy — o orelse nunca executa."""
    v = _fold_dead(t, consts)
    if v is not _UNKNOWN:
        return bool(v)
    if isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not):
        return _dead_false(t.operand, consts)
    return False


def _dead_empty_iter(t, consts=None):
    """True quando o iterável do `for` dobra para vazio — corpo nunca executa.

    banca-v18 (rev C-N): `for _ in range(0)`/`range(<const><=0)` (e qualquer
    literal/container dobrável) itera zero vezes."""
    v = _fold_dead(t, consts)
    if v is not _UNKNOWN:
        try:
            return len(v) == 0
        except TypeError:
            pass
    if (isinstance(t, ast.Call) and isinstance(t.func, ast.Name)
            and t.func.id == "range" and t.args and not t.keywords):
        args = [_fold_dead(a, consts) for a in t.args]
        if all(isinstance(a, int) and not isinstance(a, bool) for a in args):
            try:
                return len(range(*args)) == 0
            except (ValueError, TypeError, OverflowError):
                return False
    return False


# banca-v18 (residual 1): nomes builtin resolvíveis em `case Nome(...)` —
# só vale se o nome NÃO foi rebindado no escopo (sombra de builtin → braço
# vivo, conservador).
_MATCH_BUILTIN_CLS = {
    "str": str, "int": int, "float": float, "bool": bool,
    "bytes": bytes, "bytearray": bytearray, "list": list, "tuple": tuple,
    "dict": dict, "set": set, "frozenset": frozenset, "complex": complex,
    "object": object, "type": type,
}
# Literais que NUNCA são sequência para match (PEP 634 exclui str/bytes/
# bytearray explicitamente; números/dict/set/frozenset/None não são
# collections.abc.Sequence).
_MATCH_NONSEQ = (str, bytes, bytearray, dict, set, frozenset,
                 int, float, complex, bool, type(None))
# Literais que NUNCA são Mapping para match.
_MATCH_NONMAP = (str, bytes, bytearray, tuple, list, set, frozenset,
                 int, float, complex, bool, type(None))


def _pattern_dead(subject_v, pat, consts) -> bool:
    """True quando `pat` provadamente NÃO casa o subject constante `subject_v`.

    CONSERVADOR: qualquer incerteza retorna False (braço vivo) — um falso-
    positivo de ramo morto apaga um assert real do scan, que é exatamente o
    falso-verde que este scanner existe para impedir."""
    if isinstance(pat, ast.MatchValue):
        # `case <literal>` — comparação por `==` com o subject.
        v = literal_value(pat.value,
                          consts if consts is not None else _DEAD_SCOPE_CONSTS)
        if v is _UNKNOWN:
            return False
        try:
            return bool(v != subject_v)
        except Exception:
            return False
    if isinstance(pat, ast.MatchSingleton):
        # `case None/True/False` — comparação por `is`, não por `==`:
        # `match 1: case True` não casa (1 is not True).
        return subject_v is not pat.value
    if isinstance(pat, ast.MatchAs):
        if pat.pattern is None:
            return False  # capture (`case x`) / wildcard (`case _`) casa sempre
        return _pattern_dead(subject_v, pat.pattern, consts)
    if isinstance(pat, ast.MatchStar):
        return False  # só existe dentro de MatchSequence — casa sempre
    if isinstance(pat, ast.MatchOr):
        # `case a | b` é morto só se TODOS os braços são mortos.
        return all(_pattern_dead(subject_v, p, consts) for p in pat.patterns)
    if isinstance(pat, ast.MatchSequence):
        nonstar = [p for p in pat.patterns
                   if not isinstance(p, ast.MatchStar)]
        if isinstance(subject_v, (tuple, list)):
            if any(isinstance(p, ast.MatchStar) for p in pat.patterns):
                return len(subject_v) < len(nonstar)
            return len(subject_v) != len(nonstar)
        if isinstance(subject_v, _MATCH_NONSEQ):
            return True  # subject literal nunca é sequência de match
        return False
    if isinstance(pat, ast.MatchMapping):
        if not isinstance(subject_v, dict):
            return isinstance(subject_v, _MATCH_NONMAP)
        # subject dict-literal: chave requerida ausente → braço morto
        # (**rest só recolhe chaves extras, não satisfaz as requeridas).
        for k in pat.keys:
            kv = literal_value(k, consts if consts is not None
                               else _DEAD_SCOPE_CONSTS)
            if kv is _UNKNOWN:
                continue  # chave não-literal (atributo) — conservador
            try:
                if kv not in subject_v:
                    return True
            except TypeError:
                return False
        return False
    if isinstance(pat, ast.MatchClass):
        cls = pat.cls
        name = cls.id if isinstance(cls, ast.Name) else None
        T = _MATCH_BUILTIN_CLS.get(name) if name else None
        if T is None or (consts is not None and name in consts):
            # classe desconhecida, atributo (`mod.C`) ou builtin sombreado
            # por binding local/módulo → incerto → vivo.
            return False
        if T is object:
            return False
        # class pattern usa isinstance — a mesma regra do runtime
        # (isinstance(True, int) é True: `match True: case int()` casa).
        return not isinstance(subject_v, T)
    return False  # pattern desconhecido/futuro → vivo


def _dead_match_case(subject_v, case, consts=None) -> bool:
    """`match <const>` cujo braço provadamente não casa — inalcançável.

    banca-v18 (residual 1): além de `case <const diferente>`, dobra (a) a
    guarda que dobra para falsy (`case x if 1 == 2` é morto para QUALQUER
    subject) e (b) padrões estruturais trivialmente incompatíveis com o
    subject constante — sequência, mapping, classe-builtin, OR e singleton.
    `match (1,2): case [a,b]` NÃO dobra: tupla é sequência e casa em runtime
    — falso-positivo de braço morto é pior que falso-negativo."""
    if case.guard is not None:
        gv = _fold_dead(case.guard, consts)
        if gv is not _UNKNOWN and not bool(gv):
            return True  # guarda sempre falsa → braço morto (qualquer subject)
    if subject_v is _UNKNOWN:
        return False
    return _pattern_dead(subject_v, case.pattern, consts)


def _terminating_stmt(s) -> bool:
    if isinstance(s, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
        f = s.value.func
        attr = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        # +revisão cega: pytest.fail()/sys.exit() também terminam o bloco.
        return attr in ("skip", "skipTest", "xfail", "skipif",
                        "fail", "exit")
    return False


def _live_stmt_children(node):
    """Itera os filhos-statement de `node`, podando corpos provadamente mortos
    (`if`/`while`/`for`/`match` com guarda dobrável) — banca-v18 (rev C-N)."""
    if isinstance(node, ast.If):
        if _dead_false(node.test):
            yield from node.orelse
            return
        if _dead_true(node.test):
            yield from node.body
            return
        yield from node.body
        yield from node.orelse
        return
    if isinstance(node, ast.While):
        if not _dead_false(node.test):
            yield from node.body
        yield from node.orelse
        return
    if isinstance(node, (ast.For, ast.AsyncFor)):
        if not _dead_empty_iter(node.iter):
            yield from node.body
        yield from node.orelse
        return
    match_t = getattr(ast, "Match", None)
    if match_t is not None and isinstance(node, match_t):
        subject_v = _fold_dead(node.subject)
        for c in node.cases:
            if not _dead_match_case(subject_v, c):
                yield c
        return
    for child in ast.iter_child_nodes(node):
        yield child


def _dead_assert_ids(stmts, consts=None):
    """BANCA-C4: ids de ast.Assert em ramos mortos — `if False:`, `while
    False:`, `for _ in []:` e qualquer stmt após return/raise/continue/break/
    skip() no mesmo bloco nunca executam; contá-los como 'real' fabricava
    falso-verde um token além de `assert True`.

    banca-v18 (rev C-N): ramos mortos NÃO-literais (`if 1 == 2`,
    `if __name__ == "__main__"`, `while 2-2`, `for _ in range(0)`,
    `match <const>: case <outro const>`) agora dobram via literal_value —
    antes cada assert morto contava como vivo. `consts` traz os bindings
    por-ponto do escopo (nome local preso a literal também dobra)."""
    dead = set()
    match_t = getattr(ast, "Match", None)
    # __name__ é default; um binding local explícito de __name__ prevalece.
    _consts = {**_DEAD_SCOPE_CONSTS, **(consts or {})}

    def _mark_dead(block):
        for s in block:
            for n in ast.walk(s):
                if isinstance(n, ast.Assert):
                    dead.add(id(n))

    def _walk(block):
        terminated = False
        for s in block:
            if terminated:
                _mark_dead([s])
                continue
            if isinstance(s, ast.If):
                if _dead_false(s.test, _consts):
                    _mark_dead(s.body)
                    _walk(s.orelse)
                elif _dead_true(s.test, _consts):
                    _mark_dead(s.orelse)
                    _walk(s.body)
                else:
                    _walk(s.body)
                    _walk(s.orelse)
            elif isinstance(s, ast.While):
                if _dead_false(s.test, _consts):
                    _mark_dead(s.body)
                else:
                    _walk(s.body)
                _walk(s.orelse)
            elif isinstance(s, (ast.For, ast.AsyncFor)):
                if _dead_empty_iter(s.iter, _consts):
                    _mark_dead(s.body)
                else:
                    _walk(s.body)
                _walk(s.orelse)
            elif match_t is not None and isinstance(s, match_t):
                # banca-v18 (rev C-N + residual 1): braço `case <const
                # diferente>` — ou padrão estrutural que provadamente não
                # casa o subject constante / guarda que dobra falsa — é
                # inalcançável; seus asserts são mortos.
                subject_v = _fold_dead(s.subject, _consts)
                for c in s.cases:
                    if _dead_match_case(subject_v, c, _consts):
                        _mark_dead(c.body)
                    else:
                        _walk(c.body)
            else:
                for field in ("body", "orelse", "finalbody"):
                    _walk(getattr(s, field, []) or [])
                for h in getattr(s, "handlers", []) or []:
                    _walk(h.body)
                # +revisão cega: match-statement guarda corpos em `cases`.
                for c in getattr(s, "cases", []) or []:
                    _walk(c.body)
            if _terminating_stmt(s):
                terminated = True

    _walk(list(stmts))
    return dead


# banca-v18 (residual 2): asserts unittest de contexto (`with X.assert...`)
# que PRECISAM de callable posicional fora de `with` — o retorno é um
# context manager; descartá-lo cria e joga fora o contexto sem executar nada.
_CM_ASSERT_CALLABLE_MIN_ARGS = {
    "assertRaises": 2,        # (exc, callable=None, *args)
    "assertRaisesRegex": 3,   # (exc, regex, callable=None, *args)
    "assertRaisesRegexp": 3,  # alias legado
    "assertWarns": 2,         # (warning, callable=None, *args)
    "assertWarnsRegex": 3,    # (warning, regex, callable=None, *args)
    "assertWarnsRegexp": 3,   # alias legado
}
_CM_ONLY_ASSERTS = {"assertLogs", "assertNoLogs"}  # só existem como CM
_CM_ASSERT_ALL = frozenset(_CM_ASSERT_CALLABLE_MIN_ARGS) | _CM_ONLY_ASSERTS


def _testcase_bound_name(name, scope) -> bool:
    """`nome` preso neste teste a uma instância TestCase — `case = MyTest()`
    (classe com 'Test'/'Case' no nome do chamável) ou `case = self`/`cls`.

    Exige TODOS os bindings qualificados: um único binding não-TestCase e o
    `.assertRaises` sobre o nome pode ser método comum — aí contar seria o
    falso-verde que motiva o check."""
    values = scope.assigned.get(name)
    if not values:
        return False
    for v in values:
        if isinstance(v, ast.Name) and v.id in SELF_NAMES:
            continue
        if isinstance(v, ast.Call):
            f = v.func
            leaf = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if "Test" in leaf or "Case" in leaf:
                continue
        return False
    return True


def _cm_assert_base(expr, scope) -> bool:
    """Base `X` de `X.assertRaises`/`assertWarns`/`assertLogs` resolve para um
    TestCase: `self`/`cls`, ou nome preso a instância TestCase no mesmo teste."""
    if not isinstance(expr, ast.Name):
        return False
    return expr.id in SELF_NAMES or _testcase_bound_name(expr.id, scope)


# banca-v18 (rev C-N): fábricas cujo retorno é sempre um objeto-Mock truthy.
_MOCK_FACTORIES = frozenset({
    "Mock", "MagicMock", "AsyncMock", "NonCallableMock",
    "NonCallableMagicMock", "PropertyMock", "create_autospec",
})


def _is_mock_factory_call(v) -> bool:
    """True quando `v` é um Call para uma fábrica de Mock conhecida (bare ou
    atributo — `Mock()`, `mock.MagicMock()`, `m.mock.Mock()`)."""
    if not isinstance(v, ast.Call):
        return False
    f = v.func
    leaf = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
    return leaf in _MOCK_FACTORIES


def _mock_bound_names(scope) -> frozenset:
    """Nomes (incl. chaves pontilhadas `self.m`) cujos bindings são TODOS
    chamadas a fábricas de Mock — `assert nome`/`assert nome.attr` sobre eles
    é truthy-tautológico, nunca vai vermelho (banca-v18 rev C-N)."""
    names = set()
    for name, values in scope.assigned.items():
        if values and all(_is_mock_factory_call(v) for v in values):
            names.add(name)
    return frozenset(names)


def _truthy_mock_assert(test, scope, mock_names) -> bool:
    """True quando o assert olha um objeto Mock sem comparação — `assert m`,
    `assert m.foo`, `assert hasattr(m, 'x')` — child-mock/self são sempre
    truthy, logo a asserção não pode falhar (banca-v18 rev C-N).

    Atributo em MOCK_OBS (`m.called`, `m.call_count`) NÃO entra — é
    observação real do double e segue o caminho mock-only. Nome/atributo
    rebindado para valor não-Mock também segue o caminho normal."""
    if isinstance(test, ast.Name):
        return test.id in mock_names
    if isinstance(test, ast.Attribute):
        if test.attr in MOCK_OBS:
            return False
        cur = test
        while isinstance(cur, ast.Attribute):
            dotted = _dotted_name(cur)
            if dotted is not None and dotted in scope.assigned:
                # atributo rebindado explicitamente — truthy só se TODOS os
                # bindings forem mocks.
                return dotted in mock_names
            cur = cur.value
        return isinstance(cur, ast.Name) and cur.id in mock_names
    if (isinstance(test, ast.Call) and isinstance(test.func, ast.Name)
            and test.func.id == "hasattr" and test.args
            and "hasattr" not in scope.assigned):
        cur = test.args[0]
        while isinstance(cur, ast.Attribute):
            dotted = _dotted_name(cur)
            if dotted is not None and dotted in scope.assigned:
                return dotted in mock_names
            cur = cur.value
        return isinstance(cur, ast.Name) and cur.id in mock_names
    return False


def classify(fn, helpers, b, module_consts, class_consts):
    """Return (real_assertions, mock_assertions, swallow_reason_or_None)."""
    real = mock = 0
    swallow = None
    params = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
    scope = Scope(constant_bindings(fn.body, module_consts, params),
                  assigned_values(fn.body), class_consts)
    dead = _dead_assert_ids(fn.body, scope.consts)
    mock_names = _mock_bound_names(scope)
    for node in own_scope(fn.body):
        if isinstance(node, ast.Assert):
            if id(node) in dead:
                continue
            # const_truth resolve `ok = True; assert ok` / `assert not done`
            # (done=False) — a mesma tautologia que is_constant_tautology pega
            # em literais, mas através de nomes vinculados (C2-banca).
            if is_constant_tautology(node.test) or const_truth(node.test, scope) is True:
                pass
            elif _truthy_mock_assert(node.test, scope, mock_names):
                # banca-v18 (rev C-N): `assert m`/`assert m.foo`/`assert
                # hasattr(m, 'x')` sobre Mock — truthy-assert que nunca vai
                # vermelho; não é asserção real nem observação do double.
                pass
            elif observes_only_mock([node.test], scope):
                mock += 1
            else:
                real += 1
        elif isinstance(node, ast.ExceptHandler):
            if not swallow and is_broad(node.type, b) and not can_propagate(node.body, helpers, b, scope, node.name):
                swallow = "broad except whose body cannot propagate — the test cannot go red"
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            # A17 (banca v16): `with pytest.raises(E):`/`assertRaises`/`fail()`
            # é a forma canônica de asserção negativa — conta como asserção.
            # banca-v18 (rev C-N): só conta quando o objeto-base resolve para
            # pytest (módulo ou `from pytest import`) ou self/cls do unittest —
            # `.raises`/`.assertRaises` sobre base arbitrária
            # (`MagicMock().raises(E)`) é chamável comum descartado, não
            # contexto de asserção.
            for _i in node.items:
                _ce = _i.context_expr
                # banca-v18 (residual 2): `cm = self.assertRaises(E); with cm:`
                # — o CM hoistado num nome é o mesmo contexto de asserção.
                if isinstance(_ce, ast.Name):
                    _vs = scope.assigned.get(_ce.id) or []
                    if _vs and all(
                            isinstance(_v, ast.Call)
                            and isinstance(_v.func, ast.Attribute)
                            and _v.func.attr in _CM_ASSERT_ALL
                            and _cm_assert_base(_v.func.value, scope)
                            for _v in _vs):
                        real += 1
                    continue
                _f = _ce.func if isinstance(_ce, ast.Call) else None
                if _f is None:
                    continue
                if isinstance(_f, ast.Name):
                    if _f.id in b.pytest_bare:
                        real += 1
                    continue
                if not isinstance(_f, ast.Attribute):
                    continue
                _attr = _f.attr
                _base = (_f.value.id if isinstance(_f.value, ast.Name)
                         else None)
                if ((_attr in PYTEST_ASSERT or _attr == "fail")
                        and _base in b.pytest_mods):
                    real += 1
                elif (_attr in _CM_ASSERT_ALL
                        and _cm_assert_base(_f.value, scope)):
                    # banca-v18 (residual 2): `with case.assertRaises(E)` —
                    # `case` preso a `MyTest()`/`self` é contexto unittest
                    # igual a `with self.assertRaises(E)`.
                    real += 1
            if not swallow and any(suppresses_broadly(i.context_expr, b) for i in node.items):
                swallow = "suppress() of a broad exception — the test cannot go red"
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                base = f.value.id if isinstance(f.value, ast.Name) else None
                _tc_base = base in SELF_NAMES or _testcase_bound_name(base, scope)
                if UNITTEST_ASSERT.match(f.attr) and _tc_base:
                    # banca-v18 (residual 2): bare `self.assertRaises(E)` /
                    # `assertWarns(W)` / `assertLogs()` SEM callable posicional
                    # — retorna context manager descartado (cria e joga fora
                    # o contexto; nada executa, nada falha). Só é asserção na
                    # posição `context_expr` de `with` ou com callable:
                    # `self.assertRaises(E, f)` executa f esperando E.
                    if (f.attr in _CM_ONLY_ASSERTS
                            or (f.attr in _CM_ASSERT_CALLABLE_MIN_ARGS
                                and len(node.args) < _CM_ASSERT_CALLABLE_MIN_ARGS[f.attr])):
                        pass
                    elif observes_only_mock(node.args + [k.value for k in node.keywords], scope):
                        mock += 1
                    elif (
                        f.attr in ("assertTrue", "assertFalse")
                        and node.args
                        and is_constant_tautology(node.args[0])
                    ):
                        pass
                    elif call_cannot_fail(f.attr, node.args, scope):
                        pass
                    else:
                        real += 1
                # banca-v18 (rev C-N): `pytest.raises(E)`/`warns`/
                # `deprecated_call` como statement FORA de `with` cria e
                # descarta o contexto — zero efeito. Só conta como real na
                # posição `context_expr` de ast.With (ramo acima).
                elif f.attr in helpers:
                    real += 1
                elif MOCK_ASSERT.match(f.attr):
                    mock += 1
            elif isinstance(f, ast.Name) and f.id in helpers:
                real += 1
    return real, mock, swallow


def class_constants(tree):
    """Every class body's literals in this file, keyed by class name.

    `self.EXPECTED_CALLS = 1` in a class body is the unittest spelling of the
    hoist a local already loses, and a shared `class Base: EXPECTED_CALLS = 1`
    is the same hoist one scope further out, so a base named in this file is
    merged in first and the class's own bindings win. A base imported from
    ANOTHER file cannot be resolved from one parse tree and is disclosed unseen.
    """
    own = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}

    def consts_for(name, seen=frozenset()):
        node = own.get(name)
        if node is None or name in seen:
            return {}
        merged = {}
        for base in node.bases:
            base_name = getattr(base, "id", None) or getattr(base, "attr", None)
            if base_name:
                merged.update(consts_for(base_name, seen | {name}))
        merged.update(constant_bindings(node.body))
        return merged

    return {name: consts_for(name) for name in own}


# banca-v18 (rev C-N): decorators que impedem o teste de reprovar — a função
# marcada NUNCA valida o arquivo, logo entra como não-teste (fora do scanned).
_SKIP_DECO_NAMES = frozenset({"skip", "xfail", "expectedFailure"})
_SKIPCOND_DECO_NAMES = frozenset({"skipif", "skipIf", "skipUnless"})


def _deco_is_skip(d) -> bool:
    """True quando o decorator é um skip/xfail/expectedFailure — por nome
    (`@skip`, `@expectedFailure`) ou por atributo (`@unittest.skip`,
    `@pytest.mark.skip`, `@pytest.mark.xfail`).

    `skipIf`/`skipif`/`skipUnless` só não-testam quando a condição dobra a
    constante que torna o skip incondicional — `@skipUnless(sys.platform ==
    'win32')` roda em alguma plataforma e segue contando (banca-v18 rev C-N)."""
    f = d.func if isinstance(d, ast.Call) else d
    parts = []
    cur = f
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    if not parts:
        return False
    last = parts[0]  # atributo mais externo / nome puro
    if last in _SKIP_DECO_NAMES:
        return True
    if last in _SKIPCOND_DECO_NAMES:
        if not isinstance(d, ast.Call) or not d.args:
            # `@skipif` sem condição visível → trata como skip incondicional.
            return last != "skipUnless"
        cond = _fold_dead(d.args[0])
        if cond is _UNKNOWN:
            return False  # condicional dinâmico — pode rodar → conta como teste
        return bool(cond) if last != "skipUnless" else not bool(cond)
    return False


def _pytest_mark_name(d) -> str:
    """Último componente de um mark/decorator pytest — pytest.mark.skipif → 'skipif'."""
    f = d.func if isinstance(d, ast.Call) else d
    parts = []
    cur = f
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    return parts[0] if parts else ""


def _module_collection_skip(tree) -> bool:
    """Skip de COLEÇÃO (banca v20 F1): `pytestmark = pytest.mark.skip*` ou
    `pytest.skip(..., allow_module_level=True)` em qualquer ponto do escopo do
    módulo — o pytest nunca executa nenhuma função do arquivo, mas o scanner
    as contava como auditadas (verde sobre zero asserções executadas)."""
    def _walk(stmts):
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield st
            for ch in ast.iter_child_nodes(st):
                if isinstance(ch, ast.stmt):
                    yield from _walk([ch])
    for st in _walk(tree.body):
        if isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name) and t.id == "pytestmark":
                    marks = st.value.elts if isinstance(st.value, (ast.List, ast.Tuple)) else [st.value]
                    for m in marks:
                        name = _pytest_mark_name(m)
                        if name in ("skip", "xfail"):
                            return True
                        if name in ("skipif", "skipIf"):
                            if not isinstance(m, ast.Call) or not m.args:
                                return True
                            c = _fold_dead(m.args[0])
                            if c is not _UNKNOWN and bool(c):
                                return True
        elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call):
            if _pytest_mark_name(st.value) == "skip":
                for kw in st.value.keywords:
                    if kw.arg == "allow_module_level":
                        c = _fold_dead(kw.value)
                        if c is not _UNKNOWN and bool(c):
                            return True
    return False


def _parametrize_empty(fn) -> bool:
    """`@pytest.mark.parametrize(nomes, [])` — coleção gera zero casos:
    pytest reporta skipped e nenhuma asserção executa (banca v20 F1)."""
    for d in fn.decorator_list:
        if _pytest_mark_name(d) == "parametrize" and isinstance(d, ast.Call) \
                and len(d.args) >= 2:
            v = d.args[1]
            if isinstance(v, (ast.List, ast.Tuple)) and not v.elts:
                return True
    return False


def test_functions(node, by_class, class_consts=None):
    """Every (test function, enclosing class constants) reachable at module scope.

    pytest collects a `def` que uma guarda de versão, um `try/except
    ImportError` ou uma classe aninhada apenas envolve — todos são atributos
    de módulo em runtime. Ler só `tree.body` sub-coletaria em silêncio e
    devolveria verde sobre testes nunca lidos, então a recursão só para na
    fronteira do escopo de outra função.

    banca-v18 (rev C-N): `def test_*` dentro de ramo morto de módulo
    (`if TYPE_CHECKING:`, `if False:`, `if __name__ == "__main__":`,
    `while False:`) nunca é coletável — o corpo morto é podado via
    `_live_stmt_children`. E função decorada com skip/xfail/expectedFailure
    nunca reprova → não conta como teste.
    """
    class_consts = class_consts or {}
    for child in _live_stmt_children(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if (TEST_FUNC.match(child.name)
                    and not any(_deco_is_skip(d) for d in child.decorator_list)):
                yield child, class_consts
        elif isinstance(child, ast.ClassDef):
            # classe inteira marcada como skip → nenhum método roda.
            if not any(_deco_is_skip(d) for d in child.decorator_list):
                yield from test_functions(child, by_class, by_class.get(child.name, {}))
        elif not isinstance(child, ast.Lambda):
            yield from test_functions(child, by_class, class_consts)


def file_key(p):
    """The one documented identity key for a scanned file.

    `(st_dev, st_ino)` is the filesystem's own answer, so `./f`, `d//f`, `d/../d/f`,
    an absolute spelling, a symlink, an NFC/NFD spelling of the same macOS name,
    a letter-case variant on a case-insensitive filesystem, and `d f` (the
    directory plus a file inside it) all key to one entry. Keying on the spelling
    instead would double-count and inflate the scanned total.
    """
    st = p.stat()
    return (st.st_dev, st.st_ino)


def halt_on_walk_error(err):
    """A directory the walk could not read is not an empty directory.

    `os.walk` swallows a scandir failure by default: an unreadable subtree is
    dropped silently, nothing is printed, and the scan reports a clean verdict
    over test files it never opened. That is the same under-collection false
    green as a skipped symlinked tree, only quieter — and a mode-000 directory,
    a root-owned mount, or a checkout restored with tight permissions all reach
    it without any hostile intent. Re-raising puts the OSError on `collect`'s
    error path, which `run` turns into exit 2: a non-verdict, never a green.
    """
    raise err


def canonical(path):
    """The one spelling a containment test may compare: fully resolved, every '.',
    '..' and symlink component gone, so a prefix test states a fact about the
    filesystem rather than about the string the caller typed."""
    return os.path.normcase(os.path.realpath(path))


def require_inside(path, roots):
    """PATH AUTHORITY: this scan reads exactly what the caller named.

    Following links let a symlink inside a scanned tree point at a sibling
    checkout, and the scanner then read and PRINTED findings from files nobody
    authorized it to open. Documenting that behaviour is not authority to take
    it. An escape is refused BY NAME and fail-closes, because pruning it in
    silence would be the same false green the followlinks default already
    produced once. `--allow-root` is how a caller widens the scan deliberately —
    a decision they make, rather than one a link makes for them. Not a sandbox: a
    hard link or a bind mount is a real entry inside the tree and no path test
    sees through it.
    """
    real = canonical(path)
    for r in roots:
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            return
    raise OSError(
        f"{path} resolves to {os.path.realpath(path)}, outside every authorized "
        f"root - pass --allow-root to authorize that target, or take the link "
        f"out of the scanned tree")


def collect(paths, allow_roots=()):
    seen, files = set(), []
    roots = [canonical(p) for p in paths]
    for r in allow_roots:
        if not os.path.exists(r):
            raise OSError(f"--allow-root {r}: no such path")
        roots.append(canonical(r))
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            # os.walk(followlinks=True), not Path.rglob: rglob refuses to descend into
            # a symlinked directory while pytest walks straight into it, so a symlinked
            # test tree was collected by the runner and skipped by this scan - a clean
            # report over tests that were never read. Following links reintroduces the
            # risk of a directory cycle, so each directory is visited once by its real
            # path; file-level duplicates still fold through file_key below.
            found, walked = [], set()
            for root, dirs, names in os.walk(p, followlinks=True, onerror=halt_on_walk_error):
                # [HENKE-PATCH] poda de diretórios mortos (node_modules, target, dist...)
                dirs[:] = [d for d in dirs if d not in _pg.PRUNE_DIRS]
                try:
                    real = os.path.realpath(root)
                except OSError:
                    real = root
                if real in walked:
                    dirs[:] = []
                    continue
                walked.add(real)
                for d in list(dirs):
                    require_inside(os.path.join(root, d), roots)
                for n in names:
                    # [HENKE-PATCH] *.rs dentro de tests/ também é test crate
                    if TEST_FILE.match(n) or (n.endswith(".rs") and Path(root).name == "tests"):
                        require_inside(os.path.join(root, n), roots)
                        found.append(Path(root) / n)
            found = sorted(found)
        elif p.is_file():
            found = [p]
        else:
            raise OSError(f"not a readable file or directory: {raw}")
        for f in found:
            key = file_key(f)
            if key in seen:
                continue
            seen.add(key)
            files.append(f)
    return files


def _silence_stdout():
    """Stop the interpreter re-reporting a dead stdout at shutdown."""
    try:
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
    except OSError:
        pass


def emit(lines):
    """Write the verdict's evidence, or raise so the caller can exit 2.

    A verdict whose evidence never reached the artifact is not a verdict. With
    fd 1 closed (`1>&-`) CPython sets `sys.stdout` to None and `print` silently
    does nothing, which would hand a CI consumer a bare exit 0 with an empty
    captured stdout — the exact fake green this island exists to catch.
    """
    out = sys.stdout
    if out is None:
        raise OSError("stdout is closed — a verdict cannot be reported without its evidence")
    for line in lines:
        print(line, file=out)
    out.flush()


def excusal_line(names):
    """The written excusal every summary and every `error:` line carries."""
    return f"helpers declared: {', '.join(sorted(names)) or 'none'}"


def declared_from_argv(argv):
    """Read `--assert-helper` straight from argv, so a failure that happens before
    (or instead of) a successful parse still carries the excusal record."""
    names, tokens = [], iter(argv)
    for tok in tokens:
        if tok == "--assert-helper":
            nxt = next(tokens, None)
            if nxt is not None:
                names.append(nxt)
        elif tok.startswith("--assert-helper="):
            names.append(tok.split("=", 1)[1])
    return {n for n in names if n}


class RecordingParser(argparse.ArgumentParser):
    """argparse's own usage errors must carry the excusal record too."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"error: {message} ({_RECORD})", file=sys.stderr)
        raise SystemExit(2)


def scan_test_file(path: Path, helpers):
    """Escaneia UM arquivo de teste. Retorna (scanned_count, findings) ou None.

    `None` sinaliza erro de leitura/parse — jamais findings vazios por erro.
    Função pura extraída de `run()` para o adapter incremental da Wave 7:
    mesmas regras, mesmo formato de findings `(veredito, path, _Fn, why)`.
    """
    f = Path(path)
    if f.suffix.lower() != ".py":
        return scan_polyglot(f, helpers)
    try:
        tree = ast.parse(f.read_text(encoding="utf-8-sig"), filename=str(f))
    except (OSError, SyntaxError, ValueError, RecursionError) as e:
        print(f"error: cannot parse {f}: {e} ({_RECORD})", file=sys.stderr)
        return None
    b = bindings(tree)
    module_consts = constant_bindings(tree.body)
    scanned, findings = 0, []
    mod_skip = _module_collection_skip(tree)
    for fn, class_consts in test_functions(tree, class_constants(tree)):
        scanned += 1
        if mod_skip or _parametrize_empty(fn):
            findings.append(("COLLECTION-SKIP", f, fn,
                             "pytest never executes it — collection-level skip"))
            continue
        real, mock, swallow = classify(fn, helpers, b, module_consts, class_consts)
        if real == 0 and mock == 0:
            findings.append(("NO-ASSERTION", f, fn, "executes code, asserts nothing"))
        elif real == 0:
            findings.append(("MOCK-ONLY", f, fn, f"{mock} assertion(s) on the double, none on the code under test"))
        if swallow:
            findings.append(("SWALLOWED", f, fn, swallow))
    return scanned, findings


def run(argv):
    global _RECORD
    ap = RecordingParser(description="Flag tests that execute code without asserting on it.")
    ap.add_argument("paths", nargs="+", help="test files, or directories scanned for test_*.py / *_test.py")
    ap.add_argument("--allow-root", action="append", default=[], metavar="DIR",
                    help="authorize an additional root the scan may read (repeatable)")
    ap.add_argument("--assert-helper", action="append", default=[], metavar="NAME",
                    help="treat calls to NAME as a real assertion (repeatable; each use is an excusal)")
    args = ap.parse_args(argv)
    helpers = set(args.assert_helper)
    excusal = _RECORD = excusal_line(helpers)
    bad = sorted(h for h in helpers if not IDENT.match(h))
    if bad:
        print(f"error: --assert-helper needs a Python identifier, got: {', '.join(bad)} ({excusal})",
              file=sys.stderr)
        return 2

    try:
        files = collect(args.paths, args.allow_root)
    except OSError as e:
        print(f"error: {e} ({excusal})", file=sys.stderr)
        return 2

    scanned, findings = 0, []
    for f in files:
        out = scan_test_file(f, helpers)
        if out is None:
            return 2
        n, file_findings = out
        scanned += n
        findings.extend(file_findings)

    if not scanned:
        print(f"error: no test functions found — nothing to audit ({excusal})", file=sys.stderr)
        return 3

    lines = [f"{v:<13} {f}:{fn.lineno}  {fn.name}  {why}" for v, f, fn, why in findings]
    lines.append(f"{scanned} test function(s) scanned, {len(findings)} finding(s) — {excusal}")
    try:
        emit(lines)
    except (OSError, ValueError) as e:
        _silence_stdout()
        print(f"error: cannot write the verdict: {e} ({excusal})", file=sys.stderr)
        return 2
    return 1 if findings else 0


def main(argv=None):
    """Every error path returns 2 — no crash, and no `--help`, may borrow a
    verdict's code. The `__main__` seal below carries that guarantee through
    interpreter shutdown, where a failed stream flush would otherwise substitute
    CPython's 120 for whatever this function returned."""
    global _RECORD
    argv = list(sys.argv[1:] if argv is None else argv)
    _RECORD = excusal_line(declared_from_argv(argv))
    try:
        return run(argv)
    except SystemExit:      # argparse's own exit: usage or --help, never a verdict
        return 2
    except Exception as e:  # RecursionError, MemoryError, and any unforeseen bug
        try:
            print(f"error: {type(e).__name__}: {e} ({_RECORD})", file=sys.stderr)
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    # ISO-BOOT canônico (henke/_isoboot.py) — sitecustomize/usercustomize/
    # .pth hostis executam no startup e patcham verificadores no pai; o
    # respawn -Es nasce sem esses canais (prova ESTRUTURAL: sys.flags
    # autenticado + os.sys — nunca variável de ambiente). Texto sincronizado
    # por tests/test_isoboot_guard.py — editar só alterando o canônico.
    import importlib.util as _ilu
    import os.path as _osp
    _ib_p = _osp.join(_osp.dirname(_osp.abspath(__file__)), "_isoboot.py")
    if not _osp.isfile(_ib_p):
        _ib_p = _osp.join(_osp.dirname(_osp.dirname(_osp.abspath(__file__))),
                          "_isoboot.py")
    _ib_spec = _ilu.spec_from_file_location("henke_isoboot", _ib_p)
    _ib_mod = _ilu.module_from_spec(_ib_spec)
    _ib_spec.loader.exec_module(_ib_mod)
    _ib_mod.ensure_isolated(__file__, sys.argv[1:])
    # The seal: 0/1/2/3 are the only codes that may leave this process.
    # CPython flushes the std streams at shutdown and REPLACES the exit status
    # with 120 if that flush raises — a dead stdout pipe under `--help`, a dead
    # stderr under argparse's usage error. Flush both here instead, downgrade a
    # verdict whose evidence never landed to 2, and redirect the dead fd to
    # /dev/null so the shutdown flush has nothing left to fail on.
    try:
        _code = main()
    except SystemExit as _e:        # argparse's usage exit and `--help` arrive here
        _code = _e.code if isinstance(_e.code, int) else (0 if _e.code is None else 1)
    except BaseException as _e:     # no unexpected error may wear a verdict's code
        try:
            print(f"error: internal error - {type(_e).__name__}: {_e} ({_RECORD})",
                  file=sys.stderr)
        except BaseException:
            pass
        _code = 2
    for _stream, _fd in ((sys.stdout, 1), (sys.stderr, 2)):
        try:
            if _stream is not None:
                _stream.flush()
        except BaseException:
            if _code in (0, 1):     # output that never landed is not a verdict
                _code = 2
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), _fd)
            except BaseException:
                pass
    sys.exit(_code)
