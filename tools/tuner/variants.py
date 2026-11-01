#!/usr/bin/env python3
"""Output size of engine variants per kind on the tuning data (plan step 6).

A variant is the codec source (mixz.SRC) with some #define values replaced and/or context patches
applied (CTX below: add:NAME, rep:SLOT:NAME); it is built alone into a small library. Each file is coded the
way mixz codes it: the stream, engine profile, primer, row length and bytes per pixel come from
mixz.make_plan (cached), and a primed file starts from a snapshot of the primed model.

  variants.py [--split train] [--kinds py,c] [--maxbits 20] [--jobs 3] base LR1=3 SMLIM=511,APMRATE=7 add:ind rep:7:nest
Prints the payload bytes of every variant and the change against the first one, per kind.
Memory: each worker is capped (MEM_CAP), the worker count fits the free memory, and the run stops if
free memory gets low (see the guards below).
"""
import argparse
import ctypes
import hashlib
import json
import os
import re
import resource
import subprocess
import sys
from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import data  # noqa: E402
import mixz  # noqa: E402

CACHE = os.path.join(data.fetch.cache_dir(), "tuner")
# Memory guards (the 16 GB development desktop: an editor running there crashes when memory runs short). A task peaks at
# 394 MB resident and 589 MB address space (measured, maxbits 20).
TASK_RSS = 640 << 20     # planning figure per worker
MEM_CAP = 1536 << 20     # hard address-space limit per worker: a runaway task fails instead of the desktop
MEM_RESERVE = 4 << 30    # kept free for the desktop when choosing the worker count
MEM_STOP = 2 << 30       # the run stops (finished tasks stay cached) if free memory falls below this
# Context experiments on the text engine profile. "add:NAME" adds CTX[NAME] as one more hashed context
# (repeatable; the binary and image profiles then get constant extra contexts); "rep:SLOT:NAME" replaces the text
# profile's context SLOT (0-7) with it. Both add the per-byte state below.
CTX = {
    "ind": "H4(9, (m->indent > 31 ? 31 : m->indent) | (m->inlead << 8), m->lfirst, c1)",
    "indw": "H4(16, (m->indent > 31 ? 31 : m->indent) | (m->inlead << 8), m->lfirst, m->word)",
    "nest": "H4(10, m->depth > 15 ? 15 : m->depth, m->bstack & 0xffff, c4 & 0xffff)",
    "nestw": "H4(17, m->depth > 15 ? 15 : m->depth, m->bstack & 0xff, m->word)",
    "tag": "H4(11, m->tag, m->intag, c4 & 0xffff)",
    "punct": "H3(12, m->lastp, c4 & 0xffff)",
    "sparse": "H2(13, c4 & 0xffff0000)",
    "o5": "H3(14, c4, c8 & 0xff)",
    "o8": "H3(15, c4, c8)",
}
# The codec keeps the per-byte state these contexts read (indentation, brackets, tag, punctuation) for
# the code and web profiles only; a context experiment keeps it for every profile.
STATE = [("  if (m->profile >= PROF_CODE) {\n    if (c == '\\n') { m->indent = 0;",
          "  if (1) {\n    if (c == '\\n') { m->indent = 0;")]
TEXT_BRANCH = "  } else {\n    U32 col = m->pos - m->line_start;"
# Two sizes in the codec fit at most 9 contexts: the mixer input array x[NIN] (3 * NC + 4 entries) and
# mixer 3's selector table (10 rows). add: enlarges both when needed. The AVX2 mixer
# reads exactly 32 inputs, so a variant with a larger NIN is built without AVX2 (the scalar path gives
# identical results, slower). Every variant gets a compile-time check.
GUARD = ('#define NM1 4                /* first-layer mixers */\n',
         '#define NM1 4                /* first-layer mixers */\n'
         '_Static_assert(NIN_USED <= NIN, "mixer inputs overflow x[NIN]");\n'
         '#if defined(__AVX2__) && NIN != 32\n#error "the AVX2 mixer reads exactly 32 inputs"\n#endif\n')


def _sub(src: str, old: str, new: str, what: str) -> str:
    if src.count(old) != 1:
        raise SystemExit(f"{what}: text occurs {src.count(old)} times: {old[:70]!r}")
    return src.replace(old, new)


def apply_ctx(src: str, part: str, state_done: bool) -> str:
    if not state_done:
        for old, new in STATE:
            src = _sub(src, old, new, "state")
    t = src.index(TEXT_BRANCH)
    head, tail = src[:t], src[t:]
    if part.startswith("add:"):
        expr = CTX[part[4:]]
        nc = int(re.search(r"^#define NC (\d+) ", src, re.M).group(1))
        src = _sub(src, f"#define NC {nc} ", f"#define NC {nc + 1} ", part)
        if nc == 8:
            src = _sub(src, "i = (v >> 24) & 7,", "i = (v >> 24) & 31,", part)  # the log's context field
        if nc + 1 == 10:  # mixer 3 selects by the count of known contexts (0..NC): 10 rows fit up to 9
            src = _sub(src, "m->nsel[3] = 10 * 8 * 2;", "m->nsel[3] = (NC + 1) * 8 * 2;", part)
        need = 3 * (nc + 1) + 4
        nin = int(re.search(r"^#define NIN (\d+) ", src, re.M).group(1))
        if need > nin:
            src = _sub(src, f"#define NIN {nin} ", f"#define NIN {(need + 15) // 16 * 16} ", part)
        t = src.index(TEXT_BRANCH)
        head, tail = src[:t], src[t:]
        last = tail.index("\n", tail.index(f"    m->h[{nc - 1}] = ")) + 1
        return head + tail[:last] + f"    m->h[{nc}] = {expr};\n" + tail[last:]
    _, slot, name = part.split(":")
    e = tail.index(f"    m->h[{slot}] = ")
    eol = tail.index("\n", e)
    return head + tail[:e] + f"    m->h[{slot}] = {CTX[name]};" + tail[eol:]


def plan_key() -> str:
    """Changes when anything that decides a plan changes: the wrapper code and the primers."""
    h = hashlib.sha256()
    for p in [os.path.join(ROOT, "mixz.py")] + [os.path.join(mixz.PRIMER_DIR, n + ".bin") for n in mixz.PRIMER_IDS if n]:
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:16]


def plans(entries: list) -> list:
    """Per file: the model stream (cached on disk) and how mixz would code it."""
    d = os.path.join(CACHE, "plans", plan_key())
    os.makedirs(d, exist_ok=True)
    out = []
    for e in entries:
        meta_path = os.path.join(d, e["sha256"] + ".json")
        stream_path = os.path.join(d, e["sha256"] + ".bin")
        if not os.path.exists(meta_path):
            with open(e["abs"], "rb") as f:
                raw = f.read()
            p = mixz.make_plan(raw)
            if p is None:
                meta = {"stored": True}
            else:
                if p.transform != mixz.T_NONE and mixz.inverse_transform(p.transform, p.stream, p.meta) != raw:
                    raise SystemExit(f"{e['abs']}: transform does not rebuild the file")
                meta = {"stored": False, "profile": p.profile, "primer": p.primer, "stride": p.stride, "bpp": p.bpp,
                        "transform": p.transform}
                with open(stream_path, "wb") as f:
                    f.write(p.stream)
            with open(meta_path, "w") as f:
                json.dump(meta, f)
        with open(meta_path) as f:
            meta = json.load(f)
        out.append(dict(e, **meta, stream=stream_path))
    return out


def variant_source(spec: str) -> str:
    with open(mixz.SRC) as f:
        src = f.read()
    if spec == "base":
        return src
    src = _sub(src, *GUARD, "guard")
    state = False
    for part in spec.split(","):
        if part.startswith(("add:", "rep:")):
            src = apply_ctx(src, part, state)
            state = True
            continue
        name, value = part.split("=", 1)
        src, n = re.subn(rf"^#define {name} [^\n/]*", f"#define {name} {value}", src, count=1, flags=re.M)
        if n != 1:
            raise SystemExit(f"no #define {name} in the codec")
    return src


def build(spec: str) -> str:
    src = variant_source(spec)
    h = hashlib.sha256(src.replace(GUARD[1], GUARD[0]).encode()).hexdigest()[:16]  # the guard changes no output
    d = os.path.join(CACHE, "variants")
    os.makedirs(d, exist_ok=True)
    lib = os.path.join(d, h + ".so")
    if not os.path.exists(lib):
        c = os.path.join(d, h + ".c")
        with open(c, "w") as f:
            f.write(src)
        wide = int(re.search(r"^#define NIN (\d+) ", src, re.M).group(1)) != 32
        subprocess.run([os.environ.get("CC", "cc"), "-O3", "-march=native"] + (["-mno-avx2"] if wide else [])
                       + ["-fPIC", "-shared", "-fvisibility=hidden", "-o", lib + ".tmp", c], check=True)
        os.replace(lib + ".tmp", lib)
    return lib


def _load(path: str):
    lib = ctypes.CDLL(path)
    cp, sz, i, vp = ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_void_p
    lib.cm_compress.restype = ctypes.c_longlong
    lib.cm_compress.argtypes = [cp, sz, cp, sz, i, i, i, i, cp, sz]
    lib.cm_prime.restype = vp
    lib.cm_prime.argtypes = [cp, sz, i, i, i, i, ctypes.c_ulonglong]
    lib.cm_free_primed.argtypes = [vp]
    lib.cm_size_class.restype = ctypes.c_uint
    lib.cm_size_class.argtypes = [ctypes.c_ulonglong, i]
    lib.cm_worker_new.restype = vp
    lib.cm_worker_free.argtypes = [vp]
    lib.cm_compress_primed.restype = ctypes.c_longlong
    lib.cm_compress_primed.argtypes = [vp, vp, cp, sz, sz, cp, sz]
    return lib


def code_files(lib_path: str, files: list, maxbits: int) -> list:
    """Payload bytes per file (stored files count their size). Files are coded grouped by snapshot key,
    so only one primed snapshot is alive at a time."""
    lib = _load(lib_path)
    worker = lib.cm_worker_new()  # holds a full model: freed at the end
    out = [None] * len(files)

    def key(i):
        e = files[i]
        if e["stored"] or not e["primer"]:
            return (0, -1, 0)
        n = os.path.getsize(e["stream"]) + len(mixz.primer_bytes(e["primer"]))
        return (e["primer"], e["profile"], lib.cm_size_class(n, maxbits))

    snap_key, snap = None, None
    try:
        for i in sorted(range(len(files)), key=key):
            e = files[i]
            if e["stored"]:
                out[i] = e["bytes"]
                continue
            with open(e["stream"], "rb") as f:
                s = f.read()
            primer = mixz.primer_bytes(e["primer"]) if e["primer"] else b""
            cap = len(s) + len(s) // 8 + 4096
            buf = ctypes.create_string_buffer(cap)
            r = -3
            if primer:
                k = key(i)
                if k != snap_key:
                    if snap:
                        lib.cm_free_primed(snap)
                    snap_key = k
                    snap = lib.cm_prime(primer, len(primer), e["profile"], e["stride"], e["bpp"], maxbits,
                                        len(s) + len(primer))
                    if not snap:
                        raise RuntimeError(f"cm_prime failed (out of memory?) on {e['abs']}")
                r = lib.cm_compress_primed(snap, worker, s, len(s), len(primer), buf, cap)
            if r == -3:
                r = lib.cm_compress(s, len(s), primer, len(primer), e["profile"], e["stride"], e["bpp"], maxbits, buf, cap)
            if r == -2:
                r = e["bytes"]
            if r < 0:
                raise RuntimeError(f"engine error {r} on {e['abs']}")
            out[i] = r
    finally:
        if snap:
            lib.cm_free_primed(snap)
        lib.cm_worker_free(worker)
    return out


def _cached(lib_path: str, files: list, maxbits: int, path: str) -> list:
    if not os.path.exists(path):
        r = code_files(lib_path, files, maxbits)
        with open(path + ".tmp", "w") as f:
            json.dump(r, f)
        os.replace(path + ".tmp", path)
    with open(path) as f:
        return json.load(f)


def mem_available() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) << 10
    return 0


def _worker_init():
    resource.setrlimit(resource.RLIMIT_AS, (MEM_CAP, MEM_CAP))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE 0: a crash leaves no core dump or desktop report
    os.nice(10)


def measure(specs: list, files: list, maxbits: int = 20, jobs: int = 3) -> dict:
    """{spec: {kind: [payload bytes per file]}}; each (variant, kind) is one task, run in a fresh process
    (nothing a task allocates outlives it). A task's result is cached on disk (by library, file list and
    maxbits), so an interrupted run resumes."""
    libs = {s: build(s) for s in specs}
    kinds = sorted({e["kind"] for e in files}, key=lambda k: -sum(e["bytes"] for e in files if e["kind"] == k))
    d = os.path.join(CACHE, "results")
    os.makedirs(d, exist_ok=True)
    tasks = {}
    for k in kinds:  # largest kinds first
        fk = [e for e in files if e["kind"] == k]
        fh = hashlib.sha256(json.dumps([e["sha256"] for e in fk]).encode()).hexdigest()[:16]
        for s in specs:
            lh = os.path.basename(libs[s])[:-3]
            tasks[(s, k)] = (libs[s], fk, maxbits, os.path.join(d, f"{lh}-{fh}-{maxbits}.json"))
    free = mem_available()
    fit = (free - MEM_RESERVE) // TASK_RSS
    if fit < 1:
        raise SystemExit(f"only {free >> 20} MB free memory: need {(MEM_RESERVE + TASK_RSS) >> 20} MB")
    if jobs > fit:
        print(f"--jobs {jobs} lowered to {fit}: {free >> 20} MB free memory", file=sys.stderr)
        jobs = fit
    kw = {"max_tasks_per_child": 1} if sys.version_info >= (3, 11) else {}  # a fresh process per task
    ex = ProcessPoolExecutor(jobs, initializer=_worker_init, **kw)
    try:
        futs = {t: ex.submit(_cached, *a) for t, a in tasks.items()}
        while True:  # the first failed or crashed task stops the run
            done, rest = wait(futs.values(), timeout=2, return_when=FIRST_EXCEPTION)
            for f in done:
                if f.exception():
                    raise SystemExit(f"task failed: {f.exception()!r}; finished tasks are cached")
            if not rest:
                break
            if mem_available() < MEM_STOP:
                raise SystemExit(f"stopped: free memory below {MEM_STOP >> 20} MB; finished tasks are cached, "
                                 "rerun to resume")
        res = {s: {} for s in specs}
        for (s, k), f in futs.items():
            res[s][k] = f.result()
        return res
    finally:
        for p in list((ex._processes or {}).values()):
            p.kill()
        ex.shutdown(wait=True, cancel_futures=True)


def report(res: dict, files: list) -> None:
    specs = list(res)
    kinds = [k for k in data.fetch.KINDS if k in res[specs[0]]]
    raw = {k: sum(e["bytes"] for e in files if e["kind"] == k) for k in kinds}
    print(f"{'variant':32s}" + "".join(f"{k:>9s}" for k in kinds) + "    total")
    base = {k: sum(res[specs[0]][k]) for k in kinds}
    for s in specs:
        row = {k: sum(res[s][k]) for k in kinds}
        if s == specs[0]:
            cells = [f"{row[k] / raw[k]:9.2%}" for k in kinds] + [f"{sum(row.values()) / sum(raw.values()):9.2%}"]
        else:
            cells = [f"{row[k] / base[k] - 1:+9.2%}" for k in kinds] + [f"{sum(row.values()) / sum(base.values()) - 1:+9.2%}"]
        print(f"{s[:32]:32s}" + "".join(cells), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="+")
    ap.add_argument("--split", default="train")
    ap.add_argument("--kinds", default="")
    ap.add_argument("--maxbits", type=int, default=mixz.DEFAULT_MAXBITS)
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--json", help="also write the per-file results here")
    args = ap.parse_args()
    entries = [e for e in data.ensure(args.split) if not args.kinds or e["kind"] in args.kinds.split(",")]
    files = plans(entries)
    res = measure(args.specs, files, args.maxbits, args.jobs)
    report(res, files)
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"files": [e["sha256"] for e in files], "results": res}, f)


if __name__ == "__main__":
    main()
