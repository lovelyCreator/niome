"""Build a complete data/cftr2_table.json by joining CFTR2 + CPIC inputs.

==============================================================================
INPUTS YOU MUST DOWNLOAD MANUALLY (terms of use prohibit auto-scraping)
==============================================================================

1. CFTR2 variant list  ->  data/inputs/cftr2_variant_list.csv
   Source: https://cftr2.org  (Variant List History page; download CSV)
   Required columns (rename if different):
       - rsID                       (e.g. "rs113993960")
       - cDNA_name      OR hgvs     (e.g. "c.1521_1523delCTT")
       - Variant_legacy_name OR protein_name  (optional, for human readability)
       - Variant_final_determination  ("CF-causing", "non CF-causing",
         "varying clinical consequences", "indeterminate")

2. CPIC ivacaftor allele table  ->  data/inputs/cpic_ivacaftor.csv
   Source: https://files.cpicpgx.org/data/  (look for "CFTR_*Allele*.xlsx";
   export Sheet1 as CSV). Required columns:
       - Allele Name OR rsID
       - Function on Ivacaftor   (one of: "responsive", "not responsive",
         "indeterminate", or similar — script normalizes)

3. Optional, for the modulator combos (lumacaftor/ivacaftor, tezacaftor/
   ivacaftor, elexacaftor/tezacaftor/ivacaftor): each drug's FDA label
   appendix lists responsive variants. Compile each into a CSV with columns
   [rsID, response] and pass via --combo-csv:
       data/inputs/lumacaftor_ivacaftor.csv
       data/inputs/tezacaftor_ivacaftor.csv
       data/inputs/elexacaftor_tezacaftor_ivacaftor.csv

==============================================================================
USAGE
==============================================================================

    pip install pandas

    python scripts/build_cftr2_table.py \\
        --cftr2 data/inputs/cftr2_variant_list.csv \\
        --ivacaftor data/inputs/cpic_ivacaftor.csv \\
        --combo lumacaftor_ivacaftor=data/inputs/lumacaftor_ivacaftor.csv \\
        --combo tezacaftor_ivacaftor=data/inputs/tezacaftor_ivacaftor.csv \\
        --combo elexacaftor_tezacaftor_ivacaftor=data/inputs/elexacaftor_tezacaftor_ivacaftor.csv \\
        --seed \\
        --out data/cftr2_table.json

`--seed` first writes the curated baseline from scripts/seed_cftr2_table.py,
then layers the external CSVs on top — anything not found in the CSVs keeps
the seed values.
"""

import argparse
import json
import os
import re
import sys
from typing import Optional

try:
    import pandas as pd
except ImportError:
    sys.stderr.write("This script needs pandas. Run: pip install pandas\n")
    sys.exit(1)


CFTR_DRUGS = [
    "ivacaftor",
    "tezacaftor_ivacaftor",
    "elexacaftor_tezacaftor_ivacaftor",
    "lumacaftor_ivacaftor",
]


def _normalize_response(value: Optional[str]) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "indeterminate"
    s = str(value).strip().lower()
    if s in ("responsive", "yes", "y", "true", "1"):
        return "responsive"
    if s in ("not responsive", "not_responsive", "no", "n", "false", "0",
             "non-responsive", "non responsive"):
        return "not_responsive"
    if "respons" in s and "not" not in s and "non" not in s:
        return "responsive"
    if "not" in s and "respons" in s:
        return "not_responsive"
    return "indeterminate"


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _extract_rsid(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    match = re.search(r"rs\d+", str(value))
    return match.group(0) if match else None


def load_cftr2(path: str) -> dict:
    df = pd.read_csv(path)
    rsid_col = _pick_col(df, ["rsID", "rsid", "rs_id", "dbSNP"])
    hgvs_col = _pick_col(df, ["cDNA_name", "cdna_name", "hgvs", "HGVS", "c_name"])
    sig_col = _pick_col(df, [
        "Variant_final_determination", "final_determination",
        "clinical_significance", "significance",
    ])
    if not rsid_col:
        raise SystemExit(f"CFTR2 CSV needs an rsID column. Got: {list(df.columns)}")
    out = {}
    for _, row in df.iterrows():
        rsid = _extract_rsid(row[rsid_col])
        if not rsid:
            continue
        out[rsid] = {
            "hgvs": str(row[hgvs_col]) if hgvs_col and pd.notna(row[hgvs_col]) else "",
            "clinical_significance": (
                str(row[sig_col]) if sig_col and pd.notna(row[sig_col])
                else "indeterminate"
            ),
            "drug_response": {drug: "indeterminate" for drug in CFTR_DRUGS},
        }
    return out


def load_drug_csv(path: str) -> dict[str, str]:
    df = pd.read_csv(path)
    rsid_col = _pick_col(df, ["rsID", "rsid", "rs_id", "Allele Name", "allele"])
    resp_col = _pick_col(df, [
        "response", "Function on Ivacaftor", "drug_response",
        "responsive", "label",
    ])
    if not rsid_col or not resp_col:
        raise SystemExit(
            f"Drug CSV needs rsID + response columns. Got: {list(df.columns)}"
        )
    out = {}
    for _, row in df.iterrows():
        rsid = _extract_rsid(row[rsid_col])
        if rsid:
            out[rsid] = _normalize_response(row[resp_col])
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cftr2", required=True,
                        help="CFTR2 variant list CSV")
    parser.add_argument("--ivacaftor",
                        help="CPIC ivacaftor allele table CSV")
    parser.add_argument("--combo", action="append", default=[],
                        help="drug=csv_path (repeatable). drug in: " +
                             ",".join(CFTR_DRUGS[1:]))
    parser.add_argument("--seed", action="store_true",
                        help="start from scripts/seed_cftr2_table.py baseline")
    parser.add_argument("--out", default=os.path.join("data", "cftr2_table.json"))
    args = parser.parse_args()

    table = {}
    if args.seed:
        from seed_cftr2_table import SEED  # type: ignore
        table.update({k: {**v, "drug_response": dict(v["drug_response"])}
                      for k, v in SEED.items()})

    cftr2 = load_cftr2(args.cftr2)
    for rsid, entry in cftr2.items():
        if rsid in table:
            if not table[rsid].get("hgvs"):
                table[rsid]["hgvs"] = entry["hgvs"]
            table[rsid]["clinical_significance"] = entry["clinical_significance"]
        else:
            table[rsid] = entry

    if args.ivacaftor:
        iva = load_drug_csv(args.ivacaftor)
        for rsid, resp in iva.items():
            if rsid not in table:
                table[rsid] = {
                    "hgvs": "",
                    "clinical_significance": "indeterminate",
                    "drug_response": {d: "indeterminate" for d in CFTR_DRUGS},
                }
            table[rsid]["drug_response"]["ivacaftor"] = resp

    for spec in args.combo:
        if "=" not in spec:
            raise SystemExit(f"--combo must be drug=path, got {spec!r}")
        drug, path = spec.split("=", 1)
        if drug not in CFTR_DRUGS:
            raise SystemExit(f"Unknown drug {drug!r}. Must be one of {CFTR_DRUGS}")
        for rsid, resp in load_drug_csv(path).items():
            if rsid not in table:
                table[rsid] = {
                    "hgvs": "",
                    "clinical_significance": "indeterminate",
                    "drug_response": {d: "indeterminate" for d in CFTR_DRUGS},
                }
            table[rsid]["drug_response"][drug] = resp

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(table, f, indent=2, sort_keys=True)

    print(f"Wrote {len(table)} variants to {args.out}")
    fully_specified = sum(
        1 for e in table.values()
        if all(v != "indeterminate" for v in e["drug_response"].values())
    )
    print(f"  {fully_specified} have all 4 drug responses fully specified")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
