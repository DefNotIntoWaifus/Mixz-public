#!/usr/bin/env python3
"""Benchmark mixz against gzip, xz, zstd (with and without a trained dictionary) and brotli
on HELD-OUT files (the split not used to build primers). Every file is compressed on its
own, and every mixz output is decompressed and compared with the original.

Usage: bench.py [--kinds py c ...] [--max-files 60] [--max-bytes 3000000] [--json out.json]
"""
import argparse
import gzip
import hashlib
import json
import lzma
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import corpora  # noqa: E402
import mixz  # noqa: E402
import overlap  # noqa: E402

mixz.USE_SNAPSHOTS = True

try:
    import zstandard
except ImportError:
    zstandard = None
try:
    import brotli
except ImportError:
    brotli = None

# what a user would actually hand to the compressor for each kind (raw files on disk)
KINDS = ["py", "c", "js", "json", "web", "prose", "man", "elf", "png"]
PNG_GLOB = ["/usr/**/*.png"]


def raw_files(kind):
    if kind == "png":
        import glob
        fs = sorted({f for p in PNG_GLOB for f in glob.glob(p, recursive=True)
                     if os.path.isfile(f) and not os.path.islink(f) and os.path.getsize(f) > 256})
        return [f for f in fs if corpora.is_train(f)], [f for f in fs if not corpora.is_train(f)]
    return corpora.split(kind)


def pick_tests(kind, max_files, max_bytes, max_file=2_000_000):
    train, test = raw_files(kind)
    seen = {hashlib.sha1(open(f, "rb").read()).digest() for f in train}
    rng = random.Random(42)
    rng.shuffle(test)
    out, total = [], 0
    for f in test:
        d = open(f, "rb").read()
        h = hashlib.sha1(d).digest()
        if len(d) > max_file or h in seen:
            continue
        # the path split only matches the VM that built the primers; content catches twins anywhere
        if overlap.primer_overlap(kind, d) > overlap.MAX_OVERLAP:
            continue
        seen.add(h)
        out.append((f, d))
        total += len(d)
        if len(out) >= max_files or total >= max_bytes:
            break
    return train, out


def zstd_dict(train, size=262144):
    if not zstandard:
        return None
    rng = random.Random(1)
    sample = list(train)
    rng.shuffle(sample)
    samples = [open(f, "rb").read()[:65536] for f in sample[:2000]]
    try:
        return zstandard.train_dictionary(size, samples)
    except Exception:
        return None


def run_kind(kind, args):
    train, tests = pick_tests(kind, args.max_files, args.max_bytes)
    if not tests:
        return None
    total = sum(len(d) for _, d in tests)
    res = {"kind": kind, "files": len(tests), "bytes": total, "sizes": {}, "times": {}}

    def measure(name, fn):
        t = time.time()
        res["sizes"][name] = sum(len(fn(d)) for _, d in tests)
        res["times"][name] = time.time() - t

    measure("gzip -9", lambda d: gzip.compress(d, 9, mtime=0))
    measure("xz -9e", lambda d: lzma.compress(d, preset=9 | lzma.PRESET_EXTREME))
    if zstandard:
        measure("zstd -19", lambda d: zstandard.ZstdCompressor(level=19).compress(d))
        zd = zstd_dict(train)
        if zd is not None:
            zc = zstandard.ZstdCompressor(level=19, dict_data=zd)
            measure("zstd -19 +dict", lambda d: zc.compress(d))
    if brotli:
        measure("brotli -q11", lambda d: brotli.compress(d, quality=11))

    def mz(d, primers):
        c = mixz.compress(d, use_primers=primers)
        return c

    for name, primers in (("mixz (no primer)", False), ("mixz", True)):
        t = time.time()
        with ThreadPoolExecutor(args.jobs) as ex:
            outs = list(ex.map(lambda fd: mz(fd[1], primers), tests))
        res["times"][name] = time.time() - t
        res["sizes"][name] = sum(len(c) for c in outs)
        if name == "mixz":
            t = time.time()
            with ThreadPoolExecutor(args.jobs) as ex:
                back = list(ex.map(mixz.decompress, outs))
            res["times"]["mixz decompress"] = time.time() - t
            res["roundtrip_ok"] = all(b == d for b, (_, d) in zip(back, tests))
            enc = {}
            for c in outs:
                h = mixz.parse_header(c)
                key = "stored" if h.method == mixz.M_STORED else (
                    f"{mixz.TRANSFORM_NAMES[h.transform]}/{mixz.PRIMER_IDS[h.primer] or 'no-primer'}")
                enc[key] = enc.get(key, 0) + 1
            res["encodings"] = enc
    return res


def table(results):
    names = []
    for r in results:
        for n in r["sizes"]:
            if n not in names:
                names.append(n)
    lines = ["| files | n | MB | " + " | ".join(names) + " |", "|---" * (len(names) + 3) + "|"]
    for r in results:
        best = min(r["sizes"].values())
        cells = []
        for n in names:
            v = r["sizes"].get(n)
            if v is None:
                cells.append("–")
                continue
            s = f"{v / r['bytes']:.1%}"
            cells.append(f"**{s}**" if v == best else s)
        lines.append(f"| {r['kind']} | {r['files']} | {r['bytes'] / 1e6:.2f} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kinds", nargs="*", default=KINDS)
    ap.add_argument("--max-files", type=int, default=60)
    ap.add_argument("--max-bytes", type=int, default=3_000_000)
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--json")
    args = ap.parse_args()
    results = []
    for kind in args.kinds:
        r = run_kind(kind, args)
        if r is None:
            print(f"{kind}: no test files")
            continue
        results.append(r)
        print(json.dumps(r), flush=True)
    print()
    print(table(results))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(results, f, indent=1)


if __name__ == "__main__":
    main()
