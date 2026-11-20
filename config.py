"""Secrets, site-config loading/validation, defaults, run context and logging."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

import lang
from textutil import slugify

ROOT = Path(__file__).resolve().parent
REQUIRED_SECRETS = ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "SERPAPI_KEY"]
BUSINESS_TYPES = {"architecture", "agency", "ecommerce", "local_services", "other"}
DIRECTORY_TYPES = {"local_services", "firms", "tools", "products", "custom"}
INTENTS = ["informational", "navigational", "commercial", "transactional"]

# Every default threshold lives here (documented in README.md).
DEFAULTS = {
    "volume_floor": 200,
    "kd_max": 40,
    "max_keywords_per_run": 10,
    "cadence_per_week": 3,
    "location": None,
    "enable_directory": False,
    "directory_type": None,
    "directory_categories": [],
    "listing_fields": [],
    "intent_priority": None,            # derived from business_type when null
    "brand_terms": [],                  # extra branded terms to reject in Stage 1
    "language_code": "en",
    "country": None,                    # DataForSEO Labs location_name; derived from location when null
    "idea_limit": 200,                  # max keyword ideas pulled per run
    "max_seed_expansions": 5,           # seeds expanded via keyword_suggestions + related_keywords
    "required_terms": [],               # optional: keyword must contain one of these substrings;
                                        # a list of lists means one match from every group
    "kd_zero_policy": "trust",          # "assume": a difficulty of 0 is treated as unknown (see kd_assumed)
    "kd_assumed": 30,                   # difficulty used for filtering and scoring when kd_zero_policy is "assume"
    "cite_only_domains": [],            # if set, SERP pages may only be cited when their domain matches one of these
    "primary_sources": [],              # curated [{title, url, match: [words]}] cited when a match word is in the keyword
    "faq_fallback": None,               # replacement for the 3 generic FAQ questions ({kw} is filled in)
    "keyword_h2_max": None,             # max H2s (FAQ excluded) that may contain the primary keyword
    "forbid_best_lists": False,         # reject "Best ..." / "Top N ..." headings
    "word_count_max_factor": None,      # reject a draft longer than target x this factor
    "content_year": "auto",             # year content is written for ("auto": next year from October); earlier years are never named
    "excluded_terms": [],               # optional: keyword containing any of these is rejected
    "seed_relevance_min": 0.5,          # idea must share >= this share of a seed's tokens (0 disables)
    "duplicate_similarity_threshold": 0.7,
    "max_dataforseo_calls": 50,
    "max_serpapi_calls": 30,
    "serp_cache_hours": 24,
    "keyword_data_cache_days": 90,      # reuse DataForSEO volume, difficulty, intent and domain rank this long (0: off)
    "keyword_ideas_cache_days": 30,     # reuse DataForSEO idea and suggestion lists this long (0: off)
    "min_word_count": {"informational": 1500, "commercial": 1200, "transactional": 1000, "navigational": 800},
    "cite_external_sources": True,      # false: briefs carry no external sources (a store must not cite rival sellers)
    "skip_domains": [],                 # extra domains never analysed or cited (marketplaces, forums)
    "writer_notes": None,               # optional facts and limits appended to the writer and editor prompts
    "target_word_count_cap": None,      # optional ceiling on target_word_count (off by default)
    "max_gap_sections": 3,
    "thin_section_words": 150,          # consensus subtopic with fewer avg words is "thin"
    "max_revision_passes": 3,
    "include_h1_in_body": True,
    "blog_url_prefix": "/blog",
    "directory_url_prefix": "/directory",
    "directory_listings_per_category": 8,
    "writer_backend": "auto",           # auto | openrouter | anthropic | template
    "writer_model": "anthropic/claude-opus-5.5",   # OpenRouter slug: drafts and revises
    "editor_model": "openai/gpt-5.6-terra",        # OpenRouter slug: reviews, never writes
    "editor_rounds": 2,                 # writer -> editor -> writer alternations before the final gate
    "writer_max_tokens": 32000,         # output cap per writer call; doubled once if a reply is cut off
    "editor_max_fixes": 8,              # most fixes the editor may raise per round
    "max_llm_calls": 80,                # hard cap on OpenRouter calls per run
    "llm_model": "claude-opus-5-5",     # only used by writer_backend: anthropic
    "gsc_conflict_guard": True,         # Stage 1 rejects keywords another page already ranks for
    "gsc_property": None,               # e.g. "sc-domain:example.com"; null = sc-domain:<domain>
    "max_gsc_calls": 20,
    "gsc": {
        "window_days": 28,              # length of the current and the comparison window
        "lag_days": 3,                  # Search Console data is ~2-3 days behind
        "min_impressions": 20,          # ignore rows with fewer impressions in the window
        "striking_min_position": 4, "striking_max_position": 20,
        "low_ctr_max_position": 10, "low_ctr_max": 0.01,
        "decline_position_drop": 3.0, "decline_impression_drop": 0.3,
        "cannibal_min_share": 0.2,      # 2nd page needs >= this share of the query's impressions
        "max_rows_per_section": 25,
    },
    "comparisons": None,                # competitor comparison pages (comparisons.py); off unless enabled
    "retry_backoff_seconds": 1.0,       # 1s, 2s, 4s
    "interlink": {
        "min_impressions": 50, "anchor_max_position": 10, "fallback_anchor_max_position": 15,
        "priority_position_min": 4, "priority_position_max": 15,
        "max_links_per_post": 5, "max_anchors_per_target": 5,
        "money_sources": 10, "money_links_min": 2, "money_links_max": 4, "money_anchor_max_uses": 3,
        "money_pages": [],              # extra money URLs: ["/pricing/"] or [{url: /book/, anchors: [...]}]
    },
}

INTENT_WEIGHTS = {"commercial": 1.5, "transactional": 1.5, "informational": 1.0, "navigational": 0.3}
INTENT_PRIORITY_BY_TYPE = {
    "ecommerce": ["commercial", "transactional"],
    "local_services": ["commercial", "transactional"],
    "architecture": ["informational", "commercial", "transactional"],
    "agency": ["informational", "commercial", "transactional"],
    "other": ["informational", "commercial", "transactional"],
}
KNOWN_COUNTRIES = {
    "united states", "united kingdom", "canada", "australia", "ireland", "new zealand", "india",
    "germany", "france", "spain", "mexico", "italy", "netherlands", "brazil", "south africa", "singapore",
}


class ConfigError(Exception):
    pass


class BudgetExceeded(Exception):
    pass


def optional_secret(name: str) -> str:
    """Optional keys (OPENROUTER_API_KEY, GSC_SERVICE_ACCOUNT_FILE, ...). '' when unset."""
    load_dotenv(ROOT / ".env")
    return os.environ.get(name, "").strip().strip('"').strip("'")


def load_secrets() -> dict:
    load_dotenv(ROOT / ".env")
    missing = [k for k in REQUIRED_SECRETS if not os.environ.get(k, "").strip()]
    if missing:
        raise ConfigError(f"Missing required secret(s) in .env or environment: {', '.join(missing)}")
    return {k: os.environ[k].strip().strip('"').strip("'") for k in REQUIRED_SECRETS}


class Runtime:
    """Per-run state: logger, API budgets, dry-run flag. Stored at config['_runtime']."""

    def __init__(self, config: dict, dry_run: bool = False):
        self.dry_run = dry_run
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ("-dry" if dry_run else "")
        self.calls = {"dataforseo": 0, "serpapi": 0, "llm": 0, "gsc": 0}
        self.limits = {"dataforseo": config["max_dataforseo_calls"], "serpapi": config["max_serpapi_calls"],
                       "llm": config["max_llm_calls"], "gsc": config["max_gsc_calls"]}
        self.secrets = None
        log_dir = ROOT / "logs" / config["site_slug"]
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{self.run_id}.jsonl"
        self.errors: list[dict] = []
        self.editor_skipped: list[str] = []  # pieces the editor model could not review

    def get_secrets(self) -> dict:
        if self.secrets is None:
            self.secrets = load_secrets()
        return self.secrets

    def spend(self, service: str):
        if self.calls[service] >= self.limits[service]:
            raise BudgetExceeded(f"{service} call limit reached ({self.limits[service]} per run)")
        self.calls[service] += 1

    def log(self, event: str, **fields):
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if event in ("stage_error", "error"):
            self.errors.append(rec)
        if event in ("stage_error", "error", "stage", "write", "summary"):
            brief = {k: v for k, v in fields.items() if k not in ("traceback",)}
            print(f"[{time.strftime('%H:%M:%S')}] {event}: {json.dumps(brief, default=str)[:300]}")


def get_runtime(config: dict) -> Runtime:
    if config.get("_runtime") is None:
        config["_runtime"] = Runtime(config)
    return config["_runtime"]


def _derive_country(location):
    if location:
        last = location.split(",")[-1].strip()
        if last.lower() in KNOWN_COUNTRIES:
            return last
    return "United States"


def load_config(path, dry_run: bool = False) -> dict:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: config must be a YAML mapping")

    for field in ["site_name", "domain", "business_type", "niche_description", "output_directory"]:
        if not raw.get(field):
            raise ConfigError(f"{path}: missing required field '{field}'")
    if "enable_directory" not in raw:
        raise ConfigError(f"{path}: missing required field 'enable_directory'")
    if raw["business_type"] not in BUSINESS_TYPES:
        raise ConfigError(f"{path}: business_type must be one of {sorted(BUSINESS_TYPES)}")
    seeds, comps = raw.get("seed_keywords") or [], raw.get("competitor_domains") or []
    if not seeds and not comps:
        raise ConfigError(f"{path}: provide seed_keywords or competitor_domains")
    for name, val in (("seed_keywords", seeds), ("competitor_domains", comps)):
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise ConfigError(f"{path}: {name} must be a list of strings")

    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in raw.items():
        if k in ("interlink", "gsc") and isinstance(v, dict):
            cfg[k].update(v)
        elif k == "min_word_count" and isinstance(v, dict):
            cfg["min_word_count"].update(v)
        else:
            cfg[k] = v
    cfg["seed_keywords"], cfg["competitor_domains"] = seeds, comps

    for k in ["volume_floor", "kd_max", "max_keywords_per_run", "cadence_per_week", "keyword_data_cache_days",
              "keyword_ideas_cache_days"]:
        if not isinstance(cfg[k], int) or isinstance(cfg[k], bool) or cfg[k] < 0:
            raise ConfigError(f"{path}: {k} must be a non-negative integer")
    if cfg["cadence_per_week"] < 1:
        raise ConfigError(f"{path}: cadence_per_week must be >= 1")
    if not isinstance(cfg["enable_directory"], bool):
        raise ConfigError(f"{path}: enable_directory must be true or false")
    if cfg["enable_directory"]:
        cfg["directory_type"] = cfg["directory_type"] or (
            "local_services" if cfg["business_type"] == "local_services" else "custom")
        if cfg["directory_type"] not in DIRECTORY_TYPES:
            raise ConfigError(f"{path}: directory_type must be one of {sorted(DIRECTORY_TYPES)}")
    if cfg["intent_priority"]:
        bad = [i for i in cfg["intent_priority"] if i not in INTENTS]
        if bad:
            raise ConfigError(f"{path}: invalid intent_priority value(s): {bad}")
    else:
        cfg["intent_priority"] = INTENT_PRIORITY_BY_TYPE[cfg["business_type"]]

    import publish
    errs = publish.validate(cfg.get("publish"))
    if errs:
        raise ConfigError(f"{path}: " + "; ".join(errs))
    if cfg.get("comparisons") is not None:
        import comparisons
        errs = comparisons.validate(cfg["comparisons"], str(path))
        if errs:
            raise ConfigError("; ".join(errs))
    if cfg.get("forecast") is not None or cfg.get("money_pages") is not None:
        import forecast
        try:
            forecast.settings(cfg)
            forecast.money_settings(cfg)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}")
    import cluster
    import onpage
    import output
    try:
        output.page_paths(cfg)
        import aivis
        if cfg.get("ai_visibility") is not None:
            aivis.settings(cfg)
        cluster.settings(cfg)
        onpage.settings(cfg)
        import autopilot
        import budget
        import health
        import pacing
        import slop
        import watch
        autopilot.settings(cfg)
        budget.settings(cfg)
        pacing.settings(cfg)
        health.settings(cfg)
        slop.settings(cfg)
        watch.settings(cfg)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}")
    if cfg.get("evidence") is not None:
        import evidence
        try:
            evidence.settings(cfg)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}")
    if cfg.get("refresh") is not None:
        import refresh
        try:
            refresh.settings(cfg)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}")
    cfg["country"] = cfg["country"] or _derive_country(cfg["location"])
    lang.set_language(cfg["language_code"])   # text rules follow the site's language from here on
    cfg["domain"] = cfg["domain"].replace("https://", "").replace("http://", "").strip("/")
    cfg["site_slug"] = slugify(cfg["site_name"])
    cfg["config_path"] = str(path)

    out = Path(cfg["output_directory"]).expanduser()
    if not out.is_absolute():
        out = (ROOT / out).resolve()
    data = ROOT / "data" / cfg["site_slug"]
    work = (ROOT / "dry-run" / cfg["site_slug"]) if dry_run else data
    cfg["_paths"] = {
        "output": out,                                   # final Markdown (Astro collection)
        "content": (work / "content") if dry_run else out,  # where this run writes Markdown
        "data": data,                                    # inventory.json, topical_map.json
        "work": work,                                    # ranked_keywords.json, briefs/
        "briefs": work / "briefs",
        "inventory": data / "inventory.json",
        "topical_map": work / "topical_map.json",
        "cache": Path(os.environ["SEO_ENGINE_CACHE_DIR"]) if os.environ.get("SEO_ENGINE_CACHE_DIR") else ROOT / "cache",
    }
    cfg["_runtime"] = Runtime(cfg, dry_run=dry_run)
    return cfg
