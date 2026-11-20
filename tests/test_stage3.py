"""Stage 3 self-verification: writes each brief and asserts the acceptance criteria independently."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as cfg, writer
from textutil import BANNED_PHRASES, body_word_count, contains_keyword, normalize_quotes, plain_text, split_frontmatter
from tests_common import live_only, report

FM = ["title", "description", "pubDate", "slug", "tags", "primaryKeyword"]

def check_article(md, brief, c):
    fm, body = split_frontmatter(md)
    kw = brief["primary_keyword"]
    wc = body_word_count(md)
    low = normalize_quotes(md).lower()
    first100 = " ".join(plain_text(re.sub(r"^#\s.*$", "", body, count=1, flags=re.M)).split()[:100])
    intro = body.split("\n## ")[0]
    art = {"title": fm.get("title", ""), "description": fm.get("description", ""), "body": body.lstrip()}
    remaining = writer.blocking(writer.critique(art, brief, c))
    return [
        ("frontmatter keys", all(k in fm for k in FM), [k for k in FM if k not in fm]),
        ("word count >= target", wc >= brief["target_word_count"], f"{wc} vs {brief['target_word_count']}"),
        ("zero banned phrases", not [p for p in BANNED_PHRASES if p in low], [p for p in BANNED_PHRASES if p in low]),
        ("keyword in title", contains_keyword(fm.get("title", ""), kw), fm.get("title")),
        ("keyword in first 100 words", contains_keyword(first100, kw), ""),
        ("keyword in meta description", contains_keyword(fm.get("description", ""), kw), fm.get("description")),
        ("intro <= 150 words", len(plain_text(re.sub(r'^#\s.*$', '', intro, flags=re.M)).split()) <= 150, ""),
        ("FAQ section + list/table", "## Frequently asked" in body and bool(re.search(r"^(\||- |\d+\. )", body, flags=re.M)), ""),
        ("no placeholders / disclosure", not re.search(r"TODO|\[volume\]|as an ai|language model|ai-generated", md, re.I), ""),
        ("critique lists zero remaining fixes", not remaining, [f["code"] for f in remaining]),
    ]

def main(path="configs/test-minimal.yaml"):
    live_only("STAGE 3")
    c = cfg.load_config(path, dry_run=True)   # never touches final output or inventory
    live = cfg.load_config(path)
    ok, n = True, 0
    for f in sorted(live["_paths"]["briefs"].glob("brief_*.json")):
        brief = json.loads(f.read_text())
        try:
            md = writer.generate_article(brief, c)
        except Exception as e:
            print(f"  [FAIL] generate_article({brief['primary_keyword']}): {e}"); ok = False; continue
        out = c["_paths"]["content"]; out.mkdir(parents=True, exist_ok=True)
        (out / f"{brief['slug']}.md").write_text(md)
        ok &= report(f"STAGE 3 {brief['slug']}.md", check_article(md, brief, c)); n += 1
    ok &= n > 0
    print("STAGE 3:", "PASS" if ok else "FAIL")
    return ok

if __name__ == "__main__":
    sys.exit(0 if main(*[a for a in sys.argv[1:] if not a.startswith("--")]) else 1)
