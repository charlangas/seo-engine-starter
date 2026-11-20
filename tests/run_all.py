"""Run every test file and print one line each. Paid APIs stay off unless RUN_LIVE_TESTS=1.

    .venv/bin/python tests/run_all.py                    # no spend; the stage 1-4 live tests are skipped
    RUN_LIVE_TESTS=1 .venv/bin/python tests/run_all.py   # also runs stages 1-4 (DataForSEO, SerpAPI, OpenRouter)
"""
import os
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
failed = []
for t in sorted(here.glob("test_*.py")):
    r = subprocess.run([sys.executable, str(t)], cwd=here.parent, capture_output=True, text=True, timeout=1800)
    out = (r.stdout + r.stderr).strip().splitlines()
    status = "FAIL" if r.returncode else ("SKIP" if out and "SKIP" in out[-1] else "ok")
    print(f"{status:4}  {t.name}")
    if r.returncode:
        failed.append(t.name)
        print("      " + "\n      ".join(l for l in out if "FAIL" in l or "Error" in l)[-1500:])
print(f"{len(failed)} failed" + ("" if os.environ.get("RUN_LIVE_TESTS") == "1" else " (paid APIs off)"))
sys.exit(1 if failed else 0)
