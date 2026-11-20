"""evidence.py: first-party items, matching, the hard critique rules, drafts, the gap report. No spend."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, evidence, writer
from textutil import split_frontmatter
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
checks = []


def make_config(extra="", tmp=None):
    tmp = tmp or Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=True)
    c["_paths"].update(data=tmp, work=tmp, briefs=tmp / "briefs", inventory=tmp / "inventory.json",
                       topical_map=tmp / "topical_map.json", content=tmp / "content")
    (tmp / "evidence").mkdir(exist_ok=True)
    return c, tmp


ITEMS = {
    "heater-case.yaml": """
type: case_study
title: "Austin duplex: tank water heater repaired for $340 instead of replaced"
summary: "A 7-year-old 50-gallon tank stopped heating. We replaced the thermostat and lower element."
numbers: ["$340 total", "$1,850 replacement quote", "2 hours on site"]
who: "Our service log, March 2026"
topics: [water heater repair, water heater replacement]
date: 2026-03-14
""",
    "quotes.yaml": """
- id: sunday-call
  type: quote
  title: "Customer on our emergency response"
  quote: "We called on a Sunday night and the tank was fixed before breakfast"
  who: "Maria G., homeowner in Austin"
  topics: [emergency plumber]
- type: screenshot
  title: "Call log: median response time by hour"
  image: /images/evidence/response-times.png
  image_alt: "Median response time by hour of day"
  numbers: ["38 minutes median response"]
  topics: [emergency plumber]
""",
    "general.yaml": """
type: data
title: "Jobs we logged in 2025"
numbers: ["1,214 jobs"]
topics: ["*"]
""",
    "broken.yaml": """
- type: quote
  title: "No attribution"
  quote: "Great service"
  topics: [plumber]
- type: case_study
  title: "Nothing checkable"
  summary: "We did good work."
  topics: [plumber]
- type: memo
  title: "Bad type"
  numbers: ["12"]
  topics: [plumber]
""",
}

c, tmp = make_config()
for name, text in ITEMS.items():
    (tmp / "evidence" / name).write_text(text)
(tmp / "evidence" / "notes.txt").write_text("ignored")

# 1. loading and validation
items, errors = evidence.load(c)
ids = sorted(i["id"] for i in items)
checks.append(("valid items load (single item and list files; ids from file name or position)",
               ids == ["general", "heater-case", "quotes-2", "sunday-call"], ids))
checks.append(("invalid items are reported, never offered (quote without who, nothing checkable, bad type)",
               len(errors) == 3 and any("quote needs quote and who" in e for e in errors)
               and any("something checkable" in e for e in errors) and any("type must be" in e for e in errors), errors))

# 2. matching
pick = lambda kw, comp=(): [i["id"] for i in evidence.select(items, kw, (), comp, 3)]
checks.append(("topic contained in the keyword matches first; '*' is the fallback",
               pick("water heater repair cost")[:1] == ["heater-case"] and "general" in pick("water heater repair cost"), pick("water heater repair cost")))
checks.append(("an unrelated keyword only gets the general item", pick("drain cleaning") == ["general"], pick("drain cleaning")))
checks.append(("emergency pages get the quote and the screenshot",
               set(pick("emergency plumber austin")) >= {"sunday-call", "quotes-2"}, pick("emergency plumber austin")))
only_specific = [i for i in items if i["id"] != "general"]
checks.append(("no match at all -> marked missing", evidence.plan(c, "sump pump install", items=only_specific)["missing"]
               == ["no_first_party_evidence"], ""))

# 3. config
for extra, label in (("\nevidence: {when_missing: hide}\n", "unknown when_missing"), ("\nevidence: {max: 2}\n", "unknown key"),
                     ("\nevidence: {max_items: 0}\n", "zero max_items")):
    try:
        make_config(extra)
        checks.append((f"bad evidence config rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad evidence config rejected ({label})", "evidence" in str(e), str(e)[-90:]))

# 4. attach + prompt
brief = {"primary_keyword": "emergency plumber austin", "secondary_keywords": []}
ev = evidence.attach(brief, c)
block = evidence.prompt_block(brief)
checks.append(("prompt lists figures, the verbatim quote and the screenshot markdown",
               "38 minutes median response" in block and "fixed before breakfast" in block
               and "](/images/evidence/response-times.png)" in block and 'titled "How we know this"' in block, block[:300]))
checks.append(("matched item ids are recorded for the inventory", brief["inventory_extra"]["evidence"] == [i["id"] for i in ev["items"]], ""))
none_brief = {"primary_keyword": "x", "evidence": {"items": []}}
checks.append(("with no evidence, the prompt forbids invented first-party claims", "Do not claim results" in evidence.prompt_block(none_brief), ""))

# 5. critique
def codes(body, b=brief):
    out = []
    evidence.critique({"body": body}, b, lambda code, msg, **k: out.append(code))
    return out


generic = "# Emergency plumber austin\n\nIntro.\n\n## Costs\n\nYou pay a call-out fee.\n\n## FAQ\n\n### Q?\n\nA.\n"
checks.append(("a page with no first-party material fails (missing item, missing section, screenshot not embedded)",
               set(codes(generic)) == {"evidence_missing", "evidence_section_missing", "evidence_image_missing"}, codes(generic)))
good = generic.replace("## FAQ", "## How we know this\n\nOur call log shows a 38 minutes median response across 2025, "
                       "from 1,214 jobs.\n\n![Median response time by hour of day](/images/evidence/response-times.png)\n\n"
                       '> "We called on a Sunday night and the tank was fixed before breakfast." Maria G.\n\n## FAQ')
checks.append(("a page that uses the evidence in its own section passes (years and small numbers are fine)", codes(good) == [], codes(good)))
padded = good.replace("from 1,214 jobs.", "from 1,214 jobs, and 97% of customers rebooked.")
checks.append(("a figure in the evidence section that no item supports is a required fix",
               codes(padded) == ["evidence_unsupported_number"], codes(padded)))
heading_only = generic.replace("## FAQ", "## What we found\n\nThe median response was 38 minutes.\n\n## FAQ")
checks.append(("any methodology-style heading counts; a figure used in the body counts as use",
               codes(heading_only) == ["evidence_image_missing"], codes(heading_only)))

# 6. analogy rule skips the evidence section (writer.critique)
full = {"title": "t", "description": "d", "body": good}
wbrief = {**brief, "target_word_count": 10, "gaps_to_address": [], "outline": [], "internal_link_targets": [],
          "external_sources": [], "placement_rules": {}}
w = writer.critique(full, wbrief, c)
checks.append(("the evidence section is not asked for an analogy", not any(f["code"] == "analogy_missing" and f["section"] == "How we know this"
                                                                            for f in w), [f["code"] for f in w]))

# 7. drafts
c2, tmp2 = make_config("\nevidence: {when_missing: draft, require_screenshots_for: [alternatives]}\n")
b2 = {"primary_keyword": "sump pump install", "secondary_keywords": []}
evidence.attach(b2, c2)
b3 = {"primary_keyword": "sump pump install", "secondary_keywords": []}
evidence.attach(b3, c2, refreshing=True)
checks.append(("when_missing: draft marks the page draft; a refresh never does", b2["evidence"]["draft"] and not b3["evidence"]["draft"], ""))
(tmp2 / "evidence" / "h.yaml").write_text(ITEMS["heater-case.yaml"])
b4 = {"primary_keyword": "water heater repair vs replacement", "secondary_keywords": [],
      "comparison": {"page_type": "alternatives", "competitors": []}}
evidence.attach(b4, c2)
checks.append(("comparison page types listed in require_screenshots_for need a screenshot item",
               b4["evidence"]["missing"] == ["no_screenshot"] and b4["evidence"]["draft"], b4["evidence"]["missing"]))

# 8. end to end with the template writer: a real saved brief + evidence -> section added, critique clean
saved = sorted((ROOT / "data" / "test-minimal" / "briefs").glob("brief_*water-heater*.json"))
if saved:
    real = json.loads(saved[0].read_text())
    md = writer.generate_article(real, c)
    fm, body = split_frontmatter(md)
    left = writer.blocking(writer.critique({"title": fm["title"], "description": fm["description"], "body": body.lstrip()}, real, c))
    checks.append(("template writer adds the evidence section and the page passes the gate",
                   "## How we know this" in body and "$340 total" in body and not left and real["evidence"]["items"],
                   [f["code"] for f in left]))
    real2 = json.loads(saved[0].read_text())
    md2 = writer.generate_article(real2, c2)
    checks.append(("a page with matching evidence is not drafted under when_missing: draft",
                   real2["evidence"]["missing"] == [] and not real2["evidence"]["draft"] and "## How we know this" in md2, real2["evidence"]["missing"]))
else:
    checks.append(("saved water-heater brief available for the end-to-end check", False, "none in data/test-minimal/briefs"))

# 9. gap report
(tmp / "ranked_keywords.json").write_text(json.dumps([
    {"keyword": "emergency plumber austin", "volume": 900, "kd": 20, "intent": "commercial"},
    {"keyword": "sump pump install cost", "volume": 700, "kd": 10, "intent": "commercial"},
    {"keyword": "what is a sump pump", "volume": 2000, "kd": 5, "intent": "informational"}]))
(tmp / "evidence" / "general.yaml").unlink()
r = evidence.report(c)
gaps = [p["keyword"] for p in r["pages"] if p["missing"]]
checks.append(("gap report lists pages with nothing to draw on, bottom of funnel first",
               gaps == ["sump pump install cost", "what is a sump pump"] and "heater-case" in r["unused_items"], (gaps, r["unused_items"])))
md = evidence.render(r)
checks.append(("report shows broken items and what to collect next", "Items that need fixing" in md and "Collect next" in md, ""))

# Owner limits: no own prices, no client names
qs = [q for x in evidence.founder_questions(c, r=r) for q in x["questions"]]
checks.append(("by default the owner is asked what a job cost", any("cost the customer" in q for q in qs), qs[:3]))
c2 = {**c, "evidence": {**(c.get("evidence") or {}), "share_prices": False, "name_clients": False}}
qs2 = [q for x in evidence.founder_questions(c2, r=evidence.report(c2)) for q in x["questions"]]
checks.append(("with share_prices false the owner is never asked about prices",
               qs2 and not any("cost the customer" in q or "paying" in q for q in qs2), qs2[:3]))
block = evidence.prompt_block({"evidence": evidence.plan(c2, "sump pump install cost")})
checks.append(("the writer is told not to state own prices or name clients",
               "Never state" in block and "prices, fees" in block and "Never name a client" in block, block[-300:]))
checks.append(("the default prompt carries neither limit",
               "Never name a client" not in evidence.prompt_block({"evidence": evidence.plan(c, "sump pump install cost")}), ""))
try:
    evidence.settings({**c, "evidence": {"share_prices": "no"}})
    checks.append(("share_prices must be true or false", False, ""))
except Exception as e:
    checks.append(("share_prices must be true or false", "true or false" in str(e), str(e)))

sys.exit(0 if report("evidence", checks) else 1)
