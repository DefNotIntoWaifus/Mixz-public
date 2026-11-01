"""Blocks across cores (plan step 4): output size against block size, and speed against threads, on
large files. A large file per kind is the concatenation of that kind's full-tier harness files in
sets.json order (like an uncompressed archive; png and man are left out: a concatenation is not a PNG
or a gzip file). Each block starts from the primed model, so the output depends on the block size and
never on the thread count.
--file PATH measures that file instead (repeatable), e.g. the 205 MB NASA log of tools/experiments/logs.json.
Run: python3 tools/experiments/blocks.py [--sizes 0,4M,2M,1M,512K] [--speed 2M] [--threads 1,2,4] [--file PATH]"""
import argparse
import json
import os
import resource
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "harness"))
import fetch  # noqa: E402
import mixz  # noqa: E402

KINDS = ("py", "c", "js", "json", "web", "prose", "elf")


def blobs() -> dict:
    out = {}
    for e in fetch.ensure("full"):
        if e["kind"] in KINDS:
            with open(e["abs"], "rb") as f:
                out.setdefault(e["kind"], []).append(f.read())
    return {k: b"".join(v) for k, v in out.items()}


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="0,4M,2M,1M,512K")
    ap.add_argument("--speed", default="2M", help="block sizes to time with each thread count")
    ap.add_argument("--threads", default="1,2,4")
    ap.add_argument("--file", action="append", help="measure this file instead of the kind concatenations")
    args = ap.parse_args()
    sizes = [mixz.parse_size(x) for x in args.sizes.split(",")]
    speed_sizes = [mixz.parse_size(x) for x in args.speed.split(",")] if args.speed else []
    data = {os.path.basename(p): open(p, "rb").read() for p in args.file} if args.file else blobs()
    total = sum(map(len, data.values()))
    print(f"{len(data)} kinds, {total / 1e6:.1f} MB: " + ", ".join(f"{k} {len(v) / 1e6:.1f} MB" for k, v in data.items()))
    mixz.engine()
    mixz.SNAP_CACHE = 10  # the speed runs reuse the primed models of the pass before them (~1.6 GB)
    mixz.set_block_threads(0)
    res = {"sizes": {}, "speed": {}}
    print("\nblock size  " + "  ".join(f"{k:>7s}" for k in data) + "    total   blocks")
    outs = {}
    for bs in sizes:
        row = {k: len(mixz.compress(v, block_size=bs)) for k, v in data.items()}
        res["sizes"][bs] = row
        nb = sum(len(mixz.split_blocks(len(v), bs)) for v in data.values())
        base = res["sizes"][sizes[0]]
        print(f"{f'{bs // 1024} KiB' if bs else 'one':>10s}  "
              + "  ".join(f"{row[k] / len(data[k]):7.2%}" for k in data)
              + f"  {sum(row.values()) / total:7.2%}  {nb:5d}"
              + ("" if bs == sizes[0] else "   (" + ", ".join(f"{k} {row[k] / base[k] - 1:+.2%}" for k in data) + ")"))
    for bs in speed_sizes:
        outs = {k: mixz.compress(v, block_size=bs) for k, v in data.items()}
        for n in [int(x) for x in args.threads.split(",")]:
            mixz.set_block_threads(n)  # the snapshots were primed by the pass above; the workers are allocated in the timing
            t0, c0 = time.perf_counter(), cpu()
            for v in data.values():
                mixz.compress(v, block_size=bs)
            t1, c1 = time.perf_counter(), cpu()
            for k, o in outs.items():
                assert mixz.decompress(o) == data[k]
            t2, c2 = time.perf_counter(), cpu()
            res["speed"][f"{bs}/{n}"] = {"c_wall": t1 - t0, "c_cpu": c1 - c0, "d_wall": t2 - t1, "d_cpu": c2 - c1}
            print(f"blocks of {bs // 1024} KiB, {n} thread(s): compress {total / (t1 - t0) / 1e6:.3f} MB/s "
                  f"(CPU {c1 - c0:.1f} s), decompress {total / (t2 - t1) / 1e6:.3f} MB/s (CPU {c2 - c1:.1f} s)", flush=True)
    res["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    res["load"] = os.getloadavg()
    print(f"peak RSS {res['peak_rss_mb']} MB, load {res['load'][0]:.1f}")
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
