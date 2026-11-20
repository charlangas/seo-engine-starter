"""AI visibility: are we cited in AI answers for the questions our buyers ask?

  python main.py ai-visibility --config configs/site.yaml [--dry-run]

For each prompt in `ai_visibility.prompts` (the questions a buyer types into ChatGPT or Google), the check asks:
- Google AI Overview: DataForSEO SERP advanced, the `ai_overview` item's references.
- ChatGPT (web search on): DataForSEO AI Optimization LLM responses, the answer text and its cited URLs.
One DataForSEO call per prompt per engine. It records whether our domain is cited, whether the brand is named
in the answer, and which URLs are cited instead. Results go to data/<site>/ai_visibility/<date>.md|json and
latest.json, with what was gained and lost since the previous check. Run it weekly.

Briefs use latest.json: when a prompt matches the page's keyword, the URLs AI answers cite for it are listed as
the pages to beat (what they cover, more specifically and with first-party detail).
"""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlsplit

from apiclient import ApiError, dataforseo_post
from config import ConfigError, get_runtime, load_config
from textutil import jaccard, tokens

ENGINES = ("google_ai_overview", "chatgpt")
DEFAULTS = {"prompts": [], "engines": list(ENGINES), "brand_terms": [], "chatgpt_model": "gpt-4o-mini",
            "max_prompts": 25}


def settings(config) -> dict:
    raw = config.get("ai_visibility") or {}
    if not isinstance(raw, dict):
        raise ConfigError("ai_visibility must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"ai_visibility: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    if not isinstance(s["prompts"], list) or not all(isinstance(p, str) and p.strip() for p in s["prompts"]):
        raise ConfigError("ai_visibility.prompts must be a list of questions")
    if set(s["engines"]) - set(ENGINES):
        raise ConfigError(f"ai_visibility.engines must be from {ENGINES}")
    if not isinstance(s["max_prompts"], int) or s["max_prompts"] < 1:
        raise ConfigError("ai_visibility.max_prompts must be a positive whole number")
    if not s["brand_terms"]:
        s["brand_terms"] = [config["site_name"]] + ([config["comparisons"]["brand"]]
                                                    if (config.get("comparisons") or {}).get("brand") else [])
    return s


def _domain(u: str) -> str:
    return urlsplit(u if "://" in u else "https://" + u).netloc.lower().removeprefix("www.")


def _urls(obj) -> list[str]:
    """Every http(s) URL in a nested DataForSEO result, in order, de-duplicated."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("url", "source_url", "link") and isinstance(v, str) and v.startswith("http"):
                    found.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    seen, out = set(), []
    for u in found:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _texts(obj) -> str:
    parts = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("text", "markdown", "content", "answer") and isinstance(v, str):
                    parts.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return "\n".join(parts)


def google_ai_overview(config, prompt: str) -> dict:
    res = dataforseo_post(config, "serp/google/organic/live/advanced", {
        "keyword": prompt, "location_name": config["country"], "language_code": config["language_code"],
        "depth": 10, "load_async_ai_overview": True})
    items = [it for r in res for it in (r.get("items") or [])]
    ov = [it for it in items if it.get("type") == "ai_overview"]
    organic = [it.get("url") for it in items if it.get("type") == "organic" and it.get("url")]
    return {"present": bool(ov), "urls": _urls(ov), "text": _texts(ov), "organic": organic[:10]}


def chatgpt(config, prompt: str, model: str) -> dict:
    res = dataforseo_post(config, "ai_optimization/chat_gpt/llm_responses/live",
                          {"user_prompt": prompt, "model_name": model, "web_search": True})
    return {"present": bool(res), "urls": _urls(res), "text": _texts(res)}


def check(config, prompt: str, engine: str, s: dict) -> dict:
    own = config["domain"].lower().removeprefix("www.")
    try:
        r = google_ai_overview(config, prompt) if engine == "google_ai_overview" else chatgpt(config, prompt, s["chatgpt_model"])
    except ApiError as e:
        get_runtime(config).log("stage_error", stage="ai_visibility", prompt=prompt, engine=engine, error=str(e))
        return {"prompt": prompt, "engine": engine, "error": str(e)[:200]}
    cited = [u for u in r["urls"] if _domain(u) == own or _domain(u).endswith("." + own)]
    mentioned = any(re.search(r"\b" + re.escape(b) + r"\b", r["text"], re.I) for b in s["brand_terms"] if b)
    others = [u for u in r["urls"] if u not in cited]
    out = {"prompt": prompt, "engine": engine, "answer_present": r["present"], "cited": bool(cited), "cited_urls": cited,
           "mentioned": mentioned, "competitor_urls": others[:10]}
    if "organic" in r:
        out["organic_rank"] = next((i + 1 for i, u in enumerate(r["organic"]) if _domain(u).endswith(own)), None)
    return out


def run(config, today: date | None = None) -> dict:
    s = settings(config)
    today = today or date.today()
    rows = [check(config, p, e, s) for p in s["prompts"][: s["max_prompts"]] for e in s["engines"]]
    ok = [r for r in rows if "error" not in r]
    domains: dict[str, int] = {}
    for r in ok:
        for u in r["competitor_urls"]:
            domains[_domain(u)] = domains.get(_domain(u), 0) + 1
    prev = _load_latest(config)
    before = {(r["prompt"], r["engine"]): r for r in (prev or {}).get("results", []) if "error" not in r}
    gained = [f"{r['engine']}: {r['prompt']}" for r in ok if r["cited"] and not before.get((r["prompt"], r["engine"]), {}).get("cited", False)
              and (r["prompt"], r["engine"]) in before]
    lost = [f"{r['engine']}: {r['prompt']}" for r in ok if not r["cited"] and before.get((r["prompt"], r["engine"]), {}).get("cited")]
    return {"site": config["site_name"], "generated": today.isoformat(), "results": rows,
            "cited": sum(r["cited"] for r in ok), "mentioned": sum(r["mentioned"] for r in ok), "checked": len(ok),
            "top_cited_domains": sorted(domains.items(), key=lambda kv: -kv[1])[:15], "gained": gained, "lost": lost,
            "previous": (prev or {}).get("generated")}


def _load_latest(config) -> dict | None:
    f = config["_paths"]["data"] / "ai_visibility" / "latest.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except ValueError:
        return None


def render(v: dict) -> str:
    L = [f"# AI visibility: {v['site']} ({v['generated']})", "",
         f"Cited in {v['cited']} of {v['checked']} answers; brand named in {v['mentioned']}."
         + (f" Previous check: {v['previous']}." if v.get("previous") else ""), ""]
    if v["gained"] or v["lost"]:
        L += ["## Changes since the last check", ""] + [f"- Gained: {g}" for g in v["gained"]] + [f"- Lost: {x}" for x in v["lost"]] + [""]
    L += ["## Answers", "", "| prompt | engine | cited | named | cited instead |", "|---|---|---|---|---|"]
    for r in v["results"]:
        if "error" in r:
            L.append(f"| {r['prompt']} | {r['engine']} | error | | {r['error'][:60]} |")
            continue
        L.append(f"| {r['prompt']} | {r['engine']} | {'yes' if r['cited'] else 'no'} | {'yes' if r['mentioned'] else 'no'} | "
                 + ", ".join(_domain(u) for u in r["competitor_urls"][:4]) + " |")
    if v["top_cited_domains"]:
        L += ["", "## Sites AI answers cite most", ""] + [f"- {d}: {n}" for d, n in v["top_cited_domains"]]
    return "\n".join(L) + "\n"


def cli(config_path, dry_run: bool = False) -> int:
    config = load_config(config_path, dry_run=dry_run)
    s = settings(config)
    if not s["prompts"]:
        print("ai_visibility.prompts is empty: add the questions your buyers ask AI assistants to the site config.")
        return 1
    if dry_run:
        print(f"Would check {min(len(s['prompts']), s['max_prompts'])} prompt(s) on {', '.join(s['engines'])}: "
              f"{min(len(s['prompts']), s['max_prompts']) * len(s['engines'])} DataForSEO call(s).")
        return 0
    v = run(config)
    out = config["_paths"]["data"] / "ai_visibility"
    out.mkdir(parents=True, exist_ok=True)
    for name in (f"{v['generated']}.json", "latest.json"):
        (out / name).write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")
    md = render(v)
    (out / f"{v['generated']}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


# ---------------------------------------------------------------- briefs

def cited_for(config, keyword: str, limit: int = 5) -> list[dict]:
    """URLs AI answers cite (not ours) for prompts about this keyword, from the latest check."""
    v = _load_latest(config)
    if not v:
        return []
    kw = set(tokens(keyword))
    out, seen = [], set()
    for r in v.get("results", []):
        if "error" in r:
            continue
        pt = set(tokens(r["prompt"]))
        if not kw or not (kw <= pt or jaccard(kw, pt) >= 0.5):
            continue
        for u in r["competitor_urls"]:
            if u not in seen:
                seen.add(u)
                out.append({"url": u, "prompt": r["prompt"], "engine": r["engine"]})
    return out[:limit]


def attach(brief: dict, config: dict) -> list[dict]:
    if "ai_cited" not in brief:
        try:
            brief["ai_cited"] = cited_for(config, brief["primary_keyword"])
        except Exception:  # noqa: BLE001 - optional input
            brief["ai_cited"] = []
    return brief["ai_cited"]


def prompt_block(brief: dict) -> str:
    cited = brief.get("ai_cited") or []
    if not cited:
        return ""
    L = ["", "", "AI answers (ChatGPT, Google AI Overviews) currently cite these pages for related questions. Cover "
                 "what they cover, more specifically, with a direct answer and first-party detail, so this page is "
                 "the better source to cite (do not copy or link them):"]
    L += [f'- {c["url"]} (for "{c["prompt"]}", {c["engine"]})' for c in cited]
    return "\n".join(L)
