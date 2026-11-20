"""cluster.py: SERP-overlap grouping in Stage 1, the page map, merge/301 suggestions. Fake SERP cache, no spend."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cluster, config as cfg, inventory, research
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
checks = []


def make_config(extra=""):
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=False)
    c["_paths"].update(data=tmp, work=tmp, briefs=tmp / "briefs", inventory=tmp / "inventory.json",
                       topical_map=tmp / "topical_map.json", cache=tmp / "cache")
    (tmp / "cache" / "serp").mkdir(parents=True)
    return c, tmp


def serp(c, kw, urls):
    cluster.cache_file(kw, c).write_text(json.dumps({"keyword": kw, "organic": [{"url": u} for u in urls]}))


A = [f"https://site{i}.com/a/" for i in range(10)]
c, tmp = make_config()
serp(c, "plumber cost", A)
serp(c, "plumber prices", A[:4] + [f"https://other{i}.com/" for i in range(6)])          # 4 shared: same page
serp(c, "water heater repair", [f"https://r{i}.com/" for i in range(10)])
serp(c, "water heater replacement", [f"https://x{i}.com/" for i in range(10)])          # 0 shared: different
serp(c, "drain cleaning", [f"https://d{i}.com/" for i in range(10)])

# 1. basics
checks.append(("URLs compare without www, scheme or trailing slash", cluster.norm_url("https://www.A.com/x/") == cluster.norm_url("http://a.com/x"), ""))
checks.append(("cached SERP is read whatever its age; a missing one is None (never fetched)",
               len(cluster.cached_urls("plumber cost", c)) == 10 and cluster.cached_urls("nothing cached", c) is None, ""))
for extra, label in (("\nserp_clustering: {min_shared_urls: 0}\n", "zero threshold"), ("\nserp_clustering: {foo: 1}\n", "unknown key")):
    try:
        make_config(extra)
        checks.append((f"bad serp_clustering rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad serp_clustering rejected ({label})", "serp_clustering" in str(e), str(e)[-60:]))

# 2. Stage 1
inventory.add_entry(c, "plumber cost", "plumber-cost", status="written", intent="commercial")
DATA = {"plumber prices": (900, 10, "commercial"), "water heater repair": (800, 10, "commercial"),
        "water heater replacement": (700, 10, "commercial"), "drain cleaning": (600, 10, "commercial")}
research.keyword_ideas = lambda config, seeds: list(DATA)
research.keyword_suggestions = research.related_keywords = lambda config, seed, limit: []
research.keyword_overview = lambda config, kws: {k: {"volume": DATA[k][0], "cpc": None} for k in kws if k in DATA}
research.bulk_keyword_difficulty = lambda config, kws: {k: DATA[k][1] for k in kws if k in DATA}
research.search_intent = lambda config, kws: {k: DATA[k][2] for k in kws if k in DATA}
c.update(volume_floor=10, kd_max=100, seed_relevance_min=0, max_keywords_per_run=5, duplicate_similarity_threshold=0.7,
         intent_priority=["commercial", "informational", "transactional"])
sel = [x["keyword"] for x in research.get_opportunities(c)]
rej = {r["keyword"]: r for r in json.loads((tmp / "rejected_keywords.json").read_text())} if (tmp / "rejected_keywords.json").exists() else {}
checks.append(("a keyword with the same SERP as a written page is rejected and recorded on that page",
               "plumber prices" not in sel and rej.get("plumber prices", {}).get("reason") == "same_serp_as_existing"
               and inventory.find("plumber cost", c)["serp_cluster"][0]["keyword"] == "plumber prices", (sel, rej.get("plumber prices"))))
checks.append(("look-alike keywords with different SERPs are both kept",
               {"water heater repair", "water heater replacement"} <= set(sel), sel))

# 3. page map
(tmp / "ranked_keywords.json").write_text(json.dumps([{"keyword": k, "volume": v[0]} for k, v in DATA.items()]
                                                     + [{"keyword": "sump pump", "volume": 50}]))
serp(c, "drain cleaning service", [f"https://d{i}.com/" for i in range(5)] + [f"https://z{i}.com/" for i in range(5)])
(tmp / "ranked_keywords.json").write_text(json.dumps(json.loads((tmp / "ranked_keywords.json").read_text())
                                                     + [{"keyword": "drain cleaning service", "volume": 40}]))
m = cluster.build_map(c)
page = next(p for p in m["pages"] if p["slug"] == "plumber-cost")
planned = {g["primary_keyword"]: [k["keyword"] for k in g["keywords"]] for g in m["planned_pages"]}
checks.append(("page map: keyword -> existing URL with the reason", [k["keyword"] for k in page["keywords"]] == ["plumber cost", "plumber prices"]
               and "shared top-10 URLs" in page["keywords"][1]["why"] and page["url"] == "/blog/plumber-cost/", page))
checks.append(("new keywords sharing a SERP are grouped onto one planned page",
               planned.get("drain cleaning") == ["drain cleaning", "drain cleaning service"]
               and "water heater repair" in planned and "water heater replacement" in planned, planned))
checks.append(("keywords with no cached SERP are listed as unmapped", [u["keyword"] for u in m["unmapped"]] == ["sump pump"], m["unmapped"]))

# 4. conflicts from Search Console
inventory.add_entry(c, "plumber prices list", "plumber-prices-list", status="written")
serp(c, "plumber prices list", A[:5] + [f"https://q{i}.com/" for i in range(5)])
inventory.add_entry(c, "water heater repair", "water-heater-repair", status="written")
inventory.add_entry(c, "water heater replacement", "water-heater-replacement", status="written")
(tmp / "gsc").mkdir()
(tmp / "gsc" / "latest.json").write_text(json.dumps({"cannibalisation": [
    {"query": "plumber price", "impressions": 900, "pages": [{"page": "/blog/plumber-cost/", "impressions": 600, "position": 6.0},
                                                             {"page": "/blog/plumber-prices-list/", "impressions": 300, "position": 9.0}]},
    {"query": "water heater", "impressions": 500, "pages": [{"page": "/blog/water-heater-repair/", "impressions": 300, "position": 12.0},
                                                            {"page": "/blog/water-heater-replacement/", "impressions": 200, "position": 5.0}]}]}))
m = cluster.build_map(c)
cf = {x["query"]: x for x in m["conflicts"]}
checks.append(("one intent (shared SERP) -> merge, with a 301 line from the weaker page",
               cf["plumber price"]["action"] == "merge" and cf["plumber price"]["redirect"] == "/blog/plumber-prices-list/ /blog/plumber-cost/ 301", cf["plumber price"]))
checks.append(("different intents -> differentiate; the clearly better-ranking page is kept",
               cf["water heater"]["action"] == "differentiate" and cf["water heater"]["keep"] == "/blog/water-heater-replacement/", cf["water heater"]))
md = cluster.render(m)
checks.append(("report shows conflicts, redirects, pages and keywords needing their own page",
               "Search Console conflicts" in md and "```" in md and "need their own page" in md, ""))

sys.exit(0 if report("cluster", checks) else 1)
