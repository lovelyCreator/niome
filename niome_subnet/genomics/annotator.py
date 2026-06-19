"""CFTR2 annotation lookup for called variants."""

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
                f"CFTR2 table not found at {self.table_path}."
            )
        with open(self.table_path) as f:
            self._table = json.load(f)
        self._loaded = True

    def annotate_vcf(self, vcf_text: str) -> Dict[str, Any]:
        """TEMP: dump the full table to discover validator's key format."""
        self._load()
        return dict(self._table)
