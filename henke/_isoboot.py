#!/usr/bin/env python3
"""_isoboot.py — bootstrap de isolamento compartilhado por TODOS os entrypoints.

Por que existe: `sitecustomize.py`/`usercustomize.py`/`.pth` hostis via
PYTHONPATH (ou user site) executam na INICIALIZAÇÃO do interpretador — antes
de qualquer mitigação em-processo — e podem patchar scanners, verificadores
ou o próprio sinal de "estou isolado" (R2-PT-06 / R3-PT-06 / auditoria P0-01
residual: `sys.flags` é atributo plain de módulo — pode ser sombreado).

Arquitetura:
  - Todo entrypoint chama `ensure_isolated(__file__, sys.argv[1:])` dentro do
    bloco `if __name__ == "__main__":`, carregando este módulo POR PATH
    (spec_from_file_location — um meta_path finder plantado não consegue
    servir um _isoboot falso). O texto do bloco é canônico: CANONICAL_GUARD
    abaixo e tests/test_isoboot_guard.py exigem sincronia em todos os
    entrypoints.
  - Se o runtime não prova isolamento, respawna `[exe, "-Es", script, argv]`
    e sai com o código do filho. `-Es` = -E (ignora PYTHON*) + -s (pula user
    site: usercustomize/.pth de usuário). Filhos lançados com -I/-Es nascem
    limpos e passam direto.
  - `-Es` (e não `-I`) preserva o diretório do script em sys.path — imports
    irmãos continuam funcionando sem depender de preâmbulos — e NUNCA
    introduz CWD (invocação por path de arquivo não coloca CWD em sys.path).

Prova estrutural de isolamento (NUNCA variável de ambiente):
  1. `sys.flags` autenticado: o tipo real é structseq C estaticamente alocado
     — `__flags__` SEM Py_TPFLAGS_HEAPTYPE. Uma classe Python é SEMPRE
     heaptype e `type(sys.flags)` não é instanciável nem subclassável, então
     namespace/tuple-subclass plantado não forja o tipo.
  2. Consistência `sys.flags is os.sys.flags`: `os.sys` é vinculado quando o
     módulo `os` importa (antes do sitecustomize) — se `sys` foi trocado em
     sys.modules, os dois objetos divergem → fail-closed.
  3. Flags exigidos: `ignore_environment=1` E `no_user_site=1` — exatamente o
     par -Es/-I que fecha PYTHONPATH/PYTHONHOME e user site.
  4. `sys.orig_argv` (3.10+, quando presente): o flag -I ou -E+-s deve constar
     no argv real do interpretador — corrobora, nunca decide sozinho.

Fronteira declarada (documentada p/ auditoria): um startup que substitua
BUILTINS (type/isinstance) e mantenha o interpretador funcional, ou que
sirva módulos-fantasma via sys.modules/meta_path em nível total, está fora
do alcance de qualquer guarda em-processo — por isso os lançadores
(bin/henke, filhos de gate) já nascem com -I/-Es: a trava real é o startup
limpo, este guarda é a rede de segurança e o detector de invocação insegura.
"""
from __future__ import annotations

import os
import subprocess
import sys

# -E ignora PYTHON*/env do interpretador; -s pula user site (usercustomize e
# .pth de usuário). -I ficaria ainda mais estrito (-P remove o dir do script)
# mas quebraria imports irmãos em scripts sem preâmbulo de sys.path.
BOOT_FLAG = "-Es"

# Bloco canônico embutido nos `if __name__ == "__main__":` de todos os
# entrypoints. tests/test_isoboot_guard.py compara este texto com o trecho
# real de cada arquivo — um guarda divergente/ausente falha a suíte.
CANONICAL_GUARD = '''    # ISO-BOOT canônico (henke/_isoboot.py) — sitecustomize/usercustomize/
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
'''


def _real_sys():
    """`sys` verdadeiro via `os.sys` (o módulo os foi importado antes do
    sitecustomize rodar e mantém referência ao sys real). Cai para `sys`
    quando a referência não existe."""
    return getattr(os, "sys", None) or sys


def _authentic_flags(flags) -> bool:
    """O objeto é o structseq C `sys.flags` genuíno?

    O tipo real é alocado estaticamente: Py_TPFLAGS_HEAPTYPE ausente, module
    'sys', name 'flags'. Classes Python são SEMPRE heaptype — um
    SimpleNamespace/tuple-subclass/classe 'flags' com __module__='sys' não
    consegue parecer o structseq original (que também não instancia nem
    aceita subclasse).
    """
    t = type(flags)
    return (
        isinstance(flags, tuple)
        and not (getattr(t, "__flags__", 0x200) & 0x200)  # Py_TPFLAGS_HEAPTYPE
        and getattr(t, "__module__", "") == "sys"
        and getattr(t, "__name__", "") == "flags"
    )


def _orig_argv_proves_isolation(rs) -> bool:
    """Sinal corroborante: se orig_argv existe (3.10+), exige flag -I ou o
    par -E + -s no argv real do interpretador. Ausente/indisponível não
    condena (interpretador embarcado)."""
    orig = getattr(rs, "orig_argv", None)
    if not isinstance(orig, (list, tuple)) or not orig:
        return True  # sem dados — decide o resto
    merged = ""
    for a in orig[1:]:
        if not isinstance(a, str) or not a.startswith("-") or a.startswith("--"):
            continue
        merged += a[1:]
    return "I" in merged or ("E" in merged and "s" in merged)


def isolated_runtime() -> bool:
    """True sse o interpretador nasceu sem canais de auto-exec hostis.

    Exige flags autenticados (structseq C), identidade sys≡os.sys,
    ignore_environment & no_user_site (o par -Es/-I) e — quando disponível —
    o flag correspondente em sys.orig_argv.
    """
    rs = _real_sys()
    f_local = getattr(sys, "flags", None)
    f_real = getattr(rs, "flags", None)
    if f_local is not f_real:
        # sys foi sombreado/trocado em sys.modules — divergência = hostil.
        return False
    if not _authentic_flags(f_real):
        return False
    if not (getattr(f_real, "ignore_environment", 0)
            and getattr(f_real, "no_user_site", 0)):
        return False
    return _orig_argv_proves_isolation(rs)


def _reveal_usersite() -> None:
    """Re-expõe o user site SÓ para resolução de import (append em sys.path).

    `pip install --user` fica invisível sob -I/-Es e quebraria deps do tipo
    PyYAML --user (P1-05). O append NÃO executa .pth nem usercustomize —
    eles só rodam via site.addsitedir() no startup, que -s já pulou. O dir
    entra no FIM do path: stdlib/site-packages do prefixo continuam
    ganhando qualquer sombreamento.
    """
    try:
        import site as _site
        usp = _site.getusersitepackages()
    except Exception:
        return
    for d in ([usp] if isinstance(usp, str) else list(usp or [])):
        try:
            if d and os.path.isdir(d) and d not in sys.path:
                sys.path.append(d)
        except OSError:
            continue


def _msvcrt_quote(arg: str) -> str:
    """Cita um argumento no formato MSVCRT/CommandLineToArgvW — spawnv no
    Windows concatena os args com espaço SEM citar; arg com espaço/tab/
    aspas precisa vir pré-citado. Regra: `\\` literal; `\\\\` antes de `"`;
    `\\\"` para aspas literal; string inteira entre aspas quando contém
    espaço/tab/aspas."""
    s = str(arg)
    if s and not any(c in s for c in ' \t"'):
        return s
    out = '"'
    bs = 0
    for c in s:
        if c == "\\":
            bs += 1
            continue
        if c == '"':
            out += "\\" * (2 * bs + 1) + '"'
            bs = 0
            continue
        out += "\\" * bs + c
        bs = 0
    out += "\\" * (2 * bs) + '"'
    return out


def ensure_isolated(script_file: str, argv: list) -> None:
    """Garante runtime isolado para o entrypoint `script_file`.

    Se o runtime atual não prova isolamento, respawna
    `[sys.executable, "-Es", script, *argv]` e encerra com o código do
    filho. No runtime já isolado é no-op (além de reexpor o user site p/
    imports --user).

    Cadeia de respawn (anti-verde-fabricado):
      - POSIX: `os.execv` — exec NÃO RETORNA em sucesso; um subprocess.run
        plantado em sys.modules por sitecustomize/meta_path não fabrica
        returncode=0. execv retornando sem exceção = substituída → exit 2.
      - Windows: P_OVERLAY do execv PERDE o exit code do filho (o pai sai 0
        sempre — falso-verde), então `os.spawnv(P_WAIT)` é o caminho real —
        chamada C única; sys.modules['subprocess'] plantado não a alcança.
      - Fallback final: subprocess.run (fronteira declarada do módulo)."""
    if isolated_runtime():
        _reveal_usersite()
        return
    rs = _real_sys()
    exe = getattr(rs, "executable", None) or sys.executable
    script = os.path.abspath(script_file)
    # Propaga flags de isolamento AINDA mais fortes do invocador original:
    # `python -S script.py` (sem site-packages — ex.: teste de dep ausente)
    # ou `-P` (dir do script fora do path) seriam silenciosamente
    # ENFRAQUECIDOS para -Es no respawn — degradava o pedido explícito do
    # chamador e quebrava quem precisa do -S. Lê orig_argv real (nunca
    # env var) e funde os chars extras no BOOT_FLAG.
    extra = ""
    orig = getattr(rs, "orig_argv", None)
    merged = ""
    if isinstance(orig, (list, tuple)):
        for a in orig[1:]:
            if not isinstance(a, str) or not a.startswith("-") \
                    or a.startswith("--"):
                continue
            merged += a[1:]
    for ch in ("S", "P"):
        if ch in merged and ch not in BOOT_FLAG:
            extra += ch
    child_argv = [str(exe), BOOT_FLAG + extra, script, *list(argv)]
    if os.name == "posix" and hasattr(os, "execv"):
        try:
            os.execv(str(exe), child_argv)  # hazard-ok: SHELL-INJECTION — respawn isolado, argv literal (exe+script), sem shell
        except OSError:
            pass
        else:
            # execv "retornou" sem erro = função substituída — recusa.
            raise SystemExit(2)
    spawnv = getattr(os, "spawnv", None)
    p_wait = getattr(os, "P_WAIT", None)
    if spawnv is not None and p_wait is not None:
        try:
            # MSVCRT spawnv NÃO cita args com espaço — "--prompt 'audita
            # a segurança'" chegava quebrado ao filho (regressão canário
            # M43+). Citação manual regra CommandLineToArgvW (_msvcrt_quote).
            code = spawnv(p_wait, str(exe),  # hazard-ok: SHELL-INJECTION — respawn isolado no Windows; argv literal citado, sem shell
                          [_msvcrt_quote(a) for a in child_argv])
            if isinstance(code, int):
                raise SystemExit(code)
        except OSError:
            pass
    try:
        res = subprocess.run(child_argv)
        code = res.returncode if isinstance(res.returncode, int) else 2
    except OSError:
        code = 2
    raise SystemExit(code)


def python_argv(script, *args):
    """argv para filhos já-nascidos-isolados: `[exe, "-Es", script, *args]`.

    Usar em todos os spawn sites de scripts da suíte — o filho passa o
    guarda de primeira (sem custo de respawn) e os canais de auto-exec
    nunca abrem no processo do portão."""
    rs = _real_sys()
    exe = getattr(rs, "executable", None) or sys.executable
    return [exe, BOOT_FLAG, str(script), *args]
