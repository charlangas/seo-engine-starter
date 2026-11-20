"""forecast.py and money-pages mode in research.py. Mocked DataForSEO and SERPs. No spend."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apiclient, config as cfg, forecast, inventory, research, serp
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
checks = []


def make_config(extra="", tmp=None):
    tmp = tmp or Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=False)
    c["_paths"].update(data=tmp, work=tmp, briefs=tmp / "briefs", inventory=tmp / "inventory.json",
                       topical_map=tmp / "topical_map.json")
    return c


# 1. funnel stage
F = forecast.funnel_stage
cases = [("hubspot alternatives", None, None, "en", (), "bottom"), ("salesforce vs hubspot", None, None, "en", (), "bottom"),
         ("crm for real estate agents", None, None, "en", (), "bottom"), ("hubspot review", None, None, "en", (), "bottom"),
         ("how to export contacts from hubspot", None, None, "en", (), "bottom"),
         ("what is a crm", "informational", None, "en", (), "top"), ("why use a crm", "commercial", None, "en", (), "top"),
         ("which crm is best for agencies", None, None, "en", (), "middle"),
         ("marketing medical practice", "commercial", None, "en", (), "middle"),
         ("anything at all", None, "alternatives", "en", (), "bottom"),
         ("amortiguadores k y b son buenos", "commercial", None, "es", (), "bottom"),
         ("qué es un amortiguador", "informational", None, "es", (), "top"),
         ("amortiguadores jetta a4", "transactional", None, "es", (), "bottom"),
         ("amortiguadores guadalajara", "commercial", None, "es", (), "middle"),
         ("amortiguadores guadalajara", "commercial", None, "es", ("guadalajara",), "bottom")]
wrong = [(k, F(k, i, pt, l, ex), want) for k, i, pt, l, ex, want in cases if F(k, i, pt, l, ex) != want]
checks.append(("funnel stage: alternatives/vs/review/for-role/switching are bottom, questions top, Spanish too", not wrong, wrong))

# 2. per-keyword economics
c = make_config("\nforecast: {customer_value: 6000, lifetime_value: 18000, monthly_spend: 10000}\n")
s = forecast.settings(c)
m = forecast.monthly({"keyword": "hubspot alternatives", "volume": 1000, "kd": 10}, s)
checks.append(("volume -> clicks -> leads -> customers -> revenue (1000 × 1.5 × 0.18 × 0.03 × 0.15 × 6000)",
               m["clicks"] == 270 and m["leads"] == 8.1 and abs(m["customers"] - 1.215) < 1e-9 and m["revenue"] == 7290, m))
mc = forecast.monthly({"keyword": "asana alternatives", "volume": 1000, "page_type": "alternatives"}, s)
checks.append(("comparison pages use the comparison conversion rate", mc["kind"] == "comparison" and mc["leads"] == 13.5, mc))
mt = forecast.monthly({"keyword": "hubspot alternatives", "volume": 1000, "traffic_potential": 2800}, s)
checks.append(("traffic potential replaces volume, scaled from #1 to the top-3 average",
               mt["basis"] == "traffic_potential" and mt["clicks"] == round(2800 * 1.5 * 0.18 / 0.28, 1), mt))

# 3. projection
rows = [{**forecast.monthly({"keyword": "x alternatives", "volume": 1000}, s)}]
pr = forecast.project(rows, s, 0.5)
want_customers = sum(1.215 * 0.5 * min(1, mo / 6) for mo in range(1, 13))
checks.append(("12-month projection ramps linearly to full ranking at month 6",
               pr["months"][2]["customers"] == round(1.215 * 0.5 * 0.5, 2) and abs(pr["customers"] - round(want_customers, 1)) < 1e-9, pr["months"][:3]))
checks.append(("ROI uses lifetime gross profit against 12 months of spend",
               pr["spend"] == 120000 and pr["roi"] == round((round(want_customers * 18000) - 120000) / 120000, 2)
               and pr["roi_basis"] == "lifetime_gross_profit", {k: pr[k] for k in ("spend", "roi", "lifetime_gross_profit")}))
be = forecast.break_even(s)
checks.append(("break-even: spend / LTV customers, then leads and clicks", be["customers"] == round(10000 / 18000, 2)
               and be["leads"] == round(10000 / 18000 / 0.15, 1), be))
checks.append(("no money numbers are invented when customer_value is unset",
               "revenue" not in forecast.monthly({"keyword": "a vs b", "volume": 100}, forecast.settings(make_config())), ""))

# 4. config validation
for extra, label in (("\nforecast: {close_rate: 2}\n", "rate above 1"), ("\nforecast: {revenue: 5}\n", "unknown key"),
                     ("\nforecast: {conversion_rate: {landing: 0.1}}\n", "unknown conversion kind"),
                     ("\nmoney_pages: {enabled: true, target: 0}\n", "zero target")):
    try:
        make_config(extra)
        checks.append((f"bad config rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad config rejected ({label})", True, str(e)[-80:]))

# 5. the pool and the report
tmp = Path(tempfile.mkdtemp())
c = make_config("\nforecast: {customer_value: 6000, monthly_spend: 2000}\n", tmp)
(tmp / "ranked_keywords.json").write_text(json.dumps([
    {"keyword": "emergency plumber austin", "volume": 900, "kd": 20, "intent": "commercial"},
    {"keyword": "what is a sump pump", "volume": 2000, "kd": 5, "intent": "informational"},
    {"keyword": "water heater repair cost", "volume": 500, "kd": 10, "intent": "commercial"},
    {"keyword": "tankless water heater", "volume": 3000, "kd": 25, "intent": "commercial"}]))
(tmp / "comparison_keywords.json").write_text(json.dumps({"ranked": [
    {"keyword": "roto-rooter alternatives", "volume": 300, "kd": 15, "page_type": "alternatives"}], "rejected": []}))
inventory.add_entry(c, "tankless water heater", "tankless-water-heater", status="removed")
inventory.add_entry(c, "water heater repair cost", "water-heater-repair-cost", status="written", intent="commercial", volume=500)
f = forecast.build(c)
kws = {r["keyword"]: r for r in f["keywords"]}
checks.append(("pool merges research, comparisons and inventory; removed keywords are left out",
               set(kws) == {"emergency plumber austin", "what is a sump pump", "water heater repair cost", "roto-rooter alternatives"}
               and kws["water heater repair cost"]["status"] == "written" and kws["roto-rooter alternatives"]["kind"] == "comparison", sorted(kws)))
money = ("water heater repair cost", "roto-rooter alternatives", "emergency plumber austin")  # the site's city counts
checks.append(("only bottom-of-funnel keywords count toward the bet (the site's city makes a keyword bottom)",
               f["by_stage"]["bottom"]["keywords"] == 3 and kws["what is a sump pump"]["funnel"] == "top"
               and f["projection"]["monthly_at_full"]["leads"] == round(sum(kws[k]["leads"] for k in money) * 0.3, 2), f["by_stage"]))
md = forecast.render(f)
checks.append(("report names missing business numbers and shows ROI", "lifetime_value" in md and "ROI" in md
               and "customer_value" not in md.split("Your numbers still missing")[1].split("\n")[0], ""))

# 6. traffic potential (mocked SERP + DataForSEO)
serp.fetch_serp = lambda kw, config: {"organic": [{"domain": "example-plumbing.com", "url": "https://example-plumbing.com/x"},
                                                  {"domain": "rival.com", "url": f"https://rival.com/{kw.replace(' ', '-')}/"}]}
sent = {}


def fake_post(config, endpoint, payload):
    sent["endpoint"], sent["targets"] = endpoint, payload["targets"]
    return [{"items": [{"target": t, "metrics": {"organic": {"etv": 1234.4}}} for t in payload["targets"]]}]


apiclient.dataforseo_post = fake_post
items = [{"keyword": "emergency plumber austin", "volume": 900}, {"keyword": "roto-rooter alternatives", "volume": 300}]
n = forecast.traffic_potential(c, items, 1)
checks.append(("traffic potential: own domain skipped, one bulk call, only the top-N keywords",
               n == 1 and sent["endpoint"].endswith("bulk_traffic_estimation/live")
               and sent["targets"] == ["https://rival.com/emergency-plumber-austin/"] and items[0]["traffic_potential"] == 1234
               and "traffic_potential" not in items[1], (n, sent)))

# 7. money-pages mode in Stage 1
DATA = {"emergency plumber austin": (900, 20, "commercial"), "what is a sump pump": (5000, 2, "informational"),
        "how does a water heater work": (4000, 3, "informational"), "water heater repair cost": (600, 10, "commercial"),
        "plumber near me": (300, 15, "transactional")}
research.keyword_ideas = lambda config, seeds: list(DATA)
research.keyword_suggestions = research.related_keywords = lambda config, seed, limit: []
research.keyword_overview = lambda config, kws: {k: {"volume": DATA[k][0], "cpc": None} for k in kws if k in DATA}
research.bulk_keyword_difficulty = lambda config, kws: {k: DATA[k][1] for k in kws}
research.search_intent = lambda config, kws: {k: DATA[k][2] for k in kws}
base = ("\nvolume_floor: 10\nkd_max: 100\nseed_relevance_min: 0\nmax_keywords_per_run: 2\n"
        "intent_priority: [informational, commercial, transactional]\n")
tmp = Path(tempfile.mkdtemp())
off = research.get_opportunities(make_config(base, tmp))
checks.append(("money-pages mode off: selection by the normal score (informational wins on volume)",
               [x["keyword"] for x in off] == ["what is a sump pump", "how does a water heater work"], [x["keyword"] for x in off]))
tmp = Path(tempfile.mkdtemp())
c = make_config(base + "money_pages: {enabled: true, target: 2}\n", tmp)
on = research.get_opportunities(c)
ranked = json.loads((tmp / "ranked_keywords.json").read_text())
held = [x["keyword"] for x in ranked if x.get("held_by_money_pages")]
checks.append(("money-pages mode on: only bottom-of-funnel keywords selected, ordered by expected leads / difficulty",
               [x["keyword"] for x in on] == ["water heater repair cost", "emergency plumber austin"], [(x["keyword"], x["money_score"]) for x in on]))
checks.append(("held keywords stay ranked (not rejected) and are flagged", sorted(held) == ["how does a water heater work", "what is a sump pump"]
               and not any(x["selected"] for x in ranked if x.get("held_by_money_pages")), held))
for k in ("water heater repair cost", "emergency plumber austin"):
    inventory.add_entry(c, k, k.replace(" ", "-"), status="written", intent="commercial")
after = research.get_opportunities(c)
checks.append(("once the target is met, selection goes back to the normal score",
               forecast.money_mode_active(c) == (False, 2, 2) and [x["keyword"] for x in after][0] == "what is a sump pump",
               [x["keyword"] for x in after]))

sys.exit(0 if report("forecast", checks) else 1)
