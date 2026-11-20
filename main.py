"""CLI entry point.

  python main.py run --config configs/site.yaml [--dry-run]
  python main.py run-all [--dry-run]
  python main.py interlink --config configs/site.yaml --gsc export.csv [--yes]
  python main.py interlink-apply --config configs/site.yaml --plan <plan.json>
  python main.py interlink-revert --changelog <changelog.json>
  python main.py gsc-check --config configs/site.yaml [--export performance.zip]
  python main.py gsc-check --all
  python main.py categories --config configs/site.yaml
  python main.py dealers --site SITE --equipment forklifts --input export.xlsx [--input more.csv] [--no-crawl]
  python main.py refresh --config configs/site.yaml [--dry-run] [--slug SLUG ...] [--limit N]
  python main.py refresh-all [--dry-run]
  python main.py forecast --config configs/site.yaml [--traffic-potential N]
  python main.py autopilot (--config configs/site.yaml | --all) [--dry-run] [--task NAME ...] [--notify]
  python main.py health --config configs/site.yaml [--no-inspect]
  python main.py competitor-watch --config configs/site.yaml
  python main.py spend [--config configs/site.yaml | --all] [--month YYYY-MM]
  python main.py pace --config configs/site.yaml

run, run-all, refresh, refresh-all and gsc-check take --notify: a desktop alert with the outcome (scheduled jobs use it).
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import comparisons
import directory
import inventory
import notify
import output
import publish
import research
import serp
import writer
from config import ROOT, ConfigError, load_config
from textutil import jaccard, slugify, tokens


def run_site(config_path, dry_run: bool = False, limit: int | None = None) -> dict:
    """Research and write new pages. limit: at most this many pages (autopilot passes what the schedule owes);
    comparison pages count toward it and take turns with articles."""
    import budget
    with budget.job("content", keep_outer=True):
        summary = _run_site(config_path, dry_run, limit)
        config = summary.pop("_config")
        if not dry_run or summary["written"]:
            budget.note_pages(config, len(summary["written"]))
        return summary


def _run_site(config_path, dry_run: bool, limit: int | None) -> dict:
    config = load_config(config_path, dry_run=dry_run)
    if limit is not None:
        config["max_keywords_per_run"] = limit
    rt = config["_runtime"]
    rt.get_secrets()  # fail clearly before doing anything
    rt.log("stage", stage="run_start", site=config["site_name"], dry_run=dry_run, run_id=rt.run_id)
    summary = {"site": config["site_name"], "dry_run": dry_run, "written": [], "skipped": [], "failed": [],
               "directory": {"listings": 0, "categories": 0}}

    def guarded(stage, fn):
        try:
            return fn()
        except Exception as e:
            rt.log("stage_error", stage=stage, error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc())
            return None

    if not dry_run:
        try:
            publish.prepare(config)  # fail before spending anything if the site checkout cannot take the output
        except publish.PublishError as e:
            rt.log("stage_error", stage="publish_prepare", error=str(e))
            summary["failed"].append({"keyword": "(site checkout)", "error": str(e)})
            summary.update(editor_skipped=[], api_calls=dict(rt.calls), log=str(rt.log_path), _config=config)
            return summary

    guarded("import_existing", lambda: output.import_existing_content(config))
    if config["enable_directory"]:
        summary["directory"] = guarded("directory", lambda: directory.build_directory(config)) or summary["directory"]

    selected = guarded("research", lambda: research.get_opportunities(config)) or []
    import forecast
    active, have, target = forecast.money_mode_active(config)
    if active:
        summary["money_pages"] = {"written": have, "target": target}
    if comparisons.settings(config):
        if writer.pick_backend(config).name == "template":  # comparison pages need real prose and real facts
            rt.log("stage_error", stage="comparisons", error="comparison pages need an LLM writer backend; skipped")
        else:
            taken = [s["keyword"] for s in selected]
            extra = guarded("comparisons", lambda: comparisons.get_opportunities(config, taken)) or []
            if limit is not None and extra and selected:
                # one pace for both kinds: whichever kind was written last waits its turn
                last = max((e for e in inventory.load(config) if e.get("status") == "written" and e.get("date")
                            and not e.get("source")), key=lambda e: e["date"], default={})
                selected = (selected + extra) if last.get("page_type") else (extra + selected)
            else:
                selected += extra
    if limit is not None:
        selected = selected[:limit]
    ranked_all = selected
    import evidence
    ev_settings, (ev_items, ev_errors) = evidence.settings(config), evidence.load(config)
    for err in ev_errors:
        rt.log("stage_error", stage="evidence", error=err)

    def ev_check(item):  # keyword-only pre-check, so a skipped keyword costs no SERP calls
        return not evidence.plan(config, item["keyword"], (), item.get("competitors") or (), item.get("page_type"),
                                 ev_items)["missing"]

    dates = output.schedule_dates(config, len(selected))
    for item, pub in zip(selected, dates):
        kw, slug = item["keyword"], slugify(item["keyword"])
        try:
            existing = inventory.find(kw, config)
            if existing and existing.get("status") == "written":
                summary["skipped"].append(kw)
                rt.log("rejection", stage="pipeline", keyword=kw, reason="already_written")
                continue
            if not dry_run:
                inventory.add_entry(config, kw, slug, type="article", status="planned", date=pub,
                                    volume=item["volume"], kd=item["kd"], intent=item["intent"], score=item["score"],
                                    **({"page_type": item["page_type"], "competitors": item["competitors"]}
                                       if item.get("page_type") else {}))
            if ev_settings["when_missing"] == "skip" and not ev_check(item):
                summary.setdefault("no_evidence", []).append({"keyword": kw, "action": "skipped"})
                rt.log("rejection", stage="pipeline", keyword=kw, reason="no_first_party_evidence")
                continue
            item["related"] = [o["keyword"] for o in ranked_all
                               if o is not item and jaccard(tokens(o["keyword"]), tokens(kw)) >= 0.5][:5]
            brief = serp.build_brief(item, config)
            if brief.get("needs_tool") and not item.get("page_type"):
                # 3+ of the top 5 are calculators/generators: an article will not rank; a person decides on a tool
                summary.setdefault("tool_needed", []).append(kw)
                if not dry_run:
                    inventory.add_entry(config, kw, slug, type="article", status="needs_tool", volume=item["volume"],
                                        kd=item["kd"], intent=item["intent"])
                rt.log("rejection", stage="pipeline", keyword=kw, reason="serp_wants_a_tool")
                continue
            if brief.get("serp_suspect"):
                summary.setdefault("serp_suspect", []).append({"keyword": kw, "problems": brief["serp_suspect"]})
            if item.get("page_type"):
                comparisons.augment_brief(brief, item, config)
            brief["pubDate"] = pub
            md = writer.generate_article(brief, config)
            import aiaudit
            ai = aiaudit.score_page(md, config, {"evidence": brief.get("inventory_extra", {}).get("evidence", [])})
            brief.setdefault("inventory_extra", {})["ai_score"] = ai["score"]  # internal: for tuning writer rules
            rt.log("checklist_score", keyword=kw, score=ai["score"])
            path = output.write_article(md, brief, config)
            summary["written"].append(str(path))
            if brief["evidence"]["missing"]:
                summary.setdefault("no_evidence", []).append(
                    {"keyword": kw, "action": "draft" if brief["evidence"]["draft"] else "written",
                     "missing": brief["evidence"]["missing"]})
            if brief.get("comparison") or brief.get("off_site"):
                guarded("companions", lambda: summary.setdefault("companions", []).extend(
                    comparisons.write_companions(md, brief, config, writer.pick_backend(config))))
        except Exception as e:
            summary["failed"].append({"keyword": kw, "error": f"{type(e).__name__}: {e}"})
            if type(e).__name__ == "WriterError" and not dry_run:
                # Deterministic quality-gate rejection: retrying the same keyword will fail the same way.
                inventory.add_entry(config, kw, slug, type="article", status="failed", error=str(e)[:200])
            rt.log("stage_error", stage="pipeline", keyword=kw, error=f"{type(e).__name__}: {e}",
                   traceback=traceback.format_exc())
            continue

    guarded("topical_map", lambda: output.write_topical_map(config))
    if summary["written"] and not dry_run and config.get("publish"):
        try:
            summary["published"] = publish.open_pr(config, summary)
            rt.log("stage", stage="published", **summary["published"])
        except publish.PublishError as e:
            summary["publish_error"] = str(e)
            rt.log("stage_error", stage="publish", error=str(e))
    summary["editor_skipped"] = sorted(set(rt.editor_skipped))
    summary["api_calls"] = dict(rt.calls)
    summary["log"] = str(rt.log_path)
    rt.log("summary", **{k: (len(v) if isinstance(v, list) else v) for k, v in summary.items()})
    summary["_config"] = config
    return summary


def main(argv=None) -> int:
    """Entry point. With --notify, a crash still raises an alert before the traceback."""
    try:
        return _main(argv)
    except Exception as e:
        if "--notify" in (argv if argv is not None else sys.argv[1:]):
            notify.send("SEO engine: run crashed", f"{type(e).__name__}: {e}")
        raise


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SEO content engine for Astro content collections")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="process one site config end to end")
    r.add_argument("--config", required=True)
    r.add_argument("--dry-run", action="store_true")
    ra = sub.add_parser("run-all", help="process every config in configs/")
    ra.add_argument("--dry-run", action="store_true")
    for sp in (r, ra):
        sp.add_argument("--notify", action="store_true", help="desktop alert with the outcome")
    il = sub.add_parser("interlink", help="plan internal links from a Search Console export")
    il.add_argument("--config", required=True)
    il.add_argument("--gsc", help="GSC CSV with page + query rows (omit to use inventory keywords)")
    il.add_argument("--yes", action="store_true", help="apply the plan immediately (unattended weekly runs)")
    ia = sub.add_parser("interlink-apply", help="apply a previously reviewed plan")
    ia.add_argument("--config", required=True)
    ia.add_argument("--plan", required=True)
    iv = sub.add_parser("interlink-revert", help="undo an applied plan from its changelog")
    iv.add_argument("--changelog", required=True)
    gc = sub.add_parser("gsc-check", help="daily Search Console opportunity report (plans links, applies nothing)")
    gc.add_argument("--config")
    gc.add_argument("--export", help="Search Console UI export (.zip or folder) instead of the live API")
    gc.add_argument("--all", action="store_true", help="every config that sets gsc_property")
    gc.add_argument("--notify", action="store_true", help="desktop alert if the check fails")
    rf = sub.add_parser("refresh", help="re-research and revise published pages that are due (age or Search Console decline)")
    rf.add_argument("--config", required=True)
    rf.add_argument("--slug", nargs="+", help="refresh these pages now, whatever their age")
    rf.add_argument("--limit", type=int, help="pages this run (default: refresh.max_per_run)")
    rfa = sub.add_parser("refresh-all", help="refresh every config in configs/")
    for sp in (rf, rfa):
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--notify", action="store_true", help="desktop alert with the outcome")
    fc = sub.add_parser("forecast", help="project traffic, leads and revenue from the keywords found so far")
    fc.add_argument("--config", required=True)
    fc.add_argument("--traffic-potential", type=int, default=0, metavar="N",
                    help="ask DataForSEO for the #1 page's traffic on the top N keywords (spends)")
    av = sub.add_parser("ai-visibility", help="check whether AI answers cite the site for its buyer prompts (spends)")
    av.add_argument("--config", required=True)
    av.add_argument("--dry-run", action="store_true", help="only say how many calls it would make")
    pm = sub.add_parser("page-map", help="map keywords to pages by SERP overlap; merge/301 suggestions")
    pm.add_argument("--config", required=True)
    pm.add_argument("--fetch", type=int, default=0, metavar="N", help="buy SERPs for up to N uncached keywords (spends)")
    ap = sub.add_parser("autopilot", help="run every job that is due for a site (schedule this daily)")
    ap.add_argument("--config")
    ap.add_argument("--all", action="store_true", help="every config with autopilot.enabled: true")
    ap.add_argument("--dry-run", action="store_true", help="only list the jobs that are due")
    ap.add_argument("--task", nargs="+", help="run these jobs now, whatever their schedule")
    ap.add_argument("--notify", action="store_true", help="desktop alert with the weekly report or a failure")
    sp_ = sub.add_parser("spend", help="this month's spending, caps, cost per page and the pace the budget covers")
    sp_.add_argument("--config")
    sp_.add_argument("--all", action="store_true", help="every site, with a line per site")
    sp_.add_argument("--month", help="YYYY-MM (default: this month)")
    pc = sub.add_parser("pace", help="publishing pace, the next dates, and whether the signals support publishing more")
    pc.add_argument("--config", required=True)
    hc = sub.add_parser("health", help="crawl the sitemap: status, redirects, canonicals, noindex, duplicates, indexing")
    hc.add_argument("--config", required=True)
    hc.add_argument("--no-inspect", action="store_true", help="skip the Search Console URL Inspection calls")
    cw = sub.add_parser("competitor-watch", help="list pages competitors published since the last check")
    cw.add_argument("--config", required=True)
    ev = sub.add_parser("evidence", help="check first-party evidence items and list pages with nothing to draw on")
    ev.add_argument("--config", required=True)
    cg = sub.add_parser("categories", help="propose directory category pages from the site's own listing records")
    cg.add_argument("--config", required=True)
    ci = sub.add_parser("categories-install", help="copy reviewed category proposals into the site as drafts")
    ci.add_argument("--config", required=True)
    ci.add_argument("--slugs", nargs="+", required=True)
    dl = sub.add_parser("dealers", help="turn Outscraper exports into draft dealer records, checked against dealer websites")
    dl.add_argument("--site", required=True, help="folder name under data/")
    dl.add_argument("--equipment", required=True, choices=["forklifts", "air-compressors", "heavy-equipment"])
    dl.add_argument("--input", action="append", required=True, help="Outscraper .xlsx or .csv export (repeatable)")
    dl.add_argument("--no-crawl", action="store_true", help="skip reading dealer websites")
    dl.add_argument("--max-pages", type=int, default=8, help="pages read per dealer website")
    dl.add_argument("--export", dest="export_to", help="copy published records into this folder (a site's data folder)")
    args = p.parse_args(argv)

    try:
        if args.cmd == "run":
            s = run_site(args.config, args.dry_run)
            _print_summary(s)
            if args.notify:
                notify.send(*notify.run_message(s))
            return 0 if (s["written"] or s["skipped"]) and not s["failed"] and not s.get("publish_error") else 1
        if args.cmd == "run-all":
            code = 0
            for cfg in sorted((ROOT / "configs").glob("*.y*ml")):
                try:
                    s = run_site(cfg, args.dry_run)
                    _print_summary(s)
                    if args.notify:
                        notify.send(*notify.run_message(s))
                    if s["failed"]:
                        code = 1
                except Exception as e:  # one site failing must not stop the others
                    print(f"[{cfg.name}] FAILED: {type(e).__name__}: {e}")
                    if args.notify:
                        notify.send(f"SEO engine: {cfg.stem} failed", f"{type(e).__name__}: {e}")
                    code = 1
            return code
        if args.cmd == "autopilot":
            import autopilot
            if not args.all and not args.config:
                p.error("autopilot needs --config or --all")
            return autopilot.cli(args.config, args.all, args.dry_run, args.task, args.notify)
        if args.cmd == "spend":
            import budget
            return budget.cli(args.config, args.all, args.month)
        if args.cmd == "pace":
            import pacing
            return pacing.cli(args.config)
        if args.cmd == "health":
            import health
            return health.cli(args.config, not args.no_inspect)
        if args.cmd == "competitor-watch":
            import watch
            return watch.cli(args.config)
        if args.cmd == "ai-visibility":
            import aivis
            return aivis.cli(args.config, args.dry_run)
        if args.cmd == "page-map":
            import cluster
            return cluster.cli(args.config, args.fetch)
        if args.cmd == "evidence":
            import evidence
            return evidence.cli(args.config)
        if args.cmd == "forecast":
            import forecast
            return forecast.cli(args.config, args.traffic_potential)
        if args.cmd in ("refresh", "refresh-all"):
            import refresh
            cfgs = [args.config] if args.cmd == "refresh" else sorted((ROOT / "configs").glob("*.y*ml"))
            code = 0
            for cfg in cfgs:
                try:
                    import budget
                    with budget.job("refresh"):
                        s = refresh.run_refresh(cfg, args.dry_run, *((args.slug, args.limit) if args.cmd == "refresh" else ()))
                except ConfigError:
                    if args.cmd == "refresh":
                        raise
                    s = {"site": Path(cfg).stem, "failed": [{"slug": "(config)", "error": "configuration error"}],
                         "written": [], "refreshed": [], "dry_run": args.dry_run}
                except Exception as e:  # one site failing must not stop the others
                    s = {"site": Path(cfg).stem, "failed": [{"slug": "(site)", "error": f"{type(e).__name__}: {e}"}],
                         "written": [], "refreshed": [], "dry_run": args.dry_run}
                refresh.print_summary(s)
                if args.notify and (s["written"] or s["failed"]):
                    notify.send(*refresh.notify_message(s))
                code = code or (1 if s["failed"] or s.get("publish_error") else 0)
            return code
        if args.cmd == "categories-install":
            import categories
            return categories.cli_install(args.config, args.slugs)
        if args.cmd == "dealers":
            import dealers
            return dealers.cli(args.site, args.equipment, args.input, not args.no_crawl, args.max_pages, args.export_to)
        if args.cmd == "categories":
            import categories
            return categories.cli(args.config)
        if args.cmd == "gsc-check":
            import gsc
            if not args.all and not args.config:
                p.error("gsc-check needs --config or --all")
            code = gsc.cli_check_all() if args.all else gsc.cli_check(args.config, args.export)
            if code and args.notify:
                notify.send("SEO engine: Search Console check failed", "See logs/gsc-check.log for the error.")
            return code
        import interlink
        if args.cmd == "interlink":
            return interlink.cli_plan(args.config, args.gsc, args.yes)
        if args.cmd == "interlink-apply":
            return interlink.cli_apply(args.config, args.plan)
        if args.cmd == "interlink-revert":
            return interlink.cli_revert(args.changelog)
    except ConfigError as e:
        print(f"CONFIG ERROR: {e}")
        if getattr(args, "notify", False):
            notify.send("SEO engine: configuration error", str(e))
        return 2
    return 0


def _print_summary(s: dict):
    print(f"\n=== {s['site']} ({'dry run' if s['dry_run'] else 'live'}) ===")
    print(f"written: {len(s['written'])}  skipped: {len(s['skipped'])}  failed: {len(s['failed'])}  "
          f"directory: {s['directory']['listings']} listings / {s['directory']['categories']} categories")
    for w in s["written"]:
        print(f"  + {w}")
    if s.get("no_evidence"):
        print("no first-party evidence: " + ", ".join(f"{n['keyword']} ({n['action']})" for n in s["no_evidence"])
              + "  -> add items to data/<site>/evidence/ (see `main.py evidence`)")
    if s.get("money_pages"):
        print(f"money-pages mode: {s['money_pages']['written']} of {s['money_pages']['target']} bottom-of-funnel pages "
              f"written; only those keywords were selected")
    if s.get("tool_needed"):
        print("SERP wants a free tool, not an article (skipped): " + ", ".join(s["tool_needed"]))
    for x in s.get("serp_suspect") or []:
        print(f"  ? {x['keyword']}: search results looked wrong ({'; '.join(x['problems'])}); format not taken from them")
    for kw in s.get("editor_skipped") or []:
        print(f"  ? {kw}: editor review failed, piece passed the rule-based gate only")
    for f in s["failed"]:
        print(f"  ! {f['keyword']}: {f['error'][:200]}")
    for c in s.get("companions") or []:
        print(f"  ~ companion: {c}")
    if s.get("published"):
        print(f"  pull request: {s['published']['pr_url']}")
    if s.get("publish_error"):
        print(f"  ! not published: {s['publish_error']}")
    print(f"api calls: {s.get('api_calls')}  log: {s.get('log')}")


if __name__ == "__main__":
    sys.exit(main())
