"""aivis.py (AI answer citations) and GA4 observed conversion rates in forecast.py. Mocked API, no spend."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aivis, config as cfg, forecast, inventory
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
checks = []


def make_config(extra=""):
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=False)
    c["_paths"].update(data=tmp, work=tmp, inventory=tmp / "inventory.json")
    return c, tmp


# 1. config
for extra, label in (("\nai_visibility: {prompts: [ok], engines: [bard]}\n", "unknown engine"),
                     ("\nai_visibility: {prompt: [x]}\n", "unknown key")):
    try:
        make_config(extra)
        checks.append((f"bad ai_visibility rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad ai_visibility rejected ({label})", "ai_visibility" in str(e), str(e)[-60:]))
c, tmp = make_config("\nai_visibility:\n  prompts: [best emergency plumber in austin, how much does water heater repair cost]\n")
checks.append(("brand terms default to the site name", aivis.settings(c)["brand_terms"] == ["Test Minimal"], ""))

# 2. a run with mocked DataForSEO
calls = []


def fake(config, endpoint, payload):
    calls.append(endpoint)
    if endpoint.startswith("serp/"):
        kw = payload["keyword"]
        refs = [{"url": "https://rival.com/emergency/"}] + ([{"url": "https://www.example-plumbing.com/blog/emergency-plumber/"}]
                                                         if "emergency" in kw else [])
        return [{"items": [{"type": "ai_overview", "items": [{"text": "Answer.", "references": refs}]},
                           {"type": "organic", "url": "https://rival.com/x"},
                           {"type": "organic", "url": "https://example-plumbing.com/blog/x/"}]}]
    return [{"items": [{"sections": [{"text": "Test Minimal and Rival both do repairs.",
                                       "annotations": [{"url": "https://rival.com/cost/", "title": "Cost"}]}]}]}]


aivis.dataforseo_post = fake
v = aivis.run(c)
r = {(x["prompt"][:4], x["engine"]): x for x in v["results"]}
checks.append(("one call per prompt per engine", len(calls) == 4 and calls.count("ai_optimization/chat_gpt/llm_responses/live") == 2, calls))
checks.append(("AI Overview: our citation (www or not) and our organic rank are found",
               r[("best", "google_ai_overview")]["cited"] and r[("best", "google_ai_overview")]["organic_rank"] == 2
               and not r[("how ", "google_ai_overview")]["cited"], r[("best", "google_ai_overview")]))
checks.append(("ChatGPT: brand named in the answer even when not cited; cited competitors listed",
               r[("how ", "chatgpt")]["mentioned"] and not r[("how ", "chatgpt")]["cited"]
               and r[("how ", "chatgpt")]["competitor_urls"] == ["https://rival.com/cost/"], r[("how ", "chatgpt")]))
checks.append(("totals and most-cited domains", v["cited"] == 1 and v["mentioned"] == 2 and v["top_cited_domains"][0][0] == "rival.com", v["top_cited_domains"]))
(tmp / "ai_visibility").mkdir()
prev = json.loads(json.dumps(v))
for x in prev["results"]:
    x["cited"] = x["engine"] == "chatgpt"
prev["generated"] = "2026-11-11"
(tmp / "ai_visibility" / "latest.json").write_text(json.dumps(prev))
v2 = aivis.run(c)
checks.append(("gained and lost citations against the previous check",
               any("google_ai_overview: best" in g for g in v2["gained"]) and len(v2["lost"]) == 2, (v2["gained"], v2["lost"])))
md = aivis.render(v2)
checks.append(("report shows answers, changes and most-cited sites", "Changes since the last check" in md and "rival.com" in md, ""))

# 3. briefs: pages to beat
(tmp / "ai_visibility" / "latest.json").write_text(json.dumps(v))
b = {"primary_keyword": "water heater repair cost"}
cited = aivis.attach(b, c)
checks.append(("a brief gets the URLs AI answers cite for matching prompts (never our own)",
               [x["url"] for x in cited] == ["https://rival.com/emergency/", "https://rival.com/cost/"] or
               [x["url"] for x in cited] == ["https://rival.com/cost/"], cited))
checks.append(("no matching prompt -> nothing added", aivis.cited_for(c, "sump pump install") == [], ""))
checks.append(("writer prompt lists them as pages to beat", "currently cite these pages" in aivis.prompt_block(b), ""))

# 4. GA4 observed conversion rates
c2, tmp2 = make_config("\nforecast: {min_sessions: 100}\n")
inventory.add_entry(c2, "water heater repair cost", "water-heater-repair-cost", status="written", intent="commercial")
inventory.add_entry(c2, "what is a sump pump", "what-is-a-sump-pump", status="written", intent="informational")
(tmp2 / "conversions.csv").write_text("# GA4 export\n# Start date: 20261001\nLanding page,Sessions,Key events\n"
                                      "/blog/water-heater-repair-cost/,400,20\n/blog/what-is-a-sump-pump/?utm=x,50,1\n/about/,900,3\n")
obs = forecast.observed_rates(c2, forecast.settings(c2))
checks.append(("GA4 rows map to page kinds through the inventory; other pages are ignored",
               obs["bottom"]["rate"] == 0.05 and obs["bottom"]["used"] and obs["top"]["sessions"] == 50 and not obs["top"]["used"], obs))
(tmp2 / "ranked_keywords.json").write_text(json.dumps([{"keyword": "water heater repair cost", "volume": 1000, "kd": 10, "intent": "commercial"}]))
f = forecast.build(c2)
row = next(k for k in f["keywords"] if k["keyword"] == "water heater repair cost")
checks.append(("observed rate replaces the default in the projection and the report says so",
               f["assumptions"]["conversion_rate"]["bottom"] == 0.05 and row["leads"] == round(1000 * 1.5 * 0.18 * 0.05, 2)
               and "Conversion rates from GA4" in forecast.render(f), row["leads"]))
c3, tmp3 = make_config("\nforecast: {min_sessions: 100, conversion_rate: {bottom: 0.02}}\n")
inventory.add_entry(c3, "water heater repair cost", "water-heater-repair-cost", status="written", intent="commercial")
(tmp3 / "conversions.csv").write_text((tmp2 / "conversions.csv").read_text())
checks.append(("a rate set in the config wins over GA4", not forecast.observed_rates(c3, forecast.settings(c3))["bottom"]["used"], ""))

sys.exit(0 if report("aivis", checks) else 1)
