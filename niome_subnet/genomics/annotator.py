"""CFTR2 annotation lookup for called variants.

The validator scores annotations against a `cftr2_annotations.json` keyed by
rsid (see `niome_subnet/genomics/scoring.py::score_annotations`). The miner
must produce the same shape.

Build the lookup table once from:
  - cftr2.org (variant list -> hgvs, clinical significance)
  - PharmGKB / CPIC CFTR guideline (drug response per variant, per drug)

Expected schema written to NIOME_CFTR2_TABLE (default: data/cftr2_table.json):

    {
      "rs<id>": {
        "hgvs": "NM_000492.4:c.1521_1523delCTT",
        "clinical_significance": "CF-causing",
        "drug_response": {
          "ivacaftor": "...",
          "tezacaftor_ivacaftor": "...",
          "elexacaftor_tezacaftor_ivacaftor": "...",
          "lumacaftor_ivacaftor": "..."
        }
      },
      ...
    }
"""

import json
import os
from typing import Any, Dict


DEFAULT_TABLE_PATH = os.environ.get(
    "NIOME_CFTR2_TABLE", os.path.join("data", "cftr2_table.json")
)


class CFTR2Annotator:
    def __init__(self, table_path: str = DEFAULT_TABLE_PATH):
        self.table_path = table_path
        self._table: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if not os.path.exists(self.table_path):
            raise FileNotFoundError(
                f"CFTR2 table not found at {self.table_path}. "
                f"Populate it from cftr2.org + CPIC; see module docstring."
            )
        with open(self.table_path) as f:
            self._table = json.load(f)
        self._loaded = True

    def annotate_vcf(self, vcf_text: str) -> Dict[str, Any]:
        """Annotate only the variants actually called.

        Submitting the entire CFTR2 catalog tanks the score — the validator
        divides by max(truth_count, miner_count) (scoring.py:210).
        """
        self._load()
        out: Dict[str, Any] = {}
        for line in vcf_text.splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            for rsid in fields[2].split(";"):
                rsid = rsid.strip()
                entry = self._table.get(rsid)
                if entry is not None:
                    out[rsid] = entry
        return out
