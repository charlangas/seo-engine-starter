"""Stage 1: keyword research and scoring (DataForSEO Labs + Keywords Data only)."""
from __future__ import annotations

import json
import re

import inventory
from apiclient import ApiError, dataforseo_post
import dfscache
from config import INTENT_WEIGHTS, get_runtime
from textutil import content_year, has_past_year, levenshtein, normalize_keyword, similarity, tokens

LABS = "dataforseo_labs/google/"


def _loc(config) -> dict:
    return {"location_name": config["country"], "language_code": config["language_code"]}


def _scope(config) -> str:
    return f'{config["country"]}|{config["language_code"]}'


def _ideas(config, endpoint: str, payload: dict, extract) -> list[str]:
    """One idea-list request, answered from the cache while it is younger than keyword_ideas_cache_days."""
    return dfscache.per_request(config, endpoint, payload, lambda: extract(dataforseo_post(config, endpoint, payload)))


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------- endpoints ----------

def keyword_ideas(config, seeds: list[str]) -> list[str]:
    return _ideas(config, LABS + "keyword_ideas/live", {
        "keywords": seeds[:200], **_loc(config), "limit": config["idea_limit"],
        "include_serp_info": False, "order_by": ["keyword_info.search_volume,desc"]},
        lambda res: [i["keyword"] for r in res for i in (r.get("items") or []) if i.get("keyword")])


def keyword_suggestions(config, seed: str, limit: int) -> list[str]:
    """Long-tail phrases that contain the seed."""
    return _ideas(config, LABS + "keyword_suggestions/live", {
        "keyword": seed, **_loc(config), "limit": limit, "include_seed_keyword": True,
        "order_by": ["keyword_info.search_volume,desc"]},
        lambda res: [i["keyword"] for r in res for i in (r.get("items") or []) if i.get("keyword")])


def related_keywords(config, seed: str, limit: int) -> list[str]:
    """Google 'searches related to' graph around the seed (depth 2)."""
    return _ideas(config, LABS + "related_keywords/live", {
        "keyword": seed, **_loc(config), "depth": 2, "limit": limit},
        lambda res: [i["keyword_data"]["keyword"] for r in res for i in (r.get("items") or [])
                     if (i.get("keyword_data") or {}).get("keyword")])


def keywords_for_keywords(config, seeds: list[str]) -> list[str]:
    """Keywords Data fallback when Labs returns no ideas."""
    return _ideas(config, "keywords_data/google_ads/keywords_for_keywords/live", {
        "keywords": seeds[:20], **_loc(config), "sort_by": "search_volume"},
        lambda res: [r["keyword"] for r in res if r.get("keyword")])[: config["idea_limit"]]


def competitor_keywords(config, domain: str, limit: int) -> list[str]:
    return _ideas(config, LABS + "ranked_keywords/live", {
        "target": domain, **_loc(config), "limit": limit,
        "order_by": ["keyword_data.keyword_info.search_volume,desc"]},
        lambda res: [i["keyword_data"]["keyword"] for r in res for i in (r.get("items") or [])
                     if (i.get("keyword_data") or {}).get("keyword")])


def keyword_overview(config, keywords: list[str]) -> dict:
    """Volume, CPC and monthly searches per keyword (cached per keyword for keyword_data_cache_days)."""
    return dfscache.per_key(config, "overview", _scope(config), keywords, lambda ks: _keyword_overview(config, ks))


def _keyword_overview(config, keywords: list[str]) -> dict:
    out = {}
    for chunk in _chunks(keywords, 700):
        res = dataforseo_post(config, LABS + "keyword_overview/live", {"keywords": chunk, **_loc(config)})
        for r in res:
            for i in (r.get("items") or []):
                info = i.get("keyword_info") or {}
                out[i["keyword"]] = {"volume": info.get("search_volume"), "cpc": info.get("cpc"),
                                     "monthly_searches": info.get("monthly_searches")}
    return out


def bulk_keyword_difficulty(config, keywords: list[str]) -> dict:
    """Keyword difficulty 0-100 (cached per keyword for keyword_data_cache_days)."""
    return dfscache.per_key(config, "difficulty", _scope(config), keywords, lambda ks: _bulk_keyword_difficulty(config, ks))


def _bulk_keyword_difficulty(config, keywords: list[str]) -> dict:
    out = {}
    for chunk in _chunks(keywords, 1000):
        res = dataforseo_post(config, LABS + "bulk_keyword_difficulty/live", {"keywords": chunk, **_loc(config)})
        for r in res:
            for i in (r.get("items") or []):
                out[i["keyword"]] = i.get("keyword_difficulty")
    return out


def search_intent(config, keywords: list[str]) -> dict:
    """Intent label (cached per keyword for keyword_data_cache_days)."""
    return dfscache.per_key(config, "intent", config["language_code"], keywords, lambda ks: _search_intent(config, ks))


def _search_intent(config, keywords: list[str]) -> dict:
    out = {}
    for chunk in _chunks(keywords, 1000):
        res = dataforseo_post(config, LABS + "search_intent/live",
                              {"keywords": chunk, "language_code": config["language_code"]})
        for r in res:
            for i in (r.get("items") or []):
                out[i["keyword"]] = (i.get("keyword_intent") or {}).get("label")
    return out


# ---------- filtering & scoring ----------

def brand_patterns(config) -> list[str]:
    terms = [t.lower().strip() for t in config.get("brand_terms") or [] if t.strip()]
    own = config["domain"].split(".")[0].lower()
    for d in config.get("competitor_domains") or []:
        label = re.sub(r"^www\.", "", d.lower()).split(".")[0]
        if label and label != own:
            terms.append(label)
            if "-" in label:
                terms.append(label.replace("-", " "))
    return sorted(set(terms))


def is_branded(keyword: str, patterns: list[str]) -> str | None:
    squashed = re.sub(r"[^a-z0-9]", "", keyword.lower())
    for p in patterns:
        if re.search(r"\b" + re.escape(p) + r"\b", keyword.lower()) or \
                (len(p) >= 5 and re.sub(r"[^a-z0-9]", "", p) in squashed):
            return p
    return None


def score_keyword(volume: int, kd: float, intent: str) -> float:
    return round((volume * INTENT_WEIGHTS[intent]) / (kd + 1), 2)


def get_opportunities(config) -> list[dict]:
    """Run Stage 1. Writes ranked_keywords.json and returns the selected top-N items."""
    rt = get_runtime(config)
    rt.log("stage", stage="research", site=config["site_name"])

    # 1. Candidate discovery
    candidates: list[str] = []
    seeds = config["seed_keywords"]
    if seeds:
        ideas = keyword_ideas(config, seeds)
        if not ideas:
            rt.log("filter_decision", step="discovery", note="keyword_ideas empty; using keywords_for_keywords")
            ideas = keywords_for_keywords(config, seeds)
        candidates += seeds + ideas
        for seed in seeds[: config["max_seed_expansions"]]:
            for fn in (keyword_suggestions, related_keywords):
                try:
                    candidates += fn(config, seed, config["idea_limit"])
                except ApiError as e:
                    rt.log("stage_error", stage="research", step=fn.__name__, seed=seed, error=str(e))
    if config["competitor_domains"] and (not seeds or config.get("use_competitor_keywords")):
        per = max(20, config["idea_limit"] // len(config["competitor_domains"]))
        for d in config["competitor_domains"]:
            try:
                candidates += competitor_keywords(config, d, per)
            except ApiError as e:
                rt.log("stage_error", stage="research", step="competitor_keywords", domain=d, error=str(e))
    import watch
    candidates += watch.research_ideas(config)  # topics competitors just published (competitor-watch)
    seen, uniq = set(), []
    for k in candidates:
        k = k.strip().lower()
        if k and k not in seen:
            seen.add(k)
            uniq.append(k)
    rt.log("filter_decision", step="discovery", candidates=len(uniq))

    rejected: list[dict] = []

    def reject(kw, reason, **detail):
        rejected.append({"keyword": kw, "reason": reason, **detail})
        rt.log("rejection", stage="research", keyword=kw, reason=reason, **detail)

    # 1b. Topical relevance guard (seed mode only): an idea must share at least
    # `seed_relevance_min` of some seed's content tokens, otherwise it is off-niche.
    # `required_terms` is one group (a flat list: any term matches) or several groups (a list of
    # lists: every group needs a match). `excluded_terms` drops a keyword containing any of them.
    raw = config.get("required_terms") or []
    groups = [raw] if raw and not isinstance(raw[0], list) else raw
    groups = [[str(t).lower() for t in g] for g in groups if g]
    excluded = [str(t).lower() for t in config.get("excluded_terms") or []]
    if groups or excluded:
        kept = []
        for kw in uniq:
            hit = next((t for t in excluded if t in kw), None)
            if hit:
                reject(kw, "excluded_term", term=hit)
            elif not all(any(t in kw for t in g) for g in groups):
                reject(kw, "missing_required_term", required_terms=groups)
            else:
                kept.append(kw)
        rt.log("filter_decision", step="0_required_terms", kept=len(kept), of=len(uniq))
        uniq = kept
    # A keyword naming an earlier year ("... 2022") targets an out-of-date page; content is for content_year.
    year = content_year(config)
    kept = []
    for kw in uniq:
        if has_past_year(kw, year):
            reject(kw, "past_year", content_year=year)
        else:
            kept.append(kw)
    if len(kept) != len(uniq):
        rt.log("filter_decision", step="0_past_year", kept=len(kept), of=len(uniq))
    uniq = kept
    if seeds and config["seed_relevance_min"] > 0:
        seed_sets = [set(tokens(s)) for s in seeds if tokens(s)]
        kept = []
        for kw in uniq:
            kt = set(tokens(kw))
            best = max((len(kt & ss) / len(ss) for ss in seed_sets), default=0)
            typo_of = next((sd for sd in seeds if sd.lower() != kw and abs(len(sd) - len(kw)) <= 2
                            and 1 - levenshtein(sd.lower(), kw) / max(len(sd), len(kw)) >= 0.9), None)
            if typo_of:
                reject(kw, "misspelling_of_seed", seed=typo_of)
            elif best >= config["seed_relevance_min"]:
                kept.append(kw)
            else:
                reject(kw, "off_topic_for_seeds", seed_overlap=round(best, 2))
        rt.log("filter_decision", step="0_relevance", kept=len(kept), of=len(uniq))
        uniq = kept

    # 2. Filter 1: volume (keyword_overview)
    overview = keyword_overview(config, uniq) if uniq else {}
    stage1 = []
    for kw in uniq:
        vol = (overview.get(kw) or {}).get("volume")
        if vol is None:
            reject(kw, "no_volume_data")
        elif vol < config["volume_floor"]:
            reject(kw, "volume_below_floor", volume=vol, floor=config["volume_floor"])
        else:
            stage1.append(kw)
    rt.log("filter_decision", step="1_volume", kept=len(stage1), of=len(uniq))

    # 3. Filter 2: keyword difficulty (bulk_keyword_difficulty)
    kd = bulk_keyword_difficulty(config, stage1) if stage1 else {}
    stage2 = []
    kd_est = set()
    for kw in stage1:
        d = kd.get(kw)
        if d is None:
            reject(kw, "no_kd_data")
            continue
        # The provider returns 0 for many keywords it has no difficulty data for (checked on healthcare terms),
        # so a 0 can mean "unknown", not "easy". With the "assume" policy it is replaced by a middling value.
        if d == 0 and config.get("kd_zero_policy") == "assume":
            kd_est.add(kw)
            d = kd[kw] = float(config.get("kd_assumed", 30))
        if d > config["kd_max"]:
            reject(kw, "kd_above_max", kd=d, kd_max=config["kd_max"])
        else:
            stage2.append(kw)
    rt.log("filter_decision", step="2_kd", kept=len(stage2), of=len(stage1))

    # 4. Filter 3: branded competitor terms
    patterns = brand_patterns(config)
    stage3 = []
    for kw in stage2:
        hit = is_branded(kw, patterns)
        if hit:
            reject(kw, "branded_term", pattern=hit)
        else:
            stage3.append(kw)
    rt.log("filter_decision", step="3_branded", kept=len(stage3), of=len(stage2), patterns=patterns)

    # 5. Filter 4: intent (search_intent)
    intents = search_intent(config, stage3) if stage3 else {}
    allowed = set(config["intent_priority"])
    scored = []
    for kw in stage3:
        intent = intents.get(kw)
        if intent not in INTENT_WEIGHTS:
            reject(kw, "no_intent_data")
        elif intent not in allowed:
            reject(kw, "intent_not_prioritised", intent=intent, allowed=sorted(allowed))
        else:
            o = overview[kw]
            scored.append({"keyword": kw, "volume": int(o["volume"]), "kd": float(kd[kw]), "kd_estimated": kw in kd_est, "intent": intent,
                           "score": score_keyword(o["volume"], kd[kw], intent),
                           "cpc": float(o["cpc"]) if o.get("cpc") is not None else None,
                           "monthly_searches": o.get("monthly_searches")})
    rt.log("filter_decision", step="4_intent", kept=len(scored), of=len(stage3))

    # 6. Score sort, then inventory overlap + in-run near-duplicate rejection
    scored.sort(key=lambda x: (-x["score"], x["keyword"]))
    entries = inventory.load(config)
    thr = config["duplicate_similarity_threshold"]
    import gsc
    owners = [o for o in gsc.owners_for_research(config) if o["impressions"] >= config["gsc"]["min_impressions"]]
    import cluster
    ck = cluster.Checker(config, entries)
    if ck.s["fetch_candidates"]:  # optional spend: SERPs for the top candidates, so overlap can be judged
        import serp as _serp
        for item in scored[: ck.s["fetch_candidates"]]:
            if ck.urls(item["keyword"]) is None:
                try:
                    _serp.fetch_serp(item["keyword"], config)
                    ck.memo.pop(item["keyword"], None)
                except Exception as e:  # noqa: BLE001
                    rt.log("stage_error", stage="research_serp_cluster", keyword=item["keyword"], error=str(e))
    ranked = []
    for item in scored:
        kw = item["keyword"]
        # Exact match after normalisation only: a page ranking for a similar query is not a conflict.
        owner = next((o for o in owners if normalize_keyword(o["query"]) == normalize_keyword(kw)), None)
        if owner:  # Search Console shows another page already ranking for this: do not compete with it
            reject(kw, "existing_page_ranks_for_keyword", page=owner["page"], query=owner["query"],
                   impressions=owner["impressions"], position=owner["position"])
            continue
        page, n_shared = ck.same_serp_page(kw)
        if page and normalize_keyword(page["primary_keyword"]) != normalize_keyword(kw):
            reject(kw, "same_serp_as_existing", existing=page["primary_keyword"], shared_urls=n_shared)
            cluster.record_secondary(config, page, kw, n_shared)
            continue
        hit, sim = inventory.find_overlap(kw, config, entries)
        if hit and sim < 1.0 and ck.different_serps(kw, hit["primary_keyword"]):
            rt.log("filter_decision", step="serp_cluster", keyword=kw, note="similar words, different SERP: kept",
                   existing=hit["primary_keyword"])
            hit = None
        if hit:
            same = normalize_keyword(hit["primary_keyword"]) == normalize_keyword(kw)
            if same and hit.get("status") == "failed":
                reject(kw, "previously_failed_quality_gate", error=hit.get("error"))
                continue
            if same and hit.get("status") != "written" and hit.get("type") == "article":
                item["resumed"] = True  # planned earlier but never written: retry it
            else:
                reject(kw, "already_written" if same else "overlaps_inventory",
                       existing=hit["primary_keyword"], similarity=sim)
                continue
        clash = next((r for r in ranked if similarity(kw, r["keyword"]) >= thr
                      and not ck.different_serps(kw, r["keyword"])), None)
        serp_twin = None if clash else next((r for r in ranked if ck.same_serp(kw, r["keyword"])), None)
        if serp_twin:
            reject(kw, "same_serp_as_higher_scored_keyword", existing=serp_twin["keyword"],
                   shared_urls=ck.same_serp(kw, serp_twin["keyword"]))
            serp_twin.setdefault("serp_cluster", []).append(kw)
            continue
        if clash:
            reject(kw, "overlaps_higher_scored_keyword", existing=clash["keyword"],
                   similarity=round(similarity(kw, clash["keyword"]), 3))
            continue
        ranked.append(item)

    # 7. Money pages first: until money_pages.target bottom-of-funnel pages exist, only those are selected.
    import forecast
    fs, lang = forecast.settings(config), config.get("language_code", "en")
    for item in ranked:
        item["funnel"] = forecast.stage_for(config, item["keyword"], item["intent"])
        item["money_score"] = forecast.money_score(item, fs, lang)
    active, have, target = forecast.money_mode_active(config)
    selectable = ranked
    if active:
        money = sorted((r for r in ranked if r["funnel"] == "bottom"), key=lambda r: (-r["money_score"], r["keyword"]))
        rest = [r for r in ranked if r["funnel"] != "bottom"]
        for r in rest:
            r["held_by_money_pages"] = True
        ranked, selectable = money + rest, money
        rt.log("filter_decision", step="7_money_pages", money_pages_written=have, target=target,
               bottom_candidates=len(money), held=len(rest))

    top_n = config["max_keywords_per_run"]
    selected = selectable[:top_n]
    chosen = {id(x) for x in selected}
    for item in ranked:
        item["selected"] = id(item) in chosen

    work = config["_paths"]["work"]
    work.mkdir(parents=True, exist_ok=True)
    (work / "ranked_keywords.json").write_text(json.dumps(ranked, indent=2, ensure_ascii=False), encoding="utf-8")
    (work / "rejected_keywords.json").write_text(json.dumps(rejected, indent=2, ensure_ascii=False), encoding="utf-8")
    rt.log("stage", stage="research_done", candidates=len(uniq), passed=len(ranked),
           selected=len(selected), rejected=len(rejected))
    return selected
