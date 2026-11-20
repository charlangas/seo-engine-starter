"""Publishing pace: how many new pages a week, on which days, and when the signals say it is safe to publish more.

    publishing:
      per_week: 2               # new pages a week (replaces cadence_per_week, which still works)
      days: [tue, fri]          # optional; by default per_week is spread over the week
      max_per_run: 2            # most pages one autopilot run writes when it is catching up after missed days
      ramp:                     # optional: recommend a faster pace once Google is taking the pages well
        to: 4                   # the most pages a week to recommend
        min_months: 6           # months since the first engine page
        min_indexed_rate: 0.8   # share of pages 14 to 90 days old that Google has indexed
        min_impressions_growth: 0.2   # Search Console impressions now vs ~90 days ago (+20%)
        min_keywords_waiting: 20      # researched keywords not yet written
        slow_below: 0.5         # indexed rate under this: recommend slowing down

Autopilot's content job checks every day and writes a page only when a publishing day has no page yet, so the pace
is exactly `per_week`. A page gets the date of its publishing day (today, when it is catching up); slots missed more
than a week ago are dropped rather than written in a burst.

The ramp never changes the pace by itself. When every signal passes, the weekly report asks the owner to raise
`per_week` by one; when Google indexes too few new pages, it asks to slow down. Signals with no data yet (no Search
Console key, too few inspected pages) block the step up.

  python main.py pace --config configs/site.yaml
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from config import ConfigError

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SPREAD = {1: [1], 2: [1, 4], 3: [0, 2, 4], 4: [0, 1, 3, 4], 5: [0, 1, 2, 3, 4], 6: [0, 1, 2, 3, 4, 5],
          7: [0, 1, 2, 3, 4, 5, 6]}
RAMP_DEFAULTS = {"to": 4, "min_months": 6, "min_indexed_rate": 0.8, "min_impressions_growth": 0.2,
                 "min_keywords_waiting": 20, "slow_below": 0.5, "min_inspected": 5}
DEFAULTS = {"per_week": None, "days": None, "max_per_run": 2, "ramp": None}
CATCH_UP_DAYS = 7


def settings(config) -> dict:
    raw = config.get("publishing") or {}
    if not isinstance(raw, dict):
        raise ConfigError("publishing must be a mapping")
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise ConfigError(f"publishing: unknown key(s) {sorted(unknown)}; allowed: {sorted(DEFAULTS)}")
    s = {**DEFAULTS, **raw}
    per_week = s["per_week"] if s["per_week"] is not None else config.get("cadence_per_week", 3)
    if not isinstance(per_week, int) or isinstance(per_week, bool) or not 1 <= per_week <= 7:
        raise ConfigError("publishing.per_week must be a whole number from 1 to 7")
    s["per_week"] = per_week
    config["cadence_per_week"] = per_week   # older code and reports read this name
    if s["days"] is None:
        s["weekdays"] = SPREAD[per_week]
    else:
        if not isinstance(s["days"], list) or not all(isinstance(d, str) and d[:3].lower() in DAY_NAMES for d in s["days"]):
            raise ConfigError(f"publishing.days must be a list of weekdays ({', '.join(DAY_NAMES)})")
        wd = sorted({DAY_NAMES.index(d[:3].lower()) for d in s["days"]})
        if len(wd) != per_week:
            raise ConfigError(f"publishing.days names {len(wd)} day(s) but per_week is {per_week}; make them match")
        s["weekdays"] = wd
    if not isinstance(s["max_per_run"], int) or s["max_per_run"] < 1:
        raise ConfigError("publishing.max_per_run must be a whole number of pages, 1 or more")
    if s["ramp"] is not None:
        if not isinstance(s["ramp"], dict) or set(s["ramp"]) - set(RAMP_DEFAULTS):
            raise ConfigError(f"publishing.ramp: allowed keys {sorted(RAMP_DEFAULTS)}")
        s["ramp"] = {**RAMP_DEFAULTS, **s["ramp"]}
        if not isinstance(s["ramp"]["to"], int) or not 1 <= s["ramp"]["to"] <= 7:
            raise ConfigError("publishing.ramp.to must be a whole number from 1 to 7")
    return s


# ---------------------------------------------------------------- dates

def _engine_dates(config) -> list[str]:
    import inventory
    import output
    return sorted(e["date"][:10] for e in inventory.load(config)
                  if e.get("type") == "article" and e.get("status") == "written" and e.get("date")
                  and not e.get("source") and not output.is_imported(e))


def last_date(config) -> date | None:
    d = _engine_dates(config)
    return date.fromisoformat(d[-1]) if d else None


def slots(config, start: date, end: date) -> list[date]:
    """Publishing days from start to end, both included."""
    wd = set(settings(config)["weekdays"])
    out, d = [], start
    while d <= end:
        if d.weekday() in wd:
            out.append(d)
        d += timedelta(days=1)
    return out


def next_slot(config, after: date) -> date:
    return slots(config, after + timedelta(days=1), after + timedelta(days=7))[0]


def open_slots(config, today: date) -> list[date]:
    """Publishing days up to today that have no page yet (the last week only)."""
    last = last_date(config)
    if last is None or last >= today:
        return []
    return slots(config, max(today - timedelta(days=CATCH_UP_DAYS - 1), last + timedelta(days=1)), today)


def owed(config, today: date | None = None) -> int:
    """Pages the schedule wants written now."""
    today = today or date.today()
    last = last_date(config)
    if last is None:
        return 1   # a new site starts today, whatever the weekday
    return min(len(open_slots(config, today)), settings(config)["max_per_run"])


def next_dates(config, n: int, today: date | None = None) -> list[str]:
    """Publish dates for n new pages: open slots are dated today (never backdated), then the next publishing days
    after the last page already scheduled."""
    today = today or date.today()
    last = last_date(config)
    out: list[date] = []
    if last is None:
        out, cursor = [today], today          # a new site starts today
    elif last >= today:
        cursor = last                         # pages are already scheduled ahead: continue after them
    else:
        out, cursor = [today] * min(len(open_slots(config, today)), n), today
    while len(out) < n:
        cursor = next_slot(config, cursor)
        out.append(cursor)
    return [d.isoformat() for d in out[:n]]


# ---------------------------------------------------------------- ramp signals

def indexed_rate(config, today: date) -> tuple[float | None, int]:
    """Share of engine pages 14 to 90 days old that URL Inspection found indexed, and how many were inspected."""
    import health
    f = config["_paths"]["data"] / "health" / "indexing.json"
    if not f.exists():
        return None, 0
    try:
        state = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None, 0
    lo, hi = (today - timedelta(days=90)).isoformat(), (today - timedelta(days=14)).isoformat()
    pages = [p for p in health.engine_pages(config, today).values() if lo <= p["pub"] <= hi]
    seen = [state[p["url"]] for p in pages if p["url"] in state]
    if not seen:
        return None, 0
    return sum(1 for s in seen if s.get("verdict") == "PASS") / len(seen), len(seen)


def impressions_growth(config, today: date) -> tuple[float | None, int | None, int | None]:
    """Search Console impressions (28 days) in the latest daily check vs the check closest to 90 days earlier."""
    d = config["_paths"]["data"] / "gsc"
    files = sorted(d.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")) if d.exists() else []
    if not files:
        return None, None, None

    def imp(f):
        try:
            return int(json.loads(f.read_text(encoding="utf-8"))["totals"]["listed_page_impressions"])
        except (ValueError, KeyError, TypeError):
            return None

    latest = files[-1]
    target = date.fromisoformat(latest.stem) - timedelta(days=90)
    old = [f for f in files if date.fromisoformat(f.stem) <= target + timedelta(days=15)]
    if not old:
        return None, imp(latest), None
    base = min(old, key=lambda f: abs((date.fromisoformat(f.stem) - target).days))
    now, then = imp(latest), imp(base)
    if now is None or then is None:
        return None, now, then
    return (now - then) / max(then, 1), now, then


def keywords_waiting(config) -> int | None:
    import inventory
    f = config["_paths"]["data"] / "ranked_keywords.json"
    if not f.exists():
        return None
    try:
        ranked = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return None
    done = {inventory.normalize_keyword(e["primary_keyword"]) for e in inventory.load(config)}
    return sum(1 for r in ranked if inventory.normalize_keyword(r["keyword"]) not in done)


def status(config, today: date | None = None) -> dict:
    """Current pace, what is owed today, and whether the signals support a faster (or slower) pace."""
    import budget
    today = today or date.today()
    s = settings(config)
    per_week = s["per_week"]
    dates = _engine_dates(config)
    out = {"per_week": per_week, "days": [DAY_NAMES[d] for d in s["weekdays"]], "owed": owed(config, today),
           "next_dates": next_dates(config, 3, today), "ramp": bool(s["ramp"]), "ready": False, "slow_down": False,
           "next": None, "passed": [], "blocking": [], "signals": {}}
    rate, inspected = indexed_rate(config, today)
    growth, imp_now, imp_then = impressions_growth(config, today)
    months = ((today - date.fromisoformat(dates[0])).days / 30.4) if dates else 0.0
    waiting = keywords_waiting(config)
    affordable = budget.affordable_per_week(config)
    out["signals"] = {"months": round(months, 1), "indexed_rate": rate, "inspected": inspected,
                      "impressions_growth": growth, "impressions_now": imp_now, "impressions_then": imp_then,
                      "keywords_waiting": waiting, "affordable_per_week": affordable}
    r = s["ramp"]
    if not r:
        return out
    if rate is not None and inspected >= r["min_inspected"] and rate < r["slow_below"] and per_week > 1:
        out["slow_down"] = True
        out["blocking"].append(f"only {rate:.0%} of {inspected} new pages are indexed")
        return out
    if per_week >= r["to"]:
        out["blocking"].append(f"already at the ramp ceiling ({r['to']} a week)")
        return out
    nxt = per_week + 1
    out["next"] = nxt

    def need(ok, yes, no):
        (out["passed"] if ok else out["blocking"]).append(yes if ok else no)

    need(months >= r["min_months"], f"{months:.0f} months of publishing",
         f"{months:.1f} of {r['min_months']} months of publishing")
    if rate is None or inspected < r["min_inspected"]:
        out["blocking"].append(f"indexing unknown ({inspected} recent page(s) inspected; needs a Search Console key "
                               f"and {r['min_inspected']})")
    else:
        need(rate >= r["min_indexed_rate"], f"{rate:.0%} of new pages indexed",
             f"{rate:.0%} of new pages indexed (needs {r['min_indexed_rate']:.0%})")
    if growth is None:
        out["blocking"].append("impressions trend unknown (needs about 90 days of Search Console checks)")
    else:
        need(growth >= r["min_impressions_growth"], f"impressions {growth:+.0%} over 90 days",
             f"impressions {growth:+.0%} over 90 days (needs {r['min_impressions_growth']:+.0%})")
    if waiting is None:
        out["blocking"].append("no keyword research yet")
    else:
        need(waiting >= r["min_keywords_waiting"], f"{waiting} keywords waiting",
             f"{waiting} keywords waiting (needs {r['min_keywords_waiting']})")
    if affordable is not None:
        need(affordable >= nxt, f"the budget covers about {affordable:g} a week",
             f"the budget covers about {affordable:g} a week, not {nxt}")
    out["ready"] = not out["blocking"]
    return out


def asks(config, today: date | None = None) -> tuple[list[str], list[str]]:
    """(urgent, normal) asks for the weekly report."""
    st = status(config, today)
    path = config.get("config_path", "the config")
    if st["slow_down"]:
        sig = st["signals"]
        return [f"Google has indexed only {sig['indexed_rate']:.0%} of {sig['inspected']} recent pages; consider "
                f"publishing {st['per_week'] - 1} a week instead of {st['per_week']} until that recovers "
                f"(publishing.per_week in {path})"], []
    if st["ready"]:
        return [], [f"Ready to publish {st['next']} a week instead of {st['per_week']}: " + ", ".join(st["passed"])
                    + f". To approve, set publishing.per_week: {st['next']} in {path}."]
    return [], []


def report_line(config, today: date | None = None) -> str:
    st = status(config, today)
    line = f"Pace: {st['per_week']} a week ({', '.join(st['days'])}); next page {st['next_dates'][0]}"
    if st["ramp"] and not st["ready"] and not st["slow_down"] and st["blocking"] and st["next"]:
        line += f"; not ready for {st['next']} a week: {st['blocking'][0]}"
    return line + "."


def render(config, today: date | None = None) -> str:
    st = status(config, today)
    sig = st["signals"]
    lines = [f"Publishing pace: {config['site_name']}", "",
             f"{st['per_week']} new page(s) a week on {', '.join(st['days'])}.",
             f"Owed today: {st['owed']}. Next dates: {', '.join(st['next_dates'])}.", "", "Signals:",
             f"  months publishing   {sig['months']}",
             f"  indexed rate        " + (f"{sig['indexed_rate']:.0%} of {sig['inspected']} inspected"
                                         if sig["indexed_rate"] is not None else "unknown"),
             f"  impressions (28d)   " + (f"{sig['impressions_now']} now vs {sig['impressions_then']} ~90 days ago "
                                         f"({sig['impressions_growth']:+.0%})" if sig["impressions_growth"] is not None
                                         else "trend unknown"),
             f"  keywords waiting    {sig['keywords_waiting'] if sig['keywords_waiting'] is not None else 'unknown'}",
             f"  budget covers       " + (f"about {sig['affordable_per_week']:g} a week"
                                         if sig["affordable_per_week"] is not None else "no cap or no history"), ""]
    if not st["ramp"]:
        lines.append("Ramp: off (add publishing.ramp to get a recommendation when it is safe to publish more).")
    elif st["slow_down"]:
        lines.append(f"Recommendation: slow down to {st['per_week'] - 1} a week ({st['blocking'][0]}).")
    elif st["ready"]:
        lines.append(f"Recommendation: ready for {st['next']} a week. Set publishing.per_week: {st['next']} to approve.")
    else:
        lines.append("Not ready to publish more: " + "; ".join(st["blocking"]) + ".")
    return "\n".join(lines) + "\n"


def cli(config_path) -> int:
    from config import load_config
    print(render(load_config(config_path)))
    return 0
