"""Shared test helpers. Importing this module switches paid APIs off (DataForSEO, SerpAPI, OpenRouter):
a request raises instead of being sent. Set RUN_LIVE_TESTS=1 to allow them, which the stage 1-4 tests need."""
import os
import sys

LIVE = os.environ.get("RUN_LIVE_TESTS") == "1"
os.environ["SEO_ENGINE_OFFLINE"] = "0" if LIVE else "1"
if not LIVE and not os.environ.get("SEO_ENGINE_CACHE_DIR"):
    # each test process gets an empty cache, so cached real API answers never leak into mocked tests
    import tempfile
    os.environ["SEO_ENGINE_CACHE_DIR"] = tempfile.mkdtemp(prefix="seo-engine-test-cache-")
if not LIVE and not os.environ.get("SEO_ENGINE_SPEND_DIR"):
    # mocked calls never reach the real spending ledger (live tests spend real money, so they do)
    import tempfile
    os.environ["SEO_ENGINE_SPEND_DIR"] = tempfile.mkdtemp(prefix="seo-engine-test-spend-")


def minimal_config(extra: str = "", dry_run: bool = True):
    """Load configs/test-minimal.yaml plus extra YAML from a temporary folder (tests never read real site configs)."""
    import tempfile
    from pathlib import Path
    import config as cfg
    p = Path(tempfile.mkdtemp()) / "site.yaml"
    p.write_text((Path(__file__).resolve().parent.parent / "configs" / "test-minimal.yaml").read_text() + extra)
    return cfg.load_config(p, dry_run=dry_run)


def live_only(name):
    """Skip a test that spends money on paid APIs unless RUN_LIVE_TESTS=1."""
    if not LIVE:
        print(f"{name}: SKIP (makes paid API calls; run with RUN_LIVE_TESTS=1)")
        sys.exit(0)


def report(name, checks):
    ok = True
    for label, passed, detail in checks:
        ok &= bool(passed)
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f" — {detail}" if detail not in ("", None, []) else ""))
    print(f"{name}: {'PASS' if ok else 'FAIL'}")
    return ok
