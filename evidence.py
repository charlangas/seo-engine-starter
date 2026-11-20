"""First-party evidence ("information gain"): what only this site can say, fed to every page.

  python main.py evidence --config configs/site.yaml      # validate the items, list pages with nothing to draw on

A page that only summarises what already ranks has nothing a reader (or an AI answer) cannot get elsewhere.
Each site keeps hand-written evidence items in `data/<site>/evidence/*.yaml` (one item or a list per file):

    id: smile-studio-case          # optional; defaults to the file name
    type: case_study               # case_study | quote | test | survey | data | screenshot
    title: "Dental clinic in Austin: 9 to 41 booked calls a month"
    summary: "What was done and what happened, in plain facts."
    numbers: ["41 booked calls a month", "5 months", "$3,200 a month"]   # exact figures the writer may state
    quote: "Verbatim words, for type quote."
    who: "Dr. Ana Ruiz, owner of Smile Studio"   # attribution for quotes and case studies
    source_url: https://...        # optional public link
    image: /images/evidence/calls-chart.png   # optional screenshot (path as the site serves it)
    image_alt: "Booked calls per month, Jan-Jun"
    topics: [dentist seo, dental marketing]   # keywords it supports; "*" = any page
    date: 2026-08-01

Every item must carry something checkable: numbers, a quote or an image.

For each brief the best-matching items (by topic) are attached as `brief["evidence"]`. The writer must add a
section such as "How we know this" that uses at least one of them, and the critique enforces it:
an item must actually appear (one of its numbers, its quote or its image), the section must exist, a
screenshot item must be embedded, and the section may not state a figure that is not in the evidence.

Facts that change: each run of `route_facts` (the `evidence` command and the autopilot weekly job) compares
every item's figures with the previous snapshot. When a figure is removed or replaced (or an item lists it under
`supersedes: ["$1,200"]`), every engine page that still quotes the old figure and uses that item (or matches
its topics) is queued for a refresh (`refresh_queue.json`, reason `fact_changed`) with the old and new figures.

Founder questions: for the money keywords with nothing to draw on, `founder_questions` writes 3-5 specific
questions only the owner can answer; the weekly report asks the top ones.

When no item matches a page, `evidence.when_missing` decides: `note` (write it, list it in the run summary),
`draft` (write it with `draft: true`, so a person adds first-party material before it goes live) or `skip`
(do not write it this run). Refreshes never draft or skip a published page.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

import lang
from config import ConfigError, load_config
from textutil import is_faq_heading, jaccard, normalize_quotes, plain_text, split_sections, tokens

TYPES = ("case_study", "quote", "test", "survey", "data", "screenshot")
DEFAULTS = {
    "directory": None,          # default data/<site>/evidence
    "max_items": 3,             # items offered per page
    "when_missing": "note",     # note | draft | skip
    "section_heading": None,    # default "How we know this" / "Cómo lo sabemos"
    "require_screenshots_for": [],  # page types (e.g. [alternatives, vs]) that also need a screenshot item
    "share_prices": True,       # false: pages never state the owner's own prices or fees, and the owner is not asked
    "name_clients": True,       # false: pages never name a client; items describe them by industry and size
}
MATCH_MIN = 0.5

SECTION_RE = re.compile(
    r"(how we (know|tested|test|measured|checked|did)|methodology|our (data|results|numbers|tests?|experience|clients)|"
    r"what we (found|saw|measured|learned)|case stud|from our (work|clients|files|shop)|in our (work|experience|shop)|"
    r"c[oó]mo lo (sabemos|probamos|medimos|comprobamos)|metodolog[ií]a|nuestros (datos|resultados|n[uú]meros|clientes)|"
    r"nuestra experiencia|lo que (encontramos|vimos|medimos)|caso (real|de estudio)|en (nuestro|el) taller)", re.I)


# ---------------------------------------------------------------- settings and items

def settings(config) -> dict:
    raw = config.get("evidence") or {}
    if not isinstance(raw, dict):
        raise ConfigError("evidence must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"evidence: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    if not isinstance(s["max_items"], int) or not 1 <= s["max_items"] <= 10:
        raise ConfigError("evidence.max_items must be a whole number from 1 to 10")
    for k in ("share_prices", "name_clients"):
        if not isinstance(s[k], bool):
            raise ConfigError(f"evidence.{k} must be true or false")
    if s["when_missing"] not in ("note", "draft", "skip"):
        raise ConfigError("evidence.when_missing must be note, draft or skip")
    if not isinstance(s["require_screenshots_for"], list):
        raise ConfigError("evidence.require_screenshots_for must be a list of page types")
    return s


def directory(config) -> Path:
    d = settings(config)["directory"]
    if d:
        p = Path(d).expanduser()
        return p if p.is_absolute() else Path(__file__).resolve().parent / p
    return config["_paths"]["data"] / "evidence"


def _check(item: dict, where: str) -> list[str]:
    errs = []
    if not str(item.get("title") or "").strip():
        errs.append("needs a title")
    if item.get("type") not in TYPES:
        errs.append(f"type must be one of {', '.join(TYPES)}")
    topics = item.get("topics")
    if not isinstance(topics, list) or not topics:
        errs.append('needs a topics list (use ["*"] for any page)')
    if item.get("numbers") is not None and not isinstance(item["numbers"], list):
        errs.append("numbers must be a list")
    if not (item.get("numbers") or item.get("quote") or item.get("image")):
        errs.append("needs something checkable: numbers, a quote or an image")
    if item.get("type") == "quote" and not (item.get("quote") and item.get("who")):
        errs.append("a quote needs quote and who")
    if item.get("type") == "screenshot" and not item.get("image"):
        errs.append("a screenshot needs image")
    return [f"{where}: {e}" for e in errs]


def load(config) -> tuple[list[dict], list[str]]:
    """All valid items, plus one error line per invalid item (invalid items are never offered)."""
    d = directory(config)
    items, errors, ids = [], [], set()
    if not d.is_dir():
        return items, errors
    for f in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            errors.append(f"{f.name}: not valid YAML ({str(e).splitlines()[0]})")
            continue
        rows = data if isinstance(data, list) else [data]
        for i, raw in enumerate(rows):
            where = f.name if len(rows) == 1 else f"{f.name}[{i}]"
            if not isinstance(raw, dict):
                errors.append(f"{where}: must be a mapping")
                continue
            item = {**raw, "id": str(raw.get("id") or (f.stem if len(rows) == 1 else f"{f.stem}-{i + 1}"))}
            bad = _check(item, where)
            if item["id"] in ids:
                bad.append(f"{where}: duplicate id {item['id']}")
            if bad:
                errors += bad
                continue
            ids.add(item["id"])
            item["numbers"] = [str(n) for n in item.get("numbers") or []]
            item["topics"] = [str(t) for t in item["topics"]]
            item["file"] = f.name
            items.append(item)
    return items, errors


# ---------------------------------------------------------------- matching

def _terms(keyword: str, secondary=(), competitors=()) -> list[list[str]]:
    return [tokens(t) for t in [keyword, *secondary, *competitors] if tokens(t)]


def score(item: dict, keyword: str, secondary=(), competitors=()) -> float:
    """How well an item supports a page: 1.0 when every word of a topic is in the keyword, else best overlap."""
    kw = set(tokens(keyword))
    terms = _terms(keyword, secondary, competitors)
    comp = {c for name in competitors for c in tokens(name)}
    best = 0.0
    for t in item["topics"]:
        if t.strip() == "*":
            best = max(best, MATCH_MIN)  # general material: offered only when nothing more specific fits
            continue
        tt = set(tokens(t))
        if not tt:
            continue
        if tt <= kw or (comp and tt <= comp):
            best = max(best, 1.0)
            continue
        best = max(best, *(jaccard(tt, set(x)) * (1.0 if i == 0 else 0.8) for i, x in enumerate(terms)))
    return round(best, 3)


def select(items: list[dict], keyword: str, secondary=(), competitors=(), limit: int = 3) -> list[dict]:
    scored = [(score(it, keyword, secondary, competitors), it) for it in items]
    scored = [(s, it) for s, it in scored if s >= MATCH_MIN]
    scored.sort(key=lambda p: (-p[0], -_date_key(p[1]), p[1]["id"]))
    return [{**it, "match": s} for s, it in scored[:limit]]


def _date_key(item) -> int:
    d = item.get("date")
    try:
        return date.fromisoformat(str(d)[:10]).toordinal() if d else 0
    except ValueError:
        return 0


def heading(config) -> str:
    h = settings(config)["section_heading"]
    return h or ("Cómo lo sabemos" if lang.is_es() else "How we know this")


def plan(config, keyword: str, secondary=(), competitors=(), page_type=None, items=None) -> dict:
    """What a page gets: the matched items and what happens when there are none."""
    s = settings(config)
    if items is None:
        items, _ = load(config)
    chosen = select(items, keyword, secondary, competitors, s["max_items"])
    missing = []
    if not chosen:
        missing.append("no_first_party_evidence")
    if page_type and page_type in s["require_screenshots_for"] and not any(it.get("image") for it in chosen):
        missing.append("no_screenshot")
    return {"items": chosen, "missing": missing, "when_missing": s["when_missing"], "heading": heading(config),
            "available": len(items), "share_prices": s["share_prices"], "name_clients": s["name_clients"],
            "brand": (config.get("comparisons") or {}).get("brand") or config["site_name"]}


def attach(brief: dict, config: dict, refreshing: bool = False) -> dict:
    """Set brief["evidence"] (idempotent). A refresh never drafts a published page."""
    if "evidence" in brief:
        return brief["evidence"]
    comp = (brief.get("comparison") or {}).get("competitors") or []
    page_type = (brief.get("comparison") or {}).get("page_type")
    ev = plan(config, brief["primary_keyword"], brief.get("secondary_keywords", []), comp, page_type)
    ev["draft"] = bool(ev["missing"]) and ev["when_missing"] == "draft" and not refreshing
    brief["evidence"] = ev
    if ev["items"]:
        brief.setdefault("inventory_extra", {})["evidence"] = [it["id"] for it in ev["items"]]
    return ev


# ---------------------------------------------------------------- prompt and critique

def _limits(ev: dict) -> str:
    out = []
    if ev.get("share_prices") is False:
        whose = f"{ev['brand']}'s" if ev.get("brand") else "our"
        out.append(f"Never state {whose} own prices, fees, rates or package costs, or a range a reader "
                   "could take for them. Discuss what drives cost instead; any market figure must be cited to its source.")
    if ev.get("name_clients") is False:
        out.append("Never name a client. Describe them by industry, size and region only.")
    return ("\n" + " ".join(out)) if out else ""


def prompt_block(brief: dict) -> str:
    ev = brief.get("evidence") or {}
    if not ev.get("items"):
        return ("\n\nFirst-party material: none is on file for this topic. Do not claim results, clients, tests or "
                "numbers of our own (no \"our clients\", \"we found\", \"in our tests\"). Earn the page with decision "
                "rules, worked examples and specifics the ranking pages leave out." + _limits(ev))
    L = ["", "", f'First-party evidence (this is what makes the page worth reading; nobody else has it). Add an H2 '
                 f'section titled "{ev["heading"]}" (or a close variant) before the FAQ that uses at least one item, '
                 "and draw on the items in other sections where they fit:"]
    for it in ev["items"]:
        L.append(f'- [{it["type"]}] {it["title"]}')
        if it.get("summary"):
            L.append(f'  What happened: {" ".join(str(it["summary"]).split())}')
        if it["numbers"]:
            L.append(f'  Figures you may state, exactly as written: {"; ".join(it["numbers"])}')
        if it.get("quote"):
            L.append(f'  Quote (verbatim, as a Markdown blockquote, attributed to {it.get("who")}): "{it["quote"]}"')
        elif it.get("who"):
            L.append(f'  Source: {it["who"]}')
        if it.get("image"):
            L.append(f'  Screenshot to embed: ![{it.get("image_alt") or it["title"]}]({it["image"]})')
        if it.get("source_url"):
            L.append(f'  Link: {it["source_url"]}')
    L.append("In that section, state no figure that is not listed above, and do not add results or clients that are "
             "not in these items. When an item comes from another industry, say so plainly and say what carries over.")
    return "\n".join(L) + _limits(ev)


_NUM = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?\s?%?")


def numbers_in(text: str) -> set[str]:
    """Digits of every figure in the text ("$3,200" -> "3200", "41%" -> "41%")."""
    out = set()
    for m in _NUM.findall(normalize_quotes(text)):
        n = m.replace(",", "").replace(" ", "").lstrip("$€£")
        n = n.rstrip(".")
        if n:
            out.add(n)
    return out


def _notable(n: str) -> bool:
    """A figure worth policing: a percentage, or a number of 10 or more that is not a year."""
    if n.endswith("%"):
        return True
    try:
        v = float(n)
    except ValueError:
        return False
    return v >= 10 and not (1900 <= v <= 2100 and v == int(v))


def _item_numbers(it: dict) -> set[str]:
    return numbers_in(" ".join([*it["numbers"], str(it.get("summary") or ""), str(it.get("quote") or ""), it["title"]]))


def _bare(n: str) -> str:
    return n.rstrip("%")


def used(item: dict, body: str) -> bool:
    """The item shows up in the page: one of its listed figures, its quote, or its image."""
    if item.get("image") and item["image"] in body:
        return True
    if item.get("quote"):
        q = tokens(str(item["quote"]), drop_stop=False)[:10]
        if q and " ".join(q) in " ".join(tokens(plain_text(body), drop_stop=False)):
            return True
    text = plain_text(body)
    have = {_bare(n) for n in numbers_in(text)}
    listed = {_bare(n) for n in numbers_in(" ".join(item["numbers"])) if _notable(n)}
    if listed & have:
        return True
    flat = " ".join(normalize_quotes(text).lower().split())
    return any(" ".join(n.lower().split()) in flat for n in item["numbers"])  # small figures count only verbatim


def find_section(body: str, ev: dict) -> dict | None:
    _, _, sections = split_sections(body)
    want = set(tokens(ev.get("heading") or ""))
    for s in sections:
        if is_faq_heading(s["h2"]):
            continue
        if SECTION_RE.search(s["h2"]) or (want and jaccard(want, set(tokens(s["h2"]))) >= 0.6):
            return s
    return None


def is_evidence_heading(h2: str) -> bool:
    return bool(SECTION_RE.search(h2))


def critique(article: dict, brief: dict, fix) -> None:
    ev = brief.get("evidence") or {}
    items = ev.get("items") or []
    if not items:
        return
    body = article["body"]
    if not any(used(it, body) for it in items):
        names = "; ".join(f'"{it["title"]}"' for it in items)
        fix("evidence_missing", f"Use at least one first-party evidence item from the brief ({names}): state one of its "
                                "figures exactly, quote it, or embed its screenshot.")
    sec = find_section(body, ev)
    if sec is None:
        fix("evidence_section_missing", f'Add an H2 section "{ev["heading"]}" before the FAQ built on the first-party '
                                        "evidence items: what was done, what happened, with their exact figures.")
    else:
        allowed = {_bare(n) for it in items for n in _item_numbers(it)}
        extra = sorted(n for n in numbers_in(plain_text(sec["text"])) if _notable(n) and _bare(n) not in allowed)
        if extra:
            fix("evidence_unsupported_number", f'Section "{sec["h2"]}" states {", ".join(extra)}, which is not in the '
                                               "evidence items. Use only the items' own figures there, or cut the number.",
                numbers=extra)
    for it in items:
        if it.get("image") and it["type"] == "screenshot" and it["image"] not in body:
            fix("evidence_image_missing", f'Embed the screenshot ![{it.get("image_alt") or it["title"]}]({it["image"]}) '
                                          f"where it supports the text.", image=it["image"])


def template_section(brief: dict) -> str:
    """Deterministic evidence section for the template backend: item text only, so every figure is supported."""
    ev = brief["evidence"]
    parts = [f'## {ev["heading"]}', ""]
    for it in ev["items"]:
        line = f'**{it["title"]}.**'
        if it.get("summary"):
            line += " " + " ".join(str(it["summary"]).split())
        parts += [line, ""]
        if it["numbers"]:
            parts += [f"- {n}" for n in it["numbers"]] + [""]
        if it.get("quote"):
            parts += [f'> "{it["quote"]}" ({it.get("who")})', ""]
        if it.get("image"):
            parts += [f'![{it.get("image_alt") or it["title"]}]({it["image"]})', ""]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------- facts that change

def _snapshot_file(config) -> Path:
    return config["_paths"]["data"] / "evidence_snapshot.json"


def route_facts(config, items: list[dict] | None = None) -> list[dict]:
    """Queue engine pages that quote a figure an evidence item no longer has. Returns the queued records."""
    import json
    import inventory
    import output
    import refresh
    if items is None:
        items, _ = load(config)
    f = _snapshot_file(config)
    snap = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    current = {it["id"]: {"numbers": it["numbers"], "supersedes": [str(x) for x in it.get("supersedes") or []]}
               for it in items}
    queued = []
    changes = []
    for it in items:
        before = (snap or {}).get(it["id"])
        done_sup = set((before or {}).get("supersedes") or [])
        removed = [n for n in (before or {}).get("numbers", []) if n not in it["numbers"]] if before else []
        removed += [n for n in current[it["id"]]["supersedes"] if n not in done_sup and n not in removed]
        removed = [n for n in removed if numbers_in(n)]
        if removed:
            added = [n for n in it["numbers"] if n not in ((before or {}).get("numbers") or [])]
            changes.append((it, removed, added))
    for it, removed, added in changes:
        old_nums = {_bare(n) for r in removed for n in numbers_in(r)}
        for e in inventory.load(config):
            if e.get("type") != "article" or e.get("status") != "written" or output.is_imported(e):
                continue
            uses = it["id"] in (e.get("evidence") or []) or score(it, e["primary_keyword"]) >= MATCH_MIN
            if not uses:
                continue
            page = output.page_file(config, e)
            if not page.exists():
                continue
            body = page.read_text(encoding="utf-8")
            if not old_nums & {_bare(n) for n in numbers_in(plain_text(body))}:
                continue
            if refresh.enqueue(config, e["slug"], "fact_changed", item=it["id"], old=removed, new=added):
                queued.append({"slug": e["slug"], "item": it["id"], "old": removed, "new": added})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return queued


# ---------------------------------------------------------------- questions for the owner

# With evidence.share_prices false, questions that ask what work cost are swapped for these.
NO_PRICE_Q = {
    "en": {"What did your most recent {kw} job cost the customer, and what drove the price up or down?":
           "How long did your most recent {kw} project take from kickoff to launch, and what slowed it down or sped it up?",
           "Name a customer who left {comp} for you: what were they paying, and what changed after switching?":
           "Which kind of customer left {comp} for you, and what changed after switching?"},
    "es": {"¿Cuánto le costó al cliente tu último trabajo de {kw} y qué subió o bajó el precio?":
           "¿Cuánto tardó tu último proyecto de {kw} de principio a fin, y qué lo frenó o lo aceleró?",
           "¿Qué cliente dejó {comp} por ti? ¿Cuánto pagaba y qué cambió?":
           "¿Qué tipo de cliente dejó {comp} por ti, y qué cambió?"},
}

FOUNDER_Q = {
    "en": {
        "versus": ["Which customers chose you over {comp}, and what tipped the decision?",
                   "Where does {comp} beat you? Name one case where you would send a buyer to them.",
                   "Do you have a screenshot or recording of the part of your product/service that differs most from {comp}?"],
        "alternatives": ["Name a customer who left {comp} for you: what were they paying, and what changed after switching?",
                         "What is the most common complaint you hear about {comp} from prospects?",
                         "Do you have a screenshot or number that shows the difference in practice?"],
        "default": ["What did your most recent {kw} job cost the customer, and what drove the price up or down?",
                    "Which result from a real {kw} project can you share (a number, a before/after, a timeline)?",
                    "What mistake do you see most often with {kw}, and what does it cost people who make it?",
                    "Is there a photo, screenshot or short video from your own work on {kw} we can use?",
                    "What question do customers ask you about {kw} that no website answers well?"],
    },
    "es": {
        "versus": ["¿Qué clientes te eligieron a ti en lugar de {comp}, y qué los decidió?",
                   "¿En qué es mejor {comp}? Di un caso en el que mandarías a un cliente con ellos.",
                   "¿Tienes una foto o captura de lo que más te diferencia de {comp}?"],
        "alternatives": ["¿Qué cliente dejó {comp} por ti? ¿Cuánto pagaba y qué cambió?",
                         "¿Cuál es la queja más común sobre {comp} que escuchas de tus clientes?",
                         "¿Tienes una foto o un número que muestre la diferencia en la práctica?"],
        "default": ["¿Cuánto le costó al cliente tu último trabajo de {kw} y qué subió o bajó el precio?",
                    "¿Qué resultado real de {kw} puedes compartir (un número, un antes y después, un tiempo)?",
                    "¿Qué error ves más seguido con {kw} y cuánto le cuesta a quien lo comete?",
                    "¿Tienes fotos o un video corto de tu propio trabajo de {kw} que podamos usar?",
                    "¿Qué te preguntan tus clientes sobre {kw} que ninguna página responde bien?"],
    },
}


def founder_questions(config, limit_pages: int = 5, per_page: int = 3, r: dict | None = None) -> list[dict]:
    """Specific questions for the owner, for the money keywords with no first-party evidence."""
    import inventory
    r = r or report(config)
    code = "es" if lang.is_es() else "en"
    bank = FOUNDER_Q[code]
    if not settings(config)["share_prices"]:
        bank = {k: [NO_PRICE_Q[code].get(q, q) for q in qs] for k, qs in bank.items()}
    out = []
    for p in [p for p in r["pages"] if "no_first_party_evidence" in p["missing"]][:limit_pages]:
        e = inventory.find(p["keyword"], config) or {}
        pt = e.get("page_type")
        comp = (e.get("competitors") or ["the competitor"])[0]
        qs = bank.get(pt if pt in ("versus", "alternatives") else "default")
        out.append({"keyword": p["keyword"], "funnel": p["funnel"], "leads": p["leads"],
                    "questions": [q.format(kw=p["keyword"], comp=comp) for q in qs[:per_page]]})
    return out


# ---------------------------------------------------------------- gap report

def dated(config, items: list[dict]) -> list[dict]:
    """Items whose text names a year before the content year. Pages never name one, so the writer has to drop
    that part; phrase it as a duration ("over 6 months", "last quarter") so it can be used as written."""
    from textutil import content_year, past_years
    year = content_year(config)
    out = []
    for it in items:
        text = " ".join(str(it.get(k) or "") for k in ("title", "summary", "quote", "who")) + " " + " ".join(it["numbers"])
        if old := past_years(text, year):
            out.append({"id": it["id"], "years": old})
    return out


def report(config) -> dict:
    """Items on file, and which planned or written pages have nothing to draw on (money keywords first)."""
    import forecast
    import inventory
    items, errors = load(config)
    rows = []
    for k in forecast.build(config)["keywords"]:
        if k.get("status") in ("removed",):
            continue
        e = inventory.find(k["keyword"], config) or {}
        comp = e.get("competitors") or k.get("competitors") or []
        p = plan(config, k["keyword"], (), comp, k.get("page_type"), items)
        rows.append({"keyword": k["keyword"], "funnel": k.get("funnel"), "leads": k.get("leads", 0),
                     "status": k.get("status") or "candidate", "items": [it["id"] for it in p["items"]],
                     "missing": p["missing"]})
    order = {"bottom": 0, "middle": 1, "top": 2}
    rows.sort(key=lambda r: (order.get(r["funnel"], 3), -(r["leads"] or 0), r["keyword"]))
    used_ids = {i for r in rows for i in r["items"]}
    return {"dated": dated(config, items), "site": config["site_name"], "generated": date.today().isoformat(), "directory": str(directory(config)),
            "items": [{k: it.get(k) for k in ("id", "type", "title", "topics", "file")} for it in items],
            "errors": errors, "pages": rows, "unused_items": sorted({it["id"] for it in items} - used_ids),
            "when_missing": settings(config)["when_missing"]}


def render(r: dict) -> str:
    gaps = [p for p in r["pages"] if p["missing"]]
    L = [f"# First-party evidence: {r['site']}", "", f"Generated {r['generated']}. Folder: `{r['directory']}`. "
         f"Pages with no evidence: **{r['when_missing']}**.", "",
         f"{len(r['items'])} item(s) on file; {len(gaps)} of {len(r['pages'])} keywords have nothing to draw on.", ""]
    if r["errors"]:
        L += ["## Items that need fixing (never offered to the writer)", ""] + [f"- {e}" for e in r["errors"]] + [""]
    if r.get("dated"):
        L += ["## Items that name an earlier year (pages never do, so the writer drops it)", "",
              "Rephrase these as durations (\"over 6 months\", \"last quarter\") so the figures can be quoted as written.", ""]
        L += [f"- {d['id']}: {', '.join(d['years'])}" for d in r["dated"]] + [""]
    if r["items"]:
        L += ["## On file", "", "| id | type | title | topics |", "|---|---|---|---|"]
        L += [f"| {it['id']} | {it['type']} | {it['title']} | {', '.join(it['topics'])} |" for it in r["items"]] + [""]
    if gaps:
        L += ["## Collect next (bottom of funnel first, by expected leads)", "",
              "| keyword | funnel | leads/mo at #1-3 | status | missing |", "|---|---|---|---|---|"]
        L += [f"| {p['keyword']} | {p['funnel'] or '?'} | {p['leads'] or 0:g} | {p['status']} | {', '.join(p['missing'])} |"
              for p in gaps[:40]] + [""]
        if len(gaps) > 40:
            L += [f"...and {len(gaps) - 40} more.", ""]
    if r["unused_items"]:
        L += ["## Items no keyword matches (check their topics)", ""] + [f"- {i}" for i in r["unused_items"]] + [""]
    if r.get("questions"):
        L += ["## Questions for the owner (answers become evidence items)", ""]
        for q in r["questions"]:
            L += [f"**{q['keyword']}**", ""] + [f"- {x}" for x in q["questions"]] + [""]
    if r.get("routed"):
        L += ["## Pages queued because a figure changed", ""]
        L += [f"- {x['slug']}: {', '.join(x['old'])} → {', '.join(x['new']) or '(removed)'} ({x['item']})" for x in r["routed"]] + [""]
    return "\n".join(L)


def cli(config_path) -> int:
    import json
    config = load_config(config_path, dry_run=False)
    r = report(config)
    r["questions"] = founder_questions(config, r=r)
    r["routed"] = route_facts(config)
    out = config["_paths"]["data"] / "evidence_report"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{r['generated']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    md = render(r)
    (out / f"{r['generated']}.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"Report: {out / (r['generated'] + '.md')}")
    return 1 if r["errors"] else 0
