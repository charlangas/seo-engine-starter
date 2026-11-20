"""Hand finished articles to the site's git repository as a pull request.

Config (optional, per site):

    publish:
      repo: "~/Documents/ChatGPT/industrial-marketing-site"   # the site checkout
      base_branch: main
      branch_prefix: "content/"
      checks:                      # run on the new branch; any failure keeps the branch local
        - "npm run build"
        - "npm run test:architecture"
      ship_mode: pr                # pr (default): open the pull request and stop
                                   # merge_when_checks_pass: merge it once the checks above (and, with
                                   # wait_for_remote_checks, the repository's required CI) pass
      merge_method: squash         # squash | merge | rebase
      wait_for_remote_checks: false  # true: `gh pr merge --auto` (GitHub merges after required CI passes)

`prepare` runs before the pipeline spends anything: the checkout must be on the base branch with no
uncommitted tracked changes, and is fast-forwarded to origin. `open_pr` runs after the articles are
written: it moves exactly the engine's files (articles plus the route inventory) onto a new branch,
runs the checks, pushes and opens the pull request. It never commits to the base branch directly. With
`ship_mode: merge_when_checks_pass` it then merges that pull request (only after every local check passed), so
unattended runs publish without a person; without it, the pull request waits for review.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml


class PublishError(Exception):
    pass


SHIP_MODES = ("pr", "merge_when_checks_pass")
MERGE_METHODS = ("squash", "merge", "rebase")
KEYS = {"repo", "base_branch", "branch_prefix", "checks", "ship_mode", "merge_method", "wait_for_remote_checks"}


def _cfg(config) -> dict | None:
    return config.get("publish") or None


def validate(raw) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["publish must be a mapping"]
    errs = []
    if set(raw) - KEYS:
        errs.append(f"publish: unknown key(s) {sorted(set(raw) - KEYS)}; allowed: {sorted(KEYS)}")
    if not raw.get("repo"):
        errs.append("publish.repo is required")
    if raw.get("ship_mode", "pr") not in SHIP_MODES:
        errs.append(f"publish.ship_mode must be one of {SHIP_MODES}")
    if raw.get("merge_method", "squash") not in MERGE_METHODS:
        errs.append(f"publish.merge_method must be one of {MERGE_METHODS}")
    if not isinstance(raw.get("checks") or [], list):
        errs.append("publish.checks must be a list of shell commands")
    return errs


def auto_merges(config) -> bool:
    return (_cfg(config) or {}).get("ship_mode", "pr") == "merge_when_checks_pass"


def _repo(config) -> Path:
    repo = Path(str(_cfg(config)["repo"])).expanduser()
    if not (repo / ".git").exists():
        raise PublishError(f"publish.repo is not a git checkout: {repo}")
    return repo


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        raise PublishError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def _dirty(repo: Path) -> list[str]:
    """Tracked files with uncommitted changes (untracked files do not block a run)."""
    return [l[2:].lstrip() for l in _git(repo, "status", "--porcelain", "--untracked-files=no").splitlines()]


def prepare(config) -> None:
    """Raise PublishError unless the site checkout can take this run's output. Costs nothing."""
    if not _cfg(config):
        return
    repo, base = _repo(config), _cfg(config).get("base_branch", "main")
    on = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if on != base:
        raise PublishError(f"site checkout is on '{on}', not '{base}'. Switch it back to '{base}'.")
    dirty = _dirty(repo)
    if dirty:
        raise PublishError(f"site checkout has uncommitted changes ({', '.join(dirty[:3])}"
                           f"{'…' if len(dirty) > 3 else ''}). Commit or stash them; the run was skipped.")
    _git(repo, "fetch", "origin", base)
    if _git(repo, "rev-list", "--count", f"origin/{base}..HEAD") != "0":
        raise PublishError(f"site '{base}' has local commits that are not on origin. Push them first.")
    _git(repo, "merge", "--ff-only", f"origin/{base}")


def _title(path: Path) -> str:
    m = re.search(r"^title:\s*(.+?)\s*$", path.read_text(encoding="utf-8"), re.M)
    if not m:
        return path.stem
    raw = m.group(1)
    try:
        val = yaml.safe_load(raw)  # unescapes 'Buyer''s' and "\"quoted\"" the way the site's parser does
        return str(val) if val else path.stem
    except yaml.YAMLError:
        return raw.strip("'\"")


def _tail(text: str, n: int = 6) -> str:
    return " | ".join(l.strip() for l in text.strip().splitlines()[-n:] if l.strip())[:400]


def open_pr(config, summary: dict) -> dict:
    """Branch, check, push and open the pull request. Returns {'pr_url', 'branch'} or raises PublishError."""
    pub = _cfg(config)
    repo, base = _repo(config), pub.get("base_branch", "main")
    written = [Path(p) for p in summary["written"]]
    inv = (config.get("route_inventory") or {}).get("file")
    rels = [str(p.resolve().relative_to(repo.resolve())) for p in written]
    allowed = set(rels)
    if inv:
        try:
            allowed.add(str(Path(inv).expanduser().resolve().relative_to(repo.resolve())))
        except ValueError:
            pass
    stray = [f for f in _dirty(repo) if f not in allowed]
    if stray:
        raise PublishError(f"unexpected changes in the site checkout ({', '.join(stray[:3])}); nothing was published")

    branch = f"{pub.get('branch_prefix', 'content/')}{config['_runtime'].run_id.lower().rstrip('z')}"
    titles = [_title(p) for p in written]
    verb = summary.get("action", "add")
    msg = f"Blog: {verb} {titles[0]}" if len(titles) == 1 else f"Blog: {verb} {len(titles)} articles"
    note = ("Internal links added to existing pages (anchor text unchanged, only wrapped in a link). Merge to publish."
            if verb == "link" else "Title and meta description rewritten for click-through; body unchanged. Merge to publish."
            if verb == "retitle" else "Re-researched against current search results and competitor pages; pubDate kept, updatedDate set to "
            "today. Merge to publish." if verb == "refresh" else
            "The article is dated to its scheduled publication date; merge to publish.")
    _git(repo, "switch", "-c", branch)
    try:
        _git(repo, "add", "--", *sorted(allowed & (set(rels) | set(_dirty(repo)))))
        _git(repo, "commit", "-q", "-m", msg + "\n\n" + "\n".join(f"- {t}" for t in titles))
        for cmd in pub.get("checks") or []:
            r = subprocess.run(cmd, cwd=repo, shell=True, capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                raise PublishError(f"check failed on branch {branch} (kept local, not pushed): `{cmd}`: "
                                   f"{_tail(r.stdout + chr(10) + r.stderr)}")
        _git(repo, "push", "-u", "origin", branch)
        body = "\n".join(
            [f"- {t} (`{r}`)" for t, r in zip(titles, rels)]
            + ["", f"Checks passed: {', '.join(pub.get('checks') or ['none configured'])}.",
               note])
        r = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", msg, "--body", body],
                           cwd=repo, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise PublishError(f"pushed {branch} but could not open the pull request: {_tail(r.stderr)}")
        out = {"pr_url": r.stdout.strip().splitlines()[-1], "branch": branch, "merged": False}
        if auto_merges(config):
            out.update(merge(repo, out["pr_url"], pub))
        return out
    finally:
        _git(repo, "switch", base, check=False)


def merge(repo: Path, pr_url: str, pub: dict) -> dict:
    """Merge a pull request whose local checks passed. Never raises: a failed merge leaves the PR open for a person."""
    method = pub.get("merge_method", "squash")
    cmd = ["gh", "pr", "merge", pr_url, f"--{method}", "--delete-branch"]
    if pub.get("wait_for_remote_checks"):
        cmd.append("--auto")  # GitHub merges once the repository's required checks pass
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return {"merged": False, "merge_error": _tail(r.stderr or r.stdout)}
    if pub.get("wait_for_remote_checks"):
        return {"merged": False, "auto_merge": True}
    _git(repo, "switch", pub.get("base_branch", "main"), check=False)
    _git(repo, "pull", "--ff-only", "origin", pub.get("base_branch", "main"), check=False)
    return {"merged": True}
