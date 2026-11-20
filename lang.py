"""Per-language data for the text rules. English is the default and is untouched by anything here.

`load_config` calls `set_language(config["language_code"])`, so every stage that follows works in the
site's language. Sites run one after another, each loading its own config first.
"""
from __future__ import annotations

import re

_LANG = "en"
SUPPORTED = {"en", "es"}


def set_language(code: str | None) -> None:
    global _LANG
    code = (code or "en").lower().split("-")[0]
    _LANG = code if code in SUPPORTED else "en"


def get_language() -> str:
    return _LANG


def is_es() -> bool:
    return _LANG == "es"


# ---------- Spanish ----------

ES_STOPWORDS = {
    "a", "al", "con", "de", "del", "el", "en", "es", "la", "las", "lo", "los", "para", "por", "que", "se",
    "son", "su", "sus", "tu", "tus", "un", "una", "unos", "unas", "y", "o", "mi", "mis",
    "cerca", "vs", "sobre", "ya", "le", "te", "me", "nos", "esta", "este",
    "esto", "ese", "esa", "hay", "ser", "muy", "mas", "sin", "e", "u",
}

# Phrase -> replacement. Every replacement is grammatical wherever the phrase can appear.
ES_BANNED_REPLACEMENTS = {
    "en el mundo actual": "hoy",
    "en la era digital": "hoy",
    "en el panorama actual": "hoy",
    "es importante destacar que": "ten en cuenta que",
    "es importante mencionar que": "ten en cuenta que",
    "cabe destacar que": "además,",
    "cabe mencionar que": "además,",
    "en conclusión": "al final",
    "en resumen": "en pocas palabras",
    "en el ámbito de": "en",
    "sin lugar a dudas": "sin duda",
    "a la hora de": "al",
    "sumérgete en": "entra a",
    "adentrémonos en": "entremos a",
}
ES_BANNED_PHRASES = list(ES_BANNED_REPLACEMENTS)

ES_DISCLOSURE_PATTERNS = [
    r"\bcomo (una )?(ia|inteligencia artificial)\b", r"\bmodelo de lenguaje\b",
    r"\b(generad|escrit|redactad|cread)[oa] (por|con) (una )?(ia|inteligencia artificial)\b",
    r"\bsoy una (ia|inteligencia artificial)\b",
    r"\beste (artículo|texto|contenido) fue (generado|escrito|creado|redactado) (por|con)\b",
]

ES_CORPORATE = ["sinergia", "paradigma", "holístic", "empoderar", "de clase mundial", "valor agregado",
                "soluciones integrales", "revolucionar", "disruptiv", "misión crítica", "de primer nivel"]

ES_ANALOGY = re.compile(
    r"\b(piensa en|imagina|imagínate|es como|son como|funciona como|funcionan como|trabaja como|"
    r"actúa como|actúan como|como un |como una |como si |igual que|lo mismo que|de la misma (forma|manera)|"
    r"se parece a|analogía|modelo mental|regla de (oro|dedo)|"
    r"(principio|efecto|regla|ley|método|prueba|truco)\b)", re.I)

ES_NON_MAJOR = re.compile(
    r"^(preguntas frecuentes|faq|a dónde ir|siguientes? pasos?|qué hacer (ahora|después)|fuentes|"
    r"lecturas? recomendadas?|puntos clave|lo esencial|en resumen)", re.I)

# Second person (tuteo) markers.
ES_DIRECT_ADDRESS = re.compile(
    r"\b(tú|tu|tus|te|ti|contigo|puedes|tienes|debes|necesitas|quieres|vas|notas|sientes|revisa|"
    r"pregunta|pide|compara|fíjate|busca|lleva|cambia)\b", re.I)

ES_BOILERPLATE = re.compile(
    r"^(relacionad[oa]s?|recientes|populares|últim[oa]s|más) |tabla de contenidos?|índice$|contenidos?$|"
    r"compart(e|ir)|comentarios?|deja (un|tu) comentario|sobre el autor|acerca de|nosotros|contacto|contácta|"
    r"suscr[ií]b|bolet[ií]n|newsletter|s[ií]guenos|también te puede|te puede interesar|leer más|ver más|"
    r"referencias|fuentes$|men[uú]|buscar|categor[ií]as|etiquetas$|conclusi[oó]n|conclusiones|resumen$|"
    r"preguntas frecuentes|faq|cotiza|ll[aá]manos|nuestr[oa]s (servicios|clientes|productos|sucursales|marcas)|"
    r"testimonios|opiniones$|reseñas$|privacidad|cookies|aviso legal|horarios?|promociones|ofertas|"
    r"sucursales|ubicaci[oó]n|qui[eé]nes somos|carrito|env[ií]os?|productos relacionados|agenda|"
    r"reserva|solicita|c[oó]mo llegar|formas de pago|facturaci[oó]n", re.I)

ES_TOPIC_SIGNAL = re.compile(
    r"^(c[oó]mo|qu[eé]|por qu[eé]|cu[aá]ndo|cu[aá]l|cu[aá]les|cu[aá]nto|cu[aá]ntos|qui[eé]n|d[oó]nde|es|son|"
    r"se puede|puedo|debo)\b|\?$|\b(costos?|precios?|beneficios?|tipos?|errores?|consejos?|pasos?|proceso|"
    r"ejemplos?|señales?|s[ií]ntomas?|fallas?|ventajas?|desventajas?|diferencias?|gu[ií]a|funcionan?|"
    r"funcionamiento|mantenimiento|riesgos?|opciones|marcas?|duran?|duraci[oó]n|vida [uú]til|cambiar|cambio|"
    r"elegir|comparar|comparativa|vs\.?|versus|mejor(es)?|causas?|garant[ií]a|revisar|revisi[oó]n|"
    r"recomendaciones|requisitos|factores|alternativas|instalaci[oó]n|instalar)\b", re.I)

ES_SKELETON = {
    "informational": ["{Kw}: qué significa en la práctica", "{Kw}: cómo funciona, paso a paso",
                      "{Kw}: qué cambia el resultado", "{Kw}: los errores más comunes"],
    "commercial": ["{Kw}: en qué fijarte", "{Kw}: cómo se comparan las opciones principales",
                   "Precio de {kw}: de qué depende el costo", "{Kw}: cómo elegir la opción correcta"],
    "transactional": ["{Kw}: qué recibes realmente", "Precio de {kw}: de qué depende el costo",
                      "{Kw}: cómo elegir dónde comprar", "{Kw}: qué revisar antes de pagar"],
    "navigational": ["{Kw}: qué es", "{Kw}: por dónde empezar", "{Kw}: qué saber primero"],
}
ES_MISSING_ANGLES = ["{Kw}: lo que casi ninguna guía menciona", "{Kw}: cómo se ve cuando sale mal",
                     "{Kw}: preguntas que hacer antes de gastar"]
ES_FAQ_FALLBACK = ["¿Qué incluye {kw}?", "¿Cuánto cuesta normalmente {kw}?",
                   "¿Qué hay que revisar primero en {kw}?"]
ES_FAQ_H2 = "Preguntas frecuentes sobre {kw}"

ES_ACRONYMS = {"kyb", "suv", "abs", "gdl", "cdmx", "led", "faq", "oem"}

ES_WRITER_RULES = """
Language: write the whole piece (title, meta description, every heading and all body text) in natural Mexican \
Spanish. Use "tú", never "usted" or "vosotros". Use the words a driver or mechanic in Mexico uses. Translate any \
outline heading that arrives in English. Use Spanish punctuation (¿? ¡!) and sentence-case headings. The FAQ must be \
the final H2 and its heading must start with "Preguntas frecuentes". Keep the TITLE:/DESCRIPTION:/=== output \
markers exactly as written, in English. Phrases to avoid, in any casing: {banned}."""

ES_EDITOR_RULES = """
The article must be in natural Mexican Spanish ("tú" form). Flag English left in the text, translated-sounding \
phrasing and vocabulary that is not used in Mexico. Write your fixes in Spanish or English; keep the JSON keys in \
English."""
