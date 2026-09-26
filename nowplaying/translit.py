"""Lyrics in Latin letters -- transliterated, never translated.

Lines in any script that isn't Latin (Cyrillic, Greek, Korean, Arabic, Thai,
Chinese, Japanese, ...) are published spelled out in Latin letters, so they
can be read along with. Only the non-Latin runs of a line are converted;
Latin text in it, accents and all, stays exactly as written.

Backends, best available wins:
  anyascii  -- every script, character by character.
  pypinyin  -- Chinese as tone-marked pinyin.
  cutlet    -- Japanese. A kanji has several readings and anyascii would give
               the Chinese one, so Japanese needs a dictionary (MeCab over
               unidic-lite) to read it in context; without one, Japanese
               lyrics are left as they are rather than misread.

All optional: with none installed, lyrics are published unchanged.
NOWPLAYING_TRANSLITERATE=0 turns it off.
"""
from __future__ import annotations

import importlib
import itertools
import logging
import os
import re
import unicodedata
from collections.abc import Callable

log = logging.getLogger("nowplaying.translit")

ENABLED = os.environ.get("NOWPLAYING_TRANSLITERATE", "1") != "0"

_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[㐀-䶿一-鿿]")
_PUNCT = str.maketrans({"「": '"', "」": '"', "『": '"', "』": '"', "、": ", ",
                        "。": ". ", "・": " ", "〜": "~", "～": "~", "…": "...",
                        "　": " "})
_NO_SPACE_AFTER = "([\"'"
_NO_SPACE_BEFORE = ")]\"'.,!?~"

_loaded: dict[str, object] = {}


def _backend(name: str):
    """Import a backend on first use, or None if it isn't installed. Lazy,
    because the Japanese dictionary alone costs ~35 MB of memory."""
    if name not in _loaded:
        try:
            mod = importlib.import_module(name)
            if name == "cutlet":
                katsu = mod.Cutlet()
                katsu.use_foreign_spelling = False   # what is sung, not the loanword's source
                _loaded[name] = katsu
            else:
                _loaded[name] = mod
        except Exception as exc:   # not installed, or its dictionary is missing
            log.info("transliteration backend %s unavailable (%s)", name, exc)
            _loaded[name] = None
    return _loaded[name]


def _foreign(c: str) -> bool:
    """A letter (or combining mark) from a script other than Latin."""
    return (unicodedata.category(c)[0] in "LM"
            and "LATIN" not in unicodedata.name(c, "LATIN"))


def _caseless(c: str) -> bool:
    return c.lower() == c.upper()


def _converter(texts: list[str]) -> Callable[[str], str] | None:
    """How to spell this track's non-Latin runs, or None to leave it alone.

    Decided per track, not per line: a Japanese line can be all kanji, and
    only the rest of the song gives away that it isn't Chinese.
    """
    joined = "".join(texts)
    if _KANA.search(joined):
        katsu = _backend("cutlet")
        if katsu is None:
            return None
        # It capitalises what it takes for names, which mid-line mostly
        # isn't one -- colloquial kana can split into something name-like.
        return lambda run: katsu.romaji(run, capitalize=False).lower()

    pinyin = _backend("pypinyin") if _HAN.search(joined) else None
    anyascii = _backend("anyascii")
    if pinyin is None and anyascii is None:
        return None

    def convert(run: str) -> str:
        if pinyin is not None and _HAN.search(run):
            return " ".join(pinyin.lazy_pinyin(run, style=pinyin.Style.TONE))
        if anyascii is None:
            return run
        out = anyascii.anyascii(run)
        if _HAN.search(run):     # "WoAiNi": one capital per syllable
            return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", out).lower()
        if _caseless(run[0]):    # Hangul, Arabic, Thai...: no case to keep
            return out.lower()
        return out               # Cyrillic, Greek: keep the source's case

    return convert


def _line(text: str, convert: Callable[[str], str]) -> str:
    if not any(_foreign(c) for c in text):
        return text
    # NFKC folds full-width ！？（）Ａ and half-width ｶﾀｶﾅ into the usual forms.
    text = unicodedata.normalize("NFKC", text).translate(_PUNCT)
    out: list[str] = []
    for foreign, chars in itertools.groupby(text, _foreign):
        chunk = "".join(chars)
        if not foreign:
            out.append(chunk)
            continue
        r = convert(chunk)
        before = out[-1][-1:] if out else ""
        if before and not before.isspace() and before not in _NO_SPACE_AFTER:
            r = " " + r
        out.append(r)
    # Space before whatever follows a converted run, unless it's punctuation.
    res = ""
    for i, chunk in enumerate(out):
        if i and res[-1:].isalnum() and chunk[:1] and chunk[0] not in _NO_SPACE_BEFORE \
                and not chunk[0].isspace():
            res += " "
        res += chunk
    res = re.sub(r"\s{2,}", " ", res).strip()
    first = text.lstrip()[:1]
    return res[:1].upper() + res[1:] if first and _foreign(first) and _caseless(first) else res


def lyrics(lines: list[tuple[float, str]],
           plain: str) -> tuple[list[tuple[float, str]], str] | None:
    """(synced lines, plain text) in Latin letters, or None to leave them."""
    texts = [t for _, t in lines] + [plain]
    if not ENABLED or not any(_foreign(c) for t in texts for c in t):
        return None
    convert = _converter(texts)
    if convert is None:
        return None
    return ([(t, _line(s, convert)) for t, s in lines],
            "\n".join(_line(s, convert) for s in plain.splitlines()))
