"""DataForSEO answers kept on disk, so a keyword's volume or difficulty is bought once per quarter, not every run.

Two kinds, each with its own age limit in the config (0 turns that kind off):

  keyword_data_cache_days: 90   # per keyword: volume/CPC, difficulty, intent; per domain: authority rank
  keyword_ideas_cache_days: 30  # per request: idea, suggestion, related-search and competitor keyword lists

Per-keyword data is stored one keyword at a time, so a run only pays for keywords it has not seen within the
limit. A keyword DataForSEO returned nothing for is remembered too (as no data), so it is not bought again.
Files live in cache/dataforseo/ (shared by every site with the same country and language).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

from config import get_runtime

DEFAULT_DAYS = {"data": 90, "ideas": 30}
_NONE = {"none": True}  # stored for keywords DataForSEO returned no item for


def days(config, kind: str) -> int:
    v = config.get("keyword_data_cache_days" if kind == "data" else "keyword_ideas_cache_days")
    return DEFAULT_DAYS[kind] if v is None else int(v)


def _dir(config) -> Path:
    return Path(config["_paths"]["cache"]) / "dataforseo"


def _read(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(f: Path, data: dict) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(f)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-") or "any"


def per_key(config, kind: str, scope: str, keys: list[str], fetch, today: date | None = None) -> dict:
    """Values for `keys` (missing = no data), fetching only keys not cached within the age limit.

    `fetch(missing_keys) -> {key: value}` is the real API call; keys it does not return are stored as no data.
    """
    limit = days(config, "data")
    if limit <= 0:
        return fetch(keys) if keys else {}
    today = today or date.today()
    oldest = (today - timedelta(days=limit)).isoformat()
    f = _dir(config) / kind / f"{_slug(scope)}.json"
    store = _read(f)
    out, missing = {}, []
    for k in dict.fromkeys(keys):
        e = store.get(k)
        if e and e.get("at", "") >= oldest:
            if e.get("v") != _NONE:
                out[k] = e.get("v")
        else:
            missing.append(k)
    if keys:
        get_runtime(config).log("cache", kind=f"dataforseo_{kind}", cached=len(keys) - len(missing), fetched=len(missing))
    if not missing:
        return out
    got = fetch(missing)
    stamp = today.isoformat()
    for k, v in got.items():
        store[k] = {"v": v, "at": stamp}
        out[k] = v
    for k in missing:
        if k not in got:
            store[k] = {"v": _NONE, "at": stamp}
    _write(f, store)
    return out


def per_request(config, endpoint: str, payload: dict, fetch, today: date | None = None):
    """The whole answer for one request (an idea list), reused while it is younger than the ideas limit."""
    limit = days(config, "ideas")
    if limit <= 0:
        return fetch()
    today = today or date.today()
    key = hashlib.sha1(json.dumps([endpoint, payload], sort_keys=True).encode()).hexdigest()[:20]
    f = _dir(config) / "ideas" / f"{key}.json"
    e = _read(f)
    if e and e.get("at", "") >= (today - timedelta(days=limit)).isoformat():
        get_runtime(config).log("cache", kind="dataforseo_ideas", endpoint=endpoint, cached=1, fetched=0)
        return e["v"]
    v = fetch()
    if v:  # an empty answer is not kept: it may be a hiccup, and it is cheap to ask again
        _write(f, {"v": v, "at": today.isoformat(), "endpoint": endpoint, "payload": payload})
    get_runtime(config).log("cache", kind="dataforseo_ideas", endpoint=endpoint, cached=0, fetched=1)
    return v


def seed_from_research(config) -> dict:
    """Fill the cache from a site's last research files (ranked and rejected keywords), dated to when they were
    written, so the first cached run does not buy the same figures again. Never overwrites a cache entry."""
    from datetime import datetime
    work = Path(config["_paths"]["data"])
    scope = f'{config["country"]}|{config["language_code"]}'
    found = {"overview": {}, "difficulty": {}, "intent": {}}
    for name in ("ranked_keywords.json", "rejected_keywords.json"):
        f = work / name
        if not f.exists():
            continue
        day = datetime.fromtimestamp(f.stat().st_mtime).date().isoformat()
        rows = json.loads(f.read_text(encoding="utf-8"))
        for r in rows if isinstance(rows, list) else []:
            k = r.get("keyword")
            if not k:
                continue
            if "volume" in r and r.get("reason") in (None, "volume_below_floor", "kd_above_max", "intent_not_prioritised"):
                found["overview"][k] = ({"volume": r["volume"], "cpc": r.get("cpc"), "monthly_searches": r.get("monthly_searches")}, day)
            if r.get("reason") == "no_volume_data":
                found["overview"][k] = (_NONE, day)
            if r.get("kd") is not None and r.get("reason") in (None, "kd_above_max"):
                found["difficulty"][k] = (0 if r.get("kd_estimated") else r["kd"], day)  # an estimated kd was a raw 0
            if r.get("intent") and r.get("reason") in (None, "intent_not_prioritised"):
                found["intent"][k] = (r["intent"], day)
    added = {}
    for kind, rows in found.items():
        f = _dir(config) / kind / f"{_slug(scope if kind != 'intent' else config['language_code'])}.json"
        store = _read(f)
        new = {k: {"v": v, "at": d} for k, (v, d) in rows.items() if k not in store}
        if new:
            store.update(new)
            _write(f, store)
        added[kind] = len(new)
    return added


if __name__ == "__main__":
    import sys
    from config import load_config
    for path in sys.argv[1:]:
        print(path, seed_from_research(load_config(path, dry_run=True)))
