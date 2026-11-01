#!/usr/bin/env python3
"""Invariant 5 for candidate primers: no harness test file and no tuner file may have more than
MAX_OVERLAP (20%) of its bytes inside 64-byte strings of the primer of its kind.

  check.py --primers DIR [--kinds py,c]
The primer is checked in 8 MB parts and each file's covered bytes are combined (a 32 MB primer at once
needs about 1 GB); windows across a part boundary (63 per boundary) are not counted.
"""
import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(TOOLS, "harness"))
sys.path.insert(0, os.path.join(TOOLS, "tuner"))
import data  # noqa: E402
import fetch  # noqa: E402
import overlap  # noqa: E402

CHUNK = 4 << 20
PART = 8 << 20


def index(primer: bytes) -> np.ndarray:
    k = overlap.K
    parts = [np.unique(overlap.kgram_hashes(primer[i: i + CHUNK + k - 1], k)) for i in range(0, len(primer), CHUNK)]
    return np.unique(np.concatenate(parts)) if parts else np.zeros(0, dtype=np.uint64)


def covered(data_: bytes, idx: np.ndarray) -> np.ndarray:
    """Which bytes of data_ lie inside a 64-byte string whose hash is in idx."""
    k = overlap.K
    h = overlap.kgram_hashes(data_, k)
    if not len(h) or not len(idx):
        return np.zeros(len(data_), dtype=bool)
    pos = np.searchsorted(idx, h)
    pos[pos == len(idx)] = 0
    hit = (idx[pos] == h).astype(np.int64)
    c = np.concatenate(([0], np.cumsum(hit)))
    j = np.arange(len(data_))
    lo = np.maximum(j - k + 1, 0)
    hi = np.minimum(j, len(h) - 1) + 1
    return (c[np.maximum(hi, lo)] - c[lo]) > 0


def protected_files(kind: str):
    """(set name, path) of every harness and tuner file of the kind."""
    with open(fetch.SETS) as f:
        for e in json.load(f)["kinds"].get(kind, {}).get("files", []):
            yield "harness", fetch.file_path(e["source"], e["path"])
    with open(data.SETS) as f:
        for sp, ks in json.load(f)["kinds"].get(kind, {}).items():
            for e in ks["files"]:
                yield "tuner-" + sp, fetch.file_path(e["source"], e["path"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--primers", required=True)
    ap.add_argument("--kinds", default="py,c,js,json,web,prose,man")
    args = ap.parse_args()
    fetch.ensure("full")
    data.ensure()
    bad = 0
    for kind in args.kinds.split(","):
        p = os.path.join(args.primers, kind + ".bin")
        if not os.path.exists(p):
            continue
        with open(p, "rb") as f:
            primer = f.read()
        files = list(protected_files(kind))
        views = {}
        for _, path in files:
            with open(path, "rb") as f:
                views[path] = overlap.model_view(kind, f.read())
        masks = {path: np.zeros(len(v), dtype=bool) for path, v in views.items()}
        for i in range(0, len(primer), PART):  # the primer in parts, so its index stays small
            idx = index(primer[i: i + PART + overlap.K - 1])
            for path, v in views.items():
                masks[path] |= covered(v, idx)
            del idx
        worst, over, n = (0.0, ""), [], 0
        for set_name, path in files:
            v = float(masks[path].mean()) if len(masks[path]) else 0.0
            n += 1
            worst = max(worst, (v, f"{set_name} {path}"))
            if v > overlap.MAX_OVERLAP:
                over.append((round(v, 3), set_name, path))
        bad += len(over)
        print(f"{kind:6s} {n:5d} files checked; highest overlap {worst[0]:.1%} ({worst[1].split('/sets/')[-1]}); "
              f"{len(over)} above {overlap.MAX_OVERLAP:.0%}", flush=True)
        for o in over[:10]:
            print("    ", o)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
