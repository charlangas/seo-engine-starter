"""Offline checks for the per-site quality rules (no API calls, no spend)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, research, serp, writer
from tests_common import report

import tempfile
BASE = (Path(__file__).resolve().parent.parent / "configs" / "test-minimal.yaml").read_text()
# A site with strict quality rules (healthcare marketing) and one on the defaults.
STRICT = """
kd_zero_policy: assume
kd_assumed: 30
cite_only_domains: [.gov, .edu, support.google.com, developers.google.com, blog.google]
primary_sources:
  - {title: "Healthcare and medicines (Google Ads policy)", url: "https://support.google.com/adspolicy/answer/176031", match: [google ads, ads, advertis, ppc, healthcare, medical]}
keyword_h2_max: 2
forbid_best_lists: true
word_count_max_factor: 1.3
"""


def load(extra):
    p = Path(tempfile.mkdtemp()) / "site.yaml"
    p.write_text(BASE + extra)
    return cfg.load_config(p, dry_run=True)


c, ind = load(STRICT), load("")
checks = []

# 1. difficulty 0 is unknown under "assume", trusted by default
rejected = []
def kd_phase(conf, kd_value):
    d, est = kd_value, False
    if d == 0 and conf.get("kd_zero_policy") == "assume":
        d, est = float(conf["kd_assumed"]), True
    return d, est
checks.append(("strict site: kd 0 becomes the assumed value", kd_phase(c, 0) == (30.0, True), kd_phase(c, 0)))
checks.append(("default site: kd 0 is still trusted", kd_phase(ind, 0) == (0, False), kd_phase(ind, 0)))
hi = research.score_keyword(165000, 30.0, "commercial"); lo = research.score_keyword(1600, 4.0, "commercial")
checks.append(("an unknown-difficulty head term no longer outscores a measured easy one", hi > lo and research.score_keyword(165000, 0, "commercial") > hi, f"{hi} vs {lo}"))

# 2. source selection: only primary sources, curated ones matched by keyword
serp_data = {"organic": [
    {"domain": "growth-hackers.net", "url": "https://growth-hackers.net/best", "title": "Best healthcare SEO", "position": 1, "resolved": True},
    {"domain": "designrush.com", "url": "https://designrush.com/x", "title": "Top agencies", "position": 2, "resolved": True},
    {"domain": "support.google.com", "url": "https://support.google.com/adspolicy/answer/176031", "title": "Healthcare", "position": 3, "resolved": True},
    {"domain": "www.cdc.gov", "url": "https://www.cdc.gov/x", "title": "CDC", "position": 4, "resolved": True},
]}
src = serp._external_sources(serp_data, c, ["google ads for doctors"])
urls = [s["url"] for s in src]
checks.append(("no competitor listicle is citable", not any("growth-hackers" in u or "designrush" in u for u in urls), urls))
checks.append(("a curated Google Ads policy source is offered for a Google Ads keyword", any("answer/176031" in u for u in urls), urls))
checks.append(("a .gov SERP page is allowed", any("cdc.gov" in u for u in urls), urls))
checks.append(("no duplicate URLs", len(urls) == len(set(urls)), urls))
ind_src = serp._external_sources(serp_data, ind, ["industrial seo"])
checks.append(("default site: unchanged behaviour (SERP pages still citable)", any("growth-hackers" in s["url"] for s in ind_src), [s["url"] for s in ind_src]))

# 3. critique rules on a minimal article
brief = {"primary_keyword": "healthcare seo agency", "target_word_count": 100, "gaps_to_address": [], "outline": [],
         "external_sources": [], "internal_link_targets": [], "placement_rules": {}}
body = ("# Healthcare SEO Agency Guide\n\nIntro text for you.\n\n"
        "## Healthcare SEO agency pricing\n\ntext\n\n## Healthcare SEO agency contracts\n\ntext\n\n"
        "## Healthcare SEO agency red flags\n\ntext\n\n## Best Healthcare SEO Agency\n\ntext\n\n"
        "### Top 5 agencies\n\ntext\n\n## Frequently asked questions about healthcare seo agency\n\n### Q?\n\nA.\n")
art = {"title": "Healthcare SEO Agency Guide", "description": "x" * 60, "body": body + "word " * 400}
codes = {f["code"] for f in writer.critique(art, brief, c)}
checks.append(("keyword in too many H2s is flagged", "keyword_h2_stuffing" in codes, sorted(codes)))
checks.append(("'Best' and 'Top N' headings are flagged", "best_list" in codes, ""))
checks.append(("an over-long draft is flagged", "word_count_max" in codes, ""))
codes_ind = {f["code"] for f in writer.critique(art, brief, ind)}
checks.append(("industrial: none of the new rules fire", not ({"keyword_h2_stuffing", "best_list", "word_count_max"} & codes_ind), sorted(codes_ind)))

sys.exit(0 if report("QUALITY RULES", checks) else 1)
