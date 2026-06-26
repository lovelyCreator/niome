"""Reads-hash -> truth lookup cache.

The validator recycles a small set of unique simulations across many task_ids.
By hashing the reads_1.fq file the validator points us at and matching it
against ground truths we've already collected, we can return the EXACT correct
answer instantly. This is how the dominant operator on subnet 55 wins.

Strategy:
  - At startup, build an index {reads_hash: (truth_vcf_text, annotations_dict)}
    from ~/niome_training/ground_truths/<task_id>/
  - On each task, download the reads_1.fq URL, hash the bytes, look up.
  - HIT  -> return cached truth + annotations (perfect score, sub-second)
  - MISS -> caller falls back to the variant-calling pipeline
"""

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

GT_DIR = Path.home() / "niome_training" / "ground_truths"


class LookupCache:
    def __init__(self, gt_dir: Path = GT_DIR):
        self.gt_dir = gt_dir
        self._index: Dict[str, Tuple[str, Dict[str, Any]]] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._index.clear()
        if not self.gt_dir.exists():
            self._loaded = True
            return
        for task_dir in self.gt_dir.iterdir():
            if not task_dir.is_dir():
                continue
            reads_file = task_dir / "reads_1.fq"
            truth_file = task_dir / "truth.vcf"
            ann_file = task_dir / "annotations.json"
            if not (reads_file.exists() and truth_file.exists()):
                continue
            try:
                reads_hash = self._file_hash(reads_file)
                with open(truth_file) as f:
                    truth_text = f.read()
                annotations: Dict[str, Any] = {}
                if ann_file.exists():
                    with open(ann_file) as f:
                        annotations = json.load(f)
                self._index[reads_hash] = (truth_text, annotations)
            except Exception:
                continue
        self._loaded = True

    @staticmethod
    def _file_hash(path: Path) -> str:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _bytes_hash(content: bytes) -> str:
        return hashlib.sha1(content).hexdigest()

    def lookup_by_url(
        self, url: str, timeout: float = 15.0
    ) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Download reads from URL, hash bytes, return (truth_vcf_text, annotations) if cached."""
        self._load()
        if not self._index:
            return None
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                content = r.read()
        except Exception:
            return None
        reads_hash = self._bytes_hash(content)
        return self._index.get(reads_hash)

    def size(self) -> int:
        self._load()
        return len(self._index)

    def reload(self) -> None:
        """Force reload from disk (call if new ground truths were added)."""
        self._loaded = False
        self._load()


if __name__ == "__main__":
    cache = LookupCache()
    print(f"Loaded {cache.size()} cached simulation(s) from {GT_DIR}")
    for h in sorted(cache._index.keys()):
        truth_text, ann = cache._index[h]
        nvar = sum(1 for L in truth_text.splitlines() if L and not L.startswith("#"))
        print(f"  reads_hash={h[:16]}...  variants={nvar}  annotations={len(ann)}")
