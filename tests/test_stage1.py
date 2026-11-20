"""Stage 1 self-verification: runs research live and asserts the acceptance criteria."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, research
from tests_common import live_only, report

def main(path="configs/test-minimal.yaml"):
    live_only("STAGE 1")
    c = cfg.load_config(path, dry_run="--dry-run" in sys.argv)
    selected = research.get_opportunities(c)
    f = c["_paths"]["work"] / "ranked_keywords.json"
    checks = [("ranked_keywords.json exists", f.exists(), str(f))]
    items = json.loads(f.read_text()) if f.exists() else []
    checks.append(("at least one keyword passed", len(items) > 0, f"{len(items)} items"))
    bad = [i["keyword"] for i in items if not all(k in i and i[k] is not None for k in ("keyword", "volume", "kd", "intent", "score")) or "cpc" not in i]
    checks.append(("every item has volume, kd, intent, score, cpc key", not bad, bad[:5]))
    checks.append(("types match contract", all(isinstance(i["volume"], int) and isinstance(i["kd"], float) and isinstance(i["score"], float) and isinstance(i["intent"], str) and (i["cpc"] is None or isinstance(i["cpc"], float)) for i in items), ""))
    checks.append(("volume >= floor", all(i["volume"] >= c["volume_floor"] for i in items), ""))
    checks.append(("kd <= kd_max", all(i["kd"] <= c["kd_max"] for i in items), ""))
    checks.append(("intent in priorities", all(i["intent"] in c["intent_priority"] for i in items), ""))
    pats = research.brand_patterns(c)
    checks.append(("no branded terms", not any(research.is_branded(i["keyword"], pats) for i in items), ""))
    scores = [i["score"] for i in items]
    checks.append(("sorted by score desc", scores == sorted(scores, reverse=True), ""))
    checks.append(("score formula holds", all(abs(i["score"] - research.score_keyword(i["volume"], i["kd"], i["intent"])) < 0.01 for i in items), ""))
    checks.append(("selected <= max_keywords_per_run", len(selected) <= c["max_keywords_per_run"] and len(selected) == sum(1 for i in items if i["selected"]), f"{len(selected)} selected"))
    rt = c["_runtime"]
    checks.append(("DataForSEO calls within limit", rt.calls["dataforseo"] <= rt.limits["dataforseo"], f"{rt.calls['dataforseo']} calls"))
    return report("STAGE 1", checks)

if __name__ == "__main__":
    sys.exit(0 if main(*[a for a in sys.argv[1:] if not a.startswith("--")]) else 1)
