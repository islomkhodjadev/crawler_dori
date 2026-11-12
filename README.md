# Dori — pharmacy price comparison

Python + Chromium + FastAPI + SQLite. Import an invoice, find matching medicine
packages, compare observed prices and estimate **current market price** in UZS.
An admin panel with Excel upload, embedded interactive Chromium browsers, connection checks, human-verification pause/resume, CLI, CSV export and optional OpenAI parsing is included.

## Current source status (10 November 2026)

| Source | Implementation | Live verification |
| --- | --- | --- |
| ArzonApteka | Chromium search and observed product-card parser | Passed: 8 Аквадетрим variants; 4000 МЕ capsules №30 matched at 53,400 UZS |
| OsonApteka | Observed `/search?q=` navigation, AI text + screenshot assessment, visible-card and JSON-LD fallbacks | User screenshot shows accessible priced cards; matching HTML/Chromium fixtures pass. Cloud-side live verification remains blocked by WAF. |

The app works with one accessible source and labels the estimate `single_source`.
It does **not** claim a completed two-site comparison until both sources have
accepted offers. Oson requires permitted live access; AI and card extraction were validated with representative fixtures, while end-to-end local verification remains necessary;
this project cannot guarantee that browser session cookies resolve its block.
No endpoints, prices or pharmacy offers are invented.

## One-command launch on macOS / Linux

Install Python 3.11+ first. Then:

```bash
git clone https://github.com/islomkhodjadev/crawler_dori.git
cd crawler_dori
bash run.sh
```

Open **http://127.0.0.1:8000**. The launcher creates `.venv` if needed, installs or
updates the package, checks/downloads Chromium, and creates `.env` only if it does
not exist. It preserves your API key, browser profiles and database. First launch
needs internet access for installation. Stop with **Control+C**.

To update an existing checkout and restart:

```bash
git pull --ff-only origin master
bash run.sh
```

For another port: `bash run.sh --port 8001`. The executable form `./run.sh` also
works. `bash run.sh --help` shows the available launcher options. Configure your
OpenAI key and optional admin password in `.env`, then restart the app.

## Quick start

Requires Python 3.11+ (tested on 3.12), and a supported Chromium system environment.
Run from the extracted `crawler_dori` directory:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m playwright install chromium
cp .env.example .env
dori serve
```

Windows PowerShell activation: `.venv\Scripts\Activate.ps1`; copy config with
`Copy-Item .env.example .env`.

Open **http://127.0.0.1:8000**. On Linux, if browser libraries are missing, install
Playwright's documented OS dependencies (`python -m playwright install-deps chromium`)
using your machine's administrator/package-management process.

1. Import your `.xls`, `.xlsx` or UTF-8 `.csv` workbook. The first five products are selected for a small initial crawl.
2. In **Browser connections**, choose an imported product for the connection test.
3. Click **Open browser & verify** for each website. A live Chromium viewport opens inside the admin panel. Click and type directly in it to complete verification, choose the city, or accept consent when the website offers those controls. Use the text field beneath the viewer if direct typing is inconvenient.
4. Click **Check connection**. It uses that same browser session to search the test product and verify real priced listings. A loaded homepage alone does not pass. If a product has no results, choose another test product.
5. Once both sources are connected, select your product rows and click **Start collecting prices**. You can explicitly deselect an unavailable source to collect from one website; resulting estimates are labeled single-source.
6. If a site presents verification during a run, the run enters **waiting for human**. Open its browser, complete verification, then **Check connection** to resume the same product. **Cancel collection** preserves results collected so far.
7. Review uncertain matches and export the price comparison as CSV.

Connection checks expire after 15 minutes before a new collection starts. Sessions
and cookies persist in separate local profiles per website; the running browser is
reused by the collector. While collection controls a site, human input is disabled
until a verification pause. One interactive controller is allowed per website.

## Admin login and deployment

For password-protected access, set a strong password in your local `.env` and restart:

```dotenv
DORI_ADMIN_PASSWORD=choose_a_long_unique_password
DORI_ADMIN_PROFILES=data/browser-profiles
```

Sign in at `/login`; the session lasts eight hours. Without a configured password,
the panel is intended only for a trusted local machine using its default loopback
binding. Do not expose the server publicly without setting a password and HTTPS.
Browser cookies and profiles are private and must not be committed.

The embedded browser uses same-origin WebSockets (`/api/browsers/{site}/stream`),
JPEG viewport frames, and validated mouse/keyboard commands. No VNC installation,
external remote-desktop provider or exposed Chromium debugging port is needed.
A reverse proxy must forward WebSocket Upgrade/Connection headers and preserve
the external Host/Origin. Configure trusted forwarded HTTPS headers so login cookies
are marked Secure. Run one application worker per database/browser-profile set.

A website's hard deny or lack of an accessible product parser can still prevent a
successful check. There is no universal “Allow” button. This workflow supports
human verification offered by the website; it does not guarantee Oson WAF access.

Invoice headers may start below company details and merged cells. The supplied
workbook was verified to import **243 products**. Repeated imports append a new
batch. The original invoice is intentionally excluded from the source archive;
upload your local copy after starting the app.

Recognized columns: `name` / `Наименование товаров`, `manufacturer` /
`Производитель`, `price` / `Цена`, `quantity` / `Колич.`, `barcode`, `id`,
`arzon_id`, `oson_id`. Names are required; identifiers supplement matching.
Invoice serial/lot numbers are not treated as website product IDs.

## Live progress and statistics

**Collection progress** appears near the top of the admin panel. Choose a crawl run
there to see completed/selected products, a progress bar, the current product and
website, elapsed time, matched products, completed products without an accepted
match, and counts of collected/accepted/review/rejected listings. Per-site rows show
successful searches, no results and errors. Recent source results include reasons.
Counts refresh every four seconds and immediately after a manual match decision.

Product counts and listing counts are different: one product can return many
variants across two sites. A matched product has at least one accepted listing.
**Source status `ok` means listings were returned; it never means an exact match
was accepted.** AI candidates may all await review, so an `ok` Oson source can
still have a blank accepted-price cell. Review those candidates to decide which
match the actual manufacturer, strength and package.

The main CSV preserves its original columns and appends match status, candidate /
accepted / review / rejected counts for each site, and match explanations. Use
**Export candidates** for every raw collected listing, including its price, source,
match status and reasons. Unreviewed/rejected prices do not enter the market
estimate. The product list in Market overview can be filtered by match status.

`GET /api/statistics?run_id=...` returns the selected run's counters and activity;
`GET /api/export/candidates?run_id=...` downloads listing-level evidence. Older
SQLite databases receive additive progress columns automatically; product data,
existing runs, matches and browser sessions are preserved.

## How matching and estimation work

- Normalize Cyrillic/Latin names, case, spacing and common dosage forms.
- Extract strength, volume and package count; normalize supported mass units.
- Reject conflicting strengths, counts, forms, manufacturers and explicit IDs.
- Auto-accept only equal normalized brands with complete, consistent supported
  package attributes and manufacturer evidence. Incomplete data needs review.
- Fuzzy matching proposes candidates; it cannot approve a similar medicine as a
  substitute. Hard attribute conflicts cannot be manually accepted.
- AI-extracted listings always need review, even when label attributes match.

For accepted listings, compute each site's median, then the median across sites.
For example, site medians 50,000 and 60,000 yield a 55,000 UZS estimate. One site's
large listing count cannot dominate the other. Missing/blocked sources remain
missing, never zero. Failed newer crawls do not silently reuse old prices.
Each run preserves dated candidates, source URLs and collection details.

**Price scope:** Arzon search cards advertise a starting/minimum price across
pharmacies, not all individual pharmacy offers. The estimate therefore summarizes
observed advertised starting prices and is not a statistical confidence interval.
Default location is Uzbekistan as shown by the fresh website session; a saved
profile may preserve a different site region. This version does not offer an
explicit city filter. Record/verify the site region before business decisions.
Invoice cost and retail starting prices may differ in tax, discount and margin
basis. `invoice_delta_percent = (estimate / invoice_price - 1) × 100`.

This version estimates current price. It does not forecast future prices.

## Browser access and blocked pages

Arzon's observed robots.txt permits page crawling and disallows `/api/`, so the
adapter uses public rendered search pages. Robots checks precede navigation.
Crawls are sequential, at least three seconds apart by default, bounded to three
linked search pages, and retry transient 502/503/504 once. Access challenges and
rate limits are surfaced as errors. Search pagination supports `rel=next`; if the
site uses a different control, collection remains limited to loaded results.

To establish a permitted session on your computer:

```bash
dori browser-session --site oson --profile browser-profile
```

Use the visible browser, then press Enter in the terminal to save and close it.
Set in `.env`:

```dotenv
DORI_BROWSER_PROFILE=browser-profile
DORI_HEADLESS=false
```

A display is required for visible mode. Close other instances using the same
profile before starting a crawl. Profiles contain cookies and are Git-ignored.
Persistent sessions do not override a denied robots policy or a hard WAF denial.
Oson uses the `/search?q=` route observed in the user’s working browser. Its card
parser groups each visible price with its nearest single-product title; JSON-LD
is no longer required. AI assessment/extraction is preferred when enabled.
The cloud executor still cannot validate the live Oson DOM, so the fallback card
parser is tested against representative HTML and may need further site-specific tuning.
If the operator provides a documented permitted API, add a dedicated adapter
using its actual schema rather than guessing endpoints.

Keep TLS verification enabled. On corporate/cloud proxies, install the approved
CA in your OS and Chromium trust store. `DORI_BROWSER_PROXY` optionally supplies
a proxy server to Chromium. Do not disable certificate verification to fix setup.

## Optional OpenAI usage

No OpenAI key is needed for import, deterministic matching, Arzon crawling or
estimation. Add your key **locally** to `.env`:

```dotenv
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-6-astra
```

Do not paste secrets into source code, commit `.env`, or publish browser profiles.
The UI exposes **AI attributes** only when a key is configured. It sends only the
selected name and manufacturer after confirmation; suggestions are displayed and
never automatically change product identity or prices.

In the panel, **Use AI when the page parser needs help** is checked by default when `OPENAI_API_KEY` is configured; you can uncheck it. Oson uses AI as the primary extractor when enabled, and Arzon uses it when its known parser cannot read the loaded page. Both sites use AI to assess an apparent challenge before requesting human action. The key stays on the server. The checkbox is disabled until a key is configured. For standalone CLI search pages which load but have no supported markup, explicitly enable:

```dotenv
OPENAI_PARSE_HTML=true
```

AI assessment sends up to 24,000 characters of visible source-page text and a viewport screenshot to OpenAI. The model distinguishes visible product prices, human verification, hard access denial, rate limits, no results, loading and unfamiliar pages. Human action is requested only for a verification step. A hard deny, missing results, parser failure or OpenAI configuration/quota error has a separate status. A strict
schema constrains output; names, manufacturers, currency and price values must
be present in the page. These checks cannot prove correct price-to-product
association, so every AI-parsed candidate requires human review. AI cannot recover prices absent from the loaded page. If AI fails and a deterministic Oson card parser can read prices, the app collects them and shows the AI failure as a warning. AI price candidates are collected immediately but require product-match review before entering the estimate. Match review is not a website verification request. API charges and model availability depend
on your OpenAI account. The live OpenAI integration was not exercised without a key.

## CLI and API

```bash
dori import '/path/to/invoice.xls'
dori crawl --limit 5
dori crawl --ids 5,8 --limit 2 --sites arzon,oson
dori report --run-id 1
```

JSON output includes source errors and candidate decisions. `dori serve` uses a
single process and local binding. The admin crawl API requires checked interactive
browser sessions; CLI batch crawling remains an independent noninteractive path. API documentation: http://127.0.0.1:8000/docs.
Principal routes: `/api/import`, `/api/products`, `/api/browsers`,
`/api/browsers/{site}/open`, `/api/browsers/{site}/check`, `/api/crawl`,
`/api/runs`, `/api/runs/{id}/cancel`, `/api/report`, `/api/offers/{id}/review`, `/api/export`.

SQLite lives in `data/dori.sqlite3` (`DORI_DB` can override). Back up the entire
SQLite database, including WAL files when a process is running. The web app marks
unfinished runs interrupted after restart. If a standalone CLI crawl is forcibly
killed, start the web app once to recover its queued/running state before retrying.
Do not run multiple servers/CLI crawls against the same browser profile/database.

The panel supports one administrator password with in-memory signed-in sessions. It does not implement multiple administrator accounts or team-level roles. Use the deployment guidance above before sharing access.

## Monitoring

```bash
dori doctor                      # Python, Chromium, configuration, database, stalled runs
dori monitor                     # live progress of the latest crawl (refreshes every 5 s)
dori monitor --run-id 3 --once   # one snapshot of a specific run
```

`doctor` exits with status 1 when a check fails and never prints API keys or
passwords. A run is reported as stalled when it has shown no activity for ten
minutes; stop it from the panel or restart the app, which marks it interrupted.

## Logs and tracing

Every run writes `data/logs/dori.log` (rotated at 5 MB) and echoes to the terminal.
It records each API request with its status and duration, each browser navigation
and page decision, every search with its method and sample offers, and each AI
request with its model, token use, returned content and any evidence rejections.
Keys and bearer tokens are redacted; typed browser text is recorded only by length.

```bash
tail -f data/logs/dori.log                    # follow live
grep 'page assessment' data/logs/dori.log     # every AI request and response
grep 'search done' data/logs/dori.log         # outcome of each search
grep -B2 -A12 'Traceback' data/logs/dori.log  # full error backtraces
```

Set `DORI_LOG_LEVEL=DEBUG` to include the page text excerpts sent to the AI.

## Tests

```bash
pytest -q
```

See `VALIDATION.md` for the checks performed on the delivered version. Unit tests
use synthetic labels and invoices, and do not require network access or API keys.

## Push this download to your GitHub master branch

From inside the extracted project (the ZIP excludes `.git` and private data):

```bash
git init -b master
git add .
git commit -m "Build pharmacy price comparison app"
git remote add origin https://github.com/islomkhodjadev/crawler_dori.git
git push -u origin master
```

These instructions assume an empty repository. If GitHub already has commits,
clone it first, copy this project's files into the clone, and commit normally.
After pushing, you can set `master` as the default branch in GitHub settings.
