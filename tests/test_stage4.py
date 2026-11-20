"""Stage 4 + end-to-end self-verification: full live run, then inventory / output / idempotency checks."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, inventory, main as engine, output
from textutil import normalize_keyword, body_word_count, split_frontmatter, word_count
from tests_common import live_only, report

def main(path="configs/test-minimal.yaml"):
    live_only("STAGE 4")
    s = engine.run_site(path)
    c = cfg.load_config(path)
    entries = inventory.load(c)
    norms = [normalize_keyword(e["primary_keyword"]) for e in entries]
    written = [e for e in entries if e["type"] == "article" and e["status"] == "written"]
    checks = [
        ("inventory.json exists and has entries", c["_paths"]["inventory"].exists() and len(entries) > 0, f"{len(entries)} entries"),
        ("no duplicate primary_keywords (normalized)", len(norms) == len(set(norms)), ""),
        ("entries carry keyword, slug, type, status, date", all(all(k in e for k in ("primary_keyword", "slug", "type", "status", "date")) for e in entries), ""),
        ("at least one article written", len(written) >= 1, [e["slug"] for e in written]),
        ("topical_map.json written with clusters", c["_paths"]["topical_map"].exists() and len(json.loads(c["_paths"]["topical_map"].read_text())["clusters"]) > 0, ""),
        ("log file has api_call + rejection events", all(x in Path(s["log"]).read_text() for x in ('"api_call"', '"rejection"', '"filter_decision"')), s["log"]),
    ]
    for e in written:
        f = c["_paths"]["output"] / f"{e['slug']}.md"
        md = f.read_text() if f.exists() else ""
        brief = json.loads((c["_paths"]["briefs"] / f"brief_{e['slug']}.json").read_text())
        checks.append((f"{e['slug']}.md valid (frontmatter + word count)", f.exists() and not output.validate_markdown(md, brief["target_word_count"]),
                       f"{body_word_count(md)} words, target {brief['target_word_count']}"))
    if c["enable_directory"]:
        d = c["_paths"]["output"] / "directory"
        listings = [f for f in d.glob("*/*.md") if f.name != "index.md"]
        descs = []
        for f in listings:
            body = split_frontmatter(f.read_text())[1]
            descs.append(body.split("\n\n")[1] if "\n\n" in body.strip() else "")
        checks.append(("directory listings written", len(listings) > 0, f"{len(listings)} listings"))
        checks.append(("every listing description >= 80 words and unique", all(word_count(x) >= 80 for x in descs) and len(set(descs)) == len(descs), [word_count(x) for x in descs]))
        checks.append(("category index written", len(list(d.glob("*/index.md"))) > 0, ""))
        dir_links = [sum(1 for l in __import__("re").findall(r"\]\((/directory/[^)]+)\)", (c["_paths"]["output"] / f"{e['slug']}.md").read_text())) for e in written]
        checks.append(("articles link to >= 2 directory pages", all(n >= 2 for n in dir_links), dir_links))
    # idempotency: a second run must not rewrite or duplicate anything
    s2 = engine.run_site(path)
    entries2 = inventory.load(c)
    checks.append(("second run writes nothing new for written keywords", not (set(s2["written"]) & set(s["written"])), f"written again: {len(set(s2['written']) & set(s['written']))}"))
    n2 = [normalize_keyword(e["primary_keyword"]) for e in entries2]
    checks.append(("still no duplicates after second run", len(n2) == len(set(n2)), ""))
    return report("STAGE 4 / END-TO-END", checks)

if __name__ == "__main__":
    sys.exit(0 if main(*[a for a in sys.argv[1:] if not a.startswith("--")]) else 1)
