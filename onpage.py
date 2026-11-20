"""On-page and AI-search rules every page gets (not only comparison pages).

From the video's on-page checklist and the AI Search Checklist:
- Key takeaways: the first H2 is a short bullet summary an answer engine can lift whole.
- Bottom line up front: the introduction answers the query in its first two sentences.
- Query fan-out: the page answers the implicit questions behind the query (cost, time, risks, who it is not
  for, next steps, ...), each as an H2 or an FAQ question.
- Extractable, self-contained sections: no section opens by leaning on an earlier one ("This is why...",
  "As mentioned above"), and paragraphs stay short.
- "Best for" labels on every option when the page lists options.
- CTAs at the top, middle and bottom when the site has a CTA.
- Author byline (with social profiles), reading time, `updatedDate`, and JSON-LD (Article or Service, Person,
  Organization, FAQPage) in the frontmatter for the layout to render.

Everything is on by default and tuned under `on_page:` in the site config. The hard rules are part of the
critique; `aiaudit.py` scores published pages against the full checklist.
"""
from __future__ import annotations

import math
import re

import lang
from config import ConfigError
from textutil import contains_keyword as contains_kw, content_year, is_faq_heading, normalize_quotes, plain_text, split_sections, word_count

DEFAULTS = {
    "key_takeaways": True,
    "fanout": ["cost", "timeline", "risks", "not_for", "next_steps"],
    "max_paragraph_words": 90,
    "self_contained_sections": True,
    "best_for_labels": True,
    "author": None,            # {name, title, url, image, bio, socials: {linkedin, x, ...}}; default comparisons.author
    "cta": None,               # {label, url}; default comparisons.cta. Linked top, middle and bottom.
    "cta_min_links": 3,
    "organization": None,      # {name, url, logo, sameAs: [...]} for JSON-LD; default site_name + domain
    "json_ld": True,
}

# Implicit-question buckets (query fan-out). A bucket is covered when an H2 or H3 matches.
BUCKETS = {
    "cost": ("How much does it cost?", r"\b(cost|costs|price|prices|pricing|budget|how much|fees?|rates?|expensive|cheap)\b",
             "¿Cuánto cuesta?", r"\b(costo|costos|coste|precio|precios|cu[aá]nto (cuesta|sale|cobra)|presupuesto|tarifa)"),
    "timeline": ("How long does it take?", r"\b(how long|timeline|time ?frame|turnaround|set ?up|onboarding|delivery|takes?|"
                                           r"how (fast|soon|quickly)|lead time|duration)\b",
                 "¿Cuánto tiempo tarda?", r"(cu[aá]nto (tiempo|tarda|dura)|plazo|tiempo de|entrega|instalaci[oó]n|demora|"
                                          r"cada cu[aá]nto)"),
    "effort": ("How hard is it?", r"\b(difficult|difficulty|effort|hard|diy|yourself|skills?|tools you need|resources)\b",
               "¿Qué tan difícil es?", r"(dif[ií]cil|esfuerzo|t[uú] mismo|uno mismo|herramientas)"),
    "risks": ("What are the risks and common mistakes?", r"\b(risks?|mistakes?|limitations?|downsides?|avoid|pitfalls?|"
                                                         r"cons|warning|red flags?|problems?|wrong)\b",
              "¿Qué riesgos y errores comunes hay?", r"(riesgo|error|limitaci[oó]n|desventaja|evitar|contras|"
                                                    r"señales? de alerta|problema|fallas?)"),
    "not_for": ("Who is this not right for?", r"(\bnot (right|a good fit|for|worth)\b|who should(n't| not)|\bskip\b|"
                                              r"isn't (right )?for|when not to|is it worth)",
                "¿Para quién no es?", r"(no (es|conviene|vale) |qui[eé]n no|cu[aá]ndo no|vale la pena)"),
    "next_steps": ("How do you choose and get started?", r"(next steps?|get(ting)? started|how (do i|to) choose|choosing|"
                                                         r"what to ask|questions to ask|before (you )?(buy|book|hire|choose|sign)|"
                                                         r"checklist|decision|how to pick|what to look for)",
                   "¿Cómo elegir y por dónde empezar?", r"(siguientes? pasos?|c[oó]mo elegir|qu[eé] preguntar|antes de "
                                                       r"(comprar|contratar|elegir)|lista de|por d[oó]nde empezar|"
                                                       r"qu[eé] revisar|c[oó]mo saber)"),
    "after": ("What happens after you start?", r"(what happens (after|next)|after (you|the)|what to expect|aftercare|"
                                               r"maintenance)",
              "¿Qué pasa después?", r"(qu[eé] pasa despu[eé]s|despu[eé]s de|qu[eé] esperar|mantenimiento)"),
    "compare": ("How does it compare with the alternatives?", r"(\bvs\.?\b|versus|alternatives?|compar(e|ed|ison)|"
                                                             r"options|instead of|or\b.*\?)",
                "¿Cómo se compara con las alternativas?", r"(\bvs\.?\b|alternativa|compar|opciones|en lugar de)"),
    "who_for": ("Who is it for?", r"(who (is it|it's|this is|needs)|best for|is it for|ideal for|for (beginners|"
                                  r"small|large|homeowners|businesses|teams))",
                "¿Para quién es?", r"(para qui[eé]n|ideal para|mejor para|qui[eé]n necesita)"),
    "what_is": ("What is it and how does it work?", r"(what (is|are)|definition|how does .* work|how .* works|meaning)",
                "¿Qué es y cómo funciona?", r"(qu[eé] (es|son)|c[oó]mo funciona|definici[oó]n|para qu[eé] sirve)"),
}

TAKEAWAYS_RE = re.compile(r"^(key takeaways|takeaways|tl;?dr|the short version|in short|puntos clave|lo esencial|"
                          r"en resumen|resumen r[aá]pido)", re.I)
BACKREF_OPEN_EN = re.compile(r"^(this|these|that|those|it|they|such|as (mentioned|noted|discussed|we saw|shown)|"
                             r"like we said|as you (saw|read)|above|so|also|and|but)\b", re.I)
BACKREF_OPEN_ES = re.compile(r"^(esto|estos|esta|estas|eso|esos|esa|esas|ello|como (mencionamos|vimos|dijimos)|"
                             r"adem[aá]s|tambi[eé]n|y|pero|as[ií] que)\b", re.I)
BACKREF_ANY = re.compile(r"(as (mentioned|noted|discussed|explained) (above|earlier|before|previously)|"
                         r"as we (saw|discussed|covered|explained) (above|earlier|before)|see above|the above section|"
                         r"como (mencionamos|vimos|dijimos|explicamos) (antes|arriba|anteriormente)|m[aá]s arriba|"
                         r"la secci[oó]n anterior)", re.I)
BEST_FOR_RE = re.compile(r"(\bbest for\b|\bideal for\b|\bgood fit for\b|\bmejor para\b|\bideal para\b)", re.I)
LIST_KW_RE = re.compile(r"\b(best|top \d+|top|alternatives?|options|mejores|alternativas|opciones)\b", re.I)


def settings(config) -> dict:
    raw = config.get("on_page") or {}
    if not isinstance(raw, dict):
        raise ConfigError("on_page must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"on_page: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    if not isinstance(s["fanout"], list) or set(s["fanout"]) - set(BUCKETS):
        raise ConfigError(f"on_page.fanout must be a list of: {', '.join(BUCKETS)}")
    if not isinstance(s["max_paragraph_words"], int) or s["max_paragraph_words"] < 30:
        raise ConfigError("on_page.max_paragraph_words must be a whole number of 30 or more")
    if not isinstance(s["cta_min_links"], int) or not 0 <= s["cta_min_links"] <= 6:
        raise ConfigError("on_page.cta_min_links must be 0-6")
    for k in ("author", "cta", "organization"):
        if s[k] is not None and not isinstance(s[k], dict):
            raise ConfigError(f"on_page.{k} must be a mapping")
    if s["cta"] is not None and not s["cta"].get("url"):
        raise ConfigError("on_page.cta needs a url")
    comp = config.get("comparisons") or {}
    if s["author"] is None and isinstance(comp.get("author"), dict):
        s["author"] = comp["author"]
    if s["cta"] is None and isinstance(comp.get("cta"), dict) and comp["cta"].get("url"):
        s["cta"] = comp["cta"]  # a site-wide {label, url}; per-page-type CTAs stay on comparison pages
    return s


def bucket_label(b: str) -> str:
    q_en, _, q_es, _ = BUCKETS[b]
    return q_es if lang.is_es() else q_en


def bucket_re(b: str):
    _, en, _, es = BUCKETS[b]
    return re.compile(es if lang.is_es() else en, re.I)


def is_list_page(brief: dict) -> bool:
    pt = (brief.get("comparison") or {}).get("page_type")
    if pt:
        return pt in ("alternatives", "best")
    if brief.get("format") == "listicle":
        return True
    return bool(LIST_KW_RE.search(brief["primary_keyword"]))


def attach(brief: dict, config: dict) -> dict:
    """Set brief["on_page"] (idempotent): which buckets to cover, the CTA, list-page flag."""
    if "on_page" in brief:
        return brief["on_page"]
    s = settings(config)
    cta = (brief.get("comparison") or {}).get("cta") or s["cta"]
    headings = [o["h2"] for o in brief.get("outline", [])] + [h for o in brief.get("outline", []) for h in o.get("h3", [])]
    covered = [b for b in s["fanout"] if any(bucket_re(b).search(h) for h in headings)]
    brief["on_page"] = {
        "key_takeaways": bool(s["key_takeaways"]), "fanout": list(s["fanout"]), "fanout_in_outline": covered,
        "max_paragraph_words": s["max_paragraph_words"], "self_contained": bool(s["self_contained_sections"]),
        "content_year": content_year(config),
        "best_for": bool(s["best_for_labels"]) and is_list_page(brief),
        "cta": cta if cta and cta.get("url") else None,
        "cta_min_links": s["cta_min_links"] if not brief.get("comparison") else max(s["cta_min_links"], 3),
    }
    return brief["on_page"]


def takeaways_heading() -> str:
    return "Puntos clave" if lang.is_es() else "Key takeaways"


# ---------------------------------------------------------------- prompt

def prompt_block(brief: dict) -> str:
    op = brief.get("on_page")
    if not op:
        return ""
    import serpclass
    fmt = serpclass.FORMAT_GUIDE.get(brief.get("format") or "", "")
    L = ["", "", "Answer-engine rules (AI Overviews and chat assistants quote pages that follow them):",
         *(["- Page format: " + fmt] if fmt and not brief.get("comparison") else []),
         "- Bottom line up front: the first two sentences of the introduction answer the search directly, with a "
         "specific number, range or recommendation.",
         "- Write for extraction: every H2 section makes sense read alone. Open it by naming its subject in full; "
         "never open with \"This\", \"These\", \"It\" or \"As mentioned\".",
         "- Phrase most H2s as the question a reader would type, and answer it in the section's first sentence.",
         "- Subject, verb, object. Name the brand, product, place, person or standard instead of \"it\" or "
         "\"this tool\". Concrete beats general.",
         f"- Paragraphs of 1-3 sentences, never over {op['max_paragraph_words']} words. Use bullet points for steps and "
         "options.",
         f"- Mention {op.get('content_year') or content_year()} where prices, rules or availability are time-sensitive (never an earlier year).",
         "- No vague promotional claims (\"industry-leading\", \"the best service\") unless a number backs them."]
    if op["key_takeaways"]:
        L.append(f'- Right after the introduction, add "## {takeaways_heading()}" with 3-5 bullets, each one complete '
                 "sentence of at most 30 words that states a fact, number or rule from the article.")
    need = [b for b in op["fanout"] if b not in op["fanout_in_outline"]]
    if need:
        L.append("- Cover these follow-up questions, each as an H2 or as an FAQ question (H3) with a direct answer: "
                 + "; ".join(f'"{bucket_label(b)}"' for b in need) + ".")
    if op["best_for"]:
        L.append('- This page lists options: give every option (each H3 in the list) its own line "Best for: <who or '
                 'what situation>". Never call one option best for everyone.')
    if op["cta"]:
        L.append(f'- Link "{op["cta"]["label"]}" to {op["cta"]["url"]} at least {op["cta_min_links"]} times: once in '
                 "the introduction or takeaways, once mid-article, once in the last section before the FAQ.")
    return "\n".join(L)


# ---------------------------------------------------------------- critique

def _paragraphs(body: str) -> list[str]:
    out = []
    for p in re.split(r"\n\s*\n", body):
        t = p.strip()
        if not t or t.startswith(("#", "|", "-", "*", ">", "<", "!", "```", "{")) or re.match(r"^\d+\.\s", t):
            continue
        out.append(t)
    return out


def _first_sentence(text: str) -> str:
    for p in re.split(r"\n\s*\n", text.strip()):
        t = p.strip()
        if t and not t.startswith(("#", "|", "-", "*", ">", "<", "!", "```")) and not re.match(r"^\d+\.\s", t):
            return re.split(r"(?<=[.!?])\s", plain_text(t), maxsplit=1)[0]
    return ""


def takeaways(body: str) -> list[str]:
    _, _, sections = split_sections(body)
    for s in sections:
        if TAKEAWAYS_RE.search(s["h2"].strip()):
            return [plain_text(m).strip() for m in re.findall(r"^\s*[-*]\s+(.+)$", s["text"], flags=re.M)]
    return []


def covered_buckets(body: str) -> set[str]:
    heads = re.findall(r"^#{2,3}\s+(.+)$", body, flags=re.M)
    return {b for b in BUCKETS if any(bucket_re(b).search(h) for h in heads)}


def cta_positions(body: str, url: str) -> list[float]:
    n = max(1, len(body))
    return [m.start() / n for m in re.finditer(r"\]\(" + re.escape(url) + r"\)", body)]


def list_options(body: str) -> list[tuple[str, str]]:
    """(H3, text) pairs under non-FAQ, non-takeaway H2s: the options a list page compares."""
    _, _, sections = split_sections(body)
    out = []
    for s in sections:
        if is_faq_heading(s["h2"]) or TAKEAWAYS_RE.search(s["h2"]):
            continue
        for h, t in re.findall(r"^###\s+(.+?)\n(.*?)(?=^###\s|\Z)", s["text"], flags=re.M | re.S):
            out.append((h.strip(), t))
    return out


def critique(article: dict, brief: dict, fix) -> None:
    op = brief.get("on_page")
    if not op:
        return
    body = article["body"]
    h1, intro, sections = split_sections(body)
    es = lang.is_es()

    if op["key_takeaways"]:
        tk = next((s for s in sections if TAKEAWAYS_RE.search(s["h2"].strip())), None)
        if tk is None:
            fix("key_takeaways_missing", f'Add "## {takeaways_heading()}" right after the introduction: 3-5 bullets, '
                                         "each one sentence of at most 30 words with a fact, number or rule from the article.")
        else:
            items = takeaways(body)
            if not 3 <= len(items) <= 6 or any(word_count(i) > 35 for i in items):
                fix("key_takeaways_shape", f'"{tk["h2"]}" needs 3-5 bullets of at most 30 words each (has {len(items)}).')
            elif sections and sections[0] is not tk:
                fix("key_takeaways_position", f'Move "{tk["h2"]}" to be the first H2, right after the introduction.')

    limit = op["max_paragraph_words"]
    long_ = [p for p in _paragraphs(body) if word_count(plain_text(p)) > limit]
    if long_:
        fix("paragraph_too_long", f"{len(long_)} paragraph(s) run over {limit} words (first starts: "
                                  f'"{" ".join(plain_text(long_[0]).split()[:8])}..."). Split each into short, focused '
                                  "paragraphs of 1-3 sentences.", count=len(long_))

    if op["self_contained"]:
        opener = BACKREF_OPEN_ES if es else BACKREF_OPEN_EN
        bad = [s["h2"] for s in sections if not is_faq_heading(s["h2"]) and not TAKEAWAYS_RE.search(s["h2"])
               and opener.search(normalize_quotes(_first_sentence(s["text"])).strip())]
        if bad:
            fix("section_not_self_contained", "These sections open by leaning on earlier text: "
                + "; ".join(f'"{h}"' for h in bad[:5]) + ". Open each by naming its subject in full, so the "
                "section reads correctly on its own.", sections=bad)
        m = BACKREF_ANY.search(plain_text(body))
        if m:
            fix("back_reference", f'Remove "{m.group(0)}": each section must stand alone. Restate the point instead.')

    have = covered_buckets(body)
    for b in op["fanout"]:
        if b not in have:
            fix("fanout_missing", f'Answer "{bucket_label(b)}" for this topic, as an H2 or as an FAQ question (H3) '
                                  "with a direct, specific answer.", bucket=b, question=bucket_label(b))

    if op["best_for"]:
        missing = [h for h, t in list_options(body) if not BEST_FOR_RE.search(t)]
        if missing:
            lab = "Mejor para" if es else "Best for"
            fix("best_for_missing", f'Give each option a "{lab}: ..." line. Missing under: '
                + "; ".join(f'"{h}"' for h in missing[:6]) + ".", options=missing)

    cta = op.get("cta")
    if cta and op["cta_min_links"]:
        pos = cta_positions(body, cta["url"])
        thirds = {0 if p < 0.34 else 1 if p < 0.67 else 2 for p in pos}
        if len(pos) < op["cta_min_links"] or (op["cta_min_links"] >= 3 and len(thirds) < 3):
            fix("cta_placement", f'Link "{cta["label"]}" to {cta["url"]} at least {op["cta_min_links"]} times, spread over '
                                 f"the top, middle and bottom of the article (found {len(pos)}).", have=len(pos))


# ---------------------------------------------------------------- frontmatter

def reading_minutes(body: str) -> int:
    return max(1, math.ceil(word_count(plain_text(body)) / (200 if lang.is_es() else 230)))


def _person(a: dict) -> dict:
    p = {"@type": "Person", "name": a["name"]}
    if a.get("url"):
        p["url"] = a["url"]
    if a.get("title"):
        p["jobTitle"] = a["title"]
    if a.get("image"):
        p["image"] = a["image"]
    socials = [v for v in (a.get("socials") or {}).values() if v]
    if socials:
        p["sameAs"] = socials
    return p


def frontmatter(fm: dict, body: str, brief: dict, config: dict) -> dict:
    """Extra frontmatter for every page: takeaways, readingTime, author, jsonLd."""
    s = settings(config)
    out = {"readingTime": reading_minutes(body)}
    tk = takeaways(body)
    if tk:
        out["takeaways"] = tk
    author = fm.get("author") or s["author"]
    if author and author.get("name"):
        out["author"] = author
    if not s["json_ld"]:
        return out
    org = s["organization"] or {"name": config["site_name"], "url": f"https://{config['domain']}/"}
    org = {"@type": "Organization", **{k: v for k, v in org.items() if v}}
    try:
        import output
        prefix = output.destination(brief, config)[1]
    except Exception:  # noqa: BLE001 - minimal configs in tests
        prefix = config.get("blog_url_prefix", "/blog")
    url = f"https://{config['domain']}{(prefix or '/blog').rstrip('/')}/{brief['slug']}/"
    pub = fm.get("pubDate")
    page = {"@type": "Article" if brief.get("schema_type") not in ("Review",) else "Review",
            "headline": fm["title"], "description": fm["description"], "url": url,
            "datePublished": str(pub) if pub else None, "dateModified": str(fm.get("updatedDate") or pub) if pub else None,
            "author": _person(author) if author and author.get("name") else org, "publisher": org,
            "inLanguage": config.get("language_code", "en")}
    graph = [{k: v for k, v in page.items() if v is not None}]
    if brief.get("schema_type") == "Service":
        svc = {"@type": "Service", "name": fm["title"], "serviceType": brief["primary_keyword"], "provider": org,
               "url": url}
        if config.get("location"):
            svc["areaServed"] = config["location"].split(",")[0].strip()
        graph.append(svc)
    graph += video_objects(body, fm)
    if fm.get("faq"):
        graph.append({"@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": q["question"], "acceptedAnswer": {"@type": "Answer", "text": q["answer"]}}
            for q in fm["faq"]]})
    out["jsonLd"] = {"@context": "https://schema.org", "@graph": graph}
    return out


YT = re.compile(r"(?:youtube(?:-nocookie)?\.com/(?:embed/|watch\?v=|shorts/)|youtu\.be/|<YouTube[^>]*\bid=[\"'])"
                r"([A-Za-z0-9_-]{11})")


def video_objects(body: str, fm: dict, limit: int = 3) -> list[dict]:
    """VideoObject for each YouTube video embedded or linked in the body (Google shows video results from these).
    uploadDate is the frontmatter `videoUploadDate` when set, else the page's pubDate."""
    out, seen = [], set()
    for m in YT.finditer(body):
        vid = m.group(1)
        if vid in seen:
            continue
        seen.add(vid)
        line = body[body.rfind("\n", 0, m.start()) + 1: body.find("\n", m.end()) if body.find("\n", m.end()) > 0 else None]
        t = re.search(r'title=["\']([^"\']+)', line) or re.search(r"\[([^\]]+)\]\([^)]*" + re.escape(vid), line)
        up = fm.get("videoUploadDate") or fm.get("pubDate")
        out.append({k: v for k, v in {
            "@type": "VideoObject", "name": (t.group(1) if t else fm["title"]), "description": fm["description"],
            "thumbnailUrl": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", "uploadDate": str(up) if up else None,
            "embedUrl": f"https://www.youtube.com/embed/{vid}", "contentUrl": f"https://www.youtube.com/watch?v={vid}"}.items()
            if v})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- template backend helpers

def template_takeaways(brief: dict, body: str) -> str:
    kw = brief["primary_keyword"]
    _, _, sections = split_sections(body)
    heads = [s["h2"] for s in sections if not is_faq_heading(s["h2"]) and not TAKEAWAYS_RE.search(s["h2"])][:4]
    while len(heads) < 3:
        heads.append(kw)
    lines = [f"## {takeaways_heading()}", ""]
    lines += [f"- {h.rstrip('?.')} shapes what {kw} costs you and what you get for it." for h in heads[:4]]
    return "\n".join(lines) + "\n\n"


def template_fanout_answer(b: str, kw: str) -> str:
    return {
        "cost": f"The price of {kw} depends on scope, parts and labour in your area. Get two written quotes that list "
                "each line item, and ask what is excluded, so you compare like with like before you pay a deposit.",
        "timeline": f"Most {kw} jobs are scheduled within days and finished in one visit. Ask for the start date, the "
                    "expected duration and what could delay it, in writing, before you agree.",
        "effort": f"{kw.capitalize()} takes more skill than it looks. Count the tools, the time and the cost of a mistake "
                  "before you decide to handle it yourself.",
        "risks": f"The common mistakes with {kw} are choosing on price alone, skipping the written quote and not asking "
                 "about warranty terms. Each one costs more later than it saves now.",
        "not_for": f"{kw.capitalize()} is not the right call when a cheaper fix solves the problem or when the cost is "
                   "close to replacing the whole thing. Ask for both options and compare them.",
        "next_steps": f"Write down your situation, your budget and your deadline. Then ask two providers of {kw} the same "
                      "questions, compare their written answers and choose the clearer one.",
        "after": f"After {kw}, keep the invoice and warranty, note the date, and check the result again after a few weeks "
                 "so any problem is caught while it is still covered.",
        "compare": f"Compare {kw} with the alternatives on total cost, time and risk. The cheapest option up front is not "
                   "always the cheapest over the years.",
        "who_for": f"{kw.capitalize()} suits people who want the job done once and done right, with a written quote and "
                   "a warranty they can check.",
        "what_is": f"{kw.capitalize()} means getting the problem diagnosed and fixed by someone who does this work every "
                   "day, at a price agreed in advance.",
    }[b]
