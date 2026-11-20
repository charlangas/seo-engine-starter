"""The DataForSEO cache: a keyword's data is bought once per age limit, ideas lists are reused. No spend."""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests_common import report
import config as cfg, comparisons, dfscache, research

from tests_common import minimal_config
c = minimal_config("\nenable_directory: false\n")
checks = []
sent = []


def fake_post(config, endpoint, payload):
    sent.append((endpoint, list(payload.get("keywords") or [payload.get("keyword") or payload.get("targets")])))
    if endpoint.endswith("keyword_overview/live"):
        return [{"items": [{"keyword": k, "keyword_info": {"search_volume": 100 + len(k), "cpc": 1.0}}
                           for k in payload["keywords"] if k != "no data kw"]}]
    if endpoint.endswith("bulk_keyword_difficulty/live"):
        return [{"items": [{"keyword": k, "keyword_difficulty": 12} for k in payload["keywords"]]}]
    if endpoint.endswith("search_intent/live"):
        return [{"items": [{"keyword": k, "keyword_intent": {"label": "commercial"}} for k in payload["keywords"]]}]
    if endpoint.endswith("keyword_suggestions/live"):
        return [{"items": [{"keyword": payload["keyword"] + " cost"}, {"keyword": payload["keyword"] + " guide"}]}]
    if endpoint.endswith("bulk_ranks/live"):
        return [{"items": [{"target": d, "rank": 40} for d in payload["targets"]]}]
    return []


research.dataforseo_post = fake_post
comparisons.dataforseo_post = fake_post

# 1. per-keyword data: the second run buys only the new keyword
a = research.keyword_overview(c, ["seo for manufacturers", "no data kw"])
n1 = len(sent)
b = research.keyword_overview(c, ["seo for manufacturers", "no data kw", "plant marketing"])
checks.append(("first run buys both keywords", n1 == 1 and sent[0][1] == ["seo for manufacturers", "no data kw"], list(sent)))
checks.append(("second run buys only the new keyword", len(sent) == 2 and sent[1][1] == ["plant marketing"], list(sent[1:])))
checks.append(("cached values come back unchanged", b["seo for manufacturers"] == a["seo for manufacturers"], b))
checks.append(("a keyword with no data is remembered as no data", "no data kw" not in b, sorted(b)))

# 2. the age limit: 91-day-old data is bought again; 0 turns the cache off
sent.clear()
later = date.today() + timedelta(days=91)
dfscache.per_key(c, "overview", "United States|en", ["seo for manufacturers"],
                 lambda ks: research._keyword_overview(c, ks), today=later)
checks.append(("data older than 90 days is bought again", len(sent) == 1, list(sent)))
sent.clear()
dfscache.per_key(c, "overview", "United States|en", ["seo for manufacturers"],
                 lambda ks: research._keyword_overview(c, ks), today=date.today() + timedelta(days=89))
checks.append(("data younger than 90 days is reused", not sent, list(sent)))
off = {**c, "keyword_data_cache_days": 0}
research.keyword_overview(off, ["seo for manufacturers"])
checks.append(("keyword_data_cache_days: 0 always buys", len(sent) == 1, list(sent)))

# 3. difficulty and intent use the same cache rule
sent.clear()
research.bulk_keyword_difficulty(c, ["seo for manufacturers"]); research.bulk_keyword_difficulty(c, ["seo for manufacturers"])
research.search_intent(c, ["seo for manufacturers"]); research.search_intent(c, ["seo for manufacturers"])
checks.append(("difficulty and intent are bought once each", len(sent) == 2, list(sent)))

# 4. comparison research shares the keyword cache with keyword research
sent.clear()
vols, kd = comparisons._metrics(c, ["seo for manufacturers", "acme vs beta"])
checks.append(("comparisons reuse research's cached volume and difficulty",
               [s[1] for s in sent] == [["acme vs beta"], ["acme vs beta"]] and vols["seo for manufacturers"] and kd["seo for manufacturers"] == 12,
               list(sent)))
sent.clear()
comparisons.domain_ranks(c, ["www.example.com", "other.com"]); r = comparisons.domain_ranks(c, ["example.com"])
checks.append(("domain ranks are bought once per domain (www. ignored)", len(sent) == 1 and r == {"example.com": 40}, (list(sent), r)))

# 5. idea lists: reused within 30 days
sent.clear()
x = research.keyword_suggestions(c, "plant seo", 50); y = research.keyword_suggestions(c, "plant seo", 50)
checks.append(("an idea list is bought once and reused", len(sent) == 1 and x == y == ["plant seo cost", "plant seo guide"], list(sent)))
research.keyword_suggestions(c, "plant seo", 60)
checks.append(("a different request is bought", len(sent) == 2, list(sent)))
sent.clear()
research.keyword_suggestions({**c, "keyword_ideas_cache_days": 0}, "plant seo", 50)
checks.append(("keyword_ideas_cache_days: 0 always buys", len(sent) == 1, list(sent)))

# 6. tests never touch the real cache folder
checks.append(("tests use their own empty cache folder", "seo-engine-test-cache-" in str(c["_paths"]["cache"]), str(c["_paths"]["cache"])))

sys.exit(0 if report("DATAFORSEO CACHE", checks) else 1)
