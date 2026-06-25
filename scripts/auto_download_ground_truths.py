"""Automated ground-truth downloader for the niome leaderboard.

Two modes:
  1. API mode (default, fast): direct HTTP. Probes/discovers the dashboard's
     download endpoint, then fetches each task directly via requests.
  2. Browser mode (--browser): Playwright fallback if API doesn't work.

Usage:
    python scripts/auto_download_ground_truths.py [--max 30]
    python scripts/auto_download_ground_truths.py --probe       # just find the API
    python scripts/auto_download_ground_truths.py --browser     # use Playwright

The script:
  - Skips tasks already in your training corpus
  - Saves downloads to /tmp/auto_gt/<task_id>.<ext>
  - Auto-imports via collect_ground_truth.py
  - Logs progress so you can leave it running in tmux
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dep. Run: pip install requests")

DEFAULT_DASHBOARD = "https://niome-leaderboard.genomes.io"
DEFAULT_API = "https://niome-api.genomes.io"
DOWNLOAD_DIR = Path("/tmp/auto_gt")
TRAINING_INDEX = Path.home() / "niome_training" / "ground_truths" / "index.json"
COLLECTOR = Path(__file__).parent / "collect_ground_truth.py"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Tried URL patterns. The first one that returns 200 will be used for everything.
# {task_id} gets substituted.
API_PATTERNS = [
    "https://niome-api.genomes.io/api/leaderboard/task/{task_id}/ground_truth",
    "https://niome-api.genomes.io/api/leaderboard/{task_id}/ground_truth",
    "https://niome-api.genomes.io/api/leaderboard/{task_id}/download",
    "https://niome-api.genomes.io/api/tasks/{task_id}/ground_truth_url",
    "https://niome-api.genomes.io/api/tasks/{task_id}/ground_truth",
    "https://niome-api.genomes.io/api/tasks/{task_id}/download",
    "https://niome-api.genomes.io/api/tasks/{task_id}",
    "https://niome-api.genomes.io/api/ground_truth/{task_id}",
    "https://niome-api.genomes.io/api/download/{task_id}",
    "https://niome-leaderboard.genomes.io/api/download/{task_id}",
    "https://niome-leaderboard.genomes.io/api/ground_truth/{task_id}",
    "https://niome-leaderboard.genomes.io/api/tasks/{task_id}/download",
    "https://niome-leaderboard.genomes.io/api/tasks/{task_id}/ground_truth",
]


def load_existing_task_ids():
    if not TRAINING_INDEX.exists():
        return set()
    with open(TRAINING_INDEX) as f:
        return set(json.load(f).get("tasks", {}).keys())


def import_download(zip_or_dir, task_id):
    """Import a downloaded file into the training corpus."""
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), str(zip_or_dir), "--task-id", task_id],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return True
    print(f"  ✗ import failed: {result.stderr.strip()[:200]}")
    return False


# ============================================================
# API MODE
# ============================================================

def fetch_task_ids_from_dashboard():
    """Try to extract task UUIDs by fetching the dashboard's HTML/JSON."""
    found = set()
    # Direct HTML
    try:
        r = requests.get(DEFAULT_DASHBOARD, timeout=15)
        found.update(UUID_RE.findall(r.text))
    except Exception:
        pass
    # Try common JSON endpoints
    for path in ["/api/tasks", "/api/leaderboard/tasks", "/api/tasks/list",
                 "/api/leaderboard", "/api/tasks/all"]:
        for base in [DEFAULT_DASHBOARD, DEFAULT_API]:
            try:
                r = requests.get(base + path, timeout=10)
                if r.status_code == 200:
                    text = r.text
                    new = UUID_RE.findall(text)
                    if new:
                        found.update(new)
                        print(f"  Found {len(new)} UUIDs at {base}{path}")
            except Exception:
                pass
    return list(found)


def discover_api_urls():
    """Parse the dashboard's HTML + JS bundles for API URL patterns.

    Most SPAs hard-code their API base URL in their JS bundle. This finds it.
    Returns a list of candidate URLs/patterns found.
    """
    print("Fetching dashboard HTML...")
    try:
        r = requests.get(DEFAULT_DASHBOARD, timeout=30)
        html = r.text
        print(f"  Got {len(html)} bytes")
    except Exception as e:
        print(f"  ✗ failed to fetch dashboard: {e}")
        return []

    # Find all script src URLs
    scripts = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', html)
    # Also look at inline scripts
    inline = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)
    print(f"  Found {len(scripts)} script tags, {len(inline)} inline scripts")

    # Gather text content
    contents = list(inline)
    for s in scripts:
        if not s.startswith("http"):
            s = DEFAULT_DASHBOARD + (s if s.startswith("/") else "/" + s)
        try:
            r = requests.get(s, timeout=20)
            if r.status_code == 200:
                contents.append(r.text)
        except Exception as e:
            print(f"  ✗ couldn't fetch {s}: {type(e).__name__}")

    # Search for relevant URL patterns
    keywords = ["api", "download", "ground", "truth", "leaderboard", "task"]
    interesting = set()
    for txt in contents:
        # Absolute URLs
        for m in re.findall(r'["\'](https?://[^"\'\s]+)["\']', txt):
            if any(kw in m.lower() for kw in keywords):
                interesting.add(m)
        # Relative API paths
        for m in re.findall(r'["\'](\/api\/[^"\'\s]+)["\']', txt):
            interesting.add(m)

    # Filter out obviously-templated patterns (contain :param or {var})
    # but keep them visible since they reveal the structure
    print(f"\nFound {len(interesting)} candidate URL(s):")
    for u in sorted(interesting):
        print(f"  {u}")
    return list(interesting)


def probe_endpoint(task_id):
    """Try each API pattern and return the first that works."""
    print(f"Probing API endpoints with task {task_id[:8]}...")
    for pattern in API_PATTERNS:
        url = pattern.format(task_id=task_id)
        try:
            r = requests.get(url, allow_redirects=True, timeout=15)
            ct = r.headers.get("content-type", "").lower()
            cl = int(r.headers.get("content-length", "0") or 0)
            print(f"  [{r.status_code:3d}] {url}  ct={ct[:30]} len={cl}")
            if r.status_code == 200:
                # Distinguish: file download vs JSON metadata
                if "application/json" in ct:
                    try:
                        data = r.json()
                        # Maybe data has a "url" pointing to the actual download
                        for key in ("url", "download_url", "ground_truth_url", "link"):
                            if key in data:
                                print(f"      ✓ JSON contains '{key}': {data[key]}")
                                return ("json_with_url", pattern, key)
                    except Exception:
                        pass
                elif cl > 1024:  # plausible file download
                    print(f"      ✓ Looks like direct file download")
                    return ("direct_download", pattern, None)
        except requests.RequestException as e:
            print(f"  [ERR] {url} → {type(e).__name__}")
    return None


def download_via_api(task_id, mode, pattern, json_key, download_dir):
    """Use the discovered API endpoint to download one ground truth."""
    url = pattern.format(task_id=task_id)
    try:
        r = requests.get(url, allow_redirects=True, timeout=60)
        if r.status_code != 200:
            return None, f"http {r.status_code}"

        if mode == "json_with_url":
            data = r.json()
            actual_url = data.get(json_key)
            if not actual_url:
                return None, f"json missing {json_key}"
            r = requests.get(actual_url, timeout=60)
            if r.status_code != 200:
                return None, f"http {r.status_code} on json url"

        # Save by content-disposition or fallback to task_id
        cd = r.headers.get("content-disposition", "")
        fname_m = re.search(r'filename="?([^";]+)"?', cd)
        suffix = ".zip"
        if fname_m:
            suffix = Path(fname_m.group(1)).suffix or ".zip"
        target = download_dir / f"{task_id}{suffix}"
        target.write_bytes(r.content)
        return target, f"{len(r.content)//1024} KB"
    except requests.RequestException as e:
        return None, f"{type(e).__name__}: {e}"


# ============================================================
# BROWSER MODE (Playwright fallback)
# ============================================================

def browser_download(max_tasks, headed, download_dir, existing, targeted=None):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        sys.exit("Playwright not installed. Run: pip install playwright && playwright install chromium")

    downloaded = 0
    imported = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print(f"Opening {DEFAULT_DASHBOARD}")
        # Use domcontentloaded instead of networkidle (the dashboard polls forever)
        page.goto(DEFAULT_DASHBOARD, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)  # let JS render

        ids = list(dict.fromkeys(UUID_RE.findall(page.content())))
        print(f"Found {len(ids)} task UUIDs")

        if targeted:
            ids = [t for t in ids if t in targeted]
        new = [t for t in ids if t not in existing]
        if max_tasks:
            new = new[:max_tasks]
        print(f"Will attempt {len(new)} new tasks")

        for i, task_id in enumerate(new, 1):
            print(f"\n[{i}/{len(new)}] {task_id}")
            try:
                page.get_by_text(task_id[:13], exact=False).first.click(timeout=10000)
                page.wait_for_timeout(800)
                with page.expect_download(timeout=30000) as di:
                    page.get_by_role("button", name=re.compile("download.*ground.*truth", re.I)).click()
                path = download_dir / f"{task_id}{Path(di.value.suggested_filename).suffix}"
                di.value.save_as(path)
                print(f"  ✓ downloaded {path.name}")
                downloaded += 1
                if import_download(path, task_id):
                    imported += 1
            except PWTimeout:
                print(f"  ✗ timeout")
            except Exception as e:
                print(f"  ✗ {type(e).__name__}: {e}")
            page.wait_for_timeout(1000)

        browser.close()
    return downloaded, imported


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None,
                        help="Maximum NEW downloads")
    parser.add_argument("--probe", action="store_true",
                        help="Probe API endpoints and exit (no downloads)")
    parser.add_argument("--discover", action="store_true",
                        help="Parse dashboard JS bundles to find real API URLs")
    parser.add_argument("--browser", action="store_true",
                        help="Use Playwright instead of direct HTTP")
    parser.add_argument("--headed", action="store_true",
                        help="Browser mode: show window (default headless)")
    parser.add_argument("--task-ids", default=None,
                        help="File with one task ID per line (skip dashboard discovery)")
    parser.add_argument("--keep", action="store_true",
                        help="Don't delete raw downloads after import")
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    existing = load_existing_task_ids()
    print(f"Already have {len(existing)} task(s) in training corpus\n")

    # Discover mode: parse JS bundles for API URLs
    if args.discover:
        urls = discover_api_urls()
        if urls:
            print(f"\n→ Send the dashboard URLs to your AI assistant to build the fetch script.")
        else:
            print("\n✗ No API URLs found. Try --browser mode.")
        return

    # Probe-only mode
    if args.probe:
        sample_id = next(iter(existing), "d4517212-279f-413b-a2ed-1896d5f4fdd6")
        endpoint = probe_endpoint(sample_id)
        print()
        if endpoint:
            print(f"✓ Found working endpoint: {endpoint}")
        else:
            print("✗ No API endpoint found via probe. Use --discover to parse JS bundles, or --browser fallback.")
        return

    # Browser mode
    if args.browser:
        targeted = None
        if args.task_ids:
            with open(args.task_ids) as f:
                targeted = set(line.strip() for line in f if line.strip())
        downloaded, imported = browser_download(
            args.max, args.headed, DOWNLOAD_DIR, existing, targeted
        )
        print(f"\n=== Summary ===")
        print(f"Downloaded: {downloaded}")
        print(f"Imported: {imported}")
        return

    # ---- API mode (default) ----

    # Collect task IDs
    if args.task_ids:
        with open(args.task_ids) as f:
            task_ids = [line.strip() for line in f if line.strip()]
        print(f"Read {len(task_ids)} task IDs from {args.task_ids}")
    else:
        print("Discovering task IDs from dashboard...")
        task_ids = fetch_task_ids_from_dashboard()
        print(f"Found {len(task_ids)} task ID(s)")
        if not task_ids:
            print("No task IDs found. Try --task-ids or --browser modes.")
            return

    new_tasks = [tid for tid in task_ids if tid not in existing]
    print(f"  {len(new_tasks)} new (not yet imported)")
    if args.max:
        new_tasks = new_tasks[:args.max]
        print(f"  limited to {len(new_tasks)} by --max")

    if not new_tasks:
        print("Nothing to download. All known tasks already imported.")
        return

    # Probe to find the working endpoint using the first task
    probe_id = new_tasks[0]
    endpoint = probe_endpoint(probe_id)
    if not endpoint:
        print("\n✗ Could not auto-discover an API endpoint.")
        print("Run with --browser to use Playwright, OR paste the cURL from your browser.")
        return

    mode, pattern, json_key = endpoint
    print(f"\nUsing endpoint mode='{mode}', pattern={pattern}\n")

    downloaded = 0
    imported = 0
    for i, task_id in enumerate(new_tasks, 1):
        print(f"[{i}/{len(new_tasks)}] {task_id}")
        path, info = download_via_api(task_id, mode, pattern, json_key, DOWNLOAD_DIR)
        if path:
            print(f"  ✓ saved {path.name} ({info})")
            downloaded += 1
            if import_download(path, task_id):
                imported += 1
                if not args.keep:
                    path.unlink(missing_ok=True)
        else:
            print(f"  ✗ failed: {info}")
        time.sleep(0.5)

    print(f"\n=== Summary ===")
    print(f"Downloaded: {downloaded}")
    print(f"Imported: {imported}")


if __name__ == "__main__":
    main()
