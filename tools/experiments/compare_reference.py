#!/usr/bin/env python3
"""mixz against paq8px -8 and zpaq -m5 on whole kinds: each file alone, and one stream per kind.
Adapted from a reviewer's compare.py; that run's numbers are in results/compare/reviewer-xeon-vm.json.

  compare_reference.py [KIND ...]   py js json web prose: the harness quick tier; logs: the 16 Loghub
                                    samples of logs.json (default: all six)

Per kind:
  files alone  gzip -9, xz -9e, zstd -19, brotli -q11, mixz (and mixz without primer when primers are
               installed), zpaq -m5 and paq8px -8 (tools/harness/reference.py, cached per file)
  one stream   mixz bundle (compress_bundle), and zpaq -m5 and paq8px -8 on the files joined; with primers
               installed, for py and json also paq8px -8 given the same primer: size(primer + files) -
               size(primer), the bytes paq8px spends on the files once it has read the primer
mixz times are CPU seconds of a second, warm pass (primed models kept) in a fresh process; the other
tools' times are CPU seconds of their own processes. Every mixz output is decompressed and compared.
Peak memory: one paq8px process (2.4 GB), or mixz with up to 8 primed models (1.4 GB), never both.
Writes results/compare/<machine>-<primers|no-primers>.json."""
import gzip
import hashlib
import json
import lzma
import os
import resource
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tools", "harness"), os.path.join(ROOT, "tools")]
import fetch  # noqa: E402
import reference  # noqa: E402

KINDS = ("py", "js", "json", "web", "prose", "logs")
PRIMER_OF = {"py": "py", "json": "json"}  # kinds where paq8px is also given mixz's primer


def entries(kind: str) -> list:
    if kind != "logs":
        return [e for e in fetch.ensure("quick") if e["kind"] == kind]
    with open(os.path.join(HERE, "logs.json")) as f:
        small = json.load(f)["small"]
    d = os.path.join(fetch.cache_dir(), "logs", "2k")
    os.makedirs(d, exist_ok=True)
    out = []
    for s in small:
        p = os.path.join(d, s["name"])
        if not os.path.exists(p):
            with urllib.request.urlopen(s["url"]) as r, open(p + ".tmp", "wb") as f:
                f.write(r.read())
            os.replace(p + ".tmp", p)
        b = open(p, "rb").read()
        if hashlib.sha256(b).hexdigest() != s["sha256"]:
            raise SystemExit(f"{p}: sha256 differs from logs.json")
        out.append({"abs": p, "bytes": len(b), "sha256": s["sha256"], "kind": "logs"})
    return out


def cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def mixz_part(kind: str) -> dict:
    """mixz sizes and warm CPU times for one kind (run in a fresh process)."""
    import mixz
    mixz.USE_SNAPSHOTS = True
    datas = [open(e["abs"], "rb").read() for e in entries(kind)]
    size, secs = {}, {}
    primers = any(mixz.primer_bytes(i) for i in range(1, len(mixz.PRIMER_IDS)))
    for prim, label in ((False, "mixz no primer"), (True, "mixz")) if primers else ((False, "mixz"),):
        [mixz.compress(d, use_primers=prim) for d in datas]  # first pass primes the models
        t = cpu()
        outs = [mixz.compress(d, use_primers=prim) for d in datas]
        secs[label] = cpu() - t
        size[label] = sum(map(len, outs))
        t = cpu()
        back = [mixz.decompress(o) for o in outs]
        secs[label + " (decompress)"] = cpu() - t
        assert back == datas, f"{kind}: {label} round trip failed"
    mixz.compress_bundle(datas)
    t = cpu()
    b = mixz.compress_bundle(datas)
    secs["mixz bundle"] = cpu() - t
    size["mixz bundle"] = len(b)
    t = cpu()
    assert mixz.decompress_bundle(b) == datas, f"{kind}: bundle round trip failed"
    secs["mixz bundle (decompress)"] = cpu() - t
    pid = mixz.PRIMER_IDS.index(PRIMER_OF[kind]) if kind in PRIMER_OF else 0
    primer = os.path.join(mixz.PRIMER_DIR, mixz.PRIMER_IDS[pid] + ".bin") if pid and mixz.primer_bytes(pid) else None
    return {"size": size, "secs": secs, "primers": primers, "primer": primer}


def main() -> int:
    if sys.argv[1:2] == ["--mixz"]:
        print(json.dumps(mixz_part(sys.argv[2])))
        return 0
    import brotli
    import zstandard
    kinds = sys.argv[1:] or KINDS
    res = {}
    with tempfile.TemporaryDirectory(prefix="mixz-compare-") as tmp:
        for kind in kinds:
            es = entries(kind)
            datas = [open(e["abs"], "rb").read() for e in es]
            r = {"files": len(es), "bytes": sum(map(len, datas)), "size": {}, "secs": {}}
            for name, fn in (("gzip -9", lambda d: gzip.compress(d, 9, mtime=0)),
                             ("xz -9e", lambda d: lzma.compress(d, preset=9 | lzma.PRESET_EXTREME)),
                             ("zstd -19", lambda d: zstandard.ZstdCompressor(level=19).compress(d)),
                             ("brotli -q11", lambda d: brotli.compress(d, quality=11))):
                t = cpu()
                r["size"][name] = sum(len(fn(d)) for d in datas)
                r["secs"][name] = cpu() - t
            m = json.loads(subprocess.run([sys.executable, __file__, "--mixz", kind], check=True, capture_output=True,
                                          text=True).stdout)
            r["size"].update(m["size"])
            r["secs"].update(m["secs"])
            # the reference tools: each file alone, then the kind's files joined (and after the primer)
            joined = os.path.join(tmp, kind + ".joined")
            with open(joined, "wb") as f:
                f.write(b"".join(datas))
            solid = [{"abs": joined, "bytes": r["bytes"], "sha256": hashlib.sha256(b"".join(datas)).hexdigest()}]
            if m["primer"]:
                pb = open(m["primer"], "rb").read()
                with open(joined + ".primed", "wb") as f:
                    f.write(pb + b"".join(datas))
                solid += [{"abs": m["primer"], "bytes": len(pb), "sha256": hashlib.sha256(pb).hexdigest()},
                          {"abs": joined + ".primed", "bytes": len(pb) + r["bytes"],
                           "sha256": hashlib.sha256(pb + b"".join(datas)).hexdigest()}]
            say = (lambda msg: print(msg, file=sys.stderr, flush=True))
            cache = reference.fill(es + solid, say=say)
            for t in reference.TOOLS:
                k = [cache[reference.key(t, e["sha256"])] for e in es]
                r["size"][t] = sum(x[0] for x in k)
                r["secs"][t] = sum(x[1] for x in k)
                s = cache[reference.key(t, solid[0]["sha256"])]
                r["size"][t + " (one stream)"], r["secs"][t + " (one stream)"] = s
            if m["primer"]:
                p, pf = (cache[reference.key("paq8px -8", e["sha256"])] for e in solid[1:])
                r["size"]["paq8px -8 (one stream, after the primer)"] = pf[0] - p[0]
                r["secs"]["paq8px -8 (one stream, after the primer)"] = pf[1] - p[1]
            res[kind] = r
            print(json.dumps({kind: r}), flush=True)
    import run  # the harness, for the machine name
    primers = m["primers"]
    out = os.path.join(ROOT, "results", "compare",
                       f"{run.machine_slug(run.fingerprint([]))}-{'primers' if primers else 'no-primers'}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "primers": primers, "kinds": res}, f, indent=1)
    for kind, r in res.items():
        n = r["bytes"]
        print(f"\n{kind}: {r['files']} files, {n / 1e6:.2f} MB")
        for t, s in r["size"].items():
            print(f"  {t:42s} {s / n:7.2%}  {r['secs'][t]:8.1f} CPU s")
    print(f"\nsaved {os.path.relpath(out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
