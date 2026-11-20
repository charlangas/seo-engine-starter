"""Competitor comparison pages (comparisons.py) with mocked DataForSEO, SerpAPI and page fetches. No spend."""
import json, sys, tempfile
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import comparisons, config as cfg, inventory, output, serp, writer
from textutil import split_frontmatter
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp())
BLOCK = {
    "enabled": True, "brand": "Acme", "brand_summary": "Project management for small agencies.",
    "brand_facts": ["Acme costs $12 per user per month."], "category": "project management tool",
    "use_cases": ["nonprofits"], "max_per_run": 2, "volume_floor": 100, "kd_max": 40, "max_rank_gap": 25,
    "own_domain_rank": 30, "cta": {"label": "Book a demo", "url": "/demo/"},
    "author": {"name": "Pat Lee", "title": "Founder", "socials": {"linkedin": "https://linkedin.com/in/x"}},
    "competitors": [{"name": "Asana", "domain": "asana.com", "priority": 2}, {"name": "Trello", "domain": "trello.com"}],
}


def make_config():
    raw = (ROOT / "configs" / "test-minimal.yaml").read_text() + "\ncomparisons: " + json.dumps(BLOCK) + "\n"
    p = TMP / "cmp.yaml"
    p.write_text(raw)
    c = cfg.load_config(p, dry_run=True)
    c["_paths"]["inventory"] = TMP / "inventory.json"
    c["_paths"]["work"] = TMP / "work"
    c["_paths"]["briefs"] = TMP / "work" / "briefs"
    c["_paths"]["topical_map"] = TMP / "work" / "topical_map.json"
    c["_paths"]["briefs"].mkdir(parents=True, exist_ok=True)
    return c


METRICS = {"asana alternatives": (1000, 10), "asana review": (500, 5), "trello alternatives": (300, 55),
           "asana vs trello": (400, 8), "trello vs asana": (600, 8), "best project management tool": (10000, 90),
           "asana alternatives for nonprofits": (150, 3)}
SERP_TOP = {"asana alternatives": ["zapier.com", "forbes.com", "g2.com"],
            "asana review": ["smallblog.io", "nichereview.net", "tinytools.co"],
            "asana alternatives for nonprofits": ["smallblog.io", "nichereview.net", "g2.com"],
            "trello vs asana": ["nichereview.net", "tinytools.co", "smallblog.io"],
            "acme vs asana": ["acme.com", "asana.com", "smallblog.io"]}
RANKS = {"zapier.com": 91, "forbes.com": 94, "g2.com": 88, "smallblog.io": 35, "nichereview.net": 42, "tinytools.co": 50}
calls = {"dataforseo": [], "serp": []}


def fake_dfs(config, endpoint, payload):
    calls["dataforseo"].append(endpoint)
    if endpoint.endswith("keyword_overview/live"):
        return [{"items": [{"keyword": k, "keyword_info": {"search_volume": METRICS[k][0]}}
                           for k in payload["keywords"] if k in METRICS]}]
    if endpoint.endswith("bulk_keyword_difficulty/live"):
        return [{"items": [{"keyword": k, "keyword_difficulty": METRICS[k][1]} for k in payload["keywords"] if k in METRICS]}]
    if endpoint == "backlinks/bulk_ranks/live":
        return [{"items": [{"target": d, "rank": RANKS.get(d)} for d in payload["targets"]]}]
    raise AssertionError(endpoint)


def fake_serp(keyword, config, expect_terms=()):
    calls["serp"].append(keyword)
    return {"organic": [{"domain": d, "resolved": True, "url": f"https://{d}/x"} for d in SERP_TOP.get(keyword, ["g2.com"])]}


def fake_serpapi(config, params):
    if params["engine"] == "youtube":
        return {"video_results": [{"title": "Asana review 2026", "link": "https://youtube.com/watch?v=1", "channel": {"name": "PM Tips"}}]}
    return {"organic_results": [{"title": "Leaving Asana, too expensive", "snippet": "The new tiers nearly doubled our bill.",
                                 "link": "https://www.reddit.com/r/projectmanagement/comments/abc"},
                                {"title": "Asana price increase again", "snippet": "Our renewal went up 40%.",
                                 "link": "https://www.reddit.com/r/projectmanagement/comments/def"},
                                {"title": "Is Asana worth the cost?", "snippet": "Expensive for a team of 12.",
                                 "link": "https://www.reddit.com/r/asana/comments/ghi"},
                                {"title": "Asana keeps crashing on mobile", "snippet": "One-off bug report.",
                                 "link": "https://www.reddit.com/r/asana/comments/jkl"}]}


PRICING_HTML = ("<html><body><main><h1>Asana pricing</h1><p>Starter costs $10.99 per user per month billed annually.</p>"
                "<p>Advanced costs $24.99 per user per month.</p><p>" + "Plans for every team size and workflow. " * 10 +
                "</p></main></body></html>")

comparisons.dataforseo_post = fake_dfs
comparisons.serpapi_get = fake_serpapi
comparisons.fetch_page = lambda config, url: PRICING_HTML if "asana" in url else None
serp.fetch_serp = fake_serp
checks = []

# 1. config validation
bad = TMP / "bad.yaml"
bad.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + "\ncomparisons: {enabled: true, category: x, competitors: []}\n")
try:
    cfg.load_config(bad, dry_run=True)
    checks.append(("missing brand / competitors is a config error", False, "loaded"))
except cfg.ConfigError as e:
    checks.append(("missing brand / competitors is a config error", "brand" in str(e) and "competitors" in str(e), str(e)[:120]))
c = make_config()
checks.append(("sites without a comparisons block are unaffected", comparisons.settings(cfg.load_config(ROOT / "configs" / "test-minimal.yaml", dry_run=True)) is None, ""))

# 2. candidate keywords: the four page types + best, in the video's order
cands = comparisons.candidates(comparisons.settings(c))
kws = [x["keyword"] for x in cands]
checks.append(("alternatives, review, own-versus, versus (both orders) and best are generated",
               {"asana alternatives", "asana review", "acme vs asana", "asana vs trello", "trello vs asana",
                "best project management tool", "best project management tool for nonprofits",
                "asana alternatives for nonprofits"} <= set(kws), kws))

# 3. Stage 1 selection
sel = comparisons.get_opportunities(c)
data = json.loads((TMP / "work" / "comparison_keywords.json").read_text())
why = {r["keyword"]: r["reason"] for r in data["rejected"]}
checks.append(("strong-domain SERP is rejected despite low KD (authority gap)", why.get("asana alternatives") == "serp_authority_gap", why.get("asana alternatives")))
checks.append(("KD above max is rejected", why.get("trello alternatives") == "kd_above_max", ""))
checks.append(("'a vs b' and 'b vs a' collapse to the higher-volume order", why.get("asana vs trello") == "same_page_as_higher_scored_keyword"
               and any(x["keyword"] == "trello vs asana" for x in data["ranked"]), ""))
checks.append(("brand-vs-competitor waits until the alternatives page exists", why.get("acme vs asana") == "own_versus_waits_for_alternatives_page", ""))
checks.append(("competitor-branded keywords are selected by score (not filtered as branded)", [x["keyword"] for x in sel] == ["asana review", "trello vs asana"],
               [(x["keyword"], x["score"]) for x in sel]))
checks.append(("selected items carry the SERP authority evidence", sel and sel[0]["authority"]["checked"] and sel[0]["authority"]["top3_median_rank"] == 42, sel[0].get("authority") if sel else None))

# 4. cluster support: once "asana alternatives" is written, "acme vs asana" is eligible without volume
inventory.add_entry(c, "asana alternatives", "asana-alternatives", status="written", score=90.9, title="Asana Alternatives",
                    page_type="alternatives", competitors=["Asana"], comparison_cluster=["asana"])
sel2 = comparisons.get_opportunities(c)
ranked2 = json.loads((TMP / "work" / "comparison_keywords.json").read_text())["ranked"]
checks.append(("brand-vs-competitor becomes cluster support after the alternatives page", any(x["keyword"] == "acme vs asana" and x.get("cluster_support") for x in ranked2),
               [x["keyword"] for x in ranked2]))

# 5. brief augmentation
item = next(x for x in sel2 if x["keyword"] == "acme vs asana") if any(x["keyword"] == "acme vs asana" for x in sel2) else \
    {"keyword": "acme vs asana", "page_type": "own_versus", "competitors": ["Asana"], "use_case": None}
brief = {"primary_keyword": "acme vs asana", "slug": "acme-vs-asana", "secondary_keywords": [], "intent": "commercial",
         "target_word_count": 300, "outline": [{"h2": "Frequently asked questions about acme vs asana", "h3": ["Is Acme cheaper?"], "faq": True}],
         "gaps_to_address": [], "internal_link_targets": [], "external_sources": [], "placement_rules": {}, "schema_type": "Article",
         "serp_summary": {"people_also_ask": [{"question": "Is Asana worth it?"}]}}
comparisons.augment_brief(brief, item, c)
cmp = brief["comparison"]
checks.append(("required comparison sections inserted before the FAQ", brief["outline"][-1].get("faq") and len(brief["outline"]) == 5
               and all(g["type"] == "comparison_section" for g in brief["gaps_to_address"]), [o["h2"] for o in brief["outline"]]))
checks.append(("Reddit pain points, pricing facts and videos are in the brief", cmp["voice_of_customer"] and cmp["competitor_facts"]
               and "$10.99" in cmp["competitor_facts"][0]["excerpt"] and cmp["videos"], ""))
checks.append(("complaint gate keeps the price theme (3 threads) and drops the one-off crash report",
               [t["theme"] for t in cmp["complaint_themes"]] == ["price"] and len(cmp["voice_of_customer"]) == 3
               and all("crashing" not in v["title"] for v in cmp["voice_of_customer"]), cmp["complaint_themes"]))
adv = []
comparisons.critique({"body": "# Asana alternatives\n\nIntro with no answer.\n\n## Options\n\nText.\n"}, brief,
                     lambda code, msg, **k: adv.append((code, k.get("advisory"))))
checks.append(("missing verdict-first and 1/5/25-seat math are flagged as advisory",
               ("comparison_verdict_first", True) in adv and ("comparison_seat_math", True) in adv, adv))
checks.append(("cluster page (the alternatives page) becomes a required internal link",
               brief["internal_link_targets"][0]["url"] == "/blog/asana-alternatives/", brief["internal_link_targets"]))
prompt = writer._brief_for_prompt(brief)
checks.append(("writer prompt carries override, bias, freshness, CTA and facts",
               all(x in prompt for x in ("override any general rule", "we are biased", "Pricing and features checked", "/demo/", "$10.99", "too expensive")), ""))

# 6. critique rules
label = cmp["checked_label"]
good = ("Pick Asana if you need large-team portfolios; choose Acme for agencies.\n\nPricing and features checked " + label + ".\n\n"
        "| | Acme | Asana |\n|---|---|---|\n| Price | $12 per user | $10.99 per user |\n| 25 seats | quote | 25 x $10.99 |\n"
        "| Best for | agencies | large teams |\n\n"
        "We make Acme, so we are biased; weigh that accordingly. [Book a demo](/demo/) or [book a demo](/demo/).\n")
fixes = []
comparisons.critique({"body": good}, brief, lambda code, msg, **k: fixes.append(code))
checks.append(("a compliant comparison passes the comparison rules", fixes == [], fixes))
fixes = []
comparisons.critique({"body": "Asana costs $7 a month. Acme is great.\n"}, brief, lambda code, msg, **k: fixes.append(code))
checks.append(("missing table, freshness, bias, CTA and an invented price are all flagged",
               {"comparison_table", "comparison_freshness", "comparison_bias_disclosure", "comparison_cta", "comparison_unsupported_price"} <= set(fixes), fixes))
c["forbid_best_lists"] = True
art = {"title": "Acme vs Asana", "description": "x" * 60, "body": "## Best tool for agencies\n\ntext\n"}
checks.append(("forbid_best_lists does not fire on comparison pages", "best_list" not in {f["code"] for f in writer.critique(art, brief, c)}, ""))

# 7. frontmatter + inventory fields
md = writer.assemble({"title": "Acme vs Asana", "description": "d" * 60, "body": good}, {**brief, "pubDate": "2026-12-01"}, c, "openrouter")
fm, _ = split_frontmatter(md)
checks.append(("frontmatter has pageType, competitors, cluster, lastVerified, author, cta, videos",
               fm.get("pageType") == "own_versus" and fm.get("comparisonCluster") == ["asana"] and fm.get("lastVerified") == date.today()
               and fm["author"]["name"] == "Pat Lee" and fm["cta"]["url"] == "/demo/" and fm.get("videos"), {k: fm.get(k) for k in ("pageType", "lastVerified")}))
checks.append(("inventory gets page type and cluster", brief["inventory_extra"]["comparison_cluster"] == ["asana"], ""))

# 8. companions: LinkedIn rewrite must not duplicate the article
class FakeBackend:
    def __init__(self):
        self.n = 0
    def _call(self, config, system, user):
        self.n += 1
        body = user.split("ARTICLE:\n", 1)[1]
        return body if self.n == 1 else "# Acme vs Asana for agencies\n\nA different take entirely, written fresh for LinkedIn readers who manage client work."
article_body = ("We compared Acme and Asana across pricing plans setup time and support quality for small agency teams. " * 6)
brief["comparison"]["page_type"] = "alternatives"
fb = FakeBackend()
files = comparisons.write_companions("---\ntitle: x\n---\n" + article_body, brief, c, fb)
names = sorted(Path(f).name for f in files)
checks.append(("LinkedIn copy is rejected when it duplicates the article, accepted after a rewrite", fb.n == 2 and "linkedin.md" in names, names))
checks.append(("interview questions and media checklist are written", {"video_interview.md", "media_checklist.md"} <= set(names), names))
vi = (TMP / "work" / "companions" / "acme-vs-asana" / "video_interview.md").read_text()
checks.append(("interview asks the founder to repeat the question", "repeat the question" in vi and "Is Asana worth it?" in vi, ""))

# 9. topical map groups comparison pages by competitor
inventory.add_entry(c, "asana review", "asana-review", status="written", volume=500, page_type="review",
                    competitors=["Asana"], comparison_cluster=["asana"])
inventory.add_entry(c, "water heater repair", "water-heater-repair", status="written", volume=900)
tm = json.loads(output.write_topical_map(c).read_text())
asana = [cl for cl in tm["clusters"] if any(p["slug"] == "asana-review" for p in cl["pieces"])]
checks.append(("topical map puts one competitor's pages in one cluster", asana and len(asana[0]["pieces"]) == 2, [len(cl["pieces"]) for cl in tm["clusters"]]))

# 10. draft hold: comparison pages can be written as drafts, and drafts are never link targets
brief["comparison"]["draft"] = True
checks.append(("draft option marks the page draft", comparisons.frontmatter(brief).get("draft") is True, ""))
c["_paths"]["content"] = TMP / "content"
c["_paths"]["content"].mkdir(exist_ok=True)
draft_file = c["_paths"]["content"] / "trello-alternatives.md"
draft_file.write_text("---\ntitle: Trello alternatives\ndraft: true\n---\nbody\n")
e = inventory.add_entry(c, "trello alternatives", "trello-alternatives", status="written", page_type="alternatives",
                        competitors=["Trello"], comparison_cluster=["trello"], draft=True)
links = comparisons._cluster_links(c, {"competitors": ["Trello"]}, "acme-vs-trello")
checks.append(("a draft page is held out of cluster links", inventory.held(c, e) and not links, links))
draft_file.write_text("---\ntitle: Trello alternatives\n---\nbody\n")
links = comparisons._cluster_links(c, {"competitors": ["Trello"]}, "acme-vs-trello")
checks.append(("once the draft flag is removed the page is linked", not inventory.held(c, e) and len(links) == 1, links))

# 11. Spanish site: keywords, sections, freshness and bias checks work in Spanish
import lang
lang.set_language("es")
ES = {**BLOCK, "brand": "KYB", "category": "marcas de amortiguadores", "use_cases": ["camioneta"],
      "page_types": ["own_versus", "versus", "best"], "brand_relation": "la marca que distribuimos",
      "bias_note": "Somos distribuidor autorizado de KYB, así que tenemos un interés; tómalo en cuenta.",
      "competitors": [{"name": "Monroe"}, {"name": "Gabriel"}]}
s_es = comparisons.settings({"comparisons": ES, "volume_floor": 30, "kd_max": 40})
kws = {x["keyword"] for x in comparisons.candidates(s_es)}
checks.append(("Spanish keywords", {"kyb vs monroe", "monroe vs gabriel", "mejores marcas de amortiguadores",
                                    "mejores marcas de amortiguadores para camioneta"} <= kws, sorted(kws)))
item = {"page_type": "own_versus", "competitors": ["Monroe"], "use_case": None}
heads = [h for h, _ in comparisons._section_specs(item, s_es)]
checks.append(("Spanish section headings", heads[0].startswith("KYB vs Monroe"), heads))
fixes = []
fake_brief = {"comparison": {"competitors": ["Monroe"], "brand": "KYB", "brand_on_page": True, "es": True,
                             "checked_label": "noviembre de 2026", "checked_line": "Datos verificados en noviembre de 2026.",
                             "brand_relation": "la marca que distribuimos", "bias_note": "x", "cta": None,
                             "competitor_facts": [], "brand_facts": []}}
good = "Datos verificados en noviembre de 2026.\n\nSomos distribuidor de KYB, así que tenemos un interés.\n\n| | KYB | Monroe |\n|---|---|---|\n| a | b | c |\n"
comparisons.critique({"body": good}, fake_brief, lambda code, msg, **k: fixes.append(code))
bad = "KYB y Monroe.\n\n| | KYB | Monroe |\n|---|---|---|\n| a | b | c |\n"
fixes2 = []
comparisons.critique({"body": bad}, fake_brief, lambda code, msg, **k: fixes2.append(code))
checks.append(("Spanish critique passes a good page", fixes == [], fixes))
checks.append(("Spanish critique flags missing freshness and disclosure", {"comparison_freshness", "comparison_bias_disclosure"} <= set(fixes2), fixes2))
lang.set_language("en")

# 11. favor_brand: the brand is the recommended pick and must lead tables and lists
fb_brief = json.loads(json.dumps(brief, default=str))
fb_brief["comparison"].update(favor_brand=True, page_type="alternatives", draft=False)
pb = comparisons.prompt_block(fb_brief)
checks.append(("favor_brand tells the writer to recommend the brand, from facts only",
               "Favor Acme" in pb and "Never invent" in pb and "genuinely does well" in pb, ""))
def fixes_for(body):
    out = []
    comparisons.critique({"body": body}, fb_brief, lambda code, msg, **kw: out.append(code))
    return out
table_asana_first = "| Tool | Price |\n|---|---|\n| Asana | x |\n| Acme | y |\n"
table_acme_first = "| Tool | Price |\n|---|---|\n| Acme | y |\n| Asana | x |\n"
checks.append(("brand after a competitor in the table is flagged",
               "comparison_brand_not_first" in fixes_for("### Acme\n\n" + table_asana_first), fixes_for("### Acme\n\n" + table_asana_first)))
checks.append(("competitor as the first H3 on a list page is flagged",
               "comparison_brand_not_first" in fixes_for("### Asana\n\n### Acme\n\n" + table_acme_first), ""))
checks.append(("brand first in table and list passes the favor check",
               "comparison_brand_not_first" not in fixes_for("### Acme\n\n### Asana\n\n" + table_acme_first), ""))

sys.exit(0 if report("COMPARISONS (mocked APIs)", checks) else 1)
