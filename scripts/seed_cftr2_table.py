"""Write a small, curated CFTR2 annotation seed table.

Covers ~15 of the best-documented CFTR variants. Values are drawn from:
  - cftr2.org official variant list (clinical significance)
  - FDA labels for ivacaftor, lumacaftor/ivacaftor, tezacaftor/ivacaftor,
    elexacaftor/tezacaftor/ivacaftor (drug responsiveness)
  - CPIC CFTR guideline

The convention used here for drug_response strings:
    "responsive" | "not_responsive" | "indeterminate"

IMPORTANT: the validator scores with exact-string equality
(scoring.py:194-204). If the validator uses different capitalization or
phrasing in its truth table, every annotation you submit will mismatch.
Run a few tasks and inspect the score logs (precision/recall on annotations
specifically) before trusting the seed; adjust strings to match if needed.

Run:
    python scripts/seed_cftr2_table.py            # writes data/cftr2_table.json
    python scripts/seed_cftr2_table.py --out X    # custom path
"""

import argparse
import json
import os


SEED = {
    # F508del (delF508) - most common CF variant
    "rs113993960": {
        "hgvs": "NM_000492.4:c.1521_1523delCTT",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "indeterminate",
            "tezacaftor_ivacaftor": "responsive",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "responsive",
        },
    },
    # G551D - classic ivacaftor-responsive gating mutation
    "rs75527207": {
        "hgvs": "NM_000492.4:c.1652G>A",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "responsive",
            "tezacaftor_ivacaftor": "responsive",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "responsive",
        },
    },
    # G542X - premature stop, no modulator response
    "rs113993959": {
        "hgvs": "NM_000492.4:c.1624G>T",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # W1282X - premature stop
    "rs77010898": {
        "hgvs": "NM_000492.4:c.3846G>A",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # N1303K - CF-causing; recent data suggests partial ETI response
    "rs80034486": {
        "hgvs": "NM_000492.4:c.3909C>G",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "indeterminate",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # R117H - variable severity; ivacaftor-responsive
    "rs78655421": {
        "hgvs": "NM_000492.4:c.350G>A",
        "clinical_significance": "varying clinical consequences",
        "drug_response": {
            "ivacaftor": "responsive",
            "tezacaftor_ivacaftor": "responsive",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "indeterminate",
        },
    },
    # R553X - premature stop
    "rs74597325": {
        "hgvs": "NM_000492.4:c.1657C>T",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # R1162X - premature stop
    "rs74767530": {
        "hgvs": "NM_000492.4:c.3484C>T",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # A455E - residual function variant
    "rs74551128": {
        "hgvs": "NM_000492.4:c.1364C>A",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "responsive",
            "tezacaftor_ivacaftor": "responsive",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "indeterminate",
        },
    },
    # 2789+5G>A - splicing, partial residual function
    "rs80224560": {
        "hgvs": "NM_000492.4:c.2657+5G>A",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "responsive",
            "tezacaftor_ivacaftor": "responsive",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "indeterminate",
        },
    },
    # 3849+10kbC>T - splicing
    "rs75039782": {
        "hgvs": "NM_000492.4:c.3717+12191C>T",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "responsive",
            "tezacaftor_ivacaftor": "responsive",
            "elexacaftor_tezacaftor_ivacaftor": "responsive",
            "lumacaftor_ivacaftor": "indeterminate",
        },
    },
    # 1717-1G>A - splicing
    "rs76713772": {
        "hgvs": "NM_000492.4:c.1585-1G>A",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # 621+1G>T - splicing
    "rs78756941": {
        "hgvs": "NM_000492.4:c.489+1G>T",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # 711+1G>T - splicing
    "rs77188391": {
        "hgvs": "NM_000492.4:c.579+1G>T",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
    # 3120+1G>A - splicing
    "rs75096551": {
        "hgvs": "NM_000492.4:c.2988+1G>A",
        "clinical_significance": "CF-causing",
        "drug_response": {
            "ivacaftor": "not_responsive",
            "tezacaftor_ivacaftor": "not_responsive",
            "elexacaftor_tezacaftor_ivacaftor": "not_responsive",
            "lumacaftor_ivacaftor": "not_responsive",
        },
    },
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join("data", "cftr2_table.json"))
    parser.add_argument("--merge", action="store_true",
                        help="merge into existing file instead of overwriting")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    table = {}
    if args.merge and os.path.exists(args.out):
        with open(args.out) as f:
            table = json.load(f)
    table.update(SEED)

    with open(args.out, "w") as f:
        json.dump(table, f, indent=2, sort_keys=True)

    print(f"Wrote {len(table)} variants to {args.out}")


if __name__ == "__main__":
    main()
