"""Offline checks for the content-year rule: content is written for one year and names no earlier one."""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, health, textutil, writer
from tests_common import report

from tests_common import minimal_config
ind = minimal_config("\nenable_directory: false\n")
ind["content_year"] = 2027
checks = []

# 1. which year
checks.append(("November 2026 writes for 2027", textutil.content_year({}, date(2026, 11, 19)) == 2027, ""))
checks.append(("May 2026 writes for 2026", textutil.content_year({}, date(2026, 5, 1)) == 2026, ""))
checks.append(("a pinned content_year wins", textutil.content_year({"content_year": 2028}, date(2026, 5, 1)) == 2028, ""))
checks.append(("'auto' is the default", textutil.content_year({"content_year": "auto"}, date(2026, 11, 1)) == 2027, ""))

# 2. what counts as naming a year
text = ("What drives price in 2025, and the 2019-2023 trend. See [a guide](https://a.com/2024/07/post/) "
        "or https://b.com/2018/x. Costs run $2024 to $2,050 for 2,021 units. In 2027 budgets grow. `2016` in code.")
found = textutil.past_years(text, 2027)
checks.append(("prose years and ranges are caught", found == ["2019", "2023", "2025"], found))
checks.append(("link URLs, prices and code are ignored", not {"2016", "2018", "2021", "2024"} & set(found), found))
checks.append(("a 2022 keyword is a past-year keyword", textutil.has_past_year("content marketing for manufacturers 2022", 2027), ""))
checks.append(("a 2027 keyword is fine", not textutil.has_past_year("manufacturing seo 2027", 2027), ""))

# 3. the writer is told, and the critique blocks a draft that names an earlier year
system = writer.OpenRouterBackend.__new__(writer.OpenRouterBackend)._system(ind)
checks.append(("the writer prompt names the content year", "Write for 2027" in system and "before 2027" in system, ""))
brief = {"primary_keyword": "content marketing for manufacturers", "target_word_count": 100, "gaps_to_address": [],
         "outline": [], "external_sources": [], "internal_link_targets": [], "placement_rules": {}}
body = ("# Content Marketing for Manufacturers\n\nContent marketing for manufacturers, explained.\n\n"
        "## What does it cost in 2025?\n\ntext\n\n" + "word " * 300)
art = {"title": "Content Marketing for Manufacturers: A Buyer's Guide", "description": "x" * 130, "body": body}
fixes = writer.critique(art, brief, ind)
yr = [f for f in fixes if f["code"] == "past_year"]
checks.append(("a 2025 heading is a blocking fix", yr and yr in [[f] for f in writer.blocking(fixes) if f["code"] == "past_year"],
               [f["code"] for f in fixes]))
art2 = {**art, "body": body.replace("2025", "2027")}
checks.append(("the same draft for 2027 passes the year rule",
               not [f for f in writer.critique(art2, brief, ind) if f["code"] == "past_year"], ""))

# 4. title fallbacks use the content year
fb = writer._meta_fallback({"body": "Body text."}, "content marketing for manufacturers",
                          ["Content Marketing for Manufacturers (2024)"], 2027)
checks.append(("a retitle fallback uses 2027, never the year in ranking titles", "2027" in fb["title"] and "2024" not in fb["title"], fb["title"]))
t, _ = writer.TemplateBackend.__new__(writer.TemplateBackend)._title_desc(
    {"primary_keyword": "cnc seo", "intent": "informational"}, ind)
checks.append(("a short template title gets the content year", "2027" not in t or "(2027 Guide)" in t, t))

# 5. the health crawl finds published pages that name an earlier year
import tempfile, output
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "p.md"
    f.write_text("---\ntitle: 'Guide'\ndescription: 'What it costs'\npubDate: 2026-01-05\ntags:\n- seo 2022\n"
                 "faq:\n- question: Is it different in 2024?\n  answer: No.\n---\nBody in 2027. [x](https://a.com/2023/)\n")
    real = output.page_file
    output.page_file = lambda config, e: f
    try:
        got = health.page_past_years(ind, {"slug": "p"})
    finally:
        output.page_file = real
checks.append(("tags and FAQ are scanned; pubDate and link URLs are not", got == ["2022", "2024"], got))

# 6. the template writer can remove years; dated evidence items are reported
dropped = textutil.drop_past_years("Median response across 2025, see [log](https://a.com/2024/). Our log, March 2026", 2027)
checks.append(("years are dropped from prose, kept in links", dropped == "Median response, see [log](https://a.com/2024/). Our log"
               and not textutil.past_years(dropped, 2027), dropped))
import evidence
items = [{"id": "a", "title": "Jobs we logged in 2025", "numbers": ["38 minutes"]},
         {"id": "b", "title": "Jobs over 6 months", "numbers": ["38 minutes"]}]
checks.append(("an evidence item with a calendar year is reported", evidence.dated(ind, items) == [{"id": "a", "years": ["2025"]}],
               evidence.dated(ind, items)))

sys.exit(0 if report("CONTENT YEAR", checks) else 1)
