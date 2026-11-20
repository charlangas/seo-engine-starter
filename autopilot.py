"""Autopilot: one scheduled command runs every job a site needs, on its own cadence, and reports once a week.

  python main.py autopilot --config configs/site.yaml [--dry-run] [--task NAME ...] [--notify]
  python main.py autopilot --all [--dry-run] [--notify]       # every config with `autopilot.enabled: true`

Schedule it daily (launchd/com.seo-engine.autopilot.plist). Each run does only the jobs that are due, in this
order, and one failing job never stops the others:

  job              default   what it does
  gsc_check        daily     Search Console report: striking distance, low CTR, declines, link plan
  health           weekly    sitemap crawl (status, redirects, canonicals, noindex, duplicates, orphans),
                             indexing via URL Inspection; duplicate engine titles are queued for a rewrite
  facts            weekly    evidence figures that changed -> pages that quote them are queued for refresh
  competitor_watch weekly    new pages in competitors' sitemaps -> keyword ideas
  refresh          weekly    slipped, fact-changed, striking-distance, low-CTR (retitle) and old pages
  content          daily     write the new pages the publishing schedule owes today (pacing.py), often none
  interlink        weekly    apply the Search Console link plan plus money-page links, as a pull request
  ai_visibility    weekly    AI answer citations (only when ai_visibility.prompts is set; spends)
  page_map         monthly   SERP-overlap page map with merge/301 suggestions
  forecast         monthly   traffic, leads and revenue projection
  report           weekly    the weekly report (below)

Before a paid job (content, refresh, ai_visibility) the monthly budget is checked (budget.py): the job is
skipped, and tried again the next day, when a cap is reached or the job would cost more than is left. Optional jobs
stop first, then refreshes; new pages run until the cap.

Anything the engine writes goes through publish.py: with `publish.ship_mode: merge_when_checks_pass` the pull
request merges itself once the site's checks pass, so nothing waits on a person.

The weekly report is under 300 words and ends with at most 3 things only a person can do (fix a template,
answer evidence questions, approve a tool). It is saved to data/<site>/autopilot/report-<date>.md, shown as a
desktop notification, and posted to Slack when the environment variable named by `slack_webhook_env`
(default SLACK_WEBHOOK_URL) holds an incoming-webhook URL.

Config (per site):

    autopilot:
      enabled: true
      tasks: {content: 3, ai_visibility: off}   # days between runs, or daily | weekly | monthly | off
      slack_webhook_env: SLACK_WEBHOOK_URL
"""
from __future__ import annotations

import csv
import json
import os
import traceback
from datetime import date, datetime
from pathlib import Path

from config import ROOT, ConfigError, load_config

ORDER = ["gsc_check", "health", "facts", "competitor_watch", "refresh", "content", "interlink", "ai_visibility",
         "page_map", "forecast", "report"]
DEFAULT_DAYS = {"gsc_check": 1, "health": 7, "facts": 7, "competitor_watch": 7, "refresh": 7, "content": 1,
                "interlink": 7, "ai_visibility": 7, "page_map": 30, "forecast": 30, "report": 7}
NAMED = {"daily": 1, "weekly": 7, "monthly": 30, "off": 0, False: 0}
DEFAULTS = {"enabled": False, "tasks": {}, "slack_webhook_env": "SLACK_WEBHOOK_URL", "report_words": 300, "max_asks": 3}


def settings(config) -> dict:
    raw = config.get("autopilot") or {}
    if not isinstance(raw, dict):
        raise ConfigError("autopilot must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"autopilot: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    days = dict(DEFAULT_DAYS)
    for k, v in (s["tasks"] or {}).items():
        if k not in DEFAULT_DAYS:
            raise ConfigError(f"autopilot.tasks: unknown job '{k}'; jobs: {ORDER}")
        v = NAMED.get(v, v) if not isinstance(v, bool) else (DEFAULT_DAYS[k] if v else 0)
        if not isinstance(v, int) or v < 0:
            raise ConfigError(f"autopilot.tasks.{k} must be a number of days or daily/weekly/monthly/off")
        days[k] = v
    s["days"] = days
    return s


# ---------------------------------------------------------------- state

def _dir(config) -> Path:
    d = config["_paths"]["data"] / "autopilot"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(config) -> dict:
    f = _dir(config) / "state.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"tasks": {}, "events": []}
    except ValueError:
        return {"tasks": {}, "events": []}


def save_state(config, state: dict) -> None:
    (_dir(config) / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def due_tasks(config, state: dict, today: date, only: list[str] | None = None) -> list[str]:
    s = settings(config)
    out = []
    for t in ORDER:
        if only:
            if t in only:
                out.append(t)
            continue
        every = s["days"][t]
        if not every:
            continue
        if t == "ai_visibility" and not ((config.get("ai_visibility") or {}).get("prompts")):
            continue
        last = (state["tasks"].get(t) or {}).get("last_run")
        if not last or (today - date.fromisoformat(last[:10])).days >= every:
            out.append(t)
    return out


# ---------------------------------------------------------------- jobs (each returns {summary, asks?, ...})

def _has_gsc(config) -> bool:
    from config import optional_secret
    return bool(config.get("gsc_property") or optional_secret("GSC_SERVICE_ACCOUNT_FILE"))


def job_gsc_check(path, config) -> dict:
    if not _has_gsc(config):
        return {"summary": "skipped: no Search Console key or property", "skipped": True}
    import gsc
    r = gsc.run_check(load_config(path))
    return {"summary": f"{len(r['striking_distance'])} striking-distance, {len(r['low_ctr'])} low-CTR, "
                       f"{len(r['declines'])} slipped", "clicks": r["totals"]["listed_page_clicks"],
            "impressions": r["totals"]["listed_page_impressions"], "striking": len(r["striking_distance"])}


def job_health(path, config) -> dict:
    import health
    r = health.run(load_config(path), inspect_index=_has_gsc(config))
    health.save(config, r)
    return {"summary": f"{r['checked']} URLs, {len(r['issues'])} issue(s)", "asks": health.asks(r),
            "counts": r["counts"], "indexed": r["indexed"], "inspected": r["inspected"],
            "queued": r["queued_retitles"]}


def job_facts(path, config) -> dict:
    import evidence
    c = load_config(path)
    routed = evidence.route_facts(c)
    qs = evidence.founder_questions(c, limit_pages=2, per_page=2)
    asks = [f'Evidence for "{q["keyword"]}": ' + " ".join(q["questions"]) for q in qs]
    return {"summary": f"{len(routed)} page(s) queued for changed figures", "routed": routed, "asks": asks,
            "priority_asks": False}


def job_competitor_watch(path, config) -> dict:
    import watch
    c = load_config(path)
    if not watch.domains(c):
        return {"summary": "skipped: no competitor domains", "skipped": True}
    r = watch.run(c)
    out = c["_paths"]["data"] / "competitors" / f"report-{r['generated']}.md"
    out.write_text(watch.render(r), encoding="utf-8")
    new = sum(len(d["new"]) for d in r["domains"])
    return {"summary": f"{new} new competitor page(s), {len(r['ideas'])} new idea(s)", "new_pages": new,
            "ideas": [i["keyword"] for i in r["ideas"]][:5]}


def _pub(s: dict) -> dict:
    p = s.get("published") or {}
    out = {}
    if p:
        out["pr"] = p.get("pr_url")
        out["merged"] = bool(p.get("merged"))
        if p.get("merge_error"):
            out["merge_error"] = p["merge_error"]
    if s.get("publish_error"):
        out["publish_error"] = s["publish_error"]
    return out


def job_refresh(path, config) -> dict:
    import refresh
    s = refresh.run_refresh(path)
    re_ = [r for r in s["refreshed"] if r.get("changed") and r.get("mode") != "retitle"]
    rt_ = [r for r in s["refreshed"] if r.get("changed") and r.get("mode") == "retitle"]
    return {"summary": f"{len(re_)} refreshed, {len(rt_)} retitled, {len(s['failed'])} failed",
            "refreshed": [r["slug"] for r in re_], "retitled": [r["slug"] for r in rt_],
            "failed": [f"{f['slug']}: {f['error'][:120]}" for f in s["failed"]], **_pub(s)}


def job_content(path, config, today: date | None = None) -> dict:
    import budget
    import main
    import pacing
    c = load_config(path)
    n = pacing.owed(c, today)
    if not n:
        return {"summary": f"nothing due; next page {pacing.next_dates(c, 1, today)[0]}", "skipped": True}
    why = budget.gate(c, "content", n)
    if why:
        return {"summary": f"skipped: {why}", "skipped": True, "budget": True, "retry": True}
    s = main.run_site(path, limit=n)
    from textutil import split_frontmatter
    titles = []
    for w in s["written"]:
        try:
            titles.append(str(split_frontmatter(Path(w).read_text(encoding="utf-8"))[0].get("title") or Path(w).stem))
        except OSError:
            titles.append(Path(w).stem)
    asks = []
    if s.get("tool_needed"):
        asks.append("These searches are won by free tools, not articles; build one or drop them: " + ", ".join(s["tool_needed"][:3]))
    return {"summary": f"{len(s['written'])} written, {len(s['failed'])} failed", "written": titles,
            "failed": [f"{f['keyword']}: {f['error'][:120]}" for f in s["failed"]], "asks": asks,
            "no_evidence": [n["keyword"] for n in s.get("no_evidence") or []], **_pub(s)}


def _latest_pairs(config) -> list[dict] | None:
    files = sorted((config["_paths"]["data"] / "gsc").glob("page_query_*.csv"))
    if not files:
        return None
    with open(files[-1], newline="", encoding="utf-8") as f:
        return [{"page": r["page"], "query": r["query"], "clicks": float(r["clicks"]), "impressions": float(r["impressions"]),
                 "position": float(r["position"])} for r in csv.DictReader(f)]


def job_interlink(path, config) -> dict:
    import interlink
    import publish
    c = load_config(path)
    if c.get("publish"):
        publish.prepare(c)   # plan against the current site checkout
    plan = interlink.build_plan(c, _latest_pairs(c))
    gaps = c.get("_money_gaps") or []
    if not plan:
        return {"summary": "no link opportunities", "money_gaps": len(gaps)}
    log = interlink.apply_plan(plan, c["_paths"]["data"] / "interlink")
    changes = json.loads(log.read_text(encoding="utf-8"))["changes"]
    files = sorted({x["file"] for x in changes if x["status"] == "applied"})
    out = {"summary": f"{sum(1 for x in changes if x['status'] == 'applied')} link(s) added "
                      f"({sum(1 for p in plan if p.get('kind') == 'money')} to money pages)", "changelog": str(log),
           "money_gaps": len(gaps)}
    if files and c.get("publish"):
        try:
            out.update(_pub({"published": publish.open_pr(c, {"written": files, "action": "link"})}))
        except publish.PublishError as e:
            interlink.revert(log)   # leave the checkout clean for the next job
            out["publish_error"] = str(e)
    return out


def job_ai_visibility(path, config) -> dict:
    import aivis
    c = load_config(path)
    v = aivis.run(c)
    out = c["_paths"]["data"] / "ai_visibility"
    out.mkdir(parents=True, exist_ok=True)
    for name in (f"{v['generated']}.json", "latest.json"):
        (out / name).write_text(json.dumps(v, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / f"{v['generated']}.md").write_text(aivis.render(v), encoding="utf-8")
    return {"summary": f"cited in {v['cited']} of {v['checked']} answers", "cited": v["cited"], "checked": v["checked"],
            "gained": len(v["gained"]), "lost": len(v["lost"])}


def job_page_map(path, config) -> dict:
    import cluster
    c = load_config(path)
    m = cluster.build_map(c)
    out = c["_paths"]["data"] / "page_map"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{m['generated']}.json").write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / f"{m['generated']}.md").write_text(cluster.render(m), encoding="utf-8")
    merges = [x for x in m.get("conflicts") or [] if x.get("action") == "merge"]
    asks = [f"{len(merges)} page pair(s) compete for one search; merge and 301 (see page_map/{m['generated']}.md)"] if merges else []
    return {"summary": f"{len(m['pages'])} pages, {len(m.get('conflicts') or [])} conflict(s)", "asks": asks}


def job_forecast(path, config) -> dict:
    import forecast
    f = forecast.run(path, 0)
    biz = [k for k in forecast.BUSINESS_INPUTS if k in f["defaults_used"]]
    asks = [f"Fill in your business numbers in the config ({', '.join(biz[:4])}) so the forecast is yours"] if biz else []
    p = f["projection"]["monthly_at_full"]
    return {"summary": f"{p['leads']:.1f} leads/month projected", "asks": asks, "priority_asks": False}


JOBS = {"gsc_check": job_gsc_check, "health": job_health, "facts": job_facts, "competitor_watch": job_competitor_watch,
        "refresh": job_refresh, "content": job_content, "interlink": job_interlink, "ai_visibility": job_ai_visibility,
        "page_map": job_page_map, "forecast": job_forecast}


# ---------------------------------------------------------------- weekly report

def _words(text: str) -> int:
    return len(text.split())


def build_report(config, events: list[dict], today: date, limit_words: int = 300, max_asks: int = 3) -> tuple[str, list[str]]:
    import refresh
    last = {}
    for e in events:
        last.setdefault(e["task"], []).append(e)

    def latest(task):
        return (last.get(task) or [{}])[-1].get("result") or {}

    def all_of(task, key):
        return [x for e in last.get(task) or [] for x in (e.get("result") or {}).get(key) or []]

    lines = []
    written, refreshed, retitled = all_of("content", "written"), all_of("refresh", "refreshed"), all_of("refresh", "retitled")
    links = [e["result"]["summary"] for e in last.get("interlink") or [] if e.get("result")]
    pub = "merged automatically" if any((e.get("result") or {}).get("merged") for t in ("content", "refresh", "interlink")
                                        for e in last.get(t) or []) else "waiting in pull requests"
    if written or refreshed or retitled or links:
        lines.append(f"Shipped: {len(written)} new page(s), {len(refreshed)} refreshed, {len(retitled)} retitled"
                     + (f", {links[-1]}" if links else "") + f" ({pub}).")
        if written:
            lines.append("New: " + "; ".join(written[:4]) + ("..." if len(written) > 4 else "") + ".")
    else:
        lines.append("Shipped: nothing new this week.")
    g = latest("gsc_check")
    if g.get("impressions") is not None:
        lines.append(f"Search: {g['clicks']} clicks, {g['impressions']} impressions (28 days); {g.get('striking', 0)} "
                     f"queries one push from page one.")
    tests = refresh.title_test_results(config, today)
    if tests:
        better = sum(1 for t in tests if t["verdict"] == "better")
        lines.append(f"Title tests: {better} of {len(tests)} improved click-through.")
    a = latest("ai_visibility")
    if a.get("checked"):
        lines.append(f"AI answers: cited in {a['cited']} of {a['checked']} (+{a['gained']}, -{a['lost']}).")
    h = latest("health")
    if h:
        lines.append(f"Site health: {h.get('summary')}" + (f"; {h['indexed']} of {h['inspected']} checked pages indexed"
                                                           if h.get("inspected") else "") + ".")
    import budget
    import pacing
    lines.insert(1 + bool(written), budget.report_line(config))
    lines.insert(2 + bool(written), pacing.report_line(config, today))
    w = latest("competitor_watch")
    if w.get("new_pages"):
        lines.append(f"Competitors published {w['new_pages']} page(s); " + (f"new ideas: {', '.join(w['ideas'][:3])}." if w.get("ideas") else "none on-topic."))

    critical, urgent, normal = [], [], []   # broken runs, then site problems, then nice-to-haves
    critical += budget.asks(config)
    pace_urgent, pace_normal = pacing.asks(config, today)
    urgent += pace_urgent
    for e in events:
        r = e.get("result") or {}
        if e.get("error"):
            critical.append(f"The {e['task']} job crashed: {e['error'][:140]}")
        for k in ("publish_error", "merge_error"):
            if r.get(k):
                critical.append(f"{e['task']}: {k.replace('_', ' ')}: {r[k][:140]}")
        for f in r.get("failed") or []:
            critical.append(f"{e['task']} failed: {f}")
        (normal if r.get("priority_asks") is False else urgent).extend(r.get("asks") or [])
    normal += pace_normal
    seen, asks = set(), []
    for x in critical + urgent + normal:
        if x not in seen:
            seen.add(x)
            asks.append(x)
    asks = asks[:max_asks]
    head = f"SEO autopilot: {config['site_name']}, week of {today.isoformat()}"

    def assemble(ls):
        tail = (["", f"Needs you ({len(asks)}):"] + [f"{i}. {x}" for i, x in enumerate(asks, 1)]) if asks else \
            ["", "Nothing needs you this week."]
        return [head, ""] + ls + tail

    body = assemble(lines)
    while _words("\n".join(body)) > limit_words and len(lines) > 1:
        lines.pop()   # lines are ordered by importance: drop from the end
        body = assemble(lines)
    if _words("\n".join(body)) > limit_words:   # very long asks: shorten each line
        body = [" ".join(l.split()[:35]) for l in body]
    text = "\n".join(body)
    return text + "\n", asks


def deliver(config, text: str, asks: list[str], s: dict, notify_desktop: bool) -> dict:
    out = {}
    path = _dir(config) / f"report-{date.today().isoformat()}.md"
    path.write_text(text, encoding="utf-8")
    out["file"] = str(path)
    if notify_desktop:
        import notify
        notify.send(f"SEO autopilot: {config['site_name']}", asks[0] if asks else text.splitlines()[2] if len(text.splitlines()) > 2 else "")
    hook = os.environ.get(s["slack_webhook_env"] or "", "").strip()
    if not hook:
        from config import optional_secret
        hook = optional_secret(s["slack_webhook_env"]) if s["slack_webhook_env"] else ""
    if hook:
        import requests
        try:
            r = requests.post(hook, json={"text": text}, timeout=15)
            out["slack"] = r.status_code
        except requests.RequestException as e:
            out["slack_error"] = type(e).__name__
    return out


# ---------------------------------------------------------------- run

def run(path, dry_run: bool = False, only: list[str] | None = None, notify_desktop: bool = False,
        today: date | None = None, config: dict | None = None) -> dict:
    import budget
    config = config or load_config(path)
    s = settings(config)
    today = today or date.today()
    state = load_state(config)
    tasks = due_tasks(config, state, today, only)
    result = {"site": config["site_name"], "due": tasks, "ran": [], "errors": []}
    if dry_run:
        return result
    for t in tasks:
        if t == "report":
            continue
        started = datetime.now().isoformat(timespec="seconds")
        ev = {"task": t, "date": today.isoformat(), "started": started}
        why = budget.gate(config, t) if t not in ("content", "report") else None
        try:
            if why:
                ev["result"] = {"summary": f"skipped: {why}", "skipped": True, "budget": True, "retry": True}
            else:
                with budget.job(t):
                    ev["result"] = JOBS[t](path, config)
        except Exception as e:  # noqa: BLE001 - one job failing must not stop the others
            ev["error"] = f"{type(e).__name__}: {e}"
            ev["traceback"] = traceback.format_exc()[-2000:]
            result["errors"].append({"task": t, "error": ev["error"]})
        prev = (state["tasks"].get(t) or {}).get("last_run")
        retry = (ev.get("result") or {}).get("retry")   # skipped for budget: try again tomorrow
        state["tasks"][t] = {"last_run": prev if retry else today.isoformat(), "ok": "error" not in ev,
                             "summary": (ev.get("result") or {}).get("summary") or ev.get("error")}
        if retry and not prev:
            state["tasks"][t].pop("last_run")
        state["events"].append({k: v for k, v in ev.items() if k != "traceback"})
        state["events"] = state["events"][-400:]
        save_state(config, state)
        with open(_dir(config) / "log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, default=str, ensure_ascii=False) + "\n")
        result["ran"].append({"task": t, "summary": state["tasks"][t]["summary"]})
    if "report" in tasks:
        since = (state["tasks"].get("report") or {}).get("last_run") or "0000"
        events = [e for e in state["events"] if e["date"] >= since[:10]]
        text, asks = build_report(config, events, today, s["report_words"], s["max_asks"])
        result["report"] = deliver(config, text, asks, s, notify_desktop)
        result["report_text"] = text
        state["tasks"]["report"] = {"last_run": today.isoformat(), "ok": True, "summary": f"{len(asks)} ask(s)"}
        save_state(config, state)
    elif notify_desktop and result["errors"]:
        import notify
        notify.send(f"SEO autopilot: {config['site_name']}", f"{result['errors'][0]['task']} failed: {result['errors'][0]['error']}")
    return result


def configs_enabled() -> list[Path]:
    import yaml
    out = []
    for cfg in sorted((ROOT / "configs").glob("*.y*ml")):
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        if (raw.get("autopilot") or {}).get("enabled"):
            out.append(cfg)
    return out


def cli(config_path=None, all_sites: bool = False, dry_run: bool = False, only=None, notify_desktop: bool = False) -> int:
    paths = configs_enabled() if all_sites else [Path(config_path)]
    if not paths:
        print("No config has autopilot.enabled: true.")
        return 0
    code = 0
    for p in paths:
        try:
            r = run(p, dry_run, only, notify_desktop)
        except ConfigError as e:
            print(f"[{p.name}] CONFIG ERROR: {e}")
            code = 2
            continue
        print(f"\n=== autopilot: {r['site']}{' (dry run)' if dry_run else ''} ===")
        print("due: " + (", ".join(r["due"]) or "nothing"))
        for x in r["ran"]:
            print(f"  {x['task']}: {x['summary']}")
        for e in r["errors"]:
            print(f"  ! {e['task']}: {e['error'][:200]}")
            code = 1
        if r.get("report_text"):
            print("\n" + r["report_text"])
    return code
