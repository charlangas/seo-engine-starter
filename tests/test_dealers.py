"""Dealer records self-verification: synthetic Outscraper export, mocked websites (no network, no spend)."""
import csv, json, shutil, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dealers
from tests_common import report

COLS = ["query", "name", "type", "subtypes", "phone", "website", "address", "city", "state_code", "postal_code",
        "country_code", "latitude", "longitude", "rating", "reviews", "working_hours", "business_status",
        "place_id", "location_link", "logo", "company_insights.employees", "full_name", "title", "email", "contact_phone"]
HOURS = json.dumps({"Monday": ["7AM-5PM"], "Sunday": ["Closed"]})


def row(pid, name, typ, subtypes, website, city, st, rating=4.5, reviews=10, status="OPERATIONAL", person=None, cc="US"):
    full, title, email, cphone = person or ("", "", "", "")
    return {"query": "forklift dealer, 30301, Atlanta, GA, US", "name": name, "type": typ, "subtypes": subtypes,
            "phone": "+1 404-555-0100", "website": website, "address": f"1 Main St, {city}, {st}", "city": city,
            "state_code": st, "postal_code": "30301", "country_code": cc, "latitude": 33.7, "longitude": -84.4,
            "rating": rating, "reviews": reviews, "working_hours": HOURS, "business_status": status, "place_id": pid,
            "location_link": f"https://maps.google.com/?cid={pid}", "logo": "https://example.com/logo.jpg",
            "company_insights.employees": 120, "full_name": full, "title": title, "email": email, "contact_phone": cphone}


SITES = {
    "https://www.acmelift.com/": ("<html><body><nav><a href='/new-forklifts'>New Forklifts</a> <a href='/rentals'>Rentals</a>"
                                  "<a href='/careers'>Careers</a></nav><p>Acme Lift is your authorized Toyota forklift dealer "
                                  "in Georgia.</p><p>Authorized Tailift dealer Tailift is owned by Hyundai Group.</p>"
                                  "<p>Now offering new Linde and Baoli forklifts.</p><img alt='Raymond'></body></html>"),
    "https://www.acmelift.com/new-forklifts": "<p>Shop new and used forklifts. We also service all makes. Planned maintenance plans.</p>",
    "https://www.acmelift.com/rentals": ("<p>Short-term rentals available. Genuine OEM parts shipped fast. Scissor lifts and boom lifts for rent. We stock parts for skid steers and excavators.</p>"
                                         "<p>We proudly supply parts for trusted brands, including: Clark Hyster Yale</p>"
                                         "<div>Authorized dealer Parts and service Nissan Crown Linde and more</div>"),
    "https://www.acmelift.com/careers": "<p>Mitsubishi is mentioned here only. We are hiring golf cart techs.</p>",
    "https://crown.com/": "<p>Crown builds lift trucks.</p>",
    "https://bigcrane.com/": "<p>We lift heavy things anywhere in Georgia.</p>",
}


def fake_fetch(url):
    key = url if url in SITES else url.rstrip("/") + "/" if url.rstrip("/") + "/" in SITES else url.rstrip("/")
    html = SITES.get(key)
    return url, html


def main():
    tmp = Path(tempfile.mkdtemp())
    rows = [
        # Acme: 2 branches, each repeated per contact person, location suffix in Maps names
        row("p1", "Acme Lift, Inc. - Atlanta, GA", "Forklift dealer", "Forklift dealer, Forklift rental service",
            "https://www.acmelift.com/?utm_source=gmb", "Atlanta", "GA", 4.8, 100, person=("Ann Lee", "Owner", "ann@acmelift.com", "+1 404 555 0199")),
        row("p1", "Acme Lift, Inc. - Atlanta, GA", "Forklift dealer", "Forklift dealer, Forklift rental service",
            "https://www.acmelift.com/?utm_source=gmb", "Atlanta", "GA", 4.8, 100, person=("Bo Kim", "Sales", "bo@acmelift.com", "")),
        row("p2", "Acme Lift Savannah", "Forklift dealer", "Forklift dealer", "http://acmelift.com/", "Savannah", "GA", 4.0, 50),
        # secondary category: kept, flagged
        row("p3", "Big Crane Co", "Crane service", "Crane service, Forklift dealer", "https://bigcrane.com", "Macon", "GA"),
        # not a dealer at all: excluded
        row("p4", "Farm Store", "Farm equipment supplier", "Farm equipment supplier", "https://farm.com", "Tifton", "GA"),
        # closed: excluded
        row("p5", "Old Lift", "Forklift dealer", "Forklift dealer", "https://oldlift.com", "Augusta", "GA", status="CLOSED_PERMANENTLY"),
        # manufacturer branch: flagged
        row("p6", "Crown Lift Trucks", "Forklift dealer", "Forklift dealer", "https://crown.com/", "Duluth", "GA"),
        # no website, facebook page only
        row("p7", "Joe's Forklifts", "Forklift dealer", "Forklift dealer, Repair service", "https://facebook.com/joes", "Rome", "GA"),
        # outside the US: dropped
        row("p8", "Toronto Lift", "Forklift dealer", "Forklift dealer", "https://torontolift.ca", "Toronto", "ON", cc="CA"),
        # site refuses readers; a person pasted its pages
        row("p10", "Blocked Lift", "Forklift dealer", "Forklift dealer", "https://blockedlift.com", "Macon", "GA"),
        # in decisions.yaml exclude
        row("p9", "Skip Me Lifts", "Forklift dealer", "Forklift dealer", "https://skipme.com", "Athens", "GA"),
        # old site now part of Acme: decisions.yaml merge
        row("p11", "Acme Lift - Augusta", "Forklift dealer", "Forklift dealer", "https://oldacme.com", "Augusta", "GA"),
    ]
    export = tmp / "Outscraper-20261108120000abc.csv"
    with open(export, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    data = tmp / "data"
    (data / "t" / "dealers").mkdir(parents=True)
    (data / "t" / "dealers" / "decisions.yaml").write_text(
        "exclude: {skipme.com: owner asked}\nnames: {bigcrane.com: Big Crane Company}\ninclude: [bigcrane.com]\n"
        "websites: {bigcrane.com: 'https://newcrane.com/'}\nnotes: {bigcrane.com: Formerly Big Crane.}\n"
        "merge: {oldacme.com: acmelift.com}\n")
    (data / "t" / "dealers" / "pages").mkdir()
    (data / "t" / "dealers" / "pages" / "blockedlift.com.md").write_text(
        "=== https://blockedlift.com/ ===\n* [New Equipment](https://blockedlift.com/new/)\n"
        "We are proud to offer JLG, GENIE, CLARK, BYD, and Hoist among other brands.\n"
        "We offer service and parts for most makes and models.\n"
        "Popular Brands We Carry Toyota Hyster Yale Crown Komatsu Mitsubishi\n"
        "[2021 BYD ECB25](https://blockedlift.com/x?page=1&s=(Sort%20By)&d=D)\n\n"
        "=== https://blockedlift.com/new/ ===\nManufacturer\n Komatsu(4)\n Doosan(1)\n"
        "2025 HC Forklift (HANGCHA) CPYD25\nCondition:\nNew\nCapacity: 5000\n"
        "Planned maintenance plans available for every unit we sell today.\n"
        "2019 Toyota 8FGU25\nCondition:\nUsed\n")

    s = dealers.build("t", "forklifts", [export], crawl=True, max_pages=8, fetch=fake_fetch, data_root=data)
    recs = {p.stem: json.loads(p.read_text()) for p in (data / "t" / "dealers" / "records").glob("*.json")}
    acme = recs.get("acme-lift", {})
    raw_public = "".join(p.read_text() for p in (data / "t" / "dealers" / "records").glob("*.json"))
    contacts = json.loads((data / "t" / "private" / "contacts.json").read_text())
    excl = {e["name"]: e["reason"] for e in s["excluded"]}
    summary_md = (data / "t" / "dealers" / "summary.md").read_text()

    blocked = recs.get("blocked-lift", {})
    crane = recs.get("big-crane-company", {})
    site_data = tmp / "site-dealers"
    dealers.export(data / "t" / "dealers" / "records", site_data)
    exported = {p.name for p in site_data.glob("*.json") if not p.name.startswith("_")}
    exp_acme = json.loads((site_data / "acme-lift.json").read_text()) if (site_data / "acme-lift.json").exists() else {}
    exp_labels = json.loads((site_data / "_equipment.json").read_text())
    ok = report("dealers", [
        ("contact duplicates merge into locations", s["rows"] == 12 and s["locations"] == 10, f"{s['rows']} rows, {s['locations']} locations"),
        ("branches group by domain into one company", acme.get("locationCount") == 3, sorted(recs)),
        ("decisions.yaml merge lists a location under another company", "Augusta, GA" in acme.get("cities", [])
         and not any("old-acme" in k for k in recs), (acme.get("cities"), sorted(recs))),
        ("location suffix and legal suffix handled in name", acme.get("name") == "Acme Lift", acme.get("name")),
        ("tracking parameters stripped from website", acme.get("website") == "https://www.acmelift.com/", acme.get("website")),
        ("closed place excluded", "business status" in excl.get("Old Lift", ""), excl),
        ("non-dealer category excluded", "not a forklift dealer" in excl.get("Farm Store", ""), excl),
        ("non-US place dropped", not any("toronto" in k for k in recs), sorted(recs)),
        ("decisions.yaml exclude applied", "decisions.yaml" in excl.get("Skip Me Lifts", ""), excl),
        ("decisions.yaml name applied", "big-crane-company" in recs, sorted(recs)),
        ("secondary category flagged, not excluded", any("secondary" in f for f in crane.get("resolvedFlags", [])), ""),
        ("decisions.yaml include resolves flags", crane.get("flags") == [] and crane.get("resolvedFlags"), crane.get("flags")),
        ("decisions.yaml website and note applied", crane.get("website") == "https://newcrane.com/"
         and crane.get("note") == "Formerly Big Crane.", (crane.get("website"), crane.get("note"))),
        ("pasted page stands in for a blocked site", blocked.get("pastedPages") and not any("could not be read" in f for f in blocked.get("flags", [])), blocked.get("flags")),
        ("brand list after a selling verb is carried", {"Clark", "BYD"} <= set(blocked.get("brands", [])), blocked.get("brands")),
        ("new inventory unit is carried, used unit is not", "Hangcha" in blocked.get("brands", [])
         and "Toyota" not in blocked.get("brands", []), blocked.get("brands")),
        ("a run of five or more brands is not carried", not {"Hyster", "Yale", "Crown"} & set(blocked.get("brands", [])), blocked.get("brands")),
        ("links with brackets in the URL are stripped", "Sort%20By" not in json.dumps(blocked.get("evidence")), ""),
        ("filter list alone is only mentioned", "Komatsu" in blocked.get("brandsMentioned", []), blocked.get("brandsMentioned")),
        ("pasted evidence is labelled", blocked.get("evidence", {}).get("brands", {}).get("Clark", {}).get("source") == "pasted page", ""),
        ("offers read from pasted pages", {"New sales", "Service and repair"} <= set(blocked.get("services", [])), blocked.get("services")),
        ("manufacturer branch flagged", any("manufacturer" in f for f in recs.get("crown-lift-trucks", {}).get("flags", [])), ""),
        ("facebook is not a website", recs.get("joe-s-forklifts", {}).get("domain") is None, recs.get("joe-s-forklifts", {}).get("domain")),
        ("authorized brand counts as carried", acme.get("brands") == ["Baoli", "Linde", "Tailift", "Toyota"], acme.get("brands")),
        ("logo-only and stray brands only mentioned", "Raymond" in acme.get("brandsMentioned", [])
         and "Raymond" not in acme.get("brands", []), acme.get("brandsMentioned")),
        ("pages unlikely to name brands are not read", "Mitsubishi" not in acme.get("brands", []) + acme.get("brandsMentioned", []), ""),
        ("parts lists and brand runs are not carried brands", {"Clark", "Hyster", "Yale", "Nissan", "Crown", "Hyundai"}
         <= set(acme.get("brandsMentioned", [])), acme.get("brandsMentioned")),
        ("brand evidence keeps URL and quote", acme.get("evidence", {}).get("brands", {}).get("Toyota", {}).get("url", "").startswith("https://www.acmelift.com"), ""),
        ("offers found on website", set(acme.get("services", [])) >= {"New sales", "Used sales", "Rental", "Service and repair", "Parts"}, acme.get("services")),
        ("all-makes service found", acme.get("allMakesService") is True, ""),
        ("offer from Google category when no website", "Service and repair" in recs.get("joe-s-forklifts", {}).get("services", []), recs.get("joe-s-forklifts", {}).get("services")),
        ("no brand confirmed is a gap, not a flag", any("no brand" in f for f in crane.get("gaps", []))
         and not any("no brand" in f for f in crane.get("flags", []) + crane.get("resolvedFlags", [])), crane.get("gaps")),
        ("review-weighted rating", acme.get("rating") == 4.5 and acme.get("reviews") == 160, (acme.get("rating"), acme.get("reviews"))),
        ("lastVerified is the export date", acme.get("lastVerified") == "2026-11-08", acme.get("lastVerified")),
        ("open flags keep a record in draft", recs.get("crown-lift-trucks", {}).get("publicationStatus") == "draft"
         and acme.get("publicationStatus") == "published" and crane.get("publicationStatus") == "published", ""),
        ("profile equipment always listed", "forklifts" in acme.get("equipment", []), acme.get("equipment")),
        ("equipment from website text", "aerial-lifts" in acme.get("equipment", []), acme.get("equipment")),
        ("equipment from Google category", "cranes" in crane.get("equipment", []), crane.get("equipment")),
        ("parts for a machine is not selling it", "construction-equipment" not in acme.get("equipment", []), acme.get("equipment")),
        ("careers and blog pages don't add equipment", "utility-vehicles" not in acme.get("equipment", []), acme.get("equipment")),
        ("equipment evidence keeps the quote", "Scissor" in acme.get("evidence", {}).get("equipment", {}).get("aerial-lifts", {}).get("quote", ""), ""),
        ("export copies published records only, without review fields", exported == {f"{k}.json" for k, r in recs.items()
         if r["publicationStatus"] == "published"} and not any(k in exp_acme for k in ("flags", "gaps", "sources"))
         and "seenOn" not in json.dumps(exp_acme), sorted(exported)),
        ("export writes equipment labels", exp_labels.get("aerial-lifts") == "Aerial lifts", ""),
        ("no contact data in public records", "ann@acmelift.com" not in raw_public and "Ann Lee" not in raw_public
         and "555 0199" not in raw_public, ""),
        ("contacts kept privately, deduplicated", len(contacts.get("acme-lift", [])) == 2, contacts.get("acme-lift")),
        ("summary lists flagged and excluded", "## Flagged for review" in summary_md and "## Excluded" in summary_md, ""),
    ])
    shutil.rmtree(tmp)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
