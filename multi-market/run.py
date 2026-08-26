#!/usr/bin/env python3
"""
Multi-market Dimensions Pre-Check runner.

Drives a browser across every market/order-type configured in markets.json,
reusing the same scan/create engine as the single-market bookmarklet
(see ../dimensions-precheck.js, injected here as core.js). For each
market/order-type it shows a dry-run preview and asks for confirmation
before creating anything - same safety net as the bookmarklet's guided flow.

First run: a browser window opens. Log in to prologistics.info normally in
it, then answer 'y' at the prompt. The session is saved under
./browser-profile/ and reused on later runs, so you only log in once (until
the session expires) - no password is ever stored by this script.

Usage:
    pip install -r requirements.txt
    playwright install chromium
    python run.py
"""
import datetime
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
CORE_JS = (HERE / "core.js").read_text(encoding="utf-8")
PROFILE_DIR = HERE / "browser-profile"
LOG_DIR = HERE / "logs"

REQUIRED_FIELDS = ["queue_url", "board_id", "board_column_id", "department_id", "responsible_id"]


def load_markets():
    with open(HERE / "markets.json", encoding="utf-8") as f:
        data = json.load(f)
    return data["markets"]


def find_unconfigured(markets):
    missing = []
    for market in markets:
        for order_type, cfg in market["order_types"].items():
            if cfg is None:
                continue
            for field in REQUIRED_FIELDS:
                if str(cfg.get(field, "")).startswith("REPLACE_ME"):
                    missing.append(f"{market['name']} / {order_type} / {field}")
    return missing


def confirm(prompt):
    return input(f"{prompt} [y/N] ").strip().lower() == "y"


def run_one(page, log, market_name, order_type_name, cfg):
    label = f"{market_name} / {order_type_name}"
    print(f"\n=== {label} ===")
    log.append({"market": market_name, "order_type": order_type_name, "queue_url": cfg["queue_url"]})

    page.goto(cfg["queue_url"])
    page.wait_for_load_state("networkidle")
    page.evaluate(CORE_JS)

    overrides = {
        "board_id": cfg["board_id"],
        "board_column_id": cfg["board_column_id"],
        "department_id": cfg["department_id"],
        "responsible_id": cfg["responsible_id"],
    }
    page.evaluate("(o) => Object.assign(window.__dpConfig.FIXED_FIELDS, o)", overrides)

    page.evaluate("window.__dpConfig.DRY_RUN = true")
    dry = page.evaluate("window.__dpRun()")

    would_create = [r for r in dry if r["status"] == "would-create"]
    already = [r for r in dry if r["status"] == "already-logged"]
    no_button = [r for r in dry if r["status"] == "no-button"]

    print(f"  Would create: {len(would_create)}  |  Already logged: {len(already)}  |  Unresolvable: {len(no_button)}")
    for r in would_create:
        print(f"    - {r['key']}")
    if no_button:
        print("  Could not check (page issue - see 'Known limitations' in the main project README):")
        for r in no_button:
            print(f"    - {r['key']}")

    log[-1]["dry_run"] = dry

    if not would_create:
        print("  Nothing to create here.")
        return

    if not confirm(f"  Create {len(would_create)} issue log(s) for {label}?"):
        print("  Skipped by user.")
        log[-1]["decision"] = "skipped"
        return

    page.evaluate("window.__dpConfig.DRY_RUN = false")
    live = page.evaluate("window.__dpRun()")
    created = [r for r in live if r["status"] == "created"]
    errored = [r for r in live if r["status"] == "error"]
    print(f"  Created: {len(created)}  |  Failed: {len(errored)}")
    for r in errored:
        print(f"    FAILED {r['key']}: {r.get('error')}")

    log[-1]["decision"] = "confirmed"
    log[-1]["live_run"] = live


def main():
    markets = load_markets()

    missing = find_unconfigured(markets)
    if missing:
        print("markets.json has unconfigured (REPLACE_ME) fields - fill these in before running:")
        for m in missing:
            print(f"  - {m}")
        print("\nSee README.md 'Finding the per-market field values'.")
        sys.exit(1)

    PROFILE_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    log = []

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(str(PROFILE_DIR), headless=False)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto("https://www.prologistics.info/")
        if not confirm("Log in to prologistics.info in the window that just opened if you aren't already. Ready to continue?"):
            print("Aborted before login confirmation. Re-run when ready.")
            context.close()
            sys.exit(1)

        try:
            for market in markets:
                for order_type, cfg in market["order_types"].items():
                    if cfg is None:
                        continue
                    try:
                        run_one(page, log, market["name"], order_type, cfg)
                    except Exception as e:
                        print(f"  ERROR running {market['name']}/{order_type}: {e}")
                        log.append({"market": market["name"], "order_type": order_type, "error": str(e)})
        finally:
            context.close()

    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"run-{timestamp}.json"
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    print(f"\nFull run log written to {log_path}")


if __name__ == "__main__":
    main()
