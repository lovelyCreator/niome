"""Analyze collected ground truths to understand validator simulation patterns.

Use after collecting 5+ ground truths via collect_ground_truth.py. Outputs:
  - VAF distribution (what % of variants are low/medium/high VAF)
  - Variant type breakdown (SNV vs INS vs DEL)
  - Position distribution (which CFTR regions are targeted)
  - Annotation patterns (clinical_significance values, drug_response distributions)
  - Read characteristics (coverage, error rate proxy)

Helps decide:
  - Whether DeepVariant custom-training is worth the effort
  - What our miss-rate is on different variant categories
  - Where to focus the variant calling tuning

Usage:
    python scripts/analyze_validator_patterns.py
"""

import json
import os
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

TRAINING_DIR = Path.home() / "niome_training"
GT_DIR = TRAINING_DIR / "ground_truths"
INDEX_FILE = GT_DIR / "index.json"


def get_truth_variants(task_dir):
    """Extract truth variants with VAF info from BAM."""
    truth_vcf = task_dir / "truth.vcf"
    variants = []
    if not truth_vcf.exists():
        return variants
    with open(truth_vcf) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) < 5:
                continue
            variants.append({
                "chrom": fields[0],
                "pos": int(fields[1]),
                "ref": fields[3],
                "alt": fields[4],
                "var_type": variant_type(fields[3], fields[4]),
            })
    return variants


def variant_type(ref, alt):
    if len(ref) == 1 and len(alt) == 1:
        return "SNV"
    if len(ref) > len(alt) and ref.startswith(alt):
        return "DEL"
    if len(alt) > len(ref) and alt.startswith(ref):
        return "INS"
    if len(ref) == len(alt):
        return "MNV"
    return "COMPLEX"


def compute_vaf_at_position(bam, ref_fasta, chrom, pos, ref, alt):
    """Use samtools mpileup to compute VAF at a specific variant position."""
    region = f"{chrom}:{pos}-{pos}"
    try:
        result = subprocess.run(
            ["samtools", "mpileup", "-r", region, "-f", ref_fasta, bam],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) < 5:
            continue
        depth = int(fields[3])
        bases = fields[4]
        if depth == 0:
            return 0.0
        # Crude VAF: count alt-supporting bases
        alt_count = 0
        if len(ref) == 1 and len(alt) == 1:
            # SNV
            alt_count = bases.upper().count(alt.upper())
        elif len(ref) > len(alt):
            # Deletion
            alt_count = bases.count("-")
        elif len(alt) > len(ref):
            # Insertion
            alt_count = bases.count("+")
        return alt_count / depth if depth > 0 else 0.0
    return None


def categorize_vaf(vaf):
    if vaf is None:
        return "unknown"
    if vaf < 0.05:
        return "<5%"
    if vaf < 0.15:
        return "5-15%"
    if vaf < 0.30:
        return "15-30%"
    if vaf < 0.55:
        return "30-55%"
    return ">55%"


def main():
    if not INDEX_FILE.exists():
        print("No ground truths collected yet. Run collect_ground_truth.py first.")
        return

    with open(INDEX_FILE) as f:
        index = json.load(f)

    tasks = index["tasks"]
    if not tasks:
        print("Index empty. Collect some ground truths first.")
        return

    print(f"Analyzing {len(tasks)} task(s)...\n")

    # Aggregate counters
    type_counts = Counter()
    clin_sig_counts = Counter()
    drug_resp_counts = defaultdict(Counter)
    vaf_buckets = Counter()
    positions = []
    per_task_summary = []

    for task_id, meta in tasks.items():
        task_dir = GT_DIR / task_id
        if not task_dir.exists():
            continue

        variants = get_truth_variants(task_dir)
        positions.extend(v["pos"] for v in variants)

        for v in variants:
            type_counts[v["var_type"]] += 1

        # Annotations
        ann_file = task_dir / "annotations.json"
        if ann_file.exists():
            with open(ann_file) as f:
                ann = json.load(f)
            for entry in ann.values():
                cs = entry.get("clinical_significance", "")
                clin_sig_counts[cs] += 1
                for drug, resp in entry.get("drug_response", {}).items():
                    drug_resp_counts[drug][resp] += 1

        # VAF analysis (slow — only do if BAM exists)
        # The validator's task includes input FASTQ. We can't easily compute VAF
        # without alignment. Skip detailed VAF here; will be added once we have
        # the per-task aligned BAM cached.
        per_task_summary.append({
            "task_id": task_id,
            "variants": len(variants),
            "snv": sum(1 for v in variants if v["var_type"] == "SNV"),
            "ins": sum(1 for v in variants if v["var_type"] == "INS"),
            "del": sum(1 for v in variants if v["var_type"] == "DEL"),
        })

    # --- Report ---
    print(f"=== Variant Type Distribution (across {sum(type_counts.values())} variants) ===")
    total = sum(type_counts.values())
    for vtype, count in type_counts.most_common():
        pct = 100 * count / total if total else 0
        print(f"  {vtype:8s}: {count:4d} ({pct:.1f}%)")

    print(f"\n=== Clinical Significance Distribution ===")
    total = sum(clin_sig_counts.values())
    for cs, count in clin_sig_counts.most_common(10):
        pct = 100 * count / total if total else 0
        print(f"  {cs:40s}: {count:4d} ({pct:.1f}%)")

    print(f"\n=== Drug Response Distribution ===")
    for drug in sorted(drug_resp_counts):
        print(f"  {drug}:")
        for resp, count in drug_resp_counts[drug].most_common():
            print(f"    {resp:25s}: {count}")

    if positions:
        print(f"\n=== Position Range ===")
        print(f"  Min: {min(positions)}")
        print(f"  Max: {max(positions)}")
        print(f"  Span: {max(positions) - min(positions)} bp")

    print(f"\n=== Per-Task Summary ===")
    print(f"  {'task_id':40s}  variants  SNV  INS  DEL")
    for s in per_task_summary[:20]:
        print(f"  {s['task_id']:40s}  {s['variants']:8d}  {s['snv']:3d}  {s['ins']:3d}  {s['del']:3d}")
    if len(per_task_summary) > 20:
        print(f"  ... and {len(per_task_summary) - 20} more tasks")

    # Recommendations
    print("\n=== Recommendations ===")
    total_var = sum(type_counts.values())
    if total_var:
        snv_pct = 100 * type_counts["SNV"] / total_var
        indel_pct = 100 * (type_counts["INS"] + type_counts["DEL"]) / total_var
        print(f"  SNV/INDEL ratio: {snv_pct:.0f}% / {indel_pct:.0f}%")
        if indel_pct > 25:
            print("  → High indel content: custom training would help significantly")
        else:
            print("  → SNV-dominant: tuning thresholds may be enough")

    # Save analysis
    output = TRAINING_DIR / "analysis" / "patterns.json"
    output.parent.mkdir(exist_ok=True)
    with open(output, "w") as f:
        json.dump({
            "num_tasks": len(tasks),
            "type_counts": dict(type_counts),
            "clinical_significance": dict(clin_sig_counts),
            "drug_response": {k: dict(v) for k, v in drug_resp_counts.items()},
            "position_range": [min(positions), max(positions)] if positions else None,
            "per_task": per_task_summary,
        }, f, indent=2, sort_keys=True)
    print(f"\nSaved analysis to {output}")


if __name__ == "__main__":
    main()
