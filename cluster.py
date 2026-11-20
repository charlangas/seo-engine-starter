"""Group keywords by SERP overlap, not word similarity ("do the same pages rank for both?").

  python main.py page-map --config configs/site.yaml [--fetch N]

Two keywords belong on one page when their Google top 10 share at least `min_shared_urls` URLs (default 3).
Tie-break: one URL short of that (2 by default) counts as the same page only when both SERPs rank the same
kind of page (serpclass: both service pages, both list articles...); otherwise they get separate pages.
Word similarity is a poor proxy: "plumber cost" and "plumber prices" share a SERP but few letters, while
"water heater repair" and "water heater replacement" look alike and rank different pages.

- Stage 1 (research.py) uses cached SERPs only, so it costs nothing: a candidate whose SERP matches a page the
  site already has is rejected as `same_serp_as_existing` and recorded as a secondary keyword for that page;
  two candidates that only *look* alike but whose cached SERPs clearly differ are both kept.
- `page-map` writes data/<site>/page_map/<date>.md|json: every keyword -> the URL it lives on -> why
  (shared URLs, or "own page"). `--fetch N` buys SERPs (SerpAPI) for up to N keywords that have none cached.
- With a Search Console report, each "same query, several pages" conflict becomes a suggestion: merge and
  301-redirect when the pages' SERPs overlap (one intent), or differentiate their titles when they don't.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import inventory
from config import ConfigError, get_runtime, load_config
from textutil import normalize_keyword

DEFAULTS = {"min_shared_urls": 3, "fetch_candidates": 0}


def settings(config) -> dict:
    raw = config.get("serp_clustering") or {}
    if not isinstance(raw, dict):
        raise ConfigError("serp_clustering must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"serp_clustering: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    if not isinstance(s["min_shared_urls"], int) or not 1 <= s["min_shared_urls"] <= 10:
        raise ConfigError("serp_clustering.min_shared_urls must be 1-10")
    if not isinstance(s["fetch_candidates"], int) or s["fetch_candidates"] < 0:
        raise ConfigError("serp_clustering.fetch_candidates must be 0 or more")
    return s


def norm_url(u: str) -> str:
    p = urlsplit(u.strip().lower())
    host = p.netloc[4:] if p.netloc.startswith("www.") else p.netloc
    return host + (p.path.rstrip("/") or "/")


def cache_file(keyword: str, config) -> Path:
    loc = config.get("location") or config["country"]
    key = hashlib.sha1(f"{keyword.lower().strip()}|{loc.lower()}".encode()).hexdigest()
    return config["_paths"]["cache"] / "serp" / f"{key}.json"


def cached_urls(keyword: str, config, memo: dict | None = None) -> set[str] | None:
    """Top-10 URLs from the SERP cache, whatever its age. None when never fetched. Never spends."""
    if memo is not None and keyword in memo:
        return memo[keyword]
    f = cache_file(keyword, config)
    urls = None
    if f.exists():
        try:
            urls = {norm_url(r["url"]) for r in json.loads(f.read_text(encoding="utf-8")).get("organic", [])[:10]
                    if r.get("url")}
        except (ValueError, KeyError):
            urls = None
    if memo is not None:
        memo[keyword] = urls
    return urls


def shared(a: set | None, b: set | None) -> int | None:
    if a is None or b is None:
        return None
    return len(a & b)


def cached_format(keyword: str, config, memo: dict | None = None) -> str | None:
    """Dominant page format of the cached SERP (serpclass), or None when not cached."""
    key = ("__fmt__", keyword)
    if memo is not None and key in memo:
        return memo[key]
    import serpclass
    f = cache_file(keyword, config)
    fmt = None
    if f.exists():
        try:
            fmt = serpclass.summarize(json.loads(f.read_text(encoding="utf-8")).get("organic", []))["format"]
        except (ValueError, KeyError):
            fmt = None
    if memo is not None:
        memo[key] = fmt
    return fmt


def match(a: str, b: str, config, memo: dict, threshold: int) -> int | None:
    """Shared-URL count when keywords a and b belong on one page, else None (see the tie-break above)."""
    k = shared(cached_urls(a, config, memo), cached_urls(b, config, memo))
    if k is None:
        return None
    if k >= threshold:
        return k
    if threshold >= 3 and k == threshold - 1:
        fa, fb = cached_format(a, config, memo), cached_format(b, config, memo)
        if fa and fa == fb:
            return k
    return None


def own_urls(urls: set[str], config) -> set[str]:
    d = config["domain"].lower().removeprefix("www.")
    return {u for u in urls if u.split("/", 1)[0] == d}


# ---------------------------------------------------------------- Stage 1 hooks

class Checker:
    """Cache-only SERP comparisons for research.py."""

    def __init__(self, config, entries):
        self.config, self.s, self.memo = config, settings(config), {}
        self.pages = [e for e in entries if e.get("status") == "written" and e.get("primary_keyword")]

    def urls(self, kw):
        return cached_urls(kw, self.config, self.memo)

    def same_serp_page(self, kw) -> tuple[dict | None, int]:
        mine = self.urls(kw)
        if mine is None:
            return None, 0
        best, n = None, 0
        for e in self.pages:
            k = match(kw, e["primary_keyword"], self.config, self.memo, self.s["min_shared_urls"])
            if k is not None and k > n:
                best, n = e, k
        return best, n

    def different_serps(self, a, b) -> bool:
        """True only when both SERPs are cached and clearly differ (fewer than half the threshold shared)."""
        k = shared(self.urls(a), self.urls(b))
        return k is not None and k < max(1, self.s["min_shared_urls"] // 2 + (self.s["min_shared_urls"] % 2))

    def same_serp(self, a, b) -> int | None:
        return match(a, b, self.config, self.memo, self.s["min_shared_urls"])


def record_secondary(config, entry: dict, keyword: str, n: int) -> None:
    """Remember that `keyword` belongs on an existing page (feeds page-map and the next refresh)."""
    if get_runtime(config).dry_run:
        return
    cur = list(entry.get("serp_cluster") or [])
    if keyword.lower() not in [c["keyword"] for c in cur]:
        cur.append({"keyword": keyword.lower(), "shared_urls": n})
        inventory.add_entry(config, entry["primary_keyword"], entry["slug"], type=entry.get("type", "article"),
                            status=entry["status"], serp_cluster=cur)


# ---------------------------------------------------------------- page map

def _candidates(config) -> list[dict]:
    work = config["_paths"]["work"]
    out = []
    for name in ("ranked_keywords.json",):
        f = work / name
        if f.exists():
            out += [{"keyword": r["keyword"], "volume": r.get("volume")} for r in json.loads(f.read_text())]
    f = work / "comparison_keywords.json"
    if f.exists():
        out += [{"keyword": r["keyword"], "volume": r.get("volume")} for r in json.loads(f.read_text()).get("ranked", [])]
    return out


def page_url(config, e: dict) -> str:
    prefix = e.get("url_prefix") or e.get("url_prefix_override") or config.get("blog_url_prefix", "/blog")
    return f"{prefix.rstrip('/')}/{e['slug']}/"


def build_map(config, fetch: int = 0) -> dict:
    s = settings(config)
    memo: dict = {}
    entries = [e for e in inventory.load(config) if e.get("status") == "written" and e.get("slug")]
    cands = _candidates(config)
    if fetch:
        import serp
        missing = [c["keyword"] for c in sorted(cands, key=lambda c: -(c.get("volume") or 0))
                   if cached_urls(c["keyword"], config) is None][:fetch]
        missing += [e["primary_keyword"] for e in entries if cached_urls(e["primary_keyword"], config) is None][:fetch]
        for kw in missing[:fetch]:
            try:
                serp.fetch_serp(kw, config)
            except Exception as ex:  # noqa: BLE001
                get_runtime(config).log("stage_error", stage="page_map_fetch", keyword=kw, error=str(ex))
    pages = []
    for e in entries:
        pages.append({"slug": e["slug"], "url": page_url(config, e), "primary_keyword": e["primary_keyword"],
                      "serp_cached": cached_urls(e["primary_keyword"], config, memo) is not None,
                      "keywords": [{"keyword": e["primary_keyword"], "why": "primary keyword"}]
                      + [{"keyword": c["keyword"], "why": f"{c['shared_urls']} shared top-10 URLs (recorded in research)"}
                         for c in e.get("serp_cluster") or []]})
    by_slug = {p["slug"]: p for p in pages}
    seen = {normalize_keyword(k["keyword"]) for p in pages for k in p["keywords"]}
    unmapped, new_pages = [], []
    for c in cands:
        kw = c["keyword"]
        if normalize_keyword(kw) in seen:
            continue
        seen.add(normalize_keyword(kw))
        mine = cached_urls(kw, config, memo)
        if mine is None:
            unmapped.append({"keyword": kw, "why": "no SERP cached (run with --fetch N)"})
            continue
        best, n = None, 0
        for e in entries:
            k = match(kw, e["primary_keyword"], config, memo, s["min_shared_urls"])
            if k is not None and k > n:
                best, n = e, k
        if best:
            by_slug[best["slug"]]["keywords"].append({"keyword": kw, "why": f"{n} shared top-10 URLs with "
                                                                            f"\"{best['primary_keyword']}\""})
            continue
        group = next((g for g in new_pages if match(kw, g["primary_keyword"], config, memo, s["min_shared_urls"])), None)
        if group:
            group["keywords"].append({"keyword": kw, "why": f"{shared(mine, cached_urls(group['primary_keyword'], config, memo))}"
                                                            f" shared top-10 URLs with \"{group['primary_keyword']}\""})
        else:
            new_pages.append({"primary_keyword": kw, "url": "(not written)", "keywords": [{"keyword": kw, "why": "own page: no "
                              "existing page shares its SERP"}]})
    return {"site": config["site_name"], "generated": date.today().isoformat(), "min_shared_urls": s["min_shared_urls"],
            "pages": pages, "planned_pages": new_pages, "unmapped": unmapped, "conflicts": conflicts(config, entries, s, memo)}


def _slug_from_path(path: str) -> str:
    parts = [p for p in urlsplit(path).path.split("/") if p]
    return parts[-1] if parts else ""


def conflicts(config, entries, s, memo) -> list[dict]:
    """Search Console 'same query, several pages' -> merge + 301 when one intent, else differentiate."""
    f = config["_paths"]["data"] / "gsc" / "latest.json"
    if not f.exists():
        return []
    try:
        report = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []
    by_slug = {e["slug"]: e for e in entries}
    out = []
    for c in report.get("cannibalisation") or []:
        ps = c.get("pages") or []
        if len(ps) < 2:
            continue
        keep, other = ps[0], ps[1]  # most impressions first
        ek, eo = by_slug.get(_slug_from_path(keep["page"])), by_slug.get(_slug_from_path(other["page"]))
        n = None
        if ek and eo:
            n = shared(cached_urls(ek["primary_keyword"], config, memo), cached_urls(eo["primary_keyword"], config, memo))
        if keep.get("position") and other.get("position") and other["position"] < keep["position"] - 3:
            keep, other = other, keep  # a clearly better-ranking page wins over raw impressions
        if n is not None and n >= s["min_shared_urls"]:
            action, why = "merge", f"their primary keywords share {n} top-10 URLs: one intent, one page"
        elif n is None:
            action, why = "review", "SERPs not cached for both pages; compare them by hand (or run page-map --fetch)"
        else:
            action, why = "differentiate", (f"their primary keywords share only {n} top-10 URLs: different intents. "
                                            "Retitle the weaker page around its own keyword and link it to the stronger one")
        row = {"query": c["query"], "impressions": c.get("impressions"), "keep": keep["page"], "other": other["page"],
               "action": action, "why": why}
        if action == "merge":
            row["redirect"] = f"{other['page']} {keep['page']} 301"
        out.append(row)
    return out


def render(m: dict) -> str:
    L = [f"# Page map: {m['site']}", "", f"Generated {m['generated']}. Keywords share a page when their Google top 10 "
         f"share at least {m['min_shared_urls']} URLs.", ""]
    if m["conflicts"]:
        L += ["## Search Console conflicts", "", "| query | keep | other | action | why |", "|---|---|---|---|---|"]
        L += [f"| {c['query']} | {c['keep']} | {c['other']} | **{c['action']}** | {c['why']} |" for c in m["conflicts"]] + [""]
        red = [c["redirect"] for c in m["conflicts"] if c.get("redirect")]
        if red:
            L += ["Redirects to add after merging the content (Netlify/Vercel `_redirects` format):", "", "```"] + red + ["```", ""]
    L += ["## Pages", ""]
    for p in m["pages"]:
        L += [f"### {p['url']}" + ("" if p["serp_cached"] else " (SERP not cached)"), ""]
        L += [f"- {k['keyword']}: {k['why']}" for k in p["keywords"]] + [""]
    if m["planned_pages"]:
        L += ["## Keywords that need their own page", ""]
        for g in m["planned_pages"]:
            L += [f"- **{g['primary_keyword']}**" + ("" if len(g["keywords"]) == 1 else
                  " (same page: " + "; ".join(k["keyword"] for k in g["keywords"][1:]) + ")")]
        L.append("")
    if m["unmapped"]:
        L += [f"## Not mapped ({len(m['unmapped'])}: no SERP cached)", ""] + [f"- {u['keyword']}" for u in m["unmapped"][:40]] + [""]
    return "\n".join(L)


def cli(config_path, fetch: int = 0) -> int:
    config = load_config(config_path, dry_run=False)
    m = build_map(config, fetch)
    out = config["_paths"]["data"] / "page_map"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{m['generated']}.json").write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    md = render(m)
    (out / f"{m['generated']}.md").write_text(md, encoding="utf-8")
    print(md[:4000])
    print(f"Report: {out / (m['generated'] + '.md')}")
    return 0
