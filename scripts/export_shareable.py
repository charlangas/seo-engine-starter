"""Copy the engine into a new folder with no site data, no private configs and no git history, ready to share.

    .venv/bin/python scripts/export_shareable.py ~/Documents/seo-engine-share [--license-holder "Your Name"]

Copied: the code, tests, README, QUICKSTART, LICENSE, .env.example, requirements and scripts, plus the two example
configs.
Left out: data/ (except the test-minimal fixtures), logs, caches, spend logs, .env, every other config, launchd/ (scripts/install_autopilot.sh writes
a plist for the new owner's machine) and the git history.

Before it finishes, it searches every copied file for the names, domains and data folders of the configs it left out,
your home folder path and your git name and email. Any hit stops the export and lists the lines, so nothing private
ships by accident. Re-run it after engine changes to refresh the shared copy (into a new or empty folder).
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
KEEP_CONFIGS = {"configs/example-local-services.yaml", "configs/test-minimal.yaml"}
KEEP_DATA = ("data/test-minimal/",)
DROP_PREFIXES = ("data/", "launchd/", ".claude/", "logs/", "cache/", "dry-run/", "output/", "spend/")
GITIGNORE = """.env
.venv/
__pycache__/
*.pyc
.DS_Store
cache/
logs/
dry-run/
output/
spend/
data/*
!data/test-minimal/
# your site configs stay local; the examples are shared
configs/*
!configs/example-local-services.yaml
!configs/test-minimal.yaml
"""
MIT = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def files_to_copy() -> list[str]:
    listed = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    out = []
    for f in listed:
        if f.startswith(KEEP_DATA):
            pass   # test fixtures (saved briefs) some offline tests read
        elif f.startswith(DROP_PREFIXES) or f == ".gitignore" or not (ROOT / f).is_file():
            continue
        if f.startswith("configs/") and f not in KEEP_CONFIGS:
            continue
        out.append(f)
    return sorted(out)


def private_terms(license_holder: str | None) -> list[str]:
    """Strings that must not appear in the shared copy."""
    terms = set()
    for cfg in (ROOT / "configs").glob("*.y*ml"):
        if f"configs/{cfg.name}" in KEEP_CONFIGS:
            continue
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        for k in ("site_name", "domain"):
            if raw.get(k):
                terms.add(str(raw[k]).strip())
        terms.add(cfg.stem)
        brand = (raw.get("comparisons") or {}).get("brand") if isinstance(raw.get("comparisons"), dict) else None
        if isinstance(brand, str) and brand.strip():
            terms.add(brand.strip())
    for d in (ROOT / "data").iterdir() if (ROOT / "data").exists() else []:
        if d.is_dir() and f"data/{d.name}/" not in KEEP_DATA:
            terms.add(d.name)
    terms.add(str(Path.home()))
    for key in ("user.name", "user.email"):
        try:
            v = git("config", key).strip()
            if v:
                terms.add(v)
        except subprocess.CalledProcessError:
            pass
    if license_holder:
        terms.discard(license_holder)
    return sorted((t for t in terms if len(t) >= 4), key=str.lower)


def scan(dest: Path, terms: list[str]) -> list[str]:
    pats = [(t, re.compile(re.escape(t), re.I)) for t in terms]
    hits = []
    for f in sorted(dest.rglob("*")):
        if not f.is_file() or ".git" in f.parts or f.name == "LICENSE":
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for t, p in pats:
                if p.search(line):
                    hits.append(f"{f.relative_to(dest)}:{n}: [{t}] {line.strip()[:120]}")
    return hits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dest", help="new or empty folder for the shared copy")
    ap.add_argument("--license-holder", help="add an MIT LICENSE in this name")
    a = ap.parse_args(argv)
    dest = Path(a.dest).expanduser().resolve()
    if dest == ROOT or ROOT in dest.parents:
        print("The destination must be outside this repository.")
        return 2
    if dest.exists() and any(dest.iterdir()):
        print(f"{dest} is not empty. Pick a new folder (or empty it) so nothing old is left behind.")
        return 2
    files = files_to_copy()
    for f in files:
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, dest / f)
    (dest / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    if a.license_holder:
        (dest / "LICENSE").write_text(MIT.format(year=date.today().year, holder=a.license_holder), encoding="utf-8")
    hits = scan(dest, private_terms(a.license_holder))
    if hits:
        print(f"Copied {len(files)} files to {dest}, but found private text. Fix these in the engine, empty the folder "
              f"and export again:")
        print("\n".join("  " + h for h in hits[:60]))
        if len(hits) > 60:
            print(f"  ... and {len(hits) - 60} more")
        return 1
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dest, check=True)
    print(f"Exported {len(files)} files to {dest} (new git repository, nothing committed).")
    print("No private names, domains, paths or emails found." + ("" if (dest / "LICENSE").exists() else
          " No LICENSE: pass --license-holder to add an MIT license."))
    print("Next: review it, then  cd " + str(dest) + " && git add -A && git commit -m 'SEO engine'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
