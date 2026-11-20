"""Internal-linking self-verification on a synthetic site (no API calls)."""
import csv, json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import interlink
from config import DEFAULTS
from tests_common import report

def post(d, slug, body):
    (d / f"{slug}.md").write_text(f"---\ntitle: {slug}\nslug: {slug}\n---\n\n# {slug}\n\n{body}\n")

def main():
    tmp = Path(tempfile.mkdtemp())
    blog = tmp / "blog"; blog.mkdir()
    post(blog, "water-heater-repair", "Intro mentioning tankless water heaters right away.\n\nA second paragraph about pipes.\n\n## Emergency plumber heading\n\nCalling an emergency plumber costs more at night. Tankless water heaters save space.\n\nYou may also want a Tankless Water Heater someday. See [emergency plumbers](/blog/emergency-plumber/) too.")
    post(blog, "emergency-plumber", "Intro paragraph.\n\nSometimes the fix is water heater repair, not replacement.\n\n`water heater repair` in code stays untouched.")
    post(blog, "tankless-water-heaters", "Intro.\n\nNothing relevant here about tankless water heaters themselves.")
    gsc = tmp / "gsc.csv"
    with open(gsc, "w", newline="") as f:
        w = csv.writer(f); w.writerow(["Page", "Query", "Clicks", "Impressions", "CTR", "Position"])
        w.writerow(["https://x.com/blog/tankless-water-heaters/", "tankless water heaters", 10, "1,200", "1%", 6.2])
        w.writerow(["https://x.com/blog/emergency-plumber/", "emergency plumber", 40, 900, "4%", 2.1])
        w.writerow(["https://x.com/blog/water-heater-repair/", "water heater repair", 5, 300, "2%", 12.4])
        w.writerow(["https://x.com/blog/water-heater-repair/", "heater", 5, 9000, "2%", 3.0])
        w.writerow(["https://x.com/blog/emergency-plumber/", "plumber tips", 1, 10, "1%", 4.0])
    cfg = {"_paths": {"output": blog, "data": tmp}, "blog_url_prefix": "/blog", "directory_url_prefix": "/directory",
           "interlink": DEFAULTS["interlink"], "config_path": str(tmp / "c.yaml")}
    plan = interlink.build_plan(cfg, interlink.load_gsc(gsc))
    originals = {f: f.read_text() for f in blog.glob("*.md")}
    by_src = {}
    for p in plan: by_src.setdefault(Path(p["source"]).stem, []).append(p)
    whr = by_src.get("water-heater-repair", [])
    checks = [
        ("links planned", len(plan) > 0, [(Path(p["source"]).stem, p["anchor"], p["target_url"]) for p in plan]),
        ("no self links", all(Path(p["source"]).stem not in p["target_url"] for p in plan), ""),
        ("each target once per post", all(len({p["target_url"] for p in v}) == len(v) for v in by_src.values()), ""),
        ("max links per post respected", all(len(v) <= 5 for v in by_src.values()), ""),
        ("intro paragraph and headings untouched", all("Intro" not in originals[Path(p["source"])][originals[Path(p["source"])].rfind("\n", 0, p["start"]) + 1:p["end"]] and not originals[Path(p["source"])][originals[Path(p["source"])].rfind("\n", 0, p["start"]) + 1:].startswith("#") for p in plan), ""),
        ("already-linked target skipped", not any(p["target_url"] == "/blog/emergency-plumber/" for p in whr), ""),
        ("page at position 6.2 is prioritised and linked", any(p["target_url"] == "/blog/tankless-water-heaters/" and p["priority"] for p in whr), ""),
        ("position 11-15 fallback target gets a link", any(p["target_url"] == "/blog/water-heater-repair/" and p["target_position"] == 12.4 for p in plan), ""),
        ("single-word anchors never used", all(len(p["anchor"].split()) >= 2 for p in plan), ""),
        ("inline code untouched", not any("`" in originals[Path(p["source"])][p["start"] - 1:p["end"] + 1] for p in plan), ""),
    ]
    log = interlink.apply_plan(plan, tmp / "interlink")
    applied = json.loads(log.read_text())["changes"]
    checks.append(("all edits applied, anchor text unchanged", all(c["status"] == "applied" for c in applied) and all(f"[{p['anchor']}]({p['target_url']})" in Path(p["source"]).read_text() for p in plan), ""))
    done, failed = interlink.revert(log)
    checks.append(("revert restores files byte-for-byte", failed == 0 and all(f.read_text() == t for f, t in originals.items()), f"{done} reverted"))
    print(interlink.plan_table(plan))
    return report("INTERLINK", checks)

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
