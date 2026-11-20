"""refresh.py: which pages are due, how a page is revised and re-dated. Mocked brief and writer. No spend."""
import json, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, inventory, refresh, serp, writer
from textutil import build_frontmatter, split_frontmatter
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
TODAY = date(2026, 11, 18)
checks = []


def make_config(extra=""):
    p = TMP / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=False)
    content = TMP / "content"
    content.mkdir(exist_ok=True)
    c["_paths"].update(output=content, content=content, data=TMP / "data", work=TMP / "data",
                       briefs=TMP / "data" / "briefs", inventory=TMP / "data" / "inventory.json")
    c["_paths"]["briefs"].mkdir(parents=True, exist_ok=True)
    c.pop("publish", None)
    return c


BODY = ("# Water heater repair cost\n\nIntro paragraph about water heater repair cost in plain words.\n\n"
        "## What a repair costs\n\nA thermostat swap costs $150 at most shops and takes an hour or less to finish.\n\n"
        "![Our technician replacing a thermostat](/img/thermostat.jpg)\n\n"
        '<iframe src="https://www.youtube.com/embed/abc123" title="Repair walkthrough"></iframe>\n\n'
        "> We called on a Sunday night and the tank was fixed before breakfast, for less than the quote.\n\n"
        "> Tip\n\n"
        '<PriceCalculator kind="water-heater" />\n\n'
        "## Frequently asked questions about water heater repair cost\n\n### Is it worth repairing?\n\n"
        "Usually, when the tank is under ten years old and does not leak.\n")


def page(c, slug, kw, pub, updated=None, extra_fm=None, inv=None, body=BODY):
    fm = {"title": kw.title(), "description": f"What {kw} looks like in 2026, with real prices.", "pubDate": pub,
          "slug": slug, "tags": [kw], "primaryKeyword": kw, "heroImage": "/img/hero.jpg", **(extra_fm or {})}
    if updated:
        fm["updatedDate"] = updated
    (TMP / "content" / f"{slug}.md").write_text(build_frontmatter(fm) + "\n" + body)
    inventory.add_entry(c, kw, slug, status="written", date=pub.isoformat(), intent="commercial", **(inv or {}))


c = make_config()
d = lambda n: TODAY - timedelta(days=n)
page(c, "old-comparison", "asana alternatives", d(40), inv={"page_type": "alternatives", "competitors": ["Asana"]})
page(c, "fresh-article", "drain cleaning cost", d(20))
page(c, "old-article", "water heater repair cost", d(200), updated=d(100))
page(c, "a-draft", "sump pump repair", d(200), extra_fm={"draft": True})
page(c, "scheduled", "pipe burst repair", TODAY + timedelta(days=3))
page(c, "opted-out", "toilet repair cost", d(200), extra_fm={"refresh": False})
page(c, "imported", "boiler repair", d(200), inv={"source": "existing", "url_prefix": "/guides"})
page(c, "slipped", "leak detection", d(10))
page(c, "slipped-recent", "faucet repair", d(3))
(TMP / "data" / "gsc").mkdir(parents=True, exist_ok=True)
(TMP / "data" / "gsc" / "latest.json").write_text(json.dumps({"declines": [
    {"page": "/blog/slipped/", "impressions_before": 900, "impressions_now": 300, "position_before": 5.0, "position_now": 11.2},
    {"page": "/blog/slipped-recent/", "impressions_before": 500, "impressions_now": 100, "position_before": 6.0, "position_now": 9.0}]}))

# 1. selection
picked, passed = refresh.due(c, TODAY)
order = [p["entry"]["slug"] for p in picked]
why = {p["slug"]: p["reason"] for p in passed}
checks.append(("Search Console decline first, then oldest by age rule", order == ["slipped", "old-article", "old-comparison"], order))
checks.append(("comparison pages are due at 30 days, articles at 90 (age counts from updatedDate)",
               {p["entry"]["slug"]: p["kind"] for p in picked}.get("old-comparison") == "comparison"
               and why.get("fresh-article", "").startswith("fresh"), why.get("fresh-article")))
checks.append(("drafts, scheduled, opted-out and imported pages are never touched",
               why.get("a-draft") == "draft_not_published" and why.get("scheduled") == "not_published_yet"
               and why.get("opted-out") == "refresh_false_in_frontmatter" and why.get("imported") == "not_written_by_engine", why))
checks.append(("a slipped page updated days ago waits (min_age_days)", why.get("slipped-recent") == "updated_recently", why.get("slipped-recent")))
forced, _ = refresh.due(c, TODAY, slugs=["fresh-article"])
checks.append(("--slug forces a page regardless of age", [p["entry"]["slug"] for p in forced] == ["fresh-article"]
               and forced[0]["reason"] == "requested", forced))

# 2. config validation
for bad, label in (("\nrefresh: {max_age_days: {comparison: 0}}\n", "zero max age"), ("\nrefresh: {every: 3}\n", "unknown key")):
    try:
        make_config(bad)
        checks.append((f"bad refresh config is rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad refresh config is rejected ({label})", "refresh" in str(e), str(e)[:100]))
c2 = make_config("\nrefresh: {max_age_days: {article: 365}, max_per_run: 1}\n")
checks.append(("per-site overrides merge with defaults", refresh.settings(c2)["max_age_days"] == {"comparison": 30, "article": 365}, ""))

# 3. what must survive
keep = refresh.keepers(BODY)
kinds = sorted(k["kind"] for k in keep)
checks.append(("images, embeds, MDX components and real quotes are kept; one-word callouts are not",
               kinds == ["component", "embed", "image", "quote"], [k["label"] for k in keep]))
checks.append(("prompt lists every item to keep", all(k["label"] in refresh.prompt_block({"refresh": {"keep": keep}}) for k in keep), ""))

# 4. refreshing one page (mocked brief + writer)
BRIEF = {"primary_keyword": "water heater repair cost", "slug": "water-heater-repair-cost", "secondary_keywords": [],
         "intent": "commercial", "target_word_count": 40, "outline": [], "gaps_to_address": [],
         "internal_link_targets": [{"url": "/blog/old-article/", "anchor": "self"}, {"url": "/blog/x/", "anchor": "x"}],
         "external_sources": [], "placement_rules": {}, "schema_type": "Article", "serp_summary": {}}
seen = {"brief": None, "calls": []}
serp.build_brief = lambda item, config: json.loads(json.dumps(BRIEF))


class Fake:
    name = "fake"

    def draft(self, brief, config):
        raise AssertionError("a refresh must revise the existing page, not draft a new one")

    def revise(self, article, fixes, brief, config):
        seen["brief"], seen["calls"] = brief, seen["calls"] + [[f["code"] for f in fixes]]
        body = article["body"].replace("$150", "$175")
        if len(seen["calls"]) == 1:  # first pass loses the photo; the refresh rule must catch it
            body = body.replace("![Our technician replacing a thermostat](/img/thermostat.jpg)\n\n", "")
        else:
            body = body if "/img/thermostat.jpg" in body else body.replace(
                "## Frequently", "![Our technician replacing a thermostat](/img/thermostat.jpg)\n\n## Frequently")
        return {**article, "body": body}


writer.pick_backend = lambda config: Fake()
writer.critique = lambda article, brief, config: (lambda f: (refresh.critique(article, brief, lambda code, msg, **k: f.append({"code": code, "message": msg})), f)[1])([])
cand = next(p for p in picked if p["entry"]["slug"] == "old-article")
rec = refresh.refresh_page(c, cand, TODAY)
fm, body = split_frontmatter((TMP / "content" / "old-article.md").read_text())
checks.append(("existing page is revised (first call carries the refresh instruction)", seen["calls"][0][0] == "refresh", seen["calls"]))
checks.append(("a dropped photo is a required fix and gets put back", "refresh_dropped_image" in seen["calls"][1]
               and "/img/thermostat.jpg" in body, seen["calls"]))
checks.append(("pubDate kept, updatedDate is today", fm["pubDate"] == d(200) and fm["updatedDate"] == TODAY, (fm.get("pubDate"), fm.get("updatedDate"))))
checks.append(("frontmatter the engine does not own survives; slug unchanged; no draft flag added",
               fm.get("heroImage") == "/img/hero.jpg" and fm["slug"] == "old-article" and "draft" not in fm, fm))
checks.append(("the page never links to itself", all(not t["url"].endswith("/old-article/") for t in seen["brief"]["internal_link_targets"]), ""))
checks.append(("change record shows the price change", rec["changed"] and rec["prices_removed"] == ["150"]
               and rec["prices_added"] == ["175"], {k: rec[k] for k in ("prices_removed", "prices_added")}))
e = inventory.find("water heater repair cost", c)
checks.append(("inventory keeps its date and records the refresh", e["date"] == d(200).isoformat() and e["updated"] == TODAY.isoformat()
               and e["refresh_count"] == 1, e))

# 5. an identical revision does not move the date
Fake.revise = lambda self, article, fixes, brief, config: dict(article)
before = (TMP / "content" / "fresh-article.md").read_text()
rec2 = refresh.refresh_page(c, forced[0], TODAY)
checks.append(("unchanged text is not re-dated or rewritten", not rec2["changed"]
               and (TMP / "content" / "fresh-article.md").read_text() == before, rec2))

# 6. report
md = refresh.render({"site": "t", "date": TODAY.isoformat(), "dry_run": False, "refreshed": [rec, rec2], "failed": [],
                     "not_due": passed})
checks.append(("report lists why, price changes and pages not due", "Why: age" in md and "Prices removed: $150; added: $175" in md
               and "date was not moved" in md and "Pages not due" in md, ""))

sys.exit(0 if report("refresh", checks) else 1)
