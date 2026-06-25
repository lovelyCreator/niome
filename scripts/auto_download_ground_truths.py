"""Automate ground-truth downloads from the niome leaderboard dashboard.

Uses Playwright to drive a real browser (headless) and click through tasks,
then imports each one via collect_ground_truth.py.

Usage:
    # Install once on the VPS:
    pip install playwright
    playwright install chromium

    # Then run:
    python scripts/auto_download_ground_truths.py [--max 30] [--headed]

The script:
  - Opens the niome leaderboard dashboard
  - Iterates through every task in the left panel
  - For each task: clicks "Download Ground Truth", waits for download to land
  - Saves to /tmp/auto_gt/<task_id>/
  - Auto-imports via collect_ground_truth.py
  - Skips tasks already in your training corpus

Options:
    --max N        Stop after N new downloads (default: unlimited)
    --headed       Show browser window (debugging) instead of headless
    --keep         Keep raw downloads in /tmp/auto_gt (default: delete after import)
    --dashboard URL  Override dashboard URL
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sys.exit(
        "Playwright not installed. Run:\n"
        "    pip install playwright\n"
        "    playwright install chromium"
    )

DEFAULT_DASHBOARD = "https://niome-leaderboard.genomes.io"
DOWNLOAD_DIR = Path("/tmp/auto_gt")
TRAINING_INDEX = Path.home() / "niome_training" / "ground_truths" / "index.json"
COLLECTOR = Path(__file__).parent / "collect_ground_truth.py"

# UUID regex matching task IDs
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def load_existing_task_ids():
    if not TRAINING_INDEX.exists():
        return set()
    with open(TRAINING_INDEX) as f:
        return set(json.load(f).get("tasks", {}).keys())


def import_download(zip_or_dir, task_id):
    """Run collect_ground_truth.py on a downloaded file."""
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), str(zip_or_dir), "--task-id", task_id],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  ✓ imported into training corpus")
        return True
    else:
        print(f"  ✗ import failed: {result.stderr.strip()[:200]}")
        return False


def extract_task_ids_from_page(page):
    """Find all task UUIDs visible in the task panel."""
    # Read page text and extract UUIDs
    html = page.content()
    return list(dict.fromkeys(UUID_RE.findall(html)))  # de-dupe preserving order


def download_for_task(page, task_id, download_dir):
    """Click on a task in the sidebar, then click Download Ground Truth.

    Returns the downloaded file path, or None on failure.
    """
    # Try to find a clickable element containing this task UUID
    selector_candidates = [
        f"text=/.*{task_id[:8]}.*/",  # partial match of first 8 chars
        f"text=/{task_id[:13]}/",
    ]

    clicked = False
    for sel in selector_candidates:
        try:
            elements = page.locator(sel).all()
            if elements:
                elements[0].click(timeout=5000)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        # Last resort: search by text content
        try:
            page.get_by_text(task_id[:13], exact=False).first.click(timeout=5000)
            clicked = True
        except Exception:
            return None

    page.wait_for_timeout(800)  # let task details load

    # Now click the Download Ground Truth button
    try:
        with page.expect_download(timeout=30000) as download_info:
            page.get_by_role("button", name=re.compile("download.*ground.*truth", re.I)).click()
        download = download_info.value
    except (PlaywrightTimeoutError, Exception) as e:
        # Try alternate button selectors
        try:
            with page.expect_download(timeout=30000) as download_info:
                page.locator("button:has-text('Download')").first.click()
            download = download_info.value
        except Exception:
            print(f"  ✗ could not click download button: {e}")
            return None

    # Save to our directory
    target = download_dir / f"{task_id}{Path(download.suggested_filename).suffix}"
    download.save_as(target)
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None,
                        help="Maximum number of NEW downloads (skips already-imported)")
    parser.add_argument("--headed", action="store_true",
                        help="Show browser window (default: headless)")
    parser.add_argument("--keep", action="store_true",
                        help="Don't delete raw downloads after import")
    parser.add_argument("--dashboard", default=DEFAULT_DASHBOARD,
                        help=f"Dashboard URL (default: {DEFAULT_DASHBOARD})")
    parser.add_argument("--task-ids", default=None,
                        help="Optional file with task IDs to target (one per line)")
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    existing = load_existing_task_ids()
    print(f"Already have {len(existing)} task(s) in training corpus")

    targeted = None
    if args.task_ids:
        with open(args.task_ids) as f:
            targeted = set(line.strip() for line in f if line.strip())
        print(f"Targeting {len(targeted)} task ID(s) from {args.task_ids}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print(f"Opening {args.dashboard}")
        page.goto(args.dashboard, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)  # let JS finish rendering

        # Extract task UUIDs from the page
        task_ids = extract_task_ids_from_page(page)
        print(f"Found {len(task_ids)} task UUIDs in dashboard")

        # Filter to new ones
        if targeted:
            task_ids = [tid for tid in task_ids if tid in targeted]
        new_tasks = [tid for tid in task_ids if tid not in existing]
        print(f"  {len(new_tasks)} new (not yet imported)")

        if args.max:
            new_tasks = new_tasks[:args.max]
            print(f"  limited to first {len(new_tasks)} due to --max")

        downloaded = 0
        imported = 0
        for i, task_id in enumerate(new_tasks, 1):
            print(f"\n[{i}/{len(new_tasks)}] Task {task_id}")
            try:
                path = download_for_task(page, task_id, DOWNLOAD_DIR)
                if path:
                    print(f"  ✓ downloaded {path.name} ({path.stat().st_size // 1024} KB)")
                    downloaded += 1
                    if import_download(path, task_id):
                        imported += 1
                        if not args.keep:
                            path.unlink(missing_ok=True)
                else:
                    print(f"  ✗ download failed")
            except Exception as e:
                print(f"  ✗ unexpected error: {e}")

            # Brief pause to be polite to the server
            page.wait_for_timeout(1500)

        print(f"\n=== Summary ===")
        print(f"Downloaded: {downloaded}")
        print(f"Imported into training corpus: {imported}")

        browser.close()


if __name__ == "__main__":
    main()
