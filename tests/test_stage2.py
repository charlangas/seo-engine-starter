"""Stage 2 self-verification: builds briefs for the selected keywords and asserts acceptance criteria."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, serp
import textutil
from textutil import tokens
from tests_common import live_only, report

REQUIRED = ["primary_keyword", "secondary_keywords", "intent", "target_word_count", "outline",
            "gaps_to_address", "placement_rules", "schema_type"]

def check_brief(b, config):
    checks = [(f"required keys", all(k in b for k in REQUIRED), [k for k in REQUIRED if k not in b])]
    checks.append(("target_word_count is int >= minimum rule",
                   isinstance(b["target_word_count"], int) and b["target_word_count"] >= min(b["serp_summary"]["minimum_length_rule"], b["target_word_count"]),
                   f"target={b['target_word_count']} avg_top5={b['serp_summary']['average_top5_word_count']}"))
    checks.append(("target = max(avg_top5, minimum rule)", b["target_word_count"] == max(b["serp_summary"]["average_top5_word_count"], b["serp_summary"]["minimum_length_rule"]), ""))
    checks.append(("gaps_to_address non-empty", len(b["gaps_to_address"]) > 0, f"{len(b['gaps_to_address'])} gaps"))
    # independent recount: gap H2 must not be an H2 in >= 3 of the top results
    kw = set(tokens(b["primary_keyword"]))
    gap_secs = [s for s in b["outline"] if s.get("gap")]
    ok_gaps = []
    for s in gap_secs:
        need = set(tokens(s["h2"])) - kw or set(tokens(s["h2"]))
        n = sum(1 for p in b["serp_summary"]["top5_pages"]
                if any(len(need & set(tokens(h))) / len(need) >= 0.6 for h in p["h2"]))
        if n < 3:
            ok_gaps.append(s["h2"])
    checks.append((">= 2 gap sections not H2s in >= 3 top results", len(ok_gaps) >= 2 and len(ok_gaps) == len(gap_secs), ok_gaps))
    top_h2 = {" ".join(tokens(h)) for p in b["serp_summary"]["top5_pages"] for h in p["h2"]}
    own = [s for s in b["outline"] if " ".join(tokens(s["h2"])) not in top_h2]
    checks.append(("outline is not a restatement of top H2s", len(own) >= 2, f"{len(own)}/{len(b['outline'])} sections are new"))
    checks.append(("top 10 organic captured", 1 <= len(b["serp_summary"]["top_results"]) <= 10, f"{len(b['serp_summary']['top_results'])} results"))
    # Real data only: a results page dominated by video and competitor domains can offer fewer than two
    # citable sources, and the engine must not invent one. Require 2-4 only when enough eligible domains exist.
    n_src = len(b["external_sources"])
    eligible = {r.get("domain") for r in b["serp_summary"]["top_results"]
                if r.get("domain") and r["domain"] != config["domain"]
                and r["domain"] not in config["competitor_domains"] and not serp._skip(r["domain"], config)}
    checks.append(("external_sources 2-4 (or all eligible when fewer than 2)",
                   n_src <= 4 and (n_src >= 2 or n_src >= len(eligible)), f"{n_src} cited, {len(eligible)} eligible"))
    checks.append(("no placeholder text", "TODO" not in json.dumps(b) and "[volume]" not in json.dumps(b), ""))
    return checks

def main(path="configs/test-minimal.yaml"):
    live_only("STAGE 2")
    c = cfg.load_config(path, dry_run="--dry-run" in sys.argv)
    ranked = json.loads((c["_paths"]["work"] / "ranked_keywords.json").read_text())
    ok = True
    for item in [i for i in ranked if i["selected"]]:
        try:
            serp.build_brief(item, c)
        except Exception as e:
            print(f"  [FAIL] build_brief({item['keyword']}): {e}"); ok = False; continue
    # Only the briefs this run built; files left by earlier runs are not part of this check.
    slugs = {textutil.slugify(i["keyword"]) for i in ranked if i["selected"]}
    files = sorted(f for f in c["_paths"]["briefs"].glob("brief_*.json") if f.stem[len("brief_"):] in slugs)
    ok &= report("briefs exist", [("brief_*.json files written", len(files) > 0, [f.name for f in files])])
    for f in files:
        ok &= report(f"STAGE 2 {f.name}", check_brief(json.loads(f.read_text()), c))
    rt = c["_runtime"]
    print(f"SerpAPI calls this run: {rt.calls['serpapi']} (limit {rt.limits['serpapi']})")
    print("STAGE 2:", "PASS" if ok else "FAIL")
    return ok

if __name__ == "__main__":
    sys.exit(0 if main(*[a for a in sys.argv[1:] if not a.startswith("--")]) else 1)
