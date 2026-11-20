"""onpage.py (on-page + AI-search rules) and aiaudit.py (internal editor checklist). No spend."""
import json, sys, tempfile
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aiaudit, config as cfg, onpage, writer
from textutil import build_frontmatter, split_frontmatter
from tests_common import report

ROOT = Path(__file__).resolve().parent.parent
checks = []


def make_config(extra=""):
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "site.yaml"
    p.write_text((ROOT / "configs" / "test-minimal.yaml").read_text() + extra)
    c = cfg.load_config(p, dry_run=True)
    c["_paths"].update(data=tmp, work=tmp, inventory=tmp / "inventory.json", output=tmp / "content")
    return c, tmp


SITE = ("\non_page:\n  cta: {label: Book a repair, url: /book/}\n  author: {name: Dana Ruiz, title: Master plumber, "
        "url: /about/dana/, socials: {linkedin: 'https://linkedin.com/in/danaruiz'}}\n")
c, tmp = make_config(SITE)


def codes(body, brief):
    out = []
    onpage.critique({"body": body}, brief, lambda code, msg, **k: out.append((code, k)))
    return out


# 1. config
for extra, label in (("\non_page: {fanout: [cost, vibes]}\n", "unknown bucket"), ("\non_page: {colour: red}\n", "unknown key"),
                     ("\non_page: {cta: {label: x}}\n", "cta without url"), ("\non_page: {max_paragraph_words: 5}\n", "tiny paragraphs")):
    try:
        make_config(extra)
        checks.append((f"bad on_page config rejected ({label})", False, "loaded"))
    except cfg.ConfigError as e:
        checks.append((f"bad on_page config rejected ({label})", "on_page" in str(e), str(e)[-80:]))
c2, _ = make_config("\ncomparisons: {author: {name: Sam}, cta: {label: Try it, url: /trial/}}\n")
s2 = onpage.settings(c2)
checks.append(("author and CTA fall back to the comparisons block", s2["author"]["name"] == "Sam" and s2["cta"]["url"] == "/trial/", s2))

# 2. attach + prompt
brief = {"primary_keyword": "best water heater brands", "outline": [{"h2": "How much does a water heater cost?", "h3": []}]}
op = onpage.attach(brief, c)
checks.append(("fan-out buckets already in the outline are noted; list keywords need best-for labels",
               op["fanout_in_outline"] == ["cost"] and op["best_for"] and op["cta"]["url"] == "/book/", op))
pb = onpage.prompt_block(brief)
checks.append(("prompt asks for BLUF, takeaways, the missing fan-out questions and 3 CTAs",
               "Bottom line up front" in pb and "## Key takeaways" in pb and "How long does it take?" in pb
               and "How much does it cost?" not in pb and "at least 3 times" in pb, pb[-400:]))

# 3. critique
BAD = ("# Best water heater brands\n\nIntro with an answer.\n\n## Brands we like\n\nThis is the part that matters. "
       + "Word " * 100 + "end.\n\n### Rheem\n\nSolid tanks.\n\n### A. O. Smith\n\nLong warranty.\n\n"
       "## Installing one\n\nAs mentioned above, installs vary.\n\n## Frequently asked questions\n\n### Is it loud?\n\nNo.\n")
got = {k for k, _ in codes(BAD, brief)}
checks.append(("weak page: no takeaways, long paragraph, back-references, missing fan-out, no best-for, CTAs short",
               got == {"key_takeaways_missing", "paragraph_too_long", "section_not_self_contained", "back_reference",
                       "fanout_missing", "best_for_missing", "cta_placement"}, sorted(got)))
fan = sorted(k["bucket"] for code, k in codes(BAD, brief) if code == "fanout_missing")
checks.append(("each missing fan-out bucket is its own fix", fan == ["cost", "next_steps", "not_for", "risks", "timeline"], fan))
GOOD = ("# Best water heater brands\n\nRheem and A. O. Smith top our list for 2026. [Book a repair](/book/).\n\n"
        "## Key takeaways\n\n- Rheem tanks cost $900 to $1,400 installed.\n- A. O. Smith gives a 10-year warranty.\n"
        "- Tankless models save space but cost more up front.\n\n"
        "## Which water heater brands last longest?\n\nRheem and A. O. Smith last longest in our installs.\n\n"
        "### Rheem\n\nBest for: tight budgets.\n\n### A. O. Smith\n\nBest for: long warranties. [Book a repair](/book/).\n\n"
        "## How long does installation take?\n\nInstallation takes half a day.\n\n"
        "## What are the common mistakes?\n\nBuying the wrong size is the common mistake.\n\n"
        "## Who is a tankless heater not right for?\n\nSmall households rarely recover the cost.\n\n"
        "## How do you choose and get started?\n\nWater heater buyers should list size, fuel and budget. [Book a repair](/book/).\n\n"
        "## Frequently asked questions\n\n### How much does a new water heater cost?\n\n$900 to $1,400 installed in 2026.\n")
checks.append(("a page that follows the rules passes", codes(GOOD, brief) == [], codes(GOOD, brief)))
checks.append(("takeaways must be the first H2", "key_takeaways_position" in {k for k, _ in codes(
    GOOD.replace("## Key takeaways\n\n- Rheem tanks cost $900 to $1,400 installed.\n- A. O. Smith gives a 10-year warranty.\n"
                 "- Tankless models save space but cost more up front.\n\n", "") .replace(
        "## Frequently asked", "## Key takeaways\n\n- One fact here.\n- Two facts here.\n- Three facts here.\n\n## Frequently asked"), brief)}, ""))

# 4. frontmatter
fm = {"title": "Best water heater brands", "description": "d", "pubDate": date(2026, 11, 1),
      "faq": [{"question": "How much?", "answer": "$900."}]}
extra = onpage.frontmatter(fm, GOOD, {**brief, "slug": "best-water-heater-brands", "schema_type": "Service"}, c)
types = [g["@type"] for g in extra["jsonLd"]["@graph"]]
checks.append(("every page gets takeaways, reading time, author and JSON-LD (Article + Service + FAQPage, Person author)",
               extra["takeaways"][0].startswith("Rheem tanks") and extra["readingTime"] >= 1 and extra["author"]["name"] == "Dana Ruiz"
               and types == ["Article", "Service", "FAQPage"] and extra["jsonLd"]["@graph"][0]["author"]["@type"] == "Person"
               and extra["jsonLd"]["@graph"][0]["author"]["sameAs"] == ["https://linkedin.com/in/danaruiz"], (types, extra.get("author"))))

# 5. template writer satisfies every new rule end to end (real saved brief)
saved = sorted((ROOT / "data" / "test-minimal" / "briefs").glob("brief_*.json"))
if saved:
    real = json.loads(saved[0].read_text())
    md = writer.generate_article(real, c)
    f2, body = split_frontmatter(md)
    left = writer.blocking(writer.critique({"title": f2["title"], "description": f2["description"], "body": body.lstrip()}, real, c))
    checks.append(("template writer adds takeaways, fan-out answers, CTAs; critique clean", not left and "## Key takeaways" in body
                   and body.count("](/book/)") >= 3 and f2.get("takeaways") and f2.get("jsonLd"), [x["code"] for x in left]))
    # 6. audit
    a = aiaudit.score_page(md, c, {}, date(2026, 11, 18))
    checks.append(("audit scores all 81 checklist items", a["total"] == 81 and 0 < a["score"] <= 100, a["score"]))
    checks.append(("the rules the writer enforces show up as passed audit items",
                   not any(m.startswith(("Page components: Key takeaways", "Fan-out C: implicit questions: Explains cost",
                                         "Fan-out C: implicit questions: Explains risks", "How the copy reads: Every H2"))
                           for m in a["missing"]), [m for m in a["missing"]][:12]))
else:
    checks.append(("saved test brief available", False, "run tests/test_stage2.py first"))
bare = aiaudit.score_page("---\ntitle: x\n---\n# X\n\nShort page.\n", c, {}, date(2026, 11, 18))
checks.append(("a bare page scores low and lists what to add", bare["score"] < 25 and len(bare["missing"]) > 60, bare["score"]))

# 7. internal editor checklist: fixable items only, editor-only, never a command
items = aiaudit.editor_items({"title": "x", "body": "# X\n\nShort page about water heaters.\n"},
                             {"primary_keyword": "water heater repair"}, c)
checks.append(("editor gets text-fixable checklist items (capped), never ones that need a person (video, author, screenshots)",
               0 < len(items) <= 8 and not any(w in " ".join(items).lower() for w in ("video", "author", "screenshot", "testimonial")), items))
import main as _main
checks.append(("the checklist is not a user-facing command", "ai-audit" not in open(_main.__file__).read()
               and not hasattr(aiaudit, "cli"), ""))
captured = {}


class FakeEditor(writer.OpenRouterBackend):
    pass


import writer as _w
_orig = _w.openrouter_chat
_w.openrouter_chat = lambda config, model, system, user, **k: (captured.update(system=system, user=user) or '{"verdict": "approve", "fixes": []}')
try:
    FakeEditor().edit({"title": "x", "description": "d", "body": "# X\n\nShort page.\n"},
                      {**json.loads(saved[0].read_text()), "primary_keyword": "water heater repair"} if saved else
                      {"primary_keyword": "water heater repair", "secondary_keywords": [], "intent": "x", "target_word_count": 10,
                       "outline": [], "gaps_to_address": []}, c)
finally:
    _w.openrouter_chat = _orig
checks.append(("the editor (not the writer prompt, not the page) receives the checklist, marked internal",
               "INTERNAL CHECKLIST" in captured.get("user", "") and "never for the page" in captured.get("system", ""), ""))

sys.exit(0 if report("onpage", checks) else 1)
