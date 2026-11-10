# Validation — 10 November 2026

- Uploaded XLS workbook: 243 product rows imported; invoice header detected on row
  6; first product starts on row 7; totals and invoice metadata excluded.
- Live ArzonApteka Chromium crawl: search for АКВАДЕТРИМ returned eight variants.
  Only 4000 МЕ capsules №30 with matching Polpharma manufacturer was accepted;
  starting price 53,400 UZS, invoice price 60,000 UZS, delta -11%.
- OsonApteka: direct HTTP and ordinary Chromium returned SafeLine WAF Access
  Forbidden; robots.txt returned 403/challenge HTML. Current adapter reports error
  and produces no offers. Oson search selectors/structured-data support remain
  provisional and require live validation once permitted access is available.
- 24 automated tests passed. Core tests cover CSV/XLSX import, totals, unit conversion, dosage/count/form and
  manufacturer conflicts, identifier conflicts, conservative review, price
  estimation, deduplication, run history, manual review, source errors, CSV formula
  escaping, parser isolation and local-origin protection.
- Browser checks passed at desktop 1440px and mobile 390px: real XLS upload,
  243-row import, first-five selection, filtering, manual product creation and
  rendering a real stored crawl report. No JavaScript errors or mobile overflow.
- OpenAI requests were not executed because no key was supplied. Structured schema
  and evidence checks are covered locally; live model access remains unverified.
- GitHub repository creation and writing both returned permission errors. The user
  elected to download the ZIP and push to master themselves. No invoice, database,
  API key, browser session or uploaded workbook is included in the source package.

## Embedded admin browser update

- 31 automated tests pass, including admin login/session/logout, WebSocket origin
  rejection, connection prerequisites, actual-price checks, expiry, browser input
  locks, human-verification pause/resume, cancellation and restart recovery.
- Real Chromium integration check (`PYTHONPATH=. python tests/browser_smoke.py`)
  exercises a synthetic Allow button, mouse input, text and keyboard control, JPEG
  frame streaming, disconnect cleanup and a delayed product-card hydration fixture.
  This fixture does not claim to solve a real website's verification challenge.
- Live panel test: imported the 243-product XLS, opened an embedded Arzon browser,
  received JPEG frames over WebSocket, checked a real search returning eight
  priced listings, and collected the matched 4000 МЕ №30 listing at 53,400 UZS.
  Both-source gating was checked, then Arzon alone was explicitly selected for this
  live collection because Oson access remains blocked. Desktop and mobile layout
  checks passed with no JavaScript errors.
- Oson hard WAF denial remains unresolved. The panel supports showing it to a
  human; successful two-source collection is not claimed. Live OpenAI requests
  remain untested without an API key.
