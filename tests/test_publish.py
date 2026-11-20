"""publish.py against throwaway git repos and a stand-in `gh`. No network, no real repo, no cost."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import publish
from tests_common import report


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_site(tmp):
    origin, site = tmp / "origin.git", tmp / "site"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(site)], check=True, capture_output=True)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        git(site, "config", k, v)
    (site / "src/config").mkdir(parents=True); (site / "src/content/blog").mkdir(parents=True)
    (site / "src/config/search-indexing.json").write_text('{"routes": []}\n')
    git(site, "add", "-A"); git(site, "commit", "-q", "-m", "init"); git(site, "push", "-q", "origin", "HEAD:main")
    git(site, "branch", "-u", "origin/main") if False else None
    return origin, site


def cfg(site, checks):
    return {"publish": {"repo": str(site), "base_branch": "main", "checks": checks},
            "route_inventory": {"file": str(site / "src/config/search-indexing.json")},
            "_runtime": SimpleNamespace(run_id="20261117T140000Z")}


def write_article(site, slug="demo-post"):
    f = site / "src/content/blog" / f"{slug}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\ntitle: 'Demo Post'\n---\nbody\n")
    inv = site / "src/config/search-indexing.json"
    inv.write_text(inv.read_text().replace("[]", f'[{{"path": "/blog/{slug}/"}}]'))
    return f


def main():
    ok = True
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        fake = tmp / "bin"; fake.mkdir()
        (fake / "gh").write_text('#!/bin/sh\necho "https://github.com/example/site/pull/7"\n'); (fake / "gh").chmod(0o755)
        os.environ["PATH"] = f"{fake}:{os.environ['PATH']}"
        origin, site = make_site(tmp)

        c = cfg(site, ["true"])
        publish.prepare(c)
        a = write_article(site)
        r = publish.open_pr(c, {"written": [str(a)]})
        on_origin = git(origin, "branch", "--list").split()
        checks = [("returns the pull request URL", r["pr_url"].endswith("/pull/7"), r["pr_url"]),
                  ("branch pushed to origin", r["branch"] in on_origin, on_origin),
                  ("checkout back on main", git(site, "rev-parse", "--abbrev-ref", "HEAD") == "main", ""),
                  ("main checkout is clean", git(site, "status", "--porcelain", "--untracked-files=no") == "", ""),
                  ("article is on the branch, not on main",
                   "demo-post.md" in git(site, "ls-tree", "-r", "--name-only", r["branch"]) and not (site / "src/content/blog/demo-post.md").exists(), ""),
                  ("route inventory change is in the commit", "demo-post" in git(site, "show", f"{r['branch']}:src/config/search-indexing.json"), "")]
        ok &= report("PR opened", checks)

        # a failing check keeps the work on a local branch and leaves main clean
        c2 = cfg(site, ["false"]); c2["_runtime"] = SimpleNamespace(run_id="20261118T140000Z"); a2 = write_article(site, "second-post")
        try:
            publish.open_pr(c2, {"written": [str(a2)]}); failed = False
        except publish.PublishError as e:
            failed = "check failed" in str(e)
        local = git(site, "branch", "--list", "content/*").split()
        ok &= report("failing check", [("raises PublishError", failed, ""),
                     ("work preserved on a local branch", len(local) >= 2, local),
                     ("failed branch not pushed", len(git(origin, "for-each-ref", "refs/heads", "--format=%(refname)").split()) == 2, ""),
                     ("main clean afterwards", git(site, "status", "--porcelain", "--untracked-files=no") == "", "")])

        # prepare refuses a dirty checkout and an unexpected change is never swept into a PR
        (site / "src/config/search-indexing.json").write_text('{"routes": [1]}\n')
        try:
            publish.prepare(c); dirty_blocked = False
        except publish.PublishError as e:
            dirty_blocked = "uncommitted" in str(e)
        git(site, "checkout", "--", "."); 
        # fast-forward when origin moved ahead (a merged pull request)
        other = tmp / "other"; subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
            git(other, "config", k, v)
        (other / "README.md").write_text("x"); git(other, "add", "-A"); git(other, "commit", "-q", "-m", "m"); git(other, "push", "-q", "origin", "HEAD:main")
        publish.prepare(c)
        ok &= report("prepare", [("blocks on uncommitted tracked changes", dirty_blocked, ""),
                     ("fast-forwards to origin", (site / "README.md").exists(), "")])
    print("PUBLISH:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
