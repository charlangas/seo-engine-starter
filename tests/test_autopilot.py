"""Autopilot pieces: slop rules, retitle and striking-distance refresh, health crawl, competitor watch, money-page
links, fact routing, SERP guard, tool detection, 2-URL tie-break, VideoObject, self-merge and the orchestrator.
Fake HTTP, fake SERPs, template writer. No spend."""
import json, subprocess, sys, tempfile
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, inventory, lang
from textutil import build_frontmatter, split_frontmatter
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 18)
checks = []


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
    (tmp / "cache" / "serp").mkdir(parents=True)
    return c, tmp, p


def page(c, slug, kw, body, pub=TODAY - timedelta(days=60), extra=None, inv=None):
    fm = {"title": kw.title(), "description": f"All about {kw}, with prices and steps that help you decide today.",
          "pubDate": pub, "slug": slug, "primaryKeyword": kw, **(extra or {})}
    (c["_paths"]["content"] / f"{slug}.md").write_text(build_frontmatter(fm) + "\n" + body)
    inventory.add_entry(c, kw, slug, status="written", date=pub.isoformat(), intent="commercial", **(inv or {}))


# ---------------------------------------------------------------- 1. slop
import slop, writer
raw = ("It's not just a pump — it's a system. We utilize sensors in order to delve into leaks; costs run 3–5 days. "
       "Let's dive in. Experts say it’s crucial.")
out = slop.scrub(raw)
checks.append(("dashes, curly quotes, pompous verbs and chatbot lines are fixed mechanically",
               "—" not in out and "–" not in out and "3-5" in out and "use sensors to" in out and "Let's dive in" not in out
               and "’" not in out, out))
checks.append(("scrub leaves code, links and list markers alone",
               slop.scrub("- item one\n\n```\na — b\n```\n\n[x](https://a.com/path-with—dash)") ==
               "- item one\n\n```\na — b\n```\n\n[x](https://a.com/path-with—dash)", ""))
codes = []
slop.critique({"title": "t", "description": "d", "body": out}, {"slop": None},
              lambda code, msg, **k: codes.append((code, k.get("advisory"))))
checks.append(("contrast frames, AI vocabulary and unnamed experts are advisory fixes",
               {("slop_contrast_frame", True), ("slop_ai_vocabulary", True), ("slop_vague_authority", True)} <= set(codes), codes))
heavy = []
slop.critique({"title": "", "description": "", "body": "crucial pivotal vibrant tapestry moreover furthermore additionally "
               "showcase foster garner intricate"}, {"slop": {"max_hits": 5}}, lambda code, msg, **k: heavy.append((code, k.get("advisory"))))
checks.append(("past slop.max_hits a blocking slop_heavy fix is added", ("slop_heavy", None) in heavy, heavy))
checks.append(("advisory fixes never fail the gate", writer.blocking([{"code": "a", "advisory": True}, {"code": "b"}]) == [{"code": "b"}], ""))
checks.append(("a site can allow a word it needs", not slop.hits("Our landscape crews mow lawns.", ["landscape"]), ""))
lang.set_language("es")
es = slop.scrub("No solo es barato — también dura. Con el fin de ahorrar, utiliza aceite.")
es_codes = [h["code"] for h in slop.hits("No solo es barato, sino también dura. Es crucial y de vanguardia.")]
lang.set_language("en")
checks.append(("Spanish rules: dashes and 'con el fin de' fixed; 'no solo... sino' and puffery flagged",
               "—" not in es and "para ahorrar" in es.lower() and "usa aceite" in es
               and {"slop_contrast_frame", "slop_ai_vocabulary", "slop_puffery"} <= set(es_codes), (es, es_codes)))

# ---------------------------------------------------------------- 2. striking distance + retitle
import refresh, serp
c, tmp, cpath = make_config()
BODY = "# Water heater repair cost\n\nIntro.\n\n## What it costs\n\nA thermostat swap costs $150.\n"
page(c, "wh-cost", "water heater repair cost", BODY)
page(c, "drain", "drain cleaning cost", BODY, inv={"url_prefix": "/services", "output_dir": str(c["_paths"]["content"]), "page_path": "service_page"})
page(c, "dup", "sump pump repair", BODY)
(c["_paths"]["data"] / "gsc").mkdir()
(c["_paths"]["data"] / "gsc" / "latest.json").write_text(json.dumps({
    "has_page_query_pairs": True, "declines": [],
    "striking_distance": [{"query": "water heater repair price", "page": "/blog/wh-cost/", "impressions": 400, "position": 11.0},
                          {"query": "water heater thermostat", "page": "/blog/wh-cost/", "impressions": 90, "position": 9.0},
                          {"query": "near page one", "page": "/blog/wh-cost/", "impressions": 90, "position": 5.0}],
    "low_ctr": [{"query": "drain cleaning cost", "page": "/services/drain/", "impressions": 900, "ctr": 0.004, "position": 4.2}]}))
refresh.enqueue(c, "dup", "duplicate_title", duplicates=["Sump Pump Repair"])
picked, passed = refresh.due(c, TODAY)
by = {p["entry"]["slug"]: p for p in picked}
checks.append(("striking-distance pages (8-20) are revised with their queries as the focus",
               by["wh-cost"]["reason"] == "striking_distance" and [r["query"] for r in by["wh-cost"]["focus"]]
               == ["water heater repair price", "water heater thermostat"], by.get("wh-cost", {}).get("focus")))
checks.append(("low-CTR pages under a page_paths prefix get a title rewrite, duplicate titles too",
               by["drain"]["mode"] == "retitle" and by["drain"]["reason"] == "low_ctr" and by["dup"]["reason"] == "duplicate_title", ""))
checks.append(("revisions come before title rewrites", [p["entry"]["slug"] for p in picked][0] == "wh-cost", [p["entry"]["slug"] for p in picked]))
pb = refresh.prompt_block({"refresh": {"keep": [], "focus": ["water heater repair price"]}})
checks.append(("the writer is told to answer each focus query under its own heading", "water heater repair price" in pb and "heading" in pb, ""))
serp.fetch_serp = lambda kw, config, expect_terms=(): {"organic": [{"title": "Drain Cleaning Cost (2026): $150-$500", "url": "https://a.com/x"}]}
rec = refresh.retitle_page(c, by["drain"], TODAY)
fm, body = split_frontmatter((c["_paths"]["content"] / "drain.md").read_text())
e = inventory.find("drain cleaning cost", c)
checks.append(("retitle: 40-60 chars, query first, body and dates untouched",
               40 <= len(fm["title"]) <= 60 and fm["title"].lower().startswith("drain cleaning cost") and body.strip() == BODY.strip()
               and "updatedDate" not in fm and rec["changed"], fm["title"]))
checks.append(("the title test is recorded with the CTR before", e["title_tests"][0]["ctr_before"] == 0.004
               and e["title_tests"][0]["old_title"] == "Drain Cleaning Cost", e.get("title_tests")))
picked2, passed2 = refresh.due(c, TODAY)
checks.append(("one title test at a time: the page is not retitled again for 28 days",
               "drain" not in [p["entry"]["slug"] for p in picked2], [p["entry"]["slug"] for p in picked2]))
(c["_paths"]["data"] / "gsc" / "latest.json").write_text(json.dumps({"has_page_query_pairs": True, "low_ctr": [],
    "query_owners": [{"query": "drain cleaning cost", "page": "/services/drain/", "impressions": 800, "ctr": 0.02, "position": 4.0}]}))
tests = refresh.title_test_results(c, TODAY + timedelta(days=30))
checks.append(("after 28 days the test reports whether click-through improved", tests and tests[0]["verdict"] == "better", tests))
checks.append(("title rules reject long titles and a buried query",
               refresh.title_ok("A very long title that keeps going and going past the limit, drain cleaning cost", "drain cleaning cost")
               and not refresh.title_ok("Drain Cleaning Cost in Austin: Real 2026 Prices", "drain cleaning cost"), ""))

# ---------------------------------------------------------------- 3. health crawl
import health
c, tmp, cpath = make_config()
page(c, "good", "water heater repair", "# W\n\nIntro.\n\nSee [drain](/blog/dupe/).\n")
page(c, "dupe", "drain cleaning", "# D\n\nIntro.\n\nSee [water](/blog/good/).\n")
page(c, "lonely", "sump pump", "# S\n\nIntro.\n")
page(c, "unlisted", "pipe repair", "# P\n\nIntro.\n\n[water](/blog/good/) [sump](/blog/lonely/)\n")
B = "https://example-plumbing.com"
HTML = lambda title, desc="A description long enough.", canon=None, robots="": (
    f"<html><head><title>{title}</title><meta name='description' content='{desc}'>"
    + (f"<link rel='canonical' href='{canon}'>" if canon else "") + (f"<meta name='robots' content='{robots}'>" if robots else "")
    + "</head><body>x</body></html>")
SITE = {
    f"{B}/robots.txt": (200, {}, "User-agent: *\nDisallow: /private/\nSitemap: " + B + "/sitemap-index.xml\n"),
    f"{B}/sitemap-index.xml": (200, {}, f"<sitemapindex><sitemap><loc>{B}/sitemap-0.xml</loc></sitemap></sitemapindex>"),
    f"{B}/sitemap-0.xml": (200, {}, "<urlset>" + "".join(f"<url><loc>{B}{p}</loc></url>" for p in
                           ["/blog/good/", "/blog/dupe/", "/blog/lonely/", "/old/", "/gone/", "/private/x/", "/canon/", "/hidden/"]) + "</urlset>"),
    f"{B}/blog/good/": (200, {"content-type": "text/html"}, HTML("Water heater repair")),
    f"{B}/blog/dupe/": (200, {"content-type": "text/html"}, HTML("Water heater repair")),
    f"{B}/blog/lonely/": (200, {"content-type": "text/html"}, HTML("Sump pump", desc="")),
    f"{B}/old/": (301, {"location": "/older/"}, ""), f"{B}/older/": (301, {"location": "/blog/good/"}, ""),
    f"{B}/gone/": (404, {"content-type": "text/html"}, "nope"),
    f"{B}/private/x/": (200, {"content-type": "text/html"}, HTML("Private")),
    f"{B}/canon/": (200, {"content-type": "text/html"}, HTML("Canon", canon=f"{B}/blog/good/")),
    f"{B}/hidden/": (200, {"content-type": "text/html"}, HTML("Hidden", robots="noindex, follow")),
}


def fake_get(url, timeout=15):
    if url not in SITE:
        return 404, {}, ""
    st, h, t = SITE[url]
    return st, {"content-type": "text/xml", **h}, t


r = health.run(c, get=fake_get, today=TODAY, inspect_index=False)
kinds = {}
for i in r["issues"]:
    kinds.setdefault(i["issue"], set()).add(i["url"].replace(B, ""))
checks.append(("sitemap index followed; every URL checked", r["checked"] == 8 and r["sitemaps"][-1].endswith("sitemap-0.xml"), r["checked"]))
checks.append(("404, redirect chain, robots block, canonical elsewhere and noindex found",
               kinds.get("not_200") == {"/gone/"} and kinds.get("redirect_chain") == {"/old/"} and kinds.get("robots_blocked") == {"/private/x/"}
               and kinds.get("canonical_elsewhere") == {"/canon/"} and kinds.get("noindex") == {"/hidden/"}, kinds))
checks.append(("duplicate titles found, missing description found",
               kinds.get("duplicate_title") == {"/blog/good/", "/blog/dupe/"} and "/blog/lonely/" in kinds.get("missing_description", set()), ""))
checks.append(("engine page missing from the sitemap and orphan pages found",
               kinds.get("not_in_sitemap") == {"/blog/unlisted/"} and "/blog/unlisted/" in kinds.get("orphan", set())
               and "/blog/good/" not in kinds.get("orphan", set()), (kinds.get("not_in_sitemap"), kinds.get("orphan"))))
checks.append(("engine pages with a duplicate title are queued for a title rewrite",
               sorted(r["queued_retitles"]) == ["dupe", "good"] and {x["slug"] for x in refresh.load_queue(c)} == {"dupe", "good"}, r["queued_retitles"]))
asks = health.asks(r)
checks.append(("asks put broken pages and noindex first, at most 3", len(asks) == 3 and asks[0].startswith("Does not return 200")
               and "noindex" in asks[1].lower(), asks))
states = iter([{"inspectionResult": {"indexStatusResult": {"verdict": "PASS", "coverageState": "Submitted and indexed"}}},
               {"inspectionResult": {"indexStatusResult": {"verdict": "NEUTRAL", "coverageState": "Discovered - currently not indexed"}}}])
import gsc, apiclient
gsc._token = lambda: "t"


class Resp:
    def __init__(self, d): self.d = d
    def json(self): return self.d


apiclient._request = lambda *a, **k: Resp(next(states))
r2 = health.run(c, get=fake_get, today=TODAY, inspect_index=True)
ni = [i for i in r2["issues"] if i["issue"] == "not_indexed"]
checks.append(("URL Inspection: indexed pages counted; an old page that is not indexed is reported",
               r2["inspected"] == 2 and r2["indexed"] == 1 and len(ni) == 1 and "not indexed" in ni[0]["detail"], (r2["inspected"], ni)))
st = json.loads((c["_paths"]["data"] / "health" / "indexing.json").read_text())
checks.append(("indexing results are kept so indexed pages are not inspected again", sum(v["verdict"] == "PASS" for v in st.values()) == 1, ""))

# ---------------------------------------------------------------- 4. competitor watch
import watch
c, tmp, cpath = make_config("\ncompetitor_domains: [rival.com]\n")
R = "https://rival.com"
pages_v1 = ["/blog/water-heater-flush/", "/tag/x/"]
pages_v2 = pages_v1 + ["/blog/tankless-water-heater-repair-cost/", "/careers/"]
cur = {"v": pages_v1}


def rival_get(url, timeout=15):
    if url == f"{R}/robots.txt":
        return 200, {}, f"Sitemap: {R}/sitemap.xml"
    if url == f"{R}/sitemap.xml":
        return 200, {}, "<urlset>" + "".join(f"<url><loc>{R}{p}</loc></url>" for p in cur["v"]) + "</urlset>"
    return 404, {}, ""


w1 = watch.run(c, get=rival_get, today=TODAY)
cur["v"] = pages_v2
w2 = watch.run(c, get=rival_get, today=TODAY + timedelta(days=7))
checks.append(("first run records a baseline; the next lists only new content pages",
               w1["domains"][0]["baseline"] and [n["url"] for n in w2["domains"][0]["new"]] == [f"{R}/blog/tankless-water-heater-repair-cost/"], w2["domains"]))
checks.append(("on-topic new pages become keyword ideas for research",
               watch.research_ideas(c) == ["tankless water heater repair cost"], watch.research_ideas(c)))

# ---------------------------------------------------------------- 5. money-page links
import interlink
c, tmp, cpath = make_config()
long_para = "Plain text about pipes and fittings that goes on for a while. " * 6
page(c, "guide", "how water heaters work",
     "# How water heaters work\n\nIntro paragraph.\n\nIf the tank leaks, water heater repair cost depends on the part. "
     + long_para + "\n\n" + long_para + "\n\n" + long_para + "\n\nLater we compare emergency plumber pricing in Austin today.\n")
page(c, "wh-repair-cost", "water heater repair cost", "# Cost\n\nIntro.\n")
page(c, "emergency", "emergency plumber pricing", "# Emergency\n\nIntro.\n", inv={"page_type": "use_case"})
posts = interlink.load_posts(c)
rows = [{"page": "/blog/guide/", "query": "how water heaters work", "impressions": 900, "clicks": 40, "position": 3.0}]
plan, gaps = interlink.build_money_plan(c, posts, rows)
anchors = {p["anchor"]: p for p in plan}
guide_text = (c["_paths"]["content"] / "guide.md").read_text()
first = min(plan, key=lambda p: p["start"]) if plan else None
checks.append(("the most-visited article gets money links: one in the first 40%, 2 in all",
               len(plan) == 2 and first and first["start"] < len(guide_text) * 0.45 and all(p["kind"] == "money" for p in plan),
               [(p["anchor"], p["start"], len(guide_text)) for p in plan]))
checks.append(("anchors are the money pages' own keywords found in the text (3-7 words or the exact keyword)",
               set(anchors) == {"water heater repair cost", "emergency plumber pricing"}, list(anchors)))
for i in range(3):
    page(c, f"src{i}", f"source {i}", f"# S\n\nIntro.\n\nSee [water heater repair cost](/blog/wh-repair-cost/).\n")
plan2, _ = interlink.build_money_plan(c, interlink.load_posts(c), rows)
checks.append(("an anchor already used 3 times for the same page is not used again",
               "water heater repair cost" not in {p["anchor"] for p in plan2}, [p["anchor"] for p in plan2]))

# ---------------------------------------------------------------- 6. fact routing + founder questions
import evidence
c, tmp, cpath = make_config()
evd = c["_paths"]["data"] / "evidence"
evd.mkdir()
item = lambda nums: ("id: wh-job\ntype: case_study\ntitle: Water heater job\nsummary: s\nnumbers: " + json.dumps(nums)
                     + "\ntopics: [water heater repair]\n")
(evd / "wh-job.yaml").write_text(item(["$1,200 total", "3 hours"]))
page(c, "wh", "water heater repair", "# W\n\nOur last job cost $1,200 total and took 3 hours.\n", inv={"evidence": ["wh-job"]})
page(c, "other", "drain cleaning", "# D\n\nA drain job cost $1,200 too.\n")
checks.append(("first fact routing only takes a snapshot", evidence.route_facts(c) == [], ""))
(evd / "wh-job.yaml").write_text(item(["$1,350 total", "3 hours"]))
routed = evidence.route_facts(c)
q = refresh.load_queue(c)
checks.append(("a changed figure queues only pages that quote it and use the item",
               [x["slug"] for x in routed] == ["wh"] and q[0]["reason"] == "fact_changed" and q[0]["old"] == ["$1,200 total"]
               and q[0]["new"] == ["$1,350 total"], routed))
picked, _ = refresh.due(c, TODAY)
checks.append(("the refresh run picks it up and tells the writer the old and new figure",
               picked and picked[0]["reason"] == "fact_changed" and "$1,350 total" in refresh.prompt_block(
                   {"refresh": {"keep": [], "fact": picked[0]["queued"]}}), [p["reason"] for p in picked]))
checks.append(("no change, no queue", evidence.route_facts(c) == [], ""))
(c["_paths"]["data"] / "ranked_keywords.json").write_text(json.dumps([{"keyword": "tankless water heater install", "volume": 500,
                                                                      "kd": 20, "intent": "transactional", "score": 9}]))
fq = evidence.founder_questions(c)
checks.append(("money keywords with no evidence get 3 specific questions for the owner",
               any(x["keyword"] == "tankless water heater install" and len(x["questions"]) == 3
                   and "tankless water heater install" in x["questions"][0] for x in fq), fq))

# ---------------------------------------------------------------- 7. SERP guard, tool detection, tie-break
import importlib
importlib.reload(serp)
good = {"organic": [{"domain": f"d{i}.com", "resolved": True, "title": f"Asana option {i}", "url": f"https://d{i}.com/"} for i in range(10)]}
bad = {"organic": [{"domain": "google.com", "resolved": False, "title": "x", "url": "https://google.com/goto?x"} for _ in range(6)]}
checks.append(("a normal SERP passes; unresolved Google redirects fail the guard",
               serp.serp_quality(good) == [] and len(serp.serp_quality(bad)) >= 2, serp.serp_quality(bad)))
checks.append(("a comparison SERP that never mentions the competitor is suspect",
               serp.serp_quality(good, ["Monday"]) and not serp.serp_quality(good, ["Asana"]), ""))
c, tmp, cpath = make_config()
calls = []
serp._fetch_live = lambda kw, config, loc, no_cache=False: (calls.append(no_cache), json.loads(json.dumps(bad)))[1]
s1 = serp.fetch_serp("x", c)
checks.append(("a bad SERP is fetched once more without cache, then marked suspect and never cached",
               calls == [False, True] and s1.get("suspect") and not list((c["_paths"]["cache"] / "serp").glob("*.json")), calls))
import serpclass
tools = [{"url": f"https://t{i}.com/mortgage-calculator/", "title": "Mortgage Calculator"} for i in range(3)] + \
        [{"url": "https://b.com/blog/how-mortgages-work/", "title": "How mortgages work"}] * 2
sm = serpclass.summarize(tools)
checks.append(("3 of the top 5 are calculators -> the keyword needs a tool, not an article",
               sm["needs_tool"] and sm["format"] == "tool" and sm["types"][:3] == ["tool"] * 3, sm))
checks.append(("a guide that mentions a tool in its title stays a blog post",
               serpclass.classify({"url": "https://x.com/blog/how-to-use-a-mortgage-calculator/", "title": "How to use a mortgage calculator"}) == "blog_post", ""))
import cluster
c, tmp, cpath = make_config()
svc = lambda host, n: [{"url": f"https://{host}{i}.com/services/repair/", "title": "Repair"} for i in range(n)]
blog = lambda host, n: [{"url": f"https://{host}{i}.com/blog/a-long-guide-about-repairs/", "title": "Guide"} for i in range(n)]
common = [{"url": "https://shared0.com/services/x/", "title": "x"}, {"url": "https://shared1.com/services/y/", "title": "y"}]
for kw, org in (("a", common + svc("a", 8)), ("b", common + svc("b", 8)), ("c", common + blog("c", 8))):
    cluster.cache_file(kw, c).write_text(json.dumps({"organic": org}))
memo = {}
checks.append(("2 shared URLs: same page when both SERPs rank the same page type, separate when they differ",
               cluster.match("a", "b", c, memo, 3) == 2 and cluster.match("a", "c", c, memo, 3) is None, ""))

# ---------------------------------------------------------------- 8. VideoObject
import onpage
c, tmp, cpath = make_config()
fmx = onpage.frontmatter({"title": "Water heater repair cost in 2026", "description": "d", "pubDate": TODAY},
                         "# T\n\n<iframe src=\"https://www.youtube.com/embed/abcdefghijk\" title=\"Repair walkthrough\"></iframe>\n",
                         {"slug": "wh", "primary_keyword": "water heater repair cost", "schema_type": "Article"}, c)
vids = [g for g in fmx["jsonLd"]["@graph"] if g["@type"] == "VideoObject"]
checks.append(("an embedded YouTube video adds VideoObject JSON-LD (name, thumbnail, uploadDate, embedUrl)",
               len(vids) == 1 and vids[0]["name"] == "Repair walkthrough" and vids[0]["thumbnailUrl"].endswith("abcdefghijk/hqdefault.jpg")
               and vids[0]["uploadDate"] == str(TODAY), vids))

# ---------------------------------------------------------------- 9. self-merge
import publish
checks.append(("publish config is validated", publish.validate({"repo": "x", "ship_mode": "yolo"}) != []
               and publish.validate({"repo": "x", "ship_mode": "merge_when_checks_pass"}) == [], ""))
ran = []
real_run = subprocess.run


def fake_run(cmd, **kw):
    ran.append(cmd)
    return subprocess.CompletedProcess(cmd, 0, "", "")


publish.subprocess.run = fake_run
publish._git = lambda repo, *a, check=True: ""
m = publish.merge(Path("/tmp"), "https://github.com/o/r/pull/1", {"merge_method": "squash"})
m2 = publish.merge(Path("/tmp"), "https://github.com/o/r/pull/2", {"wait_for_remote_checks": True})
publish.subprocess.run = real_run
checks.append(("merge_when_checks_pass squash-merges the PR; with remote checks it uses gh --auto",
               m == {"merged": True} and ran[0][:4] == ["gh", "pr", "merge", "https://github.com/o/r/pull/1"] and "--squash" in ran[0]
               and m2 == {"merged": False, "auto_merge": True} and "--auto" in ran[1], ran))

# ---------------------------------------------------------------- 10. orchestrator
import autopilot
c, tmp, cpath = make_config("\nautopilot: {enabled: true, tasks: {content: 3, page_map: off}}\n")
for bad_cfg in ("\nautopilot: {tasks: {nope: 1}}\n", "\nautopilot: {tasks: {content: sometimes}}\n"):
    try:
        make_config(bad_cfg)
        checks.append(("bad autopilot config rejected", False, bad_cfg))
    except cfg.ConfigError:
        checks.append(("bad autopilot config rejected", True, ""))
st = {"tasks": {"gsc_check": {"last_run": TODAY.isoformat()}, "content": {"last_run": TODAY.isoformat()},
                "refresh": {"last_run": (TODAY - timedelta(days=8)).isoformat()}}, "events": []}
due = autopilot.due_tasks(c, st, TODAY)
checks.append(("only due jobs run: daily jobs ran today, page_map off, AI check needs prompts",
               "gsc_check" not in due and "content" not in due and "refresh" in due and "page_map" not in due
               and "ai_visibility" not in due and due[-1] == "report", due))
events = [{"task": "content", "date": TODAY.isoformat(), "result": {"summary": "2 written", "written": ["Page A", "Page B"],
                                                                    "merged": True, "asks": ["Build a tool for: mortgage calculator"]}},
          {"task": "health", "date": TODAY.isoformat(), "result": {"summary": "40 URLs, 3 issue(s)", "asks": [
              "Does not return 200: 1 page(s), e.g. /gone/ 404", "Marked noindex: 1 page(s)", "Redirect chain: 1 page(s)"]}},
          {"task": "facts", "date": TODAY.isoformat(), "result": {"summary": "0", "priority_asks": False,
                                                                  "asks": ["Evidence for x: " + "question " * 30]}},
          {"task": "refresh", "date": TODAY.isoformat(), "error": "RuntimeError: boom"}]
text, asks = autopilot.build_report(c, events, TODAY)
checks.append(("weekly report: under 300 words, at most 3 asks, crashes first, evidence questions last",
               len(text.split()) <= 300 and len(asks) == 3 and asks[0].startswith("The refresh job crashed")
               and not any(a.startswith("Evidence") for a in asks) and "2 new page(s)" in text and "merged automatically" in text, text))
called = []
autopilot.JOBS = {k: (lambda name: (lambda path, config: (called.append(name), {"summary": f"{name} ok"})[1]))(k) for k in autopilot.JOBS}
autopilot.JOBS["health"] = lambda path, config: (_ for _ in ()).throw(RuntimeError("site down"))
res = autopilot.run(cpath, today=TODAY, config=c)
checks.append(("a failing job does not stop the others; the report is written",
               "refresh" in called and "content" in called and res["errors"][0]["task"] == "health" and res.get("report", {}).get("file"),
               (called, res["errors"])))
again = autopilot.run(cpath, today=TODAY, dry_run=True, config=c)
checks.append(("state is kept: nothing runs twice on the same day", again["due"] == [], again["due"]))

sys.exit(0 if report("autopilot", checks) else 1)
