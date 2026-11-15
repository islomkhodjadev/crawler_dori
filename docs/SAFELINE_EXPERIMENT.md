# SafeLine browser-verification experiment

Branch: `experiment/safeline-browser-verification`. Based on `7700547`.
This branch can be deployed independently. Do not merge it into master until
verified on the intended host and browser profile.

## Included

- Detect HTTP 468, SafeLine challenge-script markers, explicit human verification,
  debugging detection, hard denials and rate limits as separate outcomes.
- Allow the website's own JS/WASM to execute in Chromium within a bounded wait.
  Observe main-frame navigation so the original 468 is not mistaken for the final
  response. Confirm a granted session with a normal repeat request when needed.
- Fetch robots.txt through the same browser context, cookies and browser identity
  as the search pages. No separate HTTPX bot request. Cache policy for five minutes.
  Missing policy (404) is handled; Disallow and unreadable policy still stop crawling.
- Respect Retry-After seconds and HTTP dates, with one retry for 429/502/503/504.
  If the server's requested delay exceeds the configured maximum, report the delay
  without retrying earlier than requested. No unbounded retry or verification loop.
- Show loading, automatic verification, backoff and timeout states in RU/UZ/EN.
  Keep the existing persistent browser profiles, human handoff and AI price parsing.
- Never store verification cookie values in diagnostics or source code.

## Deliberately not inferred

The supplied proposal's direct SHA1 route needs the actual challenge parameters,
verification endpoint and payload. Those were not present in our observed Oson
403 pages. This experiment runs browser-delivered verification code; it does not
invent a PoW endpoint, forge tokens, spoof fingerprints, install stealth patches,
or claim to solve a hard WAF denial. Native Chromium already supports JS/WASM.
If the site reports Debugging Detected, changing headless mode alone is not a
verified fix. Obtain permitted access if the browser continues to be denied.

## Run on macOS

Stop the currently running app, then inside the repository:

```bash
git fetch origin
git switch --track origin/experiment/safeline-browser-verification
bash run.sh
```

For repeat updates on that branch: `git pull --ff-only` then `bash run.sh`.
The branch supports the existing `.env`; the new settings have defaults:

```dotenv
DORI_VERIFICATION_TIMEOUT=30
DORI_MAX_BACKOFF=30
```

To use a visible Chromium window on a Mac, optionally set `DORI_HEADLESS=false`.
Linux visible mode requires an existing display; default headless mode can still
be controlled through the embedded viewer. Do not fake a Mac User-Agent on Linux.

Use a separate deployment directory and separate `DORI_DB` and
`DORI_ADMIN_PROFILES` values when running master and this branch simultaneously.
Never open the same persistent profile from both servers. Back up the stopped
app's data before experimenting.

## Validation and limitations

Unit tests cover response detection, numeric/date Retry-After, refusal of an early
retry, navigation completion and timeout cleanup. Real Chromium fixtures simulate
an HTTP 468 that runs its own script, creates its own cookie and reloads; the
robots policy and product request share that session. Fixtures also test Disallow,
real-human handoff, hard denial and a challenge that never completes.
These are controlled fixtures, not proof of defeating Oson's current WAF. The
cloud's observed live Oson responses were 403 hard denials without the proposed
SHA1 or WASM parameters. Live deployment verification is still required.
