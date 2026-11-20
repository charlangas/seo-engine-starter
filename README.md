# SEO Engine

Autonomous SEO content pipeline that turns a site config into static Markdown for Astro Content Collections.
One YAML file per site; any number of independent sites. No CMS, no database, no admin panel.

New here? Start with [QUICKSTART.md](QUICKSTART.md): first site, budget, schedule and the daily job, in one page.

```
config.yaml ─► Stage 1 research ─► Stage 2 SERP brief ─► Stage 3 draft → critique → revise ─► Stage 4 write + inventory
                (DataForSEO)        (SerpAPI + top-5 pages)   (deterministic critique)        (Markdown, inventory.json, topical_map.json)
```

## Setup

```bash
cd seo-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in the three keys
```

`.env` (no quotes needed):

```
DATAFORSEO_LOGIN=...
DATAFORSEO_PASSWORD=...
SERPAPI_KEY=...
OPENROUTER_API_KEY=...          # optional, see "Writing backends"
GSC_SERVICE_ACCOUNT_FILE=...    # optional, see "Daily Search Console check"
```

The run stops immediately with a clear message if any of the three required keys is missing.

## Commands

```bash
# one site, live: writes Markdown to output_directory and updates inventory
.venv/bin/python main.py run --config configs/my-site.yaml

# dry run: research + briefs + drafts into ./dry-run/<site>/, inventory untouched
.venv/bin/python main.py run --config configs/my-site.yaml --dry-run

# every config in configs/
.venv/bin/python main.py run-all

# every test, no spend: paid APIs (DataForSEO, SerpAPI, OpenRouter) are switched off while tests run,
# and a real request raises instead of being sent. The stage 1-4 tests run the live pipeline, so they are skipped.
.venv/bin/python tests/run_all.py

# live acceptance runs, which spend money (stage 1 and 4: DataForSEO; 2 and 4: SerpAPI; 3 and 4: OpenRouter)
RUN_LIVE_TESTS=1 .venv/bin/python tests/test_stage1.py
RUN_LIVE_TESTS=1 .venv/bin/python tests/test_stage2.py
RUN_LIVE_TESTS=1 .venv/bin/python tests/test_stage3.py
RUN_LIVE_TESTS=1 .venv/bin/python tests/test_stage4.py

# single files
.venv/bin/python tests/test_interlink.py
.venv/bin/python tests/test_openrouter.py   # writer-editor loop, mocked transport, no spend
.venv/bin/python tests/test_gsc.py          # Search Console check, mocked API, no spend
.venv/bin/python tests/test_quality_rules.py # per-site quality rules, no spend
.venv/bin/python tests/test_comparisons.py   # competitor comparison pages, mocked APIs, no spend
.venv/bin/python tests/test_refresh.py       # page refresh selection and rewrite, mocked, no spend
.venv/bin/python tests/test_forecast.py      # revenue forecast and money-pages mode, mocked, no spend
.venv/bin/python tests/test_evidence.py      # first-party evidence matching and gate, no spend
.venv/bin/python tests/test_cluster.py       # SERP-overlap grouping, page map, merge/301 suggestions, no spend
.venv/bin/python tests/test_pagetypes.py     # switch/export/audience pages, page folders, SERP classes, YouTube metadata
.venv/bin/python tests/test_aivis.py         # AI answer citations and GA4 conversion rates, mocked, no spend
.venv/bin/python tests/test_onpage.py        # on-page / AI-search rules and the internal editor checklist, no spend
.venv/bin/python tests/test_autopilot.py     # autopilot, slop rules, retitles, health, watch, money links, fact routing
.venv/bin/python tests/test_dealers.py       # dealer records from Outscraper exports, mocked websites, no spend

# what ranking could be worth, from the keywords found so far (see "Revenue forecast and money pages")
.venv/bin/python main.py forecast --config configs/my-site.yaml [--traffic-potential 20]

# weekly: are we cited in ChatGPT and Google AI Overview answers? (see "AI visibility"; spends)
.venv/bin/python main.py ai-visibility --config configs/my-site.yaml [--dry-run]

# map keywords to pages by SERP overlap; merge/301 suggestions for Search Console conflicts (see "Page map")
.venv/bin/python main.py page-map --config configs/my-site.yaml [--fetch 20]

# check first-party evidence items and list pages with nothing to draw on (see "First-party evidence")
.venv/bin/python main.py evidence --config configs/my-site.yaml

# refresh published pages that are due (see "Keeping pages fresh")
.venv/bin/python main.py refresh --config configs/my-site.yaml [--dry-run] [--slug a-slug] [--limit 1]

# daily Search Console opportunity report (see "Daily Search Console check")
.venv/bin/python main.py gsc-check --config configs/my-site.yaml

# everything that is due for a site, on its own schedule, plus the weekly report (see "Autopilot")
.venv/bin/python main.py autopilot --all --notify            # schedule this daily
.venv/bin/python main.py autopilot --config configs/my-site.yaml --dry-run   # which jobs are due

# crawl the live sitemap: status, redirects, canonicals, noindex, duplicates, orphans, indexing (see "Site health")
.venv/bin/python main.py health --config configs/my-site.yaml [--no-inspect]

# pages competitors published since the last check (see "Competitor watch")
.venv/bin/python main.py competitor-watch --config configs/my-site.yaml

# this month's spending, caps, cost per page and the pace the budget covers (see "Monthly budget")
.venv/bin/python main.py spend --config configs/my-site.yaml
.venv/bin/python main.py spend --all

# publishing pace, next dates, and whether it is safe to publish more (see "Publishing pace")
.venv/bin/python main.py pace --config configs/my-site.yaml
```

A manual `run` takes the next `max_keywords_per_run` best keywords that are not already in inventory. Under
autopilot, the content job writes only what the publishing schedule owes that day (see "Publishing pace").

## Autopilot (`autopilot.py`)

One daily command runs every job a site needs on its own cadence, and once a week sends a short report. Turn it on
per site with `autopilot: {enabled: true}`, set `publish.ship_mode: merge_when_checks_pass` so pull requests merge
themselves, and load the daily job:

```bash
sh scripts/install_autopilot.sh        # macOS: daily at 07:00 (pass hour and minute to change it)
```

On Linux, schedule `cd /path/to/seo-engine && .venv/bin/python main.py autopilot --all` daily with cron.

It replaces the separate `content-run`, `gsc-check` and site-run jobs for the sites you enable (unload those so
nothing runs twice). Each run does only the jobs that are due, in this order; one failing job never stops the others:

| Job | Default | What it does |
|---|---|---|
| `gsc_check` | daily | Search Console report and link plan (needs a Search Console key) |
| `health` | weekly | sitemap crawl and indexing check; duplicate engine titles are queued for a rewrite |
| `facts` | weekly | changed evidence figures queue the pages that quote them; questions for the owner |
| `competitor_watch` | weekly | new pages in competitors' sitemaps become keyword ideas |
| `refresh` | weekly | slipped, fact-changed, striking-distance, low-CTR (retitle) and old pages |
| `content` | daily | write the pages the publishing schedule owes today, often none (see "Publishing pace") |
| `interlink` | weekly | Search Console links plus money-page links, applied as a pull request |
| `ai_visibility` | weekly | AI answer citations (only when `ai_visibility.prompts` is set; spends) |
| `page_map` | monthly | SERP-overlap page map with merge/301 suggestions |
| `forecast` | monthly | traffic, leads and revenue projection |
| `report` | weekly | the weekly report |

The **weekly report** is under 300 words: what shipped (new, refreshed, retitled, links; merged or waiting), Search
Console clicks and striking-distance queries, title-test results, AI citations, site health and competitor pages. It
ends with at most 3 things only a person can do, most urgent first: a job that crashed or could not publish, then site
problems (broken pages, noindex, a free tool to build), then evidence questions and business numbers. It is saved to
`data/<site>/autopilot/report-<date>.md`, shown as a desktop notification and, when `SLACK_WEBHOOK_URL` (in `.env` or
the environment) holds a Slack incoming-webhook URL, posted to Slack. State is in `data/<site>/autopilot/state.json`,
a log of every job in `log.jsonl`.

```yaml
autopilot:
  enabled: true
  tasks: {content: 3, ai_visibility: off}   # days between runs, or daily | weekly | monthly | off
  slack_webhook_env: SLACK_WEBHOOK_URL
```

Before each paid job (`content`, `refresh`, `ai_visibility`) the monthly budget is checked; a job that does not
fit is skipped and tried again the next day (see "Monthly budget"). The weekly report shows this month's spend and
the publishing pace, and asks the owner to approve a faster pace when the signals support it.

What still needs a person: first-party evidence (case studies, screenshots, answers to the report's questions), the
site's business numbers for the forecast, and site-template fixes the health check reports.

## Monthly budget (`budget.py`)

Every paid call is written to a spending log, one file per month for all sites: `spend/<YYYY-MM>.jsonl` (not in git).

| Service | What is recorded |
|---|---|
| OpenRouter | the `usage.cost` OpenRouter reports for the call |
| DataForSEO | the `cost` field DataForSEO returns with every response |
| SerpAPI | `serpapi_usd_per_search` per search (SerpAPI bills a monthly plan: divide its price by its searches) |
| Claude API | `llm_usd_fallback` per call (only the legacy `writer_backend: anthropic`) |

A call that reports no cost is charged at the fallback and marked `estimated`.

```yaml
budget:
  monthly_usd: 60            # this site, all services, per calendar month (null: no cap)
  services: {llm: 40, dataforseo: 15, serpapi: 10}   # optional caps per service
  serpapi_usd_per_search: 0.015
  cut_optional_at: 0.7       # from 70% of a cap, skip the optional AI visibility check
  cut_refresh_at: 0.85       # from 85%, skip refreshes too; new pages keep going until the cap
```

`MONTHLY_BUDGET_USD` in `.env` caps all sites together. Caps are checked twice:

- **Before every paid call.** A call over any cap is not sent; it raises `BudgetExceeded`, the piece fails with that
  reason and the run moves on. This also covers manual commands.
- **Before every autopilot job.** Optional jobs stop first, then refreshes; new pages run until the cap. A job whose
  expected cost (its average over the last 90 days, or a first guess) is more than what is left is skipped. A skipped
  job stays due and is tried again the next day; the free jobs (Search Console, health, links) always run.

A reached cap is the first ask in the weekly report. `main.py spend` shows the month by service and by job, the
cost per new page over the last 90 days, and how many pages a week the budget covers after the other jobs.

## Publishing pace (`pacing.py`)

```yaml
publishing:
  per_week: 2               # new pages a week (replaces cadence_per_week, which still works)
  days: [tue, fri]          # optional; by default per_week is spread over the week
  max_per_run: 2            # most pages one run writes when catching up after missed days
  ramp:                     # optional: recommend a faster pace once Google takes the pages well
    to: 4                   # the most pages a week to recommend
    min_months: 6           # months since the first engine page
    min_indexed_rate: 0.8   # share of pages 14 to 90 days old that Google has indexed
    min_impressions_growth: 0.2   # Search Console impressions now vs ~90 days ago
    min_keywords_waiting: 20      # researched keywords not yet written
    slow_below: 0.5         # indexed rate under this: recommend slowing down
```

Autopilot's content job checks every day and writes a page only when a publishing day has no page yet, so the pace
is exactly `per_week` whatever `max_keywords_per_run` says. Pages are dated their publishing day (today when catching
up, never backdated); slots missed more than a week ago are dropped instead of written in a burst. Comparison pages
count toward the pace and take turns with articles.

**The ramp never raises the pace by itself.** Publishing more does not earn favour with Google; it only helps when
Google is already indexing and showing what the site publishes, there are good keywords left, and the budget covers
it. When every signal passes, the weekly report asks the owner to raise `per_week` by one. When Google indexes too
few new pages, it asks to slow down. A signal with no data yet (no Search Console key, fewer than 5 inspected pages,
under ~90 days of daily checks) blocks the step up. `main.py pace` shows every signal and the next dates.

The indexing signal comes from the weekly health check's URL Inspection results and the impressions trend from the
daily Search Console check, so the ramp needs `GSC_SERVICE_ACCOUNT_FILE`.

## Site health (`health.py`)

`health` reads the sitemap (robots.txt `Sitemap:` lines, else `/sitemap-index.xml` or `/sitemap.xml`, nested indexes
followed) and checks every URL up to `max_urls`: final status, redirects and chains, a canonical pointing elsewhere,
`noindex` (meta tag or `X-Robots-Tag`), robots.txt blocks, missing and duplicate titles and descriptions. From local
files it adds published engine pages missing from the sitemap and orphan pages no other page links to. With a Search
Console key, the URL Inspection API checks whether recent engine pages are indexed (`inspect_limit` per run; indexed
pages are not asked again); a page still not indexed after `not_indexed_after_days` is reported. Engine pages with a
duplicate title are queued for a title rewrite; everything else goes in the report and the weekly asks. Output:
`data/<site>/health/<date>.md|json`. Defaults: `max_urls: 300`, `inspect_limit: 15`, `delay_seconds: 0.2`,
`not_indexed_after_days: 14`, `sitemap_url: null`.

## Competitor watch (`watch.py`)

`competitor-watch` reads the sitemaps of `competitor_domains` (plus comparison competitors that have a `domain`, plus
`competitor_watch.domains`) and lists URLs that are new since the previous run, with the slug as a topic. The first run
records a baseline. New topics that match the site's seeds go to `data/<site>/competitors/ideas.json`, which Stage 1
adds to its keyword ideas (they still pass every filter). Free: public sitemaps only.

## DataForSEO cache (`dfscache.py`)

DataForSEO answers are kept in `cache/dataforseo/` and reused, so a keyword's figures are bought once per
quarter instead of on every run. Sites with the same country and language share them.

| Setting | Default | What it covers |
|---|---|---|
| `keyword_data_cache_days` | 90 | per keyword: volume, CPC and monthly searches, difficulty, intent; per domain: authority rank |
| `keyword_ideas_cache_days` | 30 | per request: keyword ideas, suggestions, related searches, competitor keywords |

A run buys only the keywords it has not seen within the limit, and remembers keywords DataForSEO has no data for.
Comparison research shares the same cache. Set either to 0 to always buy fresh. The forecast's traffic estimate,
AI visibility checks and SerpAPI results are not covered (SerpAPI keeps its own 24-hour cache).
`.venv/bin/python dfscache.py configs/<site>.yaml` seeds the cache from the site's last research files.

## Content year

Pages are written for one year and never name an earlier one. The year is `content_year` in the config: `auto`
(the default) means next year from October onward (in November 2026 that is 2027), and a number pins it.

- Research rejects keywords that name an earlier year (`past_year`), and briefs drop such secondary keywords and tags.
- The writer is told the year. The critique blocks any earlier year in the title, description, headings, FAQ or body.
  Link URLs, prices and code are ignored.
- Retitles use the content year when a year helps and reject titles that name an earlier one.
- The weekly health crawl queues published pages that name an earlier year (in their text, tags or FAQ) for a refresh,
  whatever their age.
- `main.py evidence` lists evidence items whose text names an earlier year. Phrase those as durations ("over 6 months")
  so the writer can quote them as written.

## Writing rules against AI tells (`slop.py`)

Every writer pass is cleaned mechanically, outside code blocks and link targets: em and en dashes become commas (or a
hyphen in number ranges), spaced hyphens used as dashes become commas, curly quotes become straight, "utilize" becomes
"use", "in order to" becomes "to", and stock chatbot lines ("Let's dive in.", "I hope this helps.") are removed. The
critique then flags what needs a rewrite: "not just X, but Y" frames, AI vocabulary (delve, pivotal, crucial,
moreover...), puffery, "experts say" with no linked source, "in this article we'll" openers and formulaic transitions.
These are advisory: the writer and editor are asked to fix them, but they fail an article only when there are more than
`slop.max_hits` (8). Spanish has its own lists. Titles of 40-60 characters are also advisory. A site that needs a
flagged word adds it to `slop.allow` (e.g. a landscaping company and "landscape").

## Adding a site

1. Copy an example from `configs/` to `configs/<your-site>.yaml`.
2. Set the required fields (below).
3. Run with `--dry-run`, read `dry-run/<site>/`, then run live.

### Required fields

| Field | Type | Notes |
|---|---|---|
| `site_name` | string | Also names the working folder `data/<slug>/` |
| `domain` | string | Your own domain is never cited as an external source |
| `business_type` | `architecture` \| `agency` \| `ecommerce` \| `local_services` \| `other` | Drives default intent filter |
| `niche_description` | string | |
| `seed_keywords` or `competitor_domains` | list of strings | At least one of the two |
| `location` | string or null | e.g. `Austin, Texas, United States`; null = country-level |
| `output_directory` | path | Absolute, `~`, or relative to the engine folder |
| `enable_directory` | boolean | |
| `volume_floor` | int | default 200 |
| `kd_max` | int | default 40 |
| `max_keywords_per_run` | int | default 10; pages per manual `run` |
| `publishing.per_week` | int | default 3 (or `cadence_per_week`); see "Publishing pace" |

### Optional fields

`directory_type` (`local_services` \| `firms` \| `tools` \| `products` \| `custom`), `directory_categories`,
`listing_fields`, `intent_priority`, plus everything in the defaults table below.

### Example: local services (blog + directory)

```yaml
site_name: Austin Plumbing Pros
domain: austinplumbingpros.com
business_type: local_services
niche_description: Residential plumbing repair, drain cleaning and water heater service for Austin homeowners.
seed_keywords: [emergency plumber, water heater repair, drain cleaning]
location: Austin, Texas, United States
output_directory: ../austin-plumbing-site/src/content/blog
enable_directory: true
directory_type: local_services
directory_categories: [plumbers, water heater installers]
listing_fields: [address, phone, website, rating, reviews, hours]
directory_output_directory: ../austin-plumbing-site/src/content/directory
volume_floor: 200
kd_max: 40
max_keywords_per_run: 10
publishing: {per_week: 3}
budget: {monthly_usd: 60}
brand_terms: [roto-rooter, mr rooter]
```

### Example: B2B agency / publisher (blog only)

```yaml
site_name: Plant Floor Marketing
domain: plantfloormarketing.example
business_type: agency
niche_description: Industrial and B2B marketing strategies for manufacturers and industrial companies
seed_keywords: [industrial marketing strategy, b2b marketing for manufacturers, industrial content marketing]
location: null
output_directory: ~/sites/plant-floor-site/src/content/blog
enable_directory: false
volume_floor: 200
kd_max: 40
max_keywords_per_run: 8
publishing: {per_week: 2}
intent_priority: [informational, commercial]
required_terms: [industrial, manufactur, b2b]     # keep research inside the niche
existing_content:                                  # pages already on the site
  - directory: ~/sites/plant-floor-site/src/content/guides
    url_prefix: /guides
```

## What gets written

| Path | Contents |
|---|---|
| `<output_directory>/<slug>.md` | Articles |
| `<output_directory>/directory/<category>/<listing>.md` and `.../index.md` | Listings and category indexes (only when `enable_directory: true`; move with `directory_output_directory`) |
| `data/<site>/inventory.json` | Every planned or written piece: `primary_keyword, slug, type, status, date` |
| `data/<site>/topical_map.json` | Clusters, pillar keyword, pieces and URLs |
| `data/<site>/ranked_keywords.json` | Every keyword that passed all filters, score-sorted, with `selected` flag |
| `data/<site>/rejected_keywords.json` | Every rejected keyword with the reason |
| `data/<site>/briefs/brief_<slug>.json` | One brief per keyword |
| `logs/<site>/<run_id>.jsonl` | Every API call, filter decision, rejection, critique round and error |
| `cache/serp/` | SERP results, 24 h, keyed by keyword + location |
| `dry-run/<site>/` | Everything a dry run produces (nothing else is touched) |

Article frontmatter: `title, description, pubDate, slug, tags, primaryKeyword` (required) plus
`secondaryKeywords, schemaType, faq` (question/answer pairs for FAQ schema). A refreshed page also gets
`updatedDate` (see "Keeping pages fresh").

## Stage rules

### Stage 1: research (`research.py`, `get_opportunities(config)`)

DataForSEO Labs `keyword_ideas`, `keyword_suggestions`, `related_keywords`, `keyword_overview`,
`bulk_keyword_difficulty`, `search_intent` (and `ranked_keywords` for `competitor_domains`), with Keywords Data
`keywords_for_keywords` as fallback when Labs returns no ideas.

Before the numbered filters, relevance guards drop off-niche ideas: `required_terms` and `excluded_terms` (if set) and
`seed_relevance_min`. Misspellings of a seed are dropped too. Then, in this exact order:

1. volume < `volume_floor` → out
2. keyword difficulty > `kd_max` → out
3. branded competitor terms → out (`brand_terms` + the name part of each `competitor_domains` entry)
4. intent not in `intent_priority` → out
5. `score = (volume × intent_weight) / (kd + 1)`; weights: commercial/transactional 1.5, informational 1.0, navigational 0.3
6. sort by score, drop anything overlapping inventory or a higher-scored pick, take the top `max_keywords_per_run`
7. with money-pages mode on and its target not met, only bottom-of-funnel keywords are taken, by `money_score`
   (see "Revenue forecast and money pages")

A keyword with no volume, KD or intent data from the API is rejected, never estimated.

### Stage 2: brief (`serp.py`, `build_brief(keyword, config)`)

SerpAPI `engine=google`: top 10 organic, People Also Ask, related searches. The first five readable article
pages are fetched for H2/H3 headings and word counts. `target_word_count = max(average of those pages,
minimum length for the intent)`.

- **Consensus subtopic**: a heading topic that is an H2 on ≥ 3 of the analysed pages. If those pages average
  under `thin_section_words` on it, it is also logged as a `thin_coverage` gap.
- **Gap sections** (2 to `max_gap_sections`, never an H2 on ≥ 3 top pages), in priority order: PAA questions no
  page answers, subtopics only 2 pages cover, related searches without a section, PAA answered by < 3 pages,
  then generic missing angles.
- When the SERP has little heading consensus, an intent-based skeleton fills the core sections.

### Stage 3: writing (`writer.py`, `generate_article(brief, config)`)

Draft → critique → revision, up to `max_revision_passes` rounds. The critique always runs and is deterministic.
A piece is written only when the critique returns zero fixes; otherwise it is logged and skipped.

Critique checks: word count ≥ target; an analogy or named mental model in every major H2; banned phrases;
primary keyword in title, H1, first 100 words and meta description; meta description 50–160 characters; intro
≤ 150 words; every `gaps_to_address` item covered (80+ words, 250+ for thin-coverage gaps, or an FAQ H3); FAQ
present; a list or table; sentence length, single-sentence paragraphs, direct address, no corporate jargon;
no placeholders; no statements about how the text was produced; required internal, directory and external links.

Banned phrases (case-insensitive, also scrubbed deterministically as a last line of defence): "in today's
digital landscape", "unlock", "leverage", "delve", "it's important to note", "in conclusion", "in the realm of",
"harness the power", "cutting-edge", "seamless", "robust".

#### Writing backends

| `writer_backend` | When | What it does |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` is in `.env` (`auto` picks this first) | A writer model and an editor model alternate through OpenRouter (below) |
| `anthropic` | `ANTHROPIC_API_KEY` is in `.env` and no OpenRouter key | Claude (`llm_model`) drafts and revises against the rule-based fix list, no editor |
| `template` | No key (`auto` falls back to this) | Deterministic composer built from the brief. Passes every structural check, but the prose is generic. Files get `draft: true` in frontmatter so they are held for an editorial pass |

Set `OPENROUTER_API_KEY` for publishable copy. The template backend exists so the pipeline runs end to end on
the three required keys alone.

**Writer-editor loop (`openrouter`).** Any two OpenRouter model slugs work; using different model families
gives the draft an independent read.

1. `writer_model` drafts from the brief.
2. `editor_model` reads the brief and the draft and returns a JSON fix list (at most `editor_max_fixes`). It
   looks for statistics the brief's sources do not support, gap sections that go no deeper than competitors,
   generic sections, advice without a concrete example, forced analogies and padding. The editor never writes
   article text, so none of its notes can end up on the page.
3. The writer revises against the editor's fixes plus the rule-based fixes.
4. Steps 2 and 3 repeat up to `editor_rounds` times, or stop early when the editor approves.
5. The rule-based critique then runs as the final gate, exactly as for the other backends
   (`max_revision_passes`). A piece that still has required fixes is skipped, not published.

An editor reply that cannot be parsed is logged and skipped; it never blocks a piece. Every call is logged to
the run log with model, tokens and cost, and `max_llm_calls` caps calls per run.

### Directory hybrid (`directory.py`)

Only when `enable_directory: true`. `local_services` and `firms` with a `location` pull businesses from SerpAPI
Google Maps; other types use organic results for each category. Each listing gets a unique description of 80+
words composed from its real data (rating, review count, rank within the category, address, hours). Each
category gets an index page with a comparison table. Articles link to at least 2 directory pages when 2 or more
exist.

### Stage 4: inventory (`inventory.py`, `output.py`)

`is_duplicate(keyword, site)` and `add_entry(...)`. A keyword overlaps when it matches an existing
`primary_keyword` exactly after normalisation (lowercase, plurals stemmed, stopwords dropped, tokens sorted) or
its normalised Levenshtein similarity is ≥ `duplicate_similarity_threshold`. Keywords with status `written` are
skipped on every later run; `planned` ones that never got written are retried.

## Competitor comparison pages (`comparisons.py`)

Opt-in per site. The idea is to get in front of buyers who already know a bigger competitor and are choosing
between options. These searches convert better than broad category terms, and a smaller site can win them.
Each run adds up to `max_per_run` comparison pieces on top of the normal `max_keywords_per_run` (set that to 0 to
write only comparisons). Comparison pages need an LLM writer backend; with `template` they are skipped.

**Spanish sites** (`language_code: es`) get Spanish keywords (`alternativas a X`, `X opiniones`, `mejores <categoría>`),
Spanish section headings, a Spanish freshness line ("Datos verificados en noviembre de 2026.") and a Spanish bias check.
For a brand you distribute rather than make, set `brand_relation` and `bias_note` so the page does not say "we make" it.

```yaml
comparisons:
  enabled: true
  brand: Acme                                 # your product; "A vs B" pages are titled "A vs B vs Acme"
  brand_summary: Project management for small agencies.
  brand_facts: ["Acme costs $12 per user per month, no annual contract."]   # the only claims allowed about you
  category: project management tool
  use_cases: [agencies, nonprofits]           # "best <category> for <use case>", "<competitor> alternatives for <use case>"
  competitors:
    - {name: Asana, domain: asana.com, priority: 2, pain_points: ["price nearly doubles between tiers"]}
    - {name: Trello, domain: trello.com, pricing_url: https://trello.com/pricing}
  max_per_run: 1
  cta: {label: Book a demo, url: /demo/}      # or per page type: {alternatives: {label, url}, best: {...}}
  author: {name: Pat Lee, title: Founder, url: /about/, image: /img/pat.jpg, socials: {linkedin: ..., x: ...}}
  # optional: volume_floor, kd_max (default: the site's), max_rank_gap (25), own_domain_rank, page_types,
  # authority_check_limit, reddit_research, find_videos, cta_min_links (2), linkedin_page_types, linkedin_max_overlap
  draft: true   # write comparison pages with `draft: true`; they stay out of internal links until the flag is removed
  favor_brand: true   # the brand is the recommended pick: first in tables and lists, the verdict's choice; checked by the critique (comparison_brand_not_first)
```

**Stage 1 (keywords).** For each competitor it generates candidate keywords for every page type:

| Page type | Keyword | Notes |
|---|---|---|
| `alternatives` | `<competitor> alternatives` (`... for <use case>`) | Highest weight (2.0): searched by people who know the brand and want something else |
| `review` | `<competitor> review` | Weight 1.6. An honest review; your brand appears at most once |
| `versus` | `<A> vs <B>` (keyword), titled `A vs B vs <brand>` | Weight 1.3. Both orders are checked; the higher-volume one is kept |
| `own_versus` | `<brand> vs <competitor>` | Cluster support: once that competitor's alternatives page is written it qualifies without search volume |
| `best` | `best <category>` (`... for <use case>`) | Weight 1.0, usually the hardest SERPs |

`score = volume × type weight × competitor priority / (kd + 1)`. Competitor-branded keywords are not
rejected here, unlike in normal research. The volume floor, KD max and `kd_zero_policy` apply. Then comes a **SERP authority check**:
for the best `authority_check_limit` candidates, the top-3 ranking domains are rated with DataForSEO
`backlinks/bulk_ranks` (0-100). A keyword is rejected (`serp_authority_gap`) when their median is more than `max_rank_gap` above
this site's rank, even if KD looks easy. If the Backlinks API is not on the DataForSEO plan, the check is logged
as unavailable and KD alone decides. Results: `<work>/comparison_keywords.json` (ranked + rejected with reasons).

**Stage 2 (brief).** The normal SERP brief, plus:
- Reddit threads about leaving the competitor (SerpAPI) and your configured `pain_points`
- the competitor's pricing page text (no API cost), dated
- YouTube videos to embed
- required sections per page type (for example "Why people look for X alternatives", a side-by-side table,
  "When X is the better choice")
- links to every written page in the same competitor's cluster, with the brand-vs-competitor page first.

**Stage 3 (writing).** The writer is told to compare specifics instead of adjectives and to be fair about where competitors win.
These are hard fixes:
- every listed company named
- a Markdown table that names at least two companies
- "Pricing and features checked <Month YYYY>"
- a bias disclosure when your brand is on the page
- `cta_min_links` links to the CTA
- **no dollar amount that is not in the fetched pricing pages or `brand_facts`**

`forbid_best_lists` does not apply to comparison pages. A site's `writer_notes` still applies, so remove any rule
there that forbids naming companies before enabling this.

**Frontmatter** adds `pageType`, `competitors`, `comparisonCluster`, `lastVerified`, and `author`, `cta`, `videos`
when set. The site layout should use them for:
- the byline
- the top and floating CTA
- a "related comparisons" footer that lists every page with the same `comparisonCluster`
- the freshness callout
- video embeds.

**Companions** go to `<work>/companions/<slug>/`. None of them are published:
- `linkedin.md`: for `linkedin_page_types`, a rewrite to publish from the company page, then repost from the
  founder. It is rejected and retried if more than `linkedin_max_overlap` of its 5-word phrases match the
  article, so it never duplicates the site page.
- `video_interview.md`: founder interview questions taken from the keyword, People Also Ask and secondary keywords,
  with the instruction to repeat each question in the answer.
- `media_checklist.md`: screenshots to capture (pricing pages, inside each product, G2 and Trustpilot, Reddit), videos,
  CTA and GA4 tracking, and the date to re-check facts.

Cost per comparison piece: about 1 to 3 SerpAPI calls (Reddit, YouTube; the authority SERP is reused from cache by the
brief) on top of the normal brief. Each run also makes 2 to 3 DataForSEO calls.

## Keeping pages fresh (`refresh.py`)

Freshness is a ranking signal in Google and in AI answers, and comparison pages go stale as soon as a
competitor changes its prices. `refresh` re-researches published pages and revises them in place.

```bash
.venv/bin/python main.py refresh --config configs/my-site.yaml --dry-run   # writes to dry-run/<site>/content/
.venv/bin/python main.py refresh --config configs/my-site.yaml             # live, ends in a pull request
.venv/bin/python main.py refresh --config configs/my-site.yaml --slug asana-alternatives   # this page, now
.venv/bin/python main.py refresh-all --notify                              # every site (weekly schedule)
```

**Which pages are due**, in this order, up to `max_per_run` revisions and `max_retitles_per_run` title rewrites per run
(the rest wait for the next run):

1. Pages the latest live `gsc-check` lists under "Pages that slipped" (biggest impression loss first).
2. Pages in `data/<site>/refresh_queue.json` because a first-party figure they quote changed (see "First-party evidence").
3. **Striking distance**: pages whose queries rank 8-20 with real impressions. The page is revised with those queries as
   the focus: each one gets a heading or the opening sentence of the section that answers it.
4. **Low click-through** on page one, and engine pages the health check found with a duplicate title: only the title and
   meta description are rewritten (`retitle`). Titles are 40-60 characters with the query first and match what the
   ranking titles signal (a year, prices, a list); descriptions are 120-160 characters with the query. The body and
   dates are not touched. The old title, query and CTR are stored in the inventory as a title test; one test per page
   at a time (`retitle_cooldown_days`, 28), and the weekly report says whether click-through improved.
5. Pages whose last update (`updatedDate`, else `pubDate`) is older than `max_age_days`: 30 days for comparison
   pages, 90 for other articles.

Never touched: pages the engine did not write (`existing_content` imports), `draft: true` pages, pages dated in the
future, pages updated in the last `min_age_days`, and any page with `refresh: false` in its frontmatter (use it for a
page someone rewrote by hand). Choosing the pages reads local files only, so a run with nothing due costs nothing and
does not touch the site checkout.

**How a page is refreshed:**
- The SERP brief is rebuilt; comparison pages also re-fetch pricing pages, Reddit threads and videos.
- The writer revises the existing article against the new brief instead of drafting a new one. The editor loop and
  the rule-based critique gate it as usual, so a stale price fails the "only fetched prices" rule.
- One refresh-only rule: every image, embed (`<iframe>`, `<img>`, `<video>`), MDX component and quote (blockquote of 6+
  words) on the published page must still be there. Screenshots and client quotes added by hand are never lost.
- Kept: `pubDate`, the slug, the `draft` state and any frontmatter the engine does not own (hero image, categories).
  Set: `updatedDate` to today; comparison pages also get a new `lastVerified`.
- If the revised text is identical, the file is left alone and the date is not moved.
- Inventory keeps the original `date` and records `updated` and `refresh_count`.

Each run writes `data/<site>/refresh/<date>.md` and `.json`: why each page was picked, word count before and after,
how much of the old text was kept, prices removed and added, sections added and removed. With a `publish` block the
changed pages go out as one pull request titled "Blog: refresh ...". The template backend cannot refresh (it would
replace the page's prose), so this needs `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`.

```yaml
refresh:                          # optional; these are the defaults
  max_age_days: {comparison: 30, article: 90}
  max_per_run: 2
  include_declines: true          # use gsc-check's "Pages that slipped"
  min_age_days: 7
  include_striking: true          # revise pages whose queries rank striking_min-max_position
  striking_min_position: 8
  striking_max_position: 20
  include_low_ctr: true           # retitle page-one pages nobody clicks
  retitle_cooldown_days: 28
  max_retitles_per_run: 3         # one SerpAPI call + one short LLM call each
```

The site layout should show "Updated <updatedDate>" next to the published date, and use `updatedDate` for
`dateModified` in the Article schema and `<lastmod>` in the sitemap.

Cost per page: one normal brief (1 SerpAPI SERP call plus page fetches; comparison pages add 1 to 3 SerpAPI calls)
and the writer/editor loop. To schedule it, copy the launchd example under "Daily Search Console check" with
`refresh-all --notify` as the arguments and a weekly `StartCalendarInterval` (add `<key>Weekday</key><integer>1</integer>`).
Run it after `gsc-check` so the decline list is current.

## Revenue forecast and money pages (`forecast.py`)

Start from money, not traffic: find out what the bottom-of-funnel keywords could be worth before writing, and write
those pages first.

```bash
.venv/bin/python main.py forecast --config configs/my-site.yaml                         # local files only, free
.venv/bin/python main.py forecast --config configs/my-site.yaml --traffic-potential 20  # see below, spends
```

**What it projects.** Every keyword the engine has found (`ranked_keywords.json`, `comparison_keywords.json`) or
written (inventory) that has a volume goes through:

```
searches × volume_correction × ctr_top3 → clicks × conversion_rate[kind] → leads × close_rate → customers × customer_value
```

Only bottom-of-funnel keywords count toward "the bet": `share_ranked` of them reach the top 3, ramping linearly to
full traffic over `months_to_rank`. The report (`data/<site>/forecast/<date>.md` and `.json`) shows clicks, leads,
customers, first-year revenue, lifetime gross profit, spend and ROI for the first `months`; the same at each
`scenarios` share; the break-even point (customers, leads and clicks a month that cover `monthly_spend`); the funnel
mix of everything found; and every keyword with its projected clicks and leads. Without `customer_value` it stops at
customers: it never makes up money figures. The report lists every business number you have not set.

Keywords the owner removed from the plan (inventory status `removed` or `excluded`) are left out.

**Funnel stage** (`funnel_stage`, English and Spanish):

| Stage | Rule |
|---|---|
| bottom | Comparison pages; alternatives, vs, review, best, top N, price/cost, near me, hire, agency, company, services, software, "for <role>"; "how to switch / export / migrate from"; Spanish alternativas, opiniones, son buenos, mejores, precio, cerca de mí, venta, comprar, "para <uso>"; the site's city (first part of `location`); `money_pages.bottom_terms`; transactional intent |
| top | Starts with a question word (what, why, how, qué, cómo, ...) and has no buying word |
| middle | Everything else (DataForSEO labels most keywords "commercial", so the label alone is not enough) |

**`--traffic-potential N`.** One keyword ranks on many related searches, so the volume of the head term undercounts
a page. With this flag, for the N most valuable keywords the forecast takes the current #1 result (from the SERP cache,
or SerpAPI when not cached; counts against `max_serpapi_calls`) and asks DataForSEO Labs `bulk_traffic_estimation`
for that page's estimated traffic in one call. That replaces volume as the basis, scaled from position 1 to the
top-3 average (`ctr_top3 / ctr_position1`).

**Money-pages mode.** With `money_pages.enabled`, Stage 1 selects only bottom-of-funnel keywords, ordered by expected
leads a month divided by difficulty, until `target` bottom-of-funnel pages exist. Pages count when the engine wrote them
or a site page already owns the keyword (inventory status `owned_by_page`). Other keywords stay in
`ranked_keywords.json` with `held_by_money_pages: true`, and are picked up by the normal score once the target is met.
If none is left, the run writes only comparison pages (or nothing) and says so. Every ranked keyword carries `funnel`
and `money_score`.

```yaml
money_pages:
  enabled: true
  target: 20                     # bottom-of-funnel pages before anything else
  bottom_terms: [zapopan, tlaquepaque]   # optional extra buying words for this site
forecast:                        # your numbers; everything here is optional
  customer_value: 6000           # first-year revenue per customer
  lifetime_value: 18000
  gross_margin: 0.7
  monthly_spend: 2500
  close_rate: 0.15               # lead -> customer (1.0 when a conversion is a sale)
  conversion_rate: {comparison: 0.05, bottom: 0.03, middle: 0.01, top: 0.002}   # visit -> lead
  share_ranked: 0.3
  months_to_rank: 6
  # market assumptions: volume_correction 1.5 (keyword tools under-report), ctr_top3 0.18, ctr_position1 0.28,
  # months 12, scenarios [0.1, 0.3, 0.5]
```

The defaults for click-through and conversion rates are rough assumptions to replace. Use your own Search Console
click-through by position. For conversion rates, drop a GA4 export at `data/<site>/conversions.csv` (or set
`forecast.conversions_csv`), either "Landing page" or "Pages and screens" with sessions and key events or
conversions. Pages are matched to the inventory by slug and grouped by kind (comparison, bottom, middle, top). Once
a kind has `min_sessions` (default 200), its observed rate replaces the default, unless you set that rate yourself
under `forecast.conversion_rate`. The report lists the observed rates and whether each one is in use.

## Page types, folders and SERP format (`comparisons.py`, `output.py`, `serpclass.py`)

Beyond alternatives, review, versus, own-versus and best pages, `comparisons.page_types` accepts:

| Type | Keywords | Required sections |
|---|---|---|
| `switch` (default on) | "how to switch from X", "migrate from X to <brand>" | why people leave, what changes (table), steps, when to stay |
| `export` (opt-in) | "export data from X", "how to export from X" | what you can export, steps, what to do with the file |
| `use_case` (default on) | "<category> for <use case>", e.g. "seo agency for dentists" | what this audience needs, how it works, cost, how to choose a provider |

Pages can go to their own folders and URLs instead of `/blog/`:

```yaml
page_paths:
  comparison: {url_prefix: /compare, output_directory: ../site/src/content/compare}   # every comparison type
  use_case: {url_prefix: /industries, output_directory: ../site/src/content/industries}
  service_page: {url_prefix: /services, output_directory: ../site/src/content/services}  # by SERP format
  # any single page type (alternatives, versus, switch, ...) can override "comparison"
```

The folder is chosen by page type, then `comparison`, then the SERP format. The inventory records `url_prefix`
and `output_dir`, so internal links, refresh, Search Console checks and route registration all use the right URL.

Every brief classifies the top 10 results as blog post, service page, product page, listicle, directory, forum,
LinkedIn or YouTube. The dominant on-site type becomes the brief's `format`, and the writer matches it: a service
page when businesses' service pages rank, or a list with "Best for" lines when listicles rank. LinkedIn, YouTube or
Reddit results in the top 10 are off-site opportunities. Any page with one, not only comparison pages, gets these
companions:
- `off_site.md`: what to post where.
- `linkedin.md`: the LinkedIn version.
- `video_interview.md`: the questions for the video.
- `youtube.md`: the YouTube metadata, with:
  - a title of at most 60 characters with the keyword first;
  - a description with the article link, secondary keywords and chapters;
  - tags within 500 characters;
  - the keyword as the file name;
  - caption and thumbnail notes.

### Free tools, scrambled results and the 2-URL tie-break

- When 3 or more of the top 5 results are interactive tools (calculators, generators, checkers), the run skips the
  keyword (inventory status `needs_tool`) and the weekly report asks whether to build a free tool: an article will not
  rank there.
- Each SERP is checked before it is trusted: fewer than 5 results, mostly unresolved `google.com/goto` links, results
  pointing at Google, one domain filling the page, or (comparison pages) a competitor named nowhere in the results.
  A bad SERP is fetched once more without SerpAPI's cache; if it is still bad it is not cached and the brief does not
  take its page format or off-site work from it. The run summary lists those keywords.
- Keyword grouping: one URL short of `min_shared_urls` (2 shared URLs by default) counts as the same page only when
  both SERPs rank the same kind of page.

### Comparison page rules added

Verdict first (the sentences right after the H1 say who should pick which, including where a competitor wins);
per-seat pricing shown at 1, 5 and 25 seats when a product is priced per seat (both advisory). Reddit complaints are
searched over the last 12 months only, and a complaint theme (price, support, reliability, contract, missing features,
complexity, data export) reaches the writer only when 3 or more threads raise it (`min_complaint_mentions`). Any page
that embeds or links a YouTube video gets `VideoObject` JSON-LD (uploadDate from `videoUploadDate` in frontmatter, else
`pubDate`).

## AI visibility (`aivis.py`)

List the questions your buyers ask AI assistants, and the engine checks weekly whether the answers cite you:

```yaml
ai_visibility:
  prompts:
    - best seo agency for dentists
    - how much does dental seo cost
  engines: [google_ai_overview, chatgpt]
  brand_terms: [Example Agency]     # default: site_name and comparisons.brand
  chatgpt_model: gpt-4o-mini
  max_prompts: 25
```

`main.py ai-visibility` makes one DataForSEO call per prompt per engine:
- Google AI Overview references, from SERP advanced;
- ChatGPT with web search, from AI Optimization LLM responses.

For each answer it records whether the site is cited, whether the brand is named, which URLs are cited instead,
and the site's organic rank. It writes `data/<site>/ai_visibility/<date>.md|json` and `latest.json`, with
citations gained and lost since the last check and the sites AI answers cite most. `--dry-run` only counts the
calls. Briefs read `latest.json`: when a prompt matches the page's keyword, the URLs cited for it go to the writer
as the pages to beat. The writer covers what they cover, more specifically and with first-party detail, and never
copies or links them.

## Page map: grouping keywords by SERP overlap (`cluster.py`)

Two keywords belong on one page when the same pages rank for both: their Google top 10 share at least
`min_shared_urls` URLs (default 3). Word similarity alone misses "plumber cost" / "plumber prices" (one intent)
and wrongly merges "water heater repair" / "water heater replacement" (two intents).

Stage 1 applies this from the SERP cache only, so it costs nothing:

- `same_serp_as_existing`: the candidate's SERP matches a page the site already has. It is rejected, and the
  keyword is stored on that page's inventory entry (`serp_cluster`). The next refresh of that page uses it as a
  secondary keyword.
- `same_serp_as_higher_scored_keyword`: two candidates in one run share a SERP, so only the higher-scored one is
  kept and the other becomes its secondary keyword.
- Candidates whose words look alike but whose cached SERPs clearly differ are no longer rejected as duplicates.

`main.py page-map` writes `data/<site>/page_map/<date>.md|json`. It lists every page URL with the keywords that
live on it and why, the keywords that need a page of their own (grouped when they share a SERP), and the ones with
no cached SERP (`--fetch N` buys up to N SERPs). With a Search Console report, each "same query, several pages"
conflict gets a verdict:
- **merge**: the pages' primary keywords share a SERP. The report includes the 301 line from the weaker page.
- **differentiate**: different intents. Retitle the weaker page.
- **review**: the SERPs are not cached.

```yaml
serp_clustering:
  min_shared_urls: 3
  fetch_candidates: 0   # >0 buys SERPs for the top N Stage 1 candidates so overlap can be judged (spends)
```

## On-page and AI search rules (`onpage.py`, `aiaudit.py`)

Every page, not only comparison pages, is written for answer engines (AI Overviews, ChatGPT, Perplexity) as well
as Google. These are required fixes in the critique:

| Code | Rule |
|---|---|
| `key_takeaways_missing` / `_shape` / `_position` | The first H2 is "Key takeaways" ("Puntos clave"): 3-5 one-sentence bullets of at most 30 words. |
| `paragraph_too_long` | No paragraph over `max_paragraph_words` (90). |
| `section_not_self_contained` | No H2 section opens by leaning on earlier text ("This is why...", "These...", "Esto..."). |
| `back_reference` | No "as mentioned above", "see above", "como vimos antes". |
| `fanout_missing` | Each query fan-out question in `fanout` is answered as an H2 or FAQ H3 (default: cost, timeline, risks, who it is not for, how to choose / next steps). |
| `best_for_missing` | On list pages (alternatives, best, "top" keywords) each option H3 has a "Best for:" line. |
| `cta_placement` | When the site has a CTA, it is linked at least 3 times: top, middle and bottom. |

The writer prompt also asks for bottom-line-up-front introductions, question-based H2s answered in their first
sentence, subject-verb-object sentences that name brands, products and places, the current year where facts are
time-sensitive, and no vague promotional claims.

Every page's frontmatter gains `takeaways`, `readingTime`, `author` (with social profiles) and `jsonLd`: a
schema.org graph with Article (or Review), a Person author, the Organization, Service for service pages and
FAQPage. Refreshed pages also get `changeNote`, one line on what the update changed. **The site layout has to
render these** (byline, updated date and change note, takeaways box, `<script type="application/ld+json">`).

```yaml
on_page:
  author: {name: Dana Ruiz, title: Master plumber, url: /about/dana/, bio: "...", socials: {linkedin: "https://..."}}
  cta: {label: Book a repair, url: /book/}      # default: comparisons.author / comparisons.cta
  cta_min_links: 3
  fanout: [cost, timeline, risks, not_for, next_steps]   # also: effort, after, compare, who_for, what_is
  max_paragraph_words: 90
  key_takeaways: true
  best_for_labels: true
  self_contained_sections: true
  organization: {name: Acme Plumbing, url: "https://acme.com/", logo: "https://acme.com/logo.png", sameAs: []}
  json_ld: true
```

The editor also runs an internal AI-search checklist (`aiaudit.py`, 81 items: page components, unique
information, copy, writing mechanics, freshness and query fan-out). It is editor-only, never shown on the page
and not a command. Each review round, the editor gets the checklist items the draft misses that the writer can
fix from the text, such as question headers, a verdict section or "Best for" labels, and turns the ones that fit
the page into fixes. Items that need a person, such as screenshots, video, an author profile or original
research, are left to the evidence rules, the refresh loop and the layout. Each written page's checklist score
goes to the run log and the inventory (`ai_score`) for tuning.

## First-party evidence (`evidence.py`)

A page that only summarises what already ranks gives readers and AI answers no reason to cite it. Each site keeps
hand-written evidence in `data/<site>/evidence/*.yaml`: case studies, customer quotes, tests, surveys, data and
screenshots. Every item needs something checkable (exact figures, a verbatim quote or an image) and a `topics` list:

```yaml
type: case_study            # case_study | quote | test | survey | data | screenshot
title: "Dental clinic in Austin: 9 to 41 booked calls a month"
summary: "What was done and what happened, plain facts."
numbers: ["41 booked calls a month", "5 months", "$3,200 a month"]
who: "Dr. Ana Ruiz, owner of Smile Studio"
image: /images/evidence/calls-chart.png     # optional screenshot, as the site serves it
topics: [dentist seo, dental marketing]     # "*" = any page
date: 2026-08-01
```

A file can hold one item or a list. For every page the best-matching items (up to `max_items`) go into the brief.
The writer must add a section such as "How we know this" before the FAQ, and these are required fixes:

- `evidence_missing`: no item shows up in the page (none of its figures, its quote or its screenshot).
- `evidence_section_missing`: no methodology-style H2 ("How we know this", "How we tested", "What we found", "Cómo lo sabemos"...).
- `evidence_unsupported_number`: that section states a figure (a percentage, or 10+ that is not a year) no item contains.
- `evidence_image_missing`: a matched screenshot item is not embedded.

Pages with no evidence get a prompt that forbids claims like "our clients" or "we tested", and the editor flags
first-party claims that aren't in the evidence. The editor also runs the information-gain test: delete everything
that came from the evidence, and if the rest could be any competitor's article, it flags the two most generic
sections. The evidence section is exempt from the analogy rule and is never padded.

```yaml
evidence:
  when_missing: note        # note (write, list in the run summary) | draft (draft: true) | skip (don't write it)
  max_items: 3
  section_heading: "How we know this"
  require_screenshots_for: [alternatives, vs]   # these comparison page types also need a screenshot item
  # directory: path/to/evidence                 # default data/<site>/evidence
```

Two owner limits, both on by default: `share_prices: false` keeps the owner's own prices and fees off every page and
swaps the report's "what did it cost" questions for timeline questions; `name_clients: false` tells the writer never to
name a client (items describe them by industry and size). An item from another industry is used only on pages its
topics match, and the writer has to say it comes from another industry.

Refreshes never draft or skip an already-published page. `main.py evidence` validates every item and writes
`data/<site>/evidence_report/<date>.md`, which lists the keywords with nothing to draw on (bottom of funnel first,
by expected leads) so you know which case study or screenshot to collect next.

### When a figure changes, and questions for the owner

`evidence` (and the autopilot `facts` job) compares every item's figures with the last snapshot
(`data/<site>/evidence_snapshot.json`). When a figure is removed or replaced, or an item lists old figures under
`supersedes: ["$1,200"]`, every engine page that quotes the old figure and uses that item (or matches its topics) is
queued for a refresh with the old and new figures, and the writer is told to replace them everywhere. The report also
turns the money keywords with no evidence into 3 specific questions for the owner (what a recent job cost, a result to
share, a photo; for comparison pages, who switched and why); the weekly report asks the top ones.

## Internal linking from Search Console (`interlink.py`)

```bash
# 1. plan: prints source post → anchor → target URL → target's query and position. Changes nothing.
.venv/bin/python main.py interlink --config configs/my-site.yaml --gsc gsc-export.csv

# 2. apply the reviewed plan
.venv/bin/python main.py interlink-apply --config configs/my-site.yaml --plan data/<site>/interlink/plan_<ts>.json

# 3. undo
.venv/bin/python main.py interlink-revert --changelog data/<site>/interlink/changelog_<ts>.json

# unattended weekly run: plan and apply in one step
.venv/bin/python main.py interlink --config configs/my-site.yaml --gsc gsc-export.csv --yes
```

The CSV needs one row per page + query with `Page`, `Query`, `Impressions`, `Position` columns (last 3 months).
The Search Console UI export keeps pages and queries in separate tabs, so export pairs through the API, Looker
Studio or Search Analytics for Sheets. Without `--gsc`, each post's own `primaryKeyword` and
`secondaryKeywords` are used as anchors (useful before a site has data).

Rules: anchors are a page's queries at position 1–10 with ≥ `min_impressions`; pages with nothing on page one
fall back to their position 11–15 queries. Any ranking page can be a link target, including pages that are
not Markdown (JSON-backed listings, hub pages); only Markdown posts are edited. Pages whose top query sits at position 4–15 receive links first. At
most `max_links_per_post` new links per post, one per paragraph. Never in headings, the intro paragraph,
existing links, code, tables or images. Never a self-link, each target once per post, no link if the post
already links there. Only existing text is wrapped; sentences are never rewritten. Single-word queries are
ignored.

### Links to money pages

The same plan also links the site's most-visited articles (by Search Console impressions, `money_sources`, 10) to its
money pages: comparison and audience pages, bottom-of-funnel keywords, and any URLs in `interlink.money_pages`. Each
source article gets at least one money link in its first 40% and 2-4 in all (`money_links_min`/`max`). Anchors are 3-7
words already on the page, or the money page's exact keyword; no exact anchor is used for the same page more than
`money_anchor_max_uses` (3) times across the site. Text is never rewritten, so articles with no fitting phrase are
listed as gaps. These links are marked `kind: money` in the plan.

## Sites with a route inventory

Some Astro sites only build a page for URLs listed in a central file. Set `route_inventory` and each new post
is added to that file as it is written (never in a dry run, never for posts flagged `draft: true`):

```yaml
route_inventory:
  file: "~/path/to/site/src/config/search-indexing.json"   # JSON with a top-level "routes" array
  kind: blog                      # default
  publication_status: published   # default
  index_status: index             # default; use noindex to hold new posts for review
```

The entry written is `{"path": "/blog/<slug>/", "kind", "publicationStatus", "indexStatus"}`.

## Directory category proposals (`categories.py`)

For a site that keeps a curated directory as structured records (one JSON file per listing, with tag lists):

```bash
.venv/bin/python main.py categories --config configs/my-site.yaml
.venv/bin/python tests/test_categories.py   # synthetic listings, no spend
```

It proposes one category page per tag and writes each as a draft record to
`data/<site>/category_proposals/<slug>.md`, with `summary.md` and `summary.json`. **Nothing is written into
the site**; a person picks which proposals become pages. No API or LLM is called.

- **Membership:** exactly the published listings whose record carries the tag, alphabetical. One tag per page.
- **Skipped:** tags on more than `max_share` (80%) of listings, which would duplicate the parent page, and
  tags on fewer than `min_listings` (3). Skipped tags still appear in the summary with their search demand.
- **Copy:** assembled from the records. Per-listing text is the record's `summary`, verbatim. Disclosure,
  comparison fields and "how to choose" copy are copied from `template_record`. No ranking words.
- **`lastVerified`:** the oldest date among member listings, never today's date.
- **Demand evidence:** queries from the latest live `gsc-check` that contain one of the tag's `aliases` and a
  provider word (`intent_terms`: agency, firm, company, services…). Tool-prompt queries and searches for a
  named listing are excluded. Counts are raw and usually small.
- **Checks before a proposal is saved:** same frontmatter keys as the template record, tags inside the site
  vocabulary, both body markers present once, no slug collision, status `draft`.

To turn reviewed proposals into draft pages in the site:

```bash
.venv/bin/python main.py categories-install --config configs/my-site.yaml --slugs branding paid-media
```

This copies each file into the folder that holds `template_record` and, when `route_inventory` is set, adds its
route as `kind: category`, `draft`, `noindex`. It never overwrites an existing file and never publishes;
publishing and indexing are edits you make in the site.

Settings live under `category_generator` in the site config: `listings_directory`, `template_record`,
`vocabulary_file`, `min_listings`, `max_share`, `min_query_words` (3), `max_questions` (8), `intent_terms`,
`member_notes`, and per-tag `facets` (`slug`, `title`, `noun`, `aliases`).

## Equipment dealer records (`dealers.py`)

Turns Google Maps exports from Outscraper into one draft record per dealer company, checked against each
dealer's own website. The records are the input for a dealer directory's company and category pages.

```bash
.venv/bin/python main.py dealers --site my-dealer-directory --equipment forklifts --input export.xlsx [--input more.csv]
.venv/bin/python tests/test_dealers.py   # synthetic export, mocked websites, no spend
```

`--equipment` is `forklifts`, `air-compressors` or `heavy-equipment`. Each profile lists the Google categories
that count as a dealer and the brands to look for (`PROFILES` in `dealers.py`).

- **Merging:** Outscraper repeats a place once per contact person; rows merge by Google `place_id`, and
  locations group into companies by website domain. Branch names lose their "- City, ST" suffix.
- **Checks:** closed places and places with no dealer category are excluded. A dealer category that is only
  secondary, manufacturer-owned branches, marketplaces, missing websites and locations trading under another
  name are flagged for a person to decide.
- **Website reading:** the homepage plus up to `--max-pages` pages about brands, products, rental, service or
  parts. robots.txt is respected, sites that refuse automated readers are flagged and not retried, and pages
  are cached for 30 days in `cache/dealers/`.
- **Brands:** a brand counts as carried only when the site names it next to a dealer word ("authorized Crown
  dealer", "Mitsubishi Forklift Dealer") or offers it new ("offering new Linde forklifts"), or it is in the
  business name. Brands in parts and service lists ("parts for Toyota, Hyster, Yale") or in a run of three or
  more brands are only `brandsMentioned`. Every finding keeps the page URL and the sentence.
- **Offers:** new, used, rental, service, parts and operator training, from the website or, failing that,
  from the Google categories. Each keeps its source.

Output in `data/<site>/dealers/`: `records/<slug>.json` (all `publicationStatus: draft`), `summary.md` and
`summary.json`. Contact people, emails and personal phone numbers go only to `data/<site>/private/contacts.json`,
which git ignores; they never enter a record. Decisions that survive reruns go in `data/<site>/dealers/decisions.yaml`:

```yaml
exclude: {crown.com: "manufacturer branches, not a dealer"}   # domain or record slug -> reason
include: [macscrane.com]                                      # keep despite a flag (moves to resolvedFlags)
names: {wolterinc.com: "Wolter, Inc."}                        # display name override
websites: {ipwlifttechs.com: "https://www.mhiusa.net/"}       # public website override
notes: {ipwlifttechs.com: "Formerly IPW Lift Techs."}         # one line shown on the listing
merge: {paperents.com: papemh.com}                            # list these locations under another company
equipment: {forkliftparts.us: [forklifts]}                    # equipment categories override
```

**Equipment categories:** besides the profile's own category, each record lists the other equipment the dealer
sells or rents (`EQUIPMENT` in `dealers.py`: aerial lifts, telehandlers, cranes, construction equipment, tractors,
utility vehicles, floor cleaning, dock and warehouse, air compressors), from Google categories or the dealer's
pages, with a quote for each. Blog, news and careers pages don't count, nor do sentences about parts, training,
where a machine is used, or a biography.

**Publishing and export:** a record with no open flag is `published`; "no brand confirmed" and "no website" are
listed as gaps and don't hold a record back. `--export <site data folder>` copies the published records there,
without the review fields, plus `_equipment.json` with category labels.

A site that refuses automated readers is flagged with the file to fill: paste its pages (copied from a browser)
into `data/<site>/dealers/pages/<domain>.md`, each starting with a `=== <url> ===` line. Pasted pages are read
like crawled ones and their evidence is labelled `pasted page`. Two more brand signals apply to every page: a
short list after a selling verb ("proud to offer CLARK, BYD, and JLG") and new inventory units ("2025 Komatsu
FG25T-16 … Condition: New"). A sentence naming five or more brands never counts as carried.

## Daily Search Console check (`gsc.py`)

```bash
# live, through the Search Console API
.venv/bin/python main.py gsc-check --config configs/my-site.yaml

# every config that sets gsc_property (use this one for the daily schedule)
.venv/bin/python main.py gsc-check --all

# from a UI export (.zip or the unpacked folder): query-level and page-level sections only
.venv/bin/python main.py gsc-check --config configs/my-site.yaml --export Performance-on-Search.zip
```

Writes `data/<site>/gsc/<date>.md` and `.json`, `latest.json`, and (live mode) `page_query_<date>.csv`.
It calls Search Console only: no DataForSEO, SerpAPI or LLM spend. Sections:

| Section | Rule | What to do with it |
|---|---|---|
| Striking distance | Position 4–20, ≥ 20 impressions | Link to the ranking page, deepen the section that answers the query |
| Low click-through on page one | Position ≤ 10, CTR under 1% | Rewrite title and meta description |
| Articles competing with a ranking page | An inventory article's keyword is the same (after normalisation) as a query another page gets impressions for | Re-angle or drop the article |
| Same query, several pages | Second page holds ≥ 20% of the query's impressions | Consolidate or differentiate |
| Pages that slipped | Position worse by ≥ 3, or impressions down ≥ 30%, against the previous window | Refresh the page |
| Visible but far from page one | Position over 20 | Add depth or links to the page that ranks |
| Internal links | Same rules as `interlink` | A plan is saved to `data/<site>/interlink/plan_gsc_<date>.json`. Nothing is applied until you run `interlink-apply` |

Live mode compares the last `window_days` (28) ending `lag_days` (3) ago with the 28 days before that.
A UI export has pages and queries in separate files, so anything that needs to know which page ranks for
which query is marked as needing the API instead of being guessed.

After a live check, Stage 1 also uses the result: a candidate keyword that is the same (after normalisation) as a query another page
already ranks for is rejected with reason `existing_page_ranks_for_keyword` (turn off with `gsc_conflict_guard: false`).

### One-time setup for the live check

1. In Google Cloud, create a project, enable the **Google Search Console API**, create a **service account**
   and download its JSON key.
2. In Search Console → Settings → Users and permissions, add the service account's `client_email` as a user
   (Restricted is enough).
3. Put the key file outside the repo and set `GSC_SERVICE_ACCOUNT_FILE=/full/path/key.json` in `.env`.
4. Add `gsc_property: "sc-domain:example.com"` to the site config (or `https://example.com/` for a URL-prefix
   property).
5. Run `gsc-check --config ...` once by hand and confirm it returns rows.

### Daily schedule (macOS)

Add `--notify` to a scheduled `run`, `run-all` or `gsc-check` command to get a macOS desktop alert:
`run` reports every outcome (written, nothing to write, failed), `gsc-check` only reports failures.
The alert is local; nothing is sent off the machine.

Use launchd. Save this as `~/Library/LaunchAgents/com.seo-engine.gsc-check.plist` with your own paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.seo-engine.gsc-check</string>
  <key>WorkingDirectory</key><string>/full/path/to/seo-engine</string>
  <key>ProgramArguments</key><array>
    <string>/full/path/to/seo-engine/.venv/bin/python</string>
    <string>main.py</string><string>gsc-check</string><string>--all</string>
  </array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>7</integer><key>Minute</key><integer>15</integer></dict>
  <key>StandardOutPath</key><string>/full/path/to/seo-engine/logs/gsc-check.log</string>
  <key>StandardErrorPath</key><string>/full/path/to/seo-engine/logs/gsc-check.log</string>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.seo-engine.gsc-check.plist
```

If the project sits under `~/Documents`, `~/Desktop` or `~/Downloads`, macOS privacy protection can block a
background job from reading it. Either move the project elsewhere or grant the Python binary Full Disk Access
in System Settings → Privacy & Security. Check `logs/gsc-check.log` after the first scheduled run. On Linux,
the cron equivalent is:

```
15 7 * * * cd /full/path/to/seo-engine && .venv/bin/python main.py gsc-check --all >> logs/gsc-check.log 2>&1
```

## Languages

Set `language_code` in the site config. English (`en`) is the default. Spanish (`es`) switches every text rule:
accent-insensitive keyword matching that tolerates connectors ("amortiguadores guadalajara" matches
"amortiguadores en Guadalajara"), Spanish stopwords and plural stemming, Spanish banned phrases and tone checks
("tú" form), Spanish outline skeletons and FAQ heading ("Preguntas frecuentes sobre ..."), and writer and editor
prompts that require Mexican Spanish. The data lives in `lang.py`. The `template` backend writes English only, so
a Spanish site needs `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`.

Two options work for any language: `writer_notes` (facts and limits appended to the writer and editor prompts,
for example what a business does and does not do) and `skip_domains` (extra domains never analysed or cited,
such as marketplaces). `cite_external_sources: false` builds briefs with no external sources, for a store whose
search results are mostly rival sellers.

Keyword volumes come from DataForSEO Labs at country level. For a local business, lower `volume_floor`
(the Guadalajara example uses 30).

## Default thresholds

All live in `DEFAULTS` in `config.py`; override any of them per site in the YAML.

| Setting | Default | Meaning |
|---|---|---|
| `volume_floor` | 200 | Minimum monthly searches |
| `kd_max` | 40 | Maximum keyword difficulty |
| `max_keywords_per_run` | 10 | Pieces per manual `run` (autopilot writes what the schedule owes) |
| `publishing.per_week` | 3 | New pages a week, on spread weekdays; `cadence_per_week` is the older name |
| `budget.monthly_usd` | none | Dollar cap per site per month; see "Monthly budget" |
| `intent_priority` | commercial + transactional for `ecommerce`/`local_services`; informational + commercial + transactional otherwise | Allowed intents |
| intent weights | 1.5 / 1.5 / 1.0 / 0.3 | commercial / transactional / informational / navigational |
| `idea_limit` | 200 | Keywords requested per discovery call |
| `max_seed_expansions` | 5 | Seeds expanded with suggestions + related keywords |
| `seed_relevance_min` | 0.5 | Share of a seed's words an idea must contain (0 disables) |
| `required_terms` | none | Keyword must contain one of these. A list of lists requires one match from every group (e.g. audience and topic) |
| `excluded_terms` | none | Keyword containing any of these is rejected |
| `brand_terms` | none | Extra branded terms to reject |
| `duplicate_similarity_threshold` | 0.7 | Normalised Levenshtein similarity that counts as overlap |
| `max_dataforseo_calls` | 50 | Hard cap per run (every attempt counts) |
| `max_serpapi_calls` | 30 | Hard cap per run |
| retries | 3 attempts, 1 s / 2 s / 4 s (`retry_backoff_seconds`) | On network errors, 429 and 5xx |
| `serp_cache_hours` | 24 | SERP cache lifetime |
| `min_word_count` | informational 1500, commercial 1200, transactional 1000, navigational 800 | Minimum length rule |
| `target_word_count_cap` | none | Optional ceiling on the target |
| `kd_zero_policy` / `kd_assumed` | `trust` / 30 | `assume` treats a difficulty of 0 as unknown and uses `kd_assumed` for the cap and the score (`kd_estimated: true` in `ranked_keywords.json`) |
| `cite_only_domains` | none | If set, only SERP pages whose domain matches (suffixes such as `.gov`) can be cited |
| `primary_sources` | none | Curated `{title, url, match: [words]}` sources offered when a match word is in the keyword |
| `faq_fallback` | generic | Replacement for the 3 fallback FAQ questions (`{kw}` filled in) |
| `keyword_h2_max` | none | Critique fix when more than this many non-FAQ H2s contain the primary keyword |
| `forbid_best_lists` | false | Critique fix for "Best ..." and "Top N ..." headings |
| `word_count_max_factor` | none | Critique fix when the draft exceeds target x factor (the target is capped by `target_word_count_cap`) |
| `max_gap_sections` | 3 | Gap H2s per brief (minimum is always 2) |
| `thin_section_words` | 150 | Below this average, a consensus subtopic is "thin" |
| `max_revision_passes` | 3 | Revision rounds before a piece is skipped |
| `include_h1_in_body` | true | Set false if your Astro layout already renders `title` as the H1 |
| `blog_url_prefix` / `directory_url_prefix` | `/blog` / `/directory` | Used for internal links |
| `directory_listings_per_category` | 8 | Listings per category |
| directory cache | 7 days | Listing data lifetime |
| `writer_backend` | `auto` | `openrouter` if its key is set, else `anthropic`, else `template` |
| `writer_model` / `editor_model` | `anthropic/claude-opus-5.5` / `openai/gpt-5.6-terra` | OpenRouter slugs |
| `editor_rounds` | 2 | Writer → editor → writer alternations before the final gate |
| `editor_max_fixes` | 8 | Fixes the editor may raise per round |
| `writer_max_tokens` | 32000 | Output cap per writer call. A reply cut off at the cap is retried once with twice the room |
| `max_llm_calls` | 80 | Hard cap on OpenRouter calls per run |
| `llm_model` | `claude-opus-5-5` | Only for `writer_backend: anthropic` |
| `gsc_property` | `sc-domain:<domain>` | Search Console property |
| `max_gsc_calls` | 20 | Hard cap per check (a check uses 4) |
| `gsc.window_days` / `gsc.lag_days` | 28 / 3 | Current and comparison window; data lag |
| `gsc.min_impressions` | 20 | Rows below this are ignored |
| `gsc.striking_min_position` / `striking_max_position` | 4 / 20 | Striking-distance band |
| `gsc.low_ctr_max_position` / `low_ctr_max` | 10 / 0.01 | Low-CTR rule |
| `gsc.decline_position_drop` / `decline_impression_drop` | 3.0 / 0.3 | Slipped-page rule |
| `gsc.cannibal_min_share` | 0.2 | Second page's share of a query's impressions |
| `gsc.max_rows_per_section` | 25 | Rows listed per report section |
| `gsc_conflict_guard` | true | Stage 1 rejects keywords another page already ranks for |
| `interlink.min_impressions` | 50 | Impressions for a query to count as an anchor |
| `interlink.anchor_max_position` / `fallback_anchor_max_position` | 10 / 15 | Anchor query positions |
| `interlink.priority_position_min` / `max` | 4 / 15 | Pages that get links first |
| `interlink.max_links_per_post` | 5 | New links per post |
| `interlink.max_anchors_per_target` | 5 | Queries tried per target page |

## Failure behaviour

- A failed keyword (API error after 3 attempts, budget cap, critique not satisfied, validation failure) is
  logged with its reason and the run moves to the next keyword.
- A failed site in `run-all` does not stop the other sites.
- Files are validated (required frontmatter, word count, banned phrases) before they are written.

## Modules

`config.py` secrets, config validation, defaults, run log · `apiclient.py` retries, call limits, call logging ·
`budget.py` spending log and monthly dollar caps · `pacing.py` publishing days and the ramp ·
`research.py` Stage 1 · `serp.py` Stage 2 · `writer.py` Stage 3 · `directory.py` directory pages ·
`inventory.py` + `output.py` Stage 4 · `interlink.py` internal linking · `gsc.py` Search Console check · `categories.py` directory category proposals · `refresh.py` page refresh · `forecast.py` revenue forecast and funnel stage · `evidence.py` first-party evidence · `onpage.py` on-page and AI-search rules · `aiaudit.py` internal AI-search editor checklist · `cluster.py` SERP-overlap page map · `serpclass.py` SERP result types · `aivis.py` AI answer citations · `autopilot.py` scheduled jobs and the weekly report · `health.py` site health crawl ·
`watch.py` competitor sitemap watch · `slop.py` AI-tell rules · `main.py` CLI · `tests/` stage checks.

## Publishing through a pull request

Set a `publish` block in a site config and every live `run` ends as a pull request on the site's repository
(example below):

- **Before the run spends anything**, the site checkout must be on `base_branch` with no uncommitted tracked
  changes and no unpushed commits. It is then fast-forwarded to origin. Otherwise the run is skipped with an alert.
- **After the articles are written**, exactly the engine's files (the articles and the route inventory) move to a
  new `content/<timestamp>` branch, the `checks` run on it, and the branch is pushed with a pull request.
- If a check fails, the branch stays local and unpushed, the checkout returns to `base_branch`, and the alert says which
  check failed. By default nothing is merged automatically; merging the pull request is what publishes.
- **Self-merge** (`ship_mode: merge_when_checks_pass`): once every local check passed, the pull request is merged with
  `merge_method` (squash by default) and the branch deleted, so scheduled runs publish with no one in the loop. With
  `wait_for_remote_checks: true` it uses `gh pr merge --auto` instead, which merges after the repository's required CI
  passes. A merge that fails leaves the pull request open and goes into the weekly report.

```yaml
publish:
  repo: "~/Documents/my-site"
  checks: ["npm run build"]
  ship_mode: merge_when_checks_pass   # default: pr
  merge_method: squash                # squash | merge | rebase
  wait_for_remote_checks: false
```

Needs the `gh` CLI signed in. Dry runs never publish.
