"""Internal linking from Google Search Console data.

Plan -> review -> apply -> (optional) revert:
  1. For each article, take the queries where it ranks best (position 1-10, decent impressions)
     as its target anchors.
  2. Scan every published post; where the text already mentions another article's query
     (or a close variant), propose turning that phrase into a link.
  3. Rules: max N new links per post, one per paragraph, never in headings, the intro paragraph,
     existing links, code or tables; never self-link; each target once per post; text is never
     rewritten; pages ranking 4-15 get links first.
  4. Nothing changes until the plan is approved (interlink-apply, or --yes for unattended runs).
  5. Every applied plan writes a changelog that interlink-revert can undo.

Money-page links (added to the same plan, marked kind "money"): the articles with the most Search Console
impressions (`money_sources`, default 10) each get links to the site's money pages (comparison pages,
audience pages, bottom-of-funnel keywords, plus any `interlink.money_pages` URLs):
  - at least one money link in the first 40% of the article, `money_links_min`-`money_links_max` (2-4) in all;
  - anchors are 3-7 words of text already on the page (or the money page's exact primary keyword);
  - no exact anchor is used for the same page more than `money_anchor_max_uses` (3) times across the site.
Articles where no phrase fits are listed in `money_gaps` for the writer or a person.
"""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from config import load_config
from textutil import split_frontmatter

COLS = {
    "page": ["page", "pages", "url", "landing page", "top pages", "address"],
    "query": ["query", "queries", "top queries", "keyword", "search query"],
    "impressions": ["impressions"],
    "position": ["position", "avg position", "average position", "avg. position"],
    "clicks": ["clicks"],
}


def _num(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip() or 0)
    except ValueError:
        return 0.0


def load_gsc(path) -> list[dict]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = {h.lower().strip(): h for h in (reader.fieldnames or [])}
        col = {k: next((header[a] for a in aliases if a in header), None) for k, aliases in COLS.items()}
        missing = [k for k in ("page", "query", "position") if not col[k]]
        if missing:
            raise ValueError(
                f"GSC export needs page + query + position columns per row; missing {missing}. "
                f"Found columns: {list(header.values())}. The standard UI export splits Queries and Pages into "
                f"separate tabs; export page+query pairs via the Search Console API, Looker Studio, or "
                f"'Search Analytics for Sheets'.")
        rows = []
        for r in reader:
            if r.get(col["page"]) and r.get(col["query"]):
                rows.append({"page": r[col["page"]].strip(), "query": r[col["query"]].strip().lower(),
                             "impressions": _num(r.get(col["impressions"])) if col["impressions"] else 0.0,
                             "clicks": _num(r.get(col["clicks"])) if col["clicks"] else 0.0,
                             "position": _num(r[col["position"]])})
        return rows


def load_posts(config) -> list[dict]:
    import output
    sources = list(output.engine_dirs(config))
    for src in config.get("existing_content") or []:
        d = Path(src["directory"]).expanduser()
        if not d.is_absolute():
            d = (Path(config["config_path"]).resolve().parent / d).resolve()
        sources.append((d, src.get("url_prefix", config["blog_url_prefix"])))
    posts, seen = [], set()
    dir_prefix = config["directory_url_prefix"].rstrip("/")
    for root, prefix in sources:
        if not Path(root).is_dir():
            continue
        for f in sorted(list(Path(root).rglob("*.md")) + list(Path(root).rglob("*.mdx"))):
            if f in seen:
                continue
            seen.add(f)
            text = f.read_text(encoding="utf-8")
            fm, _ = split_frontmatter(text)
            slug = str(fm.get("slug") or fm.get("routeSlug") or f.stem)
            rel = f.relative_to(root).parts
            if rel[0] == "directory":
                url = f"{dir_prefix}/{rel[1]}/" if f.stem == "index" else f"{dir_prefix}/{rel[1]}/{slug}/"
            else:
                url = f"{prefix.rstrip('/')}/{slug}/"
            posts.append({"path": f, "slug": slug, "url": url, "text": text, "fm": fm,
                          "is_listing": fm.get("type") in ("listing", "category")})
    return posts


def build_targets(posts, gsc_rows, opts) -> list[dict]:
    """One target per article: its anchors (queries), headline query and position."""
    by_slug = {p["slug"]: p for p in posts}
    targets = []
    if gsc_rows is None:  # no GSC data yet: fall back to each post's own keywords
        for p in posts:
            anchors = [str(a).lower() for a in [p["fm"].get("primaryKeyword")] + list(p["fm"].get("secondaryKeywords") or []) if a]
            if anchors and not p["is_listing"]:
                targets.append({"url": p["url"], "slug": p["slug"], "anchors": anchors[: opts["max_anchors_per_target"]],
                                "query": anchors[0], "position": None, "impressions": 0, "priority": False})
        return targets
    grouped: dict[str, list] = {}
    for r in gsc_rows:
        path = urlparse(r["page"]).path if "//" in r["page"] else r["page"]
        slug = path.rstrip("/").split("/")[-1]
        if slug:  # any ranking page can be a link target (e.g. JSON-backed listings); only posts are sources
            path = path if path.endswith("/") else path + "/"
            known = by_slug.get(slug)
            grouped.setdefault(known["url"] if known and known["url"] == path else path, []).append({**r, "path": path})
    for key, rows in grouped.items():
        slug = key.rstrip("/").split("/")[-1]
        rows = [r for r in rows if len(r["query"].split()) >= 2]  # single words make weak, ambiguous anchors
        good = [r for r in rows if 1 <= r["position"] <= opts["anchor_max_position"] and r["impressions"] >= opts["min_impressions"]]
        if not good:  # page sits just off page one: still a priority target, use its 11-15 queries
            good = [r for r in rows if 1 <= r["position"] <= opts["fallback_anchor_max_position"]
                    and r["impressions"] >= opts["min_impressions"]]
        if not good:
            continue
        good.sort(key=lambda r: -r["impressions"])
        top = good[0]
        targets.append({"url": top["path"], "slug": slug,
                        "anchors": [r["query"] for r in good[: opts["max_anchors_per_target"]]],
                        "query": top["query"], "position": round(top["position"], 1), "impressions": int(top["impressions"]),
                        "priority": opts["priority_position_min"] <= top["position"] <= opts["priority_position_max"]})
    targets.sort(key=lambda t: (not t["priority"], -t["impressions"]))
    return targets


def _blocks(text: str):
    """Yield (start, end) spans of linkable prose blocks in the Markdown body."""
    m = re.match(r"^---\n.*?\n---\n", text, flags=re.S)
    pos = m.end() if m else 0
    spans, cur, in_code, first_prose_skipped = [], None, False, False
    for line in text[pos:].splitlines(keepends=True):
        stripped = line.strip()
        start, pos = pos, pos + len(line)
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code = not in_code
            cur = None
            continue
        skip = (in_code or not stripped or stripped.startswith(("#", "|", ">", "<", "![", "import ", "export ", "---"))
                or re.match(r"^\{", stripped))
        if skip:
            cur = None
            continue
        if cur is None:
            cur = [start, pos]
            spans.append(cur)
        else:
            cur[1] = pos
    out = []
    for s, e in spans:
        if not first_prose_skipped:   # never link inside the intro paragraph
            first_prose_skipped = True
            continue
        out.append((s, e))
    return out


def _mask(block: str) -> str:
    """Blank out existing links, inline code and bare URLs so they are never matched."""
    def blank(m):
        return " " * len(m.group(0))
    block = re.sub(r"!?\[[^\]]*\]\([^)]*\)", blank, block)
    block = re.sub(r"`[^`]*`", blank, block)
    block = re.sub(r"https?://\S+", blank, block)
    return re.sub(r"<[^>]+>", blank, block)


def _anchor_regex(query: str):
    words = re.findall(r"[a-z0-9']+", query.lower())
    if not words:
        return None
    parts = [re.escape(w) for w in words]
    last = words[-1]
    parts[-1] = re.escape(last[:-1]) + "s?" if last.endswith("s") and len(last) > 3 else re.escape(last) + "(?:s|es)?"
    return re.compile(r"(?<![\w/\-])" + r"[\s\-]+".join(parts) + r"(?![\w/\-])", re.I)


def build_plan(config, gsc_rows) -> list[dict]:
    opts = config["interlink"]
    posts = load_posts(config)
    targets = build_targets(posts, gsc_rows, opts)
    plan = []
    for post in posts:
        text = post["text"]
        existing_links = set(re.findall(r"\]\(([^)\s]+)\)", text))
        blocks = _blocks(text)
        used_blocks, count = set(), 0
        for t in targets:
            if count >= opts["max_links_per_post"]:
                break
            if t["url"] == post["url"] or t["slug"] == post["slug"] or t["url"] in existing_links or t["url"].rstrip("/") in existing_links:
                continue
            placed = False
            for anchor in sorted(t["anchors"], key=len, reverse=True):
                if len(anchor.split()) < 2:
                    continue  # single words make weak, ambiguous anchors
                rx = _anchor_regex(anchor)
                for bi, (s, e) in enumerate(blocks):
                    if bi in used_blocks or not rx:
                        continue
                    m = rx.search(_mask(text[s:e]))
                    if m:
                        plan.append({"source": str(post["path"]), "source_url": post["url"],
                                     "anchor": text[s + m.start(): s + m.end()], "start": s + m.start(), "end": s + m.end(),
                                     "target_url": t["url"], "target_query": t["query"],
                                     "target_position": t["position"], "target_impressions": t["impressions"],
                                     "priority": t["priority"]})
                        used_blocks.add(bi)
                        count += 1
                        placed = True
                        break
                if placed:
                    break
    money, gaps = build_money_plan(config, posts, gsc_rows, plan)
    config["_money_gaps"] = gaps
    return plan + money


def money_targets(config, posts) -> list[dict]:
    """Money pages: comparison/audience pages, bottom-of-funnel keywords, and interlink.money_pages entries."""
    import forecast
    import inventory
    by_url = {p["url"]: p for p in posts}
    out, seen = [], set()
    entries = inventory.load(config) if (config.get("_paths") or {}).get("inventory") else []
    for e in entries:
        if e.get("type", "article") != "article" or e.get("status") != "written" or inventory.held(config, e):
            continue
        stage = forecast.stage_for(config, e["primary_keyword"], e.get("intent"), e.get("page_type"))
        if not e.get("page_type") and stage != "bottom":
            continue
        url = f'{(e.get("url_prefix") or config["blog_url_prefix"]).rstrip("/")}/{e["slug"]}/'
        post = by_url.get(url)
        anchors = [e["primary_keyword"]] + list(((post or {}).get("fm") or {}).get("secondaryKeywords") or [])
        if post and post["fm"].get("title"):
            anchors.append(str(post["fm"]["title"]))
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "slug": e["slug"], "keyword": e["primary_keyword"], "anchors": anchors})
    for m in (config.get("interlink") or {}).get("money_pages") or []:
        url = m["url"] if isinstance(m, dict) else str(m)
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "slug": url.rstrip("/").rsplit("/", 1)[-1], "keyword": (m.get("anchors") or [""])[0]
                        if isinstance(m, dict) else "", "anchors": list(m.get("anchors") or []) if isinstance(m, dict) else []})
    return out


def _anchor_ok(anchor: str, keyword: str) -> bool:
    n = len(anchor.split())
    return 3 <= n <= 7 or (n >= 2 and anchor.lower() == keyword.lower())


def build_money_plan(config, posts, gsc_rows, taken: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """(link edits, articles where no anchor fitted). Text is never rewritten: only existing phrases are linked."""
    opts = config.get("interlink") or {}
    targets = money_targets(config, posts)
    if not targets:
        return [], []
    target_urls = {t["url"] for t in targets}
    imp: dict[str, float] = {}
    for r in gsc_rows or []:
        path = urlparse(r["page"]).path if "//" in r["page"] else r["page"]
        path = path if path.endswith("/") else path + "/"
        imp[path] = imp.get(path, 0) + r.get("impressions", 0)
    sources = [p for p in posts if not p["is_listing"] and p["url"] not in target_urls]
    sources.sort(key=lambda p: -imp.get(p["url"], 0))
    if gsc_rows:
        sources = [p for p in sources if imp.get(p["url"], 0) > 0]
    sources = sources[: opts.get("money_sources", 10)]

    used: dict[tuple, int] = {}   # (anchor lower, url) -> uses across the site
    for p in posts:
        for a, u in re.findall(r"\[([^\]]+)\]\(([^)\s]+)\)", p["text"]):
            if u in target_urls:
                used[(a.lower(), u)] = used.get((a.lower(), u), 0) + 1
    taken_spans = {}
    for t in taken or []:
        taken_spans.setdefault(t["source"], []).append((t["start"], t["end"]))

    plan, gaps = [], []
    lo, hi, cap = opts.get("money_links_min", 2), opts.get("money_links_max", 4), opts.get("money_anchor_max_uses", 3)
    for post in sources:
        text = post["text"]
        m = re.match(r"^---\n.*?\n---\n", text, flags=re.S)
        body_start = m.end() if m else 0
        cutoff = body_start + int((len(text) - body_start) * 0.4)
        links = re.findall(r"\]\(([^)\s]+)\)", text)
        have = [u for u in links if u in target_urls]
        early = any(text.find(f"]({u})") < cutoff for u in set(have))
        if len(have) >= hi or (len(have) >= lo and early):
            continue
        blocks = [(s, e) for s, e in _blocks(text)
                  if not any(s < te and ts < e for ts, te in taken_spans.get(str(post["path"]), []))]
        used_blocks, added, linked = set(), [], set(have)
        want = max(lo - len(have), 0 if early else 1)
        room = hi - len(have)

        def place(early_only: bool):
            for t in targets:
                if len(added) >= room or t["url"] in linked:
                    continue
                for anchor in sorted(t["anchors"], key=lambda a: -len(a.split())):
                    if not _anchor_ok(anchor, t["keyword"]):
                        continue
                    rx = _anchor_regex(anchor)
                    if not rx:
                        continue
                    for bi, (s, e) in enumerate(blocks):
                        if bi in used_blocks or (early_only and s >= cutoff):
                            continue
                        mm = rx.search(_mask(text[s:e]))
                        if not mm:
                            continue
                        found = text[s + mm.start(): s + mm.end()]
                        if not _anchor_ok(found, t["keyword"]) or used.get((found.lower(), t["url"]), 0) >= cap:
                            continue
                        added.append({"source": str(post["path"]), "source_url": post["url"], "anchor": found,
                                      "start": s + mm.start(), "end": s + mm.end(), "target_url": t["url"],
                                      "target_query": t["keyword"], "target_position": None,
                                      "target_impressions": int(imp.get(t["url"], 0)), "priority": True, "kind": "money"})
                        used_blocks.add(bi)
                        linked.add(t["url"])
                        used[(found.lower(), t["url"])] = used.get((found.lower(), t["url"]), 0) + 1
                        return True
            return False

        if not early:
            place(True)
        while len(added) < want and place(False):
            pass
        if not early and not any(a["start"] < cutoff for a in added):
            gaps.append({"source_url": post["url"], "need": "a money-page link in the first 40%",
                         "targets": [t["url"] for t in targets[:3]]})
        elif len(have) + len(added) < lo:
            gaps.append({"source_url": post["url"], "need": f"{lo - len(have) - len(added)} more money-page link(s)",
                         "targets": [t["url"] for t in targets[:3]]})
        plan += added
    return plan, gaps


def plan_table(plan) -> str:
    rows = ["| Source post | Anchor | Target URL | Target's query | Position |", "|---|---|---|---|---|"]
    for p in plan:
        pos = p["target_position"] if p["target_position"] is not None else "n/a"
        rows.append(f"| {p['source_url']} | {p['anchor']} | {p['target_url']} | {p['target_query']} | {pos} |")
    return "\n".join(rows)


def apply_plan(plan, out_dir: Path) -> Path:
    """Apply edits (anchor text untouched, only wrapped in a link) and write a revertable changelog."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    by_file: dict[str, list] = {}
    for p in plan:
        by_file.setdefault(p["source"], []).append(p)
    changes = []
    for path, edits in by_file.items():
        f = Path(path)
        text = f.read_text(encoding="utf-8")
        for p in sorted(edits, key=lambda x: -x["start"]):
            if text[p["start"]:p["end"]] != p["anchor"]:
                changes.append({"file": path, "status": "skipped_file_changed", "anchor": p["anchor"], "target_url": p["target_url"]})
                continue
            ls = text.rfind("\n", 0, p["start"]) + 1
            le = text.find("\n", p["end"])
            le = len(text) if le == -1 else le
            before = text[ls:le]
            text = text[:p["start"]] + f"[{p['anchor']}]({p['target_url']})" + text[p["end"]:]
            after = text[ls:le + len(p["target_url"]) + 4]
            changes.append({"file": path, "status": "applied", "anchor": p["anchor"], "target_url": p["target_url"],
                            "before_line": before, "after_line": after})
        f.write_text(text, encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    log = out_dir / f"changelog_{stamp}.json"
    log.write_text(json.dumps({"applied_at": stamp, "changes": changes}, indent=2, ensure_ascii=False), encoding="utf-8")
    return log


def revert(changelog_path) -> tuple[int, int]:
    data = json.loads(Path(changelog_path).read_text(encoding="utf-8"))
    done = failed = 0
    for c in data["changes"]:
        if c["status"] != "applied":
            continue
        f = Path(c["file"])
        text = f.read_text(encoding="utf-8") if f.exists() else ""
        linked = f"[{c['anchor']}]({c['target_url']})"
        if c["after_line"] in text:
            f.write_text(text.replace(c["after_line"], c["before_line"], 1), encoding="utf-8")
            done += 1
        elif linked in text:
            f.write_text(text.replace(linked, c["anchor"], 1), encoding="utf-8")
            done += 1
        else:
            failed += 1
    return done, failed


# ---------- CLI ----------

def _dir(config) -> Path:
    return config["_paths"]["data"] / "interlink"


def cli_plan(config_path, gsc_path, yes: bool) -> int:
    config = load_config(config_path)
    rt = config["_runtime"]
    try:
        rows = load_gsc(gsc_path) if gsc_path else None
    except (ValueError, OSError) as e:
        print(f"GSC ERROR: {e}")
        return 2
    plan = build_plan(config, rows)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = _dir(config)
    out.mkdir(parents=True, exist_ok=True)
    plan_file = out / f"plan_{stamp}.json"
    plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    table = plan_table(plan)
    (out / f"plan_{stamp}.md").write_text(table + "\n", encoding="utf-8")
    rt.log("stage", stage="interlink_plan", links=len(plan), gsc_rows=len(rows) if rows is not None else None)
    print(table if plan else "No natural link opportunities found.")
    print(f"\n{len(plan)} proposed link(s). Plan saved: {plan_file}")
    if not plan:
        return 0
    if yes:
        log = apply_plan(plan, out)
        rt.log("stage", stage="interlink_applied", changelog=str(log))
        print(f"Applied. Changelog (use interlink-revert to undo): {log}")
    else:
        print(f"Nothing changed yet. After reviewing, apply with:\n"
              f"  python main.py interlink-apply --config {config_path} --plan {plan_file}")
    return 0


def cli_apply(config_path, plan_path) -> int:
    config = load_config(config_path)
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    log = apply_plan(plan, _dir(config))
    changes = json.loads(log.read_text())["changes"]
    applied = sum(1 for c in changes if c["status"] == "applied")
    config["_runtime"].log("stage", stage="interlink_applied", changelog=str(log), applied=applied)
    print(f"Applied {applied} of {len(plan)} link(s); {len(plan) - applied} skipped because the file changed since planning.")
    print(f"Changelog: {log}\nRevert with: python main.py interlink-revert --changelog {log}")
    return 0


def cli_revert(changelog_path) -> int:
    done, failed = revert(changelog_path)
    print(f"Reverted {done} link(s); {failed} could not be found (file edited since).")
    return 0 if not failed else 1
