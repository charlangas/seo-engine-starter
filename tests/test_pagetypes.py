"""New page types (switch, export, use_case), per-type URL folders, SERP classification, off-site companions and
YouTube metadata. No spend."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import comparisons, config as cfg, inventory, lang, output, serpclass
from textutil import split_frontmatter
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
checks = []


def make_config(extra=""):
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=False)
    c["_paths"].update(data=tmp, work=tmp, inventory=tmp / "inventory.json", output=tmp / "blog", content=tmp / "blog",
                       briefs=tmp / "briefs")
    return c, tmp


# 1. candidates for the new page types
s = {**comparisons.DEFAULTS, "brand": "Acme", "category": "seo agency", "use_cases": ["dentists"],
     "competitors": [{"name": "Rival", "priority": 1}], "page_types": ["switch", "export", "use_case"]}
cands = {c["keyword"]: c["page_type"] for c in comparisons.candidates(s)}
checks.append(("switch, export and '<category> for <audience>' keywords are generated",
               cands == {"how to switch from rival": "switch", "migrate from rival to acme": "switch",
                         "export data from rival": "export", "how to export from rival": "export",
                         "seo agency for dentists": "use_case"}, cands))
checks.append(("default page types now include switch and use_case (export stays opt-in)",
               "switch" in comparisons.DEFAULTS["page_types"] and "use_case" in comparisons.DEFAULTS["page_types"]
               and "export" not in comparisons.DEFAULTS["page_types"], comparisons.DEFAULTS["page_types"]))
checks.append(("every page type has a writer guide, a favor rule and a weight",
               all(t in comparisons.TYPE_GUIDE and t in comparisons.FAVOR_GUIDE and t in comparisons.TYPE_WEIGHT
                   for t in comparisons.PAGE_TYPES), ""))
for t, comps in (("switch", ["Rival"]), ("export", ["Rival"]), ("use_case", [])):
    specs = comparisons._section_specs({"page_type": t, "competitors": comps, "use_case": "dentists"}, s)
    checks.append((f"{t} pages get their own required sections", len(specs) >= 3, [h for h, _ in specs]))
lang.set_language("es")
es = comparisons._section_specs({"page_type": "switch", "competitors": ["Rival"]}, s)
lang.set_language("en")
checks.append(("Spanish sections exist for the new types", es[0][0].startswith("Por qué"), es[0][0]))
fixes = []
comparisons.critique({"body": "# x\n\nAcme helps dentists. Pricing and features checked November 2026. We are biased."},
                     {"comparison": {"page_type": "use_case", "competitors": [], "brand": "Acme", "checked_label": "November 2026",
                                     "brand_on_page": True, "competitor_facts": [], "brand_facts": []}},
                     lambda code, msg, **k: fixes.append(code))
checks.append(("audience landing pages do not need a company comparison table", "comparison_table" not in fixes, fixes))

# 2. page_paths
for extra, label in (("\npage_paths: {compare: {url_prefix: /compare, output_directory: x}}\n", "unknown key"),
                     ("\npage_paths: {comparison: {url_prefix: compare, output_directory: x}}\n", "prefix without slash")):
    try:
        make_config(extra)
        checks.append((f"bad page_paths rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad page_paths rejected ({label})", "page_paths" in str(e), str(e)[-80:]))
c, tmp = make_config("\npage_paths:\n  comparison: {{url_prefix: /compare, output_directory: {tmp_dir}}}\n"
                     "  use_case: {{url_prefix: /industries, output_directory: {tmp_dir2}}}\n"
                     "  service_page: {{url_prefix: /services, output_directory: {tmp_dir3}}}\n".format(
                         tmp_dir=tempfile.mkdtemp(), tmp_dir2=tempfile.mkdtemp(), tmp_dir3=tempfile.mkdtemp()))
key = lambda b: output.destination_key(b, c)
checks.append(("comparison types go to /compare, audience pages to /industries, service-format pages to /services",
               key({"comparison": {"page_type": "alternatives"}}) == "comparison" and key({"comparison": {"page_type": "use_case"}}) == "use_case"
               and key({"format": "service_page"}) == "service_page" and key({"format": "blog_post"}) is None, ""))
MD = ("---\ntitle: Rival alternatives\ndescription: d\npubDate: 2026-11-18\nslug: rival-alternatives\ntags: [x]\n"
      "primaryKeyword: rival alternatives\n---\n# Rival alternatives\n\n" + "word " * 50 + "\n")
brief = {"primary_keyword": "rival alternatives", "slug": "rival-alternatives", "target_word_count": 10, "intent": "commercial",
         "comparison": {"page_type": "alternatives"}}
path = output.write_article(MD, brief, c)
e = inventory.find("rival alternatives", c)
checks.append(("the page is written to its folder and the inventory records its URL prefix and folder",
               path.parent == output.page_paths(c)["comparison"]["output_directory"] and e["url_prefix"] == "/compare"
               and e["page_path"] == "comparison" and brief["_url"].endswith("/compare/rival-alternatives/"), e))
checks.append(("engine pages in their own folder are not mistaken for imported pages",
               not output.is_imported(e) and output.is_imported({"url_prefix": "/guides"}) and output.page_file(c, e) == path, ""))
checks.append(("interlinking and the audit see every engine folder",
               len(output.engine_dirs(c)) == 4 and output.engine_dirs(c)[1][1] == "/compare", output.engine_dirs(c)))

# 3. SERP classification
R = [("https://www.linkedin.com/pulse/x", "x"), ("https://www.youtube.com/watch?v=1", "x"), ("https://www.reddit.com/r/x", "x"),
     ("https://www.yelp.com/biz/x", "x"), ("https://site.com/blog/how-to-fix-a-water-heater/", "How to fix"),
     ("https://plumbco.com/services/water-heater-repair/", "Water heater repair"), ("https://tool.com/pricing", "Pricing"),
     ("https://mag.com/x", "10 Best Water Heaters of 2026"), ("https://localplumber.com/", "Austin Plumber")]
got = [serpclass.classify({"url": u, "title": t}) for u, t in R]
checks.append(("results are classified by platform, path and title",
               got == ["linkedin", "youtube", "forum", "directory", "blog_post", "service_page", "product_page", "listicle", "service_page"], got))
sm = serpclass.summarize([{"url": u, "title": t} for u, t in [R[0], R[5], R[8], ("https://b.com/water-heater-repair/", "x"), R[4]]])
checks.append(("dominant on-site format decides the page format; LinkedIn is an off-site opportunity",
               sm["format"] == "service_page" and [o["type"] for o in sm["off_site"]] == ["linkedin"] and sm["off_site"][0]["position"] == 1, sm))
sm2 = serpclass.summarize([{"url": f"https://m{i}.com/x", "title": f"{i + 5} Best Plumbers in Austin"} for i in range(5)])
checks.append(("mostly listicles -> listicle format", sm2["format"] == "listicle", sm2["format"]))
import onpage
b = {"primary_keyword": "water heater repair", "format": "service_page", "outline": []}
onpage.attach(b, c)
checks.append(("the writer is told to match the ranking format", "service page" in onpage.prompt_block(b), ""))
checks.append(("listicle format turns on best-for labels", onpage.is_list_page({"primary_keyword": "plumbers", "format": "listicle"}), ""))

# 4. off-site companions for a normal (non-comparison) page + YouTube metadata
ob = {"primary_keyword": "water heater repair cost", "slug": "water-heater-repair-cost", "secondary_keywords": ["water heater fix price"],
      "serp_summary": {"people_also_ask": [{"question": "Is it worth repairing a water heater?"}]},
      "off_site": [{"type": "youtube", "position": 3, "url": "https://youtube.com/watch?v=1", "title": "Repair cost"}],
      "_url": "https://example-plumbing.com/blog/water-heater-repair-cost/"}
files = comparisons.write_companions("---\ntitle: x\n---\nbody", ob, c, backend=None)
names = sorted(Path(f).name for f in files)
checks.append(("a YouTube result in the top 10 gets the video brief, YouTube metadata and an off-site note, even off comparison pages",
               names == ["off_site.md", "video_interview.md", "youtube.md"], names))
checks.append(("normal pages with no off-site results get no companions",
               comparisons.write_companions("x", {"primary_keyword": "x", "slug": "x"}, c) == [], ""))
yt = comparisons.youtube_metadata(ob, c)
title = yt.split("**Title**")[1].split(": ", 1)[1].split("\n")[0]
tags = yt.split("**Tags**: ")[1].split("\n")[0]
checks.append(("YouTube title is at most 60 characters and starts with the keyword", len(title) <= 60 and title.lower().startswith("water heater repair cost"), title))
checks.append(("description has the article link, secondary keywords and chapters; file name and captions covered",
               "https://example-plumbing.com/blog/water-heater-repair-cost/" in yt and "water heater fix price" in yt
               and "00:00" in yt and "`water-heater-repair-cost.mp4`" in yt and ".srt" in yt, ""))
checks.append(("tags fit YouTube's 500-character limit and lead with the keyword", len(tags) <= 500 and tags.startswith("water heater repair cost"), tags))

sys.exit(0 if report("pagetypes", checks) else 1)
