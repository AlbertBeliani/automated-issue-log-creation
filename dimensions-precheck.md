# Dimensions Pre-Check — Issue Log Automation

Browser console script that scans the ATS/Deco shipping order queue for orders with
dimension-less articles and creates a single aggregated "DImensions pre-check" Issue Log
entry per order, skipping orders that already have one.

Script: [`dimensions-precheck.js`](dimensions-precheck.js)

## How it works

The app has no single API that does this end-to-end, so the script runs in two phases,
using your existing logged-in browser session (`fetch(..., { credentials: 'include' })`) —
no tokens or cookies are ever hardcoded.

**Phase A — scan the order list**
On `search.php` (the filtered "shipping orders by user" queue, `layout=2`), the script reads
the rendered `#egusli` grid directly from the DOM: for each order row it finds every article
in the "shipping-prices" block and flags ones where height/width/length/weight are all `0`.

**Phase B — resolve each order and check for duplicates**
The `obj_id`/`obj` values needed to create an issue only exist on the order's *detail* page
(`auction.php?number=X&txnid=Y`), embedded in the `Add to issuelog` button's `data-page-id`
/`data-param` attributes. For each order with missing-dimension articles, the script:
1. Fetches that detail page in the background (no visible navigation) and reads `obj_id`/`obj`
   off the button.
2. Calls `GET /api/issueLog/shortList/?obj_id=...&obj=...` (the same endpoint the app's own
   UI uses to render the "Issue Logs" table) and checks whether any existing entry's
   `issue_types` field already contains "dimensions pre-check". If so, the order is skipped.
3. Otherwise, aggregates all missing-article links into one `issue_name` and POSTs to
   `js_backend.php` with `fn=addIssueLog` to create the issue.

Each processed order's key (`number/txnid`) is cached in `localStorage` so re-running the
script the same day doesn't re-check orders it already handled.

## Configuration (`CONFIG` at the top of the script)

| Key | Purpose |
|---|---|
| `DRY_RUN` | `true` logs what would happen without submitting anything. Always test with this first. |
| `LIMIT` | Caps how many orders `run()` processes — use `1`–`3` for staged testing, `Infinity` for a full batch. |
| `LIST_SELECTORS` | CSS selectors for scraping the order grid on `search.php`. |
| `DETAIL_URL` / `ENDPOINT` / `ISSUE_LIST_ENDPOINT` | The three URLs the script talks to. |
| `FIXED_FIELDS` | Constant values for every created issue: department, solving user, tag/issue type, board, column. |

## Usage

1. Navigate to the filtered order queue:
   `search.php?...&shipping_username=ATSDecoOther&...&packed_status=3&repack=3&route_unassigned=1&layout=2`
2. Open DevTools console, paste the full contents of `dimensions-precheck.js`, press enter.
3. Preview matches: `__dpScan()` — inspect the returned array (order numbers, article IDs,
   missing-dimension flags) before doing anything else.
4. Dry run: with `DRY_RUN: true`, run `__dpRun()`. Check the console log for each order's
   `issue_name` and any `[SKIP]`/`[ERROR]` lines.
5. Live test, small batch: set `DRY_RUN: false`, `LIMIT: 1` (or `2`–`3`), re-paste, run
   `__dpRun()`. Manually verify each created issue in the UI (subject, tag, board, column)
   before trusting a larger run.
6. Full batch: once confident, set `LIMIT: Infinity` and run `__dpRun()`.

## Known limitations / not yet validated

- **Multi-article orders untested.** Every order seen so far had exactly one missing-dimension
  article. The multi-line `issue_name` aggregation (joining several links with `\n`) has never
  actually been submitted — treat the first real multi-article order as its own test.
- **"Missing dimensions" rule.** Currently requires height, width, length, *and* weight to all
  be `0`. Confirm this matches the actual business rule (vs. any one dimension being `0`).
- **Only validated on one queue view** (`search.php?...&layout=2`). Other filter combinations
  on the same page are likely fine since it's the same grid template, but untested. A different
  `layout=` value may render differently.
- **Session-bound, not a background job.** This only works while pasted into a console in an
  authenticated tab. It cannot run unattended or on a schedule without a fundamentally
  different (server-side) auth setup.
- **Rate/delay (`delayBetweenOrdersMs: 1500`)** is a guess, not validated against server
  tolerance at scale.

## Security notes

- No cookies, session IDs, or tokens are ever written into this script — it relies entirely on
  the browser tab's existing authenticated session via `credentials: 'include'`.
- If you ever capture a new cURL request to extend this script, strip the `Cookie`/
  `Authorization` header values before saving or sharing the capture.
