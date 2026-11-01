#!/usr/bin/env python3
"""Pipeline speed and size per plan step, on one machine in one session. Each git revision runs from its
own worktree (built with its own Makefile) on Python and JSON (the full-tier harness files) and logs (the
16 Loghub samples of tools/experiments/logs.json). Per revision and kind, each in a fresh process:
  c1, d1   1 core (pinned to one CPU): compress cold (priming included), then warm (snapshots loaded, as
           in a running server); decompress the same way. Every output is checked by sha256.
  cN, dN   the same files on N threads in one process, as the server and the harness run them (warm).
  pN       N processes, one per CPU, each with its share of the files (warm): no GIL between them.
  big      the kind's files joined into one stream, 1 core, warm: the model with per-file costs amortised.
  b1, bN   bundles (mixz.compress_bundle): all files in one bundle on 1 core; N bundles on N threads.
Revision '.' is the working tree as it is (not rebuilt). The revisions in the example below are in the development
repository, which is not published; in the public repository only '.' and its own commits exist.
Speeds are input MB per wall-clock second. Results are cached per (revision, kind, mode).

  steps.py --revs 0696daa,e7a232a,4a54acc,af82acf,e161887,c23c5d6,ff28c4f [--kinds py,json,logs]
           [--modes c1,d1,cN,dN,big] [--cores 4] [--json OUT]
"""
import argparse
import hashlib
import json
import multiprocessing
import os
import resource
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "tools", "harness"))
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "mixz", "steps")
STAGES = ("make_plan", "choose_primer", "_acquire_snapshot", "cm_encode", "cm_decode")


def kind_files(kind: str) -> list:
    """(path, sha256) of the kind's files."""
    if kind == "logs":
        with open(os.path.join(HERE, "logs.json")) as f:
            small = json.load(f)["small"]
        d = os.path.join(CACHE, "..", "logs", "2k")
        return [(os.path.join(d, e["name"]), e["sha256"]) for e in small]
    import fetch
    return [(e["abs"], e["sha256"]) for e in fetch.ensure("full") if e["kind"] == kind]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def cpu_s() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


# ---------------------------------------------------------------- child (runs one revision's mixz)
def load_mixz(repo: str):
    sys.path.insert(0, repo)
    import mixz
    mixz.USE_SNAPSHOTS = True
    mixz.engine()
    stages = {}

    def timed(name, fn):
        def w(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                stages[name] = stages.get(name, 0.0) + time.perf_counter() - t
        return w
    for name in STAGES:
        if hasattr(mixz, name):
            setattr(mixz, name, timed(name, getattr(mixz, name)))
    return mixz, stages


def timed_pass(fn, items, threads: int = 1):
    stages_before = dict(STAGE_TIMES)
    t, c = time.perf_counter(), cpu_s()
    if threads > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(threads) as ex:
            out = list(ex.map(fn, items))
    else:
        out = [fn(x) for x in items]
    st = {k: STAGE_TIMES.get(k, 0.0) - stages_before.get(k, 0.0) for k in STAGE_TIMES}
    return out, {"wall": time.perf_counter() - t, "cpu": cpu_s() - c, "stages": st}


STAGE_TIMES: dict = {}


def _part(args):
    """One process of a pN run: warm up on its share, wait for the others, time a warm pass."""
    repo, mode, cpu, items, barrier, q = args
    os.sched_setaffinity(0, {cpu})
    mixz, _ = load_mixz(repo)
    fn = mixz.compress if mode == "c" else mixz.decompress
    [fn(x) for x in items]
    barrier.wait()
    t = time.perf_counter()
    [fn(x) for x in items]
    q.put(time.perf_counter() - t)


def child(repo: str, kind: str, mode: str, cores: int, blobdir: str) -> dict:
    global STAGE_TIMES
    with open(os.path.join(blobdir, "files.json")) as f:  # written by the parent: importing the harness here
        files = json.load(f)                                # would import the main checkout's mixz
    datas = []
    for p, h in files:
        with open(p, "rb") as f:
            datas.append(f.read())
        if sha(datas[-1]) != h:
            raise SystemExit(f"{p}: sha256 mismatch")
    nbytes = sum(map(len, datas))
    res = {"files": len(datas), "bytes": nbytes}
    if mode in ("c1", "d1", "big"):
        os.sched_setaffinity(0, {os.cpu_count() - 1})
    if mode == "pN":
        ctx = multiprocessing.get_context("spawn")
        blobs = [open(os.path.join(blobdir, f"{i}.mxz"), "rb").read() for i in range(len(datas))]
        for m, items in (("c", datas), ("d", blobs)):
            shares, load = [[] for _ in range(cores)], [0] * cores
            for x in sorted(items, key=len, reverse=True):  # largest first onto the least loaded process
                i = load.index(min(load))
                shares[i].append(x)
                load[i] += len(x)
            with ctx.Manager() as mgr:
                barrier, q = mgr.Barrier(cores), mgr.Queue()
                procs = [ctx.Process(target=_part, args=((repo, m, i, shares[i], barrier, q),)) for i in range(cores)]
                for p in procs:
                    p.start()
                for p in procs:
                    p.join()
                    if p.exitcode:
                        raise SystemExit(f"pN process failed ({p.exitcode})")
                res[m + "_wall"] = max(q.get() for _ in procs)
        return res
    mixz, STAGE_TIMES = load_mixz(repo)
    threads = cores if mode in ("cN", "dN") else 1
    if mode in ("b1", "bN"):
        n = cores if mode == "bN" else 1
        shares, load = [[] for _ in range(n)], [0] * n
        for i in sorted(range(len(datas)), key=lambda i: -len(datas[i])):
            j = load.index(min(load))
            shares[j].append(i)
            load[j] += len(datas[i])
        bundles_in = [[datas[i] for i in sorted(sh)] for sh in shares]
        outs, cold = timed_pass(mixz.compress_bundle, bundles_in, n)
        outs2, warm = timed_pass(mixz.compress_bundle, bundles_in, n)
        backs, d = timed_pass(mixz.decompress_bundle, outs, n)
        if outs != outs2 or backs != bundles_in:
            raise SystemExit("bundle: round trip failed")
        return dict(res, out=sum(map(len, outs)), cold=cold, warm=warm, d=d)
    if mode == "big":
        stream = b"".join(datas)
        mixz.compress(stream)  # primes the snapshot for this size class
        (out,), c = timed_pass(mixz.compress, [stream])
        (back,), d = timed_pass(mixz.decompress, [out])
        if back != stream:
            raise SystemExit("big: round trip failed")
        return dict(res, out=len(out), c=c, d=d)
    if mode[0] == "c":
        outs, cold = timed_pass(mixz.compress, datas, threads)
        outs2, warm = timed_pass(mixz.compress, datas, threads)
        if outs != outs2:
            raise SystemExit("compress: the warm pass differs from the cold pass")
        if mode == "c1":
            for i, o in enumerate(outs):
                with open(os.path.join(blobdir, f"{i}.mxz"), "wb") as f:
                    f.write(o)
        return dict(res, out=sum(map(len, outs)), cold=cold, warm=warm)
    blobs = [open(os.path.join(blobdir, f"{i}.mxz"), "rb").read() for i in range(len(datas))]
    backs, cold = timed_pass(mixz.decompress, blobs, threads)
    backs2, warm = timed_pass(mixz.decompress, blobs, threads)
    if any(sha(b) != h for b, (_, h) in zip(backs, files)) or backs != backs2:
        raise SystemExit("decompress: round trip failed")
    return dict(res, cold=cold, warm=warm)


# ---------------------------------------------------------------- parent
def worktree(rev: str, base: str) -> str:
    if rev == ".":
        return ROOT
    full = subprocess.run(["git", "-C", ROOT, "rev-parse", rev], capture_output=True, text=True, check=True).stdout.strip()
    wt = os.path.join(base, full[:10])
    if not os.path.isdir(wt):
        subprocess.run(["git", "-C", ROOT, "worktree", "add", "--detach", wt, full], check=True, capture_output=True)
    subprocess.run(["make", "-C", wt, "-s"], check=True, capture_output=True)
    return wt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revs", default="HEAD")
    ap.add_argument("--kinds", default="py,json,logs")
    ap.add_argument("--modes", default="c1,d1,cN,dN,big")
    ap.add_argument("--cores", type=int, default=os.cpu_count())
    ap.add_argument("--worktrees", default=os.path.join(CACHE, "wt"))
    ap.add_argument("--json")
    ap.add_argument("--child", nargs=5, metavar=("REPO", "KIND", "MODE", "CORES", "BLOBDIR"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.child:
        repo, kind, mode, cores, blobdir = args.child
        json.dump(child(repo, kind, mode, int(cores), blobdir), sys.stdout)
        return 0
    os.makedirs(CACHE, exist_ok=True)
    allres = {}
    for rev in args.revs.split(","):
        wt = worktree(rev, args.worktrees)
        key_rev = "worktree" if rev == "." else os.path.basename(wt)
        for kind in args.kinds.split(","):
            blobdir = os.path.join(args.worktrees, "blobs", key_rev, kind)
            os.makedirs(blobdir, exist_ok=True)
            with open(os.path.join(blobdir, "files.json"), "w") as f:
                json.dump(kind_files(kind), f)
            for mode in args.modes.split(","):
                cache = os.path.join(CACHE, f"{key_rev}-{kind}-{mode}-{args.cores}.json")
                if not os.path.exists(cache) or (mode in ("d1", "dN", "pN") and not os.path.exists(os.path.join(blobdir, '0.mxz'))):
                    if mode in ("d1", "dN", "pN") and not os.path.exists(os.path.join(blobdir, '0.mxz')):
                        need = os.path.join(CACHE, f"{key_rev}-{kind}-c1-{args.cores}.json")
                        if os.path.exists(need):
                            os.remove(need)  # rerun c1 to write the blobs again
                        allres[(rev, kind, "c1")] = run_child(wt, kind, "c1", args.cores, blobdir, need)
                    allres[(rev, kind, mode)] = run_child(wt, kind, mode, args.cores, blobdir, cache)
                else:
                    with open(cache) as f:
                        allres[(rev, kind, mode)] = json.load(f)
                r = allres[(rev, kind, mode)]
                print(f"{rev:8s} {kind:5s} {mode:4s} {summary(r)}", flush=True)
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"/".join(k): v for k, v in allres.items()}, f, indent=0)
    return 0


def run_child(wt, kind, mode, cores, blobdir, cache) -> dict:
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "--child", wt, kind, mode, str(cores), blobdir],
                       capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"{wt} {kind} {mode}: failed\n{p.stderr[-2000:]}")
    r = json.loads(p.stdout)
    with open(cache + ".tmp", "w") as f:
        json.dump(r, f)
    os.replace(cache + ".tmp", cache)
    return r


def summary(r: dict) -> str:
    mb = r["bytes"] / 1e6
    if "c_wall" in r:
        return f"processes: compress {mb / r['c_wall']:.3f} MB/s, decompress {mb / r['d_wall']:.3f} MB/s"
    if "d" in r and "warm" in r:
        return (f"size {r['out'] / r['bytes']:.2%}; bundles: compress warm {mb / r['warm']['wall']:.3f} MB/s, "
                f"decompress {mb / r['d']['wall']:.3f} MB/s")
    if "c" in r:
        return f"size {r['out'] / r['bytes']:.2%}; one stream: compress {mb / r['c']['wall']:.3f} MB/s, decompress {mb / r['d']['wall']:.3f} MB/s"
    s = f"cold {mb / r['cold']['wall']:.3f} MB/s, warm {mb / r['warm']['wall']:.3f} MB/s (cpu {mb / r['warm']['cpu']:.3f})"
    return (f"size {r['out'] / r['bytes']:.2%}; " if "out" in r else "") + s


if __name__ == "__main__":
    sys.exit(main())
