# Dimensions Pre-Check - Issue Log Automation

Browser bookmarklet that scans the ATS/Deco shipping order queue for orders with
dimension-less articles and creates a single aggregated "DImensions pre-check" Issue Log
entry per order, skipping orders that already have one.

Source: [`dimensions-precheck.js`](dimensions-precheck.js)
Bookmarklet (ready to install): [`dimensions-precheck.bookmarklet.txt`](dimensions-precheck.bookmarklet.txt)

## Installing the bookmarklet

One-time setup, per person:

1. Show the bookmarks bar if it's hidden: `Ctrl+Shift+B` in Chrome.
2. Right-click the bookmarks bar -> **Add page** (or **Add bookmark**).
3. Name it something like `Dimensions Pre-Check`.
4. Open `dimensions-precheck.bookmarklet.txt` in a text editor, select all, copy the entire
   line (it starts with `javascript:`), and paste it as the bookmark's **URL**.
5. Save.

Whenever the script changes, update the bookmark's URL with the new file contents rather than
creating a second bookmark.

## How it works

The app has no single API that does this end-to-end, so the script runs in two phases, using
your existing logged-in browser session (`fetch(..., { credentials: 'include' })`) - no tokens
or cookies are ever hardcoded.

**Phase A - scan the order list**
On `search.php` (the filtered "shipping orders by user" queue, `layout=2`), the script reads
the rendered `#egusli` grid directly from the DOM. Each order row can list several articles
side-by-side (a CSS grid, one `<td>` per article); the script finds every genuine article via
its `input.to_calculate_sp[data-id]` checkbox (this also naturally excludes the "shipping
prices for all cartons together" summary column, which has no such checkbox) and flags any
article where height/width/length/weight are all `0`.

**Phase B - resolve each order and check for duplicates**
The `obj_id`/`obj` values needed to create an issue only exist on the order's *detail* page
(`auction.php?number=X&txnid=Y`), embedded in the `Add to issuelog` button's `data-page-id`
/`data-param` attributes. For each order with missing-dimension articles, the script:
1. Fetches that detail page in the background (no visible navigation) and reads `obj_id`/`obj`
   off the button.
2. Calls `GET /api/issueLog/shortList/?obj_id=...&obj=...` (the same endpoint the app's own
   UI uses to render the "Issue Logs" table) and checks whether any existing entry's
   `issue_types` field already contains "dimensions pre-check" - regardless of which predefined
   variant it was created under. If so, the order is skipped (one dimensions-check issue per
   order, period).
3. Otherwise, aggregates all missing-article links into one `issue_name` (one link per line)
   and POSTs to `js_backend.php` with `fn=addIssueLog` to create the issue.

Each processed order's key (`number/txnid`) is cached in `localStorage` so re-running the
script the same day doesn't re-check orders it already handled.

## Predefined issue picker

Each run, the bookmarklet asks which predefined issue to use for the whole batch:

| # | Predefined issue | `board_column_id` | Board column |
|---|---|---|---|
| 1 | `1535626: DIMENSIONS PRE-CHECK ATS/DECO` (default) | `412` | "DImensions pre-check ATS/DECO to do" |
| 2 | `1535622: DIMENSIONS PRE-CHECK` | `383` | "DImensions pre-check to do" |

Confirmed via the `shortList` API on two manually-created test issues: the two predefined
options only change `board_column_id`. The tag (`issue_type[]=1681`, "DImensions pre-check"),
board (`24`, Rajkowo), department, and responsible user are identical either way.

The choice applies to every order processed in that run - there's no per-order picker, since
prompting once per order across a multi-order batch would be impractical.

## Usage

1. Navigate to the filtered order queue:
   `search.php?...&shipping_username=ATSDecoOther&...&packed_status=3&repack=3&route_unassigned=1&layout=2`
2. Click the bookmarklet.
3. Choose a predefined issue (1 or 2) when prompted.
4. Enter how many orders to process this run, or leave blank for all found.
5. The script runs a dry run automatically and shows a popup listing exactly which orders
   will get a new issue, how many are already logged (skipped), and any it couldn't check.
   Nothing is created yet at this point.
6. Click OK on that popup to actually create the issues, or Cancel to stop with nothing
   created.
7. A final popup lists which orders were created and flags any failures by order number.

No DevTools/console access is required for normal use. The script still logs detailed
step-by-step output to the console for anyone debugging.

## Configuration (`CONFIG` at the top of the script)

| Key | Purpose |
|---|---|
| `LIST_SELECTORS` | CSS selectors for scraping the order grid on `search.php`. |
| `DETAIL_URL` / `ENDPOINT` / `ISSUE_LIST_ENDPOINT` | The three URLs the script talks to. |
| `FIXED_FIELDS` | Constant values for every created issue: department, solving user, tag, board. `board_column_id` is overwritten each run based on the predefined issue chosen in the popup. |
| `PREDEFINED_ISSUES` | The two selectable presets and the `board_column_id` each maps to. |
| `delayBetweenOrdersMs` | Pause between orders during a run (currently `1500`ms, a guess - not validated against server tolerance at scale). |
| `dedupStorageKey` | `localStorage` key used to remember processed orders across runs. |

`DRY_RUN` and `LIMIT` are still present in `CONFIG` but are set automatically by the guided
popup flow each run rather than edited by hand.

### Advanced / debugging

The underlying functions are still exposed on `window` for console use if needed:
- `__dpScan()` - returns the raw array of matched orders (order numbers, article IDs,
  missing-dimension flags) without creating anything.
- `__dpRun()` - runs a batch using whatever `CONFIG.DRY_RUN`/`CONFIG.LIMIT`/
  `CONFIG.FIXED_FIELDS.board_column_id` are currently set to, bypassing the popups.

## Known limitations / not yet validated

- **"Missing dimensions" rule.** Currently requires height, width, length, *and* weight to all
  be `0`. Confirm this matches the actual business rule (vs. any one dimension being `0`).
- **Only validated on one queue view** (`search.php?...&layout=2`). Other filter combinations
  on the same page are likely fine since it's the same grid template, but untested. A different
  `layout=` value may render differently.
- **Session-bound, not a background job.** This only works while the bookmarklet is clicked in
  an authenticated tab. It cannot run unattended or on a schedule without a fundamentally
  different (server-side) auth setup.
- **Cross-browser dedup race.** Dedup (both `localStorage` and the live `shortList` check) is
  per-browser. If two people run it on overlapping order sets at nearly the same time, there's
  a small window where both could create an issue for the same order before either dedup check
  would catch it.
- **Page markup dependency.** If `prologistics.info` changes class names, the checkbox
  structure, or the endpoint shapes used here, the script will start silently skipping or
  erroring rather than failing loudly. Worth an occasional spot-check against a known order.

## Security notes

- No cookies, session IDs, or tokens are ever written into this script - it relies entirely on
  the browser tab's existing authenticated session via `credentials: 'include'`.
- Popup text is kept ASCII-only on purpose: the bookmarklet is a URL-encoded copy of the
  script, and non-ASCII characters have previously been corrupted (mojibake) by tools that
  read the source file with the wrong encoding when regenerating that URL.
- If you ever capture a new cURL request to extend this script, strip the `Cookie`/
  `Authorization` header values before saving or sharing the capture.
