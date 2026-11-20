"""Internal editor checklist for AI search (81 items: on-page components, unique information, copy, writing
mechanics, freshness, query fan-out). Never shown to readers and not a command.

During the writer/editor loop, `editor_items` gives the editor the items a draft misses that the writer can fix
from the text; the editor turns the ones that matter into fixes. Some checks are proxies (a regex for "pros and
cons", a count of named entities). The score of each written page is kept in the inventory (`ai_score`) and the
run log, for tuning the writer rules.
"""
from __future__ import annotations

import re
from datetime import date

import evidence
import lang
import onpage
from textutil import is_faq_heading, plain_text, split_frontmatter, split_sections, word_count

VAGUE = re.compile(r"(industry[- ]leading|world[- ]class|best[- ]in[- ]class|cutting[- ]edge|unparalleled|second to none|"
                   r"state[- ]of[- ]the[- ]art|#1 (choice|provider|company)|the best (service|company|team|provider) in|"
                   r"unmatched|l[ií]der (en el|del) (mercado|sector)|el mejor servicio|la mejor calidad|insuperable|"
                   r"de primera calidad)", re.I)
QUESTION_H = re.compile(r"(\?\s*$|^(what|how|why|when|which|who|where|is|are|can|should|do|does|will|qu[eé]|c[oó]mo|"
                        r"por qu[eé]|cu[aá]ndo|cu[aá]l|cu[aá]les|qui[eé]n|d[oó]nde|cu[aá]nto|¿))", re.I)
WEAK_OPEN = re.compile(r"^(there (is|are|was|were)|it is|it's|the (pros|benefits|advantages) of|hay |es importante|"
                       r"existen )", re.I)
YEAR = re.compile(r"\b(19|20)\d{2}\b")
MONTHS = re.compile(r"\b(january|february|march|april|may|june|july|august|september|october|november|december|enero|"
                    r"febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b", re.I)
EXAMPLE = re.compile(r"(for example|for instance|e\.g\.|say you|imagine|por ejemplo|supongamos|imagina)", re.I)
PROS_CONS = re.compile(r"\b(pros|cons|advantages|disadvantages|upsides?|downsides?|ventajas|desventajas|pros y contras)\b", re.I)
BETTER_FIT = re.compile(r"(better (fit|choice|pick|option)|best for|choose .{1,40} if|pick .{1,40} if|go with .{1,40} if|"
                        r"mejor opci[oó]n|conviene|elige .{1,40} si|mejor para)", re.I)
ONE_SIZE_CLAIM = re.compile(r"(best for (everyone|anyone|all)|right for everyone|the only (option|choice) you need|"
                            r"el mejor para todos|para cualquiera)", re.I)
DEPENDS = re.compile(r"(it depends|depends on|varies (by|with)|depende|var[ií]a seg[uú]n)", re.I)
CONTEXT_WORDS = re.compile(r"\b(budget|size|small|large|location|city|urgent|urgency|emergency|goal|beginner|advanced|"
                           r"presupuesto|tamaño|ciudad|urgente|urgencia|objetivo|principiante)\b", re.I)
AUDIENCE_H = re.compile(r"\b(for|para)\s+[a-záéíóúñ]+", re.I)
DECISION_H = re.compile(r"(how to choose|how do i choose|decision|checklist|what to ask|questions to ask|step|c[oó]mo elegir|"
                        r"lista|qu[eé] preguntar|pasos)", re.I)
SUMMARY_H = re.compile(r"(conclusion|bottom line|verdict|recommendation|summary|our pick|the short answer|key takeaways|"
                       r"conclusi[oó]n|veredicto|recomendaci[oó]n|resumen|puntos clave)", re.I)
METHOD_RE = re.compile(r"(criteria|how we (chose|picked|tested|ranked|evaluated|compared)|methodology|selection|"
                       r"criterios|c[oó]mo (elegimos|evaluamos|comparamos)|metodolog[ií]a)", re.I)
CONTRARIAN = re.compile(r"((most|many|plenty of) (people|guides|sites|agencies|shops|articles|advice) (say|tell|recommend|claim)|"
                        r"contrary to|the common advice|a common myth|myth|the usual advice|in practice|"
                        r"(la mayor[ií]a|muchos) (dicen|recomiendan)|mito|en la pr[aá]ctica)", re.I)
FRAMEWORK = re.compile(r"\b(?:The |El |La )?(?:[A-Z][\w'-]+ ){1,4}(Framework|Method|Model|Audit|Score|Checklist|Rule|Test|"
                       r"System|Matrix|Index|Método|Modelo|Regla|Prueba|Índice)\b")
TOOL_LINK = re.compile(r"\]\([^)]*(/tools?/|calculator|calculadora|template|plantilla|download|descarga|quiz|estimator)",
                       re.I)
COMPONENT = re.compile(r"^<[A-Z][A-Za-z]+[\s/>]", re.M)
VIDEO = re.compile(r"(youtube\.com/embed|youtube-nocookie|player\.vimeo|<YouTube|<Video)", re.I)
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
QUOTE = re.compile(r"^>\s*[\"“«]", re.M)
ATTRIB = re.compile(r"^>.*[\"”»].*(—|–|-|\()\s*[A-ZÁÉÍÓÚ][\w.]+", re.M)


class Page:
    def __init__(self, md: str, entry: dict | None, config: dict, today: date, ev_items: dict):
        self.fm, self.body = split_frontmatter(md)
        self.entry, self.config, self.today = entry or {}, config, today
        self.h1, self.intro, self.sections = split_sections(self.body)
        self.text = plain_text(self.body)
        self.wc = max(1, word_count(self.text))
        self.h2 = [s["h2"] for s in self.sections]
        self.h3 = re.findall(r"^###\s+(.+)$", self.body, flags=re.M)
        self.paras = [word_count(plain_text(p)) for p in onpage._paragraphs(self.body)]
        self.sents = [x for x in re.split(r"(?<=[.!?])\s+", re.sub(r"^#+ .*$", "", self.text, flags=re.M)) if word_count(x)]
        self.links = re.findall(r"\]\(([^)\s]+)", self.body)
        self.external = [l for l in self.links if l.startswith("http") and self.config["domain"] not in l]
        self.internal = [l for l in self.links if l.startswith("/") or self.config["domain"] in l]
        self.images = IMAGE.findall(self.body)
        self.local_images = [i for i in self.images if not i.startswith("http") or self.config["domain"] in i]
        self.faq = next((s for s in self.sections if is_faq_heading(s["h2"])), None)
        self.faq_answers = re.findall(r"^###\s+.+?\n+(.*?)(?=^###\s|\Z)", self.faq["text"], flags=re.M | re.S) if self.faq else []
        self.faq_qs = re.findall(r"^###\s+(.+)$", self.faq["text"], flags=re.M) if self.faq else []
        self.buckets = onpage.covered_buckets(self.body)
        self.kw = str(self.fm.get("primaryKeyword") or self.entry.get("primary_keyword") or "")
        self.ev = [ev_items[i] for i in self.entry.get("evidence", []) if i in ev_items]
        self.ev_section = evidence.find_section(self.body, {"heading": evidence.heading(config)})
        self.author = self.fm.get("author") if isinstance(self.fm.get("author"), dict) else None
        self.major = [s for s in self.sections if not is_faq_heading(s["h2"]) and not onpage.TAKEAWAYS_RE.search(s["h2"])]

    def date(self, key):
        v = self.fm.get(key)
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v)[:10]) if v else None
        except ValueError:
            return None

    def last_updated(self):
        return self.date("updatedDate") or self.date("pubDate")

    def age(self):
        d = self.last_updated()
        return (self.today - d).days if d else None

    def first_words(self, n=150):
        return " ".join(plain_text(re.sub(r"^#\s.*$", "", self.body, count=1, flags=re.M)).split()[:n])

    def entities_per_100(self):
        n = 0
        for s in self.sents:
            words = s.split()[1:]
            n += sum(1 for w in words if re.match(r"^[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+|^[A-Z]{2,}", w))
        return n * 100 / self.wc


def _has_bucket(p, *bs):
    return any(b in p.buckets for b in bs)


def _faq_rich(p, rx):
    if not p.faq_answers:
        return False
    return sum(1 for a in p.faq_answers if rx.search(a)) >= max(1, len(p.faq_answers) // 2)


# (group, item, check(page) -> bool, proxy?)  -- 81 items, in the checklist's order
CHECKS = [
    # 01 Page components (15)
    ("Page components", "Comparison tables", lambda p: bool(re.search(r"^\|.+\|\s*\n\|[\s:|-]+\|", p.body, re.M)), False),
    ("Page components", "Key takeaways", lambda p: len(onpage.takeaways(p.body)) >= 3, False),
    ("Page components", "FAQ section", lambda p: bool(p.faq) and len(p.faq_qs) >= 3, False),
    ("Page components", "FAQ: specific numbers", lambda p: _faq_rich(p, re.compile(r"\d")), False),
    ("Page components", "FAQ: dates", lambda p: any(YEAR.search(a) or MONTHS.search(a) for a in p.faq_answers), False),
    ("Page components", "FAQ: facts", lambda p: _faq_rich(p, re.compile(r"(\d|\]\(http|\b[A-Z][a-z]+ [A-Z][a-z]+)")), True),
    ("Page components", "No vague claims that could read as biased", lambda p: not VAGUE.search(p.text), True),
    ("Page components", "Short, easily digestible paragraphs", lambda p: bool(p.paras) and max(p.paras) <= 90 and sum(p.paras) / len(p.paras) <= 60, False),
    ("Page components", "Customer reviews (direct quotes)", lambda p: bool(QUOTE.search(p.body)) or any(i.get("quote") for i in p.ev), False),
    ("Page components", "Bullet points", lambda p: bool(re.search(r"^\s*[-*]\s+\S", p.body, re.M)), False),
    ("Page components", "Screenshots and/or unique images", lambda p: bool(p.local_images), False),
    ("Page components", "An actual, first-hand review", lambda p: p.ev_section is not None, True),
    ("Page components", "Multimedia", lambda p: bool(p.images or VIDEO.search(p.body) or COMPONENT.search(p.body)), False),
    ("Page components", "Links to social profiles", lambda p: bool(p.author and any((p.author.get("socials") or {}).values())), False),
    ("Page components", "Author profiles", lambda p: bool(p.author and p.author.get("name") and (p.author.get("url") or p.author.get("bio"))), False),
    # 02 Unique information (10)
    ("Unique information", "Screenshots of the products actually being used",
     lambda p: any(i.get("image") for i in p.ev) or bool(p.ev_section and IMAGE.search(p.ev_section["text"])), True),
    ("Unique information", "First-party / proprietary data", lambda p: bool(p.ev_section and re.search(r"\d", p.ev_section["text"])), False),
    ("Unique information", "Customer testimonials", lambda p: bool(ATTRIB.search(p.body)), True),
    ("Unique information", "Video", lambda p: bool(VIDEO.search(p.body) or p.fm.get("videos")), False),
    ("Unique information", "Original research and surveys",
     lambda p: any(i["type"] in ("survey", "test", "data") for i in p.ev)
     or bool(re.search(r"(we (surveyed|tested|analy[sz]ed|measured)|our (survey|study|test)|encuestamos|medimos|probamos)", p.text, re.I)), True),
    ("Unique information", "Named expert interviews", lambda p: bool(ATTRIB.search(p.body)) and bool(re.search(r"(interview|said|told us|entrevista|nos dijo)", p.text, re.I)), True),
    ("Unique information", "First-hand experience and case-specific teardowns",
     lambda p: any(i["type"] == "case_study" for i in p.ev) or any(re.search(r"(case study|teardown|caso real|caso de estudio)", h, re.I) for h in p.h2), True),
    ("Unique information", "Contrarian, evidence-backed positions", lambda p: bool(CONTRARIAN.search(p.text)), True),
    ("Unique information", "Interactive tools and standalone resources", lambda p: bool(COMPONENT.search(p.body) or TOOL_LINK.search(p.body)), False),
    ("Unique information", "Named, proprietary frameworks (coined)", lambda p: bool(FRAMEWORK.search(p.text)), True),
    # 03 Copy (7)
    ("How the copy reads", "Question-based headers", lambda p: bool(p.major) and sum(1 for s in p.major if QUESTION_H.search(s["h2"].strip())) / len(p.major) >= 0.3, False),
    ("How the copy reads", "Bottom-line-up-front",
     lambda p: bool(p.kw) and onpage.contains_kw(p.first_words(150), p.kw) and word_count(plain_text(p.intro)) <= 150, True),
    ("How the copy reads", "Extractable passages",
     lambda p: bool(p.major) and sum(1 for s in p.major if 6 <= word_count(onpage._first_sentence(s["text"])) <= 35) / len(p.major) >= 0.7, True),
    ("How the copy reads", "Content broken into short, focused paragraphs", lambda p: bool(p.paras) and sum(p.paras) / len(p.paras) <= 60, False),
    ("How the copy reads", "Citable facts with sources", lambda p: len(set(p.external)) >= 2, False),
    ("How the copy reads", "Every H2 is a self-contained island",
     lambda p: not onpage.BACKREF_ANY.search(p.text) and not any(
         (onpage.BACKREF_OPEN_ES if lang.is_es() else onpage.BACKREF_OPEN_EN).search(onpage._first_sentence(s["text"]).strip())
         for s in p.major), False),
    ("How the copy reads", "Entity-rich writing", lambda p: p.entities_per_100() >= 1.0, True),
    # 04 Writing mechanics (5)
    ("Writing mechanics", "Every sentence is short, simple, and easy to understand",
     lambda p: bool(p.sents) and sum(word_count(x) for x in p.sents) / len(p.sents) <= (24 if lang.is_es() else 20), False),
    ("Writing mechanics", "Content uses semantic triples", lambda p: bool(p.sents) and sum(1 for x in p.sents if WEAK_OPEN.search(x.strip())) / len(p.sents) <= 0.05, True),
    ("Writing mechanics", "Images and infographics are present", lambda p: bool(p.images), False),
    ("Writing mechanics", "Free tools and interactive components are present", lambda p: bool(COMPONENT.search(p.body) or TOOL_LINK.search(p.body)), False),
    ("Writing mechanics", "An embedded YouTube video is present", lambda p: bool(re.search(r"(youtube\.com/embed|youtube-nocookie|<YouTube)", p.body, re.I)), False),
    # 05 Freshness (4)
    ("Recency check", "Updated within the last 30 days", lambda p: p.age() is not None and p.age() <= 30, False),
    ("Recency check", "Information refreshed", lambda p: p.age() is not None and p.age() <= 30, False),
    ("Recency check", "Examples refreshed", lambda p: bool(p.fm.get("changeNote")) and (p.age() or 99) <= 30 or (p.age() is not None and p.age() <= 30 and not p.fm.get("updatedDate")), True),
    ("Recency check", "Current data in place", lambda p: str(p.today.year) in p.text or (p.date("lastVerified") and (p.today - p.date("lastVerified")).days <= 90), True),
    # 06 Query fan-out: A core intent (5)
    ("Fan-out A: core intent", "Direct answer within the first 100-150 words",
     lambda p: bool(p.kw) and onpage.contains_kw(p.first_words(150), p.kw) and bool(re.search(r"\d", p.first_words(150))), True),
    ("Fan-out A: core intent", "Obvious who the page is for",
     lambda p: _has_bucket(p, "who_for") or bool(re.search(r"\b(if you|for (homeowners|owners|teams|businesses|anyone|people)|si (tienes|eres)|para (quienes|dueños))", p.first_words(200), re.I)), True),
    ("Fan-out A: core intent", "Core topic defined in plain language",
     lambda p: _has_bucket(p, "what_is") or bool(re.search(r"\b(is (a|an|the)|means|refers to|es (un|una|el|la)|significa)\b", p.first_words(200), re.I)), True),
    ("Fan-out A: core intent", "The main qualifier (best / top / recommended) is explained",
     lambda p: not re.search(r"\b(best|top|recommended|mejor(es)?|recomendad)", p.kw, re.I) or bool(METHOD_RE.search(p.text)), True),
    ("Fan-out A: core intent", "Includes a clear summary, recommendation, or conclusion", lambda p: any(SUMMARY_H.search(h) for h in p.h2), False),
    # B related topics (3)
    ("Fan-out B: related topics", "Links to or briefly covers adjacent topics", lambda p: len(set(p.internal)) >= 2, False),
    ("Fan-out B: related topics", "Answers what else to know before deciding", lambda p: _has_bucket(p, "next_steps", "not_for"), False),
    ("Fan-out B: related topics", "Includes relevant definitions, examples, or context", lambda p: bool(EXAMPLE.search(p.text)), True),
    # C implicit questions (6)
    ("Fan-out C: implicit questions", "Explains cost, pricing, or budget", lambda p: _has_bucket(p, "cost"), False),
    ("Fan-out C: implicit questions", "Explains timeline, setup, onboarding, or delivery time", lambda p: _has_bucket(p, "timeline"), False),
    ("Fan-out C: implicit questions", "Explains difficulty, effort, or resources required", lambda p: _has_bucket(p, "effort"), False),
    ("Fan-out C: implicit questions", "Explains risks, limitations, or common mistakes", lambda p: _has_bucket(p, "risks"), False),
    ("Fan-out C: implicit questions", "Explains what happens after someone takes action", lambda p: _has_bucket(p, "after"), False),
    ("Fan-out C: implicit questions", "Answers what to know before choosing", lambda p: _has_bucket(p, "next_steps", "not_for"), False),
    # D comparative (6)
    ("Fan-out D: comparative", "Compares the main option against relevant alternatives", lambda p: _has_bucket(p, "compare") or bool(p.fm.get("competitors")), False),
    ("Fan-out D: comparative", "Includes a comparison table or clearly structured comparison", lambda p: bool(re.search(r"^\|.+\|\s*\n\|[\s:|-]+\|", p.body, re.M)), False),
    ("Fan-out D: comparative", "Explains the pros and cons of each option", lambda p: bool(PROS_CONS.search(p.text)), True),
    ("Fan-out D: comparative", "Names competitors, substitutes, or alternative approaches", lambda p: bool(p.fm.get("competitors")) or _has_bucket(p, "compare"), True),
    ("Fan-out D: comparative", "Explains when each option is the better fit", lambda p: bool(BETTER_FIT.search(p.text)), True),
    ("Fan-out D: comparative", "Avoids claiming one option is best for everyone", lambda p: not ONE_SIZE_CLAIM.search(p.text), True),
    # E recency (4)
    ("Fan-out E: recency", "Visible last updated date", lambda p: bool(p.fm.get("updatedDate") or p.fm.get("pubDate")), False),
    ("Fan-out E: recency", "References the current year where relevant", lambda p: str(p.today.year) in p.text or str(p.today.year) in str(p.fm.get("title", "")), False),
    ("Fan-out E: recency", "Rankings, pricing, features, and availability kept current",
     lambda p: (p.date("lastVerified") and (p.today - p.date("lastVerified")).days <= 90) or (not re.search(r"[$€]\s?\d", p.body) and (p.age() or 999) <= 90), True),
    ("Fan-out E: recency", "Explains what changed since the last update", lambda p: bool(p.fm.get("changeNote")), False),
    # F contextual variation (5)
    ("Fan-out F: contextual variation", "Explains who the recommendation is best for", lambda p: _has_bucket(p, "who_for") or bool(onpage.BEST_FOR_RE.search(p.text)), False),
    ("Fan-out F: contextual variation", "Includes use-case sections for different audiences", lambda p: sum(1 for h in p.h2 + p.h3 if AUDIENCE_H.search(h)) >= 2, True),
    ("Fan-out F: contextual variation", "Explains how the answer changes by budget, size, location, urgency, or goal",
     lambda p: len({m.lower() for m in CONTEXT_WORDS.findall(p.text)}) >= 3, True),
    ("Fan-out F: contextual variation", "Uses best-for labels where relevant", lambda p: bool(onpage.BEST_FOR_RE.search(p.text)), False),
    ("Fan-out F: contextual variation", "Avoids a one-size-fits-all answer", lambda p: bool(DEPENDS.search(p.text)) and not ONE_SIZE_CLAIM.search(p.text), True),
    # G next steps (6)
    ("Fan-out G: next steps", "Explains what the reader should do after reading", lambda p: _has_bucket(p, "next_steps"), False),
    ("Fan-out G: next steps", "Includes a clear next step or decision framework",
     lambda p: any(DECISION_H.search(h) for h in p.h2 + p.h3) or bool(re.search(r"^\d+\.\s", p.body, re.M)), True),
    ("Fan-out G: next steps", "Answers the follow-up questions that come after the main query", lambda p: len(p.faq_qs) >= 4, True),
    ("Fan-out G: next steps", "Includes FAQs that cover post-decision questions",
     lambda p: any(onpage.bucket_re(b).search(q) for q in p.faq_qs for b in ("after", "timeline", "next_steps")), False),
    ("Fan-out G: next steps", "Links to deeper resources for users who need more", lambda p: len(set(p.internal)) >= 2 and len(set(p.external)) >= 1, False),
    ("Fan-out G: next steps", "Reduces the need to run another search", lambda p: len(p.buckets) >= 6, True),
    # H evidence + trust (5)
    ("Fan-out H: evidence and trust", "Cites reputable sources where factual claims are made", lambda p: len(set(p.external)) >= 2, True),
    ("Fan-out H: evidence and trust", "Includes original experience and examples", lambda p: p.ev_section is not None, False),
    ("Fan-out H: evidence and trust", "Includes case studies, screenshots, data, or quotes", lambda p: bool(p.ev) or bool(QUOTE.search(p.body)) or bool(p.local_images), False),
    ("Fan-out H: evidence and trust", "Explains how the recommendations were chosen", lambda p: bool(METHOD_RE.search(p.text)) or p.ev_section is not None, True),
    ("Fan-out H: evidence and trust", "Shows author, reviewer, or company expertise", lambda p: bool(p.author and (p.author.get("title") or p.author.get("bio"))), False),
]
assert len(CHECKS) == 81, len(CHECKS)


def score_page(md: str, config: dict, entry: dict | None = None, today: date | None = None, ev_items=None) -> dict:
    if ev_items is None:
        ev_items = {i["id"]: i for i in evidence.load(config)[0]}
    p = Page(md, entry, config, today or date.today(), ev_items)
    rows = []
    for group, label, fn, proxy in CHECKS:
        try:
            ok = bool(fn(p))
        except Exception:  # noqa: BLE001 - one bad check never sinks the audit
            ok = False
        rows.append({"group": group, "item": label, "ok": ok, "proxy": proxy})
    n = sum(r["ok"] for r in rows)
    groups = {}
    for r in rows:
        g = groups.setdefault(r["group"], [0, 0])
        g[0] += r["ok"]
        g[1] += 1
    return {"score": round(100 * n / len(rows)), "passed": n, "total": len(rows),
            "groups": {k: f"{a}/{b}" for k, (a, b) in groups.items()},
            "missing": [f"{r['group']}: {r['item']}" + (" (proxy)" if r["proxy"] else "") for r in rows if not r["ok"]],
            "rows": rows}


# Items the writer can fix from the text alone, with the instruction the editor passes on. Items that need a
# person (author profile, screenshots, video, original research, testimonials, dates) are left to the evidence
# rules, the refresh loop and the layout, so the editor never asks the writer to invent them.
EDITOR_HINTS = {
    "Comparison tables": "Where the page weighs options, prices or approaches, put the comparison in a Markdown table.",
    "FAQ: specific numbers": "Give the FAQ answers specific numbers (prices, times, quantities) the article supports.",
    "No vague claims that could read as biased": "Replace vague promotional claims with specific, supportable statements.",
    "Short, easily digestible paragraphs": "Break long paragraphs into short, focused ones.",
    "Bullet points": "Turn at least one list of steps, options or criteria into bullet points.",
    "Question-based headers": "Phrase more H2s as the question a reader would type, answered in the section's first sentence.",
    "Bottom-line-up-front": "Answer the search in the first two sentences of the introduction.",
    "Extractable passages": "Open each section with one sentence that fully answers its heading on its own.",
    "Entity-rich writing": "Name the specific brands, products, places, people and standards instead of general terms.",
    "Every sentence is short, simple, and easy to understand": "Shorten the longest sentences.",
    "Content uses semantic triples": "Rewrite 'There are...' / 'It is...' sentences as subject, verb, object.",
    "Contrarian, evidence-backed positions": "Where the evidence supports it, state plainly where common advice is wrong and why.",
    "Named, proprietary frameworks (coined)": "If the article teaches a method, give it a short name readers can remember, "
                                               "using the brand name, e.g. \"The <Brand> <X> Check\".",
    "Direct answer within the first 100-150 words": "Put a specific number, range or recommendation in the first 150 words.",
    "Obvious who the page is for": "Say in the introduction who this page is for.",
    "Core topic defined in plain language": "Define the core topic in one plain sentence early on.",
    "The main qualifier (best / top / recommended) is explained": "Explain what 'best' means here: the criteria used.",
    "Includes a clear summary, recommendation, or conclusion": "End the body with a short verdict or recommendation section.",
    "Answers what else to know before deciding": "Add what the reader should know before deciding.",
    "Includes relevant definitions, examples, or context": "Add a concrete worked example.",
    "Explains difficulty, effort, or resources required": "Say how much effort, skill or resources this takes.",
    "Explains what happens after someone takes action": "Explain what happens after the reader acts (next days, follow-up, maintenance).",
    "Explains the pros and cons of each option": "Give the pros and cons of each option.",
    "Explains when each option is the better fit": "Say when each option is the better fit.",
    "Avoids claiming one option is best for everyone": "Remove any claim that one option is best for everyone.",
    "References the current year where relevant": "Mention the current year where prices, rules or availability are time-sensitive.",
    "Includes use-case sections for different audiences": "Add short sections for two or more different reader situations.",
    "Explains how the answer changes by budget, size, location, urgency, or goal": "Explain how the answer changes with budget, size, location or urgency.",
    "Uses best-for labels where relevant": "Add 'Best for' labels to the options.",
    "Avoids a one-size-fits-all answer": "Say what the answer depends on rather than giving one answer for everyone.",
    "Includes a clear next step or decision framework": "Give a short decision framework or numbered next steps.",
    "Answers the follow-up questions that come after the main query": "Add FAQ questions a reader asks after deciding.",
    "Includes FAQs that cover post-decision questions": "Add an FAQ question about what happens after choosing or booking.",
    "Explains how the recommendations were chosen": "Say how the recommendations were chosen.",
}


def editor_items(article: dict, brief: dict, config: dict, limit: int = 8) -> list[str]:
    """Internal, editor-only: checklist items this draft misses that the writer can fix from the text."""
    from textutil import build_frontmatter
    fm = {"title": article.get("title") or "", "primaryKeyword": brief["primary_keyword"]}
    if (brief.get("comparison") or {}).get("competitors"):
        fm["competitors"] = brief["comparison"]["competitors"]
    md = build_frontmatter(fm) + "\n" + article["body"]
    entry = {"evidence": [it["id"] for it in (brief.get("evidence") or {}).get("items", [])]}
    r = score_page(md, config, entry)
    out = []
    for row in r["rows"]:
        if not row["ok"] and row["item"] in EDITOR_HINTS and EDITOR_HINTS[row["item"]] not in out:
            out.append(EDITOR_HINTS[row["item"]])
    return out[:limit]
