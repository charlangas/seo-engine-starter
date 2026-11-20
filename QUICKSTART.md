# Quickstart

From nothing to a site that publishes on its own, within a budget you set. The [README](README.md) has everything
else; you do not need it to start.

## What this is, and what it assumes

The engine researches keywords, studies what ranks, writes articles with one model and has a second model edit them,
then saves them as Markdown files. Every week it checks Search Console, refreshes pages that slip, adds internal
links and sends you a short report.

It assumes:

- **An [Astro](https://astro.build) site with a content collection** (for example `src/content/blog/`). The engine
  writes `.md` files with frontmatter into that folder. Other static site generators that read Markdown folders
  work too if the frontmatter fits.
- **Python 3.11 or newer** (tested on 3.14).
- **macOS for the daily schedule** (a script sets it up). On Linux, use cron (below).
- Optional: **the site in a GitHub repository and the `gh` CLI signed in**, so new pages arrive as pull requests,
  which can merge themselves once the site builds.

## What it costs

You pay the services directly. The engine records what each call cost, and stops at the monthly budget you set.

| Service | Needed for | Required |
|---|---|---|
| [DataForSEO](https://dataforseo.com) | keyword volumes, difficulty, ideas | yes (pay as you go) |
| [SerpAPI](https://serpapi.com) | the current Google results for each keyword | yes (monthly plan) |
| [OpenRouter](https://openrouter.ai) | the writer and editor models | yes, for real articles (without it you get template drafts) |
| Google Search Console | the daily check, refreshes, indexing, and the signal to publish more | free; strongly recommended |

Keyword data is cached for 90 days, so research gets cheaper after the first month. After your first few pages,
`main.py spend` shows what a page really costs you.

## 1. Install

```bash
git clone <this repository> seo-engine
cd seo-engine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`: `DATAFORSEO_LOGIN`, `DATAFORSEO_PASSWORD` (the API password from the DataForSEO dashboard, not your
login password), `SERPAPI_KEY` and `OPENROUTER_API_KEY`. Set `MONTHLY_BUDGET_USD` too if you want one cap across all
your sites.

Check the install. This makes no paid calls:

```bash
.venv/bin/python tests/run_all.py
```

## 2. Describe your site

```bash
cp configs/example-local-services.yaml configs/my-site.yaml
```

Edit `configs/my-site.yaml`. The fields that matter most:

| Field | What to put |
|---|---|
| `site_name`, `domain` | your site |
| `business_type` | `local_services`, `ecommerce`, `agency`, `architecture` or `other` |
| `niche_description` | one or two sentences on what you sell and to whom; the writer reads it |
| `seed_keywords` | 4 to 8 phrases your buyers search; research grows from these |
| `location` | `City, State, Country` for a local business, `null` for a whole country |
| `output_directory` | your site's content folder, e.g. `~/code/my-site/src/content/blog` |
| `enable_directory` | `false` unless you want local business listings pages |
| `publishing.per_week` | **start at 2** |
| `budget.monthly_usd` | the most this site may spend in a calendar month |
| `writer_notes` | facts and limits for the writer: what you do, what never to claim |

If your site has only a few pages, lower `volume_floor` (minimum monthly searches) to 50 or 100 and keep `kd_max`
(maximum keyword difficulty) at 40 or below. A new site cannot win hard keywords yet.

## 3. Dry run

Research, briefs and drafts go to `dry-run/<site>/`. Nothing touches your site. This does spend money, so you see
the real cost.

```bash
.venv/bin/python main.py run --config configs/my-site.yaml --dry-run
.venv/bin/python main.py spend --config configs/my-site.yaml
```

Read `dry-run/<site>/ranked_keywords.json` (are these keywords your buyers would search?) and the drafts in
`dry-run/<site>/content/`. If research drifts off-topic, add `required_terms` and `excluded_terms` (see the README,
"Stage rules").

## 4. First live run

Set `max_keywords_per_run: 1` for the first time, run it, and read the page on your site before you trust it with more.

```bash
.venv/bin/python main.py run --config configs/my-site.yaml
```

To get new pages as pull requests on your site's repository instead of files in your checkout, add a `publish` block
(README, "Publishing through a pull request").

## 5. Search Console (recommended)

Create a Google Cloud service account, download its JSON key, add the service account's email as a user on your
Search Console property, and set `GSC_SERVICE_ACCOUNT_FILE=/path/to/key.json` in `.env`. Details: README, "Daily
Search Console check". Without it the engine still writes, but it cannot see what ranks, refresh pages that slip,
or tell you when it is safe to publish more.

## 6. Turn on autopilot

In `configs/my-site.yaml`:

```yaml
autopilot:
  enabled: true
```

Check which jobs would run today, then schedule the daily job:

```bash
.venv/bin/python main.py autopilot --config configs/my-site.yaml --dry-run
sh scripts/install_autopilot.sh
```

On Linux, add this line with `crontab -e` instead: `0 7 * * * cd /path/to/seo-engine && .venv/bin/python main.py autopilot --all`

Every day the engine does what is due: it writes a page on your publishing days, checks Search Console, and
refreshes and links pages once a week. It never spends past your budget: near the cap it drops optional work first,
then refreshes, and new pages stop at the cap until the 1st.

## 7. Your weekly routine

The report lands in `data/<site>/autopilot/report-<date>.md` (and Slack, if you set `SLACK_WEBHOOK_URL`). It ends with
at most three things only you can do: fix something on the site, answer a question about your business, or approve a
faster pace. Ten minutes a week.

```bash
.venv/bin/python main.py spend --config configs/my-site.yaml   # money this month, cost per page
.venv/bin/python main.py pace --config configs/my-site.yaml    # pace, next dates, signals for publishing more
```

## When to publish more

Stay at 2 pages a week for the first months. Publishing more does not earn favour with Google; it only pays off when
Google is already indexing and showing what you publish. With `publishing.ramp` set (the example config has it), the
weekly report asks you to go up by one page a week when:

- you have published for at least 6 months,
- Google has indexed at least 80% of your pages from the last 90 days,
- Search Console impressions are up at least 20% on 90 days ago,
- there are enough good keywords left, and
- your budget covers the faster pace.

To approve, change `publishing.per_week`. If Google stops indexing your new pages, the report asks you to slow down.
