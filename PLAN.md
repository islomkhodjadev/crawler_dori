# Implementation plan

Goal: import the supplied pharmacy invoice, find the same packaged medicines on
OsonApteka and ArzonApteka, compare current UZS prices, and estimate current market
price. This is not a future forecast or a medical substitution tool.

1. Inspect spreadsheet and public website search traffic. Prefer observed public
   JSON endpoints; use Chromium for rendered data where needed. Report challenges
   and failures without bypassing access controls.
2. Import XLS/XLSX/CSV, automatically detect the invoice header, preserve source row,
   manufacturer, quantity and invoice price. Support optional barcode and site IDs.
3. Normalize Cyrillic/Latin names and reject conflicting strength, volume, package
   count, form or manufacturer. Expose uncertain matches for manual approval.
4. Implement independent, rate-limited site adapters, bounded retries, robots checks,
   timeouts and per-site errors. Never treat a failed source as a zero price.
5. Store products, crawl runs, candidate offers and dated observations in SQLite.
   Estimate market price as the median of per-site medians for accepted matches;
   require both websites for a comparison and label single-source estimates.
6. Provide a local web app for upload, crawl progress, candidate review, price table,
   provenance, invoice comparison and CSV export. Add CLI batch execution.
7. Optional OpenAI parsing may suggest structured product attributes, with explicit
   opt-in and validation. It cannot invent prices or approve mismatched variants.
   Secrets remain environment variables, never committed.
8. Test import, matching, estimators, storage and API flows. Run live smoke checks,
   documenting site blockers. Publish source and setup instructions to GitHub.

## Privacy and rollout
The supplied workbook is a business invoice. Keep it and the database out of Git,
particularly because the supplied GitHub repository is public. Use synthetic test
fixtures. Start with a small crawl; full-sheet crawl is a user-triggered action.
Future forecasting requires repeated dated observations and is outside this version.
