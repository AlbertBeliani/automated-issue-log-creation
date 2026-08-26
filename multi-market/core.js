// Scan/create engine for the Dimensions Pre-Check automation, adapted for
// external orchestration (Playwright, via run.py) instead of a bookmarklet.
//
// This is Phase A/B logic carried over from ../dimensions-precheck.js as-is:
// same selectors, same "all 4 dims are 0" rule, same two-phase
// scan-list-then-resolve-detail-page approach, same dedup rules. The only
// differences from the bookmarklet version:
//   - CONFIG is exposed on window (as __dpConfig) so the Python driver can
//     override FIXED_FIELDS per market/order-type before each run.
//   - No prompt()/confirm()/alert() UI - guidedFlow() doesn't exist here.
//     run.py handles the dry-run preview and confirmation instead.
//
// Kept as a separate file (not shared/imported) so changes here can never
// break the shipped, tagged bookmarklet. If the site's markup or endpoints
// change, update both files.
(function () {
  'use strict';

  const CONFIG = {
    DRY_RUN: true,
    LIMIT: Infinity,

    LIST_SELECTORS: {
      orderRow: '#egusli > tbody > tr',
      auctionNumberCell: 'td.auction-number',
      articleCheckbox: 'td.shipping-prices input.to_calculate_sp[data-id]',
    },

    DETAIL_URL: (number, txnid) =>
      `https://www.prologistics.info/auction.php?number=${number}&txnid=${txnid}`,

    ENDPOINT: 'https://www.prologistics.info/js_backend.php',
    ISSUE_LIST_ENDPOINT: (objId, obj) =>
      `https://www.prologistics.info/api/issueLog/shortList/?obj_id=${objId}&obj=${obj}`,

    // Overwritten per market/order-type by run.py before each run - the
    // values below are placeholders and are never used as-is.
    FIXED_FIELDS: {
      fn: 'addIssueLog',
      department_id: '',
      responsible_id: '',
      'issue_type[]': '1681',
      issue_priority: '0',
      restricted_access: '0',
      board_id: '',
      board_column_id: '',
    },

    articleUrlTemplate: (articleId) =>
      `https://www.prologistics.info/article.php?original_article_id=${articleId}`,

    subjectSeparator: '\n',
    delayBetweenOrdersMs: 1500,
    dedupStorageKey: 'dimensionsPrecheck_processedOrders',
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function getProcessedSet() {
    try {
      return new Set(JSON.parse(localStorage.getItem(CONFIG.dedupStorageKey) || '[]'));
    } catch {
      return new Set();
    }
  }
  function markProcessed(key) {
    const set = getProcessedSet();
    set.add(key);
    localStorage.setItem(CONFIG.dedupStorageKey, JSON.stringify([...set]));
  }

  function parseDims(text) {
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

  // ---- Phase A: scan list page ----
  function scanOrders() {
    const processed = getProcessedSet();
    const rows = document.querySelectorAll(CONFIG.LIST_SELECTORS.orderRow);
    const orders = [];

    rows.forEach((row) => {
      const cellEl = row.querySelector(CONFIG.LIST_SELECTORS.auctionNumberCell);
      if (!cellEl) return;

      const parsed = extractOrderKey(cellEl);
      if (!parsed) return;
      const { number, txnid } = parsed;
      const key = `${number}/${txnid}`;
      if (processed.has(key)) return;

      const checkboxes = row.querySelectorAll(CONFIG.LIST_SELECTORS.articleCheckbox);
      const seenArticleIds = new Set();
      const missingArticles = [];
      checkboxes.forEach((cb) => {
        const articleId = cb.dataset.id;
        const thEl = cb.closest('th');
        if (!articleId || !thEl || seenArticleIds.has(articleId)) return;
        seenArticleIds.add(articleId);
        const dims = parseDims(thEl.textContent);
        if (isDimensionless(dims)) missingArticles.push({ articleId, dims });
      });

      if (missingArticles.length > 0) {
        orders.push({ number, txnid, key, missingArticles });
      }
    });

    return orders;
  }

  function buildIssueName(order) {
    return order.missingArticles.map((a) => CONFIG.articleUrlTemplate(a.articleId)).join(CONFIG.subjectSeparator);
  }

  function hasExistingDimensionsIssue(issueList) {
    return issueList.some((issue) => (issue.issue_types || '').toLowerCase().includes('dimensions pre-check'));
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

  // ---- Phase B: resolve obj_id/obj from the order's detail page, then check for an existing issue ----
  async function fetchOrderDetails(order) {
    const url = CONFIG.DETAIL_URL(order.number, order.txnid);
    const res = await fetch(url, { credentials: 'include' });
    if (!res.ok) throw new Error(`Detail page fetch failed (${res.status}) for ${order.key}`);
    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const btn = doc.querySelector('button.issueLog');
    if (!btn) return null;
    const objId = btn.dataset.pageId;
    const obj = btn.dataset.param;
    const issueList = await fetchIssueList(objId, obj);
    return {
      objId,
      obj,
      alreadyLogged: hasExistingDimensionsIssue(issueList),
    };
  }

  async function createIssueLog(order) {
    const ref = await fetchOrderDetails(order);
    if (!ref) {
      return { key: order.key, status: 'no-button' };
    }

    if (ref.alreadyLogged) {
      markProcessed(order.key);
      return { key: order.key, status: 'already-logged' };
    }

    const issue_name = buildIssueName(order);

    if (CONFIG.DRY_RUN) {
      return { key: order.key, status: 'would-create', issue_name };
    }

    const body = new URLSearchParams({
      ...CONFIG.FIXED_FIELDS,
      issue_name,
      obj_id: ref.objId,
      obj: ref.obj,
    });

    const postRes = await fetch(CONFIG.ENDPOINT, {
      method: 'POST',
      credentials: 'include',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body,
    });
    if (!postRes.ok) throw new Error(`HTTP ${postRes.status} for ${order.key}`);
    await postRes.text();
    markProcessed(order.key);
    return { key: order.key, status: 'created', issue_name };
  }

  async function run() {
    const allOrders = scanOrders();
    const orders = allOrders.slice(0, CONFIG.LIMIT);
    const results = [];
    for (const order of orders) {
      try {
        results.push(await createIssueLog(order));
      } catch (e) {
        results.push({ key: order.key, status: 'error', error: String((e && e.message) || e) });
      }
      await sleep(CONFIG.delayBetweenOrdersMs);
    }
    return results;
  }

  window.__dpConfig = CONFIG;
  window.__dpScan = scanOrders;
  window.__dpRun = run;
})();
