"""Equipment dealer records from Google Maps exports (Outscraper), checked against each dealer's own website.

  python main.py dealers --site my-dealer-directory --equipment forklifts --input export.xlsx [--input more.csv]
                         [--no-crawl] [--max-pages 8]

Outscraper gives one row per contact person, so the same location appears several times, and branches of
one company appear as separate places. This step turns the raw rows into one draft record per company:

  1. Rows are merged into locations by Google place_id (later exports win).
  2. Locations are grouped into companies by website domain (by name when there is no website).
  3. Each location is checked against the equipment profile: closed places and places with no dealer
     category are excluded; secondary categories, manufacturer-owned branches and marketplaces are flagged.
  4. Each company website is read (robots.txt respected, cached 30 days) for the brands it sells and whether
     it offers new, used, rental, service and parts. Every finding keeps the page URL and the sentence.
  5. One JSON record per company goes to data/<site>/dealers/records/, all with publicationStatus: draft,
     plus summary.md / summary.json for review. Contact people, emails and personal phone numbers go only to
     data/<site>/private/contacts.json, which git ignores; they never enter a public record.

data/<site>/dealers/decisions.yaml (optional) holds a person's decisions so reruns keep them:

  exclude: {crown.com: "manufacturer branches, not a dealer"}   # domain or record slug -> reason
  include: [macscrane.com]                                      # keep despite a flag (moves to resolvedFlags)
  names: {wolterinc.com: "Wolter, Inc."}                        # display name override
  websites: {ipwlifttechs.com: "https://www.mhiusa.net/"}       # public website override
  notes: {ipwlifttechs.com: "Formerly IPW Lift Techs."}         # one line shown on the listing
  equipment: {forkliftparts.us: [forklifts]}                    # equipment categories override
  merge: {paperents.com: papemh.com}                            # list these locations under another company

Sites that refuse automated readers can be covered by pasting their pages into
data/<site>/dealers/pages/<domain>.md, with a '=== <url> ===' line before each page.
"""
from __future__ import annotations

import csv
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml

from textutil import slugify

ROOT = Path(__file__).resolve().parent
CRAWL_CACHE_DAYS = 30
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Google Maps categories that make a place a dealer for this equipment (primary), and brands to look for.
# Brand patterns are regexes; the first entry is the display name.
PROFILES = {
    "forklifts": {
        "label": "forklift dealers",
        "dealer_types": ["Forklift dealer", "Material handling equipment supplier", "Forklift rental service"],
        "nouns": r"forklifts?|fork lifts?|lift trucks?|equipment|material handling|units?|trucks?",
        "brands": {
            "Toyota": r"Toyota", "Hyster": r"Hyster", "Yale": r"Yale", "Crown": r"Crown",
            "Raymond": r"Raymond", "Clark": r"CLARK|Clark", "Cat": r"Cat®|CAT®|Cat [Ll]ift [Tt]rucks?|Cat [Ff]orklifts?|Caterpillar",
            "Mitsubishi": r"Mitsubishi", "Komatsu": r"Komatsu", "Linde": r"Linde", "Doosan": r"Doosan",
            "Bobcat": r"Bobcat", "Hyundai": r"Hyundai", "Jungheinrich": r"Jungheinrich",
            "Nissan": r"Nissan", "UniCarriers": r"UniCarriers", "TCM": r"TCM", "Combilift": r"Combilift", "Hangcha": r"Hangcha|HANGCHA|HC Forklift", "Navigator": r"Navigator|NAVIGATOR",
            "Heli": r"HELI|Heli", "EP Equipment": r"EP Equipment", "BYD": r"BYD", "Big Joe": r"Big Joe",
            "Taylor": r"Taylor Machine Works|Taylor Big Red", "Kalmar": r"Kalmar", "Sellick": r"Sellick",
            "Noblelift": r"Noblelift", "Tailift": r"Tailift", "Baoli": r"Baoli", "Blue Giant": r"Blue Giant", "Big Lift": r"Big Lift",
        },
        "manufacturer_domains": ["crown.com", "raymondcorp.com", "toyotaforklift.com", "hyster.com", "yale.com",
                                 "navigatorforklift.com"],
    },
    "air-compressors": {
        "label": "air compressor dealers",
        "dealer_types": ["Air compressor supplier", "Air compressor repair service", "Compressor supplier"],
        "nouns": r"(air )?compressors?|equipment|units?|systems?|dryers?",
        "brands": {
            "Atlas Copco": r"Atlas Copco", "Ingersoll Rand": r"Ingersoll[- ]Rand", "Kaeser": r"Kaeser",
            "Sullair": r"Sullair", "Quincy": r"Quincy", "Gardner Denver": r"Gardner Denver",
            "Sullivan-Palatek": r"Sullivan[- ]Palatek", "ELGi": r"ELGi|Elgi", "CompAir": r"CompAir",
            "Champion": r"Champion", "Curtis-Toledo": r"Curtis[- ]Toledo|FS[- ]Curtis", "Chicago Pneumatic":
            r"Chicago Pneumatic", "Hitachi": r"Hitachi", "Mattei": r"Mattei", "BOGE": r"BOGE",
            "Rogers Machinery": r"Rogers Machinery", "Saylor-Beall": r"Saylor[- ]Beall",
        },
        "manufacturer_domains": ["atlascopco.com", "ingersollrand.com", "kaeser.com", "sullair.com",
                                 "quincycompressor.com"],
    },
    "heavy-equipment": {
        "label": "heavy equipment dealers",
        "dealer_types": ["Construction machine dealer", "Heavy equipment dealer", "Construction equipment supplier",
                         "Equipment dealer"],
        "nouns": r"equipment|machines?|machinery|excavators?|loaders?|dozers?|units?",
        "brands": {
            "Cat": r"Caterpillar|Cat®|CAT®", "John Deere": r"John Deere|Deere", "Komatsu": r"Komatsu",
            "Case": r"CASE®|Case Construction|CASE CE", "Volvo": r"Volvo", "Bobcat": r"Bobcat",
            "Kubota": r"Kubota", "Takeuchi": r"Takeuchi", "JCB": r"JCB", "Hitachi": r"Hitachi",
            "Develon": r"Develon|DEVELON|Doosan", "Hyundai": r"Hyundai", "Liebherr": r"Liebherr",
            "Kobelco": r"Kobelco", "Link-Belt": r"Link[- ]Belt", "New Holland": r"New Holland",
            "Wacker Neuson": r"Wacker Neuson", "Yanmar": r"Yanmar", "SANY": r"SANY|Sany", "LiuGong": r"LiuGong",
        },
        "manufacturer_domains": [],
    },
}
# Equipment a dealer sells or rents, beyond the profile it was found under. Each category counts from a Google
# category or from the dealer's own pages (not blog or news posts), and keeps its evidence like brands do.
EQUIPMENT = {
    "forklifts": {"label": "Forklifts", "types": ["Forklift dealer", "Forklift rental service"],
                  "text": r"\bforklifts?\b|\blift trucks?\b"},
    "aerial-lifts": {"label": "Aerial lifts", "types": ["Aerial work platform rental service"],
                     "text": r"\b(?:scissor lifts?|boom lifts?|aerial (?:lifts?|work platforms?|equipment)|man ?lifts?)\b"},
    "telehandlers": {"label": "Telehandlers", "types": [], "text": r"\b(?:telehandlers?|telescopic handlers?)\b"},
    "cranes": {"label": "Cranes", "types": ["Crane dealer", "Crane rental agency", "Crane service"],
               "text": r"\b(?:cranes? (?:sales|rentals?|for sale|for rent)|(?:mobile|carry ?deck|rough terrain|crawler|truck|"
                       r"industrial) cranes?)\b"},
    "construction-equipment": {"label": "Construction equipment",
                               "types": ["Construction machine dealer", "Construction equipment supplier",
                                         "Construction machine rental service", "Heavy equipment dealer"],
                               "text": r"\b(?:excavators?|skid steers?|wheel loaders?|backhoes?|bulldozers?|"
                                       r"compact track loaders?)\b"},
    "tractors": {"label": "Tractors and farm equipment", "types": ["Farm equipment supplier", "Tractor dealer"],
                 "text": r"\b(?:farm equipment|compact tractors?|utility tractors?|farm tractors?)\b"},
    "utility-vehicles": {"label": "Golf carts and utility vehicles", "types": ["Golf cart dealer"],
                         "text": r"\b(?:golf carts?|utility vehicles?|UTVs?|burden carriers?|personnel carriers?)\b"},
    "floor-cleaning": {"label": "Floor scrubbers and sweepers", "types": [],
                       "text": r"\b(?:floor scrubbers?|floor sweepers?|scrubbers? (?:and|&) sweepers?|sweepers? (?:and|&) scrubbers?)\b"},
    "dock-and-warehouse": {"label": "Dock and warehouse equipment", "types": [],
                           "text": r"\b(?:dock (?:levelers?|equipment|doors?|seals?)|pallet racking|racking systems?|"
                                   r"conveyors?|mezzanines?)\b"},
    "air-compressors": {"label": "Air compressors", "types": ["Air compressor supplier", "Air compressor repair service"],
                        "text": r"\bair compressors?\b"},
}
PROFILE_EQUIPMENT = {"forklifts": "forklifts", "air-compressors": "air-compressors",
                     "heavy-equipment": "construction-equipment"}
NOT_OFFER_PAGES = re.compile(r"/(?:blog|news|press|articles?|posts?|careers?|jobs)(?:/|$)", re.I)
# A mention in a sentence about parts, training, where a machine is used, or someone's biography is not an offer.
EQUIPMENT_NOT_OFFERED = re.compile(r"\b(?:parts|components|training|certification|used in|such as|environments?|"
                                   r"designed for|ideal for|built to|indoor|outdoor|his|her|he|she|founded|launched|career)\b", re.I)
# Informational: not a reason to hold a listing back.
GAP_PREFIXES = ("no brand confirmed", "no website")
MARKETPLACE_DOMAINS = ["forklifttrader.com", "machinerytrader.com", "equipmenttrader.com", "ironplanet.com",
                       "machinio.com", "equipmentwatch.com", "rbauction.com", "govdeals.com"]
NOT_A_WEBSITE = ["facebook.com", "linkedin.com", "instagram.com", "yelp.com", "google.com", "sites.google.com",
                 "business.site", "yellowpages.com", "bbb.org"]
DEALER_WORDS = re.compile(r"authori[sz]ed|dealers?(?:hip)?|distributor|franchise", re.I)
SELLING_BEFORE = re.compile(r"(?i:\b(?:new|sells?|selling|offering|offer|carry|carries|carrying))\s+(?:(?i:new)\s+)?"
                            r"(?:[A-Z][\w&-]*(?:,\s*(?:and\s+|&\s+)?|\s+(?:and|&)\s+|\s+)){0,4}$")
# Inventory cards: the condition label nearest to a brand says whether the unit is new.
CONDITION = re.compile(r"Condition:?\s*\|?\s*(New|Used|Pre-?Owned|Rental)\b", re.I)
# Brands named as makes a business services or supplies parts for: not brands it sells.
SERVICE_CONTEXT = re.compile(r"parts (?:for|&|and)|service (?:for|all)|we service|makes|brands you|all brands|"
                             r"repair (?:all|any)|support the", re.I)
OFFER_SUBTYPES = {"rental": ["rental service", "rental agency"], "service": ["repair service", "maintenance service"],
                  "parts": ["parts supplier", "parts store"], "used": ["used "], "training": ["training center"]}
PAGE_HINTS = re.compile(r"brand|product|new|used|pre-?owned|rent|service|repair|maint|part|about|line|"
                        r"manufactur|equipment|inventory|forklift|compressor|machin|sales", re.I)
LEGAL_SUFFIX = re.compile(r"[,.]?\s+(inc|llc|l\.l\.c|co|corp|corporation|company|ltd)\.?$", re.I)
CONTACT_COLUMNS = ["full_name", "first_name", "last_name", "title", "email", "email.emails_validator.status",
                   "contact_phone", "contact_phones", "contact_linkedin", "contact_facebook", "contact_instagram",
                   "contact_x"]


# ---------- reading exports ----------

def read_rows(path: Path) -> list[dict]:
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        ws = load_workbook(path, read_only=True, data_only=True).worksheets[0]
        it = ws.iter_rows(values_only=True)
        head = [str(h) if h is not None else "" for h in next(it)]
        return [dict(zip(head, r)) for r in it if any(v not in (None, "") for v in r)]
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [r for r in csv.DictReader(f)]
    raise ValueError(f"{path.name}: expected an Outscraper .xlsx or .csv export")


def source_date(path: Path) -> str:
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", path.name)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            pass
    return date.fromtimestamp(path.stat().st_mtime).isoformat()


def _s(v) -> str:
    return "" if v is None or (isinstance(v, float) and v != v) else str(v).strip()


def _num(v):
    try:
        f = float(_s(v))
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _json(v):
    try:
        return json.loads(_s(v)) if _s(v) else None
    except json.JSONDecodeError:
        return None


def clean_url(url: str) -> str:
    """Drop tracking parameters. Outscraper encodes the '?' as %3F, which otherwise becomes part of the path."""
    return re.sub(r"(?:\?|%3F|#).*$", "", url, flags=re.I)


def homepage(url: str) -> str:
    p = urlparse(url if "//" in url else f"https://{url}")
    return f"{p.scheme or 'https'}://{p.netloc}/"


def domain_of(url: str) -> str:
    host = urlparse(url if "//" in url else f"//{url}").netloc.lower().split(":")[0]
    host = re.sub(r"^www\d?\.", "", host)
    return "" if not host or any(host == d or host.endswith("." + d) for d in NOT_A_WEBSITE) else host


def merge_locations(files: list[Path]) -> tuple[dict, dict]:
    """place_id -> location (public fields only) and place_id -> [contacts] (private)."""
    locs, contacts = {}, {}
    for path in sorted(files, key=source_date):  # later exports overwrite earlier ones
        seen_on = source_date(path)
        for r in read_rows(path):
            pid = _s(r.get("place_id"))
            if not pid or not _s(r.get("name")):
                continue
            hours = _json(r.get("working_hours"))
            loc = {
                "placeId": pid, "name": _s(r.get("name")), "type": _s(r.get("type")),
                "subtypes": [t.strip() for t in _s(r.get("subtypes")).split(",") if t.strip()],
                "website": clean_url(_s(r.get("website"))) or None,
                "domain": domain_of(_s(r.get("website"))),
                "phone": _s(r.get("phone")) or None, "address": _s(r.get("address")) or None,
                "street": _s(r.get("street")) or None, "city": _s(r.get("city")) or None,
                "state": _s(r.get("state_code")) or None, "postalCode": _s(r.get("postal_code")).split(".")[0] or None,
                "lat": _num(r.get("latitude")), "lng": _num(r.get("longitude")),
                "rating": _num(r.get("rating")), "reviews": _num(r.get("reviews")),
                "reviewsPerScore": _json(r.get("reviews_per_score")),
                "hours": {d: ", ".join(h) if isinstance(h, list) else h for d, h in hours.items()}
                if isinstance(hours, dict) else None,
                "mapsUrl": _s(r.get("location_link")) or None, "logo": _s(r.get("logo")) or None,
                "businessStatus": _s(r.get("business_status")) or None,
                "verifiedOnMaps": _s(r.get("verified")).lower() in ("true", "1"),
                "employees": _num(r.get("company_insights.employees")),
                "foundedYear": _num(r.get("company_insights.founded_year")),
                "chain": _s(r.get("chain_info.chain")) in ("1", "1.0", "True", "true"),
                "query": _s(r.get("query")) or None, "seenOn": seen_on, "sourceFile": path.name,
            }
            if _s(r.get("country_code")) not in ("", "US"):
                continue  # US directory
            locs[pid] = loc
            person = {k: _s(r.get(k)) for k in CONTACT_COLUMNS if _s(r.get(k))}
            if person:
                people = contacts.setdefault(pid, [])
                if person not in people:
                    people.append(person)
    return locs, contacts


# ---------- checking locations ----------

def check_location(loc: dict, profile: dict) -> tuple[str | None, list[str]]:
    """(exclusion reason or None, flags)."""
    if loc["businessStatus"] and loc["businessStatus"] != "OPERATIONAL":
        return f"business status {loc['businessStatus']}", []
    types = {t.lower() for t in profile["dealer_types"]}
    flags = []
    if loc["type"].lower() not in types:
        if not types & {t.lower() for t in loc["subtypes"]}:
            return f"not a {profile['label'][:-1]} on Google (category: {loc['type'] or 'none'})", []
        flags.append(f"main Google category is '{loc['type']}', dealer category is secondary")
    if loc["domain"] in profile["manufacturer_domains"]:
        flags.append("manufacturer-owned branch, not an independent dealer")
    if loc["domain"] in MARKETPLACE_DOMAINS:
        flags.append("marketplace or listing site, not a dealer")
    if not loc["domain"]:
        flags.append("no website")
    return None, flags


def clean_name(name: str, loc: dict) -> str:
    n = re.split(r"\s+[-–|]\s+|\s*-\s+(?=[A-Z])", name)[0].strip()
    for tail in filter(None, [f"{loc.get('city')}, {loc.get('state')}", loc.get("city"), loc.get("state")]):
        n = re.sub(rf"[\s,]+{re.escape(tail)}\.?$", "", n, flags=re.I).strip()
    n = re.sub(r"[\s,]+(?:[A-Z][a-z]+\s?){1,3},\s*[A-Z]{2}$", "", n).strip()
    return n.rstrip(" ,") or name


def _name_key(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", LEGAL_SUFFIX.sub("", n.lower()))


def company_name(locs: list[dict]) -> str:
    cleaned = [clean_name(l["name"], l) for l in locs]
    counts = Counter(_name_key(n) for n in cleaned)
    best = max(counts, key=lambda k: (counts[k], -len(k)))
    return min((n for n in cleaned if _name_key(n) == best), key=len)


# ---------- reading dealer websites ----------

def _cache_file(domain: str) -> Path:
    return ROOT / "cache" / "dealers" / f"{slugify(domain)}.json"


def _visible_text(html: str) -> tuple[str, list[tuple[str, str]]]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    links = [(a.get("href") or "", " ".join(a.get_text(" ").split())) for a in soup.find_all("a")]
    alts = [img.get("alt") or img.get("title") or "" for img in soup.find_all("img")]
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    text = " ".join(soup.get_text(" ").split())
    return (text + " | " + " | ".join(a for a in alts if a)).strip(), links


def crawl_site(website: str, max_pages: int = 8, fetch=None, refresh: bool = False) -> dict:
    """{pages: [{url, text}], error} for the homepage plus the internal pages most likely to name brands/offers."""
    domain = domain_of(website)
    cf = _cache_file(domain)
    if not refresh and fetch is None and cf.exists() and time.time() - cf.stat().st_mtime < CRAWL_CACHE_DAYS * 86400:
        return json.loads(cf.read_text(encoding="utf-8"))
    session = requests.Session()
    session.headers["User-Agent"] = UA

    def _get(url):
        if fetch:
            got, html = fetch(url)
            return got, html, 200 if html else 404
        r = session.get(url, timeout=20)
        ctype = r.headers.get("content-type", "")
        if "charset" not in ctype.lower():
            r.encoding = "utf-8"
        ok = r.status_code == 200 and "html" in ctype
        return r.url, (r.text if ok else None), r.status_code

    home = website if website.startswith("http") else f"https://{website}"
    robots = RobotFileParser()
    try:
        if fetch is None:
            rr = session.get(urljoin(home, "/robots.txt"), timeout=10)
            robots.parse(rr.text.splitlines() if rr.status_code == 200 else [])
        else:
            robots.parse([])
    except requests.RequestException:
        robots.parse([])
    out = {"domain": domain, "fetched": date.today().isoformat(), "pages": [], "error": None}
    final = html = None
    for attempt in range(2):  # one retry: a slow or busy server is common
        try:
            final, html, status = _get(home)
        except requests.RequestException as e:
            status = type(e).__name__
        if html or status in (401, 403):
            break
        if fetch is None:
            time.sleep(3)
    if not html:
        out["error"] = ("site refuses automated reading (HTTP 403)" if status in (401, 403)
                        else f"homepage did not load ({status})")
        return out
    text, links = _visible_text(html)
    out["pages"].append({"url": final, "text": text})
    host = urlparse(final).netloc.lower()
    queue, seen = [], {final.rstrip("/")}
    for href, label in links:
        u = urljoin(final, href).split("#")[0]
        p = urlparse(u)
        if p.scheme not in ("http", "https") or p.netloc.lower() != host or u.rstrip("/") in seen:
            continue
        if re.search(r"\.(pdf|jpe?g|png|gif|zip|docx?|xlsx?)$", p.path, re.I) or not PAGE_HINTS.search(p.path + " " + label):
            continue
        seen.add(u.rstrip("/"))
        queue.append(u)
    for u in queue[: max_pages - 1]:
        if not robots.can_fetch(UA, u):
            continue
        try:
            got, html, _ = _get(u)
        except requests.RequestException:
            continue
        if html:
            out["pages"].append({"url": got, "text": _visible_text(html)[0]})
        if fetch is None:
            time.sleep(0.5)
    if fetch is None:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _snippet(text: str, start: int, end: int, pad: int = 90) -> str:
    s = text[max(0, start - pad): end + pad].strip()
    return ("…" if start > pad else "") + s + ("…" if end + pad < len(text) else "")


def _sentence(text: str, start: int, end: int, cap: int = 150) -> tuple[int, int]:
    """Bounds of the sentence (or menu/alt-text segment) around a match, at most `cap` characters either side."""
    lo = max(text.rfind(c, max(0, start - cap), start) for c in ".!?|")
    hi = [i for i in (text.find(c, end, end + cap) for c in ".!?|") if i != -1]
    return (lo + 1 if lo != -1 else max(0, start - cap)), (min(hi) if hi else min(len(text), end + cap))


def _sells(text: str, start: int, end: int, all_brands) -> bool:
    """A dealer word within 40 characters of the brand, in the same sentence, where the sentence is neither a
    parts/service list ("parts for Toyota, Hyster...") nor a run of three or more brands."""
    lo, hi = _sentence(text, start, end)
    if SERVICE_CONTEXT.search(text[lo:hi]):
        return False
    if len({b.lower() for b in all_brands.findall(text[lo:hi])}) >= 5:
        return False  # "Popular brands we carry: Toyota Hyster Yale Crown Komatsu..." is used stock, not a dealership
    if SELLING_BEFORE.search(text[max(lo, start - 60): start]):
        return True  # "offering new Linde and Baoli forklifts", "proud to offer CLARK, BYD, and JLG"
    if len({b.lower() for b in all_brands.findall(text[max(lo, start - 60): min(hi, end + 60)])}) >= 3:
        return False
    a, b = max(lo, start - 40), min(hi, end + 40)
    for d in DEALER_WORDS.finditer(text, a, b):
        between = text[d.end():start] if d.end() <= start else text[end:d.start()]
        if not all_brands.search(between):  # the dealer word belongs to this brand, not a neighbour
            return True
    return False


def _new_unit(text: str, start: int, end: int) -> bool:
    """The brand starts an inventory unit title ("2025 Komatsu FG25T-16") and the next condition label after it,
    within 150 characters, is New."""
    if not re.search(r"(?:19|20)\d{2}\s*$", text[max(0, start - 6): start]):
        return False
    m = CONDITION.search(text, end, min(len(text), end + 150))
    return bool(m) and m.group(1).lower() == "new"


def find_brands(pages: list[dict], profile: dict, names: list[str]) -> dict:
    """brand -> {level: dealer|mentioned, url, quote}. 'dealer' needs a dealer word near the brand, or the brand
    in the business's own name."""
    found = {}
    all_brands = re.compile(r"(?<![A-Za-z])(?:" + "|".join(profile["brands"].values()) + r")(?![A-Za-z])")
    for brand, pat in profile["brands"].items():
        rx = re.compile(rf"(?<![A-Za-z]){pat}(?![A-Za-z])")
        for n in names:
            if rx.search(n):
                found[brand] = {"level": "dealer", "url": None, "quote": f"Business name: {n}"}
                break
        for pg in pages:
            for m in rx.finditer(pg["text"]):
                sells = _sells(pg["text"], m.start(), m.end(), all_brands) or _new_unit(pg["text"], m.start(), m.end())
                level = "dealer" if sells else "mentioned"
                cur = found.get(brand)
                if not cur or (cur["level"] == "mentioned" and level == "dealer"):
                    found[brand] = {"level": level, "url": pg["url"], "quote": _snippet(pg["text"], m.start(), m.end()),
                                    "source": "pasted page" if pg.get("pasted") else "website"}
                if level == "dealer":
                    break
    return found


def find_equipment(pages: list[dict], subtypes: set[str], base: str) -> dict:
    """category -> {source, url, quote}. The profile's own category always counts (the place passed its check)."""
    out = {base: {"source": "google", "url": None, "quote": "Listed on Google as a dealer for this equipment"}}
    low = {t.lower(): t for t in subtypes}
    for key, cat in EQUIPMENT.items():
        if key in out:
            continue
        t = next((low[x.lower()] for x in cat["types"] if x.lower() in low), None)
        if t:
            out[key] = {"source": "google", "url": None, "quote": f"Google category: {t}"}
            continue
        rx = re.compile(cat["text"], re.I)
        for pg in pages:
            if NOT_OFFER_PAGES.search(urlparse(pg["url"]).path):
                continue
            for m in rx.finditer(pg["text"]):
                lo, hi = _sentence(pg["text"], m.start(), m.end())
                if EQUIPMENT_NOT_OFFERED.search(pg["text"][lo:hi]):
                    continue
                out[key] = {"source": "pasted page" if pg.get("pasted") else "website", "url": pg["url"],
                            "quote": _snippet(pg["text"], m.start(), m.end(), 70)}
                break
            if key in out:
                break
    return out


def find_offers(pages: list[dict], profile: dict, subtypes: set[str]) -> dict:
    nouns = profile["nouns"]
    patterns = {
        "new": rf"\bnew\s+(?:and\s+used\s+)?(?:[\w-]+\s+)?(?:{nouns})\b|\bnew\s*(?:&|and|/|or)\s*(?:used|pre-?owned)\b",
        "used": rf"\b(?:used|pre-?owned|refurbished|reconditioned)\s+(?:[\w-]+\s+)?(?:{nouns})\b|"
                rf"\bnew\s*(?:&|and|/|or)\s*(?:used|pre-?owned)\b",
        "rental": r"\brentals?\b|\brent\s+(?:a|an|our)\b|\bshort[- ]term\b|\blong[- ]term\s+rental",
        "service": r"\b(?:repairs?|planned maintenance|preventive maintenance|preventative maintenance|"
                   r"service department|field service|mobile service|emergency service|service technicians?|service and repair|"
                   r"service (?:and|&) parts|parts,? (?:and |& )?service|call for service)\b",
        "parts": r"\b(?:parts department|replacement parts|OEM parts|aftermarket parts|parts and service|"
                 r"parts & service|parts sales|order parts|service (?:and|&) parts|forklift parts|call for parts)\b",
        "training": r"\b(?:operator training|OSHA[- ]compliant training|safety training|operator certification)\b",
        "allMakesService": r"\ball (?:makes|brands)\b",
    }
    out = {}
    for key, pat in patterns.items():
        rx = re.compile(pat, re.I)
        for pg in pages:
            m = rx.search(pg["text"])
            if m:
                out[key] = {"source": "pasted page" if pg.get("pasted") else "website", "url": pg["url"], "quote": _snippet(pg["text"], m.start(), m.end(), 70)}
                break
        if key not in out:
            for t in subtypes:
                if any(w in t.lower() for w in OFFER_SUBTYPES.get(key, [])):
                    out[key] = {"source": "google", "url": None, "quote": f"Google category: {t}"}
                    break
    return out


def load_pasted(pages_dir: Path, domain: str | None) -> list[dict]:
    """Page text a person pasted for a site that refuses automated readers: data/<site>/dealers/pages/<domain>.md,
    one '=== <url> ===' line before each page. Markdown links keep their text; each line becomes a segment."""
    if not domain:
        return []
    f = next((pages_dir / f"{domain}{ext}" for ext in (".md", ".txt") if (pages_dir / f"{domain}{ext}").is_file()), None)
    if not f:
        return []
    out = []
    for m in re.finditer(r"^=== (\S+) ===\s*$(.*?)(?=^=== \S+ ===\s*$|\Z)", f.read_text(encoding="utf-8"), re.M | re.S):
        body = re.sub(r"!?\[([^\]]*)\]\((?:[^()\s]|\([^()\s]*\))*\)", r"\1", m.group(2)).replace("&amp;", "&")
        lines = [re.sub(r"^\s*[*-]\s+", "", ln).strip() for ln in body.splitlines()]
        out.append({"url": m.group(1), "text": " | ".join(ln for ln in lines if ln), "pasted": True})
    return out


# ---------- building records ----------

def build(site: str, equipment: str, inputs: list[Path], crawl: bool = True, max_pages: int = 8,
          fetch=None, data_root: Path | None = None) -> dict:
    profile = PROFILES[equipment]
    base = (data_root or ROOT / "data") / site
    out_dir, private = base / "dealers", base / "private"
    decisions_file = out_dir / "decisions.yaml"
    decisions = (yaml.safe_load(decisions_file.read_text(encoding="utf-8")) or {}) if decisions_file.exists() else {}
    dec_excl, dec_incl, dec_names = decisions.get("exclude") or {}, set(decisions.get("include") or []), decisions.get("names") or {}
    dec_sites, dec_notes = decisions.get("websites") or {}, decisions.get("notes") or {}
    dec_equipment = decisions.get("equipment") or {}
    dec_merge = decisions.get("merge") or {}

    locs, contacts = merge_locations(inputs)
    excluded, groups = [], {}
    for loc in locs.values():
        reason, flags = check_location(loc, profile)
        if reason:
            excluded.append({"name": loc["name"], "city": loc["city"], "state": loc["state"], "reason": reason})
            continue
        loc["_flags"] = flags
        key = loc["domain"] or f"name:{_name_key(clean_name(loc['name'], loc))}"
        key = dec_merge.get(key, key)
        groups.setdefault(key, []).append(loc)

    companies = []
    for key, members in groups.items():
        domain = key if not key.startswith("name:") else None
        name = dec_names.get(key) or company_name(members)
        companies.append({"key": key, "domain": domain, "name": name, "locations": members})

    pages_by_domain = {}
    if crawl:
        sites = {c["domain"]: homepage(next(l["website"] for l in c["locations"] if l["website"]))
                 for c in companies if c["domain"]}
        with ThreadPoolExecutor(max_workers=6) as pool:
            for d, res in zip(sites, pool.map(lambda w: crawl_site(w, max_pages, fetch), sites.values())):
                pages_by_domain[d] = res

    used_slugs, records, private_out = set(), [], {}
    for c in sorted(companies, key=lambda c: c["name"].lower()):
        members = sorted(c["locations"], key=lambda l: (l["state"] or "", l["city"] or ""))
        slug = slugify(c["name"])
        if slug in used_slugs:
            slug = slugify(f"{c['name']} {members[0]['state'] or ''}")
        used_slugs.add(slug)
        flags = sorted({f for l in members for f in l["_flags"]})
        other = sorted({l["name"] for l in members
                        if not _name_key(clean_name(l["name"], l)).startswith(_name_key(c["name"]))})
        if other:
            flags.append("a location trades under another name: " + ", ".join(other))
        crawl_res = pages_by_domain.get(c["domain"]) if c["domain"] else None
        crawled = (crawl_res or {}).get("pages") or []
        pasted = load_pasted(out_dir / "pages", c["domain"])
        pages = crawled + pasted
        landed = domain_of(crawled[0]["url"]) if crawled else None
        moved_to = None
        if landed and c["domain"] and landed != c["domain"]:
            a, b = (re.sub(r"[^a-z]", "", d.split(".")[0]) for d in (landed, c["domain"]))
            if a.startswith(b) or b.startswith(a):
                moved_to = homepage(crawled[0]["url"])  # same name, new domain: just use it
            else:
                flags.append(f"website now redirects to {landed} (acquired or renamed?)")
        if crawl and c["domain"] and not pages:
            flags.append(f"website could not be read ({(crawl_res or {}).get('error') or 'no pages'}); "
                         f"paste its pages into dealers/pages/{c['domain']}.md")
        brands = find_brands(pages, profile, [l["name"] for l in members])
        offers = find_offers(pages, profile, {t for l in members for t in l["subtypes"]})
        equipment_found = find_equipment(pages, {t for l in members for t in l["subtypes"]}, PROFILE_EQUIPMENT[equipment])
        dealer_brands = sorted(b for b, v in brands.items() if v["level"] == "dealer")
        if crawl and pages and not dealer_brands:
            flags.append("no brand confirmed as carried on the website")
        gaps = [f for f in flags if f.startswith(GAP_PREFIXES)]
        flags = [f for f in flags if not f.startswith(GAP_PREFIXES)]
        if (c["key"] in dec_excl) or (slug in dec_excl):
            excluded.append({"name": c["name"], "city": None, "state": None,
                             "reason": f"decisions.yaml: {dec_excl.get(c['key']) or dec_excl.get(slug)}"})
            continue
        resolved = []
        if c["key"] in dec_incl or slug in dec_incl:
            flags, resolved = [], flags
        rated = [l for l in members if l["rating"] is not None and l["reviews"]]
        total_reviews = sum(l["reviews"] for l in rated)
        services = [label for key, label in (("new", "New sales"), ("used", "Used sales"), ("rental", "Rental"),
                                             ("service", "Service and repair"), ("parts", "Parts"),
                                             ("training", "Operator training")) if key in offers]
        rec = {
            "name": c["name"], "slug": slug, "publicationStatus": "draft" if flags else "published",
            "equipment": dec_equipment.get(c["key"]) or dec_equipment.get(slug) or
                         [k for k in EQUIPMENT if k in equipment_found],
            "website": dec_sites.get(c["key"]) or dec_sites.get(slug) or moved_to or (
                homepage(next(l["website"] for l in members if l["website"])) if c["domain"] else None),
            "note": dec_notes.get(c["key"]) or dec_notes.get(slug),
            "domain": c["domain"], "brands": dealer_brands,
            "brandsMentioned": sorted(b for b, v in brands.items() if v["level"] == "mentioned"),
            "services": services, "allMakesService": "allMakesService" in offers,
            "states": sorted({l["state"] for l in members if l["state"]}),
            "cities": sorted({f"{l['city']}, {l['state']}" for l in members if l["city"]}),
            "locationCount": len(members),
            "rating": round(sum(l["rating"] * l["reviews"] for l in rated) / total_reviews, 1) if total_reviews else None,
            "reviews": total_reviews or None,
            "employees": max((l["employees"] for l in members if l["employees"]), default=None),
            "foundedYear": min((l["foundedYear"] for l in members if l["foundedYear"]), default=None),
            "logo": next((l["logo"] for l in members if l["logo"]), None),
            "locations": [{k: v for k, v in l.items() if not k.startswith("_") and k not in
                           ("employees", "foundedYear", "chain", "query", "sourceFile", "logo", "domain")}
                          for l in members],
            "evidence": {"brands": brands, "offers": offers, "equipment": equipment_found},
            "flags": flags, "resolvedFlags": resolved, "gaps": gaps,
            "lastVerified": min(l["seenOn"] for l in members),
            "websiteChecked": (crawl_res or {}).get("fetched") if crawled else None,
            "pastedPages": [pg["url"] for pg in pasted],
            "sources": sorted({l["sourceFile"] for l in members}),
        }
        records.append(rec)
        people = [dict(p, location=l["name"], city=l["city"], state=l["state"])
                  for l in members for p in contacts.get(l["placeId"], [])]
        if people:
            private_out[slug] = people

    # write
    rec_dir = out_dir / "records"
    rec_dir.mkdir(parents=True, exist_ok=True)
    for old in rec_dir.glob("*.json"):
        old.unlink()
    for rec in records:
        (rec_dir / f"{rec['slug']}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    private.mkdir(parents=True, exist_ok=True)
    (private / "contacts.json").write_text(json.dumps(private_out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = {"site": site, "equipment": equipment, "inputs": [p.name for p in inputs],
               "rows": sum(len(read_rows(p)) for p in inputs), "locations": len(locs),
               "companies": len(records), "excluded": excluded,
               "flagged": [{"slug": r["slug"], "flags": r["flags"]} for r in records if r["flags"]],
               "published": sum(1 for r in records if r["publicationStatus"] == "published"),
               "records_dir": str(rec_dir), "contacts_file": str(private / "contacts.json"),
               "contacts": sum(len(v) for v in private_out.values())}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "summary.md").write_text(render_summary(summary, records, profile), encoding="utf-8")
    return summary


def _cell(v: str) -> str:
    return v.replace("|", "/")


def render_summary(s: dict, records: list[dict], profile: dict) -> str:
    L = [f"# Dealer records: {profile['label']} ({s['site']})", "",
         f"From {', '.join(s['inputs'])}: {s['rows']} rows, {s['locations']} unique locations, "
         f"**{s['companies']} companies** kept, {len(s['excluded'])} excluded, {len(s['flagged'])} flagged for review.",
         f"**{s['published']} published**: a record is published once it has no open flag; flagged records stay drafts "
         "until a decision in `decisions.yaml` (next to this file) resolves them. Decisions survive reruns.", "",
         "## Companies", "",
         "| Company | Locations | States | Equipment | Brands carried | Offers | Rating (reviews) | Flags |",
         "|---|---|---|---|---|---|---|---|"]
    for r in records:
        L.append(f"| {_cell(r['name'])} ({r['domain'] or 'no website'}) | {r['locationCount']} | {', '.join(r['states'])} | "
                 f"{', '.join(r['equipment'])} | {', '.join(r['brands']) or '-'} | {', '.join(r['services']) or '-'} | "
                 f"{r['rating'] or '-'} ({r['reviews'] or 0}) | {len(r['flags']) or ''} |")
    if s["flagged"]:
        L += ["", "## Flagged for review", ""]
        for f in s["flagged"]:
            L += [f"- **{f['slug']}**: " + "; ".join(f["flags"])]
    gaps = [r for r in records if r["gaps"]]
    if gaps:
        L += ["", "## Gaps (published anyway)", ""]
        L += [f"- **{r['slug']}**: " + "; ".join(r["gaps"]) for r in gaps]
    if s["excluded"]:
        L += ["", "## Excluded", ""]
        for e in s["excluded"]:
            where = f" ({e['city']}, {e['state']})" if e.get("city") else ""
            L += [f"- {e['name']}{where}: {e['reason']}"]
    found = Counter(b for r in records for b in r["brands"])
    if found:
        L += ["", "## Brands carried, by number of companies", "",
              ", ".join(f"{b} {n}" for b, n in found.most_common())]
    eq = Counter(e for r in records for e in r["equipment"])
    L += ["", "## Equipment, by number of companies", "", ", ".join(f"{EQUIPMENT[e]['label']} {n}" for e, n in eq.most_common())]
    offers = Counter(o for r in records for o in r["services"])
    if offers:
        L += ["", "## Offers, by number of companies", "", ", ".join(f"{o} {n}" for o, n in offers.most_common())]
    L += ["", f"Contact people ({s['contacts']}) are in `{Path(s['contacts_file']).name}` under data/<site>/private/, "
          "which git ignores. They never go into a public record.", ""]
    return "\n".join(L)


PRIVATE_FIELDS = ("flags", "resolvedFlags", "gaps", "sources")


def export(records_dir: Path, to: Path) -> int:
    """Copy published records to a site's data folder (replacing its JSON files). Review fields stay behind."""
    to.mkdir(parents=True, exist_ok=True)
    for old in to.glob("*.json"):
        old.unlink()
    n = 0
    for f in sorted(records_dir.glob("*.json")):
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec.get("publicationStatus") != "published":
            continue
        for k in PRIVATE_FIELDS:
            rec.pop(k, None)
        for loc in rec.get("locations", []):
            loc.pop("seenOn", None)
        (to / f.name).write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        n += 1
    (to / "_equipment.json").write_text(json.dumps({k: v["label"] for k, v in EQUIPMENT.items()}, indent=2) + "\n",
                                        encoding="utf-8")
    return n


def cli(site: str, equipment: str, inputs: list[str], crawl: bool, max_pages: int, export_to: str | None = None) -> int:
    paths = [Path(p).expanduser() for p in inputs]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print("Input not found: " + ", ".join(missing))
        return 1
    s = build(site, equipment, paths, crawl, max_pages)
    print(f"{s['rows']} rows -> {s['locations']} locations -> {s['companies']} companies "
          f"({len(s['excluded'])} excluded, {len(s['flagged'])} flagged)")
    print(f"Records: {s['records_dir']}")
    print(f"Review:  {Path(s['records_dir']).parent / 'summary.md'}")
    print(f"Contacts (private, git-ignored): {s['contacts_file']}")
    if export_to:
        n = export(Path(s["records_dir"]), Path(export_to).expanduser())
        print(f"Exported {n} published records to {export_to}")
    return 0
