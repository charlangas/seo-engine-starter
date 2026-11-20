"""Keep published pages fresh: re-research, revise and re-date pages the engine wrote.

  python main.py refresh --config configs/site.yaml [--dry-run] [--slug a-slug ...] [--limit N]

Which pages are due (in this order):
1. Pages the last live `gsc-check` lists under "Pages that slipped", once they are `min_age_days` old.
2. Pages queued in data/<site>/refresh_queue.json (a first-party fact they quote changed; see evidence.py).
3. Striking distance: pages whose queries rank 8-20 with real impressions. The page is revised with those
   queries as the focus (a section or heading that answers each one directly).
4. Low click-through on page one, and engine pages the health check found with a duplicate title: only the
   title and meta description are rewritten ("retitle"), 40-60 characters, keyword first, shaped like the
   titles that rank for that query. The old title, the query and its CTR are kept in the inventory as a title
   test; the weekly report says after 28 days whether the click-through improved.
5. Pages whose last update (`updatedDate`, else `pubDate`) is older than `max_age_days` for their kind:
   comparison pages 30 days, other articles 90.
Only pages the engine wrote and the site has published are refreshed: imported pages, drafts, future-dated
pages and pages with `refresh: false` in their frontmatter are left alone.

How a page is refreshed: the SERP brief is rebuilt (comparison pages also re-fetch pricing pages, Reddit and
videos), and the writer revises the *existing* page against it instead of drafting from scratch. The usual
editor loop and rule-based critique are the gate, plus one refresh rule: every image, embed and quote a
person added to the page must still be there. `pubDate`, the slug and any frontmatter the engine does not own
are kept; `updatedDate` becomes today. A page whose text comes back unchanged is not re-dated.
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import date
from pathlib import Path

import comparisons
import inventory
import output
import publish
import serp
import writer
from config import ConfigError, get_runtime, load_config
from textutil import body_word_count, build_frontmatter, split_frontmatter

DEFAULTS = {
    "max_age_days": {"comparison": 30, "article": 90},
    "max_per_run": 2,
    "include_declines": True,   # refresh pages gsc-check reports as slipped, before the age rule
    "min_age_days": 7,          # never refresh a page more often than this, whatever the reason
    "include_striking": True,   # revise pages whose queries rank in striking distance
    "striking_min_position": 8, "striking_max_position": 20,
    "include_low_ctr": True,    # rewrite title + description of page-one pages nobody clicks
    "retitle_cooldown_days": 28,  # one title test at a time per page, long enough to measure it
    "max_retitles_per_run": 3,  # title rewrites are cheap (one SERP + one short LLM call) and counted apart
}

_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)")
_TAG_SRC = re.compile(r"<(?:iframe|img|video|source|embed)\b[^>]*\bsrc=[\"']([^\"']+)", re.I)
_COMPONENT = re.compile(r"^\s*<([A-Z][A-Za-z0-9]*)\b[^\n]*$", re.M)   # MDX components such as <YouTube id="..."/>


def settings(config) -> dict:
    raw = config.get("refresh") or {}
    if not isinstance(raw, dict):
        raise ConfigError("refresh must be a mapping")
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k == "max_age_days" and isinstance(v, dict):
            s[k].update(v)
        elif k in s:
            s[k] = v
        else:
            raise ConfigError(f"refresh: unknown setting '{k}'; use {sorted(DEFAULTS)}")
    for kind, days in s["max_age_days"].items():
        if not isinstance(days, int) or days < 1:
            raise ConfigError(f"refresh.max_age_days.{kind} must be a positive integer")
    return s


# ---------- what must survive a refresh ----------

def keepers(body: str) -> list[dict]:
    """Things a person may have added that a rewrite must not lose: images, embeds, components, quotes."""
    out, seen = [], set()

    def add(kind, needle, label):
        if needle not in seen:
            seen.add(needle)
            out.append({"kind": kind, "needle": needle, "label": label})

    for url in _IMAGE.findall(body):
        add("image", url, f"image {url}")
    for url in _TAG_SRC.findall(body):
        add("embed", url, f"embed {url}")
    for m in _COMPONENT.finditer(body):
        add("component", m.group(0).strip(), f"component {m.group(0).strip()[:80]}")
    for block in re.findall(r"(?:^>[^\n]*\n?)+", body, flags=re.M):
        text = _squash(re.sub(r"^>\s?", "", block, flags=re.M))
        if len(text.split()) >= 6:  # a real quote, not a one-word callout
            add("quote", text, f'quote "{text[:70]}"')
    return out


def _squash(s: str) -> str:
    return " ".join(re.sub(r"[*_`]", "", s).split())


def missing_keepers(body: str, keep: list[dict]) -> list[dict]:
    flat = _squash(re.sub(r"^>\s?", "", body, flags=re.M))
    return [k for k in keep if (k["needle"] not in body if k["kind"] != "quote" else k["needle"] not in flat)]


def critique(article: dict, brief: dict, fix) -> None:
    """Refresh rule, called from writer.critique for refresh briefs."""
    r = brief.get("refresh")
    if not r:
        return
    for k in missing_keepers(article["body"], r["keep"]):
        fix("refresh_dropped_" + k["kind"], f'The refreshed article dropped the {k["label"]} that the published page has. '
                                            f"Put it back unchanged, in the section it belongs to.")


def prompt_block(brief: dict) -> str:
    r = brief.get("refresh")
    if not r:
        return ""
    lines = ["", "THIS IS A REFRESH OF A PUBLISHED PAGE (these rules override any general rule above):",
             "- Revise the current article; do not start over. Keep sections, examples and wording that are still accurate.",
             "- Bring every fact, price and plan name in line with the research above. Remove claims it no longer supports.",
             "- Add any required section the page lacks; answer reader questions that are new in the search results.",
             "- Keep every outbound link unless the research shows the source is gone.",
             "- Never mention that the page was updated, refreshed or revised; the site shows the date."]
    if r.get("focus"):
        lines.append("- This page ranks just off page one for these searches. Answer each directly: give it a heading "
                     "(H2 or H3) or the first sentence of the section that answers it, with the specific answer right after:")
        lines += [f'  - "{q}"' for q in r["focus"]]
    if r.get("fact"):
        f = r["fact"]
        lines.append(f'- A first-party fact this page quotes has changed ({f.get("item", "")}): replace '
                     f'{", ".join(f.get("old") or [])} with the current figure(s) {", ".join(f.get("new") or []) or "from the evidence above"} '
                     "everywhere, and adjust any sentence that depends on it.")
    if r["keep"]:
        lines.append("- Keep each of these exactly as written, in the section it belongs to:")
        lines += [f'  - {k["label"]}' for k in r["keep"]]
    return "\n".join(lines)


# ---------- which pages are due ----------

def _as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _declines(config) -> dict:
    latest = config["_paths"]["data"] / "gsc" / "latest.json"
    if not latest.exists():
        return {}
    report = json.loads(latest.read_text(encoding="utf-8"))
    return {d["page"]: d for d in report.get("declines") or []}


def _gsc_signals(config, s: dict) -> tuple[dict, dict]:
    """(striking: page -> [query rows], low_ctr: page -> best row) from the last live gsc-check."""
    latest = config["_paths"]["data"] / "gsc" / "latest.json"
    if not latest.exists():
        return {}, {}
    rep = json.loads(latest.read_text(encoding="utf-8"))
    if not rep.get("has_page_query_pairs"):
        return {}, {}
    striking: dict[str, list] = {}
    if s["include_striking"]:
        for r in rep.get("striking_distance") or []:
            if r.get("page") and s["striking_min_position"] <= r["position"] <= s["striking_max_position"]:
                striking.setdefault(r["page"], []).append(r)
    low: dict[str, dict] = {}
    if s["include_low_ctr"]:
        for r in rep.get("low_ctr") or []:
            if r.get("page") and r.get("query") and (r["page"] not in low or r["impressions"] > low[r["page"]]["impressions"]):
                low[r["page"]] = r
    return striking, low


def queue_file(config) -> Path:
    return config["_paths"]["data"] / "refresh_queue.json"


def load_queue(config) -> list[dict]:
    f = queue_file(config)
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []
    except ValueError:
        return []


def enqueue(config, slug: str, reason: str, **detail) -> bool:
    """Ask the next refresh run to revise this page (fact changed, duplicate title...). False if already queued."""
    q = load_queue(config)
    if any(x["slug"] == slug and x["reason"] == reason for x in q):
        return False
    q.append({"slug": slug, "reason": reason, "queued": date.today().isoformat(), **detail})
    queue_file(config).parent.mkdir(parents=True, exist_ok=True)
    queue_file(config).write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def dequeue(config, slug: str) -> None:
    q = [x for x in load_queue(config) if x["slug"] != slug]
    if queue_file(config).exists():
        queue_file(config).write_text(json.dumps(q, indent=2, ensure_ascii=False), encoding="utf-8")


def page_url(config, e: dict) -> str:
    return f'{(e.get("url_prefix") or config["blog_url_prefix"]).rstrip("/")}/{e["slug"]}/'


def due(config, today: date | None = None, slugs: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """(due pages in refresh order, pages passed over with the reason). Reads only local files."""
    today = today or date.today()
    s = settings(config)
    declines = _declines(config) if s["include_declines"] else {}
    striking, low_ctr = _gsc_signals(config, s)
    queued = {}
    for x in load_queue(config):
        queued.setdefault(x["slug"], x)
    picked, passed = [], []
    for e in inventory.load(config):
        if e.get("type") != "article" or e.get("status") != "written":
            continue
        if slugs and e["slug"] not in slugs:
            continue

        def skip(reason):
            passed.append({"slug": e["slug"], "reason": reason})

        if output.is_imported(e):
            skip("not_written_by_engine")
            continue
        path = output.page_file(config, e)
        if not path.exists():
            skip("file_missing")
            continue
        fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        pub = _as_date(fm.get("pubDate"))
        if fm.get("refresh") is False:
            skip("refresh_false_in_frontmatter")
            continue
        if fm.get("draft") is True:
            skip("draft_not_published")
            continue
        if not pub or pub > today:
            skip("not_published_yet")
            continue
        last = _as_date(fm.get("updatedDate")) or pub
        age = (today - last).days
        kind = "comparison" if e.get("page_type") else "article"
        url = page_url(config, e)
        cand = {"entry": e, "path": path, "age_days": age, "last_updated": last.isoformat(), "kind": kind, "mode": "revise"}
        tests = e.get("title_tests") or []
        last_test = _as_date(tests[-1]["date"]) if tests else None
        can_retitle = not last_test or (today - last_test).days >= s["retitle_cooldown_days"]
        q = queued.get(e["slug"])
        if age < s["min_age_days"] and not slugs:
            if q and q["reason"] == "past_year":
                pass  # an out-of-date year is wrong now, however recently the page was updated
            elif can_retitle and not slugs and (url in low_ctr or queued.get(e["slug"], {}).get("reason") == "duplicate_title"):
                pass  # a title rewrite does not touch the body, so the body's age does not matter
            else:
                skip("updated_recently")
                continue
        if url in declines and age >= s["min_age_days"]:
            d = declines[url]
            picked.append({**cand, "reason": "search_console_decline", "decline": d,
                           "_order": (0, d["impressions_now"] - d["impressions_before"])})
        elif q and q["reason"] != "duplicate_title" and (age >= s["min_age_days"] or q["reason"] == "past_year"):
            picked.append({**cand, "reason": q["reason"], "queued": q, "_order": (1, -age)})
        elif url in striking and age >= s["min_age_days"]:
            rows = sorted(striking[url], key=lambda r: -r["impressions"])
            picked.append({**cand, "reason": "striking_distance", "focus": rows[:5],
                           "_order": (2, -sum(r["impressions"] for r in rows))})
        elif url in low_ctr and can_retitle and not slugs:
            r = low_ctr[url]
            picked.append({**cand, "reason": "low_ctr", "mode": "retitle", "query": r["query"], "gsc": r,
                           "_order": (3, -r["impressions"])})
        elif q and q["reason"] == "duplicate_title" and can_retitle and not slugs:
            picked.append({**cand, "reason": "duplicate_title", "mode": "retitle", "queued": q,
                           "query": e["primary_keyword"], "_order": (3, 0)})
        elif slugs:
            picked.append({**cand, "reason": "requested", "_order": (4, -age)})
        elif age >= s["max_age_days"].get(kind, DEFAULTS["max_age_days"]["article"]):
            picked.append({**cand, "reason": "age", "_order": (4, -age)})
        else:
            skip(f"fresh ({age} of {s['max_age_days'].get(kind)} days)")
    picked.sort(key=lambda c: c.pop("_order"))
    return picked, passed


# ---------- retitle: title + meta description only ----------

def serp_titles(config, query: str) -> list[str]:
    try:
        return [r["title"] for r in serp.fetch_serp(query, config)["organic"] if r.get("title")][:10]
    except Exception as e:  # noqa: BLE001 - patterns are a hint; the rules still apply without them
        get_runtime(config).log("stage_error", stage="retitle", step="serp", query=query, error=str(e)[:200])
        return []


def title_ok(title: str, query: str) -> list[str]:
    from textutil import contains_keyword, tokens
    problems = []
    if not 40 <= len(title) <= 60:
        problems.append(f"title is {len(title)} characters; it must be 40-60")
    if not contains_keyword(title, query):
        problems.append(f'title must contain "{query}"')
    else:
        first = (tokens(query) or [""])[0]
        half = title[: max(len(title) // 2 + len(first), 30)].lower()
        if first and first not in " ".join(tokens(half)):
            problems.append("put the query in the first half of the title")
    return problems


def desc_ok(desc: str, query: str) -> list[str]:
    from textutil import contains_keyword
    problems = []
    if not 120 <= len(desc) <= 160:
        problems.append(f"description is {len(desc)} characters; it must be 120-160")
    if not contains_keyword(desc, query):
        problems.append(f'description must contain "{query}"')
    return problems


def retitle_page(config, cand: dict, today: date | None = None) -> dict:
    """Rewrite only the title and meta description for click-through. Body and dates are untouched."""
    import slop
    today = today or date.today()
    rt = get_runtime(config)
    e, path = cand["entry"], cand["path"]
    old_md = path.read_text(encoding="utf-8")
    fm, body = split_frontmatter(old_md)
    query = cand.get("query") or e["primary_keyword"]
    old_title, old_desc = str(fm.get("title") or ""), str(fm.get("description") or "")
    rt.log("stage", stage="retitle", slug=e["slug"], reason=cand["reason"], query=query)
    titles = serp_titles(config, query)
    others = []
    if cand["reason"] == "duplicate_title":
        others = (cand.get("queued") or {}).get("duplicates") or []
    new = writer.rewrite_meta(config, {"title": old_title, "description": old_desc, "body": body}, query,
                              e["primary_keyword"], titles, avoid=others + [old_title])
    title, desc = slop.scrub(new["title"]).strip(), slop.scrub(new["description"]).strip()
    problems = title_ok(title, query) + desc_ok(desc, query)
    record = {"slug": e["slug"], "keyword": e["primary_keyword"], "reason": cand["reason"], "mode": "retitle",
              "query": query, "previous_update": cand["last_updated"], "file": str(path),
              "old_title": old_title, "new_title": title, "old_description": old_desc, "new_description": desc}
    if problems:
        raise writer.WriterError(f"retitle for {e['slug']} failed the title rules: " + "; ".join(problems))
    if title == old_title and desc == old_desc:
        return {**record, "changed": False}
    fm["title"], fm["description"] = title, desc
    out_md = build_frontmatter(fm) + "\n" + body.strip() + "\n"
    out_dir = (Path(config["_paths"]["content"]) / e["page_path"]) if (rt.dry_run and e.get("page_path")) else \
        (Path(e["output_dir"]) if e.get("output_dir") else Path(config["_paths"]["content"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'{e["slug"]}.md'
    out_path.write_text(out_md, encoding="utf-8")
    record.update(changed=True, file=str(out_path), updated=today.isoformat())
    if not rt.dry_run:
        g = cand.get("gsc") or {}
        test = {"date": today.isoformat(), "query": query, "reason": cand["reason"], "old_title": old_title,
                "new_title": title, "ctr_before": g.get("ctr"), "position_before": g.get("position"),
                "impressions_before": g.get("impressions")}
        inventory.add_entry(config, e["primary_keyword"], e["slug"], type="article", status="written", date=e["date"],
                            title=title, title_tests=(e.get("title_tests") or []) + [test])
        if cand["reason"] == "duplicate_title":
            dequeue(config, e["slug"])
    rt.log("write", file=str(out_path), stage="retitle_done", old=old_title, new=title)
    return record


def title_test_results(config, today: date | None = None, min_days: int = 28) -> list[dict]:
    """Title tests at least `min_days` old, with the query's CTR before and now (from the last live gsc-check)."""
    today = today or date.today()
    latest = config["_paths"]["data"] / "gsc" / "latest.json"
    rows = {}
    if latest.exists():
        rep = json.loads(latest.read_text(encoding="utf-8"))
        for r in (rep.get("low_ctr") or []) + (rep.get("striking_distance") or []):
            if r.get("page") and r.get("query"):
                rows[(r["page"], r["query"])] = r
        rows.update({(r["page"], r["query"]): r for r in rep.get("query_owners") or [] if r.get("page")})
    out = []
    for e in inventory.load(config):
        for t in e.get("title_tests") or []:
            d = _as_date(t["date"])
            if not d or (today - d).days < min_days or t.get("ctr_before") is None:
                continue
            now = rows.get((page_url(config, e), t["query"]))
            if not now or now.get("ctr") is None:
                continue
            verdict = "better" if now["ctr"] > t["ctr_before"] * 1.1 else "worse" if now["ctr"] < t["ctr_before"] * 0.9 else "same"
            out.append({"slug": e["slug"], "query": t["query"], "old_title": t["old_title"], "new_title": t["new_title"],
                        "ctr_before": t["ctr_before"], "ctr_now": now["ctr"], "verdict": verdict, "date": t["date"]})
    return out


# ---------- refreshing one page ----------

def _h2s(body: str) -> list[str]:
    return [h.strip() for h in re.findall(r"^##\s+(.+)$", body, flags=re.M)]


def change_note(old_body: str, new_body: str) -> str:
    """One visible line on what this update changed (answer engines favour pages that say so)."""
    import lang
    es = lang.is_es()
    old_p, new_p = comparisons._price_set(old_body), comparisons._price_set(new_body)
    added = [h for h in _h2s(new_body) if h not in _h2s(old_body)]
    parts = []
    if old_p != new_p:
        parts.append("precios actualizados" if es else "prices updated")
    elif old_p:
        parts.append("precios verificados" if es else "prices re-checked")
    if added:
        parts.append(("nuevas secciones: " if es else "new sections: ") + "; ".join(added[:3]))
    parts.append("datos y ejemplos revisados" if es else "facts and examples reviewed")
    text = ", ".join(parts)
    return text[0].upper() + text[1:] + "."


def refresh_page(config, cand: dict, today: date | None = None) -> dict:
    """Rebuild the brief, revise the page, write it. Returns a change record. Raises on failure."""
    if cand.get("mode") == "retitle":
        return retitle_page(config, cand, today)
    today = today or date.today()
    rt = get_runtime(config)
    e, path = cand["entry"], cand["path"]
    old_md = path.read_text(encoding="utf-8")
    old_fm, old_body = split_frontmatter(old_md)
    rt.log("stage", stage="refresh", slug=e["slug"], reason=cand["reason"], age_days=cand["age_days"])

    item = {"keyword": e["primary_keyword"], "intent": e.get("intent") or "commercial",
            "volume": e.get("volume"), "kd": e.get("kd"), "score": e.get("score"), "related": [],
            "serp_cluster": e.get("serp_cluster") or []}
    if e.get("page_type"):
        item.update(page_type=e["page_type"], competitors=e.get("competitors") or [], use_case=e.get("use_case"))
    brief = serp.build_brief(item, config)
    brief["slug"] = e["slug"]
    if e.get("page_type"):
        comparisons.augment_brief(brief, item, config)
    own = f'/{e["slug"]}/'
    brief["internal_link_targets"] = [t for t in brief["internal_link_targets"] if not t["url"].endswith(own)]
    brief["pubDate"] = str(_as_date(old_fm.get("pubDate")))
    keep = keepers(old_body)
    brief["refresh"] = {"keep": keep, "reason": cand["reason"], "previous_update": cand["last_updated"],
                        "focus": [r["query"] for r in cand.get("focus") or []],
                        "fact": (cand.get("queued") or {}) if cand["reason"] == "fact_changed" else None}

    base = {"title": str(old_fm.get("title") or ""), "description": str(old_fm.get("description") or ""), "body": old_body}
    md = writer.generate_article(brief, config, base=base)
    new_fm, new_body = split_frontmatter(md)

    record = {"slug": e["slug"], "keyword": e["primary_keyword"], "reason": cand["reason"],
              "previous_update": cand["last_updated"], "file": str(path)}
    if _squash(new_body) == _squash(old_body):
        rt.log("stage", stage="refresh_unchanged", slug=e["slug"])
        return {**record, "changed": False}

    fm = dict(old_fm)            # keep frontmatter the engine does not own (hero images, categories, ...)
    fm.update(new_fm)
    fm["slug"] = old_fm.get("slug") or e["slug"]
    fm["pubDate"] = _as_date(old_fm["pubDate"])
    fm["updatedDate"] = today
    fm["changeNote"] = change_note(old_body, new_body)
    if "draft" in old_fm:
        fm["draft"] = old_fm["draft"]
    else:
        fm.pop("draft", None)  # a published page stays published
    out_md = build_frontmatter(fm) + "\n" + new_body.strip() + "\n"
    errors = output.validate_markdown(out_md, brief["target_word_count"])
    if errors:
        raise output.ValidationError(f"{e['slug']}.md failed validation: " + "; ".join(errors))

    out_dir = (Path(config["_paths"]["content"]) / e["page_path"]) if (rt.dry_run and e.get("page_path")) else \
        (Path(e["output_dir"]) if e.get("output_dir") else Path(config["_paths"]["content"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'{e["slug"]}.md'
    out_path.write_text(out_md, encoding="utf-8")

    old_prices, new_prices = comparisons._price_set(old_body), comparisons._price_set(new_body)
    old_h, new_h = _h2s(old_body), _h2s(new_body)
    record.update(
        changed=True, file=str(out_path), updated=today.isoformat(),
        words_before=body_word_count(old_md), words_after=body_word_count(out_md),
        text_kept=round(comparisons.overlap(old_body, new_body), 2),
        prices_removed=sorted(old_prices - new_prices), prices_added=sorted(new_prices - old_prices),
        sections_removed=[h for h in old_h if h not in new_h], sections_added=[h for h in new_h if h not in old_h],
        kept_items=len(keep), title_changed=old_fm.get("title") != fm.get("title"))
    if cand.get("decline"):
        record["decline"] = cand["decline"]
    if cand.get("focus"):
        record["focus"] = [r["query"] for r in cand["focus"]]
    if not rt.dry_run:
        if cand.get("queued"):
            dequeue(config, e["slug"])
        inventory.add_entry(config, e["primary_keyword"], e["slug"], type="article", status="written", date=e["date"],
                            updated=today.isoformat(), refresh_count=int(e.get("refresh_count") or 0) + 1,
                            words=record["words_after"], title=fm["title"],
                            **({"last_verified": brief["inventory_extra"]["last_verified"]}
                               if brief.get("inventory_extra", {}).get("last_verified") else {}))
    rt.log("write", file=str(out_path), stage="refresh_done", **{k: v for k, v in record.items() if k != "file"})
    return record


# ---------- run ----------

def render(summary: dict) -> str:
    lines = [f"# Refresh: {summary['site']} ({summary['date']}{', dry run' if summary['dry_run'] else ''})", ""]
    for r in summary["refreshed"]:
        lines += [f"## {r['keyword']} (`{r['slug']}`)", "",
                  f"- Why: {r['reason']}" + (f" (impressions {r['decline']['impressions_before']} → "
                                             f"{r['decline']['impressions_now']}, position {r['decline']['position_before']} → "
                                             f"{r['decline']['position_now']})" if r.get("decline") else "")
                  + f"; last updated {r['previous_update']}"]
        if r.get("mode") == "retitle":
            lines += [f'- Query: "{r["query"]}"', f'- Title: "{r["old_title"]}" → "{r["new_title"]}"',
                      f'- Description: "{r["new_description"]}"', "- Body and dates unchanged.", ""]
            continue
        if r.get("focus"):
            lines.append("- Focus queries: " + "; ".join(r["focus"]))
        if not r["changed"]:
            lines += ["- No change: the revision came back identical, so the date was not moved.", ""]
            continue
        lines += [f"- Words: {r['words_before']} → {r['words_after']}; {int(r['text_kept'] * 100)}% of the old text kept",
                  f"- Prices removed: {', '.join('$' + x for x in r['prices_removed']) or 'none'}; "
                  f"added: {', '.join('$' + x for x in r['prices_added']) or 'none'}",
                  f"- Sections added: {'; '.join(r['sections_added']) or 'none'}",
                  f"- Sections removed: {'; '.join(r['sections_removed']) or 'none'}",
                  f"- Images, embeds and quotes kept: {r['kept_items']}"
                  + ("; the title changed" if r["title_changed"] else ""), ""]
    for f in summary["failed"]:
        lines += [f"- FAILED `{f['slug']}`: {f['error'][:300]}"]
    if not summary["refreshed"] and not summary["failed"]:
        lines.append("Nothing was due.")
    if summary["not_due"]:
        lines += ["", "<details><summary>Pages not due</summary>", ""]
        lines += [f"- `{p['slug']}`: {p['reason']}" for p in summary["not_due"]]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def run_refresh(config_path, dry_run: bool = False, slugs: list[str] | None = None, limit: int | None = None,
                today: date | None = None) -> dict:
    config = load_config(config_path, dry_run=dry_run)
    rt = config["_runtime"]
    rt.get_secrets()
    today = today or date.today()
    s = settings(config)
    summary = {"site": config["site_name"], "dry_run": dry_run, "date": today.isoformat(), "action": "refresh",
               "refreshed": [], "written": [], "failed": [], "not_due": []}
    picked, summary["not_due"] = due(config, today, slugs)  # local files only: costs nothing
    if not picked:
        return summary
    if writer.pick_backend(config).name == "template" and any(c.get("mode") != "retitle" for c in picked):
        summary["failed"].append({"slug": "(all)", "error": "refreshing needs an LLM writer backend (OPENROUTER_API_KEY "
                                                            "or ANTHROPIC_API_KEY); the template backend would erase the page's prose"})
        return summary
    if not dry_run:
        try:
            publish.prepare(config)  # git pull: the pages are read below, after the checkout is current
        except publish.PublishError as e:
            summary["failed"].append({"slug": "(site checkout)", "error": str(e)})
            return summary
        picked, summary["not_due"] = due(config, today, slugs)
    n_revise = limit if limit is not None else s["max_per_run"]
    revise = [c for c in picked if c.get("mode") != "retitle"][:n_revise]
    retitle = [c for c in picked if c.get("mode") == "retitle"][: s["max_retitles_per_run"]]
    chosen = revise + retitle
    for cand in chosen:
        try:
            r = refresh_page(config, cand, today)
            summary["refreshed"].append(r)
            if r["changed"]:
                summary["written"].append(r["file"])
        except Exception as e:
            summary["failed"].append({"slug": cand["entry"]["slug"], "error": f"{type(e).__name__}: {e}"})
            rt.log("stage_error", stage="refresh", slug=cand["entry"]["slug"], error=f"{type(e).__name__}: {e}",
                   traceback=traceback.format_exc())
    summary["waiting"] = [c["entry"]["slug"] for c in picked if c not in chosen]
    if summary["refreshed"] and all(r.get("mode") == "retitle" for r in summary["refreshed"]):
        summary["action"] = "retitle"

    out = Path(config["_paths"]["work"]) / "refresh"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{today.isoformat()}.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str),
                                                   encoding="utf-8")
    (out / f"{today.isoformat()}.md").write_text(render(summary), encoding="utf-8")
    summary["report"] = str(out / f"{today.isoformat()}.md")

    if summary["written"] and not dry_run and config.get("publish"):
        try:
            summary["published"] = publish.open_pr(config, summary)
        except publish.PublishError as e:
            summary["publish_error"] = str(e)
            rt.log("stage_error", stage="publish", error=str(e))
    summary["api_calls"] = dict(rt.calls)
    summary["log"] = str(rt.log_path)
    rt.log("summary", stage="refresh", **{k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()
                                          if k in ("refreshed", "written", "failed", "waiting")})
    return summary


def print_summary(s: dict) -> None:
    print(f"\n=== refresh: {s['site']} ({'dry run' if s['dry_run'] else 'live'}) ===")
    for r in s["refreshed"]:
        if r.get("mode") == "retitle":
            print(f"  ~ {r['slug']} ({r['reason']}): title \"{r['old_title']}\" → \"{r['new_title']}\"")
        elif r["changed"]:
            print(f"  + {r['slug']} ({r['reason']}): {r['words_before']} → {r['words_after']} words, "
                  f"prices -{len(r['prices_removed'])} +{len(r['prices_added'])}")
        else:
            print(f"  = {r['slug']} ({r['reason']}): unchanged, not re-dated")
    for f in s["failed"]:
        print(f"  ! {f['slug']}: {f['error'][:200]}")
    if s.get("waiting"):
        print(f"  … also due, next run: {', '.join(s['waiting'])}")
    if not s["refreshed"] and not s["failed"]:
        print("  nothing due")
    if s.get("published"):
        print(f"  pull request: {s['published']['pr_url']}")
    if s.get("publish_error"):
        print(f"  ! not published: {s['publish_error']}")
    if s.get("report"):
        print(f"  report: {s['report']}")


def notify_message(s: dict) -> tuple[str, str]:
    site = s["site"]
    if s["failed"]:
        f = s["failed"][0]
        return f"SEO engine: {site} refresh failed", f"{f['slug']}: {f['error']}"
    if s["written"]:
        names = ", ".join(Path(p).stem for p in s["written"])
        if s.get("publish_error"):
            return f"SEO engine: {site} refreshed {len(s['written'])}, not published", s["publish_error"]
        tail = f" Pull request ready: {s['published']['pr_url']}" if s.get("published") else ""
        return f"SEO engine: {site} refreshed {len(s['written'])}", names + "." + tail
    return f"SEO engine: {site} refresh", "Nothing was due."
