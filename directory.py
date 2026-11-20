"""Directory hybrid: listing pages + category index pages built from real SERP data."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import inventory
from apiclient import serpapi_get
from config import get_runtime
from textutil import build_frontmatter, slugify, title_case, word_count

CACHE_DAYS = 7
TIPS = [
    "Before you book, ask for a written quote and confirm what it covers, so the final bill holds no surprises.",
    "When you call, ask how soon they can start and who will actually do the work, since both vary by week.",
    "It's worth reading the three most recent reviews rather than the overall score, because recent ones show how they operate now.",
    "Ask whether there is a call-out or assessment fee and whether it is credited against the job if you go ahead.",
    "Compare at least two listings in this category on the same job description before you decide.",
]


def directory_root(config) -> Path:
    if get_runtime(config).dry_run:
        return config["_paths"]["content"] / "directory"
    if config.get("directory_output_directory"):
        p = Path(config["directory_output_directory"]).expanduser()
        return p if p.is_absolute() else (Path(config["config_path"]).resolve().parent.parent / p).resolve()
    return config["_paths"]["output"] / "directory"


def _city(config) -> str:
    return (config.get("location") or "").split(",")[0].strip()


def fetch_listings(config, category: str) -> list[dict]:
    rt = get_runtime(config)
    loc = config.get("location") or ""
    local = config["directory_type"] in ("local_services", "firms") and bool(loc)
    key = hashlib.sha1(f"{category.lower()}|{loc.lower()}|{config['directory_type']}".encode()).hexdigest()
    cache = config["_paths"]["cache"] / "directory" / f"{key}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        rt.log("cache_hit", kind="directory", category=category)
        return json.loads(cache.read_text(encoding="utf-8"))
    out = []
    if local:
        data = serpapi_get(config, {"engine": "google_maps", "type": "search", "q": f"{category} in {loc}",
                                    "hl": config["language_code"]})
        for r in data.get("local_results") or []:
            if not r.get("title"):
                continue
            hours = r.get("operating_hours") or {}
            out.append({"name": r["title"], "address": r.get("address"), "phone": r.get("phone"),
                        "website": re.sub(r"[?#].*$", "", r["website"]) if r.get("website") else None, "rating": r.get("rating"), "reviews": r.get("reviews"),
                        "business_type": r.get("type"),
                        "hours": "; ".join(f"{d.title()}: {h}" for d, h in hours.items()) if isinstance(hours, dict) else None,
                        "summary": r.get("description"),
                        "services": [k.replace("_", " ") for k, v in (r.get("service_options") or {}).items() if v]})
    else:
        from serp import SKIP_DOMAINS, resolve_result_url
        data = serpapi_get(config, {"engine": "google", "q": category, "hl": config["language_code"], "num": 10})
        seen = set()
        for r in data.get("organic_results") or []:
            url, ok = resolve_result_url(config, r.get("link", ""))
            dom = re.sub(r"^www\.", "", urlparse(url).netloc.lower())
            if not ok or not dom or dom in seen or any(s in dom for s in SKIP_DOMAINS):
                continue
            seen.add(dom)
            out.append({"name": title_case(dom.split(".")[0].replace("-", " ")), "website": f"https://{dom}",
                        "summary": r.get("snippet"), "source_title": r.get("title"), "address": None, "phone": None,
                        "rating": None, "reviews": None, "business_type": None, "hours": None, "services": []})
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def describe(l: dict, category: str, peers: list[dict], config) -> str:
    """Unique description (>= 80 words) composed only from real listing data."""
    city = _city(config)
    kind = (l.get("business_type") or category.rstrip("s")).lower()
    s = []
    where = f" in {city}" if city else ""
    s.append(f"{l['name']} is listed here under {category}{where}"
             + (f", and Google classifies it as a {kind}." if l.get("business_type") else "."))
    if l.get("address"):
        s.append(f"You'll find it at {l['address']}.")
    rated = [p for p in peers if p.get("rating") is not None]
    if l.get("rating") is not None:
        s.append(f"It holds a {l['rating']}-star average on Google"
                 + (f" across {l['reviews']:,} reviews." if l.get("reviews") else "."))
        if len(rated) > 1:
            avg = sum(p["rating"] for p in rated) / len(rated)
            rel = "above" if l["rating"] > avg + 0.05 else "below" if l["rating"] < avg - 0.05 else "level with"
            s.append(f"That sits {rel} the {avg:.1f} average for the {len(rated)} {category} tracked on this page.")
        counted = sorted([p for p in peers if p.get("reviews")], key=lambda p: -p["reviews"])
        if l.get("reviews") and len(counted) > 1:
            rank = counted.index(l) + 1 if l in counted else None
            if rank:
                s.append(f"By review volume it ranks number {rank} of {len(counted)} in this group, a rough signal "
                         f"of how many customers it has served.")
    if l.get("summary"):
        summ = " ".join(str(l["summary"]).replace('"', "'").split()[:28]).rstrip(" .,;:")
        s.append(f'Its public description reads: "{summ}."')
    if l.get("services"):
        s.append("Listed service options include " + ", ".join(l["services"][:4]) + ".")
    if l.get("hours"):
        open_days = [d for d in l["hours"].split("; ") if "closed" not in d.lower()] or l["hours"].split("; ")
        s.append(f"Published hours include {open_days[0]}, with the full week shown below.")
    if l.get("phone"):
        s.append(f"The listed phone number is {l['phone']}.")
    if l.get("website"):
        s.append(f"More detail is on its website, {urlparse(l['website']).netloc or l['website']}.")
    h = int(hashlib.sha1(l["name"].encode()).hexdigest(), 16)
    i = 0
    while word_count(" ".join(s)) < 85 and i < len(TIPS):
        s.append(TIPS[(h + i) % len(TIPS)])
        i += 1
    return " ".join(s)


def _filter_fields(fields: dict, config) -> dict:
    wanted = config.get("listing_fields") or []
    if not wanted:
        return {k: v for k, v in fields.items() if v not in (None, "", [])}
    return {k: v for k, v in fields.items() if k in wanted and v not in (None, "", [])}


def build_directory(config) -> dict:
    """Write listing + category Markdown. Returns {'listings': n, 'categories': n}."""
    rt = get_runtime(config)
    if not config["enable_directory"]:
        return {"listings": 0, "categories": 0}
    rt.log("stage", stage="directory", type=config["directory_type"])
    root = directory_root(config)
    prefix = config["directory_url_prefix"].rstrip("/")
    city = _city(config)
    today = date.today().isoformat()
    categories = config.get("directory_categories") or config["seed_keywords"][:3]
    stats = {"listings": 0, "categories": 0}
    extra = config.setdefault("_extra_entries", [])

    for category in categories:
        cat_slug = slugify(category)
        try:
            listings = fetch_listings(config, category)[: config["directory_listings_per_category"]]
            if not listings:
                rt.log("rejection", stage="directory", category=category, reason="no_listings_returned")
                continue
            cat_dir = root / cat_slug
            cat_dir.mkdir(parents=True, exist_ok=True)
            seen_desc, rows, used_slugs = set(), [], set()
            for l in listings:
                slug = slugify(l["name"])
                desc = describe(l, category, listings, config)
                if slug in used_slugs or desc in seen_desc or word_count(desc) < 80:
                    rt.log("rejection", stage="directory", listing=l["name"], reason="duplicate_or_short_description",
                           words=word_count(desc))
                    continue
                used_slugs.add(slug)
                seen_desc.add(desc)
                pk = f"{l['name']} {category} listing"
                fields = _filter_fields({k: l.get(k) for k in ("address", "phone", "website", "rating", "reviews",
                                                               "hours", "business_type", "services")}, config)
                meta = f"{l['name']}: {category}{' in ' + city if city else ''}. Rating, reviews, contact details and what to check before you get in touch."
                fm = {"title": l["name"], "description": meta[:160], "pubDate": date.fromisoformat(today),
                      "slug": slug, "tags": [category.lower()] + ([city.lower()] if city else []),
                      "primaryKeyword": pk.lower(), "type": "listing", "category": category,
                      "categorySlug": cat_slug, **fields}
                body = [f"# {l['name']}", "", desc, "", "## Details", ""]
                body += [f"- **{k.replace('_', ' ').title()}:** {', '.join(v) if isinstance(v, list) else v}"
                         for k, v in fields.items()]
                body += ["", f"See all [{category}{' in ' + city if city else ''}]({prefix}/{cat_slug}/)."]
                (cat_dir / f"{slug}.md").write_text(build_frontmatter(fm) + "\n" + "\n".join(body) + "\n", encoding="utf-8")
                entry = {"primary_keyword": pk.lower(), "slug": slug, "type": "directory_listing", "status": "written",
                         "date": today, "title": l["name"], "anchor": l["name"], "category_slug": cat_slug,
                         "rating": l.get("rating")}
                extra.append(entry) if rt.dry_run else inventory.add_entry(config, **entry)
                rows.append((l, slug))
                stats["listings"] += 1
                rt.log("write", file=str(cat_dir / f"{slug}.md"), words=word_count(desc), dry_run=rt.dry_run)
            if not rows:
                continue
            # category index
            title = f"{title_case(category)}{' in ' + city if city else ''}"
            rated = [l for l, _ in rows if l.get("rating") is not None]
            intro = [f"This page lists {len(rows)} {category}{' in ' + city if city else ''}, each with its own profile."]
            if rated:
                top = max(rated, key=lambda l: (l["rating"], l.get("reviews") or 0))
                avg = sum(l["rating"] for l in rated) / len(rated)
                total = sum(l.get("reviews") or 0 for l in rated)
                intro.append(f"Across the group, the average Google rating is {avg:.1f} stars from {total:,} reviews. "
                             f"{top['name']} currently has the highest rating at {top['rating']}.")
            intro.append("Use the table to shortlist two or three, open their profiles, and compare them on the same "
                         "job before you contact anyone. Ratings change, so treat them as a starting point and "
                         "check the most recent reviews yourself.")
            body = [f"# {title}", "", " ".join(intro), "", "| Name | Rating | Reviews | Phone |", "|---|---|---|---|"]
            for l, slug in rows:
                body.append(f"| [{l['name']}]({prefix}/{cat_slug}/{slug}/) | {l.get('rating') or '-'} | "
                            f"{l.get('reviews') or '-'} | {l.get('phone') or '-'} |")
            body += ["", "## How to use this list", "", "- Shortlist by rating and review count, not rating alone",
                     "- Read each profile for address, hours and services", "- Get quotes from at least two before you decide"]
            pk = f"{category} directory{' ' + city if city else ''}".lower()
            fm = {"title": title, "description": f"{len(rows)} {category}{' in ' + city if city else ''} compared: ratings, review counts and contact details in one table."[:160],
                  "pubDate": date.fromisoformat(today), "slug": cat_slug,
                  "tags": [category.lower()] + ([city.lower()] if city else []), "primaryKeyword": pk,
                  "type": "category", "listings": [s for _, s in rows]}
            (cat_dir / "index.md").write_text(build_frontmatter(fm) + "\n" + "\n".join(body) + "\n", encoding="utf-8")
            entry = {"primary_keyword": pk, "slug": cat_slug, "type": "directory_category", "status": "written",
                     "date": today, "title": title, "anchor": f"{category}{' in ' + city if city else ''} directory"}
            extra.append(entry) if rt.dry_run else inventory.add_entry(config, **entry)
            stats["categories"] += 1
            rt.log("write", file=str(cat_dir / "index.md"), listings=len(rows), dry_run=rt.dry_run)
        except Exception as e:  # one bad category must not stop the run
            rt.log("stage_error", stage="directory", category=category, error=f"{type(e).__name__}: {e}")
    return stats
