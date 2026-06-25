"""Organize downloaded ground truth files into a training dataset.

Usage:
    # After downloading a ground truth zip/files from the dashboard:
    python scripts/collect_ground_truth.py <downloaded_dir_or_zip> [--task-id TASK_ID]

The script:
  - Extracts/locates: reads_1.fq, reads_2.fq, truth.vcf, annotations.json
  - Verifies all 4 files are present and valid
  - Organizes into ~/niome_training/ground_truths/<task_id>/
  - Records metadata in ~/niome_training/ground_truths/index.json
  - Computes quick statistics (variant count, VAF distribution)
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

REQUIRED_FILES = ["reads_1.fq", "reads_2.fq", "truth.vcf", "annotations.json"]
TRAINING_DIR = Path.home() / "niome_training"
GT_DIR = TRAINING_DIR / "ground_truths"
INDEX_FILE = GT_DIR / "index.json"


def load_index():
    if not INDEX_FILE.exists():
        return {"tasks": {}, "by_truth_hash": {}}
    with open(INDEX_FILE) as f:
        idx = json.load(f)
    idx.setdefault("by_truth_hash", {})
    return idx


def save_index(index):
    GT_DIR.mkdir(parents=True, exist_ok=True)
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2, sort_keys=True)


def file_hash(path):
    """SHA1 of a file's contents — short enough for dedup key, no collisions in practice."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def find_required_files(source_dir):
    """Locate all 4 required files in source_dir (handles nested layouts)."""
    found = {}
    for required in REQUIRED_FILES:
        matches = list(Path(source_dir).rglob(required))
        if not matches:
            return None, f"missing: {required}"
        found[required] = matches[0]
    return found, None


def extract_if_zip(source):
    """If source is a zip, extract to a temp dir and return that dir."""
    src = Path(source)
    if src.is_file() and src.suffix.lower() == ".zip":
        extract_dir = src.parent / f".extracted_{src.stem}"
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            zf.extractall(extract_dir)
        return extract_dir
    return src


def analyze_ground_truth(task_dir):
    """Quick statistics on the ground truth."""
    stats = {}

    # Truth VCF: count variants by type
    truth_vcf = task_dir / "truth.vcf"
    if truth_vcf.exists():
        snv = ins = del_ = mnv = 0
        positions = []
        with open(truth_vcf) as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                fields = line.split("\t")
                if len(fields) < 5:
                    continue
                ref, alt = fields[3], fields[4]
                positions.append(int(fields[1]))
                if len(ref) == 1 and len(alt) == 1:
                    snv += 1
                elif len(ref) > len(alt):
                    del_ += 1
                elif len(alt) > len(ref):
                    ins += 1
                else:
                    mnv += 1
        stats["truth_variants"] = snv + ins + del_ + mnv
        stats["truth_snv"] = snv
        stats["truth_ins"] = ins
        stats["truth_del"] = del_
        stats["truth_mnv"] = mnv
        if positions:
            stats["position_min"] = min(positions)
            stats["position_max"] = max(positions)

    # Annotations: how many entries
    ann_file = task_dir / "annotations.json"
    if ann_file.exists():
        with open(ann_file) as f:
            ann = json.load(f)
        stats["annotation_entries"] = len(ann)
        if ann:
            sample = next(iter(ann.values()))
            stats["sample_hgvs"] = sample.get("hgvs", "")
            stats["sample_clinical_significance"] = sample.get("clinical_significance", "")

    # Read counts
    for read_file in ["reads_1.fq", "reads_2.fq"]:
        rp = task_dir / read_file
        if rp.exists():
            # FASTQ: 4 lines per read
            with open(rp) as f:
                lines = sum(1 for _ in f)
            stats[f"{read_file}_reads"] = lines // 4

    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Downloaded directory or zip with ground truth")
    parser.add_argument("--task-id", default=None,
                        help="Task ID (UUID from dashboard). If omitted, uses source basename.")
    parser.add_argument("--move", action="store_true",
                        help="Move files instead of copying (saves disk)")
    args = parser.parse_args()

    src = Path(args.source)
    if not src.exists():
        sys.exit(f"Source not found: {src}")

    source_dir = extract_if_zip(src)
    found, err = find_required_files(source_dir)
    if err:
        sys.exit(f"Source missing required files ({err}). Expected: {REQUIRED_FILES}")

    task_id = args.task_id or src.stem

    # Deduplicate by truth hash. Validator recycles the same simulation across
    # many task_ids; only the truth file content is what matters for training.
    truth_hash = file_hash(found["truth.vcf"])

    index = load_index()

    if truth_hash in index["by_truth_hash"]:
        # Duplicate simulation — record the new task_id alias but don't re-copy files
        existing_task_id = index["by_truth_hash"][truth_hash]
        index["tasks"].setdefault(existing_task_id, {}).setdefault("aliases", [])
        if task_id not in index["tasks"][existing_task_id]["aliases"]:
            index["tasks"][existing_task_id]["aliases"].append(task_id)
        save_index(index)
        print(f"DUPLICATE: truth_hash={truth_hash} already exists at task {existing_task_id}")
        print(f"  Recorded {task_id} as alias. Skipping file copy.")
        # Clean up the temp dir to save space
        if args.move:
            try:
                shutil.rmtree(source_dir)
            except Exception:
                pass
        print(f"Unique simulations: {len(index['by_truth_hash'])} | Total tasks seen: "
              f"{sum(1 + len(t.get('aliases', [])) for t in index['tasks'].values())}")
        return

    # New unique simulation — store it
    target_dir = GT_DIR / task_id
    if target_dir.exists():
        print(f"Warning: {target_dir} already exists, will overwrite")
    target_dir.mkdir(parents=True, exist_ok=True)

    op = shutil.move if args.move else shutil.copy2
    for req, path in found.items():
        op(str(path), str(target_dir / req))

    stats = analyze_ground_truth(target_dir)
    stats["task_id"] = task_id
    stats["truth_hash"] = truth_hash
    stats["aliases"] = []

    index["tasks"][task_id] = stats
    index["by_truth_hash"][truth_hash] = task_id
    save_index(index)

    print(f"NEW UNIQUE: {target_dir} (truth_hash={truth_hash})")
    print(f"Stats: {json.dumps(stats, indent=2)}")
    print(f"Unique simulations: {len(index['by_truth_hash'])}")


if __name__ == "__main__":
    main()
