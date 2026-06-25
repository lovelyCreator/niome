"""ClinVar VariationID annotation lookup for called variants."""

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
            raise FileNotFoundError(f"CFTR2 table not found at {self.table_path}")
        with open(self.table_path) as f:
            self._table = json.load(f)
        self._loaded = True

    def annotate_vcf(self, vcf_text: str) -> Dict[str, Any]:
        """Look up VCF ID column entries. Handles both 'rs<num>' and pure numeric IDs."""
        self._load()
        out: Dict[str, Any] = {}
        for line in vcf_text.splitlines():
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 3:
                continue
            for raw_id in fields[2].split(";"):
                raw_id = raw_id.strip()
                if not raw_id or raw_id == ".":
                    continue
                # Try direct match (ClinVar VariationID) or stripped rs prefix
                keys_to_try = [raw_id]
                if raw_id.startswith("rs"):
                    keys_to_try.append(raw_id[2:])
                for key in keys_to_try:
                    if key in self._table:
                        out[key] = self._table[key]
                        break
        return out
