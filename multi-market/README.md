# Multi-Market Dimensions Pre-Check Runner

A standalone Python program that runs the same Dimensions Pre-Check automation as the
[bookmarklet](../dimensions-precheck.md), but across every market (DE/AT/FR, NL, OTHER, UK, CH,
PL) and both order types (standard, ATS/DECO) in one guided run - no browser bookmarklet click
needed per queue.

## How it works

It drives a real Chromium browser (via [Playwright](https://playwright.dev/python/)) instead of
running inside a bookmarklet click. For each market/order-type configured in `markets.json`, it:

1. Opens that market's queue URL.
2. Injects [`core.js`](core.js) - the same scan/create engine as the bookmarklet (identical
   selectors, identical "all 4 dimensions are 0" rule, identical dedup logic).
3. Runs a dry run and prints a preview: how many orders would get a new issue, how many are
   already logged, how many couldn't be checked.
4. Asks you to confirm (`y`/`N`) before creating anything for that market/order-type.
5. Moves on to the next market/order-type.

Nothing is ever created without an explicit `y` per market/order-type. A full JSON log of every
run (dry-run results, your decision, live-run results) is written to `logs/run-<timestamp>.json`.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuring `markets.json`

Fill in every `REPLACE_ME` before running - `run.py` refuses to start otherwise. Per
market/order-type you need:

| Field | What it is |
|---|---|
| `queue_url` | The filtered `search.php` URL for that market's queue (same idea as the single-market bookmarklet's usage URL - see the main project's docs for the URL shape). |
| `board_id`, `board_column_id` | Which issue-tracker board/column the created issue lands in. |
| `department_id`, `responsible_id` | Who the issue is assigned to. |

If a market doesn't use one of the two order types, set that order type's value to `null` in
`markets.json` (not an object) and it will be skipped.

**Only the `DE/AT/FR` market's values are filled in** (carried over from the original
single-market script/`.md` doc) - the other five markets' `board_id`/`board_column_id`/
`department_id`/`responsible_id` are **not guessable** and must be looked up per market.

### Finding the per-market field values

Same method the original project used to confirm its two predefined issues (see "Predefined
issue picker" in [dimensions-precheck.md](../dimensions-precheck.md)):

1. Manually create one test "Dimensions pre-check" / "Dimensions pre-check ATS/DECO" issue in
   that market's board through the normal UI.
2. Find that issue's `obj_id`/`obj` (same way `core.js` does - from the `Add to issuelog`
   button's `data-page-id`/`data-param` on the order detail page), then call
   `GET /api/issueLog/shortList/?obj_id=...&obj=...` to read back its `board_column_id`, etc.
   Or simpler: ask whoever administers that market's board directly.

## Running

```bash
python run.py
```

A browser window opens. Log in to prologistics.info in it (first run only), then answer `y` at
the prompt. The login session is saved under `./browser-profile/` and reused on later runs - your
password is never read or stored by this script.

## Known limitations

Same caveats as the underlying engine (see [dimensions-precheck.md](../dimensions-precheck.md)
"Known limitations") apply per market/order-type here too, plus:

- **Assumes identical page markup across all markets.** `core.js`'s selectors were only verified
  against the DE/AT/FR queue. If another market's queue page renders differently, that
  market/order-type may silently find 0 orders rather than failing loudly - worth a manual
  spot-check the first time you configure a new market.
- **One market/order-type at a time, sequentially.** A run across all 6 markets x 2 types takes
  noticeably longer than a single-market bookmarklet click; there's a `delayBetweenOrdersMs`
  pause (1.5s) between orders inside `core.js` on top of that.
- **`browser-profile/` contains your live session.** Treat it like a password - it's gitignored,
  never commit it or share the folder.

## Security notes

- No credentials are ever read, typed, or stored by this script - authentication is entirely via
  your own interactive login in the Playwright-controlled browser window, persisted as a normal
  browser session in `browser-profile/`.
- `logs/*.json` records order numbers and issue names/links, not credentials - still, treat it as
  internal data (it's gitignored by default).
