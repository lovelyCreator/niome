"""Clean up existing duplicate ground truth directories.

The validator recycles the same simulation across many task_ids. After
running the watcher for a while we have many duplicates eating disk space.

This script:
  - Hashes truth.vcf in each ground_truth dir
  - Keeps the FIRST occurrence of each unique hash (alphabetical task_id)
  - Removes all duplicate directories
  - Updates index.json to record removed task_ids as aliases of the kept one

Run this AFTER updating to the new collect_ground_truth.py — the watcher
will use the new dedup logic going forward, and this script cleans the past.

Usage:
    python scripts/dedupe_ground_truths.py            # dry run (lists what would be removed)
    python scripts/dedupe_ground_truths.py --apply    # actually delete duplicates
"""

import argparse
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

TRAINING_DIR = Path.home() / "niome_training"
GT_DIR = TRAINING_DIR / "ground_truths"
INDEX_FILE = GT_DIR / "index.json"


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually delete duplicates")
    args = parser.parse_args()

    if not GT_DIR.exists():
        sys.exit(f"{GT_DIR} does not exist")

    # Hash every truth.vcf
    print("Hashing truth.vcf files...")
    by_hash = defaultdict(list)
    task_dirs = sorted([d for d in GT_DIR.iterdir() if d.is_dir()])
    for d in task_dirs:
        truth = d / "truth.vcf"
        if not truth.exists():
            print(f"  ! {d.name}: no truth.vcf, skipping")
            continue
        try:
            h = file_hash(truth)
            by_hash[h].append(d.name)
        except Exception as e:
            print(f"  ! {d.name}: hash failed ({e})")

    # Plan: keep first task_id per hash, remove others
    keepers = {}
    removals = []
    total_size_to_free = 0
    for h, task_ids in by_hash.items():
        keepers[h] = task_ids[0]
        for tid in task_ids[1:]:
            d = GT_DIR / tid
            try:
                size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            except Exception:
                size = 0
            total_size_to_free += size
            removals.append((tid, h, size))

    print(f"\nFound {len(by_hash)} unique simulation(s) across {len(task_dirs)} task dirs")
    print(f"Will keep: {len(keepers)} dirs (one per unique simulation)")
    print(f"Will remove: {len(removals)} duplicate dirs")
    print(f"Disk space to free: {total_size_to_free / 1024 / 1024:.1f} MB")

    print("\nUnique simulations:")
    for h, tid in keepers.items():
        n_aliases = len(by_hash[h]) - 1
        print(f"  {h}  kept={tid}  +{n_aliases} aliases")

    if not args.apply:
        print("\n(dry run — re-run with --apply to actually delete)")
        return

    # Apply: update index, then delete dirs
    print(f"\n{'='*50}")
    print(f"APPLYING — removing {len(removals)} duplicate dirs")
    print(f"{'='*50}")

    # Update index
    if INDEX_FILE.exists():
        with open(INDEX_FILE) as f:
            index = json.load(f)
    else:
        index = {"tasks": {}}
    index.setdefault("by_truth_hash", {})

    new_tasks = {}
    for h, tid in keepers.items():
        # Build alias list = all other task_ids that shared this hash
        aliases = [t for t in by_hash[h] if t != tid]
        prev = index["tasks"].get(tid, {})
        prev_aliases = prev.get("aliases", [])
        merged_aliases = sorted(set(prev_aliases) | set(aliases))
        prev["task_id"] = tid
        prev["truth_hash"] = h
        prev["aliases"] = merged_aliases
        new_tasks[tid] = prev
        index["by_truth_hash"][h] = tid

    index["tasks"] = new_tasks
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)
    print(f"Updated index.json")

    # Delete duplicate dirs
    removed_count = 0
    for tid, h, size in removals:
        d = GT_DIR / tid
        try:
            shutil.rmtree(d)
            removed_count += 1
        except Exception as e:
            print(f"  ! failed to remove {tid}: {e}")
    print(f"Removed {removed_count} dirs ({total_size_to_free / 1024 / 1024:.1f} MB freed)")

    # Final summary
    remaining = [d for d in GT_DIR.iterdir() if d.is_dir()]
    print(f"\nDone. {len(remaining)} unique simulation dirs remain on disk.")


if __name__ == "__main__":
    main()
