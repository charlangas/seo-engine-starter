"""Stage 2: live SERP analysis and brief construction (SerpAPI, engine=google)."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

import inventory
import lang
from apiclient import ApiError, fetch_page, serpapi_get
from config import get_runtime
from textutil import (content_year, has_past_year, jaccard, normalize_keyword, slugify, smart_cap, title_case, tokens,
                      word_count)

BOILERPLATE = re.compile(
    r"^(related|recent|popular|latest|more) |table of contents|contents$|share|comment|leave a reply|about (the )?author|"
    r"about us|contact|subscribe|newsletter|sign up|follow us|you may also|read more|references|sources$|"
    r"footnotes|navigation|menu|search|categories|tags$|conclusion|final thoughts|summary$|faq|frequently asked|"
    r"get (a |your )?(free )?(quote|estimate)|call (us|now)|our (services|team|locations|reviews)|testimonials|"
    r"reviews$|privacy|cookie|disclaimer|key takeaways|wrapping up|bottom line|service areas?|why choose|"
    r"schedule|book (now|online)|request|hours|financing|coupons?|special offers?|(our|featured) clients?|"
    r"case stud(y|ies)|portfolio|our work|locations?$|awards?|careers|let'?s talk|get in touch|ready to|"
    r"what our|who we are|our (story|mission|approach|process|expertise)|trusted by|partners?$", re.I)
SKIP_DOMAINS = ("youtube.com", "reddit.com", "facebook.com", "pinterest.", "instagram.com", "tiktok.com",
                "quora.com", "yelp.com", "twitter.com", "x.com", "linkedin.com", "amazon.", "nextdoor.com")
AUTHORITY_TLDS = (".gov", ".edu", ".org")
TOPIC_SIGNAL = re.compile(
    r"^(how|what|why|when|which|who|where|is|are|can|do|does|should)\b|\?$|\b(costs?|pric(e|es|ing)|benefits?|"
    r"types?|mistakes?|tips?|steps?|process|examples?|strateg(y|ies)|measur\w*|metrics?|challenges?|trends?|tools?|"
    r"best practices?|vs\.?|versus|checklist|timeline|maintenance|risks?|requirements?|pros|cons|advantages?|"
    r"disadvantages?|signs?|factors?|options?|alternatives?|budget|roi|results?|common|choos\w*|compar\w*|"
    r"differences?|guide|basics|works?|plan(ning)?|ideas?|tactics?|channels?|framework)\b", re.I)
GL_CODES = {"United States": "us", "United Kingdom": "uk", "Canada": "ca", "Australia": "au", "Ireland": "ie",
            "New Zealand": "nz", "India": "in", "Germany": "de", "France": "fr", "Spain": "es", "Mexico": "mx",
            "Italy": "it", "Netherlands": "nl", "Brazil": "br", "South Africa": "za", "Singapore": "sg"}


def _skip(domain: str, config) -> bool:
    return any(s in domain for s in SKIP_DOMAINS + tuple(config.get("skip_domains") or ()))


def _boilerplate(label: str) -> bool:
    return bool(BOILERPLATE.search(label) or (lang.is_es() and lang.ES_BOILERPLATE.search(label)))


def _topic_signal(label: str) -> bool:
    return bool(lang.ES_TOPIC_SIGNAL.search(label) if lang.is_es() else TOPIC_SIGNAL.search(label))


def _domain(url: str) -> str:
    return re.sub(r"^www\.", "", urlparse(url).netloc.lower())


def _displayed_domain(displayed: str) -> str:
    first = displayed.split("\u203a")[0].strip().split(" ")[0]
    return _domain(first if "//" in first else "https://" + first)


def resolve_result_url(config, link: str) -> tuple[str, bool]:
    """Google result links can be opaque google.com/goto redirects; follow one hop to the real URL."""
    if not re.match(r"https?://(www\.)?google\.[a-z.]+/(goto|url)\?", link):
        return link, True
    rt = get_runtime(config)
    try:
        resp = requests.get(link, timeout=15, allow_redirects=False, headers={"User-Agent": "Mozilla/5.0"})
        target = resp.headers.get("location", "")
        target = re.sub(r"[?&]srsltid=[^&#]*", "", target)
        ok = resp.status_code in (301, 302, 303, 307, 308) and target.startswith("http") and "google." not in _domain(target)
        rt.log("redirect_resolve", ok=ok, status=resp.status_code, target=target[:200])
        return (target, True) if ok else (link, False)
    except requests.RequestException as e:
        rt.log("redirect_resolve", ok=False, error=str(e)[:150])
        return link, False


# ---------- SERP quality guard ----------

def serp_quality(serp: dict, expect_terms=()) -> list[str]:
    """Problems that make a SERP untrustworthy (empty list = fine). A bad SERP is never cached, and briefs built
    on it do not use it for the page format or keyword clustering."""
    org = serp.get("organic") or []
    problems = []
    if len(org) < 5:
        problems.append(f"only {len(org)} organic results")
    unresolved = sum(1 for r in org if not r.get("resolved", True))
    if org and unresolved >= max(3, len(org) * 0.4):
        problems.append(f"{unresolved} of {len(org)} result links are unresolved Google redirects")
    google = sum(1 for r in org if re.search(r"(^|\.)google\.", r.get("domain") or ""))
    if google >= 3:
        problems.append(f"{google} results point at google.com")
    doms: dict[str, int] = {}
    for r in org:
        doms[r.get("domain") or ""] = doms.get(r.get("domain") or "", 0) + 1
    top = max(doms.items(), key=lambda kv: kv[1]) if doms else ("", 0)
    if top[1] >= 5 and top[0]:
        problems.append(f"{top[1]} of {len(org)} results are from {top[0]}")
    for term in expect_terms:
        t = term.lower().replace(" ", "")
        if t and not any(t in (r.get("domain") or "").replace("-", "") or term.lower() in (r.get("title") or "").lower()
                         for r in org):
            problems.append(f'"{term}" appears in no result (wrong or scrambled SERP?)')
    return problems


# ---------- SERP fetch with 24h cache ----------

def fetch_serp(keyword: str, config, expect_terms=()) -> dict:
    """Top 10 for the keyword (24h cache). A SERP that fails `serp_quality` is fetched once more without
    SerpAPI's cache; if it still fails it is returned with `suspect` set and not cached."""
    rt = get_runtime(config)
    # No city configured: pin the SERP to the site's country so results are never geolocated at random.
    loc = config.get("location") or config["country"]
    key = hashlib.sha1(f"{keyword.lower().strip()}|{loc.lower()}".encode()).hexdigest()
    cache_file = config["_paths"]["cache"] / "serp" / f"{key}.json"
    ttl = config["serp_cache_hours"] * 3600
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
        rt.log("cache_hit", kind="serp", keyword=keyword, location=loc)
        return json.loads(cache_file.read_text(encoding="utf-8"))

    serp = _fetch_live(keyword, config, loc)
    problems = serp_quality(serp, expect_terms)
    if problems:
        rt.log("filter_decision", step="serp_guard", keyword=keyword, problems=problems, note="refetching without cache")
        serp = _fetch_live(keyword, config, loc, no_cache=True)
        problems = serp_quality(serp, expect_terms)
    if problems:
        serp["suspect"] = problems
        rt.log("stage_error", stage="serp_guard", keyword=keyword, problems=problems)
        return serp
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(serp, indent=2, ensure_ascii=False), encoding="utf-8")
    return serp


def _fetch_live(keyword: str, config, loc, no_cache: bool = False) -> dict:
    rt = get_runtime(config)
    params = {"engine": "google", "q": keyword, "hl": config["language_code"], "num": 10}
    if no_cache:
        params["no_cache"] = "true"
    if config["country"] in GL_CODES:
        params["gl"] = GL_CODES[config["country"]]
    if loc:
        params["location"] = loc
    try:
        data = serpapi_get(config, params)
    except ApiError as e:
        if loc and "location" in str(e).lower():
            rt.log("filter_decision", step="serp", note="location rejected by SerpAPI; retrying without it", keyword=keyword)
            params.pop("location")
            data = serpapi_get(config, params)
        else:
            raise
    organic = []
    for r in (data.get("organic_results") or [])[:10]:
        if not r.get("link"):
            continue
        url, resolved = resolve_result_url(config, r["link"])
        domain = _domain(url) if resolved else _displayed_domain(r.get("displayed_link", ""))
        organic.append({"position": r.get("position"), "title": r.get("title", ""), "url": url,
                        "domain": domain, "resolved": resolved, "snippet": r.get("snippet", "")})
    serp = {
        "keyword": keyword, "location": loc, "fetched_at": int(time.time()),
        "organic": organic,
        "people_also_ask": [{"question": q.get("question", ""), "snippet": q.get("snippet") or "",
                             "source_url": q.get("link") or "", "source_title": q.get("title") or ""}
                            for q in (data.get("related_questions") or []) if q.get("question")],
        "related_searches": [r["query"] for r in (data.get("related_searches") or []) if r.get("query")],
    }
    if not serp["organic"]:
        raise ApiError(f"serpapi returned no organic results for '{keyword}'")
    return serp


# ---------- competitor page analysis ----------

def analyze_page(html: str) -> dict:
    """Extract H2/H3 headings (with words under each) and approximate word count."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "iframe"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    if word_count(main.get_text(" ")) < 150 and soup.body:
        main = soup.body
    heads = []
    for h in main.find_all(["h2", "h3"]):
        text = re.sub(r"\s+", " ", h.get_text(" ")).strip()
        if 2 < len(text) <= 140:
            idx = len(heads)
            heads.append({"level": int(h.name[1]), "text": text})
            h.replace_with(f" §§{idx}§§ ")
        else:
            h.decompose()
    text = main.get_text(" ")
    parts = re.split(r"§§(\d+)§§", text)
    total = word_count(parts[0])
    for i in range(1, len(parts) - 1, 2):
        wc = word_count(parts[i + 1])
        heads[int(parts[i])]["words"] = wc
        total += wc + word_count(heads[int(parts[i])]["text"])
    # words "under" an H2 include its H3 subsections
    cur = None
    for h in heads:
        h.setdefault("words", 0)
        if h["level"] == 2:
            cur = h
            h["section_words"] = h["words"]
        elif cur is not None:
            cur["section_words"] += h["words"]
    return {"headings": heads, "word_count": total}


def _topic_tokens(text: str, kw_tokens: set) -> frozenset:
    text = re.sub(r"^\s*((step|paso)\s*)?\d+[\.\):]?\s*", "", text.lower())
    toks = set(tokens(text))
    rest = toks - kw_tokens
    return frozenset(rest if rest else toks)


def _clean_heading(text: str) -> str:
    text = re.sub(r"^\s*((step|paso)\s*)?#?\d+[\.\):\-]?\s+", "", text.strip(), flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" :-–—")
    return text if not text.isupper() else title_case(text.lower())


def _same_topic(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    if jaccard(a, b) >= 0.5:
        return True
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return len(small) >= 2 and small <= big


def cluster_headings(pages: list[dict], keyword: str) -> list[dict]:
    """Group headings across pages into subtopics; count pages covering each (overall and as H2)."""
    kw = set(tokens(keyword))
    clusters: list[dict] = []
    for pi, page in enumerate(pages):
        parent = None
        for order, h in enumerate(page["headings"]):
            label = _clean_heading(h["text"])
            if _boilerplate(label) or len(label.split()) > 16:
                if h["level"] == 2:
                    parent = None
                continue
            tk = _topic_tokens(label, kw)
            if not tk:
                continue
            c = next((c for c in clusters if _same_topic(tk, c["tokens"])), None)
            if c is None:
                c = {"tokens": tk, "labels": [], "pages": set(), "h2_pages": set(), "orders": [],
                     "section_words": [], "children": []}
                clusters.append(c)
            c["labels"].append(label)
            c["pages"].add(pi)
            c["orders"].append(order / max(1, len(page["headings"])))
            if h["level"] == 2:
                c["h2_pages"].add(pi)
                c["section_words"].append(h.get("section_words", h["words"]))
                parent = c
            elif parent is not None and parent is not c:
                parent["children"].append(label)
    return clusters


def _best_label(labels: list[str]) -> str:
    ok = [l for l in labels if 2 <= len(l.split()) <= 10] or labels
    return sorted(ok, key=lambda l: (abs(len(l.split()) - 5), len(l)))[0]


def _covered_pages(topic_tokens: set, pages: list[dict], kw: set, h2_only=False) -> int:
    """How many analysed pages have a heading covering this topic."""
    need = set(topic_tokens) - kw or set(topic_tokens)
    if not need:
        return len(pages)
    n = 0
    for page in pages:
        for h in page["headings"]:
            if h2_only and h["level"] != 2:
                continue
            ht = set(tokens(h["text"]))
            if len(need & ht) / len(need) >= 0.6:
                n += 1
                break
    return n


# ---------- brief ----------

SKELETON = {
    "informational": ["{Kw}: what it actually means", "{Kw}: how it works, step by step",
                      "{Kw}: what changes the outcome", "{Kw}: the mistakes people make most"],
    "commercial": ["{Kw}: what to look for", "{Kw}: how the main options compare",
                   "{Kw} pricing: what drives the cost", "{Kw}: how to choose the right fit"],
    "transactional": ["{Kw}: what you actually get", "{Kw} pricing: what drives the cost",
                      "{Kw}: how to choose a provider", "{Kw}: what to check before you commit"],
    "navigational": ["{Kw}: what it is", "{Kw}: how to get started", "{Kw}: what to know first"],
}


def _schema_type(keyword: str, intent: str, config) -> str:
    k = keyword.lower()
    if k.startswith(("how to", "how do")) or (lang.is_es() and k.startswith(("como ", "cómo "))):
        return "HowTo"
    if config["business_type"] == "local_services" and intent in ("commercial", "transactional"):
        return "Service"
    if config["business_type"] == "ecommerce" and intent == "transactional":
        return "Product"
    return "Article"


def _domain_matches(domain: str, patterns) -> bool:
    d = domain.lower()
    return any(d == p.lstrip(".") or d.endswith("." + p.lstrip(".")) or (p.startswith(".") and d.endswith(p))
               for p in (x.lower() for x in patterns))


def _curated_sources(config, keywords: list[str]) -> list[dict]:
    """Hand-picked primary sources whose `match` words appear in the keyword or its secondary keywords."""
    text = " ".join(keywords).lower()
    out = []
    for s in config.get("primary_sources") or []:
        if any(w.lower() in text for w in s.get("match") or []):
            out.append({"title": s["title"], "url": s["url"], "domain": _domain(s["url"])})
    return out


def _external_sources(serp: dict, config, keywords: list[str] | None = None) -> list[dict]:
    if not config.get("cite_external_sources", True):
        return []
    own = config["domain"]
    blocked = {own} | {re.sub(r"^www\.", "", d.lower()) for d in config["competitor_domains"]}
    cands = [r for r in serp["organic"]
             if r.get("resolved", True) and r["domain"] not in blocked and not _skip(r["domain"], config)]
    only = config.get("cite_only_domains") or []
    if only:   # primary sources only: never cite other publishers' ranking pages
        cands = [r for r in cands if _domain_matches(r["domain"], only)]
    curated = _curated_sources(config, keywords or [])
    cands.sort(key=lambda r: (0 if r["domain"].endswith(AUTHORITY_TLDS) or "wikipedia.org" in r["domain"] else 1,
                              -(r["position"] or 0)))
    out, seen, urls = [], set(), set()
    for c in curated:
        if c["url"] not in urls:
            urls.add(c["url"])
            out.append(c)
    for r in cands:
        if r["domain"] not in seen and not any(c["domain"] == r["domain"] for c in curated):
            seen.add(r["domain"])
            out.append({"title": r["title"], "url": r["url"], "domain": r["domain"]})
    return out[:4]


def _internal_targets(keyword: str, config) -> list[dict]:
    kw = set(tokens(keyword))
    blog, dirp = config["blog_url_prefix"].rstrip("/"), config["directory_url_prefix"].rstrip("/")
    scored = []
    for e in inventory.load(config) + list(config.get("_extra_entries") or []):
        if e.get("status") != "written" and e.get("type") == "article":
            continue
        if e.get("type") == "excluded":  # keyword blocked on purpose; there is no page to link to
            continue
        if inventory.held(config, e):  # written as a draft and not yet published: linking would 404
            continue
        if normalize_keyword(e["primary_keyword"]) == normalize_keyword(keyword):
            continue
        et = set(tokens(e["primary_keyword"] + " " + e.get("title", "")))
        overlap = len(kw & et)
        t = e.get("type", "article")
        if t == "article":
            url = f"{(e.get('url_prefix') or blog).rstrip('/')}/{e['slug']}/"
        elif t == "directory_category":
            url = f"{dirp}/{e['slug']}/"
        else:
            url = f"{dirp}/{e.get('category_slug', 'listings')}/{e['slug']}/"
        scored.append({"url": url, "anchor": e.get("anchor") or e.get("title") or e["primary_keyword"],
                       "type": t, "relevance": overlap, "rating": e.get("rating") or 0})
    scored.sort(key=lambda x: (-x["relevance"], {"directory_category": 0, "article": 1}.get(x["type"], 2), -x["rating"]))
    articles = [s for s in scored if s["type"] == "article" and s["relevance"] > 0][:4]
    directory = []
    if config["enable_directory"]:
        directory = [s for s in scored if s["type"] != "article"][:4]
    return articles + directory


def build_brief(keyword, config) -> dict:
    """Build and save brief_{slug}.json for one keyword (str or ranked_keywords item)."""
    rt = get_runtime(config)
    item = keyword if isinstance(keyword, dict) else {"keyword": keyword}
    kw = item["keyword"]
    slug = slugify(kw)
    intent = item.get("intent") or "informational"
    rt.log("stage", stage="brief", keyword=kw)

    expect = item.get("competitors") if item.get("page_type") in ("versus", "alternatives", "switch", "export") else ()
    serp = fetch_serp(kw, config, expect_terms=tuple(expect or ()))

    # Top 5 organic URLs: headings + word counts
    pages = []
    for r in serp["organic"]:
        if len(pages) == 5:
            break
        if _skip(r["domain"], config):  # not article pages; take the next organic result
            rt.log("rejection", stage="brief", keyword=kw, url=r["url"], reason="non_article_domain")
            continue
        html = fetch_page(config, r["url"]) if not r["url"].lower().endswith(".pdf") else None
        if html:
            info = analyze_page(html)
            if info["word_count"] >= 100:
                pages.append({"url": r["url"], "domain": r["domain"], **info})
                continue
        rt.log("rejection", stage="brief", keyword=kw, url=r["url"], reason="page_unreadable_or_empty")
    counts = [p["word_count"] for p in pages]
    avg_wc = round(sum(counts) / len(counts)) if counts else 0
    min_len = config["min_word_count"].get(intent, 1200)
    target = max(avg_wc, min_len)
    if config.get("target_word_count_cap"):
        target = min(target, int(config["target_word_count_cap"]))

    kwt = set(tokens(kw))
    clusters = cluster_headings(pages, kw)
    major = sorted([c for c in clusters if len(c["h2_pages"]) >= 3],
                   key=lambda c: sum(c["orders"]) / len(c["orders"]))[:6]

    outline, gaps = [], []
    used_tokens: list[frozenset] = []

    def add_section(h2, h3s, source, gap=False, **meta):
        outline.append({"h2": h2, "h3": h3s, "source": source, "gap": gap, **meta})
        used_tokens.append(frozenset(tokens(h2)))

    def already_used(text):
        t = frozenset(tokens(text))
        return any(_same_topic(t - kwt or t, u - kwt or u) for u in used_tokens)

    # Consensus subtopics (H2 in >= 3 of the top 5)
    for c in major:
        label = _best_label(c["labels"])
        seen, h3s = set(), []
        for ch in sorted(set(c["children"]), key=lambda x: -c["children"].count(x)):
            k = frozenset(tokens(ch))
            if k and k not in seen and not _boilerplate(ch):
                seen.add(k)
                h3s.append(ch)
            if len(h3s) == 3:
                break
        avg_sec = round(sum(c["section_words"]) / len(c["section_words"])) if c["section_words"] else 0
        thin = avg_sec < config["thin_section_words"]
        add_section(label, h3s, "serp_consensus", coverage=len(c["h2_pages"]), competitor_avg_words=avg_sec, thin=thin)
        if thin:
            gaps.append({"type": "thin_coverage", "topic": label, "section": label,
                         "reason": f"H2 in {len(c['h2_pages'])} of top {len(pages)} results but they average only "
                                   f"{avg_sec} words on it; cover it in real depth (250+ words)."})

    # Intent skeleton when the SERP has little heading consensus
    if len(outline) < 3:
        skeleton = lang.ES_SKELETON if lang.is_es() else SKELETON
        for tpl in skeleton.get(intent, skeleton["informational"]):
            h2 = tpl.format(Kw=smart_cap(kw), kw=kw)
            if len(outline) >= 4:
                break
            if not already_used(h2):
                add_section(h2, [], "intent_skeleton", coverage=_covered_pages(set(tokens(h2)), pages, kwt, h2_only=True))

    # Gap candidates (never an H2 in >= 3 of the top results)
    paa = serp["people_also_ask"]
    cands = []
    for q in paa:
        qt = set(tokens(q["question"]))
        n_any = _covered_pages(qt, pages, kwt)
        n_h2 = _covered_pages(qt, pages, kwt, h2_only=True)
        q["_cov"] = n_any
        if n_any == 0:
            cands.append((0, q["question"], "unanswered_paa", n_h2,
                          f"People Also Ask question that none of the top {len(pages)} results answer under a heading."))
    vocab = set(kwt)
    for text in ([r["title"] + " " + r["snippet"] for r in serp["organic"]] + serp["related_searches"]
                 + [q["question"] for q in paa]):
        vocab |= set(tokens(text))
    for c in clusters:
        if len(c["pages"]) == 2 and len(c["h2_pages"]) <= 2 and len(c["tokens"]) >= 1 and c["h2_pages"]:
            label = _best_label(c["labels"])
            lt = set(tokens(label))
            if not _topic_signal(label):
                continue  # brand / entity names from listicles are not subtopics
            if not lt or len(lt & vocab) / len(lt) < 0.5 or len(label.split()) < 2:
                continue  # off-topic or site-furniture heading
            cands.append((1, label, "minority_subtopic", len(c["h2_pages"]),
                          f"Only {len(c['pages'])} of the top {len(pages)} results cover this; the rest skip it."))
    for rs in serp["related_searches"]:
        n_h2 = _covered_pages(set(tokens(rs)), pages, kwt, h2_only=True)
        rst = set(tokens(rs))
        on_topic = len(rst & kwt) / len(kwt) >= 0.5 if kwt else True
        if n_h2 < 3 and rst - kwt and on_topic and not {"near", "cerca"} & set(rs.lower().split()) and len(rst - kwt) <= 4:
            cands.append((2, title_case(rs), "uncovered_related_search", n_h2,
                          f"Related search that only {n_h2} of the top {len(pages)} results address as a section."))
    for q in paa:
        if 0 < q["_cov"] < 3:
            n_h2 = _covered_pages(set(tokens(q["question"])), pages, kwt, h2_only=True)
            if n_h2 < 3:
                cands.append((3, q["question"], "underanswered_paa", n_h2,
                              f"People Also Ask question answered by only {q['_cov']} of the top {len(pages)} results."))
    angles = lang.ES_MISSING_ANGLES if lang.is_es() else [
        "{Kw}: what most guides leave out", "{Kw}: what it looks like when it goes wrong",
        "{Kw}: questions to ask before you spend money"]
    for tpl in angles:
        h2 = tpl.format(Kw=smart_cap(kw))
        cands.append((4, h2, "missing_angle", _covered_pages(set(tokens(h2)), pages, kwt, h2_only=True),
                      "No top result has a section on this angle."))

    gap_sections = 0
    paa_as_h2 = set()
    paa_budget = max(0, len(paa) - 3)  # keep at least 3 PAA questions for the FAQ
    ordered = sorted(cands, key=lambda c: c[0])
    eligible = [c for c in ordered if c[3] < 3]
    non_paa = sum(1 for c in eligible if "paa" not in c[2] and c[0] < 4)
    if non_paa + paa_budget < 2:
        paa_budget = 2 - non_paa
    for _, topic, gtype, n_h2, reason in ordered:
        if n_h2 >= 3 or already_used(topic):
            continue
        is_paa = "paa" in gtype
        if gtype == "missing_angle" and gap_sections >= 2:
            continue
        if gap_sections < config["max_gap_sections"] and (not is_paa or paa_budget > 0):
            paa_budget -= 1 if is_paa else 0
            h3s = []
            if "paa" not in gtype:
                tt = set(tokens(topic)) - kwt
                h3s = [q["question"] for q in paa if tt & set(tokens(q["question"]))][:2]
            add_section(topic, h3s, f"gap:{gtype}", gap=True, top_results_with_h2=n_h2)
            gaps.append({"type": gtype, "topic": topic, "section": topic, "reason": reason})
            gap_sections += 1
            if "paa" in gtype:
                paa_as_h2.add(topic)
        elif gtype in ("unanswered_paa", "underanswered_paa"):
            gaps.append({"type": gtype, "topic": topic, "section": "FAQ", "reason": reason})

    # FAQ from PAA
    faq_qs = [q["question"] for q in paa if q["question"] not in paa_as_h2][:6]
    if len(faq_qs) < 3:
        extra = [f"What does {kw} involve?", f"How much should you expect to pay for {kw}?",
                 f"What should you check first when it comes to {kw}?"]
        if config.get("faq_fallback"):
            extra = [q.format(kw=kw) for q in config["faq_fallback"]]
        if lang.is_es():
            extra = [q.format(kw=kw) for q in lang.ES_FAQ_FALLBACK]
        faq_qs += [q for q in extra if q not in faq_qs][: 3 - len(faq_qs)]
    faq_h2 = lang.ES_FAQ_H2.format(kw=kw) if lang.is_es() else f"Frequently asked questions about {kw}"
    outline.append({"h2": faq_h2, "h3": faq_qs, "source": "people_also_ask", "gap": False, "faq": True})
    for g in gaps:
        if g["section"] == "FAQ":
            g["section"] = faq_h2

    secondary, seen = [], {" ".join(tokens(kw))}
    for s in item.get("serp_cluster") or []:  # same Google top 10 as this keyword: same page, whatever the words
        s = s["keyword"] if isinstance(s, dict) else s
        n = " ".join(tokens(s))
        if n and n not in seen:
            seen.add(n)
            secondary.append(s.lower())
    for s in serp["related_searches"] + item.get("related", []):
        n = " ".join(tokens(s))
        if kwt and len(set(tokens(s)) & kwt) / len(kwt) < 0.5:
            continue  # unrelated or branded suggestion
        if n and n not in seen:
            seen.add(n)
            secondary.append(s.lower())
    year = content_year(config)
    secondary = [s for s in secondary if not has_past_year(s, year)]  # never target or tag an earlier year
    internal = _internal_targets(kw, config)
    dir_links = [t for t in internal if t["type"] != "article"]

    brief = {
        "primary_keyword": kw,
        "slug": slug,
        "secondary_keywords": secondary[:8],
        "intent": intent,
        "target_word_count": int(target),
        "outline": outline,
        "placement_rules": {
            "primary_keyword_in": ["title", "h1", "first_100_words", "meta_description", "at_least_one_h2"],
            "secondary_keywords": "use each at most twice, only where it reads naturally",
            "intro_max_words": 150,
            "faq": "final H2, one H3 per question, answers 40-90 words",
            "structure": "at least one list or table; one analogy or named mental model per major H2",
            "meta_description_chars": [50, 160],
            "internal_links": "link each internal_link_target once, in body paragraphs, never in headings",
            "directory_links_min": 2 if (config["enable_directory"] and len(dir_links) >= 2) else 0,
            "external_links": "cite each external source once with descriptive anchor text",
        },
        "external_sources": _external_sources(serp, config, [kw] + list(secondary)),
        "internal_link_targets": internal,
        "schema_type": _schema_type(kw, intent, config),
        "gaps_to_address": gaps,
        "serp_summary": {
            "location": serp["location"],
            "top_results": serp["organic"],
            "people_also_ask": [{k: v for k, v in q.items() if not k.startswith("_")} for q in paa],
            "related_searches": serp["related_searches"],
            "top5_pages": [{"url": p["url"], "word_count": p["word_count"],
                            "h2": [h["text"] for h in p["headings"] if h["level"] == 2][:25]} for p in pages],
            "average_top5_word_count": avg_wc,
            "minimum_length_rule": min_len,
            "pages_analysed": len(pages),
        },
        "metrics": {k: item.get(k) for k in ("volume", "kd", "score", "cpc") if k in item},
    }
    import serpclass
    sc = serpclass.summarize(serp["organic"])
    for r, t in zip(brief["serp_summary"]["top_results"], sc["types"]):
        r["type"] = t
    brief["serp_summary"]["result_types"] = sc["counts"]
    if serp.get("suspect"):   # do not let a scrambled SERP decide the page format or off-site work
        brief["serp_suspect"] = serp["suspect"]
        brief["format"], brief["off_site"], brief["needs_tool"] = "blog_post", [], False
    else:
        brief["format"] = sc["format"]
        brief["off_site"] = sc["off_site"]
        brief["needs_tool"] = sc["needs_tool"]
    if (not gaps or gap_sections < 2) and not item.get("page_type"):  # comparison pages add their own sections
        raise ApiError(f"brief for '{kw}' has fewer than 2 gap-filling sections")
    out = config["_paths"]["briefs"]
    out.mkdir(parents=True, exist_ok=True)
    (out / f"brief_{slug}.json").write_text(json.dumps(brief, indent=2, ensure_ascii=False), encoding="utf-8")
    rt.log("stage", stage="brief_done", keyword=kw, target_word_count=brief["target_word_count"],
           sections=len(outline), gaps=len(gaps), pages_analysed=len(pages))
    return brief
