"""Site health check: crawl the live sitemap and catch what silently loses rankings.

  python main.py health --config configs/site.yaml [--no-inspect]

For every URL in the sitemap (robots.txt `Sitemap:` lines, else /sitemap-index.xml or /sitemap.xml; nested
indexes are followed) up to `health.max_urls`:
- the final status (errors and 4xx/5xx), redirects and redirect chains
- the canonical tag (pointing somewhere else), `noindex` in the meta robots tag or X-Robots-Tag header
- robots.txt rules that block Googlebot
- missing, duplicate titles and meta descriptions
Plus, from local files: engine pages that are published but not in the sitemap, and orphan pages (no other
page links to them). With a Search Console key, the URL Inspection API checks whether Google has indexed the
engine's recent pages (`inspect_limit` per run, oldest unchecked first; pages already indexed are not asked
again).

Engine pages with a duplicate title are queued for a title rewrite (refresh.py picks them up). Everything else
is a site or template problem: it goes in the report and the weekly report's asks. Writes
data/<site>/health/<date>.md|json and latest.json. Plain HTTP GETs to the site's own pages; nothing is spent
except Search Console calls.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

import inventory
from config import ConfigError, get_runtime, load_config

DEFAULTS = {"sitemap_url": None, "max_urls": 300, "inspect_limit": 15, "delay_seconds": 0.2, "timeout": 15,
            "not_indexed_after_days": 14}
UA = "Mozilla/5.0 (compatible; seo-engine-health/1.0)"
INSPECT_API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
ISSUE_LABELS = {
    "error": "Did not load (network error)",
    "not_200": "Does not return 200",
    "redirect": "Listed in the sitemap but redirects",
    "redirect_chain": "Redirect chain (2+ hops)",
    "canonical_elsewhere": "Canonical points to a different URL",
    "noindex": "Marked noindex",
    "robots_blocked": "Blocked by robots.txt",
    "missing_title": "No <title>",
    "missing_description": "No meta description",
    "duplicate_title": "Title shared with another page",
    "duplicate_description": "Meta description shared with another page",
    "not_in_sitemap": "Published engine page missing from the sitemap",
    "orphan": "No internal link points to it",
    "not_indexed": "Not indexed by Google",
    "past_year": "Names a year before the content year",
}


def settings(config) -> dict:
    raw = config.get("health") or {}
    if not isinstance(raw, dict):
        raise ConfigError("health must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"health: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    for k in ("max_urls", "inspect_limit", "timeout", "not_indexed_after_days"):
        if not isinstance(s[k], int) or s[k] < 0:
            raise ConfigError(f"health.{k} must be a whole number >= 0")
    return s


def http_get(url: str, timeout: int = 15):
    """One GET without following redirects. Returns (status, headers, text) or raises requests.RequestException."""
    r = requests.get(url, timeout=timeout, allow_redirects=False, headers={"User-Agent": UA})
    ctype = r.headers.get("content-type", "")
    text = r.text if ("html" in ctype or "xml" in ctype or "text" in ctype) else ""
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, text


# ---------------------------------------------------------------- sitemaps

def norm(u: str) -> str:
    p = urlsplit(u.strip())
    host = p.netloc.lower().removeprefix("www.")
    path = p.path or "/"
    return f"{host}{path if path.endswith('/') or '.' in path.rsplit('/', 1)[-1] else path + '/'}"


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """(page URLs, child sitemap URLs)."""
    locs = [re.sub(r"<!\[CDATA\[|\]\]>", "", l).strip() for l in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, flags=re.S | re.I)]
    if re.search(r"<sitemapindex\b", xml, flags=re.I):
        return [], locs
    return locs, []


def robots(base: str, get, timeout: int) -> tuple[RobotFileParser, list[str]]:
    rp = RobotFileParser()
    try:
        status, _, text = get(urljoin(base, "/robots.txt"), timeout)
    except requests.RequestException:
        status, text = 0, ""
    rp.parse(text.splitlines() if status == 200 else [])
    maps = re.findall(r"^\s*sitemap:\s*(\S+)", text if status == 200 else "", flags=re.I | re.M)
    return rp, maps


def sitemap_urls(base: str, get, timeout: int, first: list[str], limit: int) -> tuple[list[str], list[str]]:
    """(page URLs, sitemaps read). Tries robots.txt sitemaps, then the usual paths."""
    todo = list(first) or [urljoin(base, "/sitemap-index.xml"), urljoin(base, "/sitemap_index.xml"), urljoin(base, "/sitemap.xml")]
    read, pages, tried = [], [], set()
    while todo and len(pages) < limit and len(tried) < 50:
        sm = todo.pop(0)
        if sm in tried:
            continue
        tried.add(sm)
        try:
            status, headers, text = get(sm, timeout)
            hops = 0
            while status in (301, 302, 303, 307, 308) and headers.get("location") and hops < 3:
                status, headers, text = get(urljoin(sm, headers["location"]), timeout)
                hops += 1
        except requests.RequestException:
            continue
        if status != 200 or "<loc>" not in text.lower():
            continue
        read.append(sm)
        urls, children = parse_sitemap(text)
        pages += urls
        todo += children
        if read and not children and not first:
            break  # found the site's sitemap at a default path; do not also read the other defaults
    seen, out = set(), []
    for u in pages:
        if norm(u) not in seen:
            seen.add(norm(u))
            out.append(u)
    return out[:limit], read


# ---------------------------------------------------------------- one URL

def check_url(url: str, get, timeout: int) -> dict:
    hops, cur = [], url
    try:
        status, headers, text = get(cur, timeout)
        while status in (301, 302, 303, 307, 308) and headers.get("location") and len(hops) < 6:
            nxt = urljoin(cur, headers["location"])
            hops.append({"from": cur, "to": nxt, "status": status})
            cur = nxt
            status, headers, text = get(cur, timeout)
    except requests.RequestException as e:
        return {"url": url, "error": f"{type(e).__name__}"[:80], "hops": hops}
    out = {"url": url, "final_url": cur, "status": status, "hops": hops}
    if status == 200 and text:
        soup = BeautifulSoup(text, "html.parser")
        t = soup.find("title")
        d = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        c = soup.find("link", attrs={"rel": lambda v: v and "canonical" in (v if isinstance(v, list) else [v])})
        r = soup.find("meta", attrs={"name": re.compile("^(robots|googlebot)$", re.I)})
        out.update(title=" ".join(t.get_text().split()) if t else "",
                   description=" ".join((d.get("content") or "").split()) if d else "",
                   canonical=urljoin(cur, c.get("href")) if c and c.get("href") else None,
                   noindex="noindex" in ((r.get("content") or "").lower() if r else "")
                           or "noindex" in headers.get("x-robots-tag", "").lower())
    return out


# ---------------------------------------------------------------- local pages

def engine_pages(config, today: date) -> dict:
    """Published engine pages: normalised URL -> inventory entry (with `url`)."""
    import output
    from textutil import split_frontmatter
    out = {}
    for e in inventory.load(config):
        if e.get("type") != "article" or e.get("status") != "written" or output.is_imported(e):
            continue
        f = output.page_file(config, e)
        if not f.exists():
            continue
        fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
        pub = str(fm.get("pubDate") or "")[:10]
        if fm.get("draft") is True or not pub or pub > today.isoformat():
            continue
        path = f'{(e.get("url_prefix") or config["blog_url_prefix"]).rstrip("/")}/{e["slug"]}/'
        out[norm(f"https://{config['domain']}{path}")] = {**e, "url": f"https://{config['domain']}{path}", "pub": pub}
    return out


def page_past_years(config, entry) -> list[str]:
    """Years before the content year in a page's title, description, tags, FAQ and body (not its dates or links)."""
    import output
    from textutil import content_year, past_years, split_frontmatter
    f = output.page_file(config, entry)
    if not f.exists():
        return []
    fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
    faq = " ".join(f'{q.get("question", "")} {q.get("answer", "")}' for q in fm.get("faq") or [] if isinstance(q, dict))
    parts = [fm.get("title"), fm.get("description"), " ".join(map(str, fm.get("tags") or [])),
             " ".join(map(str, fm.get("secondaryKeywords") or [])), faq, body]
    return past_years("\n".join(str(x) for x in parts if x), content_year(config))


def inbound_links(config) -> dict:
    """Normalised URL path -> number of other local pages linking to it."""
    import interlink
    counts: dict[str, int] = {}
    for p in interlink.load_posts(config):
        targets = set()
        for href in re.findall(r"\]\(([^)\s]+)", p["text"]) + re.findall(r'href="([^"]+)"', p["text"]):
            if href.startswith("/") or config["domain"] in href:
                path = urlsplit(href).path
                path = path if path.endswith("/") else path + "/"
                if path != p["url"]:
                    targets.add(path)
        for t in targets:
            counts[t] = counts.get(t, 0) + 1
    return counts


# ---------------------------------------------------------------- Search Console URL Inspection

def _index_file(config):
    return config["_paths"]["data"] / "health" / "indexing.json"


def inspect(config, token: str, url: str) -> dict:
    import gsc
    from apiclient import _request
    resp = _request(config, "gsc", "urlInspection", "POST", INSPECT_API, headers={"Authorization": f"Bearer {token}"},
                    json={"inspectionUrl": url, "siteUrl": gsc.property_id(config)})
    r = (resp.json().get("inspectionResult") or {}).get("indexStatusResult") or {}
    return {"verdict": r.get("verdict"), "coverage": r.get("coverageState"), "last_crawl": r.get("lastCrawlTime"),
            "google_canonical": r.get("googleCanonical")}


def run_inspections(config, pages: dict, s: dict, today: date) -> tuple[dict, str | None]:
    f = _index_file(config)
    state = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    todo = [p for p in pages.values() if state.get(p["url"], {}).get("verdict") != "PASS"
            and p["pub"] <= (today - timedelta(days=3)).isoformat()]
    todo.sort(key=lambda p: state.get(p["url"], {}).get("checked", ""))
    if not todo or not s["inspect_limit"]:
        return state, None
    import gsc
    try:
        token = gsc._token()
    except ConfigError as e:
        return state, str(e)
    for p in todo[: s["inspect_limit"]]:
        try:
            state[p["url"]] = {**inspect(config, token, p["url"]), "checked": today.isoformat()}
        except Exception as e:  # noqa: BLE001 - budget or API error: stop asking, keep what we have
            get_runtime(config).log("stage_error", stage="health", step="inspect", url=p["url"], error=str(e)[:200])
            break
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state, None


# ---------------------------------------------------------------- run

def run(config, get=None, today: date | None = None, inspect_index: bool = True) -> dict:
    s = settings(config)
    get = get or http_get
    today = today or date.today()
    base = f"https://{config['domain']}/"
    rp, robot_maps = robots(base, get, s["timeout"])
    first = [s["sitemap_url"]] if s["sitemap_url"] else robot_maps
    urls, read = sitemap_urls(base, get, s["timeout"], first, s["max_urls"])
    results = []
    for i, u in enumerate(urls):
        if i and s["delay_seconds"]:
            time.sleep(s["delay_seconds"])
        results.append(check_url(u, get, s["timeout"]))

    issues = []

    def add(kind, url, detail="", engine=False):
        issues.append({"issue": kind, "url": url, "detail": detail, "engine": engine})

    pages = engine_pages(config, today)
    by_title, by_desc = {}, {}
    for r in results:
        eng = norm(r["url"]) in pages
        if r.get("error"):
            add("error", r["url"], r["error"], eng)
            continue
        if r["hops"]:
            add("redirect", r["url"], f'→ {r["final_url"]} ({r["hops"][0]["status"]})', eng)
            if len(r["hops"]) >= 2:
                add("redirect_chain", r["url"], f'{len(r["hops"])} hops', eng)
        if r["status"] != 200:
            add("not_200", r["url"], str(r["status"]), eng)
            continue
        if not rp.can_fetch("Googlebot", r["final_url"]):
            add("robots_blocked", r["url"], "", eng)
        if r.get("noindex"):
            add("noindex", r["url"], "", eng)
        if r.get("canonical") and norm(r["canonical"]) != norm(r["final_url"]):
            add("canonical_elsewhere", r["url"], f'→ {r["canonical"]}', eng)
        if "title" in r and not r["title"]:
            add("missing_title", r["url"], "", eng)
        if "description" in r and not r["description"]:
            add("missing_description", r["url"], "", eng)
        if r["hops"]:
            continue  # a redirected URL is reported as a redirect; its target is checked on its own
        if r.get("title"):
            by_title.setdefault(r["title"].lower(), []).append(r)
        if r.get("description"):
            by_desc.setdefault(r["description"].lower(), []).append(r)
    queued = []
    for groups, kind in ((by_title, "duplicate_title"), (by_desc, "duplicate_description")):
        for _, rs in groups.items():
            if len(rs) < 2:
                continue
            for r in rs:
                eng = norm(r["url"]) in pages
                add(kind, r["url"], f'shared with {len(rs) - 1} other page(s): "{(r.get("title") if kind == "duplicate_title" else r.get("description"))[:70]}"', eng)
                if eng and kind == "duplicate_title":
                    import refresh
                    if refresh.enqueue(config, pages[norm(r["url"])]["slug"], "duplicate_title",
                                       duplicates=[x["title"] for x in rs]):
                        queued.append(pages[norm(r["url"])]["slug"])

    if urls:
        in_map = {norm(u) for u in urls}
        for key, p in pages.items():
            if key not in in_map:
                add("not_in_sitemap", p["url"], "", True)
    for p in pages.values():  # pages are written for the content year and name no earlier one
        if old := page_past_years(config, p):
            add("past_year", p["url"], f"names {', '.join(old)}", True)
            import refresh
            if refresh.enqueue(config, p["slug"], "past_year", years=old):
                queued.append(p["slug"])
    links = inbound_links(config)
    for p in pages.values():
        path = urlsplit(p["url"]).path
        if not links.get(path):
            add("orphan", p["url"], "no other page links here", True)

    index_state, index_error = ({}, None)
    if inspect_index and pages:
        index_state, index_error = run_inspections(config, pages, s, today)
        for p in pages.values():
            st = index_state.get(p["url"])
            old = p["pub"] <= (today - timedelta(days=s["not_indexed_after_days"])).isoformat()
            if st and st.get("verdict") not in ("PASS", None) and old:
                add("not_indexed", p["url"], st.get("coverage") or st.get("verdict"), True)

    counts: dict[str, int] = {}
    for i in issues:
        counts[i["issue"]] = counts.get(i["issue"], 0) + 1
    return {"site": config["site_name"], "generated": today.isoformat(), "sitemaps": read, "checked": len(results),
            "engine_pages": len(pages), "issues": issues, "counts": counts, "queued_retitles": queued,
            "indexed": sum(1 for p in pages.values() if index_state.get(p["url"], {}).get("verdict") == "PASS"),
            "inspected": sum(1 for p in pages.values() if p["url"] in index_state), "inspect_error": index_error}


def asks(report: dict, limit: int = 3) -> list[str]:
    """Problems a person has to fix (site or template), most damaging first."""
    order = ["not_200", "noindex", "robots_blocked", "canonical_elsewhere", "not_indexed", "not_in_sitemap",
             "redirect_chain", "redirect", "missing_title", "missing_description", "duplicate_description"]
    out = []
    for kind in order:
        rows = [i for i in report["issues"] if i["issue"] == kind]
        if rows:
            out.append(f'{ISSUE_LABELS[kind]}: {len(rows)} page(s), e.g. {urlsplit(rows[0]["url"]).path} {rows[0]["detail"]}'.strip())
    return out[:limit]


def render(r: dict) -> str:
    L = [f"# Site health: {r['site']} ({r['generated']})", "",
         f"Checked {r['checked']} URL(s) from {', '.join(r['sitemaps']) or 'no sitemap found'}. "
         f"Engine pages published: {r['engine_pages']}; indexed {r['indexed']} of {r['inspected']} inspected."
         + (f" URL Inspection skipped: {r['inspect_error']}" if r.get("inspect_error") else ""), ""]
    if not r["sitemaps"]:
        L += ["**No sitemap found** at robots.txt, /sitemap-index.xml or /sitemap.xml. Set `health.sitemap_url`.", ""]
    if r["queued_retitles"]:
        L += ["Queued for a title rewrite (duplicate titles): " + ", ".join(r["queued_retitles"]), ""]
    if not r["issues"]:
        L.append("No issues found.")
    for kind, label in ISSUE_LABELS.items():
        rows = [i for i in r["issues"] if i["issue"] == kind]
        if not rows:
            continue
        L += [f"## {label} ({len(rows)})", "", "| URL | Detail | Engine page |", "|---|---|---|"]
        L += [f"| {i['url']} | {i['detail']} | {'yes' if i['engine'] else 'no'} |" for i in rows[:50]]
        L.append("")
    return "\n".join(L) + "\n"


def save(config, r: dict) -> str:
    out = config["_paths"]["data"] / "health"
    out.mkdir(parents=True, exist_ok=True)
    for name in (f"{r['generated']}.json", "latest.json"):
        (out / name).write_text(json.dumps(r, indent=2, ensure_ascii=False), encoding="utf-8")
    md = out / f"{r['generated']}.md"
    md.write_text(render(r), encoding="utf-8")
    return str(md)


def cli(config_path, inspect_index: bool = True) -> int:
    config = load_config(config_path)
    r = run(config, inspect_index=inspect_index)
    path = save(config, r)
    print(f"\n=== health: {r['site']} ===\nchecked {r['checked']} URL(s); issues: "
          + (", ".join(f"{k} {v}" for k, v in sorted(r["counts"].items())) or "none"))
    for a in asks(r):
        print(f"  ! {a}")
    print(f"report: {path}")
    return 0
