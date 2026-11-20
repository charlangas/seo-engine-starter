"""Directory category proposals built from a site's own agency records.

  python main.py categories --config configs/site.yaml

For sites that keep a curated directory as structured records (one JSON file per listing with
tag lists such as `capabilities`), this proposes one category page per tag: the listings that
carry the tag, in alphabetical order, with copy assembled only from the records themselves.

Nothing is written into the site. Proposals go to data/<site>/category_proposals/ with a
summary table, so a person decides which ones become real pages.

Rules:
  - One tag per page. Membership is exactly the published listings whose record carries the tag.
  - A tag carried by nearly every listing is skipped (it would duplicate the parent page);
    a tag carried by too few is skipped (too thin) but still reported when searches mention it.
  - No new claims about a listing: per-listing text is the record's own summary, verbatim.
  - No ranking language. Disclosure and comparison settings are copied from the template record.
  - lastVerified is the oldest date among the member listings, never today's date.
  - Search Console queries (latest gsc-check) are shown as demand evidence, raw numbers only.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import yaml

from config import ConfigError, get_runtime, load_config
from textutil import slugify, split_frontmatter

DEFAULTS = {
    "min_listings": 3,          # fewer member listings than this: too thin for a page
    "max_share": 0.8,           # tag on more than this share of listings: duplicates the parent page
    "min_query_words": 3,       # ignore shorter Search Console queries as evidence
    "max_questions": 8,         # aeoQuestions per proposal
    # A query only counts as demand for a category page if it is looking for a provider.
    "intent_terms": ["agency", "agencies", "firm", "firms", "company", "companies", "provider", "providers",
                     "services", "consultant", "consultants", "bureaus", "experts", "specialists"],
    "url_prefix": "/agencies",
    "guides_url_prefix": "/guides",
    "tag_fields": {"capabilities": "capability", "manufacturingExperience": "segment"},
    "requirement_fields": {"capability": "relevantCapabilityRequirements",
                           "segment": "relevantManufacturingExperienceCriteria"},
    "facets": {},               # per tag: {slug, title, noun, aliases}
    "member_notes": {},         # listing slug -> sentence added under the comparison intro when it is a member
}
MARKERS = ["<!-- agency-comparison -->", "<!-- related-guides -->"]
QUESTION = re.compile(r"\?$|^(which|what|who|how|where|can|should|is|are|do|does)\b")
RANKING_WORDS = re.compile(r"\b(best|top|leading|#1|number one|award[- ]winning)\b", re.I)


def _resolve(config, value) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (Path(config["config_path"]).resolve().parent / p).resolve()


def options(config) -> dict:
    raw = config.get("category_generator")
    if not raw:
        raise ConfigError("category_generator is not set in this site config (needs listings_directory "
                          "and template_record)")
    opts = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(opts.get(k), dict) and k != "facets":
            opts[k].update(v)
        else:
            opts[k] = v
    for need in ("listings_directory", "template_record"):
        if not opts.get(need):
            raise ConfigError(f"category_generator.{need} is required")
    return opts


# ---------- inputs ----------

def load_listings(directory: Path) -> list[dict]:
    out = []
    for f in sorted(directory.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("slug") and rec.get("name"):
            out.append(rec)
    return out


def load_vocabulary(path: Path | None) -> dict[str, list[str]]:
    """`export const NAME = [ 'a', 'b' ]` blocks from a TS/JS vocabulary file -> {NAME: [values]}."""
    if not path or not path.is_file():
        return {}
    out = {}
    for m in re.finditer(r"export const (\w+)\s*=\s*\[(.*?)\]", path.read_text(encoding="utf-8"), flags=re.S):
        out[m.group(1)] = [a or b for a, b in re.findall(r"'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\"", m.group(2))]
    return out


def load_indexing(path: Path | None) -> dict[str, str]:
    if not path or not path.is_file():
        return {}
    return {r["path"]: r.get("indexStatus", "") for r in json.loads(path.read_text(encoding="utf-8")).get("routes", [])}


def load_queries(config) -> tuple[list[dict], str | None]:
    """Query rows from the most recent live Search Console check, tool-prompt queries removed."""
    files = sorted((config["_paths"]["data"] / "gsc").glob("page_query_*.csv"))
    if not files:
        return [], None
    agg: dict[str, dict] = {}
    with open(files[-1], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            q = r["query"].strip().lower()
            if "<" in q or ">" in q:  # templated prompts from monitoring tools, not demand
                continue
            a = agg.setdefault(q, {"query": q, "impressions": 0, "best_position": 999.0})
            a["impressions"] += int(float(r["impressions"]))
            a["best_position"] = min(a["best_position"], float(r["position"]))
    return list(agg.values()), files[-1].name


def load_guides(config, opts) -> list[dict]:
    out = []
    for src in config.get("existing_content") or []:
        d = _resolve(config, src["directory"])
        for f in sorted(list(d.glob("*.md")) + list(d.glob("*.mdx"))) if d.is_dir() else []:
            fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
            status = fm.get("publicationStatus") or fm.get("status")
            if status != "published" or not fm.get("title"):
                continue
            slug = str(fm.get("routeSlug") or fm.get("slug") or f.stem)
            out.append({"label": str(fm["title"]).split(":")[0].strip(), "title": str(fm["title"]),
                        "href": f'{src.get("url_prefix", opts["guides_url_prefix"]).rstrip("/")}/{slug}/',
                        "description": str(fm.get("description") or "").strip()})
    return out


# ---------- proposal building ----------

def _facet(tag: str, kind: str, opts) -> dict:
    over = opts["facets"].get(tag) or {}
    head = tag.split(" / ")[0].strip()
    noun = over.get("noun") or tag
    aliases = [a.lower() for a in (over.get("aliases") or [p.strip() for p in tag.split(" / ")])]
    if kind == "capability":
        title = over.get("title") or f"{head} agencies for manufacturers"
    else:
        title = over.get("title") or f"Marketing agencies with {head.lower()} experience"
    return {"tag": tag, "kind": kind, "noun": noun, "aliases": aliases, "title": title,
            "slug": over.get("slug") or slugify(head)}


def _evidence(facet, queries, opts, brands=()) -> dict:
    rx = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(a) for a in facet["aliases"]) + r")(?![a-z0-9])")
    intent = re.compile(r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in opts["intent_terms"]) + r")(?![a-z0-9])")
    hits = []
    for q in queries:
        text = q["query"]
        squashed = re.sub(r"[^a-z0-9]", "", text)
        if len(text.split()) < opts["min_query_words"] or not rx.search(text) or not intent.search(text):
            continue
        if any(b in squashed for b in brands):  # a search for one named listing is not category demand
            continue
        hits.append(q)
    hits.sort(key=lambda q: (-q["impressions"], q["query"]))
    return {"queries": len(hits), "impressions": sum(q["impressions"] for q in hits),
            "examples": [{"query": q["query"], "impressions": q["impressions"],
                          "best_position": round(q["best_position"], 1)} for q in hits[:10]]}


def _names(members) -> str:
    names = [m["name"] for m in members]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def build_proposal(facet, members, total, template_fm, guides, evidence, opts) -> str:
    n, noun, kind = len(members), facet["noun"], facet["kind"]
    if kind == "capability":
        claim = f"publicly list {noun} among their services"
        definition = (f"This page lists agencies from the directory whose reviewed public materials include {noun}. "
                      "The label records what an agency says it offers, not how well it does it.")
        criterion = f"Its directory record lists {facet['tag']} as a capability, based on source-linked public evidence."
        generic_q = f"Which manufacturing marketing agencies offer {noun}?"
    else:
        claim = f"show public work in {noun}"
        definition = (f"This page lists agencies from the directory whose reviewed public materials show work in {noun}. "
                      "The label records public evidence of work in the segment, not depth or results.")
        criterion = f"Its directory record lists {facet['tag']} under manufacturing experience, based on source-linked public evidence."
        generic_q = f"Which marketing agencies have {noun} experience?"
    methodology = (f"We started from the {total} agencies in the directory and kept the {n} whose records list "
                   f"{facet['tag']}. No agency was added or removed by hand.")
    questions = [generic_q]
    for ex in evidence["examples"]:
        q = ex["query"].strip()
        if len(q) <= 120 and q not in questions and QUESTION.search(q):
            questions.append(q[:1].upper() + q[1:])
    questions.append("How was this list selected?")

    words = set(re.findall(r"[a-z]+", " ".join(facet["aliases"])))
    # Guides whose title mentions the tag come first; the template record's own guide list fills the rest.
    overlap = lambda g: len(words & set(re.findall(r"[a-z]+", g["title"].lower())))
    ranked = sorted((g for g in guides if overlap(g)), key=lambda g: -overlap(g))
    known = {g["href"] for g in guides}
    for t in template_fm.get("relatedGuides") or []:
        if t.get("href") in known and t["href"] not in {g["href"] for g in ranked}:
            ranked.append({"label": t["label"], "href": t["href"], "description": t.get("description", "")})
    ranked += [g for g in guides if g["href"] not in {r["href"] for r in ranked}]
    fm = {
        "title": facet["title"],
        "routeSlug": facet["slug"],
        "metaTitle": f"{facet['title']}: compare {n} firms",
        "metaDescription": (f"Compare {n} agencies with recurring manufacturing or industrial work that {claim}. "
                            "See focus, services, examples, and location."),
        "publicationStatus": "draft",
        "lastVerified": min(str(m["lastVerified"]) for m in members),
        "directAnswer": (f"{n} of the {total} agencies in this directory {claim}: {_names(members)}. "
                         "They are listed alphabetically; none is ranked."),
        "categoryDefinition": definition,
        "inclusionCriteria": ["The agency meets the inclusion criteria of the main agency directory.", criterion],
        "relevantCapabilityRequirements": [], "relevantManufacturingExperienceCriteria": [],
        "agencyIds": [m["slug"] for m in members],
        "manuallyIncludedAgencyIds": [], "manuallyExcludedAgencyIds": [],
        "comparisonFields": list(template_fm.get("comparisonFields") or []),
        "methodologyCopy": methodology,
        "choosingCopy": template_fm.get("choosingCopy", ""),
        "disclosure": template_fm.get("disclosure", ""),
        "relatedGuides": [{"label": g["label"], "href": g["href"], "description": g["description"]} for g in ranked[:3]],
        "aeoQuestions": questions[: opts["max_questions"]],
    }
    fm[opts["requirement_fields"][kind]] = [facet["tag"]]
    fm = {k: fm[k] for k in template_fm if k in fm} | {k: v for k, v in fm.items() if k not in template_fm}

    prefix = opts["url_prefix"].rstrip("/")
    notes = [opts["member_notes"][m["slug"]] for m in members if m["slug"] in opts["member_notes"]]
    body = ["## Compare the agencies", "",
            "Agencies do not pay for placement. Listings are alphabetical, with no scores or preferred placements.", ""]
    for note in notes:
        body += [note, ""]
    body += [MARKERS[0], "", "## How this list was built", "", methodology, "", definition, "",
             "## What each agency's record says", "",
             "Each line is the summary from that agency's directory record.", ""]
    body += [f"- **[{m['name']}]({prefix}/{m['slug']}/):** {m['summary'].strip()}" for m in members]
    body += ["", "## Before you contact anyone", "", fm["choosingCopy"], "", fm["disclosure"], "", MARKERS[1], ""]
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n\n" + "\n".join(body)


def check_proposal(md: str, template_fm: dict, vocab_values: set, listing_slugs: set, reserved_slugs: set) -> list[str]:
    """Problems that would stop the site from accepting this record. Empty list = clean."""
    fm, body = split_frontmatter(md)
    errs = []
    missing = [k for k in template_fm if k not in fm]
    if missing:
        errs.append(f"missing frontmatter keys: {missing}")
    extra = [k for k in fm if k not in template_fm]
    if extra:
        errs.append(f"frontmatter keys the template record does not have: {extra}")
    for marker in MARKERS:
        if body.count(marker) != 1:
            errs.append(f"marker {marker} must appear exactly once")
    if fm.get("routeSlug") in reserved_slugs:
        errs.append(f"routeSlug '{fm.get('routeSlug')}' collides with an existing listing or category")
    unknown = [a for a in fm.get("agencyIds") or [] if a not in listing_slugs]
    if unknown:
        errs.append(f"unknown listing ids: {unknown}")
    if not fm.get("agencyIds"):
        errs.append("no member listings")
    tags = (fm.get("relevantCapabilityRequirements") or []) + (fm.get("relevantManufacturingExperienceCriteria") or [])
    if vocab_values and [t for t in tags if t not in vocab_values]:
        errs.append(f"tags outside the site vocabulary: {[t for t in tags if t not in vocab_values]}")
    if fm.get("publicationStatus") != "draft":
        errs.append("proposals must be publicationStatus: draft")
    hit = RANKING_WORDS.search(" ".join(str(fm.get(k, "")) for k in ("title", "metaTitle", "metaDescription", "directAnswer")))
    if hit:
        errs.append(f"ranking language in title or summary fields: '{hit.group(0)}'")
    if re.search(r"\{[a-z_]+\}|\bTODO\b|\bTBD\b", md):
        errs.append("placeholder text")
    return errs


# ---------- entry point ----------

def generate(config) -> dict:
    rt = get_runtime(config)
    opts = options(config)
    listings_dir = _resolve(config, opts["listings_directory"])
    template_path = _resolve(config, opts["template_record"])
    if not listings_dir.is_dir():
        raise ConfigError(f"category_generator.listings_directory not found: {listings_dir}")
    if not template_path.is_file():
        raise ConfigError(f"category_generator.template_record not found: {template_path}")
    template_fm, _ = split_frontmatter(template_path.read_text(encoding="utf-8"))
    all_listings = load_listings(listings_dir)
    listings = sorted((l for l in all_listings if l.get("publicationStatus", "published") == "published"),
                      key=lambda l: l["name"].lower())
    total = len(listings)
    vocab = load_vocabulary(_resolve(config, opts["vocabulary_file"]) if opts.get("vocabulary_file") else None)
    vocab_values = {v for vals in vocab.values() for v in vals}
    inv = (config.get("route_inventory") or {}).get("file")
    indexing = load_indexing(_resolve(config, inv) if inv else None)
    queries, gsc_file = load_queries(config)
    guides = load_guides(config, opts)
    existing = {split_frontmatter(f.read_text(encoding="utf-8"))[0].get("routeSlug") for f in template_path.parent.glob("*.md")}
    reserved = {l["slug"] for l in all_listings} | {s for s in existing if s}
    prefix = opts["url_prefix"].rstrip("/")
    brands = {b for l in all_listings for b in (re.sub(r"[^a-z0-9]", "", l["name"].lower()),
                                                 re.sub(r"[^a-z0-9]", "", l["slug"].lower())) if len(b) >= 5}

    out_dir = config["_paths"]["data"] / "category_proposals"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):
        old.unlink()  # proposals are regenerated from the records every time

    rows = []
    for field, kind in opts["tag_fields"].items():
        tags = sorted({t for l in listings for t in l.get(field) or []})
        for tag in tags:
            facet = _facet(tag, kind, opts)
            members = [l for l in listings if tag in (l.get(field) or [])]
            ev = _evidence(facet, queries, opts, brands)
            noindex = [m["slug"] for m in members if indexing.get(f"{prefix}/{m['slug']}/") == "noindex"]
            row = {"tag": tag, "kind": kind, "slug": facet["slug"], "title": facet["title"], "listings": len(members),
                   "of": total, "queries": ev["queries"], "impressions": ev["impressions"], "examples": ev["examples"],
                   "noindex_members": len(noindex), "status": "proposed", "reason": "", "file": None, "problems": []}
            if facet["slug"] in existing:
                row.update(status="installed", reason="already a category record in the site")
            elif total and len(members) / total > opts["max_share"]:
                row.update(status="skipped", reason=f"on {len(members)} of {total} listings; would duplicate the parent page")
            elif len(members) < opts["min_listings"]:
                row.update(status="skipped", reason=f"only {len(members)} listing(s); minimum is {opts['min_listings']}")
            else:
                md = build_proposal(facet, members, total, template_fm, guides, ev, opts)
                row["problems"] = check_proposal(md, template_fm, vocab_values, {l["slug"] for l in all_listings}, reserved)
                if row["problems"]:
                    row.update(status="invalid", reason="; ".join(row["problems"]))
                else:
                    path = out_dir / f"{facet['slug']}.md"
                    path.write_text(md, encoding="utf-8")
                    row["file"] = str(path)
                    reserved.add(facet["slug"])
            rows.append(row)
    order = {"proposed": 0, "installed": 1, "invalid": 2, "skipped": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["impressions"], -r["queries"], -r["listings"], r["tag"]))

    summary = {"site": config["site_name"], "listings": total, "search_console_file": gsc_file,
               "thresholds": {k: opts[k] for k in ("min_listings", "max_share", "min_query_words")}, "rows": rows}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    summary["directory"] = str(out_dir)
    rt.log("stage", stage="category_proposals", proposed=sum(r["status"] == "proposed" for r in rows),
           skipped=sum(r["status"] == "skipped" for r in rows), invalid=sum(r["status"] == "invalid" for r in rows))
    return summary


def render_summary(s: dict) -> str:
    t = s["thresholds"]
    lines = [f"# Category proposals: {s['site']}", "",
             f"- {s['listings']} published listings read from the site's records.",
             "- Search Console evidence: " + (f"`{s['search_console_file']}` (only queries of "
                                              f"{t['min_query_words']}+ words that look for a provider; tool prompts "
                                              "and searches for a named listing removed)."
                                              if s["search_console_file"] else "none found; run `gsc-check` first."),
             f"- A tag needs at least {t['min_listings']} listings and at most {int(t['max_share'] * 100)}% of all listings.",
             "- Every proposal is `publicationStatus: draft`. Nothing has been written into the site.", "",
             "## Proposed", "",
             "| Tag | Proposed URL slug | Listings | Matching queries | Impressions | Members set to noindex |",
             "|---|---|---|---|---|---|"]
    for r in s["rows"]:
        if r["status"] == "proposed":
            lines.append(f"| {r['tag']} | `{r['slug']}` | {r['listings']} of {r['of']} | {r['queries']} | "
                         f"{r['impressions']} | {r['noindex_members']} |")
    lines += ["", "## Not proposed", "", "| Tag | Listings | Matching queries | Impressions | Why |", "|---|---|---|---|---|"]
    for r in s["rows"]:
        if r["status"] != "proposed":
            lines.append(f"| {r['tag']} | {r['listings']} of {r['of']} | {r['queries']} | {r['impressions']} | {r['reason']} |")
    lines += ["", "## Queries behind the numbers", ""]
    for r in s["rows"]:
        if r["examples"]:
            lines.append(f"**{r['tag']}** ({r['status']})")
            lines += [f"- {e['query']} ({e['impressions']} impressions, best position {e['best_position']})" for e in r["examples"]]
            lines.append("")
    return "\n".join(lines) + "\n"


def install(config, slugs: list[str]) -> list[dict]:
    """Copy chosen proposals into the site's category collection as drafts and register their routes.

    A proposal is installed exactly as generated: publicationStatus draft, route noindex.
    Publishing and indexing stay a manual decision in the site. Existing files are never overwritten.
    """
    import output
    rt = get_runtime(config)
    opts = options(config)
    src_dir = config["_paths"]["data"] / "category_proposals"
    dest_dir = _resolve(config, opts["template_record"]).parent
    listing_files = {f.stem for f in _resolve(config, opts["listings_directory"]).glob("*.json")}
    prefix = opts["url_prefix"].rstrip("/")
    results = []
    for slug in slugs:
        src, dest = src_dir / f"{slug}.md", dest_dir / f"{slug}.md"
        if not src.is_file():
            results.append({"slug": slug, "status": "not_found", "detail": f"no proposal at {src}"})
            continue
        if dest.exists():
            results.append({"slug": slug, "status": "exists", "detail": f"{dest} already exists; left untouched"})
            continue
        md = src.read_text(encoding="utf-8")
        fm, _ = split_frontmatter(md)
        unknown = [a for a in fm.get("agencyIds") or [] if a not in listing_files]
        if unknown or fm.get("publicationStatus") != "draft":
            results.append({"slug": slug, "status": "invalid",
                            "detail": f"unknown listing files {unknown}" if unknown else "proposal is not a draft"})
            continue
        dest.write_text(md, encoding="utf-8")
        route = f"{prefix}/{fm['routeSlug']}/"
        added = bool(config.get("route_inventory")) and output.add_route(config, route, "category", "draft", "noindex")
        rt.log("stage", stage="category_installed", slug=slug, file=str(dest), route=route, route_added=added)
        results.append({"slug": slug, "status": "installed", "detail": str(dest), "route": route, "route_added": added})
    return results


def cli_install(config_path, slugs) -> int:
    config = load_config(config_path)
    results = install(config, slugs)
    for r in results:
        print(f"  {r['status']:<10} {r['slug']}: {r['detail']}" + (f"  (route {r['route']} draft, noindex)" if r.get("route_added") else ""))
    return 0 if all(r["status"] == "installed" for r in results) else 1


def cli(config_path) -> int:
    config = load_config(config_path)
    s = generate(config)
    print(f"\n=== Category proposals: {s['site']} ({s['listings']} listings) ===")
    for r in s["rows"]:
        mark = {"proposed": "+", "installed": "=", "skipped": "-", "invalid": "!"}[r["status"]]
        print(f"  {mark} {r['tag']:<38} {r['listings']:>2} listings  {r['queries']:>2} queries  {r['impressions']:>4} impr"
              + (f"  ({r['reason']})" if r["reason"] else ""))
    print(f"proposals and summary: {s['directory']}")
    return 0
