#!/usr/bin/env python3
"""Output size, priming time and memory against primer size (plan step 7).

Each kind's tuner files (train and valid: the public-domain corpus shares no content with them) are
coded the way mixz codes them, with the stream, engine profile and transform of mixz.make_plan, but
with the last N bytes of a candidate primer (tools/mind/build.py puts the most valuable segments
last). Every (kind, N, maxbits) runs in a fresh process with an address-space limit; the worker
count fits the free memory and the run stops if free memory falls below MEM_STOP (16 GB development desktop).
Results are cached per task.

  sizecurve.py --primers DIR --sizes 0,0.5,1,2,4,8,16,32 --maxbits 20,22 [--kinds py,c] [--json OUT]
"""
import argparse
import ctypes
import hashlib
import json
import os
import resource
import sys
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(TOOLS, "tuner"))
import variants  # noqa: E402

MIB = 1 << 20
MEM_RESERVE = 2 << 30  # kept free beyond the running tasks
MEM_STOP = variants.MEM_STOP


def model_bytes(total: int, maxbits: int) -> int:
    """Approximate memory of one model (engine/cm.c model_sizes)."""
    t = min(max((total * 2 + 1).bit_length() + 1 - 1, 16), maxbits)
    b = min(max((total + 1).bit_length(), 16), 28)
    h = min(max(b - 2, 16), 24)
    return 8 * (16 << t) + 8 * (4 << (t - 1)) + (1 << b) + (4 << h) + 9 * MIB


def task_bytes(n: int, maxbits: int) -> int:
    return 2 * model_bytes(n + MIB, maxbits) + 200 * MIB  # snapshot + worker + Python and streams


def _init(cap: int):
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # no core dump on a crash
    os.nice(10)


def _cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def run(lib_path: str, primer_path: str, n: int, maxbits: int, files: list) -> dict:
    lib = variants._load(lib_path)
    with open(primer_path, "rb") as f:
        primer = f.read()[-n:] if n else b""
    worker = lib.cm_worker_new()
    out, prime_cpu, code_cpu, snap, snap_key = 0, 0.0, 0.0, None, None
    try:
        for e in sorted(files, key=lambda e: (e["stored"], e.get("profile", 0), e.get("stride", 0), e.get("bpp", 0))):
            if e["stored"]:
                out += e["bytes"]
                continue
            with open(e["stream"], "rb") as f:
                s = f.read()
            cap = len(s) + len(s) // 8 + 4096
            buf = ctypes.create_string_buffer(cap)
            if primer:
                k = (e["profile"], e["stride"], e["bpp"], lib.cm_size_class(len(s) + len(primer), maxbits))
                if k != snap_key:
                    if snap:
                        lib.cm_free_primed(snap)
                    t = _cpu()
                    snap = lib.cm_prime(primer, len(primer), e["profile"], e["stride"], e["bpp"], maxbits, len(s) + len(primer))
                    prime_cpu += _cpu() - t
                    snap_key = k
                    if not snap:
                        raise RuntimeError("cm_prime failed (out of memory?)")
                t = _cpu()
                r = lib.cm_compress_primed(snap, worker, s, len(s), len(primer), buf, cap)
            else:
                t = _cpu()
                r = lib.cm_compress(s, len(s), b"", 0, e["profile"], e["stride"], e["bpp"], maxbits, buf, cap)
            code_cpu += _cpu() - t
            if r == -3:
                raise RuntimeError("snapshot does not fit the file")
            out += e["bytes"] if r == -2 else r
            if r < 0 and r != -2:
                raise RuntimeError(f"engine error {r}")
    finally:
        if snap:
            lib.cm_free_primed(snap)
        lib.cm_worker_free(worker)
    return {"bytes": out, "prime_cpu": prime_cpu, "code_cpu": code_cpu,
            "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}


def _cached(path: str, *a) -> dict:
    if not os.path.exists(path):
        r = run(*a)
        with open(path + ".tmp", "w") as f:
            json.dump(r, f)
        os.replace(path + ".tmp", path)
    with open(path) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--primers", required=True)
    ap.add_argument("--sizes", default="0,0.5,1,2,4,8,16,32")
    ap.add_argument("--maxbits", default="20")
    ap.add_argument("--kinds", default="py,c,js,json,web,prose,man")
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--json")
    args = ap.parse_args()
    kinds = args.kinds.split(",")
    entries = [e for e in variants.data.ensure() if e["kind"] in kinds]
    files = variants.plans(entries)
    lib = variants.build("base")
    d = os.path.join(variants.CACHE, "sizecurve")
    os.makedirs(d, exist_ok=True)
    tasks = []
    for kind in kinds:
        pp = os.path.join(args.primers, kind + ".bin")
        if not os.path.exists(pp):
            print(f"{kind}: no primer in {args.primers}", file=sys.stderr)
            continue
        plen = os.path.getsize(pp)
        with open(pp, "rb") as f:
            ph = hashlib.sha256(f.read()).hexdigest()[:16]
        fk = [e for e in files if e["kind"] == kind]
        for mb in (int(x) for x in args.maxbits.split(",")):
            for s in args.sizes.split(","):
                n = min(int(float(s) * MIB), plen)
                if float(s) and n < int(float(s) * MIB):
                    continue  # the primer is smaller than this size
                key = f"{os.path.basename(lib)[:-3]}-{ph}-{n}-{mb}-{kind}"
                tasks.append(((kind, n, mb), task_bytes(n, mb), (os.path.join(d, key + ".json"), lib, pp, n, mb, fk)))
    res = {}
    pending = sorted(tasks, key=lambda t: t[1])
    running = {}
    while pending or running:
        free = variants.mem_available()
        if free < MEM_STOP:
            raise SystemExit(f"stopped: free memory {free >> 20} MB; finished tasks are cached, rerun to resume")
        used = sum(t[1] for t in running.values())
        while pending and len(running) < args.jobs and free - pending[0][1] - used >= MEM_RESERVE:
            t = pending.pop(0)
            ex = ProcessPoolExecutor(1, initializer=_init, initargs=(t[1] + 512 * MIB,))
            running[ex.submit(_cached, *t[2])] = t + (ex,)
            used += t[1]
        if not running:
            raise SystemExit(f"not enough free memory for {pending[0][0]}: needs {(pending[0][1] + MEM_RESERVE) >> 20} MB, "
                             f"{free >> 20} MB free; finished tasks are cached")
        done, _ = wait(list(running), timeout=2, return_when=FIRST_EXCEPTION)
        for f in done:
            t = running.pop(f)
            t[3].shutdown()
            if f.exception():
                raise SystemExit(f"task {t[0]} failed: {f.exception()!r}; finished tasks are cached")
            res[t[0]] = f.result()
            print(f"{t[0]}: {f.result()}", file=sys.stderr, flush=True)
    raw = {k: sum(e["bytes"] for e in files if e["kind"] == k) for k in kinds}
    print(f"{'kind':6s} {'maxbits':>7s} {'primer MB':>9s} {'size':>8s} {'vs none':>8s} {'prime s':>8s} {'code s':>7s} {'peak MB':>8s}")
    for kind in kinds:
        for mb in sorted({k[2] for k in res if k[0] == kind}):
            rows = sorted((k[1], v) for k, v in res.items() if k[0] == kind and k[2] == mb)
            base = next((v["bytes"] for n, v in rows if n == 0), None)
            for n, v in rows:
                rel = f"{v['bytes'] / base - 1:+8.2%}" if base else f"{'':8s}"
                print(f"{kind:6s} {mb:7d} {n / MIB:9.2f} {v['bytes'] / raw[kind]:8.2%} {rel} {v['prime_cpu']:8.1f} "
                      f"{v['code_cpu']:7.1f} {v['peak_rss'] >> 20:8d}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({f"{k[0]}/{k[1]}/{k[2]}": v for k, v in res.items()}, f, indent=0)


if __name__ == "__main__":
    main()
