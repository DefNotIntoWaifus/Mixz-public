"""Primer overlap: the share of a file's bytes that also occur verbatim in a primer.

A primer is made of 1 KB pieces of training files (tools/build_primers.py). If a test file,
or a near-copy of it, was used to build a primer, long runs of its bytes are inside the
primer and cost almost nothing to code, which makes the benchmark look better than it is.
This module measures that from content alone, so it works on any machine. The path-based
split in corpora.py cannot: the same file has different paths on different distributions
(for example dist-packages on Ubuntu and site-packages on Arch).

overlap(data, primer, k) = share of the bytes of `data` that lie inside some k-byte string
that also occurs in `primer`. k=64 ignores short shared idioms; license headers still count.

MAX_OVERLAP: files above it are treated as (near-copies of) primer training data and are not
used for benchmarks. Calibrated on downloaded sources that were not on the VM (2026-10-23,
`tools/harness/fetch.py --calibrate`): their 90th percentile was at most 10% for every kind,
while sources that were on the VM reached 45% median (botocore) and 90% (lodash).
"""
import gzip
import os
import sys

import numpy as np

K = 64
MAX_OVERLAP = 0.20
_M = 1 << 64
_B = 0x9E3779B97F4A7C15  # odd, so it has an inverse mod 2^64
_BINV = pow(_B, -1, _M)


def _powers(base: int, n: int) -> np.ndarray:
    p = np.full(n, base, dtype=np.uint64)
    p[0] = 1
    return np.cumprod(p, dtype=np.uint64)  # wraps mod 2^64


def kgram_hashes(data: bytes, k: int = K) -> np.ndarray:
    """h[i] = sum_j data[i+j] * B^j mod 2^64, for every window start i."""
    n = len(data)
    if n < k:
        return np.zeros(0, dtype=np.uint64)
    a = np.frombuffer(data, dtype=np.uint8).astype(np.uint64)
    with np.errstate(over="ignore"):
        s = np.zeros(n + 1, dtype=np.uint64)
        np.cumsum(a * _powers(_B, n), dtype=np.uint64, out=s[1:])
        return (s[k:] - s[:-k]) * _powers(_BINV, n - k + 1)


_primer_cache: dict = {}


def primer_index(primer: bytes, k: int = K) -> np.ndarray:
    key = (id(primer), len(primer), k)
    if key not in _primer_cache:
        _primer_cache[key] = (primer, np.unique(kgram_hashes(primer, k)))
    return _primer_cache[key][1]


def overlap(data: bytes, primer: bytes, k: int = K) -> float:
    if not data or len(primer) < k:
        return 0.0
    h = kgram_hashes(data, k)
    if not len(h):
        return 0.0
    idx = primer_index(primer, k)
    pos = np.searchsorted(idx, h)
    pos[pos == len(idx)] = 0
    hit = (idx[pos] == h).astype(np.int64)
    # byte j is covered if a matching window starts in [j-k+1, j]
    c = np.concatenate(([0], np.cumsum(hit)))
    j = np.arange(len(data))
    lo = np.maximum(j - k + 1, 0)
    hi = np.minimum(j, len(h) - 1) + 1
    covered = (c[np.maximum(hi, lo)] - c[lo]) > 0
    return float(covered.mean())


def model_view(kind: str, data: bytes) -> bytes:
    """Roughly what the model sees for a file of this kind; primers were built from the same view."""
    import mixz
    if kind == "man":
        try:
            return gzip.decompress(data)
        except Exception:
            return data
    if kind == "elf":
        return mixz.e8e9(data, inverse=False)
    return data


def kind_primers(kind: str) -> list:
    import mixz
    names = {"png": [], "elf": mixz.BIN_PRIMERS}.get(kind, mixz.TEXT_PRIMERS)
    return [p for p in (mixz.primer_bytes(mixz.PRIMER_IDS.index(n)) for n in names) if p]


def primer_overlap(kind: str, data: bytes) -> float:
    """Highest overlap of a raw file of this kind with any primer mixz could choose for it."""
    view = model_view(kind, data)
    return max((overlap(view, p) for p in kind_primers(kind)), default=0.0)


def main():
    """overlap.py PRIMER FILE...: print the overlap of each file with the primer."""
    primer = open(sys.argv[1], "rb").read()
    for f in sys.argv[2:]:
        print(f"{overlap(open(f, 'rb').read(), primer):7.2%}  {os.path.basename(f)}")


if __name__ == "__main__":
    main()
