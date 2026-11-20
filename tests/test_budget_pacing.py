"""Monthly budget (budget.py) and publishing pace (pacing.py): the spending ledger, dollar caps checked before every
paid call, job gating in autopilot, publishing days, catch-up, and the ramp signals. Fake transport, no spend."""
import json, os, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests_common import report
import config as cfg, inventory
from textutil import build_frontmatter

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 20)   # a Friday
checks = []
os.environ.update(OPENROUTER_API_KEY="test", DATAFORSEO_LOGIN="test", DATAFORSEO_PASSWORD="test", SERPAPI_KEY="test",
                  MONTHLY_BUDGET_USD="")


def fresh_ledger():
    os.environ["SEO_ENGINE_SPEND_DIR"] = tempfile.mkdtemp(prefix="seo-engine-test-spend-")


def make_config(extra=""):
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=False)
    content = tmp / "blog"
    content.mkdir()
    c["_paths"].update(output=content, content=content, data=tmp / "data", work=tmp / "data", briefs=tmp / "data" / "briefs",
                       inventory=tmp / "data" / "inventory.json", topical_map=tmp / "data" / "tm.json", cache=tmp / "cache")
    (tmp / "data").mkdir()
    return c, p


def bad(extra, label):
    try:
        make_config(extra)
        checks.append((label, False, extra))
    except cfg.ConfigError as e:
        checks.append((label, True, str(e)[:80]))


import budget, pacing, apiclient, requests
from config import BudgetExceeded

# ---------------------------------------------------------------- 1. settings
bad("\nbudget: {monthly_usd: -5}\n", "a negative budget is rejected")
bad("\nbudget: {services: {openai: 5}}\n", "an unknown service cap is rejected")
bad("\nbudget: {cut_optional_at: 2}\n", "a cut share above 1 is rejected")
bad("\npublishing: {per_week: 9}\n", "more than 7 pages a week is rejected")
bad("\npublishing: {per_week: 2, days: [mon, wed, fri]}\n", "publishing days must match per_week")
bad("\npublishing: {per_week: 2, ramp: {to: 12}}\n", "a ramp ceiling above 7 is rejected")
c, _ = make_config()
checks.append(("cadence_per_week still sets the pace when publishing is absent",
               pacing.settings(c)["per_week"] == 3 and pacing.settings(c)["weekdays"] == [0, 2, 4], pacing.settings(c)))
c, _ = make_config("\npublishing: {per_week: 2}\n")
checks.append(("2 a week spreads to Tuesday and Friday", pacing.settings(c)["weekdays"] == [1, 4]
               and c["cadence_per_week"] == 2, pacing.settings(c)))

# ---------------------------------------------------------------- 2. ledger and caps through the real API client
fresh_ledger()
c, _ = make_config("\nbudget: {monthly_usd: 1.0, services: {serpapi: 0.03}, serpapi_usd_per_search: 0.02}\n")
sent = []


class Resp:
    def __init__(self, body):
        self.status_code, self._body, self.text = 200, body, json.dumps(body)

    def json(self):
        return self._body


def fake(method, url, **kw):
    sent.append(url)
    if "openrouter" in url:
        return Resp({"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.4}})
    if "dataforseo" in url:
        return Resp({"status_code": 20000, "cost": 0.05, "tasks": [{"status_code": 20000, "result": [{"x": 1}]}]})
    return Resp({"organic_results": [{"link": "https://a.com"}]})


requests.request = fake
with budget.job("content"):
    apiclient.openrouter_chat(c, "m", "s", "u", "writer")
    apiclient.dataforseo_post(c, "keywords", {})
    apiclient.serpapi_get(c, {"engine": "google", "q": "x"})
t = budget.totals(c)
checks.append(("OpenRouter's reported cost, DataForSEO's cost field and SerpAPI's price per search are recorded",
               t["by_service"] == {"llm": 0.4, "dataforseo": 0.05, "serpapi": 0.02} and t["by_job"] == {"content": 0.47}, t))
apiclient.serpapi_get(c, {"engine": "google", "q": "y"})
n = len(sent)
try:
    apiclient.serpapi_get(c, {"engine": "google", "q": "z"})
    checks.append(("a service cap stops that service before the request is sent", False, ""))
except BudgetExceeded as e:
    checks.append(("a service cap stops that service before the request is sent", len(sent) == n, str(e)))
apiclient.dataforseo_post(c, "keywords", {})
checks.append(("other services keep going under a service cap", len(sent) == n + 1, len(sent)))
apiclient.openrouter_chat(c, "m", "s", "u", "writer")   # 0.4 + 0.05 + 0.04 + 0.05 + 0.4 = 0.94
apiclient.openrouter_chat(c, "m", "s", "u", "writer")   # 1.34: over the site cap
n = len(sent)
try:
    apiclient.dataforseo_post(c, "keywords", {})
    checks.append(("the site's monthly cap stops every paid call", False, ""))
except BudgetExceeded as e:
    checks.append(("the site's monthly cap stops every paid call", len(sent) == n and "monthly budget" in str(e), str(e)))
asks = budget.asks(c)
checks.append(("a reached cap becomes an ask in the weekly report", len(asks) == 2 and "paused until the 1st" in asks[0], asks))
checks.append(("the report line names the spend, the cap and the services",
               budget.report_line(c).startswith("Spend: $1.34 of $1.00 this month (OpenRouter $1.20"), budget.report_line(c)))

fresh_ledger()
os.environ["MONTHLY_BUDGET_USD"] = "0.5"
other, _ = make_config()
other["site_slug"] = "another-site"
budget.record(other, "llm", 0.6)
c, _ = make_config()
try:
    budget.check(c, "llm")
    checks.append(("MONTHLY_BUDGET_USD caps all sites together", False, ""))
except BudgetExceeded as e:
    checks.append(("MONTHLY_BUDGET_USD caps all sites together", "all sites" in str(e), str(e)))
os.environ["MONTHLY_BUDGET_USD"] = ""

# ---------------------------------------------------------------- 3. cost per page and gating
fresh_ledger()
c, _ = make_config("\nbudget: {monthly_usd: 20}\n")
for _ in range(2):
    with budget.job("content"):
        budget.record(c, "llm", 1.5)
        budget.record(c, "dataforseo", 0.5)
        budget.note_pages(c, 1)
with budget.job("refresh"):
    budget.record(c, "llm", 1.0)
checks.append(("cost per page divides content spend by pages written", budget.cost_per_page(c) == 2.0, budget.cost_per_page(c)))
checks.append(("below the cut-offs every job runs", all(budget.gate(c, j) is None for j in ("content", "refresh", "ai_visibility")),
               [budget.gate(c, j) for j in ("content", "refresh", "ai_visibility")]))
budget.record(c, "llm", 9.5)   # 14.5 of 20
checks.append(("from 70% optional jobs stop; refreshes and new pages go on",
               budget.gate(c, "ai_visibility") and budget.gate(c, "refresh") is None and budget.gate(c, "content") is None,
               budget.gate(c, "ai_visibility")))
budget.record(c, "llm", 2.6)   # 17.1 of 20
checks.append(("from 85% refreshes stop too; one new page still fits",
               budget.gate(c, "refresh") and budget.gate(c, "content", 1) is None, budget.gate(c, "refresh")))
checks.append(("a content run that would cost more than is left is skipped",
               "would cost" in (budget.gate(c, "content", 2) or ""), budget.gate(c, "content", 2)))
checks.append(("free jobs are never gated", budget.gate(c, "health") is None and budget.gate(c, "gsc_check") is None, ""))
checks.append(("the budget covers a pace (pages a week after other jobs)", budget.affordable_per_week(c) is not None,
               budget.affordable_per_week(c)))

# ---------------------------------------------------------------- 4. publishing days and catch-up
c, _ = make_config("\npublishing: {per_week: 2, max_per_run: 2}\n")
checks.append(("a new site gets its first page today, then the publishing days",
               pacing.next_dates(c, 3, TODAY) == ["2026-11-20", "2026-11-24", "2026-11-27"] and pacing.owed(c, TODAY) == 1,
               pacing.next_dates(c, 3, TODAY)))


def written(c, slug, d, **extra):
    inventory.add_entry(c, slug.replace("-", " "), slug, status="written", date=d.isoformat(), intent="commercial", **extra)


written(c, "first-page", date(2026, 11, 17))   # Tuesday
checks.append(("Friday after a Tuesday page: one page owed, dated today",
               pacing.owed(c, TODAY) == 1 and pacing.next_dates(c, 2, TODAY) == ["2026-11-20", "2026-11-24"],
               (pacing.owed(c, TODAY), pacing.next_dates(c, 2, TODAY))))
checks.append(("Thursday: nothing owed", pacing.owed(c, TODAY - timedelta(days=1)) == 0, ""))
checks.append(("after a missed slot, catch up but never more than max_per_run",
               pacing.owed(c, date(2026, 11, 24)) == 2 and pacing.owed(c, date(2026, 12, 15)) == 2, pacing.owed(c, date(2026, 11, 24))))
written(c, "ahead", date(2026, 11, 24))
checks.append(("with a page already scheduled ahead, nothing is owed and new dates follow it",
               pacing.owed(c, TODAY) == 0 and pacing.next_dates(c, 2, TODAY) == ["2026-11-27", "2026-12-01"],
               pacing.next_dates(c, 2, TODAY)))
inventory.add_entry(c, "old guide", "old-guide", status="written", date="2026-12-30", source="existing", url_prefix="/guides")
checks.append(("imported pages do not move the schedule", pacing.last_date(c) == date(2026, 11, 24), pacing.last_date(c)))

# ---------------------------------------------------------------- 5. ramp signals
ramp = "\npublishing: {per_week: 2, ramp: {to: 3, min_months: 6, min_keywords_waiting: 3}}\n"
c, _ = make_config(ramp)
st = pacing.status(c, TODAY)
checks.append(("with no data, the ramp is blocked and says why", not st["ready"] and len(st["blocking"]) >= 3, st["blocking"]))
urls = {}
for i, ago in enumerate((200, 80, 70, 60, 50, 40, 30, 20)):   # 6.6 months of publishing; 7 pages 14-90 days old
    d = TODAY - timedelta(days=ago)
    slug = f"page-{i}"
    fm = {"title": f"Page {i}", "description": "x" * 80, "pubDate": d, "slug": slug, "primaryKeyword": f"page {i}"}
    (c["_paths"]["content"] / f"{slug}.md").write_text(build_frontmatter(fm) + "\nbody\n")
    written(c, slug, d)
    urls[slug] = f"https://{c['domain']}/blog/{slug}/"
hd = c["_paths"]["data"] / "health"
hd.mkdir()
gd = c["_paths"]["data"] / "gsc"
gd.mkdir()
for d, imp in ((TODAY - timedelta(days=92), 400), (TODAY - timedelta(days=1), 700)):
    (gd / f"{d.isoformat()}.json").write_text(json.dumps({"totals": {"listed_page_impressions": imp}}))
(c["_paths"]["data"] / "ranked_keywords.json").write_text(json.dumps([{"keyword": f"kw {i}"} for i in range(5)]
                                                                     + [{"keyword": "page 1"}]))
(hd / "indexing.json").write_text(json.dumps({u: {"verdict": "PASS"} for u in urls.values()}))
rate, inspected = pacing.indexed_rate(c, TODAY)
growth = pacing.impressions_growth(c, TODAY)[0]
checks.append(("indexed rate counts pages 14 to 90 days old", inspected == 7 and rate == 1.0, (rate, inspected)))
checks.append(("impressions growth compares with the check ~90 days earlier", round(growth, 2) == 0.75, growth))
checks.append(("keywords waiting leaves out written ones", pacing.keywords_waiting(c) == 5, pacing.keywords_waiting(c)))
st = pacing.status(c, TODAY)
checks.append(("all signals pass: ready for one more a week, never applied by itself",
               st["ready"] and st["next"] == 3 and pacing.settings(c)["per_week"] == 2, st["blocking"]))
urgent, normal = pacing.asks(c, TODAY)
checks.append(("the ready ask tells the owner what to change", not urgent and "publishing.per_week: 3" in normal[0], normal))
(hd / "indexing.json").write_text(json.dumps({u: {"verdict": "NEUTRAL"} for u in urls.values()}))
st = pacing.status(c, TODAY)
urgent, normal = pacing.asks(c, TODAY)
checks.append(("too few pages indexed: recommend slowing down instead", st["slow_down"] and "1 a week" in urgent[0], urgent))
c3, _ = make_config("\npublishing: {per_week: 3, ramp: {to: 3}}\n")
checks.append(("at the ceiling, no step up", not pacing.status(c3, TODAY)["ready"], pacing.status(c3, TODAY)["blocking"]))

# ---------------------------------------------------------------- 6. autopilot
import autopilot
fresh_ledger()
c, cpath = make_config("\npublishing: {per_week: 2}\nbudget: {monthly_usd: 5}\nautopilot: {enabled: true}\n")
checks.append(("autopilot checks the content job daily", autopilot.settings(c)["days"]["content"] == 1, ""))
budget.record(c, "llm", 4.5)   # 90%
called = []
autopilot.JOBS = {k: (lambda name: (lambda path, config: (called.append(name), {"summary": f"{name} ok"})[1]))(k)
                  for k in autopilot.JOBS}
res = autopilot.run(cpath, today=TODAY, config=c, only=["refresh", "health"])
st = autopilot.load_state(c)
checks.append(("over the refresh cut-off, refresh is skipped and stays due; free jobs run",
               "refresh" not in called and "health" in called and "last_run" not in st["tasks"]["refresh"]
               and "skipped" in st["tasks"]["refresh"]["summary"], (called, st["tasks"])))
text, asks = autopilot.build_report(c, [], TODAY)
checks.append(("the weekly report shows spend and pace", "Spend: $4.50 of $5.00" in text and "Pace: 2 a week" in text, text))

sys.exit(0 if report("budget and pacing", checks) else 1)
