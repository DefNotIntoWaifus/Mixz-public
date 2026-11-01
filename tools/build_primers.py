#!/usr/bin/env python3
"""Build per-format primers from the TRAINING split only.

A primer is a sample of typical content for one format. Before coding a file, encoder
and decoder both replay the primer through the model, so the file starts with learned
statistics and a match buffer full of typical strings.

Two selection methods:
  concat  random training files (each capped at --per-file bytes) until the budget is full
  cover   "most repeated segments": split training files into segments and greedily pick
          the segments whose 8-byte substrings occur in the most training files, skipping
          substrings already covered (a simplified version of zstd's COVER dictionary
          builder). This is the working version of "find the most repeated patterns per
          file type": variable-length, byte-aligned, and used as model knowledge rather
          than as a fixed code table.
"""
import argparse
import heapq
import os
import random
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import corpora  # noqa: E402

PRIMER_DIR = os.path.join(os.path.dirname(HERE), "primers")


def load_train(kind: str, max_bytes: int, per_file: int, seed: int = 1):
    train, _ = corpora.split(kind)
    rng = random.Random(seed)
    rng.shuffle(train)
    out, total = [], 0
    for f in train:
        d = corpora.content(kind, f)[:per_file]
        if len(d) < 64:
            continue
        out.append(d)
        total += len(d)
        if total >= max_bytes:
            break
    return out


def primer_concat(docs, budget: int) -> bytes:
    buf = bytearray()
    for d in docs:
        if len(buf) >= budget:
            break
        buf += d[: budget - len(buf)]
    return bytes(buf)


def _dmers(d: bytes, dlen: int = 8) -> np.ndarray:
    a = np.frombuffer(d, dtype=np.uint8).astype(np.uint64)
    if len(a) < dlen:
        return np.zeros(0, dtype=np.uint64)
    h = np.zeros(len(a) - dlen + 1, dtype=np.uint64)
    for i in range(dlen):
        h = (h << np.uint64(8)) | a[i: len(a) - dlen + 1 + i]
    return h


def primer_cover(docs, budget: int, seg: int = 1024) -> bytes:
    # document frequency of every 8-byte substring
    per_doc = [np.unique(_dmers(d)) for d in docs]
    allm = np.concatenate(per_doc)
    vals, counts = np.unique(allm, return_counts=True)
    segments, heap = [], []
    for di, d in enumerate(docs):
        for s in range(0, len(d), seg):
            piece = d[s: s + seg]
            if len(piece) < 64:
                continue
            m = np.unique(_dmers(piece))
            idx = np.searchsorted(vals, m)
            f = counts[idx]
            keep = f > 1  # substrings seen in more than one file
            segments.append((piece, set(m[keep].tolist()), dict(zip(m[keep].tolist(), f[keep].tolist()))))
            score = int(f[keep].sum())
            heapq.heappush(heap, (-score, len(segments) - 1))
    covered, chosen, size = set(), [], 0
    while heap and size < budget:
        neg, i = heapq.heappop(heap)
        piece, ms, freq = segments[i]
        score = sum(freq[x] for x in ms - covered)
        if heap and score < -heap[0][0]:
            heapq.heappush(heap, (-score, i))  # lazy greedy re-insert
            continue
        if score <= 0:
            break
        chosen.append((score, piece))
        covered |= ms
        size += len(piece)
    # most valuable segments last: they are freshest in the model when the file starts
    chosen.sort(key=lambda t: t[0])
    return b"".join(p for _, p in chosen)[-budget:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", nargs="*", default=list(corpora.CORPORA))
    ap.add_argument("--budget", type=int, default=512 * 1024)
    ap.add_argument("--method", choices=["concat", "cover"], default="cover")
    ap.add_argument("--per-file", type=int, default=16384)
    ap.add_argument("--out", default=PRIMER_DIR)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for kind in args.kinds:
        pool = args.budget * (12 if args.method == "cover" else 1) + args.per_file
        docs = load_train(kind, pool, args.per_file)
        if not docs:
            print(f"{kind}: no training files, skipped")
            continue
        p = primer_cover(docs, args.budget) if args.method == "cover" else primer_concat(docs, args.budget)
        with open(os.path.join(args.out, kind + ".bin"), "wb") as f:
            f.write(p)
        print(f"{kind}: {len(docs)} training files sampled -> primer {len(p)} bytes ({args.method})")


if __name__ == "__main__":
    main()
