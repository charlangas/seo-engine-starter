"""Monthly spending: what every paid call cost, dollar caps per site and overall, and what a page costs.

Every paid call is written to a ledger, one file per month for all sites: spend/<YYYY-MM>.jsonl.

  OpenRouter   the `usage.cost` OpenRouter reports for the call
  DataForSEO   the `cost` field DataForSEO returns with every response
  SerpAPI      `serpapi_usd_per_search` per search (SerpAPI bills a monthly plan; set your plan's price per search)
  Claude API   `llm_usd_fallback` per call (only the legacy `writer_backend: anthropic`)

A call that reports no cost is charged at the fallback and marked `estimated`.

Caps, checked before every paid call (a call over a cap raises BudgetExceeded) and before every autopilot job:

    budget:
      monthly_usd: 60            # this site, all services, per calendar month (null: no cap)
      services: {llm: 40, dataforseo: 15, serpapi: 10}   # optional caps per service
      serpapi_usd_per_search: 0.015
      cut_optional_at: 0.7       # from 70% of a cap, skip optional jobs (the AI visibility check)
      cut_refresh_at: 0.85       # from 85%, skip refreshes too; new pages keep going until the cap

MONTHLY_BUDGET_USD in .env (or the environment) caps all sites together.

  python main.py spend [--config configs/site.yaml | --all] [--month 2026-11]
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from config import ROOT, BudgetExceeded, ConfigError, optional_secret

SERVICES = ("dataforseo", "serpapi", "llm")
SERVICE_NAMES = {"dataforseo": "DataForSEO", "serpapi": "SerpAPI", "llm": "OpenRouter"}
DEFAULTS = {
    "monthly_usd": None,
    "services": {},
    "serpapi_usd_per_search": 0.015,
    "dataforseo_usd_fallback": 0.01,
    "llm_usd_fallback": 0.25,
    "cut_optional_at": 0.7,
    "cut_refresh_at": 0.85,
}
# Autopilot's other jobs (Search Console, health, facts, competitor watch, links, page map, forecast) make no paid
# calls; if one ever does, the per-call check still stops it at a cap.
OPTIONAL_JOBS = ("ai_visibility",)
PAID_JOBS = ("content", "refresh") + OPTIONAL_JOBS
# What a job costs before there is any history to go by (dollars per run; content is per page).
FIRST_GUESS = {"content": 1.5, "refresh": 1.0, "ai_visibility": 1.0}

_job: dict | None = None   # the autopilot job (or command) the current calls belong to


def settings(config) -> dict:
    raw = config.get("budget") or {}
    if not isinstance(raw, dict):
        raise ConfigError("budget must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"budget: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    if s["monthly_usd"] is not None and (not isinstance(s["monthly_usd"], (int, float)) or s["monthly_usd"] < 0):
        raise ConfigError("budget.monthly_usd must be a number of dollars or null")
    services = s["services"] or {}
    if not isinstance(services, dict) or set(services) - set(SERVICES):
        raise ConfigError(f"budget.services: keys must be among {list(SERVICES)}")
    for k, v in services.items():
        if not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"budget.services.{k} must be a number of dollars")
    for k in ("serpapi_usd_per_search", "dataforseo_usd_fallback", "llm_usd_fallback"):
        if not isinstance(s[k], (int, float)) or s[k] < 0:
            raise ConfigError(f"budget.{k} must be a number of dollars")
    for k in ("cut_optional_at", "cut_refresh_at"):
        if not isinstance(s[k], (int, float)) or not 0 < s[k] <= 1:
            raise ConfigError(f"budget.{k} must be a share between 0 and 1")
    s["services"] = services
    return s


def global_cap() -> float | None:
    v = os.environ.get("MONTHLY_BUDGET_USD", "").strip() or optional_secret("MONTHLY_BUDGET_USD")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        raise ConfigError(f"MONTHLY_BUDGET_USD must be a number of dollars, not {v!r}")


# ---------------------------------------------------------------- ledger

def spend_dir() -> Path:
    return Path(os.environ["SEO_ENGINE_SPEND_DIR"]) if os.environ.get("SEO_ENGINE_SPEND_DIR") else ROOT / "spend"


def month_of(d: date | None = None) -> str:
    return (d or date.today()).strftime("%Y-%m")


def _ledger(month: str) -> Path:
    return spend_dir() / f"{month}.jsonl"


def load(month: str | None = None) -> list[dict]:
    f = _ledger(month or month_of())
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def load_range(since: date, until: date | None = None) -> list[dict]:
    until = until or date.today()
    months, d = [], date(since.year, since.month, 1)
    while d <= until:
        months.append(month_of(d))
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    lo = since.isoformat()
    return [e for m in months for e in load(m) if e.get("ts", "")[:10] >= lo]


def _append(entry: dict) -> None:
    d = spend_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(_ledger(month_of()), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _context(config) -> tuple[str, str]:
    if _job:
        return _job["name"], _job["run"]
    rt = config.get("_runtime")
    return "other", (rt.run_id if rt else "unknown")


@contextmanager
def job(name: str, keep_outer: bool = False):
    """Tag every call inside with this job. keep_outer: leave an enclosing job's tag in place."""
    global _job
    if keep_outer and _job:
        yield
        return
    prev, _job = _job, {"name": name, "run": uuid.uuid4().hex[:10]}
    try:
        yield
    finally:
        _job = prev


def record(config, service: str, usd, endpoint: str = "") -> float:
    """Write one paid call to the ledger. usd None: charge the service's fallback and mark it estimated."""
    s = settings(config)
    estimated = usd is None
    if estimated:
        usd = {"serpapi": s["serpapi_usd_per_search"], "dataforseo": s["dataforseo_usd_fallback"]}.get(
            service, s["llm_usd_fallback"])
    usd = round(float(usd), 6)
    name, run = _context(config)
    _append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "site": config["site_slug"],
             "service": service, "job": name, "run": run, "usd": usd, "estimated": estimated,
             "endpoint": endpoint[:80], "dry_run": bool(getattr(config.get("_runtime"), "dry_run", False))})
    return usd


def note_pages(config, n: int) -> None:
    """Pages a content run produced, so spend can be divided into a cost per page."""
    if n <= 0:
        return
    name, run = _context(config)
    _append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "site": config["site_slug"],
             "kind": "pages", "job": name, "run": run, "n": int(n)})


def _calls(entries):
    return [e for e in entries if e.get("kind") != "pages"]


# ---------------------------------------------------------------- totals and caps

def totals(config=None, month: str | None = None) -> dict:
    entries = _calls(load(month))
    site = config["site_slug"] if config else None
    mine = [e for e in entries if site is None or e.get("site") == site]
    by_service, by_job = {}, {}
    for e in mine:
        by_service[e["service"]] = by_service.get(e["service"], 0) + e["usd"]
        by_job[e.get("job") or "other"] = by_job.get(e.get("job") or "other", 0) + e["usd"]
    return {"site": round(sum(e["usd"] for e in mine), 4), "all": round(sum(e["usd"] for e in entries), 4),
            "by_service": {k: round(v, 4) for k, v in by_service.items()},
            "by_job": {k: round(v, 4) for k, v in by_job.items()},
            "estimated": round(sum(e["usd"] for e in mine if e.get("estimated")), 4)}


def caps(config) -> list[dict]:
    """Every cap that applies to this site: [{label, cap, spent, service}] (service None: all services)."""
    s, t = settings(config), totals(config)
    out = []
    if s["monthly_usd"] is not None:
        out.append({"label": f"{config['site_name']} monthly budget", "cap": float(s["monthly_usd"]),
                    "spent": t["site"], "service": None})
    for svc, cap in s["services"].items():
        out.append({"label": f"{config['site_name']} {SERVICE_NAMES[svc]} budget", "cap": float(cap),
                    "spent": t["by_service"].get(svc, 0.0), "service": svc})
    g = global_cap()
    if g is not None:
        out.append({"label": "MONTHLY_BUDGET_USD (all sites)", "cap": g, "spent": t["all"], "service": None})
    return out


def used_share(config, service: str | None = None) -> float | None:
    """Highest share of any cap used (only caps covering `service` when given). None: no cap."""
    cs = [c for c in caps(config) if service is None or c["service"] in (None, service)]
    if not cs:
        return None
    return max((c["spent"] / c["cap"]) if c["cap"] else 1.0 for c in cs)


def remaining(config) -> float | None:
    cs = [c for c in caps(config) if c["service"] is None]
    return min(c["cap"] - c["spent"] for c in cs) if cs else None


def check(config, service: str) -> None:
    """Raise BudgetExceeded when a cap covering this service is used up. Called before every paid call."""
    for c in caps(config):
        if c["service"] in (None, service) and c["spent"] >= c["cap"]:
            raise BudgetExceeded(f"{c['label']} reached: ${c['spent']:.2f} of ${c['cap']:.2f} this month")


# ---------------------------------------------------------------- estimates

def _history(config, days: int = 90) -> list[dict]:
    site = config["site_slug"]
    return [e for e in load_range(date.today() - timedelta(days=days)) if e.get("site") == site]


def cost_per_page(config, days: int = 90) -> float | None:
    """Content spend divided by pages written, over the last `days`. None until a content run has written a page."""
    h = _history(config, days)
    runs = {e["run"] for e in h if e.get("kind") == "pages" and e.get("job") == "content"}
    pages = sum(e["n"] for e in h if e.get("kind") == "pages" and e.get("job") == "content")
    if not pages:
        return None
    usd = sum(e["usd"] for e in _calls(h) if e.get("job") == "content" and e.get("run") in runs)
    return round(usd / pages, 4)


def job_cost(config, name: str, days: int = 90) -> float:
    """Average dollars per run of this job over the last `days`; a first guess before there is history."""
    if name == "content":
        return cost_per_page(config, days) or FIRST_GUESS["content"]
    h = [e for e in _calls(_history(config, days)) if e.get("job") == name]
    runs = {e["run"] for e in h}
    return round(sum(e["usd"] for e in h) / len(runs), 4) if runs else FIRST_GUESS.get(name, 0.0)


def other_monthly(config, days: int = 90) -> float:
    """Average monthly spend on everything except new pages."""
    h = [e for e in _calls(_history(config, days)) if e.get("job") != "content"]
    if not h:
        return 0.0
    first = min(date.fromisoformat(e["ts"][:10]) for e in h)
    span = max((date.today() - first).days + 1, 30)
    return round(sum(e["usd"] for e in h) * 30 / span, 2)


def affordable_per_week(config) -> float | None:
    """New pages a week this site's monthly budget covers, after the other jobs. None: no cap or no history."""
    s = settings(config)
    cap = s["monthly_usd"]
    g = global_cap()
    if cap is None and g is None:
        return None
    cap = min(x for x in (cap, g) if x is not None)
    cpp = cost_per_page(config)
    if not cpp:
        return None
    return round(max(cap - other_monthly(config), 0) / cpp * 7 / 30.4, 1)


def gate(config, name: str, pages: int = 1) -> str | None:
    """Reason to skip this autopilot job now for budget, or None to run it."""
    if name not in PAID_JOBS:
        return None
    s = settings(config)
    share = used_share(config)
    if share is None:
        return None
    if share >= 1:
        return "monthly budget reached"
    if name in OPTIONAL_JOBS and share >= s["cut_optional_at"]:
        return f"{share:.0%} of the monthly budget used; optional jobs pause from {s['cut_optional_at']:.0%}"
    if name == "refresh" and share >= s["cut_refresh_at"]:
        return f"{share:.0%} of the monthly budget used; refreshes pause from {s['cut_refresh_at']:.0%}"
    est = job_cost(config, name) * (pages if name == "content" else 1)
    left = remaining(config)
    if left is not None and est > left:
        return f"would cost about ${est:.2f}; ${left:.2f} left this month"
    return None


# ---------------------------------------------------------------- report

def money(v: float) -> str:
    return f"${v:,.2f}"


def report_line(config) -> str:
    """One line for the weekly report."""
    t = totals(config)
    parts = sorted(t["by_service"].items(), key=lambda kv: -kv[1])
    detail = ", ".join(f"{SERVICE_NAMES.get(k, k)} {money(v)}" for k, v in parts if v >= 0.005)
    site_cap = settings(config)["monthly_usd"]
    head = f"Spend: {money(t['site'])}" + (f" of {money(site_cap)}" if site_cap is not None else " (no budget set)")
    cpp = cost_per_page(config)
    return (head + " this month" + (f" ({detail})" if detail else "")
            + (f"; about {money(cpp)} per new page" if cpp else "") + ".")


def asks(config) -> list[str]:
    """Budget asks for the weekly report: a cap that has been reached."""
    out = []
    for c in caps(config):
        if c["spent"] >= c["cap"]:
            what = "new pages and refreshes" if c["service"] is None else f"{SERVICE_NAMES[c['service']]} calls"
            out.append(f"{c['label']} reached ({money(c['spent'])} of {money(c['cap'])}); {what} are paused until "
                       f"the 1st. Raise it in the config to continue.")
    return out


def render(config=None, month: str | None = None) -> str:
    month = month or month_of()
    t = totals(config, month)
    lines = [f"Spend for {month}" + (f": {config['site_name']}" if config else ": all sites"), ""]
    lines.append(f"Total: {money(t['site'] if config else t['all'])}"
                 + (f" ({money(t['estimated'])} estimated)" if t["estimated"] else ""))
    for k, v in sorted(t["by_service"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {SERVICE_NAMES.get(k, k):<11} {money(v)}")
    if t["by_job"]:
        lines.append("By job:")
        for k, v in sorted(t["by_job"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {k:<14} {money(v)}")
    if config:
        cs = caps(config)
        if cs:
            lines.append("Caps:")
            for c in cs:
                lines.append(f"  {c['label']}: {money(c['spent'])} of {money(c['cap'])} "
                             f"({c['spent'] / c['cap']:.0%})" if c["cap"] else f"  {c['label']}: $0 cap")
        else:
            lines.append("Caps: none. Set budget.monthly_usd in the config, or MONTHLY_BUDGET_USD in .env.")
        cpp = cost_per_page(config)
        lines.append(f"Cost per new page (90 days): {money(cpp)}" if cpp else "Cost per new page: no pages written yet")
        aff = affordable_per_week(config)
        if aff is not None:
            lines.append(f"The budget covers about {aff:g} new pages a week after other jobs "
                         f"({money(other_monthly(config))}/month).")
    return "\n".join(lines) + "\n"


def cli(config_path=None, all_sites: bool = False, month: str | None = None) -> int:
    from config import load_config
    if config_path:
        print(render(load_config(config_path), month))
        return 0
    print(render(None, month))
    if all_sites:
        sites = sorted({e.get("site") for e in _calls(load(month))})
        for s in sites:
            total = sum(e["usd"] for e in _calls(load(month)) if e.get("site") == s)
            print(f"  {s:<32} {money(total)}")
    g = global_cap()
    if g is not None:
        print(f"MONTHLY_BUDGET_USD: {money(totals(None, month)['all'])} of {money(g)}")
    return 0
