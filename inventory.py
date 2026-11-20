"""Per-site inventory.json: every planned or written piece, with duplicate detection."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from config import DEFAULTS, ROOT
from textutil import normalize_keyword, similarity, slugify, split_frontmatter

ARTICLE = "article"


def _path(site) -> Path:
    if isinstance(site, dict):
        return Path(site["_paths"]["inventory"])
    return ROOT / "data" / slugify(str(site)) / "inventory.json"


def _threshold(site) -> float:
    if isinstance(site, dict):
        return site.get("duplicate_similarity_threshold", DEFAULTS["duplicate_similarity_threshold"])
    return DEFAULTS["duplicate_similarity_threshold"]


def held(config: dict, e: dict) -> bool:
    """True while an entry written with `draft: true` is still a draft on disk (the site does not build it,
    so nothing may link to it). Once the owner removes the flag, the page counts as live."""
    if not e.get("draft") or e.get("url_prefix"):
        return False
    f = Path(config["_paths"]["content"]) / f'{e["slug"]}.md'
    if not f.exists():
        return True
    fm, _ = split_frontmatter(f.read_text(encoding="utf-8"))
    return fm.get("draft") is True


def load(site) -> list[dict]:
    p = _path(site)
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8")).get("entries", [])


def save(site, entries: list[dict]):
    p = _path(site)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"entries": entries}, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def find(keyword: str, site):
    """Entry whose normalised primary_keyword exactly matches, else None."""
    norm = normalize_keyword(keyword)
    for e in load(site):
        if normalize_keyword(e["primary_keyword"]) == norm:
            return e
    return None


def find_overlap(keyword: str, site, entries=None):
    """Return (entry, similarity) for the first overlapping entry, else (None, 0).

    Overlap = exact match after normalisation (any entry type), or normalised
    Levenshtein similarity >= threshold (default 0.7) against article entries.
    """
    norm = normalize_keyword(keyword)
    thr = _threshold(site)
    for e in (load(site) if entries is None else entries):
        if normalize_keyword(e["primary_keyword"]) == norm:
            return e, 1.0
        if e.get("type", ARTICLE) == ARTICLE:
            sim = similarity(keyword, e["primary_keyword"])
            if sim >= thr:
                return e, round(sim, 3)
    return None, 0.0


def is_duplicate(keyword: str, site) -> bool:
    return find_overlap(keyword, site)[0] is not None


def add_entry(site, primary_keyword: str, slug: str, type: str = ARTICLE, status: str = "planned",
              date: str | None = None, **extra) -> dict:
    """Insert or update (matched on normalised primary_keyword). Never creates duplicates."""
    entries = load(site)
    norm = normalize_keyword(primary_keyword)
    entry = {"primary_keyword": primary_keyword, "slug": slug, "type": type, "status": status,
             "date": date or _today(), **extra}
    for i, e in enumerate(entries):
        if normalize_keyword(e["primary_keyword"]) == norm:
            entries[i] = {**e, **entry}
            save(site, entries)
            return entries[i]
    entries.append(entry)
    save(site, entries)
    return entry


def _today() -> str:
    return date.today().isoformat()
