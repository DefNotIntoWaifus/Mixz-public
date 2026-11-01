#!/usr/bin/env python3
"""Primers from the public-domain corpus (plan step 7), with bounded memory.

The cover method of tools/build_primers.py: split the training files into 1 KB segments and
greedily pick the segments whose 8-byte substrings occur in the most files, counting a substring
only once it is new. That version kept every substring in Python sets (tens of GB for an 8 MB
primer). Here a substring is hashed to one of 2^HBITS cells: a byte array holds the number of files
it occurs in (saturating at 255) and a boolean array marks the covered ones, 2 * 2^HBITS bytes in
all (128 MB at HBITS 26). A segment's score is recomputed from its bytes when it is popped (lazy
greedy), so nothing per segment is stored but its score and place. Collisions make a few scores
slightly high; the order of the large ones does not change.

The most valuable segments come last, freshest in the model when a file starts, so the last N
bytes of a primer are the primer of budget N.

  build.py --kinds py,c --budget 33554432 --out DIR     writes DIR/KIND.bin
"""
import argparse
import heapq
import os
import random
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import corpus  # noqa: E402

HBITS = 26  # 2 * 64 MB of tables; ranking of the large scores is unaffected by the extra collisions
SEG = 1024
DLEN = 8
POOL = 256 << 20  # at most this much training data per kind (a seeded sample of files beyond it)
PER_SOURCE = 4 << 20  # at most this much of one kind from one source


def hashes(b: bytes) -> np.ndarray:
    """The distinct cells of b's 8-byte substrings."""
    a = np.frombuffer(b, dtype=np.uint8)
    n = len(a) - DLEN + 1
    if n <= 0:
        return np.zeros(0, dtype=np.int64)
    h = np.zeros(n, dtype=np.uint64)
    for i in range(DLEN):
        h = (h << np.uint64(8)) | a[i: i + n].astype(np.uint64)
    h *= np.uint64(0x9E3779B97F4A7C15)
    return np.unique((h >> np.uint64(64 - HBITS)).astype(np.int64))


def build(docs: list, budget: int, log=print) -> bytes:
    t = time.time()
    df = np.zeros(1 << HBITS, dtype=np.uint8)
    for d in docs:
        idx = hashes(d)
        df[idx] += df[idx] < 255
    log(f"  document frequencies of {sum(map(len, docs)) / 1e6:.1f} MB in {time.time() - t:.0f} s")
    covered = np.zeros(1 << HBITS, dtype=bool)

    def score(piece: bytes) -> int:
        idx = hashes(piece)
        f = df[idx]
        return int(f[(f > 1) & ~covered[idx]].sum())

    heap = []
    for di, d in enumerate(docs):
        for s in range(0, len(d), SEG):
            if len(d) - s >= 64:
                heap.append((-score(d[s: s + SEG]), di, s))
    heapq.heapify(heap)
    log(f"  {len(heap)} segments scored in {time.time() - t:.0f} s")
    chosen, size = [], 0
    while heap and size < budget:
        neg, di, s = heapq.heappop(heap)
        piece = docs[di][s: s + SEG]
        sc = score(piece)
        if heap and sc < -heap[0][0]:
            heapq.heappush(heap, (-sc, di, s))  # its score fell: put it back
            continue
        if sc <= 0:
            break
        chosen.append((sc, piece))
        covered[hashes(piece)] = True
        size += len(piece)
    log(f"  chose {len(chosen)} segments, {size / 1e6:.1f} MB, in {time.time() - t:.0f} s")
    chosen.sort(key=lambda x: x[0])  # most valuable last
    return b"".join(p for _, p in chosen)[-budget:]


def kind_docs(kind: str, seed: int = 1) -> list:
    """The kind's corpus files: at most PER_SOURCE bytes from one source (a seeded sample of its files), so
    no single project dominates, and at most POOL in all."""
    by = {}
    for k, sid, _, b in corpus.files(corpus.load()):
        if k == kind:
            by.setdefault(sid, []).append(b)
    rng = random.Random(seed)
    docs = []
    for sid in sorted(by):
        rng.shuffle(by[sid])
        n = 0
        for b in by[sid]:
            if n + len(b) > PER_SOURCE:
                continue
            docs.append(b)
            n += len(b)
    rng.shuffle(docs)
    out, total = [], 0
    for d in docs:
        if total + len(d) > POOL:
            break
        out.append(d)
        total += len(d)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", default=",".join(corpus.KINDS))
    ap.add_argument("--budget", type=int, default=8 << 20)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for kind in args.kinds.split(","):
        docs = kind_docs(kind)
        print(f"{kind}: {len(docs)} files, {sum(map(len, docs)) / 1e6:.1f} MB", flush=True)
        p = build(docs, args.budget, log=lambda m: print(m, flush=True))
        with open(os.path.join(args.out, kind + ".bin"), "wb") as f:
            f.write(p)
        print(f"{kind}: primer {len(p)} bytes", flush=True)


if __name__ == "__main__":
    main()
