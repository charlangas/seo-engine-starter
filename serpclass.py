"""Classify the Google top 10: what kind of page ranks decides what kind of page to write.

Each organic result becomes one of: blog_post, service_page, product_page, listicle, tool, directory, forum,
linkedin, youtube, other. When 3 or more of the top 5 are interactive tools (calculators, generators, checkers),
`needs_tool` is set: an article will not rank there, so the run skips the keyword and the weekly report asks
for a free tool instead. The dominant on-site format (ignoring LinkedIn, YouTube, forums and directories)
becomes the brief's `format`, which the writer follows and `page_paths` can route to a folder (for example
service pages under /services/). LinkedIn, YouTube and Reddit results in the top 10 are off-site
opportunities: the engine writes the LinkedIn version and the video brief for that page even when it is not a
comparison page.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

DIRECTORIES = ("yelp.", "g2.com", "capterra.", "clutch.co", "angi.com", "angieslist", "homeadvisor.", "thumbtack.",
               "yellowpages.", "bbb.org", "trustpilot.", "tripadvisor.", "upcity.", "sortlist.", "designrush.",
               "expertise.com", "goodfirms.", "softwareadvice.", "getapp.", "houzz.", "nextdoor.", "mapquest.",
               "seccionamarilla.", "doctoralia.", "healthgrades.", "zocdoc.", "mercadolibre.", "amazon.")
FORUMS = ("reddit.com", "quora.com", "stackexchange.com", "stackoverflow.com", "forum", "community.", "foro")
LISTICLE = re.compile(r"(^\d+\s+(best|top|great)|\btop\s+\d+\b|\b\d+\s+best\b|\bbest\b.*\b(20\d\d|of the year|for \w+)|"
                      r"^(the )?best\b|\blas? \d+ mejores\b|\bmejores\b.*20\d\d|^los mejores)", re.I)
BLOG_PATH = re.compile(r"/(blog|news|articles?|guides?|learn|resources|insights|posts?|magazine|library|academy|"
                       r"noticias|articulos?|guias?)/|/20\d\d/", re.I)
SERVICE_PATH = re.compile(r"/(services?|servicios?|locations?|areas?(-served)?|service-areas?|industries|solutions|"
                          r"treatments?|specialties|what-we-do|hire)(/|$)", re.I)
PRODUCT_PATH = re.compile(r"/(pricing|plans|product|products|features|software|app|platform|tienda|producto|shop|store|"
                          r"catalog|catalogo)(/|$)", re.I)
TOOL = re.compile(r"\b(calculator|calc|generator|checker|converter|estimator|simulator|planner|quiz|tool|"
                  r"calculadora|generador|cotizador|simulador|verificador|convertidor)s?\b", re.I)
OFF_SITE = ("linkedin", "youtube", "forum")


def classify(r: dict) -> str:
    url = (r.get("url") or "").lower()
    p = urlsplit(url if "://" in url else "https://" + url)
    host, path = p.netloc.removeprefix("www."), p.path or "/"
    title = r.get("title") or ""
    if "linkedin.com" in host:
        return "linkedin"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if any(f in host for f in FORUMS) or "/forum" in path or "/community/" in path:
        return "forum"
    if any(d in host for d in DIRECTORIES):
        return "directory"
    explainer = re.search(r"\b(how|what|why|guide|best|vs|c[oó]mo|qu[eé]|gu[ií]a)\b", title, re.I) or LISTICLE.search(title)
    if not BLOG_PATH.search(path) and not explainer and (TOOL.search(re.sub(r"[-_/]", " ", path)) or TOOL.search(title)):
        return "tool"
    if LISTICLE.search(title):
        return "listicle"
    if BLOG_PATH.search(path):
        return "blog_post"
    if SERVICE_PATH.search(path):
        return "service_page"
    if PRODUCT_PATH.search(path):
        return "product_page"
    if path in ("", "/"):
        return "service_page"  # a home page ranking for a commercial query is a business selling the thing
    if path.count("/") <= 2 and not re.search(r"\d", path) and len(path.strip("/").split("-")) <= 4:
        return "service_page"  # short, shallow slug on a business site
    return "blog_post" if len(path.strip("/").split("-")) >= 5 else "other"


def summarize(organic: list[dict]) -> dict:
    """Types per result, counts, dominant on-site format and off-site opportunities."""
    types = [classify(r) for r in organic[:10]]
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    onsite = {k: v for k, v in counts.items() if k in ("blog_post", "service_page", "product_page", "listicle", "tool", "other")}
    n = sum(onsite.values())
    needs_tool = types[:5].count("tool") >= 3
    if needs_tool:
        fmt = "tool"
    elif not n:
        fmt = "blog_post"
    elif onsite.get("listicle", 0) / n >= 0.4:
        fmt = "listicle"
    elif (onsite.get("service_page", 0) + onsite.get("product_page", 0)) / n >= 0.5:
        fmt = "product_page" if onsite.get("product_page", 0) > onsite.get("service_page", 0) else "service_page"
    else:
        fmt = "blog_post"
    off = [{"type": t, "position": i + 1, "url": organic[i].get("url"), "title": organic[i].get("title", "")}
           for i, t in enumerate(types) if t in OFF_SITE]
    return {"types": types, "counts": counts, "format": fmt, "off_site": off, "needs_tool": needs_tool}


FORMAT_GUIDE = {
    "blog_post": "",
    "tool": "Ranking pages are interactive tools. Lead with a worked example the reader can follow by hand (inputs, "
            "the formula or table, the result), then explain each input.",
    "listicle": "Ranking pages are mostly list articles. Structure this one as a list of options (one H3 each) with a "
                "\"Best for\" line per option, after the selection criteria.",
    "service_page": "Ranking pages are mostly service pages from businesses, not blog posts. Write this as a service page: "
                    "what the reader gets, who it is for, the process step by step, price drivers, proof, service area "
                    "and a clear call to action, then the FAQ. Keep it scannable; skip essay-style scene setting.",
    "product_page": "Ranking pages are mostly product and pricing pages. Lead with what the product does, plans and price "
                    "drivers, who it fits, and a comparison table, then the FAQ.",
}
