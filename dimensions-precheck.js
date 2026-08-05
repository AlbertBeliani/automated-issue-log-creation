(function () {
  'use strict';

  const CONFIG = {
    DRY_RUN: true,
    LIMIT: Infinity, // set to 1 for a single-order test run

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

    FIXED_FIELDS: {
      fn: 'addIssueLog',
      department_id: '211',
      responsible_id: 'WeArcisz',
      'issue_type[]': '1681',
      issue_priority: '0',
      restricted_access: '0',
      board_id: '24',
      // board_column_id is overwritten per run based on the chosen predefined issue below.
      board_column_id: '412',
    },

    // The "Predefined issue" dropdown only changes board_column_id - tag, board, and
    // everything else stay identical regardless of which one is picked.
    PREDEFINED_ISSUES: [
      { id: '1535626', label: '1535626: DIMENSIONS PRE-CHECK ATS/DECO', board_column_id: '412' },
      { id: '1535622', label: '1535622: DIMENSIONS PRE-CHECK', board_column_id: '383' },
    ],

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
      console.warn(`[SKIP] ${order.key}: button.issueLog not found on detail page`);
      return { key: order.key, status: 'no-button' };
    }

    if (ref.alreadyLogged) {
      console.log(`[SKIP] ${order.key}: DImensions pre-check issue already exists`);
      markProcessed(order.key);
      return { key: order.key, status: 'already-logged' };
    }

    const issue_name = buildIssueName(order);
    console.log(`[ORDER ${order.key}] obj_id=${ref.objId} obj=${ref.obj}\nissue_name:\n${issue_name}`);

    if (CONFIG.DRY_RUN) {
      console.log('[DRY_RUN] would POST addIssueLog now');
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
    console.log(`[DONE] ${order.key}:`, await postRes.text());
    markProcessed(order.key);
    return { key: order.key, status: 'created', issue_name };
  }

  async function run() {
    const allOrders = scanOrders();
    const orders = allOrders.slice(0, CONFIG.LIMIT);
    console.log(
      `Found ${allOrders.length} unprocessed order(s) with dimension-less items - processing ${orders.length} (LIMIT=${CONFIG.LIMIT})`
    );
    const results = [];
    for (const order of orders) {
      try {
        results.push(await createIssueLog(order));
      } catch (e) {
        console.error(`[ERROR] ${order.key}:`, e);
        results.push({ key: order.key, status: 'error', error: String((e && e.message) || e) });
      }
      await sleep(CONFIG.delayBetweenOrdersMs);
    }
    console.log('Run complete.');
    return results;
  }

  window.__dpScan = scanOrders;
  window.__dpRun = run;

  function listOrders(results, status) {
    const matches = results.filter((r) => r.status === status);
    return matches.length ? matches.map((r) => `  - ${r.key}`).join('\n') : '  (none)';
  }

  function choosePredefinedIssue() {
    const options = CONFIG.PREDEFINED_ISSUES;
    const listText = options.map((o, i) => `${i + 1}) ${o.label}`).join('\n');
    const input = prompt(`Which predefined issue should this batch use?\n\n${listText}`, '1');
    if (input === null) return null; // cancelled

    const idx = parseInt(input.trim(), 10) - 1;
    if (Number.isNaN(idx) || idx < 0 || idx >= options.length) {
      alert(`"${input}" is not a valid choice - expected 1 or 2. Click the bookmark again to retry.`);
      return null;
    }
    return options[idx];
  }

  // ---- Guided flow: runs automatically each time the bookmarklet is clicked ----
  async function guidedFlow() {
    const found = scanOrders();
    if (found.length === 0) {
      alert('No unprocessed orders with missing dimensions found on this page.');
      return;
    }

    const chosenIssue = choosePredefinedIssue();
    if (!chosenIssue) return;
    CONFIG.FIXED_FIELDS.board_column_id = chosenIssue.board_column_id;

    const limitInput = prompt(
      `Found ${found.length} order(s) with missing dimensions.\n\nHow many should be processed this run? (leave blank for all ${found.length})`,
      ''
    );
    if (limitInput === null) return; // cancelled

    const trimmed = limitInput.trim();
    const limit = trimmed === '' ? Infinity : Math.max(0, parseInt(trimmed, 10) || 0);

    CONFIG.LIMIT = limit;
    CONFIG.DRY_RUN = true;
    const dryResults = await run();

    const willCreate = dryResults.filter((r) => r.status === 'would-create');
    const alreadyLogged = dryResults.filter((r) => r.status === 'already-logged');
    const noButton = dryResults.filter((r) => r.status === 'no-button');

    let preview = `DRY RUN - nothing has been created yet.\n\n`;
    preview += `Predefined issue: ${chosenIssue.label}\n\n`;
    preview += `Will create issue logs for ${willCreate.length} order(s):\n${listOrders(dryResults, 'would-create')}`;
    preview += `\n\nAlready logged, will be skipped: ${alreadyLogged.length} order(s)`;
    if (noButton.length) {
      preview += `\n\nCould not check ${noButton.length} order(s) (page problem, will be skipped):\n${listOrders(
        dryResults,
        'no-button'
      )}`;
    }
    preview += `\n\nClick OK to actually create the ${willCreate.length} issue log(s) listed above now, or Cancel to stop here.`;

    const proceed = confirm(preview);
    if (!proceed) {
      console.log('Live run cancelled - nothing was created.');
      return;
    }

    if (willCreate.length === 0) {
      alert('Nothing to create - every order was already logged or could not be checked.');
      return;
    }

    CONFIG.DRY_RUN = false;
    const liveResults = await run();

    const created = liveResults.filter((r) => r.status === 'created');
    const errored = liveResults.filter((r) => r.status === 'error');

    let summary = `LIVE RUN COMPLETE\n\n`;
    summary += `Predefined issue: ${chosenIssue.label}\n\n`;
    summary += `Created issue logs for ${created.length} order(s):\n${listOrders(liveResults, 'created')}`;
    if (errored.length) {
      summary += `\n\n${errored.length} order(s) FAILED - please check these manually:\n`;
      summary += errored.map((r) => `  - ${r.key}: ${r.error}`).join('\n');
    }
    alert(summary);
  }

  guidedFlow();
})();
