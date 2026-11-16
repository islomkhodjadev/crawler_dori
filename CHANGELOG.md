# Dori — Source Document

Pharmacy price comparison system. Imports product lists from Excel/CSV, searches
ArzonApteka and OsonApteka for matching offers, scores each match, and estimates
the market price so the pharmacy can set a competitive retail price.

---

## Stack

- **Python 3.11+**, FastAPI + Uvicorn (async HTTP + WebSocket)
- **Playwright** — headless Chromium for browser-based crawling
- **SQLite** in WAL mode — single file `data/dori.sqlite3`
- **OpenAI GPT-4o-mini** — optional; used for query variants and page analysis
- **Chrome extension** (Manifest V3) — routes Oson fetches through the user's browser
- **openpyxl / xlrd** — Excel import and export
- **rapidfuzz** — fuzzy name matching
- Entry point: `dori` CLI → `dori.cli:main`

---

## Package layout

```
dori/
  app.py           FastAPI application, all HTTP + WS endpoints
  cli.py           CLI entry point (serve / import / crawl / monitor / doctor)
  storage.py       SQLite store, all persistence
  service.py       Crawl orchestrator
  adapters.py      HTTP + browser crawlers, HTML parsers, Arzon + Oson
  matching.py      Name normalisation, attribute extraction, offer scoring
  ai.py            OpenAI calls: search variants, page assessment, price advice
  estimate.py      Market price from accepted offers (median of medians)
  importer.py      Excel / CSV → product rows
  agent.py         AgentBus: WebSocket channel to Chrome extension
  browser_admin.py BrowserAdmin: persistent Chromium sessions per site
  browser_config.py Playwright launch options (headless/visible, proxy, channel)
  challenge.py     WAF / CAPTCHA state detection, settle() wait loop
  page_state.py    Lightweight text-based page classifier
  oson_cards.py    Visible card parser for Oson (price + nearest title)
  auth.py          Admin password, session cookie, rate limiting
  logs.py          Rotating file + console logging, secret redaction
  monitor.py       CLI health check (doctor) and live progress (watch)
  session.py       Open a visible browser to build a permitted session
  static/
    index.html     Single-page admin panel
    app.js         All frontend JS (~1 100 lines)
    i18n.js        UI strings RU / UZ / EN
    style.css      Panel styles
chrome-extension/
  manifest.json    Manifest V3
  background.js    Service worker — WebSocket client, fetch task handler
  popup.js / popup.html  Connection UI
```

---

## Data model (`storage.py`)

Five SQLite tables:

| Table | Key columns |
|---|---|
| `products` | `id`, `batch` (import filename), `data` (JSON) |
| `runs` | `id`, `started_at / finished_at`, `status`, `total / completed`, `active_product_id / active_site`, `phase`, `last_activity`, `sites` (JSON) |
| `results` | `id`, `run_id`, `product_id`, `site`, `status`, `details` (JSON) |
| `offers` | `id`, `result_id`, `data` (JSON), `match_status`, `score`, `reasons` (JSON), `reviewed_at`, `manual_price`, `price_edited_at` |
| `price_advice` | `run_id`, `product_id`, `price / low / high / reason`, `model`, `created_at` |

`Store` methods:
- `import_products(rows, batch)` — upsert by `(name, manufacturer)`, returns new IDs
- `products(ids=None)` — list products, optionally filtered
- `create_run(products, sites)` — open a run row, returns `run_id`
- `update_run(run_id, **fields)` — partial update, also stamps `last_activity`
- `recover()` — called on startup; moves stuck `running` runs to `interrupted`
- `save_result(run_id, product_id, site, details, offers)` — write result + matched offers
- `review(offer_id, status)` — accept / reject an offer manually
- `save_advice(run_id, product_id, price, low, high, reason, model)` — store AI price advice
- `set_price(offer_id, price)` — record an operator-entered manual price
- `report(run_id=None)` — full per-product report with offers and price advice
- `statistics(run_id=None)` — counters used by admin panel and CLI monitor

---

## Import (`importer.py`)

`read_products(data: bytes, filename: str)` accepts `.xls`, `.xlsx`, `.csv`.

Header detection scans each row for column aliases (case-insensitive, in any language):

| Field | Aliases |
|---|---|
| `name` | наименование товаров, наименование, название, product name, name, товар |
| `manufacturer` | производитель, manufacturer, завод |
| `invoice_price` | цена, price, invoice_price, purchase price |
| `quantity` | колич., количество, quantity, qty |
| `external_id` | id, код, product_id, артикул |
| `barcode` | barcode, штрихкод, штрих код, ean |
| `oson_id` | oson_id |
| `arzon_id` | arzon_id |

Rows whose name matches `/^(итого|всего|total|сумма)/i` are skipped (totals rows).
Multi-sheet workbooks are all processed; each row records `sheet` and `source_row`.

---

## Crawl service (`service.py`)

`crawl(store, run_id, products, sites, admin, use_ai, agent_bus)` iterates every
`product × site` pair:

```
for product in products:
    for site in sites:
        if admin and site == 'oson' and agent_bus and agent_bus.connected():
            offers, details = agent_search_oson(agent_bus, product, use_ai)
        elif admin:
            offers, details = admin.search(site, product, use_ai)
        else:
            offers, details = browser.search(site, product, use_ai)
```

On `VerificationRequired` with an admin session: pauses the run
(`status = waiting_for_human`), calls `admin.wait_for_human(site)`, then retries.
Without an admin session: saves an error result and continues.

Cancelled via `asyncio.CancelledError` → status `interrupted`.

---

## Adapters (`adapters.py`)

### HTTP path (no browser)

`BrowserCrawler` — Playwright async context manager.

- `navigate(url)` — fetches the page, calls `challenge.settle()` to let JS/WASM
  finish, raises `VerificationRequired` if `human_verification` or `access_blocked`
  is returned after the settle timeout.
- `search(site, product, use_ai)` → calls `_search` which builds the search URL,
  navigates, parses HTML, optionally calls AI if the page state is ambiguous.
- `assess_page(text, html)` — calls `ai.analyze_visible_page()` if the text-based
  classifier returns `unknown`; upgrades the state.
- `ai_extract(text, page_url)` — GPT-4o-mini extracts structured offers from plain
  text when normal DOM parsing returned nothing.

### Arzon parser

`parse_arzon(html, page_url)` — BeautifulSoup. Finds product cards, extracts name,
manufacturer, price (strips " сум"), URL. `price_value(text)` strips ` `, spaces,
converts to float.

### Oson parser (JSON-LD path)

`parse_oson_jsonld(html, page_url)` — walks `<script type="application/ld+json">`
blocks, picks `Product` / `ItemList` entries, normalises price into UZS.

### Oson card parser (`oson_cards.py`)

`parse_oson_cards(html, page_url)` — walks the DOM for visible price nodes
(`\d[\d\s.,]* сум`), walks up ≤7 parent levels to find the nearest product title.
Guards against: pharmacy headings ("999 Apteka №1"), price wrappers counted twice,
multi-medicine containers.

### Agent path (Chrome extension)

`_fetch_oson_page(agent_bus, query)` — sends one `{type:"fetch", id, url, site}`
task to the connected extension via `AgentBus.fetch()`, returns `{html, url}`.

`agent_search_oson(agent_bus, product, use_ai)`:
1. Builds primary search URL from `search_term(product, 'oson')`.
2. Fetches via extension, parses JSON-LD + visible cards.
3. If no offers and `use_ai`: calls `search_term_variants()` for up to 3 alternate
   queries, tries each one until offers are found.

---

## Matching (`matching.py`)

### Name normalisation

`norm(s)` — lowercase, collapse whitespace, strip trailing punctuation.

### Attribute extraction

`attributes(name)` — two-pass parenthesis stripping:
1. Strip any `(…)` block that contains a Latin brand name or is > 60 chars.
2. Strip any remaining `(…)` block that itself contains no digit+unit pattern.

Then extracts:
- **dosage** — `\d+[\.,]?\d*\s*(мг|mg|мл|ml|г\b|g\b|ме|IU|%)`
- **form** — tablet, capsule, ampoule, cream, spray, drops, … (60+ RU/EN/UZ variants)
- **package** — `№\d+`, `N\d+`, `\d+\s*шт`, `\d+\s*tab`
- **route** — oral, injection, topical, inhalation, nasal

### Search term

`search_term(product, site)`:
- For Oson: strips leading Latin-only words (e.g. "NOW "), then re-attaches any
  letter+digit suffix that was split off (e.g. "Д3").
- For Arzon: returns name as-is.

### Scoring

`match(product, offer)`:
- Base score from `rapidfuzz.token_sort_ratio` on normalised names, scaled to 0–1.
- **Manufacturer bonus/penalty**: `+0.15` if same manufacturer; `−0.1` if different
  and both are known.
- **Attribute conflicts**: wrong dosage, wrong form, wrong package → `−0.2` each,
  flags `conflict`.
- Threshold `score ≥ 0.65` → `review`; exact name match without missing attributes →
  `accepted` automatically; any `conflict` flag → `rejected`.

---

## AI module (`ai.py`)

All calls use `OPENAI_API_KEY`. If key absent, functions return empty/default values
silently.

| Function | Model | Purpose |
|---|---|---|
| `suggest_attributes(name)` | gpt-4o-mini | Extract dosage/form/package from a messy name |
| `search_term_variants(product)` | gpt-4o-mini | Up to 3 alternate search queries |
| `validate_visible_offers(text, product)` | gpt-4o-mini | Confirm visible offers match the product |
| `analyze_visible_page(text, url)` | gpt-4o-mini | Classify page state: prices_visible / human_verification / access_blocked / rate_limited / no_results / loading / unknown |
| `recommend_price(product, offers, invoice_price)` | gpt-4o | Price recommendation with low/high range and reason |

---

## Price estimation (`estimate.py`)

`estimate(offers, invoice_price)`:
1. Filter to `match_status = accepted`, currency `UZS`.
2. Per site: compute median price.
3. Return median of per-site medians (equal weight per source regardless of offer count).

---

## WAF handling (`challenge.py`, `page_state.py`)

`page_state.classify_text(text)` — text-only triage:
- `prices_visible` — price pattern `\d… сум` found
- `human_verification` — captcha / "я не робот" keywords
- `access_blocked` — 403/forbidden keywords
- `rate_limited` — 429 / "too many requests"
- `no_results` — "ничего не найдено" / "лекарств: 0"
- `unknown` — none of the above

`challenge.settle(page, response, timeout)`:
- Polls `page.locator('body').inner_text()` every 400 ms.
- Detects SafeLine WAF markers: `sl-challenge`, `leading_zero_bit`, `calc.wasm`, HTTP 468.
- If `automatic_verification`: waits for the browser's own JS/WASM to pass the
  challenge — no token replay, no cookie injection.
- Returns `Outcome(status, state, automatic)`.

---

## Browser admin (`browser_admin.py`)

`BrowserAdmin` manages one Playwright `BrowserContext` per site, stored as `Session`
dataclasses.

- `open(site)` — launches persistent Chromium profile at `data/browser-{site}/`,
  streams JPEG frames back to the admin panel.
- `check(site, product)` — runs a real search with the live browser, returns offers.
- `require_ready(sites)` — raises `VerificationRequired` if a session is older than
  15 minutes (forces re-check before each crawl).
- `wait_for_human(site, reason)` — sets `session.ready` asyncio Event to False,
  blocks until the operator clicks "Mark Ready" in the panel.
- `stream(websocket, site)` — streams JPEG screenshots at 0.7 s intervals over a
  WebSocket; used by the admin panel's browser viewer.

Browser config (`browser_config.py`):
- `DORI_BROWSER_CHANNEL` — `chromium` (default) or `chrome`
- `DORI_HEADLESS` — `true` / `false`
- `DORI_BROWSER_PROXY` — proxy server URL
- Servers without a display get `--headless=new` (Chrome's modern headless, same
  renderer as visible Chrome, not flagged by WAF).

---

## Chrome extension agent (`agent.py`, `chrome-extension/`)

### Server side — `AgentBus`

- `_websockets: list` — connected extension instances (keeps last connected)
- `_pending: dict[str, asyncio.Future]` — in-flight tasks keyed by UUID
- `attach(ws)` — message loop for one WebSocket; dispatches replies to pending futures
- `fetch(url, site) → dict|None` — creates `{type:"fetch", id, url, site}` task,
  waits 30 s, returns `{html, url}` or `None` on timeout/error
- `connected() → int` — number of active WebSocket connections

### Extension side — `background.js` (service worker)

- Keeps a persistent `wss://` WebSocket to `/api/agent/ws?password=…`
- Reconnects after 5 s on close; Chrome alarms keep the service worker alive (MV3)
- On `{type:"fetch"}` task:
  1. Validates target host is `osonapteka.uz` or `arzonapteka.uz`
  2. Opens a background tab (`active: false`)
  3. Waits for `tabs.onUpdated` → `complete`, then 2 s settle
  4. Injects `document.documentElement.outerHTML` via `scripting.executeScript`
  5. Sends `{id, html, url, status:200}` back over WebSocket
  6. Closes the tab

### Extension UI — `popup.js`

- Pre-fills Server URL with `https://predictor.03dorixona.uz` (from `chrome.storage.local`)
- Sends `{type:"connect", serverUrl, password}` to background on click
- Shows live status dot: `connected / connecting / disconnected / error`
- Shows tasks-handled counter

---

## HTTP API (`app.py`)

All endpoints under FastAPI. Admin endpoints require `dori_admin` cookie.

| Method | Path | Description |
|---|---|---|
| GET | `/` | Serve `index.html` |
| GET | `/api/health` | `{"status":"ok"}` |
| POST | `/api/import` | Upload Excel/CSV; calls `importer.read_products` → `store.import_products` |
| GET | `/api/products` | List products |
| POST | `/api/products` | Add a single product manually |
| GET | `/api/runs` | List all runs |
| POST | `/api/crawl` | Start a crawl run (product IDs + sites + use_ai flag) |
| POST | `/api/runs/{run_id}/cancel` | Cancel a running crawl |
| GET | `/api/browsers` | List browser sessions + status |
| POST | `/api/browsers/{site}/open` | Open a browser session for a site |
| POST | `/api/browsers/{site}/check` | Mark a browser session as ready |
| WS | `/api/browsers/{site}/stream` | Stream JPEG screenshots |
| GET | `/api/agent/status` | Extension connection status |
| POST | `/api/agent/check` | Trigger a test fetch via extension |
| GET | `/api/agent/extension.zip` | Download the Chrome extension as a zip (built on-the-fly from `chrome-extension/`) |
| WS | `/api/agent/ws` | WebSocket channel for Chrome extension |
| GET | `/api/statistics` | Live run counters for the admin panel |
| GET | `/api/report` | Full report JSON for a run |
| POST | `/api/offers/{offer_id}/review` | Accept or reject an offer |
| POST | `/api/offers/{offer_id}/price` | Set a manual price on an offer |
| POST | `/api/products/{product_id}/optimal-price` | Ask AI for a price recommendation |
| POST | `/api/products/{product_id}/suggest-attributes` | Ask AI to parse product attributes |
| GET | `/api/export` | CSV export of results |
| GET | `/api/export/candidates` | CSV export of all offers |
| GET | `/api/export/xlsx` | Excel export of results (green header, auto-width) |
| GET | `/api/export/candidates/xlsx` | Excel export of all offers |

App startup: `create_app()` creates `Store`, calls `store.recover()`, starts
`BrowserAdmin` and `AgentBus`, mounts static files.

---

## Auth (`auth.py`)

`AdminAuth`:
- Single password from `DORI_ADMIN_PASSWORD` env var
- In-memory sessions, 8-hour expiry, `dori_admin` cookie (httponly, samesite=strict)
- Rate limiting: 5 failed login attempts per 60 s per IP → 429

---

## Logging (`logs.py`)

- Rotating file: `data/logs/dori.log`, 5 × 5 MB
- Console: `HH:MM:SS LEVEL message`
- `DORI_LOG_LEVEL=DEBUG` enables page text excerpts sent to AI
- `DORI_LOG` overrides log file path
- Secret redaction: strips `sk-…`, `Bearer …`, `OPENAI_API_KEY=…` before writing

---

## Monitoring and diagnostics (`monitor.py`)

`doctor()` — returns `(name, level, detail)` rows:
- Python version ≥ 3.11
- Chromium installed via Playwright
- `DORI_ADMIN_PASSWORD` set
- `OPENAI_API_KEY` set
- SQLite integrity check, table presence, product count
- Stalled runs (running but silent > 10 min)
- Runs stuck in `waiting_for_human`

`watch(store, run_id, interval, once)` — prints live progress until run finishes.

CLI usage:
```
dori doctor
dori monitor [--run-id N] [--interval 5] [--once]
```

---

## Browser session bootstrap (`session.py`)

`open_session(site, profile)` — opens a visible browser at the target site so the
operator can manually pass any initial login/CAPTCHA. Saves the persistent Playwright
context to `profile/` for reuse.

```
dori browser-session --site oson --profile browser-profile
```

---

## Admin panel (`static/`)

Single-page app. Five workflow tabs + Help:

| Tab | Purpose |
|---|---|
| 1 Products | Upload Excel/CSV or add manually; select products for crawl |
| 2 Connect | Download + configure Chrome extension; manage browser sessions |
| 3 Collect | Start / cancel a crawl; live progress bar and site-level counters |
| 4 Review | Accept / reject ambiguous offers; set manual prices |
| 5 Results | Full report table with market price, optimal price, export buttons |
| ? Help | Built-in usage documentation in RU / UZ / EN |

UI strings live in `i18n.js` with keys for every label in all three languages.
Language is persisted in `localStorage`.

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DORI_ADMIN_PASSWORD` | — | Required for the admin panel (omit to run unauthenticated locally) |
| `OPENAI_API_KEY` | — | Enables AI features |
| `DORI_DB` | `data/dori.sqlite3` | Database path |
| `DORI_LOG` | `data/logs/dori.log` | Log file path |
| `DORI_LOG_LEVEL` | `INFO` | Log verbosity (`DEBUG` logs AI page excerpts) |
| `DORI_HEADLESS` | `true` | Set `false` to show Playwright browser windows |
| `DORI_BROWSER_CHANNEL` | `chromium` | `chromium` or `chrome` |
| `DORI_BROWSER_PROXY` | — | Proxy for Playwright requests |

---

## Deployment

Production server: `predictor.03dorixona.uz`
Install: `pip install -e .` in `/opt/dori/app/`
Service: `systemctl restart dori`
Deploy: `git pull && systemctl restart dori`
