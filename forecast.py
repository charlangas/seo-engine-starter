"""Revenue forecast and funnel classification ("money pages first").

  python main.py forecast --config configs/site.yaml [--traffic-potential 20]

Reverse-engineers what ranking could be worth: for every keyword the engine has found or written,
monthly searches -> clicks in the top 3 -> leads -> customers -> revenue, then a 12-month projection
against monthly spend. It reads local files only (ranked_keywords.json, comparison_keywords.json,
inventory.json), so it costs nothing unless --traffic-potential asks DataForSEO for the traffic the
current #1 page gets (one call, plus SerpAPI only for keywords with no cached SERP).

These are projections to decide whether a bet is worth making, not predictions. Every input that came
from a default rather than the site config is listed in the report.

`funnel_stage` is also what money-pages mode in research.py uses: until `money_pages.target` bottom-of-funnel
pages exist, Stage 1 only selects bottom-of-funnel keywords.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import inventory
from config import ConfigError, get_runtime, load_config
from textutil import normalize_keyword

# Assumptions, all overridable under `forecast:` in the site config. The report flags which ones were not set.
DEFAULTS = {
    "volume_correction": 1.5,      # keyword tools under-report real searches; 1.0 turns the correction off
    "ctr_top3": 0.18,              # average click-through across positions 1-3 (assumption; replace with your own)
    "ctr_position1": 0.28,         # used to scale a #1 page's traffic down to the top-3 average
    "conversion_rate": {           # visit -> lead (or sale, with close_rate 1.0)
        "comparison": 0.05, "bottom": 0.03, "middle": 0.01, "top": 0.002},
    "close_rate": 0.15,            # lead -> customer; 1.0 when a conversion is already a sale
    "customer_value": None,        # first-year revenue per customer
    "lifetime_value": None,        # revenue over the customer's lifetime
    "gross_margin": 1.0,
    "monthly_spend": None,         # what the site spends on SEO per month
    "share_ranked": 0.3,           # share of the keywords expected to reach the top 3
    "months_to_rank": 6,           # linear ramp from 0 to full traffic
    "months": 12,
    "scenarios": [0.1, 0.3, 0.5],  # alternative share_ranked values shown side by side
    "conversions_csv": None,       # GA4 export (page, sessions, conversions); default data/<site>/conversions.csv
    "min_sessions": 200,           # sessions a page kind needs before its observed rate replaces the default
}
BUSINESS_INPUTS = ["customer_value", "lifetime_value", "monthly_spend", "conversion_rate", "close_rate", "gross_margin"]
MONEY_DEFAULTS = {"enabled": False, "target": 20,
                  "bottom_terms": []}   # extra buying words for this site, e.g. city names for a local shop

# ---------- funnel stage ----------

_BOTTOM = {
    "en": [r"\balternatives?\b", r"\bvs\.?\b", r"\bversus\b", r"\breviews?\b", r"\bbest\b", r"\btop \d+\b",
           r"\bpric(e|es|ing)\b", r"\bcosts?\b", r"\bquotes?\b", r"\bnear me\b", r"\bhire\b", r"\bagenc(y|ies)\b",
           r"\bcompan(y|ies)\b", r"\bservices?\b", r"\bconsultants?\b", r"\bfirms?\b", r"\bsoftware\b", r"\btools?\b",
           r"\bproviders?\b", r"\bcompared?\b", r"\bcomparison\b", r"\bbuy\b", r"\bfor (?!example\b)\w+",
           r"\b(switch|migrate|export|move|cancel)\w* (from|out of|my)\b"],
    "es": [r"\balternativas?\b", r"\bvs\.?\b", r"\bopiniones\b", r"\bson buen[oa]s\b", r"\bmejor(es)?\b",
           r"\bprecios?\b", r"\bcostos?\b", r"\bcotiza", r"\bcerca de m[ií]\b", r"\bventa\b", r"\bcomprar\b",
           r"\bdonde comprar\b", r"\bagencias?\b", r"\bempresas?\b", r"\bservicios?\b", r"\bpara \w+",
           r"\bcomparativa\b"],
}
_SWITCH = r"^how (to|do i|can i) (switch|migrate|export|move|cancel|transfer)\b"
_QUESTION = {
    "en": r"^(what|why|how|when|who|where|which|is|are|does|do|can|should|will)\b",
    "es": r"^(qu[eé]|c[oó]mo|cu[aá]ndo|por qu[eé]|cu[aá]l(es)?|qui[eé]n|d[oó]nde|para qu[eé])\b",
}


def funnel_stage(keyword: str, intent: str | None = None, page_type: str | None = None, lang: str = "en",
                 extra=()) -> str:
    """'bottom' (ready to buy), 'middle' or 'top' (research). Comparison pages are always bottom.
    `extra`: site-specific buying words (money_pages.bottom_terms), matched as plain substrings."""
    kw = keyword.lower().strip()
    if page_type:
        return "bottom"
    if re.search(_SWITCH, kw):
        return "bottom"  # someone exporting their data is switching products
    langs = ["en", "es"] if lang == "es" else ["en"]
    if any(re.search(_QUESTION[l], kw) for l in langs):
        # "best" or "vs" inside a question is still a buyer ("which crm is best for agencies")
        return "middle" if any(re.search(p, kw) for l in langs for p in _BOTTOM[l][:6]) else "top"
    if any(re.search(p, kw) for l in langs for p in _BOTTOM[l]) or any(t.lower() in kw for t in extra):
        return "bottom"
    if intent == "transactional":
        return "bottom"
    if intent == "navigational":
        return "top"
    return "middle"


def stage_for(config, keyword: str, intent=None, page_type=None) -> str:
    """funnel_stage with the site's own buying words: money_pages.bottom_terms plus the site's city, since
    "<service> <city>" is someone hiring locally."""
    extra = list((config.get("money_pages") or {}).get("bottom_terms") or [])
    city = str(config.get("location") or "").split(",")[0].strip().lower()
    if city:
        extra.append(city)
    return funnel_stage(keyword, intent, page_type, config.get("language_code", "en"), extra)


def kind(stage: str, page_type: str | None) -> str:
    return "comparison" if page_type else stage


# ---------- settings ----------

def settings(config) -> dict:
    raw = config.get("forecast") or {}
    if not isinstance(raw, dict):
        raise ConfigError("forecast must be a mapping")
    s = json.loads(json.dumps(DEFAULTS))
    s["_set"] = sorted(raw)
    for k, v in raw.items():
        if k not in DEFAULTS:
            raise ConfigError(f"forecast: unknown setting '{k}'; use {sorted(DEFAULTS)}")
        if k == "conversion_rate":
            if not isinstance(v, dict) or any(x not in DEFAULTS["conversion_rate"] for x in v):
                raise ConfigError(f"forecast.conversion_rate keys must be {sorted(DEFAULTS['conversion_rate'])}")
            s[k].update(v)
        else:
            s[k] = v
    for k in ("ctr_top3", "ctr_position1", "close_rate", "gross_margin", "share_ranked"):
        if not isinstance(s[k], (int, float)) or not 0 < s[k] <= 1:
            raise ConfigError(f"forecast.{k} must be a number above 0 and at most 1")
    for k in ("customer_value", "lifetime_value", "monthly_spend"):
        if s[k] is not None and (not isinstance(s[k], (int, float)) or s[k] < 0):
            raise ConfigError(f"forecast.{k} must be a non-negative number")
    if not isinstance(s["months_to_rank"], int) or not 0 <= s["months_to_rank"] <= s["months"]:
        raise ConfigError("forecast.months_to_rank must be a whole number between 0 and forecast.months")
    return s


def money_settings(config) -> dict:
    raw = config.get("money_pages") or {}
    if not isinstance(raw, dict):
        raise ConfigError("money_pages must be a mapping")
    bad = [k for k in raw if k not in MONEY_DEFAULTS]
    if bad:
        raise ConfigError(f"money_pages: unknown setting(s) {bad}; use {sorted(MONEY_DEFAULTS)}")
    s = {**MONEY_DEFAULTS, **raw}
    if not isinstance(s["target"], int) or s["target"] < 1:
        raise ConfigError("money_pages.target must be a positive integer")
    return s


# ---------- per-keyword economics ----------

def monthly(item: dict, s: dict, lang: str = "en") -> dict:
    """Monthly clicks, leads, customers and revenue once the keyword ranks in the top 3.
    Uses item['funnel'] when set (callers with a config set it through stage_for)."""
    stage = item.get("funnel") or funnel_stage(item["keyword"], item.get("intent"), item.get("page_type"), lang)
    k = kind(stage, item.get("page_type"))
    tp = item.get("traffic_potential")
    if tp:
        clicks = tp * s["volume_correction"] * s["ctr_top3"] / s["ctr_position1"]
        basis = "traffic_potential"
    else:
        clicks = (item.get("volume") or 0) * s["volume_correction"] * s["ctr_top3"]
        basis = "volume"
    leads = clicks * s["conversion_rate"][k]
    customers = leads * s["close_rate"]
    out = {"funnel": stage, "kind": k, "basis": basis, "clicks": round(clicks, 1), "leads": round(leads, 2),
           "customers": round(customers, 3)}
    if s["customer_value"] is not None:
        out["revenue"] = round(customers * s["customer_value"], 2)
    return out


def money_score(item: dict, s: dict, lang: str = "en") -> float:
    """Expected leads per month once ranked, discounted by difficulty. Orders money-pages mode."""
    return round(monthly(item, s, lang)["leads"] * 100 / ((item.get("kd") or 0) + 1), 3)


# ---------- the keyword pool ----------

def _status_of(config) -> dict:
    return {normalize_keyword(e["primary_keyword"]): e for e in inventory.load(config)}


def pool(config) -> list[dict]:
    """Every keyword the engine has found or written that has a volume, with its status."""
    work = Path(config["_paths"]["work"])
    data = Path(config["_paths"]["data"])
    items: dict[str, dict] = {}
    status = _status_of(config)

    def add(x, source):
        if not x.get("volume"):
            return
        key = normalize_keyword(x["keyword"])
        if key not in items and not (status.get(key) or {}).get("status") in ("removed", "excluded"):
            items[key] = {"keyword": x["keyword"], "volume": int(x["volume"]), "kd": x.get("kd"),
                          "intent": x.get("intent"), "page_type": x.get("page_type"), "source": source,
                          "traffic_potential": x.get("traffic_potential")}

    for base in {work, data}:
        f = base / "comparison_keywords.json"
        if f.exists():
            for x in json.loads(f.read_text(encoding="utf-8")).get("ranked") or []:
                add(x, "comparison_research")
        f = base / "ranked_keywords.json"
        if f.exists():
            for x in json.loads(f.read_text(encoding="utf-8")):
                add(x, "research")
    for e in inventory.load(config):
        if e.get("type", "article") == "article":
            add({"keyword": e["primary_keyword"], **e}, "inventory")
    for key, it in items.items():
        e = status.get(key)
        it["status"] = (e or {}).get("status") or "candidate"
        if e and e.get("page_type") and not it.get("page_type"):
            it["page_type"] = e["page_type"]
    return list(items.values())


def money_pages_written(config) -> list[str]:
    """Bottom-of-funnel pages that exist (written by the engine, imported, or owned by a site page)."""
    return [e["primary_keyword"] for e in inventory.load(config)
            if ((e.get("status") == "written" and e.get("type", "article") == "article")
                or e.get("status") == "owned_by_page")  # a site page (home, product, landing) already targets it
            and stage_for(config, e["primary_keyword"], e.get("intent"), e.get("page_type")) == "bottom"]


def money_mode_active(config) -> tuple[bool, int, int]:
    """(active, money pages written, target)."""
    m = money_settings(config)
    n = len(money_pages_written(config))
    return bool(m["enabled"]) and n < m["target"], n, m["target"]


# ---------- traffic potential (optional, spends) ----------

def traffic_potential(config, items: list[dict], limit: int) -> int:
    """Fill item['traffic_potential'] with the estimated monthly traffic of the current #1 page.

    Uses cached SERPs where they exist, SerpAPI otherwise (counts against max_serpapi_calls), then one
    DataForSEO Labs bulk_traffic_estimation call for all the URLs. Returns how many items got a value.
    """
    import serp
    from apiclient import ApiError, dataforseo_post
    rt = get_runtime(config)
    own = config["domain"].lower()
    tops = {}
    for it in items[:limit]:
        try:
            res = serp.fetch_serp(it["keyword"], config)
        except Exception as e:
            rt.log("stage_error", stage="traffic_potential", keyword=it["keyword"], error=f"{type(e).__name__}: {e}")
            continue
        top = next((r for r in res.get("organic") or [] if own not in (r.get("domain") or "").lower()), None)
        if top:
            tops[it["keyword"]] = top["url"]
    if not tops:
        return 0
    try:
        res = dataforseo_post(config, "dataforseo_labs/google/bulk_traffic_estimation/live", {
            "targets": sorted(set(tops.values()))[:1000], "location_name": config["country"],
            "language_code": config["language_code"], "item_types": ["organic"]})
    except ApiError as e:
        rt.log("stage_error", stage="traffic_potential", error=str(e))
        return 0
    etv = {}
    for r in res:
        for i in r.get("items") or []:
            v = (((i.get("metrics") or {}).get("organic") or {}).get("etv"))
            if i.get("target") and v is not None:
                etv[i["target"].rstrip("/")] = float(v)
    n = 0
    for it in items:
        url = tops.get(it["keyword"])
        if url and etv.get(url.rstrip("/")) is not None:
            it["traffic_potential"] = round(etv[url.rstrip("/")])
            it["top_url"] = url
            n += 1
    return n


# ---------- the projection ----------

def project(rows: list[dict], s: dict, share: float) -> dict:
    """Month-by-month totals for the bottom-of-funnel rows at a given share ranked."""
    full = {k: sum(r[k] for r in rows) * share for k in ("clicks", "leads", "customers")}
    months = []
    cum_customers = 0.0
    ramp = s["months_to_rank"]
    for m in range(1, s["months"] + 1):
        f = min(1.0, m / ramp) if ramp else 1.0
        c = full["customers"] * f
        cum_customers += c
        months.append({"month": m, "clicks": round(full["clicks"] * f), "leads": round(full["leads"] * f, 1),
                       "customers": round(c, 2)})
    out = {"share_ranked": share, "monthly_at_full": {k: round(v, 2) for k, v in full.items()},
           "months": months, "customers": round(cum_customers, 1)}
    if s["customer_value"] is not None:
        out["first_year_revenue"] = round(cum_customers * s["customer_value"])
    if s["lifetime_value"] is not None:
        out["lifetime_gross_profit"] = round(cum_customers * s["lifetime_value"] * s["gross_margin"])
    if s["monthly_spend"]:
        spend = s["monthly_spend"] * s["months"]
        out["spend"] = round(spend)
        basis = out.get("lifetime_gross_profit", out.get("first_year_revenue"))
        if basis is not None:
            out["roi"] = round((basis - spend) / spend, 2)
            out["roi_basis"] = "lifetime_gross_profit" if "lifetime_gross_profit" in out else "first_year_revenue"
    return out


def break_even(s: dict) -> dict | None:
    """Customers, leads and clicks a month needed to cover monthly spend."""
    value = s["lifetime_value"] * s["gross_margin"] if s["lifetime_value"] is not None else s["customer_value"]
    if not s["monthly_spend"] or not value:
        return None
    customers = s["monthly_spend"] / value
    leads = customers / s["close_rate"]
    return {"customers": round(customers, 2), "leads": round(leads, 1),
            "clicks_at_bottom_rate": round(leads / s["conversion_rate"]["bottom"]),
            "value_per_customer": value}


def observed_rates(config, s: dict) -> dict:
    """Visit -> conversion rates by page kind from a GA4 export, matched to inventory pages by slug.

    Accepts a GA4 "Pages and screens" or "Landing page" CSV (comment lines starting with # are skipped). Columns are
    found by name: page path / landing page, sessions (or views/users), and conversions / key events / leads."""
    import csv
    f = Path(s["conversions_csv"]).expanduser() if s.get("conversions_csv") else config["_paths"]["data"] / "conversions.csv"
    if not f.exists():
        return {}
    lines = [ln for ln in f.read_text(encoding="utf-8-sig").splitlines() if ln.strip() and not ln.startswith("#")]
    rows = list(csv.DictReader(lines))
    if not rows:
        return {}
    cols = {c.lower().strip(): c for c in rows[0]}
    pick = lambda *names: next((cols[c] for c in cols if any(n in c for n in names)), None)  # noqa: E731
    page_c, sess_c = pick("landing page", "page path", "page"), pick("sessions", "views", "users")
    conv_c = pick("key events", "conversions", "leads", "conversion")
    if not (page_c and sess_c and conv_c):
        return {}
    by_slug = {e["slug"]: e for e in inventory.load(config) if e.get("slug")}
    agg: dict[str, list[float]] = {}
    num = lambda v: float(re.sub(r"[^\d.]", "", str(v)) or 0)  # noqa: E731
    for r in rows:
        parts = [p for p in str(r.get(page_c) or "").split("?")[0].split("/") if p]
        e = by_slug.get(parts[-1]) if parts else None
        if not e:
            continue
        k = kind(stage_for(config, e["primary_keyword"], e.get("intent"), e.get("page_type")), e.get("page_type"))
        a = agg.setdefault(k, [0.0, 0.0])
        a[0] += num(r.get(sess_c))
        a[1] += num(r.get(conv_c))
    explicit = set(((config.get("forecast") or {}).get("conversion_rate") or {}))
    return {k: {"sessions": int(v[0]), "conversions": round(v[1], 1), "rate": round(v[1] / v[0], 4) if v[0] else 0.0,
                "used": v[0] >= s["min_sessions"] and k not in explicit, "file": f.name} for k, v in agg.items()}


def build(config, items: list[dict] | None = None) -> dict:
    s = settings(config)
    observed = observed_rates(config, s)
    for k, o in observed.items():
        if o["used"]:
            s["conversion_rate"][k] = o["rate"]
    lang = config.get("language_code", "en")
    items = pool(config) if items is None else items
    rows = []
    for it in items:
        it["funnel"] = stage_for(config, it["keyword"], it.get("intent"), it.get("page_type"))
        rows.append({**it, **monthly(it, s, lang), "money_score": money_score(it, s, lang)})
    rows.sort(key=lambda r: (r["funnel"] != "bottom", -r["leads"], r["keyword"]))
    bottom = [r for r in rows if r["funnel"] == "bottom"]
    active, written, target = money_mode_active(config)
    by_stage = {}
    for st in ("bottom", "middle", "top"):
        rs = [r for r in rows if r["funnel"] == st]
        by_stage[st] = {"keywords": len(rs), "volume": sum(r["volume"] for r in rs),
                        "clicks": round(sum(r["clicks"] for r in rs)), "leads": round(sum(r["leads"] for r in rs), 1)}
    return {"site": config["site_name"], "generated": date.today().isoformat(),
            "assumptions": {k: v for k, v in s.items() if k != "_set"},
            "defaults_used": sorted(k for k in DEFAULTS if k not in s["_set"]),
            "money_pages": {"enabled": money_settings(config)["enabled"], "active": active, "written": written,
                            "target": target},
            "by_stage": by_stage,
            "projection": project(bottom, s, s["share_ranked"]),
            "scenarios": [project(bottom, s, x) for x in s["scenarios"]],
            "break_even": break_even(s),
            "observed_conversion": observed,
            "keywords": rows}


# ---------- report ----------

def _money(v) -> str:
    return "n/a" if v is None else f"{v:,.0f}"


def render(f: dict) -> str:
    a, p = f["assumptions"], f["projection"]
    mp = f["money_pages"]
    L = [f"# Forecast: {f['site']} ({f['generated']})", "",
         "A projection to decide whether the bet is worth it, not a prediction. Re-run it as Search Console "
         "and conversion data come in, and replace the defaults below with your own numbers.", ""]
    biz = [k for k in BUSINESS_INPUTS if k in f["defaults_used"]]
    if biz:
        L += [f"**Your numbers still missing (defaults or blanks in use):** {', '.join(biz)}. Set them under "
              f"`forecast:` in the site config.", ""]
    obs = f.get("observed_conversion") or {}
    if obs:
        L += ["Conversion rates from GA4: " + "; ".join(
            f"{k} {o['rate']:.2%} ({o['sessions']:,} sessions)" + ("" if o["used"] else ", not used yet (too few sessions "
                                                                    "or set in config)") for k, o in sorted(obs.items())) + ".", ""]
    other = [k for k in f["defaults_used"] if k not in BUSINESS_INPUTS
             and k not in ("months", "scenarios", "conversions_csv", "min_sessions")]
    if other:
        L += [f"Market assumptions at their defaults: {', '.join(other)}.", ""]
    L += ["## The bet", "",
          f"Bottom-of-funnel keywords: **{f['by_stage']['bottom']['keywords']}** "
          f"({f['by_stage']['bottom']['volume']:,} searches/month reported, ×{a['volume_correction']} under-reporting correction). "
          f"If {int(a['share_ranked'] * 100)}% of them reach the top 3 within {a['months_to_rank']} months:", "",
          "| | Per month at full ranking | First " + str(a["months"]) + " months |", "|---|---|---|",
          f"| Clicks | {p['monthly_at_full']['clicks']:,.0f} | {sum(m['clicks'] for m in p['months']):,} |",
          f"| Leads | {p['monthly_at_full']['leads']:,.1f} | {sum(m['leads'] for m in p['months']):,.1f} |",
          f"| Customers | {p['monthly_at_full']['customers']:,.2f} | {p['customers']:,.1f} |"]
    if "first_year_revenue" in p:
        L.append(f"| Revenue (first-year value) | | {_money(p['first_year_revenue'])} |")
    if "lifetime_gross_profit" in p:
        L.append(f"| Lifetime gross profit | | {_money(p['lifetime_gross_profit'])} |")
    if "spend" in p:
        L.append(f"| SEO spend | | {_money(p['spend'])} |")
    if "roi" in p:
        L.append(f"| ROI ({p['roi_basis'].replace('_', ' ')}) | | {p['roi'] * 100:,.0f}% |")
    if "first_year_revenue" not in p:
        L += ["", "_Set `forecast.customer_value` (and `lifetime_value`, `monthly_spend`) to turn customers into money._"]
    L += ["", "## Scenarios", "", "| Share in top 3 | Customers | Revenue | Lifetime gross profit | ROI |", "|---|---|---|---|---|"]
    for sc in f["scenarios"]:
        L.append(f"| {int(sc['share_ranked'] * 100)}% | {sc['customers']:,.1f} | {_money(sc.get('first_year_revenue'))} | "
                 f"{_money(sc.get('lifetime_gross_profit'))} | "
                 f"{'n/a' if sc.get('roi') is None else str(round(sc['roi'] * 100)) + '%'} |")
    if f["break_even"]:
        b = f["break_even"]
        L += ["", f"**Break-even:** {b['customers']} customers a month = {b['leads']} leads = about "
                  f"{b['clicks_at_bottom_rate']:,} clicks a month on bottom-of-funnel pages."]
    L += ["", "## Funnel mix of everything found", "", "| Stage | Keywords | Searches/mo | Clicks/mo if ranked | Leads/mo if ranked |",
          "|---|---|---|---|---|"]
    for st, v in f["by_stage"].items():
        L.append(f"| {st} | {v['keywords']} | {v['volume']:,} | {v['clicks']:,} | {v['leads']:,} |")
    L += ["", "## Money pages", "",
          f"{mp['written']} bottom-of-funnel pages exist; target {mp['target']}. Money-pages mode is "
          + ("**on**: research only selects bottom-of-funnel keywords until the target is met." if mp["active"] else
             "on, and the target is met: research selects by its normal score." if mp["enabled"] else
             "off (`money_pages: {enabled: true}` to write these first)."), "",
          "| Keyword | Stage | Status | Volume | KD | Clicks/mo | Leads/mo | Basis |", "|---|---|---|---|---|---|---|---|"]
    for r in f["keywords"][:40]:
        L.append(f"| {r['keyword']} | {r['kind']} | {r['status']} | {r['volume']:,} | "
                 f"{'' if r.get('kd') is None else int(r['kd'])} | {r['clicks']:,.0f} | {r['leads']:,.2f} | {r['basis']} |")
    if len(f["keywords"]) > 40:
        L.append(f"\n_{len(f['keywords']) - 40} more in the JSON file._")
    L += ["", "## Assumptions", "", "| Input | Value |", "|---|---|"]
    for k, v in f["assumptions"].items():
        if k not in ("scenarios",):
            L.append(f"| {k} | {v} |")
    return "\n".join(L) + "\n"


def run(config_path, tp_limit: int = 0) -> dict:
    config = load_config(config_path, dry_run=False)
    items = pool(config)
    if tp_limit:
        config["_runtime"].get_secrets()
        lang = config.get("language_code", "en")
        s = settings(config)
        for it in items:
            it["funnel"] = stage_for(config, it["keyword"], it.get("intent"), it.get("page_type"))
        items.sort(key=lambda it: -money_score(it, s, lang))  # spend on the keywords that matter most
        traffic_potential(config, items, tp_limit)
    f = build(config, items)
    out = Path(config["_paths"]["data"]) / "forecast"
    out.mkdir(parents=True, exist_ok=True)
    stamp = f["generated"]
    (out / f"{stamp}.json").write_text(json.dumps(f, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / f"{stamp}.md").write_text(render(f), encoding="utf-8")
    f["report"] = str(out / f"{stamp}.md")
    return f


def cli(config_path, tp_limit: int = 0) -> int:
    try:
        f = run(config_path, tp_limit)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        return 2
    p, mp = f["projection"], f["money_pages"]
    print(f"\n=== forecast: {f['site']} ===")
    print(f"  bottom-of-funnel keywords: {f['by_stage']['bottom']['keywords']}, "
          f"{p['monthly_at_full']['leads']:.1f} leads/month at {int(f['assumptions']['share_ranked'] * 100)}% ranked")
    if "first_year_revenue" in p:
        print(f"  first {f['assumptions']['months']} months: {_money(p['first_year_revenue'])} revenue"
              + (f", ROI {p['roi'] * 100:.0f}%" if "roi" in p else ""))
    print(f"  money pages: {mp['written']} of {mp['target']}"
          + (" (money-pages mode is selecting only these)" if mp["active"] else ""))
    biz = [k for k in BUSINESS_INPUTS if k in f["defaults_used"]]
    if biz:
        print(f"  your numbers still missing: {', '.join(biz)}")
    print(f"  report: {f['report']}")
    return 0
