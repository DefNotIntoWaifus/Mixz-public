#!/usr/bin/env python3
"""Compress and decompress speed of mixz and the other tools on one input, one core, CPU time.

  speed_compare.py [KIND]   the harness quick tier's files of KIND joined into one input (default py)

Every tool codes the same joined input once as one stream. gzip -9, xz -9e, zstd -19 and brotli -q11
run in this process (best of 3 timings, repeated until each takes at least 0.5 s); mixz runs in this
process too (mixz.compress / mixz.decompress, best of 3); paq8px -8 and zpaq -m5 (the pinned builds of
tools/harness/reference.py) run once each way as their own processes, timed by their own CPU time.
The process and its children are pinned to the last CPU. Every output is decompressed and compared.
Writes results/compare/<machine>-speed.json."""
import gzip
import json
import lzma
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools", "harness"), os.path.join(ROOT, "tools")]
import fetch  # noqa: E402
import reference  # noqa: E402


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def best(fn, arg, reps: int = 3, floor: float = 0.5) -> tuple:
    """(result, CPU seconds per call): fastest of `reps` timings, each repeated to last `floor` s."""
    t = cpu()
    out = fn(arg)
    n = max(1, int(floor / max(cpu() - t, 1e-6)))
    secs = []
    for _ in range(reps):
        t = cpu()
        for _ in range(n):
            fn(arg)
        secs.append((cpu() - t) / n)
    return out, min(secs)


def child(cmd: list) -> float:
    """Run a command to completion; its own CPU seconds."""
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _, status, ru = os.wait4(p.pid, 0)
    if os.waitstatus_to_exitcode(status) != 0:
        raise SystemExit(f"{cmd[0]} failed (exit {os.waitstatus_to_exitcode(status)})")
    return ru.ru_utime + ru.ru_stime


def main() -> int:
    import brotli
    import zstandard
    import mixz
    kind = sys.argv[1] if len(sys.argv) > 1 else "py"
    os.sched_setaffinity(0, {max(os.sched_getaffinity(0))})
    es = [e for e in fetch.ensure("quick") if e["kind"] == kind]
    data = b"".join(open(e["abs"], "rb").read() for e in es)
    n = len(data)
    res = {}
    for name, c, d in (
            ("gzip -9", lambda b: gzip.compress(b, 9, mtime=0), gzip.decompress),
            ("xz -9e", lambda b: lzma.compress(b, preset=9 | lzma.PRESET_EXTREME), lzma.decompress),
            ("zstd -19", zstandard.ZstdCompressor(level=19).compress, zstandard.ZstdDecompressor().decompress),
            ("brotli -q11", lambda b: brotli.compress(b, quality=11), brotli.decompress),
            ("mixz", mixz.compress, mixz.decompress)):
        floor = 0 if name == "mixz" else 0.5
        out, cs = best(c, data, floor=floor)
        back, ds = best(d, out, floor=floor)
        assert back == data, f"{name}: round trip failed"
        res[name] = {"size": len(out), "compress_cpu_s": cs, "decompress_cpu_s": ds}
    with tempfile.TemporaryDirectory(prefix="mixz-speed-") as tmp:
        src = os.path.join(tmp, "f")
        with open(src, "wb") as f:
            f.write(data)
        for tool in reference.TOOLS:
            exe = reference.binary(reference.TOOLS[tool])
            d = os.path.join(tmp, tool.split()[0])
            os.makedirs(d)
            arc, back = os.path.join(d, "o"), os.path.join(d, "back")
            if tool == "paq8px -8":
                cs = child([exe, "-8", src, arc])
                ds = child([exe, "-d", arc, back])
            else:
                cs = child(["sh", "-c", 'exec "$0" 5 <"$1" >"$2"', exe, src, arc])
                ds = child(["sh", "-c", 'exec "$0" d <"$1" >"$2"', exe, arc, back])
            assert open(back, "rb").read() == data, f"{tool}: round trip failed"
            res[tool] = {"size": os.path.getsize(arc), "compress_cpu_s": cs, "decompress_cpu_s": ds}
            shutil.rmtree(d)
    import run  # the harness, for the machine name
    out = os.path.join(ROOT, "results", "compare", f"{run.machine_slug(run.fingerprint([]))}-speed.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "files": len(es), "bytes": n,
                   "cpu": max(os.sched_getaffinity(0)), "tools": res}, f, indent=1)
    print(f"{kind}: {len(es)} files joined, {n / 1e6:.2f} MB, one core, CPU time")
    for t, r in res.items():
        print(f"  {t:12s} {r['size'] / n:7.2%}  compress {n / r['compress_cpu_s'] / 1e6:9.3f} MB/s  "
              f"decompress {n / r['decompress_cpu_s'] / 1e6:9.3f} MB/s  ({r['decompress_cpu_s'] * 1e9 / n:9.1f} ns/B)")
    print(f"\nsaved {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
