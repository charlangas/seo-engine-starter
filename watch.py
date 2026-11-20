"""Competitor watch: which pages did competitors publish since last week? Free (their public sitemaps).

  python main.py competitor-watch --config configs/site.yaml

For each domain in `competitor_domains` (and each comparison competitor with a `domain`), the sitemap is read
(robots.txt `Sitemap:` lines, else the usual paths, nested indexes followed, up to `max_urls` per domain).
The URL list is kept in data/<site>/competitors/<domain>.json; each run lists URLs that are new since the
previous run, with the slug turned into a topic phrase. The first run only records a baseline. New topics
that match the site's seeds are written to data/<site>/competitors/ideas.json, which research.py adds to the
keyword ideas it checks (they still pass every Stage 1 filter).
"""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlsplit

from config import ConfigError, load_config
from textutil import tokens

DEFAULTS = {"max_urls": 5000, "max_new_listed": 30, "domains": []}
SKIP_PATH = re.compile(r"/(tag|tags|category|categories|author|page|feed|wp-content|cdn-cgi|search|cart|account|"
                       r"login|privacy|terms|legal|careers|jobs)(/|$)|\.(xml|jpg|png|pdf|gif|webp)$", re.I)


def settings(config) -> dict:
    raw = config.get("competitor_watch") or {}
    if not isinstance(raw, dict):
        raise ConfigError("competitor_watch must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"competitor_watch: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    return {**DEFAULTS, **raw}


def domains(config) -> list[str]:
    s = settings(config)
    out = list(s["domains"]) + list(config.get("competitor_domains") or [])
    for c in ((config.get("comparisons") or {}).get("competitors") or []):
        if isinstance(c, dict) and c.get("domain"):
            out.append(c["domain"])
    seen, res = set(), []
    for d in out:
        d = d.lower().replace("https://", "").replace("http://", "").strip("/").removeprefix("www.")
        if d and d not in seen and d != config["domain"].lower().removeprefix("www."):
            seen.add(d)
            res.append(d)
    return res


def topic(url: str) -> str:
    """Last meaningful path segment as words: /blog/how-to-fix-a-leak/ -> 'how to fix a leak'."""
    segs = [s for s in urlsplit(url).path.split("/") if s and not re.fullmatch(r"\d+|[a-z]{2}(-[a-z]{2})?", s)]
    if not segs:
        return ""
    words = re.sub(r"\.(html?|php|aspx?)$", "", segs[-1]).replace("_", "-").split("-")
    return " ".join(w for w in words if w and not re.fullmatch(r"[0-9a-f]{6,}", w))


def run(config, get=None, today: date | None = None) -> dict:
    import health
    s = settings(config)
    get = get or health.http_get
    today = today or date.today()
    folder = config["_paths"]["data"] / "competitors"
    folder.mkdir(parents=True, exist_ok=True)
    seeds = [set(tokens(k)) for k in config.get("seed_keywords") or []]
    out = {"site": config["site_name"], "generated": today.isoformat(), "domains": []}
    ideas = []
    for d in domains(config):
        base = f"https://{d}/"
        _, maps = health.robots(base, get, 15)
        urls, read = health.sitemap_urls(base, get, 15, maps, s["max_urls"])
        urls = [u for u in urls if not SKIP_PATH.search(urlsplit(u).path)]
        f = folder / f"{d}.json"
        prev = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        row = {"domain": d, "sitemaps": read, "urls": len(urls), "baseline": prev is None, "new": []}
        if prev is not None and urls:
            old = set(prev.get("urls") or [])
            new = [u for u in urls if u not in old]
            row["new"] = [{"url": u, "topic": topic(u)} for u in new]
            for n in row["new"]:
                t = set(tokens(n["topic"]))
                if len(t) >= 2 and (not seeds or any(len(t & sd) >= max(1, len(sd) // 2) for sd in seeds)):
                    ideas.append({"keyword": n["topic"], "source": d, "url": n["url"], "found": today.isoformat()})
        if urls:  # keep the old list when the sitemap could not be read this time
            f.write_text(json.dumps({"checked": today.isoformat(), "urls": urls}, indent=1), encoding="utf-8")
        out["domains"].append(row)
    if ideas:
        idea_file = folder / "ideas.json"
        prev_ideas = json.loads(idea_file.read_text(encoding="utf-8")) if idea_file.exists() else []
        have = {i["keyword"] for i in prev_ideas}
        idea_file.write_text(json.dumps((prev_ideas + [i for i in ideas if i["keyword"] not in have])[-200:], indent=1),
                             encoding="utf-8")
    out["ideas"] = ideas
    return out


def research_ideas(config, limit: int = 30) -> list[str]:
    """Topics competitors published recently, for Stage 1 (cheap: the keyword still goes through every filter)."""
    f = config["_paths"]["data"] / "competitors" / "ideas.json"
    try:
        return [i["keyword"] for i in json.loads(f.read_text(encoding="utf-8"))][-limit:] if f.exists() else []
    except ValueError:
        return []


def render(r: dict, max_listed: int = 30) -> str:
    L = [f"# Competitor watch: {r['site']} ({r['generated']})", ""]
    for d in r["domains"]:
        if not d["sitemaps"]:
            L += [f"## {d['domain']}", "", "No sitemap found.", ""]
            continue
        if d["baseline"]:
            L += [f"## {d['domain']}", "", f"Baseline recorded: {d['urls']} URLs. New pages show from the next run.", ""]
            continue
        L += [f"## {d['domain']}: {len(d['new'])} new page(s)", ""]
        L += [f"- {n['topic'] or '(no slug)'}: {n['url']}" for n in d["new"][:max_listed]]
        L.append("")
    if r["ideas"]:
        L += ["## Added to the keyword ideas", ""] + [f"- {i['keyword']} ({i['source']})" for i in r["ideas"]]
    return "\n".join(L) + "\n"


def cli(config_path) -> int:
    config = load_config(config_path)
    r = run(config)
    out = config["_paths"]["data"] / "competitors" / f"report-{r['generated']}.md"
    out.write_text(render(r, settings(config)["max_new_listed"]), encoding="utf-8")
    print(render(r, settings(config)["max_new_listed"]))
    print(f"report: {out}")
    return 0
