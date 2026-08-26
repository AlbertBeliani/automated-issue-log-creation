# Missing-Dimensions Reference Finder

> **This is a living doc.** Update it (and the code) in the same pass whenever something
> here turns out to be wrong or gets newly confirmed, rather than letting it drift out
> of sync.

Finds spare part articles with genuinely no size record at all, resolves the real,
measured main article they belong to (a part can link to several - some of which can
themselves be unmeasured, so this isn't just "take the first link"), estimates the
spare part's own weight/dimensions from it, logs a reference ticket, and (with a
separate explicit confirmation) writes that estimate directly into the article's own
record. **No AI, no API key** - v1/v2 of this tool tried having an LLM guess the
missing dimensions from the reference data, but that needed an Anthropic API key
nobody on this team had admin access to provision. The estimate is now produced by a
small set of stated, deterministic assumptions instead (see `compute_estimate()`) -
not AI, but also not infallible; every assumption it relies on is written down in
code comments and in this doc, specifically so it can be checked and revised, not
trusted blindly.

Chains onto [`../multi-market`](../multi-market)'s existing order-queue scan (see
"Where flagged articles come from" below) rather than being a standalone catalog
crawler - same auth pattern (Playwright + persistent browser profile), same "inject a
`core.js` scan engine, drive it from Python" architecture as
[the main Dimensions Pre-Check tool](../dimensions-precheck.md).

## How "unmeasured" is actually detected (this took 3 rounds to get right)

**First attempt (wrong):** opening a spare part's own edit page and checking whether its
length/width/height/weight showed a specific placeholder value (10cm/10cm/1cm/0.1kg,
seen on 3 sample articles). **The team confirmed this is unreliable - some articles
legitimately have that exact size.**

**Second attempt (confirmed correct, but incomplete):** an article that's genuinely
never had a size entered has **no `upd_dimension_l`/`upd_dimension_w`/
`upd_dimension_h`/`upd_weight_parcel` input in its DOM at all**. Instead of an edit
form for an existing parcel record, it renders an *empty* "add new parcel" form
(unprefixed field names: `dimension_l`, `dimension_w`, `dimension_h`, `weight_parcel`)
inside a `<tr class="highlighted-row">`. Confirmed live against a real team-flagged
example, article
[1868905](https://www.prologistics.info/article.php?original_article_id=1868905)
("single chair MINA taupe") - see `core.js`'s `hasParcelData()`.

**Third addition (2026-08-26):** a saved parcel record with **all four values exactly
0** is a SECOND, separate "unmeasured" signal, missed by `hasParcelData()` alone.
Found live: after 3 wrong estimates got created, the team manually reset those
articles' size fields to `0.00` via the site's own UI to flag them for
re-estimation - the record still exists (so `hasParcelData()` alone said "measured"),
but read as `0/0/0/0`. This does NOT reintroduce the debunked placeholder-value
heuristic above (only an exact all-zero record counts, not any particular non-zero
value like `10x10x1`) - and it's the same rule `../multi-market/core.js` already uses
at the order level (`isDimensionless`), just applied here at the article level too.
`parseArticleDoc()`'s `looksUnmeasured` is now `!hasParcelData(doc) || isAllZero(dims)`.

**Fourth correction:** even these per-article checks aren't how the team actually finds
these parts day to day - see the next section.

## Where flagged articles actually come from

Per the team: articles-with-no-dimensions are found by scanning **order queues**
(`search.php`) for orders whose article line items show `0` for all of
height/width/length/weight - which is exactly what
[`../multi-market/core.js`](../multi-market/core.js)'s `scanOrders()` already does for
the Dimensions Pre-Check tool. It is **not** found by crawling the ~10,000-article
replacement-parts catalog (`articles.php` with "Article types" = "Replacement parts")
directly - that page is reported to lag badly past a few thousand rows.

So this tool **chains onto that same order-scan** (`core.js`'s
`scanQueueForFlaggedArticles()` - a re-implementation of `scanOrders()`, kept as its own
copy rather than shared, same reasoning as the multi-market tool: changes here must
never risk breaking the shipped, in-production Dimensions Pre-Check tool) to get the
flagged article IDs, then runs on each one. **This creates an additional, separate
reference ticket per article** - it does not replace or duplicate `../multi-market`'s
own per-order "dimensions pre-check" issue; run that tool separately if you also want
those.

## How it works

An article page already has the `issueLog` button inline in its own DOM
(`data-param="article"`, `data-page-id=<article id>`, `data-signs-limit="300"`) -
confirmed identical to the original brief's snippet.

1. **Queue scan (in-browser, `core.js`)** - for each `queue_urls` entry in
   `config.json`, scans that order queue the same way `../multi-market` does, and
   collects the de-duplicated list of flagged article IDs.
2. **Per article (in-browser, `core.js`)** - `fetch()`es the article's page in the
   background (no visible navigation - works from any authenticated page, not just
   while sitting on that article), confirms it's genuinely unmeasured
   (`hasParcelData()`), and reads every link in the row right after "Used as
   replacement article in" (that row's own second cell is always empty - the real
   links are in the *next* `<tr>`, nested in a `<table>`; a part can link to several
   main articles).
3. **Main article resolution (in-browser, `core.js`)** - `fetch()`es every linked main
   article and picks `bestMainArticle` = the first one that **does** have real parcel
   data (a linked main article can itself be unmeasured - e.g. a "Hardware set"
   grouping article - so "first link" alone isn't enough).
4. **Dedup check (in-browser, `core.js`)** - calls the same
   `/api/issueLog/shortList/?obj_id=...&obj=article` endpoint the Dimensions Pre-Check
   tool uses, and skips the article if a reference ticket already exists for it.
5. **Build the ticket text (Python, `run.py`'s `build_reference_issue_name`)** - no AI
   call, no article-name prose - just the numbers and a link to check them, e.g.
   `[DIM REF] 1798754: 29.3kg, 194x48x14cm - https://...`. **If the reference article
   is itself a multi-pack** (its name matches `"N pcs"`, e.g. "2 pcs set" - see
   `extract_pack_quantity()` for why that's a text-pattern match rather than a real
   field): weight divides evenly across the pack count (`13kg / 2pcs -> ~6.50kg` -
   safe, additive/linear regardless of packing), and length/width/height use
   `estimate_unit_dims()`'s **stacking assumption** (same footprint, height divided
   by pack count) for a genuine per-unit estimate, e.g. `~6.50kg, 60x60x29cm (est.,
   2pcs stacked)`. **This dimension figure is a stated assumption, not measured fact**
   - see "Multi-pack dimension estimate: the back-and-forth on this" below for the
   full reasoning and why it's built this way on purpose, not by accident.
6. **Preview + confirm (Python, `run.py`)** - prints the exact Issue Log text it would
   post per article and asks `y`/`N` before creating anything. Nothing is written
   without an explicit yes.
7. **Create (Python, `run.py`'s `create_issue_via_ui`)** - navigates to the article's
   page, clicks `button.issueLog`, and drives the real "Add to issuelog" form with
   Playwright clicks: selects the "Ai dimension estimate" predefined issue (kept as-is
   per the team, even though the tool no longer estimates - not worth re-creating it),
   fills Solving user only if it isn't already auto-filled (see `config.json`'s
   `ui_fill.responsible_search_text` for the current target - see "Fixed: wrong
   solving user, then changed again" below for why this changed twice), sets the
   Subject to the reference text, clicks OK. **Board and Column are never touched** -
   per the team, they must stay "---"/empty. **Not a raw POST** - see "How ticket
   creation actually works" below for why.
8. **Verify (Python, `run.py`'s `verify_created_issue`)** - re-queries `shortList`
   right after creating and checks the new ticket's Solving user matches whoever was
   actually selected (read back from the DOM, not assumed), so a silent breakage
   (changed selectors, predefined
   issue edited) is caught instead of assumed away.
9. **Write the estimate into the article itself (Python, `run.py`'s
   `write_estimate_to_article`) - separate confirmation, per the team (2026-08-26)**.
   This is a materially bigger action than creating a ticket: it makes the estimate
   the *live* data the shipping/logistics system treats as fact, with no human
   reviewing it first, so it never rides on the same yes as ticket creation. Navigates
   to the article, clicks `button.show_newparcel_form` (confirmed via its actual bound
   handler that this ONLY toggles visibility, doesn't save anything), fills
   `dimension_l`/`dimension_w`/`dimension_h`/`weight_parcel`, and submits the page's
   real edit form (`#main_form`, POST to `article.php`) via its "Update" button - the
   same form and button a human editing this page by hand uses. Verifies the write
   took by checking the fields switched from the "new parcel" form (unprefixed names)
   to the "existing parcel" form (`upd_`-prefixed names), not just that the button was
   clicked.

**Confirmed working end-to-end for real, including the article write-back**
(2026-08-26): tested live on article 1868905 - filled `dimension_l/w/h`,
`weight_parcel` with `60.00/60.00/29.00/6.50`, submitted, and confirmed
`upd_dimension_l` etc. now exist with those exact values. `run.py`'s
`write_estimate_to_article()` mirrors this exact sequence.

**Regression found and fixed (2026-08-26):** that test was against a genuinely blank
article - the "new parcel" form. Once ANY article has a parcel record at all (either
because this tool already wrote one, or because a team member manually edited the
size via the site's own UI - e.g. resetting values to `0` to flag for
re-estimation), the site switches to a completely different DOM path: `upd_`-prefixed
field names, already present in the DOM but hidden until *that row's own* "Edit"
button (`input.edit_row`, not `button.show_newparcel_form`) is clicked. The original
`write_estimate_to_article()` only handled the blank-article path, so on any article
with an existing record it silently did nothing - real ticket got created with a
correct estimate, but the article's actual size field never changed. Confirmed live
against article 1889020 (one of the reset articles) and fixed: the function now
checks which path applies before deciding what to click and fill. Also strengthened
the post-save check - the old one (`upd_dimension_l` exists) was trivially true on
the existing-parcel path even if the save silently failed, since that field already
existed before submitting; now compares the actually-saved value against what was
sent.

**Confirmed working end-to-end for real (ticket creation)** (2026-08-26): ran `python run.py` against
article 1868905, created ticket
[#531983](https://www.prologistics.info/react/logs/issue_logs/531983), verified
`verified: True` against `config.json`'s expected board/column. Two issues found in
that first real ticket - a misleading multi-pack reference and the wrong solving user -
are described and fixed below.

### Fixed: multi-pack reference was misleading (2026-08-26)

First real ticket showed `ref 45483 "Dining Chair MINA Taupe, 2 pcs set" = 60x60x58cm,
13kg` for article 1868905, "**single** chair MINA taupe" - i.e. it pasted the combined
weight/size of a **2-chair box** into a ticket about **one** chair, unlabeled. A human
reading that could easily take 13kg/60x60x58cm as the single chair's own size, which
is wrong. Fixed per team direction: `build_reference_issue_name()` now detects a
multi-pack reference (regex match on the name, e.g. "2 pcs set" - see
`extract_pack_quantity()`), divides the weight by the pack count for a real per-unit
number, and labels the box dimensions as pack data instead of presenting them
unqualified. Also dropped all article-name prose per the same feedback - the ticket
now shows only the numbers and a link, e.g. `[DIM REF] 1868905: ~6.50kg, 60x60x58cm
(2pcs box) - https://...`.

### Multi-pack dimension estimate: the back-and-forth on this (2026-08-26)

This went through several rounds live - recorded in full because the reasoning on
both sides is worth keeping, not just the end state:

1. **First real ticket** pasted the 2-chair box's raw dimensions/weight into a ticket
   about one chair, completely unlabeled - genuinely misleading, fixed by dividing
   weight by pack count and labeling dimensions as pack-box data (`(Npcs box)`),
   not a per-unit figure.
2. **Team asked for an actual per-unit dimension estimate too**, explicitly accepting
   it would need a packing-arrangement assumption that could be wrong. Built
   `estimate_unit_dims()`: assumes items are **stacked** in the box (same footprint,
   height divided by pack count) - `60x60x58cm / 2pcs -> 60x60x29cm`.
3. **Team flagged that specific result as wrong** - `60x60x29cm` for one dining chair
   didn't hold up. Reasoning through it surfaced a real distinction: weight-halving is
   sound because weight is additive/linear no matter how items are packed; height-
   halving has no equivalent guarantee, since packaging doesn't necessarily scale
   linearly with quantity. Reverted `estimate_unit_dims()` - back to weight-only,
   dimensions shown as unadjusted pack-box data.
4. **Team pushed back on the revert**: the tool's whole purpose is to produce a usable
   estimate, and declining to estimate defeats that - an imperfect, clearly-labeled
   assumption is more useful than punting entirely to a human. **Restored**
   `estimate_unit_dims()` with the same stacking assumption.

**Current state or this document wouldn't be able to keep up**: multi-pack references
get both weight and dimensions estimated per-unit, stacking assumption, always labeled
`(est., Npcs stacked)`. The `60x60x29cm`-for-one-chair number specifically was never
resolved as *right* - just re-accepted as an acceptable imperfect estimate over no
estimate at all. If a specific packing pattern (e.g. this MINA chair specifically,
or a whole product category) turns out to be reliably NOT stacked, that's a real
signal to add a per-category override to `estimate_unit_dims()` rather than relying
on one universal assumption.

### Bigger gap found: most spare parts are DE-ASSEMBLED COMPONENTS, not whole units (2026-08-26)

Ran the tool for the first time against a real order queue (not just the single test
article) - `ATS/Deco Other`, 4 flagged articles. 1868905 (the "single chair" case
above) worked correctly. The other 3 did not:

- `1889020` "Hardware set for MALEVIZI" (screws/fittings) came back sized like the
  entire 4-chair set it referenced.
- `1846920` "legs for dining chair, DILLEY" came back sized like an entire chair.
- `1753360` "Sitting Pillow for VITTORIA..." came back sized like the entire garden
  set (sofa + chairs) it referenced.

**Root cause**: the estimator only ever handled two cases - "reference is a multi-pack
of the same product" (divide by pack count) or fell through to "copy the reference's
full size unchanged." That fallback silently assumed every non-multi-pack reference
meant "the spare part is one whole unit of the product" - true for 1868905, false for
almost everything else. Most spare parts in this catalog are **components de-assembled
from a larger product** (legs, hardware, cushions, panels...), and there's no way to
derive "how big is just the legs" from "how big is the whole chair" mathematically -
unlike the pack-count math, this genuinely needed domain knowledge nobody but the team
has.

**Solution, per explicit team direction with real percentage ranges provided by
them**: `classify_component_reduction()` matches the spare part's OWN name (not the
main article's) against four categories, each with a team-specified dimension% /
weight% range:

| Category | Keywords | Dims % | Weight % |
|---|---|---|---|
| `hardware` | hardware, screw(s), bolt(s), nut(s), washer(s), fitting(s), fastener(s) | 5-10% | 5-15% |
| `structural` | leg(s), frame(s), support(s), bracket(s) | 20-40% | 20-35% |
| `soft` | cushion(s), pad(s), upholstery, pillow(s) | 25-50% | 30-50% |
| `panel` | panel(s), board(s), cover(s), door(s), shelv(es/ing) | 30-60% | 30-60% |
| *(unclassified)* | anything else, single-product reference | 30% flat | 30% flat |

Within a matched category, the **low end** of the range is used if the part's name
also signals "multiple small items" (`set`/`kit` - e.g. "hardware **set**"), otherwise
the **midpoint**. This check runs independently of pack-quantity detection and applies
ON TOP of it - confirmed necessary live: `1846920`'s main article is itself a "2 pcs
set", so pack-quantity alone would have (wrongly) treated "legs" as "one whole chair
from that pack" instead of applying the structural reduction. See `compute_estimate()`
for the exact composition order.

**These percentages are the team's own domain-knowledge estimates, not measured
ratios** - there is no per-part weight/size data anywhere in this system to derive
real ones from. Confirmed re-run against real data for all 3 failing articles:

```
1889020 "Hardware set for MALEVIZI" (ref: Set of 4 chairs, MALEVIZI, 89x63x51cm, 21.1kg):
  -> ~1.06kg, 4.5x3.2x2.6cm (est., hardware ~5%)

1846920 "legs for dining chair, DILLEY" (ref: dining chair, DILLEY, 2pcs set, 69x67x53cm, 13.3kg):
  -> ~1.83kg, 20.7x20.1x8.0cm (est., structural ~30% of 2pcs stacked)

1753360 "Sitting Pillow for VITTORIA..." (ref: Garden set, VITTORIA, 140x74x52cm, 36.1kg):
  -> ~14.44kg, 52.5x27.8x19.5cm (est., soft ~38%)
```

Not yet re-confirmed whether the team considers THESE specific numbers correct (only
that the mechanism runs and produces something in the right ballpark, unlike before) -
worth checking after the next real run, same as every estimate in this tool.

### Fixed: wrong solving user, then changed again (2026-08-26)

The first real ticket used "Albert Linnert" (whoever ran the script) as the Solving
user, which pulled in *his* department (`"Customer Service - Customer Operations TL
AI"` that run - it had shown `"Logistics - Shipping"` on an earlier manually-created
test ticket, suggesting the derived department isn't even stable per-user, possibly
context-dependent). Team's first direction: always use **"Adam AI"** (a service-account
persona) instead of whoever runs the script. **Later changed back to "Albert
Linnert"** as the target, this time explicitly as an auto-detect-first-else-select
target rather than something to always click - see `config.json`'s
`ui_fill.responsible_search_text` for whichever is current. Also: **Board and Column
are no longer selected at all** (must stay "---"/empty per the team) - an earlier
version of this section is why `create_issue_via_ui()` used to pick "RK Warehouse" and
had `issue_log_fields.board_id` in config; both are gone now.

### Fixed (took 3 attempts): the "Solving user" field loads slower than the rest (2026-08-26)

Changing just the Solving-user search text to "Adam AI" caused a NEW failure: the
click on the Solving-user select2 container hung for the full 30s timeout, right after
Board had just been successfully picked (which triggers a background request that
populates Column). **First (wrong) theory**: reasoned from a screenshot of the
*correct end state* (Board/Column shown disabled once Adam AI is set) that picking
Solving user had to come *before* Board - reordered the fields accordingly. That did
NOT fix it - the exact same hang recurred, just relocated to right after the
predefined-issue selection instead. That ruled out ordering as the cause.

**Second (also incomplete) theory**: reverted to board-first order and replaced the
fixed `wait_for_timeout(300)` calls with `page.wait_for_load_state("networkidle")`,
reasoning it was a generic page-wide timing race. Still failed identically -
`waiting for locator("span#select2-responsible_persons0-container")`, with NO
"element is not visible" detail this time (a plain "waiting for locator" with no
actionability sub-lines means the locator matched ZERO elements - fundamentally
different from the earlier modal-open failure, where the element existed but wasn't
visible yet).

**Root cause, confirmed by directly inspecting the live DOM at the failure point**:
the "Solving user" field isn't like the other select2 widgets on this form. Network
log showed React UI component files loading (`TextField.js`, `select/style.css`,
`EmployeeTooltip.js`) plus **three sequential** `POST /api/filtersOptions/?
type[]=employees` calls - this field loads and renders ~1278 employees via multiple
batched requests, and takes noticeably longer to finish than Board or the predefined
issue. Checked immediately after Board selection: the underlying `<select>` existed
but had only 1 option (the "---" placeholder) and no select2 container in the DOM at
all yet. Waited a few seconds and it eventually appeared fully populated
(`select2-hidden-accessible` class, 1278 options). A page-wide `networkidle` wait
isn't reliable for this specifically, since Playwright's "idle" definition can
trigger in a gap *between* the sequential employee-data requests, before all of them
are actually done.

**Attempted fix**: `select2_pick()` explicitly waits for *that specific field's*
select2 container to exist (`state="attached"`, up to 25s) before clicking it, instead
of assuming an earlier page-wide wait was enough. **Still failed live even with a 15s
budget for this field specifically** - confirmed the widget can genuinely take longer
than that to initialize, not just longer than a naive fixed delay.

**Final resolution: human-in-the-loop fallback, per the team.** Rather than keep
chasing a longer timeout for an inherently slow, ~1278-employee widget,
`create_issue_via_ui()` now tries the automatic pick once (15s budget) and, if that
fails, prints instructions and pauses on `input()` for you to pick "Solving user"
yourself in the already-visible (non-headless) browser window - then continues
automatically for Subject + OK once you confirm it's set (re-checks the field's
actual value in a loop, so pressing Enter too early just prompts again). Not yet
re-verified live.

### Fixed: modal-open race condition (2026-08-26)

First live attempt at step 7 failed: `page.click()` on the select2 container hung for
the full 30s timeout with "element is not visible", even though the underlying
`<select id="predefinedFilterId0">` was already "attached" to the DOM (that's what
`create_issue_via_ui` waited for before this fix). Root cause: `page.goto()` only
waits for the "load" event, not for this page's jQuery/select2 setup to finish
running, so the click on `button.issueLog` could fire before its handler was bound -
and the raw `<select>` select2 hides is apparently already present in the DOM as a
template even before the modal opens, so waiting on it gave no real signal that
anything had actually happened. Fixed in `open_issue_form()`: wait for
`page.wait_for_load_state("networkidle")` after navigating, wait for the modal's own
title text ("Create new issue") to become **visible** (not just attached) after
clicking the button, and retry the button click once if the modal doesn't show up
within 5s. Not yet re-verified live.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

That's it - no `.env`, no API key. **Run from a real terminal, not by double-clicking
`run.py`** - the script needs to ask `[y/N]` questions (to confirm login, confirm
before creating each ticket) and a double-clicked window gives you no way to answer
them; it just flashes and closes. In VS Code: **Terminal -> New Terminal**, then:

```bash
cd ai-dimension-estimator
python run.py
```

(Not just `run.py` - on some Windows setups, `.py` files are associated with
`pythonw.exe`, which has no console attached, so nothing can print and it fails
silently. `python run.py` always uses the console-attached interpreter.)

## Configuring `config.json`

| Field | What it is |
|---|---|
| `queue_urls` | Order queue URLs to scan for flagged articles (same queue URLs used with `../multi-market`, e.g. the "Pending CH/PL/..." links). Empty by default - fill in once you're ready to chain onto real queues instead of just testing specific articles. |
| `test_article_ids` | Specific article IDs to always process, regardless of `queue_urls` (currently `["1868905"]`, the confirmed real test case). Useful for repeatable testing. |
| `ui_fill.predefined_issue_search_text` | Text typed into the "Predefined issue" select2 search box (`"AI Dimension estimate"` - the predefined issue the team created 2026-08-25; kept as-is). |
| `ui_fill.responsible_search_text` | Text typed into the "Solving user" select2 search box if it isn't already auto-filled (currently `"Albert Linnert"` - **changed twice live** as the team iterated, first "Adam AI" then this; check this value itself for the current target, not any prose elsewhere in this doc). |

**Board and Column are deliberately never touched** - per the team (2026-08-26), they
must stay at "---"/empty, so there's no `board_search_text` or expected board/column
id in config at all anymore (an earlier version of this tool did select them - see
"Fixed" sections below for why that was removed). `verify_created_issue()` checks the
created ticket's Solving user against whatever `create_issue_via_ui()` actually
selected (read back from the DOM, not assumed from config) - not board/column, and not
a hardcoded username, since which real person to target has already changed twice.

### How ticket creation actually works (and why it's UI-driven, not a raw POST)

A real test ticket was created by hand through the UI first (issue #531511, on article
1868905) to get ground truth, since guessing field names risked creating malformed
tickets. Key findings from that:

- **The article page's "Add to issuelog" form has no separate Department field** -
  just Type, Predefined issue, Issue tag, Solving user, Board, Column, Subject.
  Confirmed via the created ticket's `shortList` response: it has `department_name:
  "Logistics - Shipping"`, which is Albert Linnert's (the assigned "Solving user")
  *own* department - department is **derived from the responsible person**, not a
  field you set directly. There's no `department_id` in `config.json` because of this.
- **Selecting the "AI Dimension estimate" predefined issue only auto-fills Issue tag
  and Subject** - Board, Column, and Solving user still need to be picked manually if
  empty (Board: search "RK Warehouse" -> auto-fills Column to "To do"; Solving user:
  search the person's name). Different from the order-tool's predefined issues (those
  fix department/tag/board/responsible all at once) - this one only fixes the tag.
- **The raw POST body for this form could not be captured**, even after trying to
  intercept and block the real `XMLHttpRequest` (to read it without creating a
  duplicate ticket) - it came back empty, meaning the request likely isn't a plain
  `XMLHttpRequest.send()` with a `fn=addIssueLog`-style body. **No duplicate ticket was
  created** by that attempt, so nothing broke, but it meant the request shape genuinely
  couldn't be reverse-engineered safely.
- **So `run.py`'s `create_issue_via_ui()` drives the real form instead**: clicks
  `button.issueLog`, clicks through each select2 widget's search-then-click-the-option
  sequence (confirmed live: select2 here only responds to real clicks - scripting
  `.value` + a `change` event does nothing, and pressing Enter after typing a filter
  also does nothing, so each pick is a genuine click on the matching
  `.select2-results__option`), fills the Subject textarea, and clicks OK. This
  guarantees correctness by reusing the site's own JS instead of guessing its request
  format. `verify_created_issue()` then re-checks the created ticket's actual
  board/column/responsible against `config.json`'s expected values as a safety net.

## Running

```bash
python run.py
```

A browser window opens. Log in to prologistics.info in it (first run only - saved
under `./browser-profile/` and reused after, your password is never read or stored by
this script), then answer `y` **in the terminal, not the browser** at the prompt. It'll
scan any configured `queue_urls`, then process `test_article_ids`, printing each
reference ticket's text for review, with **two separate confirmations** per article:
one before creating the Issue Log ticket, and a second, independent one before writing
the estimate into the article's own record - saying yes to the first does not imply
yes to the second.

## Known limitations / not yet validated

- **The multi-pack detection (`extract_pack_quantity()`) is a text-pattern match on
  the article name, not a real field** - confirmed live there's no structured "pieces
  in this box" field (see "Fixed: multi-pack reference was misleading" above). Only
  matches `"N pcs"` / `"N pc"` - **confirmed live 2026-08-26**: article 1889020's main
  article is named "**Set of 4** chairs, MALEVIZI" - a real, differently-worded pack
  that this regex does NOT catch, falling through to qty=1. Didn't matter for that
  specific case (the spare part matched the `hardware` component category, which
  dominates regardless of pack quantity), but WOULD matter for a hypothetical
  whole-unit spare part referencing a "Set of N" main article - worth broadening the
  regex to also match `"Set of N"` if that phrasing turns out to be common for
  whole-unit references specifically.
- **`classify_component_reduction()` scales length/width/height by the SAME
  percentage**, which can produce an oddly-shaped result when the main article
  itself is very elongated - e.g. `1889020`'s hardware set came out `4.5x3.2x2.6cm`
  from a 207x26x21cm bed reference (real example seen earlier in testing, different
  article than the confirmed 1889020 numbers above) - correctly small overall, but a
  sliver-like shape a bag of screws wouldn't actually have. Fine for the estimate's
  actual purpose (rough size/weight for shipping calculations, not exact geometry),
  but worth knowing if someone expects the shape itself to be meaningful.
- **The full creation click-through was verified against exactly one real article**
  (1868905, ticket #531983) after the multi-pack/solving-user fixes above were written
  - those specific fixes haven't been re-run live yet, only the underlying click
  mechanism itself. Worth re-confirming on the next real run that the ticket text and
  solving user come out as expected now.
- **`select2_pick()`'s option match uses `has_text` (substring), not exact match** -
  fine while `ui_fill`'s search text is specific enough to only match one option (as
  it is for all three fields currently), but would need tightening if a broader search
  term ever matched more than one result.
- **`bestMainArticle` only takes the first qualifying link**, not the closest match by
  name/size. Fine for the confirmed test case (only 1 candidate link); revisit if a
  part ever links to several genuinely different-sized main articles.
- **The green/active vs red/EOL link color is confirmed but currently unused** for
  anything beyond informational purposes - an EOL product's physical size doesn't
  change, so it's still valid reference data.
- **`scanQueueForFlaggedArticles()` reuses `../multi-market/core.js`'s selectors and
  detection rule (`#egusli` grid, all-4-dims-render-as-0) as a separate copy** - if that
  tool's selectors are ever updated after a site change, this file needs the same
  update manually; there's no shared source of truth between them by design (see "Where
  flagged articles actually come from").
- **300-character issue text limit**, read dynamically from `data-signs-limit` rather
  than hardcoded - `build_reference_issue_name()` trims the article names (least
  load-bearing text) first if it's ever exceeded, never the link or numbers.
- **`issue_type_id` may not exist as its own filterable field for dedup purposes.**
  Dedup (`hasExistingReferenceTicket()` in `core.js`) relies on matching the substring
  `"ai dimension estimate"` against the ticket's tag/subject text via `shortList`, not
  a dedicated flag.
- **Session-bound to one browser profile.** Same as `../multi-market` -
  `browser-profile/` holds your live session and must never be committed or shared.
- **Not validated for scale.** Even chained onto the queue scan, a full run across all
  markets would need rate limiting and almost certainly a human-review step before any
  bulk creation, not an unattended loop.
- **`write_estimate_to_article()` submits the article's ENTIRE edit form**, not just
  the 4 dimension fields - confirmed this is how the site itself works (there's no
  smaller AJAX save for just this section), and confirmed the write itself works
  correctly, but this was only tested on one article in a clean state. If a future run
  ever navigates to an article while some OTHER field on the page is mid-edit or in an
  unexpected state, submitting the form would resubmit that too - worth a spot-check
  after any bulk use, not just trusting the happy path forever.
- **The dimension estimate is a genuinely uncertain assumption being written as fact.**
  See "Multi-pack dimension estimate: the back-and-forth on this" above - the stacking
  assumption behind multi-pack estimates was explicitly flagged as possibly wrong, and
  now gets written directly into live shipping data (behind its own confirmation, but
  still). Worth periodically spot-checking written articles against reality, not just
  trusting the confirmation gate as a permanent substitute for verification.

## Security notes

- **No API keys or secrets of any kind** - this tool needs nothing beyond your own
  interactive login.
- No prologistics.info cookies/session tokens are ever written into this script -
  identical to `../multi-market`, auth is entirely via your own interactive login,
  persisted in `browser-profile/`.
- `logs/*.json` records article ids and scraped dimensions - not credentials - but
  still gitignored as internal data.
