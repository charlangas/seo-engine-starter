"""Search Console check self-verification: API-shaped fixture through a mocked transport, plus export mode."""
import json, os, shutil, sys, tempfile
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import apiclient, config as cfg, gsc, inventory
from tests_common import report

D = "https://gsc-selftest.example"
NOW = [  # page, query, clicks, impressions, ctr, position
    (f"{D}/agencies/acme/", "acme industrial", 0, 300, 0.0, 6.0),
    (f"{D}/agencies/", "industrial pump marketing", 2, 900, 0.002, 28.0),
    (f"{D}/blog/pump-marketing-guide/", "pump marketing guide", 9, 400, 0.0225, 12.0),
    (f"{D}/blog/pump-marketing-guide/", "valve marketing", 1, 120, 0.008, 9.0),
    (f"{D}/blog/valve-marketing/", "valve marketing", 1, 100, 0.01, 14.0),
    (f"{D}/blog/valve-marketing/", "tiny query", 0, 3, 0.0, 5.0),
]
PREV = [
    (f"{D}/agencies/acme/", "acme industrial", 3, 320, 0.01, 5.5),
    (f"{D}/blog/valve-marketing/", "valve marketing", 6, 400, 0.015, 7.0),
]


class FakeResp:
    status_code = 200
    def __init__(self, rows): self._rows = rows; self.text = ""
    def json(self): return {"rows": self._rows}


def main():
    tmp = Path(tempfile.mkdtemp())
    blog = tmp / "blog"; blog.mkdir()
    (blog / "pump-marketing-guide.md").write_text("---\ntitle: Pump\nslug: pump-marketing-guide\nprimaryKeyword: pump marketing guide\n---\n\n# Pump\n\nIntro.\n\nA shop like Acme Industrial handles valve marketing for distributors.\n")
    (blog / "valve-marketing.md").write_text("---\ntitle: Valve\nslug: valve-marketing\nprimaryKeyword: valve marketing\n---\n\n# Valve\n\nIntro.\n\nRead the pump marketing guide before you brief anyone.\n")
    yml = tmp / "site.yaml"
    yml.write_text(f"site_name: GSC Selftest\ndomain: gsc-selftest.example\nbusiness_type: agency\nniche_description: test\n"
                   f"seed_keywords: [pump marketing]\noutput_directory: {blog}\nenable_directory: false\n")
    c = cfg.load_config(yml)
    data_dir = c["_paths"]["data"]
    originals = {f: f.read_text() for f in blog.glob("*.md")}
    for kw, slug in (("pump marketing guide", "pump-marketing-guide"), ("valve marketing", "valve-marketing"),
                     ("industrial pump marketing", "industrial-pump-marketing")):
        inventory.add_entry(c, kw, slug, type="article", status="written", date="2026-01-01")

    seen = []
    def fake(method, url, **kw):
        body = kw["json"]; seen.append({"url": url, "auth": kw["headers"]["Authorization"], **body})
        src = NOW if body["startDate"] >= "2026-10-01" else PREV
        dims = body["dimensions"]
        if dims == ["page", "query"]:
            rows = [{"keys": [p, q], "clicks": cl, "impressions": im, "ctr": ct, "position": po} for p, q, cl, im, ct, po in src]
        elif dims == ["query"]:
            rows = [{"keys": [q], "clicks": cl, "impressions": im, "ctr": ct, "position": po} for _, q, cl, im, ct, po in src]
        else:
            rows = [{"keys": [p], "clicks": cl, "impressions": im, "ctr": ct, "position": po} for p, _, cl, im, ct, po in src]
        return FakeResp(rows)

    real_request, real_token = apiclient.requests.request, gsc._token
    apiclient.requests.request, gsc._token = fake, lambda: "fake-token"
    try:
        r = gsc.run_check(c, today=date(2026, 11, 15))
    finally:
        apiclient.requests.request, gsc._token = real_request, real_token

    md = Path(r["report_file"]).read_text()
    plan = json.loads(Path(r["interlink_plan"]["file"]).read_text())
    sd = {(x["query"], x["page"]) for x in r["striking_distance"]}
    checks = [
        ("property defaults to sc-domain:<domain>, URL-encoded", all("sc-domain%3Agsc-selftest.example" in s["url"] for s in seen), ""),
        ("windows: 28 days ending 3 days ago, then the 28 before", seen[0]["startDate"] == "2026-10-16" and seen[0]["endDate"] == "2026-11-12" and seen[1]["startDate"] == "2026-09-18" and seen[1]["endDate"] == "2026-10-15", (seen[0]["startDate"], seen[1]["startDate"])),
        ("only Search Console is called", c["_runtime"].calls == {"dataforseo": 0, "serpapi": 0, "llm": 0, "gsc": 4}, c["_runtime"].calls),
        ("striking distance keeps the ranking page", ("acme industrial", "/agencies/acme/") in sd and ("pump marketing guide", "/blog/pump-marketing-guide/") in sd, sorted(sd)),
        ("rows under the impression floor ignored", not any(x["query"] == "tiny query" for x in r["striking_distance"]), ""),
        ("low CTR on page one flagged", any(x["query"] == "acme industrial" for x in r["low_ctr"]) and not any(x["query"] == "pump marketing guide" for x in r["low_ctr"]), ""),
        ("query split across two pages flagged", [x["query"] for x in r["cannibalisation"]] == ["valve marketing"], ""),
        ("page that slipped flagged", any(d["page"] == "/blog/valve-marketing/" and d["position_before"] == 7.0 for d in r["declines"]) and not any(d["page"] == "/agencies/acme/" for d in r["declines"]), r["declines"]),
        ("article competing with another page flagged with that page", any(x["article_keyword"] == "industrial pump marketing" and x["ranking_page"] == "/agencies/" for x in r["inventory_conflicts"]), ""),
        ("article ranking for its own keyword is not a conflict", not any(x["article_keyword"] == "pump marketing guide" for x in r["inventory_conflicts"]), ""),
        ("link plan targets a page that is not Markdown", any(p["target_url"] == "/agencies/acme/" and p["anchor"] == "Acme Industrial" for p in plan), [(p["anchor"], p["target_url"]) for p in plan]),
        ("no self links in plan", all(p["source_url"] != p["target_url"] for p in plan), ""),
        ("plan is not applied", all(f.read_text() == t for f, t in originals.items()), ""),
        ("report, latest.json and page+query CSV written", Path(r["report_file"]).exists() and (data_dir / "gsc" / "latest.json").exists() and (data_dir / "gsc" / "page_query_2026-11-15.csv").exists(), ""),
        ("page+query CSV loads in the interlink command", len(__import__("interlink").load_gsc(data_dir / "gsc" / "page_query_2026-11-15.csv")) == len(NOW), ""),
        ("research guard sees the owning page", any(o["query"] == "industrial pump marketing" and o["page"] == "/agencies/" for o in gsc.owners_for_research(c)), ""),
        ("report has no placeholders", "{" not in md and "TODO" not in md, ""),
    ]

    # Export mode: no page + query rows, so pair-only sections say so instead of guessing.
    exp = tmp / "export"; exp.mkdir()
    (exp / "Queries.csv").write_text("Top queries,Clicks,Impressions,CTR,Position\nindustrial pump marketing,1,500,0.2%,30.5\nacme industrial,0,80,0%,5.9\n")
    (exp / "Pages.csv").write_text("Top pages,Clicks,Impressions,CTR,Position\nhttps://gsc-selftest.example/agencies/,1,900,0.11%,40\n")
    (exp / "Filters.csv").write_text("Filter,Value\nSearch type,Web\nDate,Last 3 months\n")
    e = gsc.run_check(cfg.load_config(yml), export=exp, today=date(2026, 11, 16))
    emd = Path(e["report_file"]).read_text()
    checks += [
        ("export: query-level sections filled", [x["query"] for x in e["striking_distance"]] == ["acme industrial"] and e["period"] == "Last 3 months", ""),
        ("export: no page is guessed for a query", all("page" not in x for x in e["striking_distance"] + e["weak_rankings"]) and e["interlink_plan"] is None, ""),
        ("export: pair-only sections say they need the API", emd.count("Needs page + query rows") == 2, ""),
        ("export: guard is off without page + query rows", gsc.owners_for_research(cfg.load_config(yml)) == [], ""),
    ]

    os.environ["GSC_SERVICE_ACCOUNT_FILE"] = ""
    try:
        gsc.fetch_live(cfg.load_config(yml))
        checks.append(("missing service-account key fails clearly", False, ""))
    except cfg.ConfigError as err:
        checks.append(("missing service-account key fails clearly", "GSC_SERVICE_ACCOUNT_FILE" in str(err), ""))
    os.environ["GSC_SERVICE_ACCOUNT_FILE"] = str(tmp / "nope.json")
    try:
        gsc.fetch_live(cfg.load_config(yml))
        checks.append(("missing key file fails clearly", False, ""))
    except cfg.ConfigError as err:
        checks.append(("missing key file fails clearly", "does not exist" in str(err), ""))

    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.rmtree(cfg.ROOT / "logs" / "gsc-selftest", ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return report("SEARCH CONSOLE CHECK (mocked API + export)", checks)

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
