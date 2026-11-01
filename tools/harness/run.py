#!/usr/bin/env python3
"""Benchmark harness. Every engine or format change is measured with this (plan step 0).

  run.py                    quick tier (~6 MB): measure, compare with this machine's baseline
  run.py --tier full        full tier (~29 MB), for milestones
  run.py --save-baseline    also store the run as this machine's baseline for the tier

What one run measures, per kind and in total:
  size         compressed bytes / input bytes, and the change against the baseline
  round-trip   every output is decompressed (which checks the CRC) and compared with the input
  determinism  outputs are identical with 1 and N worker threads, and with -O0 and -O3 builds
  speed        engine ns/byte on one pinned core, no primer (fastest of 5 runs); whole pipeline MB/s
               on all cores, and on 1 pinned core with a time breakdown by stage (full tier, or
               --one-core), compress and decompress each in a fresh process (includes primer replay,
               detection and transforms); CLI start-up for one small file, without and with 'mixz serve' running
Competitors (gzip -9, xz -9e, zstd -19, zstd -19 with the primer mixz chose as dictionary,
brotli -q11) are computed once per file and cached. With --reference also paq8px -8 and zpaq -m5
(reference.py: pinned sources, slow, cached; reference.py --tier T fills the cache beforehand).

Exit status: 0 ok; 1 broken (round-trip or determinism failed); 2 needs approval
(the total or a kind grew by more than 0.5% against the baseline: CLAUDE.md invariant 4).
Results: results/harness/<machine>/baseline-<tier>.json (committed) and runs/ (local).
"""
import argparse
import gzip
import hashlib
import json
import lzma
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, ROOT)
sys.path.insert(0, TOOLS)
sys.path.insert(0, HERE)
import fetch  # noqa: E402
import reference  # noqa: E402

RESULTS = os.path.join(ROOT, "results", "harness")
SPEED_SRC = os.path.join(HERE, "speed.c")
MIXZ_PY = os.path.join(ROOT, "mixz.py")
SIZE_LIMIT = 0.005  # relative growth that needs approval
SPEED_BYTES = 512 * 1024
SPEED_REPEATS = 5  # the fastest run is reported: other load on the machine only ever slows a run down
STARTUP_TARGET = 28 * 1024  # the start-up figure in docs/context.md is for a 28 KB file
DET_MAX_FILE = 64 * 1024


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


# --------------------------------------------------------------------------
# builds
# --------------------------------------------------------------------------
def codec_source(text_of=None) -> tuple:
    """(source path, function prefix) of the codec mixz.py writes new files with. With text_of(path) ->
    file text (or None), the same for another revision; revisions before codec 2 wrote codec 1."""
    import re
    if text_of is None:
        import mixz
        c = mixz.registry()["codecs"][mixz.CODEC]
        return os.path.join(ROOT, c["source"]), c["prefix"]
    m = re.search(r"^CODEC = (\d+)", text_of("mixz.py") or "", re.M)
    reg = json.loads(text_of("registry.json") or "{}")
    c = reg.get("codecs", {}).get(m.group(1) if m else "1", {})
    return c.get("source", "engine/mixz.c"), c.get("prefix", "mz_")


def speed_defines(prefix: str) -> list:
    """The speed tool calls mz_compress / mz_decompress: map them to the codec's functions."""
    return [] if prefix == "mz_" else [f"-Dmz_compress={prefix}compress", f"-Dmz_decompress={prefix}decompress"]


def build(what: str, opt: list) -> str:
    """Build the engine library (what='lib') or the speed tool (what='speed') into the cache."""
    import mixz
    src, prefix = codec_source()
    srcs = mixz.engine_files() if what == "lib" else [SPEED_SRC, src]
    key = sha(b"".join(read(s) for s in srcs) + " ".join(opt + (speed_defines(prefix) if what == "speed" else [])).encode())[:16]
    out = os.path.join(fetch.cache_dir(), "build", f"{what}-{key}" + (".so" if what == "lib" else ""))
    if not os.path.exists(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if what == "lib":
            r = mixz.build_engine(out + ".tmp", opt)
            if r.returncode != 0:
                raise subprocess.CalledProcessError(r.returncode, "cc", r.stdout, r.stderr)
        else:
            cc = os.environ.get("CC", "cc")
            subprocess.run([cc, *opt, *speed_defines(prefix), "-o", out + ".tmp", SPEED_SRC, src], check=True,
                           capture_output=True)
        os.replace(out + ".tmp", out)
    return out


def release_flags() -> list:
    """The flags mixz.py builds with: -O3 -march=native, or -O3 where the compiler rejects -march."""
    for opt in (["-O3", "-march=native"], ["-O3"]):
        try:
            build("lib", opt)
            return opt
        except subprocess.CalledProcessError:
            continue
    raise SystemExit("cannot build the engine")


# --------------------------------------------------------------------------
# machine description
# --------------------------------------------------------------------------
def _sysfs(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return "?"


def fingerprint(opt: list) -> dict:
    cpu = "?"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        cpu = platform.processor() or "?"
    cc = os.environ.get("CC", "cc")
    ccv = subprocess.run([cc, "--version"], capture_output=True, text=True).stdout.splitlines()[:1]
    mods = {}
    for m in ("numpy", "zstandard", "brotli"):
        try:
            mods[m] = __import__(m).__version__
        except ImportError:
            mods[m] = None
    return {"cpu": cpu, "cpus": os.cpu_count(), "kernel": platform.release(), "os": platform.platform(),
            "cc": ccv[0] if ccv else "?", "cflags": " ".join(opt), "python": platform.python_version(),
            "zlib": zlib.ZLIB_RUNTIME_VERSION, "modules": mods,
            "governor": _sysfs("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"),
            "boost": _sysfs("/sys/devices/system/cpu/cpufreq/boost"),
            "loadavg_start": os.getloadavg()[0], **memory_state()}


def memory_state() -> dict:
    kb = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                kb[k] = int(v.split()[0])
    except (OSError, ValueError):
        return {}
    return {"mem_available_gb": round(kb.get("MemAvailable", 0) / 2**20, 2),
            "swap_used_gb": round((kb.get("SwapTotal", 0) - kb.get("SwapFree", 0)) / 2**20, 2)}


def machine_slug(fp: dict) -> str:
    s = "".join(ch if ch.isalnum() else "-" for ch in fp["cpu"].lower())
    s = "-".join(p for p in s.split("-") if p and p not in ("processor", "cpu", "with", "radeon", "graphics"))
    return f"{s}-{fp['cpus']}cpu"


def git_state() -> dict:
    def g(*a):
        r = subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else None
    status = g("status", "--porcelain", "--untracked-files=no", "--", "engine", "mixz.py", "primers", "tools")  # code only
    return {"commit": g("rev-parse", "--short=12", "HEAD"), "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status)}


# --------------------------------------------------------------------------
# workers (run in their own process so each measurement starts cold)
# --------------------------------------------------------------------------
def encoding(blob: bytes) -> str:
    import mixz
    h = mixz.parse_header(blob)
    if h.method == mixz.M_STORED:
        return "stored"
    return f"{mixz.TRANSFORM_NAMES[h.transform]}/{mixz.PROFILE_NAMES[h.profile]}/{mixz.PRIMER_IDS[h.primer] or '-'}"


def worker(args) -> None:
    import mixz
    # snapshots: prime once per primer, as batch mode does; without them each file replays its primer,
    # which runs in parallel (snapshot priming holds a global lock), so the slow -O0 check uses that
    mixz.USE_SNAPSHOTS = not args.no_snapshots
    mixz.engine()
    # inclusive time per stage; exact with 1 thread, a sum over threads otherwise
    stages = {}

    def timed(name, fn):
        def w(*a, **k):
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                stages[name] = stages.get(name, 0.0) + time.perf_counter() - t
        return w
    for name in ("_acquire_snapshot", "choose_primer", "make_plan", "cm_encode", "cm_decode", "inverse_transform"):
        setattr(mixz, name, timed(name, getattr(mixz, name)))
    files = fetch.ensure(args.tier)
    idx = [int(i) for i in args.only.split(",")] if args.only else list(range(len(files)))
    paths = [os.path.join(args.outdir, f"{i}.mxz") for i in idx]
    if args.worker == "compress":
        datas = [read(files[i]["abs"]) for i in idx]
        t0, u0 = time.perf_counter(), cpu_seconds()
        with ThreadPoolExecutor(args.jobs) as ex:
            outs = list(ex.map(mixz.compress, datas))
        dt, du = time.perf_counter() - t0, cpu_seconds() - u0
        for p, o in zip(paths, outs):
            with open(p, "wb") as f:
                f.write(o)
        g = stages.get
        res = {"seconds": dt, "cpu_seconds": du, "bytes": sum(map(len, datas)),
               "files": [{"out": len(o), "sha": sha(o)[:16], "enc": encoding(o)} for o in outs],
               "stages": {"priming": g("_acquire_snapshot", 0), "primer choice": g("choose_primer", 0),
                          "coding": g("cm_encode", 0) - g("_acquire_snapshot", 0),
                          "detection + transforms": g("make_plan", 0) - g("choose_primer", 0),
                          "transform check": g("inverse_transform", 0),
                          "other": dt - g("make_plan", 0) - g("cm_encode", 0) - g("inverse_transform", 0)}}
    else:
        blobs = [read(p) for p in paths]

        def dec(b):
            try:
                return mixz.decompress(b)
            except mixz.MixzError:
                return None
        t0, u0 = time.perf_counter(), cpu_seconds()
        with ThreadPoolExecutor(args.jobs) as ex:
            backs = list(ex.map(dec, blobs))
        dt, du = time.perf_counter() - t0, cpu_seconds() - u0
        ok = [b is not None and sha(b) == files[i]["sha256"] for i, b in zip(idx, backs)]
        g = stages.get
        res = {"seconds": dt, "cpu_seconds": du, "bytes": sum(files[i]["bytes"] for i in idx), "ok": ok,
               "stages": {"priming": g("_acquire_snapshot", 0), "coding": g("cm_decode", 0) - g("_acquire_snapshot", 0),
                          "inverse transforms": g("inverse_transform", 0),
                          "other": dt - g("cm_decode", 0) - g("inverse_transform", 0)}}
    json.dump(res, sys.stdout)


def cpu_seconds() -> float:
    """CPU time of this process (all threads, user + system) so far."""
    import resource
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def call_worker(kind, tier, jobs, outdir, lib, cpu=None, only=None, snapshots=True) -> dict:
    os.makedirs(outdir, exist_ok=True)
    cmd = [sys.executable, os.path.abspath(__file__), "--worker", kind, "--tier", tier, "--jobs", str(jobs),
           "--outdir", outdir] + (["--only", ",".join(map(str, only))] if only else []) + \
        ([] if snapshots else ["--no-snapshots"])
    if cpu is not None:
        cmd = ["taskset", "-c", str(cpu)] + cmd
    r = subprocess.run(cmd, env=dict(os.environ, MIXZ_LIB=lib), capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"worker {kind} failed:\n{r.stderr}")
    return json.loads(r.stdout)


# --------------------------------------------------------------------------
# measurements in the main process
# --------------------------------------------------------------------------
def speed_streams(files: list) -> list:
    """(name, profile, stride, bpp, bytes): fixed model inputs built from the quick tier."""
    import mixz
    quick = [e for e in files if e["tier"] == "quick"]
    out = [("text", mixz.P_TEXT, 0, 1, b"".join(read(e["abs"]) for e in quick if e["kind"] == "py")[:SPEED_BYTES]),
           ("binary", mixz.P_BIN, 0, 1,
            b"".join(mixz.e8e9(read(e["abs"]), False) for e in quick if e["kind"] == "elf")[:SPEED_BYTES])]
    for e in quick:
        if e["kind"] == "png":
            r = mixz.png_forward(read(e["abs"]))
            if r:
                pix, _, rowbytes, bpp = r
                n = min(len(pix), SPEED_BYTES) // rowbytes * rowbytes
                if n >= 64 * 1024:
                    out.append(("image", mixz.P_IMAGE, rowbytes, bpp, pix[:n]))
                    break
    return out


def build_ref_speed(ref: str, opt: list) -> str:
    """The speed tool built with the engine source of the codec git revision `ref` wrote, for A/B runs."""
    def show(path):
        r = subprocess.run(["git", "-C", ROOT, "show", f"{ref}:{path}"], capture_output=True)
        return r.stdout if r.returncode == 0 else None
    path, prefix = codec_source(lambda p: (show(p) or b"").decode() or None)
    src = show(path)
    if src is None:
        raise SystemExit(f"--ab {ref}: no {path} in that revision")
    defs = speed_defines(prefix)
    key = sha(src + read(SPEED_SRC) + " ".join(opt + defs).encode())[:16]
    d = os.path.join(fetch.cache_dir(), "build")
    out = os.path.join(d, f"speed-ref-{key}")
    if not os.path.exists(out):
        os.makedirs(d, exist_ok=True)
        csrc = os.path.join(d, f"ref-{key}.c")
        with open(csrc, "wb") as f:
            f.write(src)
        cc = os.environ.get("CC", "cc")
        subprocess.run([cc, *opt, *defs, "-o", out + ".tmp", SPEED_SRC, csrc], check=True, capture_output=True)
        os.replace(out + ".tmp", out)
    return out


def engine_speed(bins: list, files: list, cpu: int, tmp: str) -> list:
    """Engine speed of each speed tool in `bins` on the same inputs. With one tool, one process
    does all repeats (as the baselines were measured). With two (an A/B run), single runs
    alternate between the tools, so the machine's slow drift affects both alike."""
    res = [{} for _ in bins]
    for name, prof, stride, bpp, data in speed_streams(files):
        if len(data) < 64 * 1024:
            continue  # too short to time
        p = os.path.join(tmp, f"speed-{name}.bin")
        with open(p, "wb") as f:
            f.write(data)
        args = [p, str(prof), str(stride), str(bpp), "20"]
        runs, rc = [[] for _ in bins], [0 for _ in bins]
        for i, b in (enumerate(bins) if len(bins) == 1 else []):
            r = subprocess.run(["taskset", "-c", str(cpu), b, *args, str(SPEED_REPEATS)], capture_output=True, text=True)
            rc[i], runs[i] = r.returncode, [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        for _ in (range(SPEED_REPEATS) if len(bins) > 1 else []):
            for i, b in enumerate(bins):
                r = subprocess.run(["taskset", "-c", str(cpu), b, *args, "1"], capture_output=True, text=True)
                rc[i] |= r.returncode
                runs[i] += [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        for i, rr in enumerate(runs):
            ok = rc[i] == 0 and len(rr) == SPEED_REPEATS and len({x["fnv"] for x in rr}) == 1
            res[i][name] = {"bytes": len(data), "out": rr[0]["out"] if rr else None, "fnv": rr[0]["fnv"] if rr else None,
                            "c_ns_per_byte": [x["c_ns_per_byte"] for x in rr],
                            "d_ns_per_byte": [x["d_ns_per_byte"] for x in rr],
                            "c_cpu_ns_per_byte": [x["c_cpu_ns_per_byte"] for x in rr],
                            "d_cpu_ns_per_byte": [x["d_cpu_ns_per_byte"] for x in rr], "ok": ok}
    return res


def engine_ns(v: dict, phase: str) -> float:
    """Headline engine speed: the fastest run's CPU time per byte (wall time for runs that predate it)."""
    return min(v.get(f"{phase}_cpu_ns_per_byte") or v[f"{phase}_ns_per_byte"])


def print_ab(cur: dict, ref: dict, ref_name: str) -> None:
    print(f"  engine A/B against {ref_name}, alternating runs in this run (negative = faster):")
    for name, v in cur.items():
        r = ref.get(name)
        if not r:
            continue
        c, rc, d, rd = (engine_ns(v, "c"), engine_ns(r, "c"), engine_ns(v, "d"), engine_ns(r, "d"))
        same = "same output" if v["fnv"] == r["fnv"] else "different output"
        print(f"    {name:6s} compress {c:7.0f} vs {rc:7.0f} ns/B {fmt_delta(pct(c, rc))}   "
              f"decompress {d:7.0f} vs {rd:7.0f} ns/B {fmt_delta(pct(d, rd))}   ({same})")


def startup(files: list, lib: str, tmp: str) -> dict:
    py = [e for e in files if e["kind"] == "py" and e["tier"] == "quick"]
    e = min(py, key=lambda x: (abs(x["bytes"] - STARTUP_TARGET), x["path"]))
    out, back = os.path.join(tmp, "startup.mxz"), os.path.join(tmp, "startup.out")
    env = dict(os.environ, MIXZ_LIB=lib)
    env.pop("MIXZ_SOCKET", None)
    import resource

    def child_cpu():
        r = resource.getrusage(resource.RUSAGE_CHILDREN)
        return r.ru_utime + r.ru_stime
    tc, td, uc, ud = [], [], [], []
    for cmd, times, cpus in (([sys.executable, MIXZ_PY, "c", e["abs"], "-o", out, "-f", "--no-server"], tc, uc),
                             ([sys.executable, MIXZ_PY, "d", out, "-o", back, "-f", "--no-server"], td, ud)):
        for _ in range(3):
            t0, u0 = time.perf_counter(), child_cpu()
            subprocess.run(cmd, env=env, check=True, capture_output=True)
            times.append(time.perf_counter() - t0)
            cpus.append(child_cpu() - u0)
    res = {"file": f"{e['source']}/{e['path']}", "bytes": e["bytes"], "out": os.path.getsize(out),
           "ok": read(back) == read(e["abs"]), "compress_s": tc, "decompress_s": td,
           "compress_cpu_s": uc, "decompress_cpu_s": ud}
    # the same commands with 'mixz serve' running (warmed by one call each): wall clock, since the work
    # happens in the server process; its output must be identical
    sock = os.path.join(tmp, "serve.sock")
    senv = dict(env, MIXZ_SOCKET=sock)
    srv = subprocess.Popen([sys.executable, MIXZ_PY, "serve", "-j", "1", "--models", "2"], env=senv,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sout, sback = os.path.join(tmp, "serve.mxz"), os.path.join(tmp, "serve.out")
    sc, sd = [], []
    try:
        for _ in range(600):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        for cmd, times in (([sys.executable, MIXZ_PY, "c", e["abs"], "-o", sout, "-f"], sc),
                           ([sys.executable, MIXZ_PY, "d", sout, "-o", sback, "-f"], sd)):
            for i in range(4):
                t0 = time.perf_counter()
                r = subprocess.run(cmd, env=senv, check=True, capture_output=True, text=True)
                if i:
                    times.append(time.perf_counter() - t0)
                if r.stderr:  # the client fell back to working without the server
                    raise RuntimeError(f"server not used: {r.stderr.strip()}")
        res.update(server_compress_s=sc, server_decompress_s=sd,
                   server_ok=read(sout) == read(out) and read(sback) == read(e["abs"]))
    finally:
        srv.terminate()
        srv.wait()
    return res


def competitors(files: list, encs: list) -> list:
    """Sizes per file for the other tools; cached by tool version and content."""
    import mixz
    try:
        import zstandard
    except ImportError:
        zstandard = None
    try:
        import brotli
    except ImportError:
        brotli = None
    cache_path = os.path.join(fetch.cache_dir(), "competitors.json")
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    tools = {"gzip -9": (f"zlib{zlib.ZLIB_RUNTIME_VERSION}", lambda d, pid: gzip.compress(d, 9, mtime=0)),
             "xz -9e": (f"py{platform.python_version()}",
                        lambda d, pid: lzma.compress(d, preset=9 | lzma.PRESET_EXTREME))}
    if zstandard:
        zv = "zstd" + ".".join(map(str, zstandard.ZSTD_VERSION))
        dicts = {}

        def zstd_dict(d, pid):
            if not pid:
                return zstandard.ZstdCompressor(level=19).compress(d)
            if pid not in dicts:
                dicts[pid] = zstandard.ZstdCompressionDict(mixz.primer_bytes(pid),
                                                           dict_type=zstandard.DICT_TYPE_RAWCONTENT)
            return zstandard.ZstdCompressor(level=19, dict_data=dicts[pid]).compress(d)
        tools["zstd -19"] = (zv, lambda d, pid: zstandard.ZstdCompressor(level=19).compress(d))
        if any(mixz.primer_bytes(pid) for pid in range(1, len(mixz.PRIMER_IDS))):  # else it is zstd -19
            tools["zstd -19 +primer dict"] = (zv, zstd_dict)
    if brotli:
        tools["brotli -q11"] = (f"brotli{brotli.__version__}", lambda d, pid: brotli.compress(d, quality=11))
    jobs = []
    for e, enc in zip(files, encs):
        primer = enc.rsplit("/", 1)[-1] if enc != "stored" else "-"
        pid = mixz.PRIMER_IDS.index(primer) if primer in mixz.PRIMER_IDS else 0
        for name, (ver, fn) in tools.items():
            key = f"{name}|{ver}|{pid if 'primer' in name else ''}|{e['sha256']}"
            if key not in cache:
                jobs.append((key, e["abs"], pid, fn))

    def one(job):
        key, path, pid, fn = job
        return key, len(fn(read(path), pid))
    with ThreadPoolExecutor(os.cpu_count() or 1) as ex:
        for key, n in ex.map(one, jobs):
            cache[key] = n
    if jobs:
        with open(cache_path + ".tmp", "w") as f:
            json.dump(cache, f)
        os.replace(cache_path + ".tmp", cache_path)
    out = []
    for e, enc in zip(files, encs):
        primer = enc.rsplit("/", 1)[-1] if enc != "stored" else "-"
        pid = mixz.PRIMER_IDS.index(primer) if primer in mixz.PRIMER_IDS else 0
        out.append({name: cache[f"{name}|{ver}|{pid if 'primer' in name else ''}|{e['sha256']}"]
                    for name, (ver, _) in tools.items()})
    return out


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
def median(xs):
    return statistics.median(xs) if xs else None


def pct(new, old):
    return None if not old or new is None else (new / old - 1) * 100


def fmt_delta(d):
    if d is None:
        return "     –"
    return f"{d:+6.2f}%"


def summarize(res: dict) -> dict:
    """Headline numbers used for comparisons."""
    s = {"kinds": {k: {"bytes": v["bytes"], "out": v["out"]} for k, v in res["kinds"].items()},
         "total": res["total"], "engine": {}, "pipeline": {}}
    for name, v in res["speed"]["engine"].items():
        s["engine"][name] = {"c_ns_per_byte": engine_ns(v, "c"), "d_ns_per_byte": engine_ns(v, "d")}
    for name in ("pipeline_1", "pipeline_n"):
        p = res["speed"][name]
        if p:
            s["pipeline"][name] = {"c_mb_s": p["bytes"] / p["compress_s"] / 1e6, "d_mb_s": p["bytes"] / p["decompress_s"] / 1e6}
            if p.get("compress_cpu_s"):  # MB per CPU-second: the cost measure, free of other programs' time
                s["pipeline"][name].update(c_mb_cpu_s=p["bytes"] / p["compress_cpu_s"] / 1e6,
                                           d_mb_cpu_s=p["bytes"] / p["decompress_cpu_s"] / 1e6)
    st = res["speed"]["startup"]
    s["startup"] = {"compress_s": median(st["compress_s"]), "decompress_s": median(st["decompress_s"])}
    if st.get("compress_cpu_s"):
        s["startup"].update(compress_cpu_s=min(st["compress_cpu_s"]), decompress_cpu_s=min(st["decompress_cpu_s"]))
    if st.get("server_compress_s"):
        s["startup"].update(server_compress_s=median(st["server_compress_s"]),
                            server_decompress_s=median(st["server_decompress_s"]))
    return s


def report(res: dict, base) -> int:
    s = summarize(res)
    b = summarize(base) if base else None
    comparable = bool(base) and base["set_digest"] == res["set_digest"]
    same_machine = bool(base) and base["machine"]["cpu"] == res["machine"]["cpu"]
    g = res["git"]
    print(f"\nmixz harness: tier {res['tier']}, {res['total']['files']} files, {res['total']['bytes'] / 1e6:.2f} MB, "
          f"commit {g['commit']}{' (uncommitted changes)' if g['dirty'] else ''}")
    print(f"machine: {res['machine']['cpu']}, {res['machine']['cpus']} CPUs, {res['machine']['cc']}, "
          f"{res['machine']['cflags']}, governor {res['machine']['governor']}, load {res['machine']['loadavg_start']:.2f}, "
          f"memory available {res['machine'].get('mem_available_gb', '?')} GB, swap used {res['machine'].get('swap_used_gb', '?')} GB")
    if base and not comparable:
        print("baseline: test set differs from the baseline's: sizes are NOT comparable")
    elif base:
        print(f"baseline: {base['time']} commit {base['git']['commit']}")
        diff = [k for k in ("cpu", "cc", "cflags", "kernel", "governor", "boost", "python", "zlib")
                if base["machine"].get(k) != res["machine"].get(k)]
        if diff:
            print("  differs from the baseline in: " + ", ".join(f"{k} ({base['machine'].get(k)} -> {res['machine'].get(k)})" for k in diff))
    else:
        print("baseline: none for this machine and tier (use --save-baseline)")
    tools = list(res["kinds"][next(iter(res["kinds"]))]["competitors"])
    head = f"\n{'kind':6s} {'files':>5s} {'MB':>6s} {'mixz':>7s} {'vs base':>8s}  " + "  ".join(f"{t:>9.9s}" for t in tools)
    print(head)
    verdict, notes = 0, []
    rows = list(res["kinds"].items()) + [("total", res["total"])]
    for k, v in rows:
        d = None
        if comparable:
            bo = b["kinds"][k]["out"] if k != "total" else b["total"]["out"]
            d = pct(v["out"], bo)
            if d is not None and d > SIZE_LIMIT * 100:
                verdict = max(verdict, 2)
                notes.append(f"{k} is {d:+.2f}% larger than the baseline")
        comp = "  ".join(f"{v['competitors'][t] / v['bytes']:9.1%}" for t in tools)
        print(f"{k:6s} {v['files']:5d} {v['bytes'] / 1e6:6.2f} {v['out'] / v['bytes']:7.2%} {fmt_delta(d):>8s}  {comp}")
    if comparable:
        same = sum(x.split(" ", 2)[1] == y.split(" ", 2)[1] for x, y in zip(res["files"], base["files"]))
        print(f"\noutputs byte-identical to the baseline: {same}/{len(res['files'])} files")
    rt = res["roundtrip"]
    det = res["determinism"]
    runs = f"{res['speed']['pipeline_n']['jobs']} threads" + (" and 1 thread" if res["speed"]["pipeline_1"] else "")
    print(f"\nround-trip: {'OK' if rt['ok'] else 'FAILED'} ({rt['passed']}/{rt['checked']} decompressions: {runs}, CLI)")
    threads = {None: "not run", True: "OK", False: "DIFFERENT"}[det["threads_ok"]]
    print(f"determinism: 1 vs {res['speed']['pipeline_n']['jobs']} threads {threads}; "
          f"-O0 vs {res['machine']['cflags']} {'OK' if det['o0_ok'] else 'DIFFERENT'} ({det['o0_files']} files); "
          f"engine repeats {'OK' if det['engine_ok'] else 'DIFFERENT'}")
    if not rt["ok"] or det["threads_ok"] is False or not (det["o0_ok"] and det["engine_ok"]):
        verdict = 1
        notes.insert(0, "round-trip or determinism failed")
    sp = res["speed"]
    print(f"\nspeed (lower ns/B and higher MB/s are better; deltas vs baseline{'' if same_machine else ': other machine, not shown'})")
    if base and base["code"]["engine_sha256"] == res["code"]["engine_sha256"]:
        print("  engine source identical to the baseline's: no engine speed change to measure")
    elif not sp.get("engine_ab"):
        print("  engine source differs from the baseline's: run with --ab REV for its speed change")
    for name, v in s["engine"].items():
        e = sp["engine"][name]
        xs = e.get("c_cpu_ns_per_byte") or e["c_ns_per_byte"]
        spread = (max(xs) - min(xs)) / min(xs) * 100
        print(f"  engine {name:6s} 1 core   compress {v['c_ns_per_byte']:7.0f} ns/B   decompress {v['d_ns_per_byte']:7.0f} ns/B"
              f"   (CPU time, fastest of {len(xs)}, spread {spread:.1f}%, {e['bytes'] // 1024} KiB)")
    for name, label in (("pipeline_1", "1 core  "), ("pipeline_n", f"{sp['pipeline_n']['jobs']} cores ")):
        v = s["pipeline"].get(name)
        if not v:
            print(f"  pipeline     {label} not run (quick tier; --one-core runs it)")
            continue
        bp = b["pipeline"].get(name) if b and same_machine and comparable else None
        cpu = name == "pipeline_1" and "c_mb_cpu_s" in v and bool(bp) and "c_mb_cpu_s" in bp
        kc, kd = ("c_mb_cpu_s", "d_mb_cpu_s") if cpu else ("c_mb_s", "d_mb_s")
        dc = pct(v[kc], bp[kc]) if bp else None
        dd = pct(v[kd], bp[kd]) if bp else None
        note = "   (deltas on CPU time)" if cpu else ("   (wall clock: noisy on a shared machine)" if bp else "")
        print(f"  pipeline     {label} compress {v['c_mb_s']:7.3f} MB/s {fmt_delta(dc)}   "
              f"decompress {v['d_mb_s']:7.3f} MB/s {fmt_delta(dd)}{note}")
    if sp.get("engine_ab"):
        print_ab(sp["engine"], sp["engine_ab"]["engine"], sp["engine_ab"]["ref"])
    if sp["pipeline_1"]:
        for phase in ("compress", "decompress"):
            stg = sp["pipeline_1"][f"{phase}_stages"]
            tot = sp["pipeline_1"][f"{phase}_s"]
            print(f"    1-core {phase:10s} time: " + ", ".join(f"{k} {v / tot:.0%}" for k, v in stg.items()))
    st = sp["startup"]
    v = s["startup"]
    k = "_cpu_s" if same_machine and "compress_cpu_s" in v and "compress_cpu_s" in b["startup"] else "_s"
    dc = pct(v["compress" + k], b["startup"]["compress" + k]) if same_machine else None
    dd = pct(v["decompress" + k], b["startup"]["decompress" + k]) if same_machine else None
    print(f"  start-up     CLI, {st['bytes'] // 1024} KB py: compress {v['compress_s']:.2f} s {fmt_delta(dc)}   "
          f"decompress {v['decompress_s']:.2f} s {fmt_delta(dd)}")
    if "server_compress_s" in v:
        bs = b["startup"] if b else {}
        dc = pct(v["server_compress_s"], bs["server_compress_s"]) if same_machine and "server_compress_s" in bs else None
        dd = pct(v["server_decompress_s"], bs["server_decompress_s"]) if same_machine and "server_decompress_s" in bs else None
        print(f"  start-up     CLI with 'mixz serve' running: compress {v['server_compress_s']:.3f} s {fmt_delta(dc)}   "
              f"decompress {v['server_decompress_s']:.3f} s {fmt_delta(dd)}   (wall clock; output identical: "
              f"{'yes' if st['server_ok'] else 'NO'})")
    print("\nverdict: " + ["OK", "BROKEN", "NEEDS APPROVAL"][verdict] + (": " + "; ".join(notes) if notes else ""))
    return verdict


# --------------------------------------------------------------------------
def run(args) -> int:
    t_start = time.time()
    files = fetch.ensure(args.tier)
    opt = release_flags()
    lib, lib0, speed_bin = build("lib", opt), build("lib", ["-O0"]), build("speed", opt)
    os.environ["MIXZ_LIB"] = lib  # this process uses mixz.e8e9 / png_forward for the speed inputs
    fp = fingerprint(opt)
    mdir = os.path.join(RESULTS, machine_slug(fp))
    base_path = os.path.join(mdir, f"baseline-{args.tier}.json")
    base = json.load(open(base_path)) if os.path.exists(base_path) else None
    engine_sha = sha(read(codec_source()[0]))  # the codec new files are written with
    if not args.ab and base and base["code"]["engine_sha256"] != engine_sha and base["git"]["commit"]:
        args.ab = base["git"]["commit"]  # the engine changed: measure its speed against the baseline's, side by side
    ref_bin = build_ref_speed(args.ab, opt) if args.ab else None
    jobs = args.jobs or os.cpu_count() or 1
    if args.engine_only:  # quick loop for engine work: no sizes, no pipeline, nothing saved
        with tempfile.TemporaryDirectory(prefix="mixz-harness-") as tmp:
            engs = engine_speed([speed_bin] + ([ref_bin] if ref_bin else []), files, (os.cpu_count() or 1) - 1, tmp)
        for name, v in engs[0].items():
            print(f"  engine {name:6s} compress {engine_ns(v, 'c'):7.0f} ns/B   decompress {engine_ns(v, 'd'):7.0f} ns/B (CPU time)"
                  f"   output fnv {v['fnv']}{'' if v['ok'] else '   NOT DETERMINISTIC OR ROUND-TRIP FAILED'}")
        if ref_bin:
            print_ab(engs[0], engs[1], args.ab)
        return 0 if all(v["ok"] for e in engs for v in e.values()) else 1
    one_core = args.one_core or args.tier == "full"
    cpu = (os.cpu_count() or 1) - 1
    det_idx = []
    for kind in fetch.KINDS:  # one small file per kind for the slow -O0 build (the smallest if none is small)
        cand = [i for i, e in enumerate(files) if e["kind"] == kind and e["tier"] == "quick"]
        small = [i for i in cand if files[i]["bytes"] <= DET_MAX_FILE]
        if cand:
            det_idx.append(small[0] if small else min(cand, key=lambda i: files[i]["bytes"]))
    with tempfile.TemporaryDirectory(prefix="mixz-harness-") as tmp:
        say = (lambda m: print(m, file=sys.stderr, flush=True))
        say(f"[{time.time() - t_start:6.1f}s] pipeline, {jobs} threads")
        cn = call_worker("compress", args.tier, jobs, os.path.join(tmp, "n"), lib)
        dn = call_worker("decompress", args.tier, jobs, os.path.join(tmp, "n"), lib)
        c1 = d1 = None
        if one_core:
            say(f"[{time.time() - t_start:6.1f}s] pipeline, 1 thread on CPU {cpu}")
            c1 = call_worker("compress", args.tier, 1, os.path.join(tmp, "1"), lib, cpu=cpu)
            d1 = call_worker("decompress", args.tier, 1, os.path.join(tmp, "1"), lib, cpu=cpu)
        say(f"[{time.time() - t_start:6.1f}s] -O0 build on {len(det_idx)} files")
        c0 = call_worker("compress", args.tier, jobs, os.path.join(tmp, "o0"), lib0, only=det_idx, snapshots=False)
        say(f"[{time.time() - t_start:6.1f}s] engine speed on CPU {cpu}" + (f", A/B against {args.ab}" if args.ab else ""))
        engs = engine_speed([speed_bin] + ([ref_bin] if ref_bin else []), files, cpu, tmp)
        eng = engs[0]
        say(f"[{time.time() - t_start:6.1f}s] CLI start-up")
        st = startup(files, lib, tmp)
    say(f"[{time.time() - t_start:6.1f}s] competitors")
    encs = [f["enc"] for f in cn["files"]]
    comp = competitors(files, encs)
    if args.reference:
        for c, r in zip(comp, reference.sizes(files)):
            c.update(r)
    kinds = {}
    for e, f, c in zip(files, cn["files"], comp):
        k = kinds.setdefault(e["kind"], {"files": 0, "bytes": 0, "out": 0, "competitors": {}, "encodings": {}})
        k["files"] += 1
        k["bytes"] += e["bytes"]
        k["out"] += f["out"]
        k["encodings"][f["enc"]] = k["encodings"].get(f["enc"], 0) + 1
        for t, n in c.items():
            k["competitors"][t] = k["competitors"].get(t, 0) + n
    total = {"files": len(files), "bytes": sum(k["bytes"] for k in kinds.values()),
             "out": sum(k["out"] for k in kinds.values()), "competitors": {}}
    for k in kinds.values():
        for t, n in k["competitors"].items():
            total["competitors"][t] = total["competitors"].get(t, 0) + n
    checked = dn["ok"] + (d1["ok"] if d1 else []) + [st["ok"]] + [st.get("server_ok", True)]
    res = {
        "harness_version": 1, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "tier": args.tier, "label": args.label,
        "git": git_state(), "machine": fp,
        "code": {"engine_sha256": engine_sha, "mixz_py_sha256": sha(read(MIXZ_PY))},
        "set_digest": sha("\n".join(f"{e['kind']}:{e['sha256']}" for e in files).encode()),
        "kinds": kinds, "total": total,
        "roundtrip": {"ok": all(checked), "checked": len(checked), "passed": sum(checked)},
        "determinism": {"threads_ok": None if not c1 else [f["sha"] for f in c1["files"]] == [f["sha"] for f in cn["files"]],
                        "o0_ok": [f["sha"] for f in c0["files"]] == [cn["files"][i]["sha"] for i in det_idx],
                        "o0_files": len(det_idx), "engine_ok": all(v["ok"] for v in eng.values())},
        "speed": {"cpu": cpu, "engine": eng, "startup": st,
                  "engine_ab": {"ref": args.ab, "engine": engs[1]} if ref_bin else None,
                  "pipeline_1": None if not c1 else {"jobs": 1, "bytes": c1["bytes"], "compress_s": c1["seconds"],
                                                     "decompress_s": d1["seconds"], "compress_cpu_s": c1["cpu_seconds"],
                                                     "decompress_cpu_s": d1["cpu_seconds"], "compress_stages": c1["stages"],
                                                     "decompress_stages": d1["stages"]},
                  "pipeline_n": {"jobs": jobs, "bytes": cn["bytes"], "compress_s": cn["seconds"], "decompress_s": dn["seconds"],
                                 "compress_cpu_s": cn["cpu_seconds"], "decompress_cpu_s": dn["cpu_seconds"]}},
        "reference_cpu_s": reference.cpu_seconds(files) if args.reference else None,
        "files": [f"{f['out']} {f['sha']} {f['enc']}" for f in cn["files"]],  # in sets.json order
        "seconds": round(time.time() - t_start, 1),
    }
    os.makedirs(os.path.join(mdir, "runs"), exist_ok=True)
    verdict = report(res, base)
    run_path = os.path.join(mdir, "runs", time.strftime("%Y%m%d-%H%M%S") + f"-{args.tier}.json")
    with open(run_path, "w") as f:
        json.dump(res, f, indent=1)
    print(f"\nrun saved: {os.path.relpath(run_path, ROOT)} ({res['seconds']:.0f} s)")
    if args.save_baseline:
        if verdict == 1:
            print("not saved as baseline: the run is broken")
        else:
            with open(base_path, "w") as f:
                json.dump(res, f, indent=1)
            print(f"baseline saved: {os.path.relpath(base_path, ROOT)}")
    return verdict


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", choices=fetch.TIERS, default="quick")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("--jobs", type=int, default=0, help="threads for the all-cores run (default: all CPUs)")
    ap.add_argument("--one-core", action="store_true", help="also run the 1-thread pipeline (always on in the full tier)")
    ap.add_argument("--label", default="", help="free text stored with the run")
    ap.add_argument("--ab", metavar="REV", help="also time the engine of git revision REV, alternating runs with the current one")
    ap.add_argument("--engine-only", action="store_true", help="only the engine speed (with --ab: the A/B comparison)")
    ap.add_argument("--reference", action="store_true",
                    help="add paq8px -8 and zpaq -m5 columns (slow the first time: hours for the full tier; cached)")
    ap.add_argument("--worker", choices=["compress", "decompress"], help=argparse.SUPPRESS)
    ap.add_argument("--outdir", help=argparse.SUPPRESS)
    ap.add_argument("--only", help=argparse.SUPPRESS)
    ap.add_argument("--no-snapshots", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.worker:
        worker(args)
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
