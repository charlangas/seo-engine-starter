"""AI-tell ("slop") rules for the writer: deterministic fixes plus advisory critique codes.

Two layers, English and Spanish:
- `scrub(text)` rewrites what can be fixed mechanically and safely, outside code blocks: em and en dashes
  (a comma, or a hyphen between numbers), spaced hyphens used as dashes, curly quotes, pompous verbs
  ("utilize" -> "use", "in order to" -> "to") and stock chatbot lines ("Let's dive in.", "I hope this helps.").
- `critique(article, fix)` lists what needs a rewrite rather than a swap: "not just X, but Y" frames,
  AI vocabulary, puffery, unnamed authorities ("experts say"), "in this article we'll..." openers and
  formulaic transitions. Each is advisory (the writer and editor are asked to fix it, but it does not fail the
  article on its own). When the total passes `slop.max_hits` (default 8) a blocking `slop_heavy` fix is added.

Config (optional):

    slop:
      max_hits: 8          # more advisory hits than this fails the article
      allow: [landscape]   # words this site legitimately needs (a landscaping company)
"""
from __future__ import annotations

import re

import lang
from config import ConfigError

DEFAULTS = {"max_hits": 8, "allow": []}

# ---------- deterministic swaps (whole words, case kept for the first letter) ----------

EN_SWAPS = [
    (r"\butiliz(e|es|ed|ing)\b", {"e": "use", "es": "uses", "ed": "used", "ing": "using"}),
    (r"\butilis(e|es|ed|ing)\b", {"e": "use", "es": "uses", "ed": "used", "ing": "using"}),
    (r"\bin order to\b", "to"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bin the event that\b", "if"),
    (r"\bcommenc(e|es|ed|ing)\b", {"e": "start", "es": "starts", "ed": "started", "ing": "starting"}),
    (r"\bfacilitat(e|es|ed|ing)\b", {"e": "help", "es": "helps", "ed": "helped", "ing": "helping"}),
    (r"\bnumerous\b", "many"),
    (r"\bserves as\b", "is"),
    (r"\bstands as\b", "is"),
]
ES_SWAPS = [
    (r"\butiliza(r)?\b", {"": "usa", "r": "usar"}),
    (r"\bcon el fin de\b", "para"),
    (r"\bdebido al hecho de que\b", "porque"),
    (r"\ben el caso de que\b", "si"),
    (r"\bsirve como\b", "es"),
]
# Whole stock sentences removed outright.
EN_DROP = re.compile(r"(?:(?<=^)|(?<=[.!?]\s))(let'?s (dive|jump|get started|dig) in|i hope (this|that) helps"
                     r"|happy (reading|learning)|without further ado)[^.!?\n]*[.!?]\s*", re.I | re.M)
ES_DROP = re.compile(r"(?:(?<=^)|(?<=[.!?]\s))(¡?(vamos|manos) a (ello|la obra)!?|espero que (esto|este artículo|esta guía) "
                     r"te (ayude|sirva)|sin más preámbulos?|sumerjámonos)[^.!?\n]*[.!?]?\s*", re.I | re.M)

# ---------- advisory patterns ----------

EN_PATTERNS = {
    "slop_contrast_frame": (r"\bnot (just|only|merely) [^.\n]{1,60}?,? but\b|\bit'?s not (about )?[^.,\n]{1,40}, it'?s\b"
                            r"|\bisn'?t (just|only) [^.\n]{1,50}?[,;] it'?s\b",
                            'Drop the "not X, but Y" frame and state Y directly.'),
    "slop_ai_vocabulary": (r"\b(delve\w*|pivotal|tapestry|landscape|testament|underscor\w*|foster\w*|garner\w*|interplay|"
                           r"intricate|crucial|vibrant|enduring|showcas\w*|moreover|furthermore|additionally|"
                           r"realm|navigat(e|ing) the (complex|world)|ever-evolving|elevate|empower\w*|embark\w*|"
                           r"meticulous\w*|boasts?|paramount)\b",
                           'Replace the AI-sounding word "{m}" with the plain word, or cut it.'),
    "slop_metaphor_noun": (r"\b(substrate|nexus|locus|bedrock|north star|flywheel|linchpin|cornerstone)\b",
                           'Replace the abstract noun "{m}" with a concrete one.'),
    "slop_puffery": (r"\b(groundbreaking|revolutionary|game-?changing|blazing[- ]fast|world-class|next-generation|"
                     r"state-of-the-art|unparalleled|unmatched|best-in-class|top-notch|second to none)\b",
                     'Cut the puffery "{m}" and put the number or the mechanism there instead.'),
    "slop_vague_authority": (r"\b(experts (believe|say|agree|recommend)|industry reports?( suggest| show)?|studies (show|suggest)|"
                             r"research shows|many (teams|people|businesses) find|it is widely (known|accepted))\b",
                             'Name the source and link it, or state the fact without "{m}".'),
    "slop_chatbot_opener": (r"\b(in this (article|guide|post),? (we'?ll|we will|you'?ll|you will|i'?ll)|as we have seen|"
                            r"as mentioned (above|earlier)|we'?ll explore|let'?s explore|buckle up)\b",
                            'Remove "{m}": start with the answer instead of announcing it.'),
    "slop_formulaic_transition": (r"\b(despite these challenges|while [^.,\n]{1,40} has its (pros and cons|advantages and disadvantages)|"
                                  r"at the end of the day|when it comes to|in today'?s (world|fast-paced|market))\b",
                                  'Replace the stock transition "{m}" with the specific trade-off.'),
    "slop_false_range": (r"\bfrom (startups|small businesses|beginners) to (fortune 500|enterprises?|experts|pros)\b",
                         'Cut the false range "{m}"; name the actual readers.'),
}
ES_PATTERNS = {
    "slop_contrast_frame": (r"\bno (solo|sólo|solamente) [^.\n]{1,60}?,? sino( también)?\b|\bno se trata de [^.\n]{1,50}?[,;] se trata de\b",
                            'Quita el marco "no solo X, sino Y" y di Y directamente.'),
    "slop_ai_vocabulary": (r"\b(crucial(es)?|fundamental(es)? para|vibrante|intrincad[oa]s?|fomentar|potenciar|"
                           r"asimismo|cabe resaltar|es menester|panorama|un testimonio de|en el vertiginoso|"
                           r"sumerg(ir|irse|irte)|adentr(ar|arse|arte)|navegar (el|por el) (complejo|mundo)|"
                           r"embarcar(se|te)|meticulos[oa]s?|primordial)\b",
                           'Cambia "{m}" por la palabra sencilla, o quítala.'),
    "slop_metaphor_noun": (r"\b(piedra angular|hoja de ruta|columna vertebral|ecosistema)\b",
                           'Cambia "{m}" por un sustantivo concreto.'),
    "slop_puffery": (r"\b(revolucionari[oa]s?|innovador(a|es)?|de vanguardia|sin igual|sin precedentes|incomparable|"
                     r"de última generación|inigualable|excepcional(es)?)\b",
                     'Quita el adorno "{m}" y pon el dato o el mecanismo.'),
    "slop_vague_authority": (r"\b(los expertos (dicen|creen|recomiendan|coinciden)|estudios (demuestran|muestran|indican)|"
                             r"según (los )?expertos|está comprobado que|se sabe que)\b",
                             'Nombra y enlaza la fuente, o di el dato sin "{m}".'),
    "slop_chatbot_opener": (r"\b(en este (artículo|post|texto|blog),? (te )?(exploraremos|veremos|vamos a ver|te contaremos|"
                            r"descubrirás|aprenderás)|como hemos visto|como mencionamos (antes|arriba))\b",
                            'Quita "{m}": empieza con la respuesta.'),
    "slop_formulaic_transition": (r"\b(a pesar de estos desafíos|al final del día|cuando se trata de|en el mundo actual|"
                                  r"hoy en día más que nunca)\b",
                                  'Cambia la transición "{m}" por el punto concreto.'),
}

_FENCE = re.compile(r"(```.*?```|~~~.*?~~~)", re.S)
_LINK_URL = re.compile(r"(\]\([^)]*\)|https?://\S+|`[^`]*`)")


def settings(config) -> dict:
    raw = config.get("slop") or {}
    if not isinstance(raw, dict):
        raise ConfigError("slop must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"slop: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    if not isinstance(s["max_hits"], int) or s["max_hits"] < 0:
        raise ConfigError("slop.max_hits must be 0 or more")
    if not isinstance(s["allow"], list):
        raise ConfigError("slop.allow must be a list of words")
    s["allow"] = [str(a).lower() for a in s["allow"]]
    return s


def _outside_code(text: str, fn) -> str:
    parts = _FENCE.split(text)
    return "".join(p if i % 2 else fn(p) for i, p in enumerate(parts))


def _protect(text: str, fn) -> str:
    """Apply fn to prose only: link targets, URLs and inline code are left byte-for-byte."""
    keep = []

    def stash(m):
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"
    out = fn(_LINK_URL.sub(stash, text))
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], out)


def _case_like(src: str, repl: str) -> str:
    return repl[:1].upper() + repl[1:] if src[:1].isupper() else repl


def _dashes(t: str) -> str:
    t = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", t)               # 3–5 -> 3-5
    t = re.sub(r"^(\s*)[–—]\s+", r"\1- ", t, flags=re.M)          # dash used as a bullet
    t = re.sub(r"\s*[–—]\s*([.,;:!?)])", r"\1", t)                # dash before punctuation
    t = re.sub(r"\s*[–—]\s*$", "", t, flags=re.M)                 # trailing dash
    t = re.sub(r"\s*[–—]\s*", ", ", t)                            # aside -> comma
    t = re.sub(r"(?<=[A-Za-z0-9À-ſ,)]) - (?=[A-Za-z(À-ſ])", ", ", t)  # spaced hyphen as a dash
    t = re.sub(r",\s*,", ",", t)
    return t


def scrub(text: str) -> str:
    """Mechanical, meaning-preserving fixes. Idempotent."""
    if not text:
        return text
    es = lang.is_es()

    def prose(t: str) -> str:
        t = (t.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'"))
        t = _dashes(t)
        for pat, repl in (ES_SWAPS if es else EN_SWAPS):
            if isinstance(repl, dict):
                t = re.sub(pat, lambda m, r=repl: _case_like(m.group(0), r[(m.group(1) or "").lower()]), t, flags=re.I)
            else:
                t = re.sub(pat, lambda m, r=repl: _case_like(m.group(0), r), t, flags=re.I)
        t = (ES_DROP if es else EN_DROP).sub("", t)
        return t

    def lines(t: str) -> str:   # headings and table separators keep their own shape
        out = []
        for line in t.split("\n"):
            if re.match(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", line) or line.startswith("---"):
                out.append(line)
            else:
                out.append(_protect(line, prose))
        return "\n".join(out)
    return _outside_code(text, lines)


def hits(text: str, allow=()) -> list[dict]:
    """Advisory matches in prose (code blocks, link targets and inline code ignored)."""
    prose = _LINK_URL.sub(" ", _FENCE.sub(" ", text))
    out = []
    for code, (pat, msg) in (ES_PATTERNS if lang.is_es() else EN_PATTERNS).items():
        for m in re.finditer(pat, prose, flags=re.I):
            word = m.group(0).strip()
            if word.lower() in allow:
                continue
            if code == "slop_vague_authority":
                sent_end = prose.find(".", m.end())
                if "](" in text and re.search(r"\]\(", prose[m.start(): sent_end if sent_end > 0 else None] or ""):
                    continue
            out.append({"code": code, "match": word, "message": msg.format(m=word)})
    return out


def critique(article: dict, config: dict, fix) -> None:
    s = settings(config)
    found = hits("\n".join([article.get("title", ""), article.get("description", ""), article.get("body", "")]), s["allow"])
    seen = set()
    for h in found:
        key = (h["code"], h["match"].lower())
        if key in seen:
            continue
        seen.add(key)
        fix(h["code"], h["message"], match=h["match"], advisory=True)
    if len(found) > s["max_hits"]:
        fix("slop_heavy", f"The text has {len(found)} AI-sounding phrases (limit {s['max_hits']}). Rewrite the flagged "
                          "sentences in plain, specific language.", have=len(found))


PROMPT_RULES_EN = """- No em dashes or en dashes anywhere; use a period, comma or colon. Number ranges use a plain hyphen (3-5).
- No "not just X, but Y" or "it's not X, it's Y" frames. State the point.
- Plain words: never delve, pivotal, tapestry, landscape, testament, underscore, foster, garner, crucial, vibrant, \
showcase, moreover, furthermore, additionally, utilize. No puffery (groundbreaking, game-changing, world-class): give \
the number or the mechanism instead.
- Never "experts say" or "studies show" without naming and linking the source.
- No chatbot lines: "In this article we'll explore", "Let's dive in", "I hope this helps", "As we have seen"."""
PROMPT_RULES_ES = """- Sin rayas (— o –); usa punto, coma o dos puntos. Los rangos de números llevan guion simple (3-5).
- Nada de "no solo X, sino Y". Di el punto directamente.
- Palabras sencillas: evita crucial, vibrante, intrincado, fomentar, potenciar, asimismo, panorama, sumergirse, \
de vanguardia, revolucionario. Pon el dato o el mecanismo en lugar del adorno.
- Nunca "los expertos dicen" o "estudios demuestran" sin nombrar y enlazar la fuente.
- Sin frases de chatbot: "En este artículo exploraremos", "¡Vamos a ello!", "Espero que te sirva"."""


def prompt_rules() -> str:
    return PROMPT_RULES_ES if lang.is_es() else PROMPT_RULES_EN
