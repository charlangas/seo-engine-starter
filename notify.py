"""Desktop alerts for unattended runs. Local only: nothing leaves this machine."""
from __future__ import annotations

import subprocess
import sys

_SCRIPT = ["-e", "on run argv", "-e",
           'display notification (item 2 of argv) with title (item 1 of argv) sound name "Basso"',
           "-e", "end run"]


def send(title: str, message: str) -> bool:
    """Show a macOS notification. Never raises: an alert must not break the run it reports on."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["/usr/bin/osascript", *_SCRIPT, title[:80], message[:240]],
                       check=True, capture_output=True, timeout=15)
        return True
    except Exception as e:
        print(f"(notification not shown: {type(e).__name__})")
        return False


def run_message(s: dict) -> tuple[str, str]:
    """Title and body for one site's run summary."""
    site = s["site"]
    if s["failed"]:
        f = s["failed"][0]
        more = f" (+{len(s['failed']) - 1} more)" if len(s["failed"]) > 1 else ""
        return f"SEO engine: {site} failed", f"{f['keyword']}: {f['error']}{more}"
    if s["written"]:
        names = ", ".join(p.rsplit("/", 1)[-1] for p in s["written"])
        if s.get("publish_error"):
            return f"SEO engine: {site} wrote {len(s['written'])}, not published", s["publish_error"]
        if s.get("published"):
            return f"SEO engine: {site} wrote {len(s['written'])}", f"{names}. Pull request ready: {s['published']['pr_url']}"
        return f"SEO engine: {site} wrote {len(s['written'])}", f"{names}. Review and push the site."
    if s["skipped"]:
        return f"SEO engine: {site} wrote nothing", "Every selected keyword is already written."
    return f"SEO engine: {site} wrote nothing", "No keyword passed research. The queue is empty."
