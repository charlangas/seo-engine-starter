"""Category proposal self-verification on a synthetic listing set (no network, no spend)."""
import csv, json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import categories, config as cfg
from textutil import split_frontmatter
from tests_common import report


def listing(d, slug, name, caps, segs, verified, summary, status="published"):
    (d / f"{slug}.json").write_text(json.dumps({
        "name": name, "slug": slug, "publicationStatus": status, "lastVerified": verified,
        "capabilities": caps, "manufacturingExperience": segs, "summary": summary}))


def main():
    tmp = Path(tempfile.mkdtemp())
    ag, cat, guides = tmp / "agencies", tmp / "cats", tmp / "guides"
    for d in (ag, cat, guides):
        d.mkdir()
    listing(ag, "zeta-works", "Zeta Works", ["SEO / organic search", "Content"], ["Packaging"], "2026-03-01", "Zeta summary sentence.")
    listing(ag, "acme-industrial", "Acme Industrial", ["SEO / organic search", "Content", "ABM"], ["Packaging", "Energy / utilities"], "2026-01-15", "Acme summary sentence.")
    listing(ag, "mid-co", "Mid Co", ["SEO / organic search", "Content"], ["Packaging"], "2026-02-01", "Mid summary sentence.")
    listing(ag, "omega", "Omega", ["Content"], ["Energy / utilities"], "2026-02-10", "Omega summary sentence.")
    listing(ag, "hidden", "Hidden Draft", ["SEO / organic search", "Content", "ABM"], ["Packaging"], "2025-01-01", "Hidden summary.", status="draft")
    (cat / "main.md").write_text(
        "---\ntitle: Main\nrouteSlug: main-directory\nmetaTitle: m\nmetaDescription: d\npublicationStatus: published\n"
        "lastVerified: '2026-01-01'\ndirectAnswer: a\ncategoryDefinition: c\ninclusionCriteria: [x]\n"
        "relevantCapabilityRequirements: []\nrelevantManufacturingExperienceCriteria: []\nagencyIds: [omega]\n"
        "manuallyIncludedAgencyIds: []\nmanuallyExcludedAgencyIds: []\ncomparisonFields: [agency]\nmethodologyCopy: m\n"
        "choosingCopy: Confirm scope directly.\ndisclosure: Inclusion is not endorsement.\nrelatedGuides: []\naeoQuestions: []\n---\n\nbody\n")
    (guides / "seo-guide.md").write_text("---\ntitle: 'SEO Guide: basics'\nrouteSlug: seo-guide\ndescription: About SEO.\nstatus: published\n---\n\nx\n")
    (guides / "draft-guide.md").write_text("---\ntitle: Draft Guide\nrouteSlug: draft-guide\ndescription: d\nstatus: draft\n---\n\nx\n")
    (tmp / "vocab.ts").write_text("export const CAPABILITY_VALUES = [\n  'SEO / organic search',\n  'Content',\n  'ABM',\n] as const;\n"
                                  "export const SEGMENTS = ['Packaging', 'Energy / utilities'] as const;\n")
    (tmp / "routes.json").write_text(json.dumps({"routes": [
        {"path": "/agencies/zeta-works/", "indexStatus": "noindex"}, {"path": "/agencies/acme-industrial/", "indexStatus": "index"}]}))
    yml = tmp / "site.yaml"
    yml.write_text(f"""site_name: Category Selftest
domain: category-selftest.example
business_type: agency
niche_description: test
seed_keywords: [x]
output_directory: {tmp / 'blog'}
enable_directory: false
existing_content:
  - directory: {guides}
    url_prefix: /guides
route_inventory:
  file: {tmp / 'routes.json'}
category_generator:
  listings_directory: {ag}
  template_record: {cat / 'main.md'}
  vocabulary_file: {tmp / 'vocab.ts'}
  min_listings: 3
  max_share: 0.8
  member_notes:
    acme-industrial: "Acme note."
  facets:
    "SEO / organic search": {{slug: seo, noun: "SEO", aliases: [seo]}}
    "Packaging": {{slug: zeta-works, noun: "packaging", aliases: [packaging]}}
    "Energy / utilities": {{slug: energy, noun: "energy", aliases: [energy, oil and gas]}}
""")
    c = cfg.load_config(yml)
    data_dir = c["_paths"]["data"]
    (data_dir / "gsc").mkdir(parents=True, exist_ok=True)
    with open(data_dir / "gsc" / "page_query_2026-01-01.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["page", "query", "clicks", "impressions", "position"])
        w.writerow(["/agencies/", "which seo agencies work with manufacturers?", 0, 7, 4.0])
        w.writerow(["/agencies/x/", "which seo agencies work with manufacturers?", 0, 3, 9.0])
        w.writerow(["/agencies/", "manufacturing seo strategy guide", 0, 500, 5.0])           # no provider intent
        w.writerow(["/agencies/", "<brand>acme</brand> seo agency criticised", 0, 900, 5.0])  # tool prompt
        w.writerow(["/agencies/", "acme industrial seo agency reviews", 0, 400, 5.0])        # named listing
        w.writerow(["/agencies/", "oil and gas marketing agency", 0, 60, 30.0])
    site_before = {f: f.read_text() for d in (ag, cat, guides) for f in d.iterdir()}

    s = categories.generate(c)
    rows = {r["tag"]: r for r in s["rows"]}
    out = Path(s["directory"])
    seo_md = (out / "seo.md").read_text() if (out / "seo.md").exists() else ""
    fm, body = split_frontmatter(seo_md)
    template_fm, _ = split_frontmatter((cat / "main.md").read_text())
    checks = [
        ("tag on 3 of 4 listings is proposed", rows["SEO / organic search"]["status"] == "proposed" and rows["SEO / organic search"]["listings"] == 3, ""),
        ("tag on every listing skipped as a duplicate of the parent", rows["Content"]["status"] == "skipped" and "duplicate" in rows["Content"]["reason"], rows["Content"]["reason"]),
        ("thin tag skipped but its demand still reported", rows["Energy / utilities"]["status"] == "skipped" and rows["Energy / utilities"]["impressions"] == 60, rows["Energy / utilities"]["reason"]),
        ("draft listings are never members", "hidden" not in (fm.get("agencyIds") or []) and rows["ABM"]["listings"] == 1, ""),
        ("membership is exactly the tagged listings, alphabetical", fm.get("agencyIds") == ["acme-industrial", "mid-co", "zeta-works"], fm.get("agencyIds")),
        ("slug colliding with a listing is rejected, no file written", rows["Packaging"]["status"] == "invalid" and "collides" in rows["Packaging"]["reason"] and not (out / "zeta-works.md").exists(), rows["Packaging"]["reason"]),
        ("same frontmatter keys as the template record", list(fm.keys()) == list(template_fm.keys()), ""),
        ("always a draft", fm.get("publicationStatus") == "draft", ""),
        ("lastVerified is the oldest member date, not today", fm.get("lastVerified") == "2026-01-15", fm.get("lastVerified")),
        ("tag recorded in the capability requirement field", fm.get("relevantCapabilityRequirements") == ["SEO / organic search"] and fm.get("relevantManufacturingExperienceCriteria") == [], ""),
        ("disclosure and comparison fields copied from the template", fm.get("disclosure") == "Inclusion is not endorsement." and fm.get("comparisonFields") == ["agency"], ""),
        ("both body markers exactly once", all(body.count(m) == 1 for m in categories.MARKERS), ""),
        ("per-listing text is the record summary verbatim", all(t in body for t in ("Acme summary sentence.", "Mid summary sentence.", "Zeta summary sentence.")) and "Omega summary" not in body, ""),
        ("member note added only when that listing is a member", "Acme note." in body, ""),
        ("only published guides linked", [g["href"] for g in fm.get("relatedGuides") or []] == ["/guides/seo-guide/"], fm.get("relatedGuides")),
        ("evidence: provider-seeking queries only, summed across pages", rows["SEO / organic search"]["queries"] == 1 and rows["SEO / organic search"]["impressions"] == 10, (rows["SEO / organic search"]["queries"], rows["SEO / organic search"]["impressions"])),
        ("real question reused as an AEO question", "Which seo agencies work with manufacturers?" in (fm.get("aeoQuestions") or []), fm.get("aeoQuestions")),
        ("no ranking language in title or summary fields", not categories.RANKING_WORDS.search(" ".join(str(fm.get(k)) for k in ("title", "metaTitle", "metaDescription", "directAnswer"))), ""),
        ("noindex member count reported", rows["SEO / organic search"]["noindex_members"] == 1, rows["SEO / organic search"]["noindex_members"]),
        ("site files untouched", all(f.read_text() == t for f, t in site_before.items()) and {f for d in (ag, cat, guides) for f in d.iterdir()} == set(site_before), ""),
        ("summary table written", (out / "summary.md").exists() and "Not proposed" in (out / "summary.md").read_text(), ""),
    ]
    bad = seo_md.replace("publicationStatus: draft", "publicationStatus: published").replace(categories.MARKERS[1], "")
    errs = categories.check_proposal(bad, template_fm, {"SEO / organic search"}, {"acme-industrial", "mid-co", "zeta-works"}, set())
    checks.append(("checker catches a published status and a missing marker", len(errs) == 2, errs))
    c2 = cfg.load_config(yml); c2.pop("category_generator")
    try:
        categories.generate(c2); checks.append(("missing settings fail clearly", False, ""))
    except cfg.ConfigError as e:
        checks.append(("missing settings fail clearly", "category_generator" in str(e), ""))

    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.rmtree(cfg.ROOT / "logs" / "category-selftest", ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return report("CATEGORY PROPOSALS", checks)

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
