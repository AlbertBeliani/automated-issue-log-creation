#!/usr/bin/env python3
"""
Missing-Dimensions Reference Finder - runner.

v3 (2026-08-25): dropped the AI-estimate step entirely - no Anthropic API key
needed anymore. Same philosophy as ../dimensions-precheck.js: DON'T guess a
number, surface the facts and let a human decide. The tool's real value was
never "guessing a size" - it was doing the tedious, error-prone part: finding
which of a part's several "used as replacement article in" links actually has
real, measured dimensions (some, like a "Hardware set" grouping article, are
themselves unmeasured) and handing that resolved reference to a human on a
plate.

For each flagged article (see config.json), this:
  1. Fetches the article's page and confirms it has no size record at all
     (see core.js's hasParcelData - CONFIRMED against a real example,
     article 1868905, "single chair MINA taupe": an unmeasured article has
     no upd_dimension_l/w/h or upd_weight_parcel input in its DOM at all,
     rather than showing any particular value).
  2. Reads the "Used as replacement article in" row to find its main
     article(s), and picks the first one that DOES have real size data
     (a linked main article can itself be unmeasured - so this isn't just
     "the first link").
  3. Builds the exact Issue Log text - part name + the resolved reference
     article's name/dimensions/link - and shows it to you.
  4. Asks for confirmation, then creates the ticket by driving the real
     "Add to issuelog" UI form with Playwright clicks (see
     create_issue_via_ui) - NOT a raw POST. A raw POST modeled on the
     order-level flow's captured request was tried first and confirmed wrong
     for this object type (see core.js's note and README), so this drives
     the actual form instead of guessing its request shape.

Where the flagged articles come from (config.json's queue_urls):
  Per the team, articles-with-no-dimensions are actually found by scanning
  ORDER queues (search.php) for orders with dimensionless article line
  items - exactly what ../multi-market's Dimensions Pre-Check tool already
  does. So this tool CHAINS onto that same detection: for each queue_url,
  it runs the same order-scan and pulls out the flagged article IDs, then
  runs this tool on each one. This is ADDITIONAL to (not a replacement for)
  ../multi-market's own per-order "dimensions pre-check" issue - run that
  tool separately if you also want those.
  If queue_urls is empty, only config.json's test_article_ids are used -
  useful for testing against specific known articles without scanning a
  live queue each time.

Same auth pattern as ../multi-market/run.py: a real Chromium window opens,
you log in once, and the session is reused via ./browser-profile/ on later
runs. No password is ever read or stored by this script.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python run.py
"""
import json
import datetime
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
CORE_JS = (HERE / "core.js").read_text(encoding="utf-8")
PROFILE_DIR = HERE / "browser-profile"
LOG_DIR = HERE / "logs"
ARTICLE_URL_TEMPLATE = "https://www.prologistics.info/article.php?original_article_id={}"


def confirm(prompt):
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def load_config():
    with open(HERE / "config.json", encoding="utf-8") as f:
        return json.load(f)


def check_issue_fields_configured(fields):
    return [k for k, v in fields.items() if str(v).startswith("REPLACE_ME")]


PACK_QTY_RE = re.compile(r"(\d+)\s*pcs?\b", re.IGNORECASE)


def extract_pack_quantity(name):
    """
    There's no reliable structured field for "how many physical pieces are
    bundled in this box" - confirmed live (2026-08-26) against article 45483
    ("Dining Chair MINA Taupe, 2 pcs set"): its own items_per_shipping_unit
    field reads 1.00, because that field means "how many shipping units does
    one ORDER of this SKU take" (1 box = 1 order), not "how many chairs are
    physically in the box". The only signal is the free-text name itself
    ("2 pcs set", "2 pcs", etc.) - a narrow, deliberately conservative regex
    match (not open-ended guessing), defaulting to 1 (no adjustment) if no
    such pattern is found. Deliberately does NOT match patterns like
    "Carton 2/3" (carton X-of-Y numbering, unrelated to piece count).
    """
    if not name:
        return 1
    m = PACK_QTY_RE.search(name)
    return int(m.group(1)) if m else 1


def fmt_num(x):
    """Whole numbers print clean (29 not 29.0), fractional ones keep 1 decimal."""
    return f"{x:.0f}" if float(x).is_integer() else f"{x:.1f}"


def estimate_unit_dims(dims, qty):
    """
    Turn a multi-pack box's dimensions into a per-unit estimate. This is a
    stated assumption, not measured fact - restored 2026-08-26 after being
    briefly reverted, per explicit team direction: the tool's purpose is to
    produce a usable estimate, and an imperfect labeled assumption serves
    that better than declining to estimate at all.

    ASSUMPTION: items in a multi-pack are stacked on top of each other
    inside one box, sharing the same length x width footprint, with height
    scaling with quantity - length/width unchanged, height divided by pack
    quantity. This will be wrong for anything packed a different way
    (side-by-side, nested, disassembled flatter than either dimension
    alone suggests) - it is a real assumption, always labeled "(est.)" with
    the assumption named, never presented as measured.
    """
    return {
        "length": dims["length"],
        "width": dims["width"],
        "height": dims["height"] / qty,
    }


SET_OR_KIT_RE = re.compile(r"\b(set|kit)\b", re.IGNORECASE)

# (category, name-keyword pattern, (dims_pct_low, dims_pct_high), (weight_pct_low, weight_pct_high))
# Ranges are the team's own domain-knowledge estimates (2026-08-26, after 3
# real cases - hardware, legs, a cushion - showed copying the main article's
# full size was way too big for a de-assembled sub-component) - NOT derived
# from any real per-part data, because no such data exists in this system.
COMPONENT_CATEGORIES = [
    ("hardware", re.compile(r"\b(hardware|screws?|bolts?|nuts?|washers?|fittings?|fasteners?)\b", re.IGNORECASE),
     (0.05, 0.10), (0.05, 0.15)),
    ("structural", re.compile(r"\b(legs?|frames?|supports?|brackets?)\b", re.IGNORECASE),
     (0.20, 0.40), (0.20, 0.35)),
    ("soft", re.compile(r"\b(cushions?|pads?|upholstery|pillows?)\b", re.IGNORECASE),
     (0.25, 0.50), (0.30, 0.50)),
    ("panel", re.compile(r"\b(panels?|boards?|covers?|doors?|shelv(?:es|ing))\b", re.IGNORECASE),
     (0.30, 0.60), (0.30, 0.60)),
]
DEFAULT_DIMS_PCT_RANGE = (0.30, 0.30)
DEFAULT_WEIGHT_PCT_RANGE = (0.30, 0.30)


def classify_component_reduction(part_name):
    """
    Picks a size/weight reduction fraction if the spare part's OWN name
    (not the main article's) matches a known de-assembled-component
    category (hardware/structural/soft/panel), else returns None - the
    caller (compute_estimate) decides what "no match" means, since that
    depends on whether the main article is itself a multi-pack.

    Uses the low end of a matched category's range when the name also
    signals "multiple small items" (set/kit - e.g. "hardware set"), else
    the range midpoint.

    These are rough, named assumptions from the team's own domain
    knowledge (2026-08-26), not measured ratios - there is no per-part
    data in this system to derive real ones from.
    """
    name = part_name or ""
    use_low = bool(SET_OR_KIT_RE.search(name))

    for category, pattern, dims_range, weight_range in COMPONENT_CATEGORIES:
        if pattern.search(name):
            dims_pct = dims_range[0] if use_low else sum(dims_range) / 2
            weight_pct = weight_range[0] if use_low else sum(weight_range) / 2
            return {"category": category, "dims_pct": dims_pct, "weight_pct": weight_pct}

    return None


def compute_estimate(part_name, main_article):
    """
    The single source of truth for "what do we think this spare part's
    weight/dimensions are", shared by the ticket text (build_reference_
    issue_name) and the article write-back (write_estimate_to_article) so
    the two can never silently disagree.

    Step 1: resolve the main article to "one unit" of whatever it's sold
    as - if it's a multi-pack (qty > 1, e.g. "2 pcs set"), divide down
    using the already-validated pack logic (weight linear, L/W/H via
    estimate_unit_dims()'s stacking assumption). This is the base
    reference point regardless of what the spare part turns out to be.

    Step 2: check whether the spare part is itself a DE-ASSEMBLED
    COMPONENT of one unit (legs, hardware, a cushion, a panel...), via
    classify_component_reduction() on the spare part's OWN name -
    independent of whether the main article happens to be a multi-pack.
    CONFIRMED live 2026-08-26 this independence is necessary: article
    1846920 ("legs for dining chair, DILLEY") references a main article
    that's itself a "2 pcs set" - checking pack-quantity alone would wrongly
    treat "legs" as "one whole chair" from that pack. If matched, the
    component fraction applies ON TOP of the per-unit base from step 1.

    If nothing matches: a multi-pack reference is assumed to mean the spare
    part is one whole unit of it (e.g. "single chair" vs a "2 pcs set" of
    that chair - already confirmed correct); a single-product reference
    with no component keyword falls back to a flat 30%/30% reduction per
    explicit team instruction, rather than being left at full main-article
    size.
    """
    d = main_article["dims"]
    qty = extract_pack_quantity(main_article["name"])

    if qty > 1:
        unit_weight = d["weight"] / qty
        unit_dims = estimate_unit_dims(d, qty)
        pack_note = f"{qty}pcs stacked"
    else:
        unit_weight = d["weight"]
        unit_dims = {"length": d["length"], "width": d["width"], "height": d["height"]}
        pack_note = None

    component = classify_component_reduction(part_name)
    if component is not None:
        note = f"{component['category']} ~{round(component['dims_pct'] * 100)}%"
        if pack_note:
            note += f" of {pack_note}"
        return {
            "weight": unit_weight * component["weight_pct"],
            "dims": {
                "length": unit_dims["length"] * component["dims_pct"],
                "width": unit_dims["width"] * component["dims_pct"],
                "height": unit_dims["height"] * component["dims_pct"],
            },
            "note": note,
        }

    if pack_note:
        return {"weight": unit_weight, "dims": unit_dims, "note": pack_note}

    default_dims_pct = sum(DEFAULT_DIMS_PCT_RANGE) / 2
    default_weight_pct = sum(DEFAULT_WEIGHT_PCT_RANGE) / 2
    return {
        "weight": unit_weight * default_weight_pct,
        "dims": {k: v * default_dims_pct for k, v in unit_dims.items()},
        "note": f"default ~{round(default_dims_pct * 100)}%",
    }


def build_reference_issue_name(prefix, article_id, estimate, main_article_href, signs_limit):
    """
    No AI guess, but a real estimate (see compute_estimate): the resolved
    reference article's dimensions/weight, adjusted per-unit or per-
    component as appropriate, plus a link to check the source. No part/
    article-name prose - per team feedback 2026-08-26, keep it to just the
    numbers. Always labeled "(est., ...)" with the reasoning named (pack
    count or component category) - never presented as measured.
    """
    dims_str = f"{fmt_num(estimate['dims']['length'])}x{fmt_num(estimate['dims']['width'])}x{fmt_num(estimate['dims']['height'])}cm"
    weight_str = f"~{estimate['weight']:.2f}kg"
    note = f" (est., {estimate['note']})"

    base = f"{prefix} {article_id}: {weight_str}, {dims_str}{note} - {main_article_href}"
    limit = signs_limit or 300
    return base[:limit]


def write_estimate_to_article(page, article_id, estimate):
    """
    Writes the estimate directly into the article's own "Size and weight of
    shipping unit" record. These fields are part of the page's full edit
    form (#main_form, POST to article.php), submitted via the same "Update"
    button a human editing this page by hand would use - NOT a separate
    small AJAX save. Only the 4 dimension fields are ever touched;
    submitting re-sends whatever's already in the DOM for every other
    field, same as a human who only edited this one section and clicked
    Update.

    TWO DIFFERENT DOM PATHS depending on whether a parcel record already
    exists - confirmed live 2026-08-26 after a real regression: a first
    write (e.g. article 1868905's very first save) goes through the "new
    parcel" form (unprefixed field names: dimension_l/w/h, weight_parcel,
    hidden behind button.show_newparcel_form - which ONLY toggles
    visibility, confirmed by reading its actual bound handler, no saving
    logic in it at all). But once a parcel record exists - after that first
    write, OR because a team member manually edited the article's size via
    the site's own UI (e.g. resetting it to 0 to flag for re-estimation) -
    the SAME article now uses the "existing parcel" form instead:
    upd_-prefixed field names (upd_dimension_l etc.), already present in
    the DOM but hidden (display:none) until that row's OWN "Edit" button
    (input.edit_row) is clicked, which just adds an `editable_row` CSS
    class to reveal them - a completely different toggle than
    show_newparcel_form. The original version of this function only
    handled the first path, so it silently did nothing on any article that
    already had a parcel record - confirmed live and fixed.

    After a successful save, this checks the actual saved value in the DOM
    matches what was sent, not just that a button was clicked.
    """
    page.goto(ARTICLE_URL_TEMPLATE.format(article_id))
    page.wait_for_load_state("networkidle")

    has_existing_parcel = page.locator('input[name="upd_dimension_l"]').count() > 0
    if has_existing_parcel:
        page.click(".parcel_item input.edit_row")
        l_field, w_field, h_field, weight_field = "upd_dimension_l", "upd_dimension_w", "upd_dimension_h", "upd_weight_parcel"
    else:
        page.click("button.show_newparcel_form")
        l_field, w_field, h_field, weight_field = "dimension_l", "dimension_w", "dimension_h", "weight_parcel"

    page.fill(f'input[name="{l_field}"]', f"{estimate['dims']['length']:.2f}")
    page.fill(f'input[name="{w_field}"]', f"{estimate['dims']['width']:.2f}")
    page.fill(f'input[name="{h_field}"]', f"{estimate['dims']['height']:.2f}")
    page.fill(f'input[name="{weight_field}"]', f"{estimate['weight']:.2f}")

    page.click('#main_form input[type="submit"][value="Update"]')
    page.wait_for_load_state("networkidle")

    if page.locator('input[name="upd_dimension_l"]').count() == 0:
        return {"success": False, "reason": "upd_dimension_l not found after submit - save may not have taken"}
    saved = {
        "length": page.eval_on_selector('input[name="upd_dimension_l"]', "el => el.value"),
        "width": page.eval_on_selector('input[name="upd_dimension_w"]', "el => el.value"),
        "height": page.eval_on_selector('input[name="upd_dimension_h"]', "el => el.value"),
        "weight": page.eval_on_selector('input[name="upd_weight_parcel"]', "el => el.value"),
    }
    matches_sent = abs(float(saved["length"]) - estimate["dims"]["length"]) < 0.01
    return {"success": matches_sent, "saved": saved}


def select2_pick(page, select_id, search_text, option_text=None, ready_timeout=25000):
    """
    Pick an option in a select2 widget. CONFIRMED live (2026-08-25): this
    site's select2 instances only respond to real clicks - setting the
    underlying <select>'s .value and firing a 'change' event does nothing,
    and pressing Enter after typing a filter also does nothing. Has to
    physically click the container to open it, then click the matching
    option by its visible text.

    CONFIRMED live (2026-08-26): the "Solving user" field's select2 widget
    (~1278 employees, loaded via several sequential /api/filtersOptions
    requests, confirmed via the network log) can take much longer to
    initialize than the other fields on this form - its underlying <select>
    had only 1 option (the "---" placeholder) and no select2 container in
    the DOM at all for several seconds, then eventually had 1278 options and
    the normal select2-hidden-accessible/container setup. A page-wide
    wait_for_load_state("networkidle") isn't reliable for this specifically,
    since Playwright's "idle" definition can trigger in gaps between the
    sequential requests before all of them finish. So: explicitly wait for
    THIS field's own select2 container to exist before clicking it, with a
    generous timeout, rather than trusting an earlier page-wide wait.
    """
    page.wait_for_selector(f"span#select2-{select_id}-container", state="attached", timeout=ready_timeout)
    page.click(f"span#select2-{select_id}-container")
    # Scoped to the currently-open widget only - this form has 5 select2
    # instances, and an unscoped input.select2-search__field selector can
    # match multiple (hidden, previously-opened) ones and hit Playwright's
    # strict-mode ambiguity error.
    search = page.locator(".select2-container--open input.select2-search__field")
    search.fill(search_text)
    page.wait_for_timeout(300)  # let select2's client-side filter re-render
    page.locator(".select2-container--open .select2-results__option", has_text=option_text or search_text).first.click()


def open_issue_form(page):
    """
    Clicks button.issueLog and waits for the modal to actually be VISIBLE,
    not just present in the DOM.

    Live failure seen (2026-08-26): waiting for "#predefinedFilterId0" with
    state="attached" passed instantly - the raw <select> select2 hides is
    apparently already in the DOM before the modal opens (likely a template
    that's always present) - so that wait gave no real signal the modal had
    opened. The actual click on the select2 container then hung for the full
    30s timeout because the modal never became visible: page.goto() only
    waits for the "load" event, not for this page's jQuery/select2 setup to
    finish, so the click on the button may have fired before its handler was
    bound. Fixed by waiting for something only true once the modal is open
    (its title text) and retrying the button click once if it doesn't show
    up quickly - cheap insurance against this exact race.
    """
    page.click("button.issueLog")
    try:
        page.wait_for_selector("text=Create new issue", state="visible", timeout=5000)
    except Exception:
        page.click("button.issueLog")
        page.wait_for_selector("text=Create new issue", state="visible", timeout=15000)


def create_issue_via_ui(page, article_id, issue_text, ui_fill):
    """
    Creates the Issue Log entry by driving the real "Add to issuelog" form
    with clicks, rather than a hand-rolled POST - see core.js's note on why
    (the raw request this form sends couldn't be safely captured, and this
    object type's form doesn't map cleanly onto the order-level flow's
    request shape). Requires navigating to the article's own page, since the
    button only exists in that page's DOM (unlike the read side, which works
    via background fetch from anywhere).

    Only Predefined issue, Solving user, and Subject are ever touched - per
    the team (2026-08-26), Board and Column must be left as-is ("---"/empty).

    Returns the actual username selected as Solving user, read back from the
    DOM after selection, for the caller to verify against.
    """
    page.goto(ARTICLE_URL_TEMPLATE.format(article_id))
    page.wait_for_load_state("networkidle")
    open_issue_form(page)

    select2_pick(page, "predefinedFilterId0", ui_fill["predefined_issue_search_text"])
    page.wait_for_load_state("networkidle")

    # Per the team (2026-08-26): Board and Column must be left untouched
    # ("---"/empty) - do NOT select them. Only Solving user gets picked - if
    # it's already auto-filled, leave it; otherwise try picking it
    # automatically. (Whose username this targets has changed a few times
    # live as the team iterated - see ui_fill.responsible_search_text in
    # config.json for the current target, not this comment.)
    #
    # This field's select2 widget (~1278 employees) has proven unreliable to
    # automate even with a generous wait (see README's "Fixed (took 3
    # attempts)..." section) - confirmed live it can simply take longer than
    # any wait we tried. Per the team: rather than keep chasing a longer
    # timeout, fall back to a human-in-the-loop pause - the browser window is
    # already visible (non-headless), so pick it by hand there, then let the
    # script continue automatically for everything else.
    if page.eval_on_selector("#responsible_persons0", "el => el.value") in (None, "", "---"):
        try:
            select2_pick(page, "responsible_persons0", ui_fill["responsible_search_text"], ready_timeout=15000)
            page.wait_for_load_state("networkidle")
        except Exception:
            print(f"  'Solving user' didn't load in time - please select "
                  f"{ui_fill['responsible_search_text']!r} yourself in the browser window.")
            while page.eval_on_selector("#responsible_persons0", "el => el.value") in (None, "", "---"):
                input("  Press Enter here once Solving user is set... ")
    # Read back the actual selected username (not just the search label used
    # to find it) so the caller can verify against ground truth afterward.
    picked_username = page.eval_on_selector("#responsible_persons0", "el => el.value")

    page.fill("#issueField0", issue_text)
    page.get_by_role("button", name="OK", exact=True).click()
    page.wait_for_timeout(500)
    return picked_username


def verify_created_issue(page, article_id, obj, expected_responsible_username):
    """
    Sanity-check the just-created ticket's Solving user against who was
    actually picked (see create_issue_via_ui), via the same shortList
    endpoint used for dedup - catches silent breakage
    (e.g. a selector change) instead of assuming the UI click worked.

    Board/Column are NOT checked here - per the team (2026-08-26), those are
    deliberately left untouched ("---"/empty), not filled by this tool at
    all, so there's no expected value to compare them against.
    """
    result = page.evaluate(
        "(a) => fetch(`https://www.prologistics.info/api/issueLog/shortList/?obj_id=${a}&obj=article`, "
        "{credentials: 'include', headers: {'X-Requested-With': 'XMLHttpRequest'}}).then(r => r.json())",
        article_id,
    )
    issues = result.get("issue_list", [])
    if not issues:
        return {"verified": False, "reason": "no issue found after creation"}
    latest = issues[0]
    mismatches = []
    if expected_responsible_username and str(latest.get("resp_username")) != str(expected_responsible_username):
        mismatches.append(f"responsible {latest.get('resp_username')} != {expected_responsible_username}")
    return {"verified": not mismatches, "mismatches": mismatches, "issue": latest}


def collect_article_ids(page, config):
    """Queue scan (if configured) + config.json's test_article_ids, de-duplicated."""
    article_ids = []
    seen = set()

    for queue_url in config.get("queue_urls", []):
        page.goto(queue_url)
        page.wait_for_load_state("networkidle")
        page.evaluate(CORE_JS)
        scan = page.evaluate("window.__aiScanQueue()")
        print(f"Queue {queue_url}\n  -> {len(scan['orders'])} order(s), {len(scan['articleIds'])} flagged article(s)")
        for article_id in scan["articleIds"]:
            if article_id not in seen:
                seen.add(article_id)
                article_ids.append(article_id)

    for article_id in config.get("test_article_ids", []):
        if article_id not in seen:
            seen.add(article_id)
            article_ids.append(article_id)

    return article_ids


def process_article(page, article_id, ai_config, ui_fill, fields_missing):
    # create_issue_via_ui (for a PRIOR article) navigates the page away from
    # wherever it was, which wipes out the injected core.js - re-inject every
    # time so __aiGatherForArticle is always available regardless of what the
    # previous iteration did.
    page.evaluate(CORE_JS)
    gathered = page.evaluate("(id) => window.__aiGatherForArticle(id)", article_id)
    entry = {"article_id": article_id, "gathered": gathered}

    if gathered.get("status") != "ok":
        print(f"[{article_id}] Could not read this article ({gathered.get('status')}) - skipping.")
        return entry

    if not gathered["part"]["looksUnmeasured"]:
        print(f"[{article_id}] Has real size data already - not actually unmeasured, skipping.")
        entry["skipped_reason"] = "has-data"
        return entry

    if gathered.get("alreadyFlagged"):
        print(f"[{article_id}] Already has a dimension-reference ticket logged - skipping.")
        entry["skipped_reason"] = "already-flagged"
        return entry

    best_main_article = gathered.get("bestMainArticle")
    if not best_main_article:
        print(
            f"[{article_id}] All {len(gathered['mainArticles'])} linked main article(s) are themselves "
            "unmeasured (or none could be fetched) - no reliable reference data, skipping."
        )
        entry["skipped_reason"] = "no-reference-data"
        return entry

    print(f"\n[{article_id}] Part: {gathered['part']['name']}")
    print(f"  Using main article {best_main_article['articleId']} ({best_main_article['name']}) "
          f"as reference (of {len(gathered['mainArticles'])} linked): {best_main_article['dims']}")

    estimate = compute_estimate(gathered["part"]["name"], best_main_article)
    entry["estimate"] = estimate
    issue_name = build_reference_issue_name(
        ai_config["aiTagPrefix"], gathered["objId"],
        estimate, best_main_article["href"], gathered.get("signsLimit"),
    )
    print(f"  Issue Log text ({len(issue_name)} chars): {issue_name}")
    entry["issue_name"] = issue_name

    if fields_missing:
        print("  Skipping creation - config.json's ui_fill still has REPLACE_ME fields.")
        return entry

    if not confirm(f"  Create this Issue Log entry for article {article_id} now?"):
        print("  Skipped by user - nothing created.")
        entry["decision"] = "skipped"
        return entry

    picked_username = create_issue_via_ui(page, article_id, issue_name, ui_fill)
    verification = verify_created_issue(page, gathered["objId"], gathered["obj"], picked_username)
    print(f"  Created. Verification: {verification}")
    entry["decision"] = "confirmed"
    entry["verification"] = verification

    # Writing the estimate into the article's own record is a separate,
    # bigger step than creating a ticket - it makes the (assumption-based)
    # estimate the live data the shipping/logistics system treats as fact,
    # with nobody reviewing it first. Gets its own explicit confirmation,
    # per the team (2026-08-26) - never bundled into the ticket-creation yes.
    if not confirm(f"  Write estimated size/weight into article {article_id}'s own record now?"):
        print("  Skipped by user - article record not changed.")
        entry["write_decision"] = "skipped"
        return entry

    write_result = write_estimate_to_article(page, article_id, estimate)
    print(f"  Article record write: {write_result}")
    entry["write_decision"] = "confirmed"
    entry["write_result"] = write_result
    return entry


def main():
    config = load_config()
    ui_fill = config["ui_fill"]
    fields_missing = check_issue_fields_configured(ui_fill)
    if fields_missing:
        print("config.json's ui_fill has unconfigured (REPLACE_ME) fields:")
        for m in fields_missing:
            print(f"  - {m}")
        print(
            "\nSee README.md 'Configuring config.json' for how these were derived. "
            "Scanning below will still run so you can preview the output, but "
            "nothing will be created until this is filled in."
        )

    PROFILE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    run_log = {"processed": []}

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto("https://www.prologistics.info/")
        if not confirm("Log in to prologistics.info in the window that just opened if you aren't already. Ready to continue?"):
            print("Aborted before login confirmation. Re-run when ready.")
            context.close()
            sys.exit(1)

        page.evaluate(CORE_JS)
        article_ids = collect_article_ids(page, config)
        print(f"\n{len(article_ids)} article(s) to process: {article_ids}")

        ai_config = page.evaluate("window.__aiConfig")
        for article_id in article_ids:
            try:
                entry = process_article(page, article_id, ai_config, ui_fill, bool(fields_missing))
            except Exception as e:
                print(f"[{article_id}] ERROR: {e}")
                entry = {"article_id": article_id, "error": str(e)}
            run_log["processed"].append(entry)

        context.close()

    _write_log(run_log)


def _write_log(run_log):
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"run-{timestamp}.json"
    log_path.write_text(json.dumps(run_log, indent=2), encoding="utf-8")
    print(f"\nRun log written to {log_path}")


if __name__ == "__main__":
    main()
