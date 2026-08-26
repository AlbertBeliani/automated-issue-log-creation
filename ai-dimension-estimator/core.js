// Scan/read engine for the Missing-Dimensions Reference Finder, for external
// orchestration (Playwright, via run.py) - same pattern as
// ../multi-market/core.js.
//
// v3 (2026-08-25): dropped AI estimation entirely - no Anthropic API key
// needed. Same philosophy as ../dimensions-precheck.js: don't guess a
// number, surface the facts (which real, measured main article a part
// belongs to) and let a human fill in the actual value. This file only
// reads/scans; run.py builds the ticket text and creates it.
//
// v2 (2026-08-25): chained onto the existing order-scan instead of being a
// standalone "one article at a time" tool. Per the team: the real way
// articles-with-no-dimensions are found is by scanning ORDER queues (exactly
// what ../multi-market/core.js's scanOrders() already does), not by opening
// each of the ~10,000 replacement-part articles' own edit pages and guessing
// from what's in the dimension fields there.
//
// So this file does two things:
//   A. scanQueueForFlaggedArticles() - the SAME order-scan logic as
//      ../multi-market/core.js's scanOrders(), reused here (kept as a
//      separate copy rather than shared/imported, same reasoning as that
//      file: changes here must never risk breaking the shipped, in-
//      production Dimensions Pre-Check tool). Run this on a queue page
//      (search.php) to get the de-duplicated list of flagged article IDs.
//   B. For each flagged article ID: fetchArticleData() it (background
//      fetch, not a page navigation) and find its main article(s), so
//      run.py can build a reference ticket for it - ADDITIONAL to (not a
//      replacement for) the existing per-order "dimensions pre-check" issue
//      multi-market's tool creates.
(function () {
  'use strict';

  const CONFIG = {
    DRY_RUN: true,

    // Confirmed live 2026-08-25 against articles 300284, 254822, 300267, 6588.
    SELECTORS: {
      dimLength: 'input[name="upd_dimension_l"]',
      dimWidth: 'input[name="upd_dimension_w"]',
      dimHeight: 'input[name="upd_dimension_h"]',
      weight: 'input[name="upd_weight_parcel"]',
      articleName: 'input[name="name[english]"]',
    },

    // ---- Order-scan selectors, copied from ../multi-market/core.js ----
    QUEUE_SELECTORS: {
      orderRow: '#egusli > tbody > tr',
      auctionNumberCell: 'td.auction-number',
      articleCheckbox: 'td.shipping-prices input.to_calculate_sp[data-id]',
    },

    ARTICLE_URL: (articleId) => `https://www.prologistics.info/article.php?original_article_id=${articleId}`,
    ISSUE_LIST_ENDPOINT: (objId, obj) =>
      `https://www.prologistics.info/api/issueLog/shortList/?obj_id=${objId}&obj=${obj}`,

    // v3 (2026-08-25): no AI estimate happens anymore - this now flags a
    // resolved REFERENCE article for a human to use, not a guessed number.
    // Kept the tag/predefined issue named "Ai dimension estimate" as-is per
    // the team (not worth re-creating it), but the ticket text prefix
    // changed so it doesn't imply a number was guessed.
    aiTagPrefix: '[DIM REF]',
    dedupKindMarker: 'ai dimension estimate',
  };

  function parseOrderDims(text) {
    const num = (label) => {
      const m = text.match(new RegExp(label + '\\s*:?\\s*(-?\\d+(\\.\\d+)?)'));
      return m ? parseFloat(m[1]) : NaN;
    };
    return { height: num('height'), width: num('width'), length: num('length'), weight: num('weight') };
  }
  function isDimensionless(d) {
    return [d.height, d.width, d.length, d.weight].every((v) => Number(v) === 0);
  }
  function extractOrderKey(cellEl) {
    const linkEl = cellEl.querySelector('a[href*="auction.php?number="]');
    if (!linkEl) return null;
    const m = linkEl.getAttribute('href').match(/number=(\d+).*txnid=(\d+)/);
    if (!m) return null;
    return { number: m[1], txnid: m[2] };
  }

  // ---- Phase A: scan a queue page (search.php) for orders with flagged articles ----
  // Same detection rule as ../multi-market/core.js: an article is flagged if
  // ALL of height/width/length/weight render as 0 on the order grid. Returns
  // both the per-order breakdown (useful for logging) and the flat,
  // de-duplicated list of article IDs to run the estimator on.
  function scanQueueForFlaggedArticles() {
    const rows = document.querySelectorAll(CONFIG.QUEUE_SELECTORS.orderRow);
    const orders = [];
    const articleIdSet = new Set();

    rows.forEach((row) => {
      const cellEl = row.querySelector(CONFIG.QUEUE_SELECTORS.auctionNumberCell);
      if (!cellEl) return;
      const parsed = extractOrderKey(cellEl);
      if (!parsed) return;

      const checkboxes = row.querySelectorAll(CONFIG.QUEUE_SELECTORS.articleCheckbox);
      const seenInRow = new Set();
      const missingArticles = [];
      checkboxes.forEach((cb) => {
        const articleId = cb.dataset.id;
        const thEl = cb.closest('th');
        if (!articleId || !thEl || seenInRow.has(articleId)) return;
        seenInRow.add(articleId);
        const dims = parseOrderDims(thEl.textContent);
        if (isDimensionless(dims)) {
          missingArticles.push({ articleId, dims });
          articleIdSet.add(articleId);
        }
      });

      if (missingArticles.length > 0) {
        orders.push({ number: parsed.number, txnid: parsed.txnid, missingArticles });
      }
    });

    return { orders, articleIds: Array.from(articleIdSet) };
  }

  // ---- Article read, by ID, via background fetch (works from ANY page) ----
  function extractArticleId(href) {
    const m = href && href.match(/original_article_id=(\d+)/);
    return m ? m[1] : null;
  }

  // CONFIRMED live 2026-08-25 against a real team-flagged article (1868905,
  // "single chair MINA taupe"): an article with genuinely no size record has
  // NO upd_dimension_l/w/h or upd_weight_parcel input at all - instead of an
  // existing-parcel edit row, it shows an EMPTY "add new parcel" form
  // (unprefixed input names: dimension_l/w/h, weight_parcel) inside a
  // <tr class="highlighted-row">. This replaces an earlier (wrong) attempt
  // at detecting "unmeasured" by matching a specific placeholder value
  // (10x10x1cm/0.1kg) - the team confirmed some articles legitimately ARE
  // that exact size, so a value-based guess can't be trusted. Presence/
  // absence of the field is a hard fact, not a guess.
  function hasParcelData(doc) {
    return !!doc.querySelector(CONFIG.SELECTORS.dimLength);
  }
  function readDimsFromDoc(doc) {
    if (!hasParcelData(doc)) return { length: null, width: null, height: null, weight: null };
    const num = (sel) => {
      const el = doc.querySelector(sel);
      return el ? parseFloat(el.value) : NaN;
    };
    return {
      length: num(CONFIG.SELECTORS.dimLength),
      width: num(CONFIG.SELECTORS.dimWidth),
      height: num(CONFIG.SELECTORS.dimHeight),
      weight: num(CONFIG.SELECTORS.weight),
    };
  }
  // CONFIRMED live 2026-08-26: a saved parcel record with ALL FOUR values
  // exactly 0 is a second, legitimate "unmeasured" signal - distinct from
  // "no parcel record at all" (hasParcelData above). Seen when a team
  // member manually reset a wrong estimate back to 0 via the site's own UI
  // to flag it for re-estimation - the record still exists (so
  // hasParcelData() alone would wrongly call it "measured"), but reads as
  // 0/0/0/0. This does NOT reintroduce the earlier-debunked placeholder-
  // value heuristic (10x10x1cm/0.1kg, confirmed some articles legitimately
  // ARE that size) - only an exact all-zero record is treated as
  // unmeasured, same rule ../multi-market/core.js already uses at the
  // order level (isDimensionless).
  function isAllZero(dims) {
    return [dims.length, dims.width, dims.height, dims.weight].every((v) => Number(v) === 0);
  }

  // The "Used as replacement article in" label sits in its own <tr> with an
  // empty second cell - the actual link(s) are in the NEXT <tr>, nested
  // inside a <table>. A part can be a replacement in MULTIPLE main articles
  // (confirmed: article 300267 links to 5). Each link's color is CONFIRMED
  // (per the team, 2026-08-25): green = active article, red = EOL
  // (discontinued, won't return to assortment) - not a kit-vs-product
  // signal, and not meaningful for size (an EOL bed's size doesn't change).
  function findMainArticleLinks(doc) {
    const rows = Array.from(doc.querySelectorAll('tr'));
    const labelIdx = rows.findIndex((tr) => {
      const b = tr.querySelector('td b');
      return b && b.textContent.trim() === 'Used as replacement article in';
    });
    if (labelIdx === -1) return [];
    const linkRow = rows[labelIdx + 1];
    if (!linkRow) return [];
    return Array.from(linkRow.querySelectorAll('a[href*="original_article_id="]')).map((a) => ({
      href: a.href,
      articleId: extractArticleId(a.href),
      label: a.textContent.trim(),
      status: a.style.color === 'rgb(0, 102, 0)' ? 'active' : a.style.color === 'rgb(153, 0, 23)' ? 'eol' : 'unknown',
    }));
  }

  function parseArticleDoc(doc) {
    const nameEl = doc.querySelector(CONFIG.SELECTORS.articleName);
    const dims = readDimsFromDoc(doc);
    const looksUnmeasured = !hasParcelData(doc) || isAllZero(dims);
    return {
      name: nameEl ? nameEl.value.trim() : null,
      dims,
      looksUnmeasured,
      mainArticleLinks: findMainArticleLinks(doc),
    };
  }

  async function fetchArticleDoc(articleId) {
    const url = CONFIG.ARTICLE_URL(articleId);
    const res = await fetch(url, { credentials: 'include' });
    if (!res.ok) throw new Error(`Article fetch failed (${res.status}) for ${articleId}`);
    const html = await res.text();
    return new DOMParser().parseFromString(html, 'text/html');
  }

  async function fetchIssueList(objId, obj) {
    const url = CONFIG.ISSUE_LIST_ENDPOINT(objId, obj);
    const res = await fetch(url, {
      credentials: 'include',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    if (!res.ok) throw new Error(`shortList fetch failed (${res.status}) for obj_id=${objId}`);
    const data = await res.json();
    return data.issue_list || [];
  }
  function hasExistingReferenceTicket(issueList) {
    return issueList.some((issue) =>
      (issue.subject || issue.issue_name || '').toLowerCase().includes(CONFIG.dedupKindMarker)
    );
  }

  // Called by run.py once per flagged article ID. Fetches the part's own
  // page (for its issueLog button + name), fetches every linked main
  // article, and picks bestMainArticle = first linked article that HAS real
  // parcel data (see hasParcelData - a main article can itself be
  // unmeasured, e.g. a "Hardware set" grouping article). No side effects
  // (nothing created yet).
  async function gatherForArticle(articleId) {
    const doc = await fetchArticleDoc(articleId);
    const btn = doc.querySelector('button.issueLog');
    if (!btn) return { articleId, status: 'no-button' };

    const objId = btn.dataset.pageId;
    const obj = btn.dataset.param;
    const signsLimit = btn.dataset.signsLimit ? parseInt(btn.dataset.signsLimit, 10) : null;
    const part = parseArticleDoc(doc);

    const mainArticles = [];
    for (const link of part.mainArticleLinks) {
      const mainDoc = await fetchArticleDoc(link.articleId);
      mainArticles.push({ ...parseArticleDoc(mainDoc), ...link });
    }
    const bestMainArticle = mainArticles.find((a) => !a.looksUnmeasured) || null;

    const issueList = await fetchIssueList(objId, obj);
    const alreadyFlagged = hasExistingReferenceTicket(issueList);

    return {
      articleId, status: 'ok', objId, obj, signsLimit,
      part: { name: part.name, dims: part.dims, looksUnmeasured: part.looksUnmeasured },
      mainArticleLinks: part.mainArticleLinks, mainArticles, bestMainArticle, alreadyFlagged,
    };
  }

  // NOTE: there used to be a createIssueLog() here that POSTed straight to
  // js_backend.php?fn=addIssueLog (mirroring the ORDER-level flow's captured
  // request). Confirmed WRONG for obj=article (2026-08-25): manually creating
  // a real test ticket (#531511) showed this form has no Department field at
  // all (derived from the responsible person instead) and the modal's actual
  // field names/request shape couldn't be safely captured. Ticket creation
  // now happens in run.py by driving the real "Add to issuelog" UI form with
  // Playwright (click button, click through the select2 widgets, click OK) -
  // see run.py's create_issue_via_ui(). This file only reads/scans; it never
  // creates anything.

  window.__aiConfig = CONFIG;
  window.__aiScanQueue = scanQueueForFlaggedArticles;
  window.__aiGatherForArticle = gatherForArticle;
})();
