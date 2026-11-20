"""Search Console opportunity check (meant to run daily).

  python main.py gsc-check --config configs/site.yaml            # live, via the Search Console API
  python main.py gsc-check --config configs/site.yaml --export performance.zip   # from a UI export
  python main.py gsc-check --all                                 # every config that sets gsc_property

Live mode pulls page + query rows for the current window and the window before it, then writes
data/<site>/gsc/<date>.md and .json (plus latest.json and a page+query CSV) with:
  - striking-distance queries (ranking 4-20) and the page that ranks
  - page-one rows with a low click-through rate
  - queries answered by more than one page (cannibalisation)
  - pages that lost position or impressions against the previous window
  - planned or written articles whose keyword another page already ranks for
  - an internal-link plan built from the same rows (never applied here; apply it with interlink-apply)

A UI export has no page + query pairs, so export mode reports the query-level and page-level
sections only. Only Search Console is called: no DataForSEO, SerpAPI or LLM spend.
"""
from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

import inventory
from apiclient import ApiError, _request
from config import ROOT, ConfigError, get_runtime, load_config, optional_secret
from textutil import normalize_keyword

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
API = "https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query"
PAGE_SIZE = 25000


# ---------- data sources ----------

def _token() -> str:
    path = optional_secret("GSC_SERVICE_ACCOUNT_FILE")
    if not path:
        raise ConfigError("GSC_SERVICE_ACCOUNT_FILE is not set in .env (path to a Google service-account "
                          "JSON key that has been added as a user on the Search Console property)")
    key = Path(path).expanduser()
    if not key.is_file():
        raise ConfigError(f"GSC_SERVICE_ACCOUNT_FILE points to a file that does not exist: {key}")
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    try:
        creds = service_account.Credentials.from_service_account_file(str(key), scopes=[SCOPE])
        creds.refresh(Request())
    except (GoogleAuthError, ValueError) as e:
        raise ConfigError(f"Search Console sign-in failed with the service-account key: {e}")
    return creds.token


def property_id(config) -> str:
    return config.get("gsc_property") or f"sc-domain:{config['domain']}"


def query_api(config, token: str, start: date, end: date, dimensions: list[str]) -> list[dict]:
    """All rows for the window, paginated. Keys: the dimension names + clicks/impressions/ctr/position."""
    url = API.format(site=quote(property_id(config), safe=""))
    rows, offset = [], 0
    while True:
        resp = _request(config, "gsc", f"searchAnalytics {'+'.join(dimensions)}", "POST", url,
                        headers={"Authorization": f"Bearer {token}"},
                        json={"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dimensions,
                              "rowLimit": PAGE_SIZE, "startRow": offset, "type": "web"})
        batch = resp.json().get("rows") or []
        for r in batch:
            row = dict(zip(dimensions, r.get("keys") or []))
            if "query" in row:
                row["query"] = row["query"].strip().lower()
            row.update(clicks=float(r.get("clicks", 0)), impressions=float(r.get("impressions", 0)),
                       ctr=float(r.get("ctr", 0)), position=float(r.get("position", 0)))
            rows.append(row)
        if len(batch) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def fetch_live(config, today: date | None = None) -> dict:
    opts = config["gsc"]
    end = (today or date.today()) - timedelta(days=opts["lag_days"])
    start = end - timedelta(days=opts["window_days"] - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=opts["window_days"] - 1)
    token = _token()
    return {"source": "api", "property": property_id(config),
            "period": f"{start.isoformat()} to {end.isoformat()}",
            "previous_period": f"{prev_start.isoformat()} to {prev_end.isoformat()}",
            "pairs": query_api(config, token, start, end, ["page", "query"]),
            "prev_pairs": query_api(config, token, prev_start, prev_end, ["page", "query"]),
            "queries": query_api(config, token, start, end, ["query"]),
            "pages": query_api(config, token, start, end, ["page"])}


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def _read_export_csv(text: str, key: str) -> list[dict]:
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        first = next(iter(r.values()), "") or ""
        if not first.strip():
            continue
        rows.append({key: first.strip().lower() if key == "query" else first.strip(),
                     "clicks": _num(r.get("Clicks")), "impressions": _num(r.get("Impressions")),
                     "ctr": _num(r.get("CTR")) / 100.0, "position": _num(r.get("Position"))})
    return rows


def load_export(path) -> dict:
    """Read a Search Console UI export (the .zip, or the folder it unpacks to)."""
    path = Path(path).expanduser()
    files = {}
    if path.is_dir():
        for f in path.glob("*.csv"):
            files[f.name] = f.read_text(encoding="utf-8-sig")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    files[Path(name).name] = z.read(name).decode("utf-8-sig")
    else:
        raise ValueError(f"{path} is not a Search Console export (.zip or unpacked folder)")
    if "Queries.csv" not in files or "Pages.csv" not in files:
        raise ValueError(f"{path}: expected Queries.csv and Pages.csv, found {sorted(files)}")
    period = "unknown"
    for r in csv.reader(io.StringIO(files.get("Filters.csv", ""))):
        if len(r) >= 2 and r[0].strip().lower() == "date":
            period = r[1].strip()
    return {"source": "export", "property": None, "period": period, "previous_period": None,
            "pairs": [], "prev_pairs": [], "queries": _read_export_csv(files["Queries.csv"], "query"),
            "pages": _read_export_csv(files["Pages.csv"], "page")}


# ---------- analysis ----------

def _path(url: str) -> str:
    p = urlparse(url).path if "//" in url else url
    return p if p.endswith("/") else p + "/"


def _row(r: dict) -> dict:
    out = {k: r[k] for k in ("query", "page") if r.get(k)}
    if "page" in out:
        out["page"] = _path(out["page"])
    out.update(impressions=int(r["impressions"]), clicks=int(r["clicks"]),
               ctr=round(r["ctr"], 4), position=round(r["position"], 1))
    return out


def _article_urls(config, entries) -> dict:
    prefix = config["blog_url_prefix"].rstrip("/")
    return {e["primary_keyword"]: f'{(e.get("url_prefix") or prefix).rstrip("/")}/{e["slug"]}/'
            for e in entries if e.get("type") == "article"}


def analyze(config, data: dict) -> dict:
    opts = config["gsc"]
    floor, cap = opts["min_impressions"], opts["max_rows_per_section"]
    thr = config["duplicate_similarity_threshold"]
    has_pairs = bool(data["pairs"])
    rows = data["pairs"] if has_pairs else data["queries"]
    solid = [r for r in rows if r["impressions"] >= floor]

    striking = sorted((r for r in solid if opts["striking_min_position"] <= r["position"] <= opts["striking_max_position"]),
                      key=lambda r: -r["impressions"])
    ctr_rows = data["pairs"] if has_pairs else data["pages"]
    low_ctr = sorted((r for r in ctr_rows if r["impressions"] >= floor and 0 < r["position"] <= opts["low_ctr_max_position"]
                      and r["ctr"] < opts["low_ctr_max"]), key=lambda r: -r["impressions"])
    weak = sorted((r for r in solid if r["position"] > opts["striking_max_position"]), key=lambda r: -r["impressions"])

    # Same query answered by several pages.
    cannibal = []
    by_query: dict[str, list] = {}
    for r in data["pairs"]:
        by_query.setdefault(r["query"], []).append(r)
    for q, rs in by_query.items():
        total = sum(r["impressions"] for r in rs)
        if total < floor or len(rs) < 2:
            continue
        rs = sorted(rs, key=lambda r: -r["impressions"])
        if rs[1]["impressions"] / total >= opts["cannibal_min_share"]:
            cannibal.append({"query": q, "impressions": int(total),
                             "pages": [{"page": _path(r["page"]), "impressions": int(r["impressions"]),
                                        "position": round(r["position"], 1)} for r in rs[:4]]})
    cannibal.sort(key=lambda c: -c["impressions"])

    # Pages that slipped against the previous window (impression-weighted position per page).
    def per_page(pairs):
        agg: dict[str, dict] = {}
        for r in pairs:
            a = agg.setdefault(_path(r["page"]), {"impressions": 0.0, "weighted": 0.0, "clicks": 0.0})
            a["impressions"] += r["impressions"]
            a["weighted"] += r["position"] * r["impressions"]
            a["clicks"] += r["clicks"]
        return {p: {"impressions": a["impressions"], "clicks": a["clicks"],
                    "position": a["weighted"] / a["impressions"] if a["impressions"] else 0.0} for p, a in agg.items()}

    declines = []
    now, before = per_page(data["pairs"]), per_page(data["prev_pairs"])
    for page, b in before.items():
        if b["impressions"] < floor:
            continue
        n = now.get(page, {"impressions": 0.0, "clicks": 0.0, "position": 0.0})
        pos_drop = (n["position"] - b["position"]) if n["impressions"] else None
        imp_drop = 1 - n["impressions"] / b["impressions"]
        if (pos_drop is not None and pos_drop >= opts["decline_position_drop"]) or imp_drop >= opts["decline_impression_drop"]:
            declines.append({"page": page, "impressions_before": int(b["impressions"]), "impressions_now": int(n["impressions"]),
                             "position_before": round(b["position"], 1),
                             "position_now": round(n["position"], 1) if n["impressions"] else None})
    declines.sort(key=lambda d: d["impressions_now"] - d["impressions_before"])

    # Articles in the inventory whose keyword the site already gets impressions for through another page.
    entries = inventory.load(config)
    urls = _article_urls(config, entries)
    import output
    imported = {e["primary_keyword"] for e in entries if output.is_imported(e)}
    conflicts, owners = [], []
    for r in sorted(solid, key=lambda r: -r["impressions"]):
        page = _path(r["page"]) if r.get("page") else None
        if page and page in urls.values():
            continue  # the article itself ranks: that is the goal, not a conflict
        if page:
            owners.append({"query": r["query"], "page": page, "impressions": int(r["impressions"]),
                           "position": round(r["position"], 1)})
        for kw, url in urls.items():
            if not page and kw in imported:
                continue  # export mode cannot tell whether the existing page itself is what ranks
            sim = 1.0 if normalize_keyword(kw) == normalize_keyword(r["query"]) else 0.0
            if sim:  # same keyword after normalisation; similar-looking queries are not conflicts
                conflicts.append({"article_keyword": kw, "article_url": url, "query": r["query"], "ranking_page": page,
                                  "impressions": int(r["impressions"]), "position": round(r["position"], 1),
                                  "similarity": round(sim, 2)})
    seen, uniq_conflicts = set(), []
    for c in conflicts:  # strongest query per article + ranking page
        k = (c["article_keyword"], c["ranking_page"])
        if k not in seen:
            seen.add(k)
            uniq_conflicts.append(c)

    return {"site": config["site_name"], "generated": datetime.now().isoformat(timespec="seconds"),
            "source": data["source"], "property": data["property"], "period": data["period"],
            "previous_period": data["previous_period"], "has_page_query_pairs": has_pairs,
            "thresholds": dict(opts),
            "totals": {"queries": len(data["queries"]), "pages": len(data["pages"]), "page_query_rows": len(data["pairs"]),
                       "listed_page_impressions": int(sum(r["impressions"] for r in data["pages"])),
                       "listed_page_clicks": int(sum(r["clicks"] for r in data["pages"]))},
            "striking_distance": [_row(r) for r in striking[:cap]],
            "low_ctr": [_row(r) for r in low_ctr[:cap]],
            "weak_rankings": [_row(r) for r in weak[:cap]],
            "cannibalisation": cannibal[:cap],
            "declines": declines[:cap],
            "inventory_conflicts": uniq_conflicts[:cap],
            "query_owners": owners}


# ---------- report ----------

def _table(rows: list[dict], cols: list[tuple]) -> str:
    if not rows:
        return "_Nothing above the thresholds._\n"
    out = ["| " + " | ".join(h for h, _ in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        cells = []
        for _, key in cols:
            v = r.get(key)
            if key == "ctr" and v is not None:
                v = f"{v * 100:.2f}%"
            cells.append("n/a" if v is None else str(v))
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out) + "\n"


def render(report: dict, plan_note: str) -> str:
    o = report["thresholds"]
    pairs = report["has_page_query_pairs"]
    need_api = "_Needs page + query rows from the Search Console API; a UI export does not contain them._\n"
    page_col = [("Page", "page")] if pairs else []
    t = report["totals"]
    parts = [
        f"# Search Console check: {report['site']}", "",
        f"- Source: {'Search Console API' if report['source'] == 'api' else 'UI export'}"
        + (f" ({report['property']})" if report["property"] else ""),
        f"- Period: {report['period']}" + (f" (compared with {report['previous_period']})" if report["previous_period"] else ""),
        f"- Rows: {t['queries']} queries, {t['pages']} pages, {t['page_query_rows']} page + query rows",
        f"- Listed pages total {t['listed_page_impressions']} impressions and {t['listed_page_clicks']} clicks", "",
        f"## Striking distance (position {o['striking_min_position']}-{o['striking_max_position']}, "
        f"{o['min_impressions']}+ impressions)", "",
        "Closest to page one. Add internal links to the ranking page and expand the section that answers the query.", "",
        _table(report["striking_distance"], [("Query", "query")] + page_col + [("Impressions", "impressions"),
                                                                              ("Clicks", "clicks"), ("Position", "position")]),
        f"## Low click-through on page one (position 1-{o['low_ctr_max_position']}, CTR under {o['low_ctr_max'] * 100:.0f}%)", "",
        "Already visible but not clicked. Rewrite the title and meta description around the query.", "",
        _table(report["low_ctr"], ([("Query", "query")] if pairs else []) + [("Page", "page"), ("Impressions", "impressions"),
                                                                          ("CTR", "ctr"), ("Position", "position")]),
        "## Articles competing with a page that already ranks", "",
        "A planned or written article targets a keyword the site already gets impressions for. "
        "Re-angle the article or point it at the ranking page instead of publishing a competitor to it.", "",
        _table(report["inventory_conflicts"], [("Article keyword", "article_keyword"), ("Matching query", "query"),
                                               ("Page that ranks", "ranking_page"), ("Impressions", "impressions"),
                                               ("Position", "position")]),
        "## Same query, several pages", "",
    ]
    if not pairs:
        parts.append(need_api)
    elif not report["cannibalisation"]:
        parts.append("_Nothing above the thresholds._\n")
    else:
        parts += ["| Query | Impressions | Pages (impressions, position) |", "|---|---|---|"]
        parts += [f"| {c['query']} | {c['impressions']} | "
                  + "; ".join(f"{p['page']} ({p['impressions']}, {p['position']})" for p in c["pages"]) + " |"
                  for c in report["cannibalisation"]]
        parts.append("")
    parts += ["## Pages that slipped since the previous period", "",
              need_api if not pairs else _table(report["declines"], [
                  ("Page", "page"), ("Impressions before", "impressions_before"), ("Impressions now", "impressions_now"),
                  ("Position before", "position_before"), ("Position now", "position_now")]),
              f"## Visible but far from page one (position over {o['striking_max_position']})", "",
              "Demand exists and the site is in the index for it. The ranking page needs depth or links before it moves.", "",
              _table(report["weak_rankings"], [("Query", "query")] + page_col + [("Impressions", "impressions"),
                                                                                ("Position", "position")]),
              "## Internal links", "", plan_note, ""]
    return "\n".join(parts)


# ---------- entry points ----------

def run_check(config, export=None, today: date | None = None) -> dict:
    rt = get_runtime(config)
    rt.log("stage", stage="gsc_check", site=config["site_name"], source="export" if export else "api")
    data = load_export(export) if export else fetch_live(config, today)
    report = analyze(config, data)
    out = config["_paths"]["data"] / "gsc"
    out.mkdir(parents=True, exist_ok=True)
    stamp = (today or date.today()).isoformat()

    plan_note = "No plan built: a UI export has no page + query rows. Run the live check to get one."
    report["interlink_plan"] = None
    if data["pairs"]:
        import interlink
        with open(out / f"page_query_{stamp}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["page", "query", "clicks", "impressions", "position"])
            w.writerows([r["page"], r["query"], int(r["clicks"]), int(r["impressions"]), round(r["position"], 2)]
                        for r in data["pairs"])
        plan = interlink.build_plan(config, data["pairs"])
        plan_dir = config["_paths"]["data"] / "interlink"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plan_dir / f"plan_gsc_{stamp}.json"
        plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        (plan_dir / f"plan_gsc_{stamp}.md").write_text(interlink.plan_table(plan) + "\n", encoding="utf-8")
        report["interlink_plan"] = {"file": str(plan_file), "links": len(plan)}
        gaps = config.get("_money_gaps") or []
        money = sum(1 for x in plan if x.get("kind") == "money")
        plan_note = (f"{len(plan)} proposed link(s) ({money} to money pages), nothing applied.\n\n"
                     + (f"{len(gaps)} top article(s) have no phrase that fits a money-page link: "
                        + ", ".join(g["source_url"] for g in gaps[:5]) + ".\n\n" if gaps else "") + (interlink.plan_table(plan) + "\n\n" if plan else "")
                     + f"Apply after review: `python main.py interlink-apply --config {config['config_path']} --plan {plan_file}`")

    (out / f"{stamp}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    md = out / f"{stamp}.md"
    md.write_text(render(report, plan_note), encoding="utf-8")
    report["report_file"] = str(md)
    rt.log("stage", stage="gsc_check_done", report=str(md),
           **{k: len(report[k]) for k in ("striking_distance", "low_ctr", "weak_rankings", "cannibalisation",
                                          "declines", "inventory_conflicts")})
    return report


def owners_for_research(config) -> list[dict]:
    """Queries another page already ranks for, from the last live check (page + query rows only)."""
    latest = config["_paths"]["data"] / "gsc" / "latest.json"
    if not config.get("gsc_conflict_guard", True) or not latest.exists():
        return []
    report = json.loads(latest.read_text(encoding="utf-8"))
    return (report.get("query_owners") or []) if report.get("has_page_query_pairs") else []


def cli_check(config_path, export=None) -> int:
    config = load_config(config_path)
    try:
        report = run_check(config, export)
    except (ValueError, OSError) as e:
        print(f"GSC ERROR: {e}")
        return 2
    except ApiError as e:
        print(f"GSC ERROR: {e}\n"
              f"Property tried: {property_id(config)}. Check that the service account's client_email is a user on "
              f"this property and that gsc_property uses the right form: 'sc-domain:{config['domain']}' for a "
              f"domain property, 'https://{config['domain']}/' for a URL-prefix property.")
        return 2
    print(f"\n=== Search Console check: {report['site']} ({report['period']}) ===")
    for key, label in (("striking_distance", "striking-distance rows"), ("low_ctr", "low-CTR rows on page one"),
                       ("inventory_conflicts", "articles competing with a ranking page"),
                       ("cannibalisation", "queries split across pages"), ("declines", "pages that slipped"),
                       ("weak_rankings", "visible but far from page one")):
        print(f"  {len(report[key]):>3}  {label}")
    if report["interlink_plan"]:
        print(f"  {report['interlink_plan']['links']:>3}  proposed internal links (not applied): {report['interlink_plan']['file']}")
    print(f"report: {report['report_file']}")
    return 0


def cli_check_all() -> int:
    import yaml
    code, ran = 0, 0
    for cfg in sorted((ROOT / "configs").glob("*.y*ml")):
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if not raw.get("gsc_property"):
            continue
        if (raw.get("autopilot") or {}).get("enabled"):
            print(f"[{cfg.name}] skipped: autopilot runs this site's Search Console check")
            continue
        ran += 1
        try:
            code = max(code, cli_check(cfg))
        except Exception as e:  # one site failing must not stop the others
            print(f"[{cfg.name}] FAILED: {type(e).__name__}: {e}")
            code = 1
    if not ran:
        print("No config sets gsc_property; nothing to check.")
    return code
