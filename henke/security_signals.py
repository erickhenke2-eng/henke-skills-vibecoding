#!/usr/bin/env python3
"""security_signals.py — FONTE ÚNICA de vocabulário de segurança (P1-04).

Vocabulário destrutivo/PUBLISH/metaconfig + normalização Unicode + matchers,
usados por `henke_router` (matriz de modo/auth) e `henke_auto` (classificação
de escopo). ANTES eram duas cópias divergentes: 343 sinais destrutivos no
router vs 277 no auto, com 15 sinais exclusivos do auto ("eliminate",
"get rid of", "joga fora"...) e ~80 exclusivos do router — cada correção
exigia espelhamento manual e a próxima divergência era questão de tempo.

Agora a lista é a UNIÃO das duas fontes — direção estritamente mais forte:
nada deixou de ser detectado; o router ganhou os sinais coloquiais do auto
e o auto ganhou as paráfrases/comandos do router. Todo sinal tem cobertura
obrigatória (tests/test_security_signals.py exige identidade router≡auto e
casamento canônico por _has_signal em cada entrada).

Sinal novo entra AQUI — nunca direto num consumidor.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata

# Homoglifos cross-script que sobrevivem ao NFKD como Ll — "dеlеta" com
# е cirílico (U+0435) não casa \bdeleta\b (banca v9 — A2). Dobramos os
# confusáveis comuns para Latin ANTES do NFKD; a direção é fail-safe
# (só aumenta matching, nunca reduz).
_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "ё": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "у": "y", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ԛ": "q",
    "һ": "h", "к": "k", "м": "m", "н": "n", "т": "t", "в": "b", "г": "r",
    "п": "n", "э": "e", "з": "3", "ҝ": "k", "օ": "o", "ո": "n", "ս": "u",
    "ɡ": "g", "ɩ": "i", "ο": "o", "α": "a", "β": "b", "γ": "y", "δ": "d",
    "ε": "e", "ζ": "z", "η": "n", "ι": "i", "κ": "k", "μ": "u", "ν": "v",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x", "ω": "w", "ϲ": "c", "ϳ": "j",
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o",
    "Р": "p", "С": "c", "Т": "t", "Х": "x", "І": "i",
    # Latim estendido/armênio/IPA que a tabela não cobria (banca v10 — A5):
    "ɑ": "a", "ɛ": "e", "ı": "i", "ե": "e", "ռ": "r", "ʀ": "r",
    "ɢ": "g", "ʟ": "l", "ɴ": "n", "ɵ": "o", "ɸ": "o", "χ": "x",
    # Leetspeak/dígitos-homoglifos (banca v11 — A3): 'd3l3t3', 'w1p3'
    # dobram para a letra óbvia antes da comparação de termos.
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
    "@": "a", "$": "s", "!": "i",
    # A2 v14 + ALTO-1 v15: ϵ/є, leet 2/6/8/9, small-caps. '|'→espaço
    # (não letra — fundia sinal ao vizinho).
    "ϵ": "e", "є": "e", "ε": "e", "2": "z", "6": "b", "8": "b", "9": "g",
    "|": " ", "Ꭰ": "a",
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f",
    "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l",
    "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ʀ": "r",
    "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y",
    "ᴢ": "z",
    # banca v16 — A6: Cherokee/Lisu que sobrevivem ao NFKD.
    "Ꮯ": "c", "Ꮇ": "m", "Ꮐ": "g", "Ꭲ": "i", "Ꮤ": "t", "Ꮪ": "d",
    "Ꮠ": "s", "Ꭴ": "u", "Ᏻ": "y", "Ꮎ": "n", "Ꮢ": "r", "Ꮓ": "z",
    "ꓯ": "l", "ꓪ": "o", "ꓫ": "o",
})


# ALTO-3 (banca v13): variantes Unicode de pontuação.
_PUNCT_FOLDS = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "﹘": "-", "﹣": "-",
    # banca v16 — A6: hífen bala/maqaf/modifier/heavy-minus sobreviviam.
    "⁃": "-", "־": "-", "˗": "-", "➖": "-",
    "∕": "/", "⁄": "/",
    "꞉": ":", "˸": ":", "։": ":", "∶": ":",
    "․": ".", "‥": "..",
    "＼": "\\",
})


def _norm(text: str) -> str:
    # Whitespace exótico (NBSP U+00A0, U+2000–U+200A, U+202F, U+3000, tab,
    # espaço duplo) quebra sinais multi-palavra ("git push --force" evade
    # "git push" — banca v9 A3): todo Zs vira espaço e \s+ colapsa para um.
    folded = text.translate(_HOMOGLYPHS).translate(_PUNCT_FOLDS)
    nfkd = unicodedata.normalize("NFKD", folded.lower())
    # Além de marcas combinantes: caracteres de FORMATO/controle invisíveis
    # (Cf/Cc/Cs/Co/Cn — ZWSP U+200B, ZWJ, WJ, BOM, soft-hyphen) quebram o \b
    # do _has_signal sem mudar a aparência para o humano — "dele​ta" evade
    # DESTRUCTIVE_VERBS (banca v7 — F-04).
    stripped = "".join(
        " " if c.isspace() or unicodedata.category(c) == "Zs" else c
        for c in nfkd
        if c.isspace()
        or (not unicodedata.combining(c)
            and unicodedata.category(c) not in ("Cf", "Cc", "Cs", "Co", "Cn",
                                               "Mn", "Mc", "Me"))
    )
    return re.sub(r"\s+", " ", stripped)


def _has_signal(text: str, sig: str) -> bool:
    """Verifica sinal com word-boundary para palavras simples, evitando colisões (check vs checklist)."""
    # O sinal também passa por _norm: sem isso entradas legíveis-para-humano
    # ("força", "destrói", "branch -D", "sdelete64" — dígitos dobram por leet)
    # nunca casavam nem consigo mesmas → sinal morto/evasão gratuita (P1-04,
    # corpus test_security_signals).
    sig_clean = _norm(sig.strip())
    if not sig_clean:
        return False
    if re.fullmatch(r"[\w-]+", sig_clean):
        if re.search(r"\b" + re.escape(sig_clean) + r"\b", text):
            return True
    else:
        prefix = r"\b" if sig_clean[0].isalnum() else ""
        suffix = r"\b" if sig_clean[-1].isalnum() else ""
        if re.search(prefix + re.escape(sig_clean) + suffix, text):
            return True
    # F-1 (banca v12): fragmentação intra-palavra ("de-lete", "de\nlete",
    # "dropTable") evade \b; sinal compactado casa por igualdade exata
    # contra tokens e pares adjacentes compactados.
    # A1 (banca v14): alfabeto por-sinal mantém a pontuação do sinal no
    # compacto ("checkout--" só casa com '-' no texto).
    _allowed = set("abcdefghijklmnopqrstuvwxyz0123456789") | {
        ch for ch in sig_clean if not ch.isalnum() and ch != " "}
    sig_compact = "".join(ch for ch in sig_clean if ch in _allowed)
    # ALTO-2/MÉDIO-5 (banca v13) + A3 (v14): >=3 fragmentos, tokens vazios,
    # [_./\\] em identificadores; range em vez de fatia (era O(n²)).
    if len(sig_compact) >= 2:
        toks: list[str] = []
        for _t in re.split(r"\s+", text):
            for _seg in re.split(r"[_./\\]+", _t):
                _seg = "".join(ch for ch in _seg if ch in _allowed)
                if _seg:
                    toks.append(_seg)
        for i in range(len(toks)):
            acc = toks[i]
            if acc == sig_compact:
                return True
            for j in range(i + 1, len(toks)):
                if len(acc) >= len(sig_compact):
                    break
                acc += toks[j]
                if acc == sig_compact:
                    return True
    return False


# Flexão morfológica PT para matching de gatilhos (sem libs externas).
# Artigos viram uma única classe (cobre um/uma/o/a) e substantivos/adjetivos
# ganham variantes de gênero e número por sufixo.
_FLEX_ARTICLES = frozenset({
    "o", "a", "os", "as", "um", "uma", "uns", "umas",
    "do", "da", "dos", "das", "no", "na", "nos", "nas", "ao", "aos",
})
_FLEX_ARTICLE_RE = r"(?:os?|as?|um|uma|uns|umas|d[oa]s?|n[oa]s?|a[oa]s?)"

# Finais que indicam adjetivo em PT — únicos onde o<->a é seguro flexionar.
_FLEX_GENDER_SUF = (
    "ivo", "iva", "oso", "osa", "ado", "ada", "ido", "ida", "ito", "ita",
    "eiro", "eira", "orio", "oria", "ario", "aria", "ento", "enta",
    "ovo", "ova", "ilo", "ila",
)


def _flex_word(tok: str) -> str:
    if len(tok) < 3:
        # "x"/"y" soltos são placeholders de gatilho ("integra com X",
        # "depois de X dias") — casam qualquer valor nomeado.
        if tok in ("x", "y"):
            return r"[\w-]+"
        return re.escape(tok)
    if tok in _FLEX_ARTICLES:
        return _FLEX_ARTICLE_RE
    if tok.endswith(("oes", "aes")):
        return re.escape(tok[:-3]) + r"(?:ao|oes|aes)"
    if tok.endswith("ao"):
        return re.escape(tok[:-2]) + r"(?:ao|oes|aes)"
    if len(tok) >= 5 and tok.endswith("eis"):
        return re.escape(tok[:-3]) + r"(?:el|eis)"
    if len(tok) >= 5 and tok.endswith("ais"):
        return re.escape(tok[:-3]) + r"(?:al|ais)"
    # Troca de gênero o<->a só em final adjetival ('responsivo'->responsiv[oa]);
    # nunca em substantivo ('bola' não pode casar 'bolo' — canário M45).
    for suf in _FLEX_GENDER_SUF:
        if tok.endswith(suf):
            return re.escape(tok[: -len(suf)] + suf[:-1]) + r"[oa]s?"
        if len(tok) > len(suf) + 1 and tok.endswith(suf + "s"):
            return re.escape(tok[: -len(suf) - 1] + suf[:-1]) + r"[oa]s?"
    if tok.endswith("es"):
        # Cobre plural -e→-es ("chave"→"chaves") E radical nu ("país"→"países"):
        # `(?:es?)?` é superconjunto do antigo `es?` — nunca perde casamento.
        return re.escape(tok[:-2]) + r"(?:es?)?"
    if tok.endswith("m"):
        return re.escape(tok[:-1]) + r"(?:m|ns)"
    if tok.endswith("l"):
        return re.escape(tok[:-1]) + r"(?:l|is)"
    # -r/-z + plural: `(?:es)?` torna o "es" todo opcional — o radical nu
    # ("fazer", "subir", "luz") precisa casar a si mesmo (o antigo `es?`
    # exigia a letra "e" e nunca casava a forma base).
    if tok.endswith(("r", "z")):
        return re.escape(tok) + r"(?:es)?"
    if tok.endswith("s"):
        return re.escape(tok[:-1]) + r"s?"
    if tok.endswith("x"):
        return re.escape(tok)
    return re.escape(tok) + r"s?"


@lru_cache(maxsize=1024)
def _flex_pattern(sig: str) -> str:
    parts = []
    for chunk in re.findall(r"[a-z0-9_]+|[^a-z0-9_]+", sig):
        if re.fullmatch(r"[a-z0-9_]+", chunk):
            parts.append(_flex_word(chunk))
        elif chunk.isspace():
            parts.append(r"\s+")
        else:
            parts.append(re.escape(chunk))
    pat = "".join(parts)
    if not pat:
        return ""
    prefix = r"\b" if sig[0].isalnum() else ""
    suffix = r"\b" if sig[-1].isalnum() else ""
    return prefix + pat + suffix


def _has_flex_signal(text: str, sig: str) -> bool:
    """_has_signal com variantes flexivas PT — usado só no matching de gatilhos."""
    sig_n = _norm(sig.strip())
    if not sig_n:
        return False
    pat = _flex_pattern(sig_n)
    return bool(pat) and bool(re.search(pat, text))


# Vocabulário destrutivo e PUBLISH (PROTOCOLO §1) — exige autorização humana
# explícita. UNIÃO de henke_router.DESTRUCTIVE_SIGNALS + henke_auto
# .DESTRUCTIVE_VERBS (P1-04 — divergência impossível agora: fonte única).
DESTRUCTIVE_SIGNALS = (
    "deleta", "deletar", "deleto", "deletamos", "delete", "exclui", "excluir", "excluo",
    "excluimos", "apago", "apaga", "apagar", "apague", "apagamos", "remove", "remover", "removo",
    "remova", "removendo", "erase", "erases", "drop", "drop table", "truncate", "wipe",
    "wipe database", "destroi", "destruo", "destruir", "destruimos", "destroy", "purge",
    "force-push", "push --force", "force push", "push -f", "push -fu", "reset --hard",
    "hard reset", "rm -rf", "rm -r", "git clean", "clean -fd", "clean -fdx", "apaga o banco",
    "apagar o banco", "derruba", "zera", "zerar", "deletando", "deletem", "deletou", "apagando",
    "apaguem", "apagou", "excluindo", "excluam", "excluiu", "removam", "removeu", "destruindo",
    "destruam", "truncando", "limpando", "limpem", "zerando", "zerem", "eliminando", "eliminem",
    "resetando", "resetem", "desinstalando", "derrubando", "derrubem", "deleting", "deletes",
    "removing", "removes", "wiping", "wipes", "dropping", "drops", "truncating", "truncates",
    "erasing", "purging", "purges", "destroying", "destroys", "resetting", "del /", "del/",
    "del *", "del.", "rmdir", "rd /", "rd/", "erase /", "format c:", "format d:",
    "formata o disco", "format disk", "mkfs", "dd if", "shred", "sdelete", "cipher /w",
    "remove-item", "clear-item", "format-volume", "dism /", "esvazia", "esvaziar", "esvazie",
    "some com", "suma com", "varre", "varrer", "limpa o banco", "limpa tudo", "tira tudo",
    "tirar tudo", "esvazia o banco", "limpa o repo", "apaga tudo", "deleted", "removed",
    "dropped", "wiped", "destroyed", "purged", "erased", "truncated", "uninstall", "uninstalled",
    "desinstalou", "rm", "rd", "del", "dd of=", "dropdb", "dropuser", "drop database",
    "checkout --", "checkout -f", "git restore", "restore --", "branch -d", "branch -df",
    "format e:", "format f:", "format a:", "format b:", "format g:", "sdelete64", "nuke",
    "nuked", "nuking", "clean", "cleaned", "cleaning", "revert", "reverted", "reverting",
    "prune", "pruned", "pruning", "undo", "undone", "reverta", "revertemos", "revertido",
    "revertida", "reverter", "git reset", "reset the", "reseta", "resetar", "resete",
    "resetemos", "resetou", "checkout .", "docker system prune", "docker volume prune",
    "kubectl delete", "terraform destroy", "helm uninstall", "systemctl stop",
    "systemctl disable", "roll back", "rolled back", "obliterate", "apaguei", "removi",
    "destrui", "zerei", "limpei", "eliminei", "formatei", "desfaz", "desfazer", "descarta",
    "sobrescreve", "desliga", "borra", "borrar", "suprime", "suprimir", "quita", "quitar",
    "supprime", "efface", "losche", "entferne", "cancella", "distruggi", "trash", "scrub",
    "flush", "unlink", "discard", "dispose", "deltree", "droptable", "forcepush", "shutdown",
    "format h:", "format i:", "format j:", "format k:", "format l:", "format m:", "format n:",
    "format o:", "format p:", "format q:", "format r:", "format s:", "format t:", "format u:",
    "format v:", "format w:", "format x:", "format y:", "format z:", "detona", "detonar",
    "detonei", "detonando", "estoura", "arregaça", "deleter", "format a drive",
    "format the drive", "format the disk", "format drive", "format usb", "format pendrive",
    "formata o hd", "formatar o disco", "formatar o pendrive", "formata o pendrive",
    "formata o cartao", "formata o sd", "formatar o hd", "formata a particao",
    "formatar o cartao", "formatar o sd", "formata o ssd", "format /", "diskpart", "fdisk",
    "newfs", "mke2fs", "blkdiscard", "wipefs", "srm", "secure erase", "hdparm --security-erase",
    "dd bs", "dd count", "dd conv", "switch -f", "switch --force", "stash clear",
    "reflog expire", "update-ref -d", "filter-branch", "filter-repo", "clear-content",
    "clear-disk", "remove-partition", "remove-volume", "remove-physicaldisk", "robocopy /mir",
    "robocopy /purge", "torch the", "gut the", "axe the", "scrap the", "annihilate", "decimate",
    "vaporize", "torch o", "destrói", "reset no", "reset na", "reset para", "reset pro",
    "reset pra", "reset em", "reset to", "reset back", "reset --soft", "reset --mixed",
    "com force", "force para", "force pro", "force pra", "com força", "força para", "força pro",
    "empurra com força", "push force", "checkout head", "checkout ~", "checkout main --",
    "switch -d", "restore --source", "restore .", "restore --staged", "rebase", "rebased",
    "rebasing", "rebase --onto", "cherry-pick --no-commit", "merge --abort",
    "revert --no-commit", "amend --no-edit", "stash drop", "stash pop", "clean -f", "branch -D",
    # --- sinais que só existiam no henke_auto (união P1-04) ---
    "exclua", "destrua", "restaura", "reset", "limpa", "limpar", "desinstala", "elimina",
    "eliminar", "eliminate", "joga fora", "jogar fora", "tira do ar", "tirar do ar",
    "get rid of", "get rid",
    # --- imperativos/subjuntivos PT (banca: "publique/suba/desinstale/
    # sobrescreva" passavam sem requires_human_auth — a lista cobria só
    # indicativo/infinitivo). Formas -e (verbs -ar) e -a (verbs -er/-ir).
    "destrua", "derrube", "derrubar", "zere", "elimine", "desinstale",
    "desinstalar", "limpe", "descarte", "descartar", "sobrescreva",
    "sobrescrever", "desligue", "desligar", "borre", "suprima", "quite",
    "restaure", "restaurar", "desfaça", "desfazer", "anule", "anular",
    "trunca", "truncar", "trunque",
)
_DESTRUCTIVE_SIGNALS = DESTRUCTIVE_SIGNALS

PUBLISH_ONLY_SIGNALS = (
    "deploy", "publica", "publicar", "producao", "production", "sobe", "subir", "pro ar",
    "no ar", "em producao", "publish", "npm publish", "pypi", "release", "lanca release",
    "git push", "push na main", "push pra main", "push origin", "push", "prod", "em prod",
    "sobe em prod", "faxina", "limpa o repo", "apaga arquivos", "reversao", "reverte",
    "rollback", "ship", "shipped", "shipping", "go live", "goes live", "going live",
    "push to prod", "push to production", "despliega", "produccion", "supprime", "publie",
    "deployer", "deploie", "mets en ligne", "veroffentlicht", "bereitstellen", "pubblica",
    # --- sinais que só existiam no henke_auto (união P1-04) ---
    "publico", "coloca no ar", "manda pra prod", "manda para prod", "bota no servidor",
    "coloca no servidor", "sobe pra producao", "manda pro ar", "released", "releases", "sube",
    # --- imperativos/subjuntivos PT ("publique o pacote", "suba para o
    # servidor" não casavam "publica"/"sobe" — deploy sem trava humana).
    "publique", "suba", "suba para", "descarregue", "libere",
)
PUBLISH_SIGNALS = PUBLISH_ONLY_SIGNALS + DESTRUCTIVE_SIGNALS
_PUBLISH_SIGNALS = PUBLISH_SIGNALS

# Metaconfig do projeto-alvo (package.json/lockfile/workflow/CI) — mexer
# neles muda a própria esteira de verificação: sempre exige humano.
METACONFIG_SIGNALS = (
    "package.json", "package-lock", "lockfile", "requirements", "pyproject",
    "workflow", "workflows", "ci.yml", "metaconfig", "build.rs", "dockerfile",
    "makefile", "lock.json",
)
