"""Stage 4: validation, file writes, publish scheduling, inventory import and topical map."""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import inventory
from config import get_runtime
from textutil import (body_word_count, find_banned, jaccard, slugify, split_frontmatter, tokens)

REQUIRED_FRONTMATTER = ["title", "description", "pubDate", "slug", "tags", "primaryKeyword"]


class ValidationError(Exception):
    pass


def validate_markdown(md: str, min_words: int = 0, required=REQUIRED_FRONTMATTER) -> list[str]:
    errors = []
    fm, body = split_frontmatter(md)
    if not fm:
        return ["missing or unparseable YAML frontmatter"]
    for k in required:
        if fm.get(k) in (None, "", []):
            errors.append(f"frontmatter key '{k}' missing or empty")
    if "tags" in fm and not isinstance(fm["tags"], list):
        errors.append("frontmatter 'tags' must be a list")
    wc = body_word_count(md)
    if wc < min_words:
        errors.append(f"word count {wc} below required {min_words}")
    if find_banned(md):
        errors.append(f"banned phrases present: {find_banned(md)}")
    if not body.strip():
        errors.append("empty body")
    return errors


COMPARISON_TYPES = ("alternatives", "review", "versus", "own_versus", "best", "switch", "export")
PATH_KEYS = COMPARISON_TYPES + ("comparison", "use_case", "article", "service_page", "listicle", "blog_post",
                                "product_page")


def page_paths(config) -> dict:
    """Validated `page_paths` with directories resolved: {key: {url_prefix, output_directory: Path}}."""
    from config import ConfigError
    raw = config.get("page_paths") or {}
    if not isinstance(raw, dict):
        raise ConfigError("page_paths must be a mapping")
    out = {}
    for k, v in raw.items():
        if k not in PATH_KEYS:
            raise ConfigError(f"page_paths: unknown key {k!r}; use {', '.join(PATH_KEYS)}")
        if not isinstance(v, dict) or not str(v.get("url_prefix") or "").startswith("/") or not v.get("output_directory"):
            raise ConfigError(f"page_paths.{k} needs url_prefix (starting with /) and output_directory")
        d = Path(v["output_directory"]).expanduser()
        if not d.is_absolute():
            d = (Path(__file__).resolve().parent / d).resolve()
        out[k] = {"url_prefix": v["url_prefix"].rstrip("/") or "/", "output_directory": d}
    return out


def destination_key(brief: dict, config) -> str | None:
    """Which page_paths entry a page goes to: its page type, then 'comparison', then its SERP format."""
    pp = page_paths(config)
    t = (brief.get("comparison") or {}).get("page_type")
    for k in ([t, "comparison" if t in COMPARISON_TYPES else None] if t else []) + [brief.get("format")]:
        if k and k in pp:
            return k
    return None


def destination(brief: dict, config: dict) -> tuple[Path, str, str | None]:
    """(output directory for this run, public URL prefix, page_paths key or None for the blog)."""
    key = destination_key(brief, config)
    if key is None:
        return config["_paths"]["content"], config["blog_url_prefix"].rstrip("/"), None
    pp = page_paths(config)[key]
    rt = get_runtime(config)
    d = (config["_paths"]["content"] / key) if rt.dry_run else pp["output_directory"]
    return d, pp["url_prefix"], key


def engine_dirs(config) -> list[tuple[Path, str]]:
    """Every folder the engine writes pages to, with its URL prefix (blog first)."""
    out = [(Path(config["_paths"]["output"]), config["blog_url_prefix"])]
    for v in page_paths(config).values():
        if (v["output_directory"], v["url_prefix"]) not in out:
            out.append((v["output_directory"], v["url_prefix"]))
    return out


def page_file(config, e: dict) -> Path:
    """Where an engine-written page lives (its own folder when it went to a page_paths entry)."""
    return Path(e["output_dir"]) / f'{e["slug"]}.md' if e.get("output_dir") else Path(config["_paths"]["output"]) / f'{e["slug"]}.md'


def is_imported(e: dict) -> bool:
    return e.get("source") == "existing" or bool(e.get("url_prefix") and not e.get("output_dir"))


def write_article(md: str, brief: dict, config: dict) -> Path:
    """Validate, write {slug}.md, then update inventory (skipped in dry-run)."""
    rt = get_runtime(config)
    errors = validate_markdown(md, brief["target_word_count"])
    if errors:
        raise ValidationError(f"{brief['slug']}.md failed validation: " + "; ".join(errors))
    out_dir, prefix, key = destination(brief, config)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{brief['slug']}.md"
    where = {"url_prefix": prefix, "output_dir": str(out_dir), "page_path": key} if key else {}
    brief["_url"] = f"https://{config['domain']}{prefix.rstrip('/')}/{brief['slug']}/"
    path.write_text(md, encoding="utf-8")
    fm, _ = split_frontmatter(md)
    if not rt.dry_run:
        inventory.add_entry(config, brief["primary_keyword"], brief["slug"], type="article", status="written",
                            date=str(fm["pubDate"]), title=fm["title"], intent=brief["intent"],
                            words=body_word_count(md), **{k: v for k, v in brief.get("metrics", {}).items()},
                            **brief.get("inventory_extra", {}), **where, **({"draft": True} if fm.get("draft") else {}))
        register_route(config, brief["slug"], draft=bool(fm.get("draft")), prefix=prefix)
    rt.log("write", file=str(path), words=body_word_count(md), dry_run=rt.dry_run)
    return path


def register_route(config: dict, slug: str, draft: bool = False, prefix: str | None = None) -> bool:
    """Add the new post to the site's route inventory (optional `route_inventory` config).

    For sites that require an explicit entry per URL before a page may build, e.g.
    {"path": "/blog/<slug>/", "kind": "blog", "publicationStatus": "published", "indexStatus": "index"}.
    Posts flagged draft are not registered. Returns True when an entry was added.
    """
    opts = config.get("route_inventory")
    if not opts or draft:
        return False
    return add_route(config, f'{(prefix or config["blog_url_prefix"]).rstrip("/")}/{slug}/', opts.get("kind", "blog"),
                     opts.get("publication_status", "published"), opts.get("index_status", "index"))


def add_route(config: dict, route_path: str, kind: str, publication_status: str, index_status: str) -> bool:
    """Append one route entry to the site's route inventory file. False when it is already there."""
    opts = config["route_inventory"]
    rt = get_runtime(config)
    file = Path(opts["file"]).expanduser()
    if not file.is_absolute():
        file = (Path(config["config_path"]).resolve().parent / file).resolve()
    text = file.read_text(encoding="utf-8")
    data = json.loads(text)
    if any(r.get("path") == route_path for r in data["routes"]):
        return False
    entry = {"path": route_path, "kind": kind, "publicationStatus": publication_status, "indexStatus": index_status}
    line = "{ " + ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in entry.items()) + " }"
    m = re.search(r"\}(\s*\n\s*\]\s*\}\s*)$", text)
    if m:  # keep the file's one-route-per-line layout
        text = text[:m.start()] + "},\n    " + line + m.group(1)
    else:
        data["routes"].append(entry)
        text = json.dumps(data, indent=2) + "\n"
    json.loads(text)  # never write a file that does not parse
    file.write_text(text, encoding="utf-8")
    rt.log("stage", stage="route_registered", path=route_path, file=str(file))
    return True


def schedule_dates(config: dict, n: int) -> list[str]:
    """Publish dates for n new pages on the site's publishing days (see pacing.py)."""
    import pacing
    return pacing.next_dates(config, n)


def import_existing_content(config: dict) -> int:
    """Seed inventory from Markdown already on the site (optional `existing_content` config list).

    Each item: {directory: path, url_prefix: /guides}. Titles become primary keywords so new
    keywords that would cannibalise an existing page are rejected in Stage 1.
    """
    rt = get_runtime(config)
    added = 0
    known = {e["slug"] for e in inventory.load(config)}
    for src in config.get("existing_content") or []:
        d = Path(src["directory"]).expanduser()
        if not d.is_absolute():
            d = (Path(config["config_path"]).resolve().parent / d).resolve()
        if not d.is_dir():
            rt.log("error", stage="import_existing", error=f"directory not found: {d}")
            continue
        for f in sorted(list(d.glob("*.md")) + list(d.glob("*.mdx"))):
            fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
            slug = f.stem
            if slug in known or rt.dry_run:
                continue
            # Pages the site does not build must not enter inventory, or articles would link to a 404.
            status = (fm.get("internal") or {}).get("status") if isinstance(fm.get("internal"), dict) else None
            if fm.get("draft") is True or (status and status != "published"):
                rt.log("stage", stage="import_existing_skip", slug=slug, reason="not published")
                continue
            kw = fm.get("primaryKeyword") or slug.replace("-", " ")
            inventory.add_entry(config, kw, slug, type="article", status="written",
                                date=str(fm.get("pubDate") or fm.get("publishedDate") or fm.get("published") or date.today().isoformat())[:10],
                                title=fm.get("title") or kw, source="existing",
                                url_prefix=src.get("url_prefix", config["blog_url_prefix"]))
            known.add(slug)
            added += 1
    if added:
        rt.log("stage", stage="import_existing", added=added)
    return added


def write_topical_map(config: dict) -> Path:
    """Cluster inventory pieces by shared terms; pillar = highest-volume keyword in the cluster."""
    rt = get_runtime(config)
    entries = inventory.load(config)
    articles = [e for e in entries if e.get("type") == "article"]
    clusters: list[dict] = []
    by_competitor: dict[str, dict] = {}
    for e in sorted(articles, key=lambda x: -(x.get("volume") or 0)):
        if e.get("comparison_cluster"):  # comparison pages cluster by their first competitor
            key = e["comparison_cluster"][0]
            if key not in by_competitor:
                by_competitor[key] = {"members": []}
                clusters.append(by_competitor[key])
            by_competitor[key]["members"].append(e)
            continue
        et = set(tokens(e["primary_keyword"]))
        best, score = None, 0.0
        for c in clusters:
            if any(m.get("comparison_cluster") for m in c["members"]):
                continue
            j = max(jaccard(et, set(tokens(m["primary_keyword"]))) for m in c["members"])
            if j > score:
                best, score = c, j
        if best is not None and score >= 0.34:
            best["members"].append(e)
        else:
            clusters.append({"members": [e]})
    blog = config["blog_url_prefix"].rstrip("/")
    out = {"site": config["site_name"], "domain": config["domain"], "generated": date.today().isoformat(),
           "clusters": [], "directory": []}
    for c in clusters:
        pillar = c["members"][0]
        out["clusters"].append({
            "cluster": pillar["primary_keyword"], "slug": slugify(pillar["primary_keyword"]),
            "pillar": pillar["primary_keyword"],
            "total_volume": sum(m.get("volume") or 0 for m in c["members"]),
            "pieces": [{"primary_keyword": m["primary_keyword"], "slug": m["slug"], "status": m["status"],
                        "date": m.get("date"), "volume": m.get("volume"), "intent": m.get("intent"),
                        "url": f"{m.get('url_prefix', blog).rstrip('/')}/{m['slug']}/"} for m in c["members"]]})
    for e in entries:
        if e.get("type") == "directory_category":
            out["directory"].append({"category": e.get("title"), "slug": e["slug"],
                                     "listings": [x["slug"] for x in entries if x.get("type") == "directory_listing"
                                                  and x.get("category_slug") == e["slug"]]})
    path = config["_paths"]["topical_map"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    rt.log("write", file=str(path), clusters=len(out["clusters"]))
    return path
