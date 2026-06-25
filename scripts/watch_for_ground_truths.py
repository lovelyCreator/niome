"""Persistent watcher: polls the niome dashboard for ground-truth availability.

Validators may keep ground truth files for only a short window after a task
completes. This script runs 24/7 in tmux, polling the API and downloading
immediately when ground truths become available.

Run in a dedicated tmux session:
    tmux -L gt-watcher new -s watcher
    cd ~/niome && source venv/bin/activate
    python3 scripts/watch_for_ground_truths.py 2>&1 | tee -a ~/niome_training/logs/watcher.log
    # Ctrl+b then d to detach

The watcher:
  - Polls /api/tasks every 60s for new task IDs
  - For each new task: tries /api/tasks/ground_truth_urls with various shapes
  - Downloads + imports if 200
  - Logs failures (especially "Ground truth not available yet")
  - Retries each task up to N times over 30 min, then gives up

Resumes safely: tracks attempted task IDs in a state file to avoid re-polling.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Install requests: pip install requests")

DASHBOARD = "https://niome-leaderboard.genomes.io"
API_LIST = f"{DASHBOARD}/api/tasks"
API_URLS = f"{DASHBOARD}/api/tasks/ground_truth_urls"
API_FILE = f"{DASHBOARD}/api/tasks/ground_truth_file"

TRAINING_DIR = Path.home() / "niome_training"
GT_DIR = TRAINING_DIR / "ground_truths"
INDEX_FILE = GT_DIR / "index.json"
STATE_FILE = TRAINING_DIR / "logs" / "watcher_state.json"
DOWNLOAD_DIR = Path("/tmp/auto_gt")
COLLECTOR = Path(__file__).parent / "collect_ground_truth.py"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

REQUEST_HEADERS = {
    "Accept": "application/json",
    "Origin": DASHBOARD,
    "Referer": DASHBOARD + "/",
    "User-Agent": "Mozilla/5.0 niome-gt-watcher",
}

# Polling cadence
POLL_TASKS_INTERVAL = 120         # seconds between fetching task list
TASK_RETRY_COOLDOWN = 300         # don't retry same task within 5 min (rate limit)
INTER_TASK_SLEEP = 0.5            # seconds between successive task attempts
STATE_SAVE_INTERVAL = 30          # seconds between state file syncs


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def load_imported_task_ids():
    """Return all task_ids known to be duplicates of something we already have.

    This includes the canonical task_id of each unique simulation AND every
    alias recorded after dedup — otherwise we'd re-download known duplicates
    every cycle just to throw them away.
    """
    if not INDEX_FILE.exists():
        return set()
    with open(INDEX_FILE) as f:
        idx = json.load(f)
    known = set(idx.get("tasks", {}).keys())
    for task in idx.get("tasks", {}).values():
        known.update(task.get("aliases", []))
    return known


def load_state():
    if not STATE_FILE.exists():
        return {"attempted": {}, "succeeded": [], "permanent_404": []}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    tmp.replace(STATE_FILE)


def fetch_task_list():
    """Get current task IDs visible on the dashboard. Returns list of UUIDs."""
    try:
        r = requests.get(API_LIST, headers=REQUEST_HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        data = r.json()
        items = data if isinstance(data, list) else data.get("items") or data.get("data") or data.get("tasks") or []
        ids = []
        for item in items:
            if not isinstance(item, dict):
                continue
            tid = item.get("task_id") or item.get("id")
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            tid = tid or content.get("task_id")
            if tid and isinstance(tid, str) and UUID_RE.fullmatch(tid):
                ids.append(tid)
        # Also scan the raw text in case the structure is different
        for m in UUID_RE.finditer(r.text):
            ids.append(m.group(0))
        return list(dict.fromkeys(ids))  # de-dupe
    except requests.RequestException as e:
        log(f"  fetch_task_list error: {type(e).__name__}: {e}")
        return []


def try_fetch_ground_truth_urls(task_id):
    """Try several URL variations to fetch ground truth URLs for a specific task.

    Returns: ({key: url_or_path, ...}, "via X") on success, (None, "reason") on failure.
    """
    # Try with task_id in different param positions
    candidates = [
        ("GET", f"{API_URLS}?task_id={task_id}", {}),
        ("GET", f"{API_URLS}?taskId={task_id}", {}),
        ("GET", f"{API_URLS}?id={task_id}", {}),
        ("GET", f"{API_URLS}/{task_id}", {}),
        ("GET", API_URLS, {"X-Task-Id": task_id}),
        ("GET", API_URLS, {"Cookie": f"selected_task={task_id}"}),
        ("GET", API_URLS, {}),  # bare, last resort
    ]
    for method, url, extra_headers in candidates:
        try:
            headers = {**REQUEST_HEADERS, **extra_headers}
            r = requests.get(url, headers=headers, timeout=30)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    continue
                if isinstance(data, dict) and data:
                    # Normalize the shape — could be {key: url} or {task_id: {key: url}}
                    return data, f"{method} {url}"
            # 404 with "not available yet" — record and move on
        except requests.RequestException:
            pass
    return None, "all candidates failed"


def download_file_via_proxy(url_or_path):
    """Download a single ground-truth file via the dashboard's proxy endpoint."""
    is_absolute = url_or_path.startswith(("http://", "https://"))
    param = "url" if is_absolute else "path"
    proxy_url = f"{API_FILE}?{param}={requests.utils.quote(url_or_path, safe='')}"
    r = requests.get(proxy_url, headers=REQUEST_HEADERS, timeout=120, stream=True)
    if r.status_code != 200:
        return None, f"http {r.status_code}"
    content = b"".join(r.iter_content(chunk_size=65536))
    return content, "ok"


def save_task_files(task_id, files_dict, scratch_dir):
    """Save the dict {key: url_or_path} as individual files in scratch_dir.

    Maps keys to expected filenames. Returns the directory path on success.
    """
    # Map from API response keys → expected filenames our collector wants
    key_to_filename = {
        "reads_1": "reads_1.fq",
        "reads_2": "reads_2.fq",
        "truth": "truth.vcf",
        "truth_vcf": "truth.vcf",
        "annotations": "annotations.json",
        "cftr2_annotations": "annotations.json",
    }

    task_dir = scratch_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for key, value in files_dict.items():
        # Skip nested structures
        if not isinstance(value, str):
            continue
        filename = key_to_filename.get(key.lower())
        if not filename:
            # Try to infer from value
            ext = os.path.splitext(value.split("?")[0])[1].lower()
            if ext in (".fq", ".fastq"):
                filename = "reads_1.fq" if "1" in key else "reads_2.fq" if "2" in key else None
            elif ext == ".vcf":
                filename = "truth.vcf"
            elif ext == ".json":
                filename = "annotations.json"
        if not filename:
            continue

        content, info = download_file_via_proxy(value)
        if content is None:
            log(f"    ✗ {key}: {info}")
            continue
        (task_dir / filename).write_bytes(content)
        saved_count += 1
        log(f"    ✓ {filename} ({len(content)//1024} KB)")
    return task_dir if saved_count >= 4 else None  # need all 4 files


def import_into_corpus(task_dir, task_id):
    result = subprocess.run(
        [sys.executable, str(COLLECTOR), str(task_dir), "--task-id", task_id, "--move"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        log(f"  ✓ imported {task_id}")
        return True
    log(f"  ✗ import failed: {result.stderr.strip()[:200]}")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="Single poll cycle and exit (for debugging)")
    args = parser.parse_args()

    DOWNLOAD_DIR.mkdir(exist_ok=True)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = load_state()

    # Migrate any prior permanent_404 entries back to the retry pool. Historical
    # tasks have proven downloadable hours/days after task creation, so we never
    # give up — we just slow down retries via per-task cooldown.
    migrated = state.pop("permanent_404", [])
    if migrated:
        log(f"Migrating {len(migrated)} previously-permanent_404 tasks back to retry pool")

    # last_attempt: {task_id: unix_ts of last fetch attempt}
    state.setdefault("last_attempt", {})
    state.setdefault("attempted", {})
    state.setdefault("succeeded", [])

    log("=== Ground truth watcher started ===")
    log(f"Already imported: {len(load_imported_task_ids())} task(s)")
    log(f"Tried previously: {len(state['attempted'])}, succeeded: {len(state['succeeded'])}")
    log(f"Retry cooldown: {TASK_RETRY_COOLDOWN}s per task; poll cadence: {POLL_TASKS_INTERVAL}s")

    def shutdown(sig, frame):
        log("Received signal, saving state and exiting")
        save_state(state)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    last_state_save = time.time()
    cycle = 0
    while True:
        cycle += 1
        log(f"--- cycle {cycle} ---")

        imported = load_imported_task_ids()
        task_ids = fetch_task_list()
        log(f"Dashboard shows {len(task_ids)} task ID(s), {len(imported)} already imported")

        # Pick candidates: visible on dashboard AND not yet imported AND
        # not retried within the cooldown window.
        now = time.time()
        eligible = [
            tid for tid in task_ids
            if tid not in imported
            and (now - state["last_attempt"].get(tid, 0)) >= TASK_RETRY_COOLDOWN
        ]
        skipped_cooldown = len([tid for tid in task_ids
                                if tid not in imported
                                and (now - state["last_attempt"].get(tid, 0)) < TASK_RETRY_COOLDOWN])
        log(f"  {len(eligible)} eligible to try, {skipped_cooldown} in cooldown")

        new_successes = 0
        for task_id in eligible:
            attempts = state["attempted"].get(task_id, 0) + 1
            state["attempted"][task_id] = attempts
            state["last_attempt"][task_id] = time.time()

            data, info = try_fetch_ground_truth_urls(task_id)
            if data is None:
                # Only log first 2 attempts and every 10th retry — avoid log spam
                if attempts <= 2 or attempts % 10 == 0:
                    log(f"  × {task_id} (try #{attempts}): {info}")
            else:
                log(f"  ✓ {task_id} (try #{attempts}): got URLs {list(data.keys())[:6]}")
                task_dir = save_task_files(task_id, data, DOWNLOAD_DIR)
                if task_dir and import_into_corpus(task_dir, task_id):
                    if task_id not in state["succeeded"]:
                        state["succeeded"].append(task_id)
                    new_successes += 1

            if time.time() - last_state_save > STATE_SAVE_INTERVAL:
                save_state(state)
                last_state_save = time.time()

            time.sleep(INTER_TASK_SLEEP)  # rate-limit API

        save_state(state)
        log(f"  Cycle {cycle} complete: +{new_successes} new ground truths")

        if args.once:
            log("--once flag set, exiting")
            return

        log(f"Sleeping {POLL_TASKS_INTERVAL}s before next cycle...")
        time.sleep(POLL_TASKS_INTERVAL)


if __name__ == "__main__":
    main()
