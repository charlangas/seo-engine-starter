"""Competitor comparison pages: alternatives, reviews, versus pages and "best" lists.

Opt-in per site with a `comparisons:` block (see README "Competitor comparison pages"). The ideas:

- Ride a bigger competitor's brand searches. Start with "<competitor> alternatives", then
  "<competitor> review", then "<competitor A> vs <competitor B>" (titled "A vs B vs <brand>"), then
  "best <category>" and "best <category> for <use case>". "<brand> vs <competitor>" pages are cluster
  support: once a competitor's alternatives page exists they are written even without search volume.
- Low keyword difficulty is not enough: the domains already in the top 3 must not out-rank this site by
  more than `max_rank_gap` (DataForSEO backlinks rank, 0-100).
- Ground each page in why people leave the competitor (Reddit threads) and in its real pricing page.
- Pages in one competitor's cluster link to each other, say when facts were checked, disclose the bias
  when the brand is in the comparison, compare specifics in a table and carry a CTA that fits the intent.
- Companions per page: a LinkedIn rewrite (not a duplicate), founder-interview questions for a YouTube
  video, and a media checklist (screenshots, embeds, tracking).

This module owns Stage 1 for comparison keywords and the comparison parts of Stages 2-4.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import date

from bs4 import BeautifulSoup

import inventory
import lang
from apiclient import ApiError, dataforseo_post, fetch_page, serpapi_get
from config import get_runtime
from textutil import normalize_keyword, slugify, smart_cap, tokens, word_count

PAGE_TYPES = ["alternatives", "review", "versus", "own_versus", "best", "switch", "export", "use_case"]
TYPE_WEIGHT = {"alternatives": 2.0, "review": 1.6, "versus": 1.3, "own_versus": 1.0, "best": 1.0,
               "switch": 1.4, "export": 1.1, "use_case": 1.5}
SCHEMA = {"review": "Review", "alternatives": "ItemList", "best": "ItemList", "switch": "HowTo", "export": "HowTo",
          "use_case": "Service"}
NO_TABLE_TYPES = ("export", "use_case")   # how-to and landing pages: no company table required
MONTHS_ES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
             "noviembre", "diciembre"]
PRICE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)")
LABS = "dataforseo_labs/google/"

DEFAULTS = {
    "enabled": False,
    "brand": None,                    # your product or company name
    "brand_summary": "",              # one or two sentences: what it is, who it is for
    "brand_facts": [],                # facts the writer may state about you (prices, plans, terms)
    "category": None,                 # e.g. "project management tool"
    "use_cases": [],                  # e.g. ["agencies", "nonprofits"]: "best <category> for <use case>"
    "competitors": [],                # [{name, domain, priority (1-3), pain_points: [], pricing_url}]
    "page_types": ["alternatives", "review", "versus", "own_versus", "best", "switch", "use_case"],  # also: export
    "max_per_run": 1,                 # comparison pieces per run, on top of max_keywords_per_run
    "min_complaint_mentions": 3,      # a Reddit complaint theme must come up in this many threads (last 12 months)
    "volume_floor": None,             # null = the site's volume_floor
    "kd_max": None,                   # null = the site's kd_max
    "max_rank_gap": 25,               # reject when the top-3 median domain rank exceeds ours by more
    "own_domain_rank": None,          # null = look it up (DataForSEO backlinks/bulk_ranks)
    "authority_check_limit": None,    # SERPs checked per run; null = 4 x max_per_run
    "reddit_research": True,          # one SerpAPI call per competitor on the page
    "find_videos": True,              # one SerpAPI YouTube search per page
    "cta": None,                      # {label, url} or per page type {alternatives: {label, url}, ...}
    "cta_min_links": 2,
    "author": None,                   # {name, title, url, image, socials: {linkedin, x}} for the byline
    "linkedin_page_types": ["best", "alternatives"],
    "linkedin_max_overlap": 0.2,      # max share of shared 5-word sequences with the site article
    "draft": False,                   # true = write comparison pages with `draft: true` for a human review
    "brand_relation": None,           # how the brand relates to the site, e.g. "the brand we distribute"; null = "our own product"
    "favor_brand": False,             # true = the brand is the recommended pick on every page (bias still disclosed)
    "bias_note": None,                # the disclosure sentence the writer must use; null = "We make <brand>, so we are biased..."
}


# ---------- config ----------

def settings(config) -> dict | None:
    raw = config.get("comparisons")
    if not raw or not raw.get("enabled"):
        return None
    s = {**DEFAULTS, **raw}
    s["volume_floor"] = config["volume_floor"] if s["volume_floor"] is None else s["volume_floor"]
    s["kd_max"] = config["kd_max"] if s["kd_max"] is None else s["kd_max"]
    s["competitors"] = [{"priority": 1, "pain_points": [], **c} for c in s["competitors"]]
    for c in s["competitors"]:
        c["slug"] = slugify(c["name"])
    if s["authority_check_limit"] is None:
        s["authority_check_limit"] = max(3, 4 * s["max_per_run"])
    return s


def validate(raw: dict, where: str) -> list[str]:
    """Problems with a `comparisons:` block (empty list = fine)."""
    if not isinstance(raw, dict):
        return [f"{where}: comparisons must be a mapping"]
    if not raw.get("enabled"):
        return []
    errs = []
    for f in ("brand", "category"):
        if not raw.get(f):
            errs.append(f"{where}: comparisons.{f} is required when comparisons.enabled is true")
    comps = raw.get("competitors") or []
    if not comps or not all(isinstance(c, dict) and c.get("name") for c in comps):
        errs.append(f"{where}: comparisons.competitors must be a list of {{name, domain}} entries")
    bad = [t for t in raw.get("page_types") or [] if t not in PAGE_TYPES]
    if bad:
        errs.append(f"{where}: comparisons.page_types has unknown value(s) {bad}; use {PAGE_TYPES}")
    return errs


def cta_for(s: dict, page_type: str) -> dict | None:
    cta = s.get("cta")
    if isinstance(cta, dict) and page_type in cta:
        cta = cta[page_type]
    return cta if isinstance(cta, dict) and cta.get("url") else None


# ---------- Stage 1: candidates ----------

def candidates(s: dict, entries: list[dict] | None = None) -> list[dict]:
    """Every comparison keyword this config implies, with page type and the companies on the page."""
    out = []
    brand, cat, types = s["brand"], s["category"], s["page_types"]
    comps = s["competitors"]

    def add(kw, page_type, names, use_case=None):
        out.append({"keyword": kw.lower(), "page_type": page_type, "competitors": names, "use_case": use_case})

    es = lang.is_es()
    alt_t, alt_uc_t, rev_t = (("alternativas a {c}", "alternativas a {c} para {uc}", "{c} opiniones") if es else
                              ("{c} alternatives", "{c} alternatives for {uc}", "{c} review"))
    best_t, best_uc_t = ("mejores {cat}", "mejores {cat} para {uc}") if es else ("best {cat}", "best {cat} for {uc}")
    for c in comps:
        if "alternatives" in types:
            add(alt_t.format(c=c["name"]), "alternatives", [c["name"]])
            for uc in s["use_cases"]:
                add(alt_uc_t.format(c=c["name"], uc=uc), "alternatives", [c["name"]], uc)
        if "review" in types:
            add(rev_t.format(c=c["name"]), "review", [c["name"]])
        if "own_versus" in types:
            add(f'{brand} vs {c["name"]}', "own_versus", [c["name"]])
    if "versus" in types:
        for i, a in enumerate(comps):
            for b in comps[i + 1:]:
                add(f'{a["name"]} vs {b["name"]}', "versus", [a["name"], b["name"]])
                add(f'{b["name"]} vs {a["name"]}', "versus", [b["name"], a["name"]])
    sw_t, mig_t, ex_t, ex2_t = (("cómo cambiar de {c}", "migrar de {c} a {b}", "exportar datos de {c}", "cómo exportar de {c}")
                                if es else ("how to switch from {c}", "migrate from {c} to {b}", "export data from {c}",
                                            "how to export from {c}"))
    for c in comps:
        if "switch" in types:
            add(sw_t.format(c=c["name"]), "switch", [c["name"]])
            add(mig_t.format(c=c["name"], b=brand), "switch", [c["name"]])
        if "export" in types:
            add(ex_t.format(c=c["name"]), "export", [c["name"]])
            add(ex2_t.format(c=c["name"]), "export", [c["name"]])
    if "use_case" in types:
        for uc in s["use_cases"]:
            add((f"{cat} para {uc}" if es else f"{cat} for {uc}"), "use_case", [], uc)
    if "best" in types:
        add(best_t.format(cat=cat), "best", [c["name"] for c in comps])
        for uc in s["use_cases"]:
            add(best_uc_t.format(cat=cat, uc=uc), "best", [c["name"] for c in comps], uc)
    seen, uniq = set(), []
    for c in out:
        if c["keyword"] not in seen:
            seen.add(c["keyword"])
            uniq.append(c)
    return uniq


def _metrics(config, keywords):
    """Volume and difficulty, shared with keyword research through the same per-keyword cache."""
    import dfscache
    scope = f'{config["country"]}|{config["language_code"]}'

    def fetch_overview(ks):
        out = {}
        for i in range(0, len(ks), 700):
            res = dataforseo_post(config, LABS + "keyword_overview/live", {
                "keywords": ks[i:i + 700], "location_name": config["country"], "language_code": config["language_code"]})
            for r in res:
                for it in r.get("items") or []:
                    info = it.get("keyword_info") or {}
                    out[it["keyword"]] = {"volume": info.get("search_volume"), "cpc": info.get("cpc"),
                                          "monthly_searches": info.get("monthly_searches")}
        return out

    def fetch_kd(ks):
        out = {}
        res = dataforseo_post(config, LABS + "bulk_keyword_difficulty/live", {
            "keywords": ks, "location_name": config["country"], "language_code": config["language_code"]})
        for r in res:
            for it in r.get("items") or []:
                out[it["keyword"]] = it.get("keyword_difficulty")
        return out

    vols = {k: (v or {}).get("volume") for k, v in dfscache.per_key(config, "overview", scope, keywords, fetch_overview).items()}
    have = [k for k in keywords if vols.get(k)]
    kd = dict(dfscache.per_key(config, "difficulty", scope, have, fetch_kd)) if have else {}
    if config.get("kd_zero_policy") == "assume":  # same rule as main research: 0 means unknown, not easy
        kd = {k: (float(config.get("kd_assumed", 30)) if v == 0 else v) for k, v in kd.items()}
    return vols, kd


def domain_ranks(config, domains: list[str]) -> dict:
    """Domain authority 0-100 (DataForSEO backlinks rank). Raises ApiError when the API is unavailable."""
    domains = sorted({re.sub(r"^www\.", "", d.lower()) for d in domains if d})
    if not domains:
        return {}
    import dfscache

    def fetch(ds):
        res = dataforseo_post(config, "backlinks/bulk_ranks/live", {"targets": ds[:1000], "rank_scale": "one_hundred"})
        return {re.sub(r"^www\.", "", (it.get("target") or "").lower()): it.get("rank")
                for r in res for it in (r.get("items") or [])}
    return dfscache.per_key(config, "domain_rank", "global", domains, fetch)


def authority_check(config, s: dict, items: list[dict]) -> list[dict]:
    """Annotate items with the SERP's domain strength; return the ones this site can realistically win."""
    import serp
    rt = get_runtime(config)
    own = config["domain"]
    serps = {}
    for it in items:
        try:
            serps[it["keyword"]] = serp.fetch_serp(it["keyword"], config, expect_terms=tuple(it.get("competitors") or ()))
        except ApiError as e:
            rt.log("stage_error", stage="comparisons", step="authority_serp", keyword=it["keyword"], error=str(e))
    top = {k: [r["domain"] for r in v["organic"] if r.get("resolved", True) and r["domain"] != own][:3]
           for k, v in serps.items()}
    try:
        ranks = domain_ranks(config, [d for ds in top.values() for d in ds] + ([own] if s["own_domain_rank"] is None else []))
    except ApiError as e:
        rt.log("filter_decision", step="comparisons_authority", note="backlinks rank unavailable; keyword difficulty only",
               error=str(e)[:200])
        for it in items:
            it["authority"] = {"checked": False}
        return items
    own_rank = s["own_domain_rank"] if s["own_domain_rank"] is not None else (ranks.get(own) or 0)
    kept = []
    for it in items:
        doms = top.get(it["keyword"])
        rs = [ranks[d] for d in doms or [] if ranks.get(d) is not None]
        if not rs:
            it["authority"] = {"checked": False}
            kept.append(it)
            continue
        med = statistics.median(rs)
        it["authority"] = {"checked": True, "own_rank": own_rank, "top3_median_rank": med,
                           "top3": [{"domain": d, "rank": ranks.get(d)} for d in doms]}
        if med - own_rank > s["max_rank_gap"]:
            it["rejected"] = {"reason": "serp_authority_gap", "own_rank": own_rank, "top3_median_rank": med,
                              "max_rank_gap": s["max_rank_gap"]}
        else:
            kept.append(it)
    return kept


def get_opportunities(config, taken: list[str] | None = None) -> list[dict]:
    """Comparison Stage 1. Writes comparison_keywords.json; returns up to max_per_run items."""
    s = settings(config)
    if not s:
        return []
    rt = get_runtime(config)
    rt.log("stage", stage="comparisons_research", site=config["site_name"])
    entries = inventory.load(config)
    written = {normalize_keyword(e["primary_keyword"]): e for e in entries
               if e.get("status") == "written" and not inventory.held(config, e)}
    blocked = {normalize_keyword(e["primary_keyword"]) for e in entries
               if e.get("status") in ("written", "failed") or e.get("type") == "excluded"}
    blocked |= {normalize_keyword(k) for k in taken or []}
    prio = {c["name"].lower(): c["priority"] for c in s["competitors"]}

    cands = [c for c in candidates(s) if normalize_keyword(c["keyword"]) not in blocked]
    vols, kd = _metrics(config, [c["keyword"] for c in cands]) if cands else ({}, {})
    rejected, scored = [], []
    for c in cands:
        kw = c["keyword"]
        vol, d = vols.get(kw), kd.get(kw)
        c.update(volume=vol or 0, kd=d, intent="commercial")
        weight = TYPE_WEIGHT[c["page_type"]] * max([prio.get(n.lower(), 1) for n in c["competitors"]] or [1])
        if c["page_type"] == "own_versus":
            alt = written.get(normalize_keyword(
                (f'alternativas a {c["competitors"][0]}' if lang.is_es() else f'{c["competitors"][0]} alternatives')))
            if not alt and (vol or 0) < s["volume_floor"]:
                rejected.append({**c, "reason": "own_versus_waits_for_alternatives_page"})
                continue
            if alt:  # cluster support: no volume needed, ranks just behind its alternatives page
                c["cluster_support"] = True
                c["score"] = round(0.9 * (alt.get("score") or 1), 2)
                scored.append(c)
                continue
        if vol is None:
            rejected.append({**c, "reason": "no_volume_data"})
        elif vol < s["volume_floor"]:
            rejected.append({**c, "reason": "volume_below_floor", "floor": s["volume_floor"]})
        elif d is None:
            rejected.append({**c, "reason": "no_kd_data"})
        elif d > s["kd_max"]:
            rejected.append({**c, "reason": "kd_above_max", "kd_max": s["kd_max"]})
        else:
            c["score"] = round(vol * weight / (d + 1), 2)
            scored.append(c)
    scored.sort(key=lambda x: (-x["score"], PAGE_TYPES.index(x["page_type"]), x["keyword"]))

    # One page per (type, companies, use case): "a vs b" and "b vs a" are the same page.
    seen, uniq = set(), []
    for c in scored:
        key = (c["page_type"], frozenset(n.lower() for n in c["competitors"]), c.get("use_case"))
        if key in seen:
            rejected.append({**c, "reason": "same_page_as_higher_scored_keyword"})
            continue
        seen.add(key)
        uniq.append(c)

    try:
        import gsc
        owners = [o for o in gsc.owners_for_research(config) if o["impressions"] >= config["gsc"]["min_impressions"]]
    except Exception:  # noqa: BLE001 - the guard is optional
        owners = []
    pool = []
    for c in uniq:
        owner = next((o for o in owners if normalize_keyword(o["query"]) == normalize_keyword(c["keyword"])), None)
        if owner:
            rejected.append({**c, "reason": "existing_page_ranks_for_keyword", "page": owner["page"]})
        else:
            pool.append(c)

    head = [c for c in pool if not c.get("cluster_support")][: s["authority_check_limit"]]
    checked = {id(c) for c in head}
    kept = authority_check(config, s, head) if head else []
    for c in head:
        if c.get("rejected"):
            why = c.pop("rejected")
            rejected.append({**c, **why})
    kept_ids = {id(c) for c in kept}
    ranked = [c for c in pool if id(c) in kept_ids or (c.get("cluster_support") and id(c) not in checked)]
    ranked.sort(key=lambda x: (-x["score"], PAGE_TYPES.index(x["page_type"])))
    selected = ranked[: s["max_per_run"]]
    for c in ranked:
        c["selected"] = c in selected
    for r in rejected:
        rt.log("rejection", stage="comparisons", keyword=r["keyword"], reason=r["reason"])

    work = config["_paths"]["work"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "comparison_keywords.json").write_text(json.dumps({"ranked": ranked, "rejected": rejected}, indent=2,
                                                              ensure_ascii=False, default=str), encoding="utf-8")
    rt.log("stage", stage="comparisons_research_done", candidates=len(cands), passed=len(ranked),
           selected=len(selected), rejected=len(rejected))
    return selected


# ---------- Stage 2: research for the brief ----------

def reddit_pain_points(config, competitor: str) -> list[dict]:
    """Reddit threads where people discuss leaving the competitor (titles + snippets, linked)."""
    q = (f"site:reddit.com {competitor} problemas OR opiniones OR cambiar" if lang.is_es() else
         f"site:reddit.com {competitor} alternative OR switch OR frustrated")
    params = {"engine": "google", "q": q, "tbs": "qdr:y",   # threads from the last 12 months only
              "hl": config["language_code"], "num": 10}
    try:
        data = serpapi_get(config, params)
    except ApiError as e:
        get_runtime(config).log("stage_error", stage="comparisons", step="reddit", competitor=competitor, error=str(e))
        return []
    return [{"competitor": competitor, "title": r.get("title", ""), "snippet": r.get("snippet", ""), "url": r["link"]}
            for r in (data.get("organic_results") or []) if "reddit.com" in (r.get("link") or "")][:10]


COMPLAINT_THEMES = {
    "price": r"\b(price|pricing|expensive|cost(s|ly)?|overpriced|price (hike|increase)|caro|precio|cobran?)\b",
    "support": r"\b(support|customer service|no (reply|response)|ticket|soporte|atenci[oó]n)\b",
    "reliability": r"\b(bugs?|buggy|slow|crash(es|ing)?|outage|down|lag|glitch|falla|lento|se cae)\b",
    "contract": r"\b(contract|cancel(lation)?|lock-?in|auto-?renew|refund|contrato|cancelar|reembolso)\b",
    "missing features": r"\b(missing|lacks?|no (way|option) to|can'?t (even )?|limited|limitad[oa]|no tiene|no permite)\b",
    "complexity": r"\b(complicated|complex|confusing|learning curve|clunky|bloated|complicad[oa]|confus[oa])\b",
    "data and export": r"\b(export|migrat\w+|import|data loss|exportar|migrar)\b",
}


def complaint_gate(threads: list[dict], min_mentions: int = 3) -> tuple[list[dict], list[dict]]:
    """Keep only threads whose complaint theme shows up in `min_mentions`+ threads for that competitor.
    Returns (kept threads, [{competitor, theme, threads}]). One-off gripes are not evidence of a pattern."""
    counts: dict[tuple, list] = {}
    for t in threads:
        text = f'{t.get("title", "")} {t.get("snippet", "")}'
        for theme, rx in COMPLAINT_THEMES.items():
            if re.search(rx, text, flags=re.I):
                counts.setdefault((t["competitor"], theme), []).append(t)
    strong = {k: v for k, v in counts.items() if len(v) >= min_mentions}
    keep, seen = [], set()
    for (_, theme), ts in strong.items():
        for t in ts:
            if t["url"] not in seen:
                seen.add(t["url"])
                keep.append({**t, "theme": theme})
    themes = [{"competitor": c, "theme": th, "threads": len(v)} for (c, th), v in sorted(strong.items(), key=lambda kv: -len(kv[1]))]
    return keep, themes


def youtube_videos(config, query: str) -> list[dict]:
    try:
        data = serpapi_get(config, {"engine": "youtube", "search_query": query, "hl": config["language_code"]})
    except ApiError as e:
        get_runtime(config).log("stage_error", stage="comparisons", step="youtube", query=query, error=str(e))
        return []
    return [{"title": v.get("title", ""), "url": v["link"], "channel": (v.get("channel") or {}).get("name", "")}
            for v in (data.get("video_results") or []) if v.get("link")][:3]


def page_facts(config, url: str, max_words: int = 900) -> str:
    """Visible text of a pricing or product page, price lines first."""
    html = fetch_page(config, url)
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe"]):
        t.decompose()
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in soup.get_text("\n").split("\n")]
    lines = [ln for ln in lines if 2 <= len(ln.split()) <= 120]
    pricey = [ln for ln in lines if PRICE.search(ln) or re.search(r"\b(per (month|user|seat)|/mo|free plan|trial|annual)\b", ln, re.I)]
    rest = [ln for ln in lines if ln not in pricey]
    out, n = [], 0
    for ln in pricey + rest:
        if n + word_count(ln) > max_words:
            break
        out.append(ln)
        n += word_count(ln)
    return "\n".join(out)


def competitor_facts(config, s: dict, names: list[str]) -> list[dict]:
    by = {c["name"].lower(): c for c in s["competitors"]}
    out = []
    for n in names:
        c = by.get(n.lower())
        if not c or not c.get("domain"):
            continue
        dom = re.sub(r"^https?://", "", c["domain"]).strip("/")
        for url in [c.get("pricing_url"), f"https://{dom}/pricing", f"https://{dom}/"]:
            if not url:
                continue
            text = page_facts(config, url)
            if word_count(text) >= 40:
                out.append({"competitor": c["name"], "url": url, "checked": date.today().isoformat(), "excerpt": text})
                break
    return out


def _section_specs_es(item: dict, s: dict) -> list[tuple[str, str]]:
    brand, comps = s["brand"], item["competitors"]
    C = comps[0] if comps else ""
    uc = f' para {item["use_case"]}' if item.get("use_case") else ""
    t = item["page_type"]
    if t == "alternatives":
        return [(f"Por qué la gente busca alternativas a {C}{uc}", "Motivos reales, solo si están en los hechos o notas de la ficha."),
                (f"Alternativas a {C} lado a lado", "Una tabla Markdown: para qué auto o uso sirve, diferencias clave, qué confirmar."),
                (f"Cómo elegir la alternativa correcta a {C}", "Reglas de decisión por tipo de auto, uso y presupuesto.")]
    if t == "review":
        return [(f"Qué hace bien {C}", "Fortalezas concretas, solo con hechos de la ficha."),
                (f"Dónde se queda corto {C}", "Limitaciones planteadas con justicia."),
                (f"Quién debería elegir otra marca en lugar de {C}", "Para quién no es y qué revisar.")]
    if t == "versus":
        A, B = comps[0], comps[1]
        return [(f"{A} vs {B} de un vistazo", "Una tabla Markdown: origen, líneas, aplicaciones y para qué sirve cada una."),
                (f"Cuándo conviene {A} y cuándo {B}", "Situaciones concretas, no adjetivos."),
                (f"Una tercera opción: {brand}", f"Para quién encaja {brand} mejor que las otras dos, con el interés del sitio dicho claramente.")]
    if t == "own_versus":
        return [(f"{brand} vs {C}: la respuesta corta", "Dos o tres frases útiles para decidir."),
                (f"{brand} y {C} lado a lado", "Una tabla Markdown: origen, líneas, aplicaciones, qué confirmar al comprar."),
                (f"Cuándo conviene {C}", f"Sé honesto sobre dónde {C} gana o es suficiente."),
                (f"Cómo confirmar la pieza correcta", "Qué datos llevar al mostrador, sin importar la marca.")]
    cat = s["category"]
    if t == "switch":
        return [(f"Por qué la gente deja {C}", "Motivos reales, solo si están en los hechos o notas de la ficha."),
                (f"Qué cambia al pasar de {C} a {brand}", "Una tabla Markdown: lo que se mantiene, lo que cambia, qué confirmar."),
                (f"Cómo cambiar de {C} paso a paso", "Pasos numerados, en orden, con lo que necesitas en cada uno."),
                (f"Cuándo conviene quedarse con {C}", "Sé honesto sobre cuándo no vale la pena cambiar.")]
    if t == "export":
        return [(f"Qué puedes exportar de {C}", "Qué datos salen, en qué formato, y qué no sale."),
                (f"Cómo exportar de {C} paso a paso", "Pasos numerados con los nombres reales de menús y botones, si están en los hechos."),
                (f"Qué hacer con los datos exportados", f"Cómo revisarlos y cargarlos en otra herramienta, incluida {brand}.")]
    if t == "use_case":
        u = item.get("use_case") or ""
        return [(f"Qué necesitan los {u} de {cat}", "Los problemas concretos de este público, no genéricos."),
                (f"Cómo funciona {cat} para {u}", "El proceso, paso a paso, con lo que recibe el cliente."),
                (f"Cuánto cuesta {cat} para {u}", "Rangos o factores de precio; precios exactos solo si están en los hechos."),
                (f"Cómo elegir un proveedor de {cat} para {u}", "Preguntas a hacer y señales de alerta.")]
    return [(f"Cómo comparamos las {cat}", "Los criterios, antes de las conclusiones."),
            (f"Las {cat} comparadas", "Una tabla Markdown con todas las opciones de la página."),
            (f"Qué opción conviene según tu auto y uso", "Reglas de decisión por tipo de auto y uso.")]


def _section_specs(item: dict, s: dict) -> list[tuple[str, str]]:
    if lang.is_es():
        return _section_specs_es(item, s)
    brand, comps = s["brand"], item["competitors"]
    C = comps[0] if comps else ""
    uc = f' for {item["use_case"]}' if item.get("use_case") else ""
    t = item["page_type"]
    if t == "alternatives":
        return [(f"Why people look for {C} alternatives{uc}", "Real reasons people leave, from the voice-of-customer threads."),
                (f"{C} alternatives compared side by side", "A Markdown table: price, cheapest plan, best for, contract, key difference."),
                (f"How to choose the right {C} alternative", "Decision rules by team type, budget and must-have feature.")]
    if t == "review":
        return [(f"What {C} does well", "Specific strengths, with what you see inside the product."),
                (f"Where {C} falls short", "Complaints from the voice-of-customer threads, fairly stated."),
                (f"{C} pricing and plans", "Only prices from the fetched pricing page, with the date checked."),
                (f"Who should pick something other than {C}", "Who it is wrong for, and what they should look at instead.")]
    if t == "versus":
        A, B = comps[0], comps[1]
        return [(f"{A} vs {B} at a glance", "A Markdown table comparing price, plans, features and best fit."),
                (f"{A} vs {B} pricing", "Only prices from the fetched pricing pages, with the date checked."),
                (f"When {A} is the better pick, and when {B} is", "Concrete situations, not adjectives."),
                (f"A third option: {brand}", f"Who {brand} fits better than either, with the bias stated plainly.")]
    if t == "own_versus":
        return [(f"{brand} vs {C}: the short answer", "Two or three sentences a buyer can act on."),
                (f"{brand} and {C} side by side", "A Markdown table: price, plans, features, setup, support, contract."),
                (f"When {C} is the better choice", f"Be honest about where {C} wins."),
                (f"Switching from {C} to {brand}", "What moves over, what does not, how long it takes.")]
    cat = s["category"]
    if t == "switch":
        return [(f"Why teams leave {C}", "Real reasons people leave, from the voice-of-customer threads."),
                (f"What changes when you move from {C} to {brand}", "A Markdown table: what carries over, what changes, what to check."),
                (f"How to switch from {C}, step by step", "Numbered steps in order, with what you need at each one."),
                (f"When to stay with {C}", "Be honest about when switching is not worth it.")]
    if t == "export":
        return [(f"What you can export from {C}", "Which data comes out, in which format, and what does not."),
                (f"How to export from {C}, step by step", "Numbered steps using the real menu and button names, if the facts show them."),
                (f"What to do with your {C} export", f"How to check the file and load it into another tool, including {brand}.")]
    if t == "use_case":
        u = item.get("use_case") or ""
        return [(f"What {u} need from {cat}", "This audience's specific problems, not generic ones."),
                (f"How {cat} works for {u}", "The process step by step, with what the client gets."),
                (f"What {cat} for {u} costs", "Price ranges or price drivers; exact prices only from the facts."),
                (f"How to choose a {cat} provider for {u}", "Questions to ask and red flags.")]
    return [(f"How we judged each {cat}", "The criteria, stated before the picks."),
            (f"{smart_cap(cat)} options compared", "A Markdown table of every option on the page."),
            (f"Which {cat} fits your situation", "Decision rules by team type, budget and use case.")]


def _cluster_links(config, item: dict, own_slug: str) -> list[dict]:
    """Written comparison pages that share a competitor with this one."""
    names = {n.lower() for n in item["competitors"]}
    blog = config["blog_url_prefix"].rstrip("/")
    out = []
    for e in inventory.load(config):
        if e.get("status") != "written" or not e.get("page_type") or e["slug"] == own_slug \
                or inventory.held(config, e):
            continue
        if names & {n.lower() for n in e.get("competitors") or []}:
            out.append({"url": f'{(e.get("url_prefix") or blog).rstrip("/")}/{e["slug"]}/',
                        "anchor": e.get("title") or e["primary_keyword"], "type": "article", "relevance": 99,
                        "rating": 0, "cluster": e["page_type"]})
    # "Want to go deeper?": the brand-vs-competitor page first, then the rest of the cluster.
    order = {"own_versus": 0, "alternatives": 1, "review": 2, "versus": 3, "best": 4}
    return sorted(out, key=lambda x: (order.get(x["cluster"], 9), x["url"]))[:6]


def augment_brief(brief: dict, item: dict, config: dict) -> dict:
    """Add the comparison research and required sections to a Stage 2 brief, then re-save it."""
    s = settings(config)
    rt = get_runtime(config)
    t = item["page_type"]
    names = item["competitors"] if t != "best" else item["competitors"][:6]
    focus = names[:2] if t in ("versus",) else names[:1] if t != "best" else []

    voice = []
    if s["reddit_research"]:
        for n in focus or names[:2]:
            voice += reddit_pain_points(config, n)
    voice, themes = complaint_gate(voice, s.get("min_complaint_mentions", 3))
    known = [{"competitor": c["name"], "pain_point": p} for c in s["competitors"]
             if c["name"] in names for p in c.get("pain_points") or []]
    facts = competitor_facts(config, s, names)
    videos = youtube_videos(config, f"{focus[0] if focus else s['category']} {'review' if t != 'best' else ''}".strip()) \
        if s["find_videos"] else []

    title_kw = brief["primary_keyword"]
    if t == "versus":
        title_kw = f"{names[0]} vs {names[1]} vs {s['brand']}"
    checked = date.today()
    es = lang.is_es()
    label = f"{MONTHS_ES[checked.month - 1]} de {checked.year}" if es else checked.strftime("%B %Y")
    relation = s["brand_relation"] or ("nuestra propia marca" if es else "our own product")
    bias_note = s["bias_note"] or (
        f"Somos distribuidor de {s['brand']}, así que tenemos un interés; tómalo en cuenta." if es else
        f"We make {s['brand']}, so we are biased; weigh that accordingly.")
    brief["comparison"] = {
        "page_type": t, "brand": s["brand"], "brand_summary": s["brand_summary"], "brand_facts": s["brand_facts"],
        "category": s["category"], "competitors": names, "use_case": item.get("use_case"),
        "title_keyword": title_kw, "checked": checked.isoformat(), "checked_label": label,
        "checked_line": (f"Datos verificados en {label}." if es else f"Pricing and features checked {label}."),
        "brand_relation": relation, "bias_note": bias_note, "es": es,
        "voice_of_customer": voice, "complaint_themes": themes, "known_pain_points": known, "competitor_facts": facts,
        "videos": videos,
        "cta": cta_for(s, t), "cta_min_links": s["cta_min_links"], "author": s["author"], "draft": bool(s["draft"]),
        "favor_brand": bool(s["favor_brand"]), "brand_on_page": t != "review" or bool(s["favor_brand"]), "authority": item.get("authority"),
        "cluster": sorted(slugify(n) for n in names),
    }
    brief["schema_type"] = SCHEMA.get(t, "Article")

    # Required sections go before the FAQ and are checked like gap sections.
    faq_at = next((i for i, o in enumerate(brief["outline"]) if o.get("faq")), len(brief["outline"]))
    have = [set(tokens(o["h2"])) for o in brief["outline"]]
    for h2, why in _section_specs(item, s):
        ht = set(tokens(h2))
        if any(len(ht & h) / max(1, len(ht)) >= 0.8 for h in have):
            continue
        brief["outline"].insert(faq_at, {"h2": h2, "h3": [], "source": f"comparison:{t}", "gap": True})
        faq_at += 1
        brief["gaps_to_address"].append({"type": "comparison_section", "topic": h2, "section": h2, "reason": why})

    cluster = _cluster_links(config, item, brief["slug"])
    known_urls = {x["url"] for x in brief["internal_link_targets"]}
    brief["internal_link_targets"] = [c for c in cluster if c["url"] not in known_urls] + brief["internal_link_targets"]
    brief["inventory_extra"] = {"page_type": t, "competitors": names, "comparison_cluster": brief["comparison"]["cluster"],
                                "last_verified": checked.isoformat()}

    out = config["_paths"]["briefs"]
    (out / f"brief_{brief['slug']}.json").write_text(json.dumps(brief, indent=2, ensure_ascii=False, default=str),
                                                     encoding="utf-8")
    rt.log("stage", stage="comparison_brief_done", keyword=brief["primary_keyword"], page_type=t,
           reddit_threads=len(voice), facts_pages=len(facts), videos=len(videos), cluster_links=len(cluster))
    return brief


# ---------- Stage 3: writer guidance and critique ----------

TYPE_GUIDE = {
    "alternatives": "An alternatives page for people who know {c} but have decided it is not a fit. List the "
                    "alternatives as H3 entries; put {brand} first and say plainly that it is {rel}. For each: who it "
                    "is for, what it costs, the one thing it does better than {c}, and the catch.",
    "review": "An honest, Wirecutter-style review of {c}. Show what it is like to use, what it costs and who it is "
              "wrong for. It is not a sales page for {brand}; mention {brand} at most once, with the bias stated.",
    "versus": "A head-to-head of {c}, written for someone deciding between them. The title includes all three names: "
              "\"{title_kw}\". Compare the first two in depth, then introduce {brand} as a third option.",
    "own_versus": "A focused {brand} vs {c} comparison for buyers already considering both. Go deeper than an "
                  "alternatives list: pricing, features, setup, support, contracts, and when {c} is the better choice.",
    "best": "A \"best {category}\" guide. State the evaluation criteria first, then the picks as H3 entries with who "
            "each is for. Include {brand} and say it is {rel}.",
    "switch": "A switching guide for people leaving {c}. Why people switch, what changes, the steps in order, and when "
              "staying on {c} is the better call. {brand} is {rel}; present it as the destination, with the bias stated.",
    "export": "A how-to for getting data out of {c}. Exact steps, formats and limits, then what to do with the file. "
              "Mention {brand} once as one place to load it, with the bias stated.",
    "use_case": "A landing page for {category} aimed at one audience. Speak to that audience's situation, show the "
                "process, price drivers and proof, and end with how to choose a provider. {brand} is {rel}.",
}


FAVOR_GUIDE = {
    "alternatives": "List it as the first alternative and the one most readers should try first.",
    "review": "Keep the review of the competitor fair, then close with {brand} as the alternative to consider, with the bias stated.",
    "versus": "After comparing the first two, present {brand} as the option to choose for readers it fits.",
    "own_versus": "Lead with where {brand} is the stronger choice; still give the competitor its own section on when it fits better.",
    "best": "Make {brand} the first pick.",
    "switch": "Make the case for moving to {brand} for the readers it fits.",
    "export": "Recommend {brand} as the place to load the export.",
    "use_case": "Present {brand} as the provider built for this audience.",
}


def prompt_block(brief: dict) -> str:
    """Extra writer/editor instructions for a comparison brief ('' for normal briefs)."""
    c = brief.get("comparison")
    if not c:
        return ""
    names = ", ".join(c["competitors"])
    lines = ["", "COMPARISON PAGE (these instructions override any general rule against naming or comparing companies, "
                 "for the companies listed here only):",
             "- " + TYPE_GUIDE[c["page_type"]].format(c=" vs ".join(c["competitors"][:2]) if c["page_type"] == "versus"
                                                     else c["competitors"][0] if c["competitors"] else c["category"],
                                                     brand=c["brand"], title_kw=c["title_keyword"], category=c["category"],
                                                     rel=c.get("brand_relation", "our own product")),
             f'- Companies on this page: {names}' + (f'; {c.get("brand_relation", "our own product")}: {c["brand"]}' if c["brand_on_page"] else "") + ".",
             f'- {c["brand"]}: {c["brand_summary"] or "(no summary given)"}',
             "- Compare with specifics, not adjectives: prices, what the free or cheapest plan includes, plan limits, "
             "contract terms, setup time, turnaround, integrations. Put them in at least one Markdown table whose rows "
             "or columns name the companies.",
             "- Prices: state only dollar amounts that appear in the facts below. Otherwise send the reader to the "
             "vendor's pricing page.",
             f'- Near the top, one line: "{c.get("checked_line") or "Pricing and features checked " + c["checked_label"] + "."}" '
             "Keep that wording.",
             "- Be fair. Name what each competitor does better and who should choose it.",
             "- Verdict first: the two or three sentences right after the H1 give the answer (who should pick which), "
             "including where a competitor is the better choice. Details follow.",
             "- When a product is priced per seat or per user, show what it costs at 1, 5 and 25 seats (a small table), "
             "using only prices from the facts below."]
    if c["brand_on_page"]:
        lines.append(f'- {c["brand"]} is {c.get("brand_relation", "our own product")}. Say so plainly where it first '
                     f'appears and again in the verdict, e.g. "{c.get("bias_note") or "We make " + c["brand"] + ", so we are biased; weigh that accordingly."}"')
    if c.get("favor_brand"):
        lines += [f'- Favor {c["brand"]}: it is the recommended pick on this page. ' + FAVOR_GUIDE[c["page_type"]].format(brand=c["brand"]),
                  f'- Put {c["brand"]} first in every comparison table (first row or first column) and first in any list of options.',
                  f'- Make the verdict recommend {c["brand"]} for the readers it fits, and say why using only the facts given for it. '
                  "Never invent results, clients, prices, team size or capabilities to make the case.",
                  "- Keep naming what each competitor genuinely does well and who it suits. Honest trade-offs make the "
                  "recommendation credible; a page that only praises its owner reads as an ad."]
    if c.get("cta"):
        lines.append(f'- Link "{c["cta"]["label"]}" to {c["cta"]["url"]} {c["cta_min_links"]} to 3 times in body '
                     f"paragraphs, where a reader ready to act would want it. Never in headings or the FAQ.")
    if c["brand_facts"]:
        lines += [f"- Facts about {c['brand']} you may state:"] + [f"  - {f}" for f in c["brand_facts"]]
    for f in c["competitor_facts"]:
        lines += [f'- Facts from {f["competitor"]}\'s own page ({f["url"]}, checked {f["checked"]}):',
                  "  " + f["excerpt"][:3000].replace("\n", "\n  ")]
    if c["voice_of_customer"] or c["known_pain_points"]:
        lines.append("- Why people leave (use as evidence, paraphrase, link a thread when you quote it). Each Reddit theme "
                     "below came up in 3 or more threads in the last 12 months; do not present any other complaint as common:")
        lines += [f'  - {t["competitor"]}: {t["theme"]} ({t["threads"]} threads)' for t in c.get("complaint_themes") or []]
        lines += [f'  - {p["competitor"]}: {p["pain_point"]}' for p in c["known_pain_points"]]
        lines += [f'  - "{v["title"]}": {v["snippet"]} ({v["url"]})' for v in c["voice_of_customer"]]
    return "\n".join(lines)


def _price_set(text: str) -> set:
    return {p.replace(",", "") for p in PRICE.findall(text)}


def critique(article: dict, brief: dict, fix) -> None:
    """Comparison-only rules; `fix(code, message, **kw)` records a required fix."""
    c = brief.get("comparison")
    if not c:
        return
    body = article["body"]
    low = body.lower()
    for n in c["competitors"]:
        if n.lower() not in low:
            fix("comparison_competitor_missing", f'Cover {n} by name; it is one of the companies this page compares.')
    tables = re.findall(r"((?:^\|.*\|\s*$\n?){3,})", body, flags=re.M)
    if c.get("page_type") not in NO_TABLE_TYPES and not any(sum(1 for n in c["competitors"] + [c["brand"]] if n.lower() in t.lower()) >= 2 for t in tables):
        fix("comparison_table", "Add a Markdown comparison table whose rows or columns name at least two of the "
                                f'companies ({", ".join(c["competitors"] + [c["brand"]])}) and compare specifics.')
    month = c["checked_label"].split()[0]
    fresh_re = (r"\b(verificad|revisad|actualizad|comprobad)[oa]s?\b[^.\n]{0,30}\b" + month + r"\b") if c.get("es") else \
        (r"\b(checked|verified|updated|accurate)\b[^.\n]{0,25}\b" + month + r"\b")
    if not re.search(fresh_re, body, flags=re.I):
        fix("comparison_freshness", f'Add the line "{c.get("checked_line") or "Pricing and features checked " + c["checked_label"] + "."}" near the top.')
    if c["brand_on_page"] or c["brand"].lower() in low:
        if c["brand"].lower() not in low and c["brand_on_page"]:
            fix("comparison_brand_missing", f'Include {c["brand"]} as an option and say it is {c.get("brand_relation", "ours")}.')
        elif not re.search(r"\b(sesg\w*|parcial\w*|inter[eé]s|tenlo en cuenta|tómalo en cuenta|tomalo en cuenta)" if c.get("es") else
                           r"\b(biased|our bias|bias|weigh (that|this|it)|discount (that|this|it|our))", low):
            fix("comparison_bias_disclosure", f'Say plainly that {c["brand"]} is {c.get("brand_relation", "your product")} and '
                                              f'the page has an interest ("{c.get("bias_note") or "We make " + c["brand"] + ", so weigh that accordingly"}").')
    if c.get("favor_brand"):
        _favor_checks(body, c, fix)
    _verdict_and_seats(body, c, fix)
    if c.get("cta"):
        n = len(re.findall(r"\]\(" + re.escape(c["cta"]["url"]) + r"\)", body))
        if n < c["cta_min_links"]:
            fix("comparison_cta", f'Link "{c["cta"]["label"]}" to {c["cta"]["url"]} at least {c["cta_min_links"]} times '
                                  f"in body paragraphs (found {n}).", have=n)
    allowed = set()
    for f in c["competitor_facts"]:
        allowed |= _price_set(f["excerpt"])
    for f in c["brand_facts"]:
        allowed |= _price_set(str(f))
    bad = sorted(_price_set(body) - allowed)
    if bad:
        fix("comparison_unsupported_price", f"Remove or source these prices; they are not in the fetched pricing pages "
                                            f"or brand facts: {', '.join('$' + b for b in bad)}.", prices=bad)


PER_SEAT = re.compile(r"(/\s*(user|seat|member)|per (user|seat|member)|por usuario|por asiento)", re.I)
CHOICE = re.compile(r"\b(choose|pick|go with|better (for|if|choice)|wins|if you need|best for|stick with|elige|escoge|"
                    r"mejor (para|si|opción)|conviene|qu[eé]date con)\b", re.I)


def _verdict_and_seats(body: str, c: dict, fix) -> None:
    """Advisory: verdict in the opening lines; per-seat pricing shown at 1, 5 and 25 seats."""
    if c.get("page_type") in ("versus", "own_versus", "alternatives"):
        intro = re.split(r"^##\s", re.sub(r"\A\s*#\s[^\n]*\n", "", body), maxsplit=1, flags=re.M)[0]
        if not (CHOICE.search(intro) and any(n.lower() in intro.lower() for n in c["competitors"])):
            fix("comparison_verdict_first", "Open with the verdict right after the H1: who should pick which option, "
                                            "including where a competitor wins.", advisory=True)
    facts = " ".join(f["excerpt"] for f in c.get("competitor_facts") or []) + " ".join(map(str, c.get("brand_facts") or []))
    if PER_SEAT.search(facts) and not re.search(r"\b25\b[^\n]{0,20}\b(seats?|users?|people|usuarios|personas)\b", body, re.I):
        fix("comparison_seat_math", "Pricing is per seat: add what each option costs at 1, 5 and 25 seats, from the "
                                    "prices in the facts only.", advisory=True)


def _favor_checks(body: str, c: dict, fix) -> None:
    """With favor_brand: the brand leads the first comparison table and, on list pages, the first H3."""
    brand = c["brand"].lower()
    for t in re.findall(r"((?:^\|.*\|\s*$\n?){3,})", body, flags=re.M):
        tl = t.lower()
        hits = {n: tl.find(n.lower()) for n in c["competitors"] if n.lower() in tl}
        if not hits:
            continue
        if brand not in tl or tl.find(brand) > min(hits.values()):
            fix("comparison_brand_not_first", f'Put {c["brand"]} first in the comparison table (first row or first column).')
        break
    if c["page_type"] in ("alternatives", "best"):
        h3 = re.search(r"^###\s+(.+)$", body, flags=re.M)
        if h3 and brand not in h3.group(1).lower():
            fix("comparison_brand_not_first", f'Make {c["brand"]} the first option (the first H3 entry) on this page.')


def frontmatter(brief: dict) -> dict:
    c = brief.get("comparison")
    if not c:
        return {}
    fm = {"pageType": c["page_type"], "competitors": c["competitors"], "comparisonCluster": c["cluster"],
          "lastVerified": date.fromisoformat(c["checked"])}
    if c.get("author"):
        fm["author"] = c["author"]
    if c.get("cta"):
        fm["cta"] = c["cta"]
    if c.get("videos"):
        fm["videos"] = [{"title": v["title"], "url": v["url"]} for v in c["videos"]]
    if c.get("draft"):
        fm["draft"] = True  # held for a human check of competitor claims before it goes live
    return fm


# ---------- companions ----------

LINKEDIN_PROMPT = """Rewrite the article below as a LinkedIn article for {brand}'s company page.

It must not duplicate the website article: different opening, different structure and different sentences. Keep \
the facts, any comparison table (re-worded headers are fine) and any bias disclosure. 700-1100 words. Plain \
Markdown, starting with "# " and a title that contains "{keyword}". End with one line linking to the full \
comparison: {url}. No hashtags. Never say how the text was produced."""


def shingles(text: str, n: int = 5) -> set:
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(w[i:i + n]) for i in range(max(0, len(w) - n + 1))}


def overlap(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / max(1, len(sa)) if sa else 0.0


def linkedin_version(md_body: str, brief: dict, config: dict, backend) -> str | None:
    s = settings(config) or {**DEFAULTS, "brand": config["site_name"]}
    rt = get_runtime(config)
    url = brief.get("_url") or f'https://{config["domain"]}{config["blog_url_prefix"].rstrip("/")}/{brief["slug"]}/'
    system = LINKEDIN_PROMPT.format(brand=s["brand"], keyword=brief["primary_keyword"], url=url)
    user = "ARTICLE:\n" + md_body
    for attempt in (1, 2):
        text = backend._call(config, system, user)
        text = re.sub(r"^```(markdown)?\s*|\s*```\s*$", "", text.strip())
        ov = overlap(text, md_body)
        rt.log("companion", kind="linkedin", keyword=brief["primary_keyword"], attempt=attempt, overlap=round(ov, 3))
        if ov <= s["linkedin_max_overlap"]:
            return text + "\n"
        user = (f"Your last version shared {ov:.0%} of its 5-word phrases with the original. Rewrite every sentence "
                f"in new words.\n\nARTICLE:\n" + md_body)
    return None


def interview_questions(brief: dict) -> str:
    c = brief.get("comparison") or {"page_type": None, "title_keyword": brief["primary_keyword"], "competitors": []}
    kw = brief["primary_keyword"]
    qs = [f"What should someone know before choosing between {kw.replace(' vs ', ' and ')}?"
          if c["page_type"] in ("versus", "own_versus") else
          f"What are the best {kw}?" if c["page_type"] == "alternatives" else
          f"Is {c['competitors'][0]} worth it?" if c["page_type"] == "review" else
          f"What is the {kw}?" if c["page_type"] else f"What should you know about {kw}?"]
    qs += [q["question"] for q in brief.get("serp_summary", {}).get("people_also_ask", [])]
    qs += [f"How do you think about {k}?" for k in brief.get("secondary_keywords", [])[:4]]
    seen, uniq = set(), []
    for q in qs:
        if q.lower() not in seen:
            seen.add(q.lower())
            uniq.append(q)
    lines = [f"# Founder interview: {smart_cap(kw)}", "",
             "Record on the SEO video channel (separate from the main brand channel). Ask each question as written; "
             "ask the founder to repeat the question in the first sentence of the answer, so the searched phrase is in "
             "the transcript. Embed the video in the article and the LinkedIn version.", "",
             f"Suggested title: {smart_cap(c['title_keyword'])}", "", "## Questions (they have search demand)", ""]
    lines += [f"{i}. {q}" for i, q in enumerate(uniq[:10], 1)]
    return "\n".join(lines) + "\n"


def media_checklist(brief: dict, config: dict) -> str:
    c = brief["comparison"]
    lines = [f"# Media and conversion checklist: {brief['primary_keyword']}", "",
             f"Facts checked {c['checked']}. Re-check pricing and features before {c['checked_label']} + 3 months and "
             "update `lastVerified`.", "", "## Screenshots to capture", ""]
    for f in c["competitor_facts"]:
        lines.append(f"- [ ] {f['competitor']} pricing page: {f['url']}")
    for n in c["competitors"]:
        lines.append(f"- [ ] Inside {n}: the main workflow on its cheapest or free plan (sign up if needed)")
        lines.append(f"- [ ] {n} reviews on G2 / Trustpilot (one positive, one critical)")
    for v in c["voice_of_customer"][:3]:
        lines.append(f"- [ ] Reddit thread: {v['url']}")
    if c["brand_on_page"]:
        lines.append(f"- [ ] {c['brand']}: the same workflow, side by side with the competitor screenshot")
    if c["videos"]:
        lines += ["", "## Videos to consider embedding", ""] + [f"- {v['title']} ({v['channel']}): {v['url']}" for v in c["videos"]]
    lines += ["", "## Conversion", ""]
    if c.get("cta"):
        lines.append(f"- CTA in the copy: \"{c['cta']['label']}\" → {c['cta']['url']}. Add a top-of-page button and a "
                     "floating CTA in the layout.")
    lines += ["- Track each CTA click as a GA4 event (or CRM source) tagged with this page's slug.",
              f"- Benchmark: ask what visitor-to-{'demo' if c.get('cta') else 'signup'} rate a {c['page_type']} page "
              f"in this industry should convert at, and compare monthly.",
              "- Judge the page on qualified demos or signups, not traffic."]
    if c["page_type"] in (settings(config) or DEFAULTS)["linkedin_page_types"]:
        lines += ["", "## LinkedIn", "",
                  "- Publish `linkedin.md` as an article from the company page, then repost from the founder's profile.",
                  "- Publish the site article first."]
    return "\n".join(lines) + "\n"


def _video_questions(brief: dict) -> list[str]:
    text = interview_questions(brief)
    return [re.sub(r"^\d+\.\s+", "", ln) for ln in text.splitlines() if re.match(r"^\d+\.\s", ln)]


def youtube_metadata(brief: dict, config: dict) -> str:
    """Title, description with chapters, tags, file name and caption notes for the interview video."""
    kw = brief["primary_keyword"]
    c = brief.get("comparison") or {}
    base = smart_cap(c.get("title_keyword") or kw)
    title = base if len(base) <= 60 else base[:57].rsplit(" ", 1)[0] + "..."
    for extra in (" (Honest Answer)", f" in {date.today().year}", " Explained"):
        if len(title + extra) <= 60 and len(title) < 45:
            title += extra
            break
    qs = _video_questions(brief)
    sec = [k for k in brief.get("secondary_keywords", [])][:5]
    url = brief.get("_url") or f'https://{config["domain"]}{config["blog_url_prefix"].rstrip("/")}/{brief["slug"]}/'
    tags, seen = [], set()
    for t in [kw] + sec + [c.get("title_keyword") or ""] + [n for n in c.get("competitors") or []] + [config["site_name"]]:
        t = (t or "").strip().lower()
        if t and t not in seen and sum(len(x) + 1 for x in tags) + len(t) <= 480:
            seen.add(t)
            tags.append(t)
    chapters = ["00:00 " + ("Introducción" if lang.is_es() else "Intro")]
    chapters += [f"0{i}:00 {q.rstrip('?')}" if i < 10 else f"{i}:00 {q.rstrip('?')}" for i, q in enumerate(qs[:8], 1)]
    L = [f"# YouTube: {kw}", "", f"**Title** ({len(title)} characters, keyword first): {title}", "",
         f"**File name**: `{slugify(kw)}.mp4` (upload with the keyword as the file name)", "",
         "**Description** (paste as is; replace the chapter times after editing):", "", "```",
         f"{smart_cap(kw)}: the short answer in the first two lines, said the same way in the first 15 seconds of the video.",
         "", f"Full write-up: {url}", ""]
    if sec:
        L += ["Also covered: " + ", ".join(sec) + ".", ""]
    L += ["Chapters:"] + chapters + ["```", "", "**Tags**: " + ", ".join(tags), "",
          "**Captions**: upload an edited .srt (do not rely on auto-captions); the keyword must appear in the first "
          "caption line. Add the same transcript below the video embed on the article if the layout allows.", "",
          "**Thumbnail**: the keyword in three to five words, a face, high contrast.", "",
          "**After upload**: embed the video in the article (frontmatter `videos`) and the LinkedIn version."]
    return "\n".join(L) + "\n"


def off_site_note(brief: dict) -> str:
    L = [f"# Off-site opportunity: {brief['primary_keyword']}", "",
         "These results rank in Google's top 10 for this keyword. A page on these platforms can rank (and get cited by "
         "AI answers) faster than the site article:", ""]
    for o in brief.get("off_site") or []:
        what = {"linkedin": "Publish `linkedin.md` from the company page, then repost from the founder.",
                "youtube": "Record the questions in `video_interview.md`; use `youtube.md` for the title and description.",
                "forum": "Answer the thread helpfully as a named person, disclose the affiliation, link only if it helps."}[o["type"]]
        L.append(f"- #{o['position']} {o['type']}: {o['title']} ({o['url']}). {what}")
    return "\n".join(L) + "\n"


def write_companions(md: str, brief: dict, config: dict, backend=None) -> list[str]:
    """Write companion files to data/<site>/companions/<slug>/ (dry-run: dry-run/<site>/...)."""
    off = {o["type"] for o in brief.get("off_site") or []}
    if not brief.get("comparison") and not off & {"linkedin", "youtube"}:
        return []
    s = settings(config) or {**DEFAULTS, "brand": config["site_name"]}
    rt = get_runtime(config)
    out = config["_paths"]["work"] / "companions" / brief["slug"]
    out.mkdir(parents=True, exist_ok=True)
    files = {"video_interview.md": interview_questions(brief), "youtube.md": youtube_metadata(brief, config)}
    if brief.get("comparison"):
        files["media_checklist.md"] = media_checklist(brief, config)
    want_li = (brief.get("comparison") and brief["comparison"]["page_type"] in s["linkedin_page_types"]) or "linkedin" in off
    if off:
        files["off_site.md"] = off_site_note(brief)
    if backend is not None and hasattr(backend, "_call") and want_li:
        from textutil import split_frontmatter
        try:
            li = linkedin_version(split_frontmatter(md)[1], brief, config, backend)
        except Exception as e:  # noqa: BLE001 - a companion never blocks the article
            rt.log("stage_error", stage="companion_linkedin", keyword=brief["primary_keyword"], error=f"{type(e).__name__}: {e}")
            li = None
        if li:
            files["linkedin.md"] = li
    written = []
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        written.append(str(out / name))
    rt.log("stage", stage="companions", keyword=brief["primary_keyword"], files=sorted(files))
    return written
