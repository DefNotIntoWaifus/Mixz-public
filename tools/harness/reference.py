#!/usr/bin/env python3
"""Reference compressors for the harness: paq8px -8 and zpaq -m5, built from pinned sources.

They show where mixz stands against the strongest open context-mixing compressors. paq8px -8 is
about 100 times slower than mixz (measured) and needs up to 2.4 GB per process, so run.py includes these columns only
with --reference; sizes and CPU times are cached per tool version and file content.

  reference.py --tier full     fill the cache for a tier (one process per file, as many in
                               parallel as memory allows); run.py --reference then reads it

paq8px -8: v217, built with -DNATIVECPU=ON; each file is compressed alone, as mixz does.
zpaq -m5: libzpaq 7.15, method "5", streaming format with no file name, comment or SHA-1 (the
archiver's journal adds about 100-200 more bytes per file). zpaq stores its model description
in every file, about 290 bytes (measured), which dominates on small files.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch  # noqa: E402

SOURCES = {
    "paq8px": {"version": "v217", "url": "https://github.com/hxim/paq8px/archive/refs/tags/v217.tar.gz",
               "sha256": "d00abfbc46cd926e88e37698abbb743267e53c282dec9ab21b31dde8f9314a58", "license": "GPL-3.0"},
    "zpaq": {"version": "7.15", "url": "https://github.com/zpaq/zpaq/releases/download/7.15/zpaq715.zip",
             "sha256": "e85ec2529eb0ba22ceaeabd461e55357ef099b80f61c14f377b429ea3d49d418", "license": "public domain"},
}
TOOLS = {"paq8px -8": "paq8px", "zpaq -m5": "zpaq"}
PEAK_MB = {"paq8px": 2500, "zpaq": 300}  # most memory one process can take (paq8px -8: 2433 MB by its help text; zpaq measured 102 MB)
RESERVE_MB = 4096  # free memory kept when sizing parallel runs (CLAUDE.md); run under a watchdog as well

ZPAQ_STREAM = r"""// zpaq_stream METHOD <in >out: libzpaq streaming format, no file name, comment or SHA-1
#include "libzpaq.h"
#include <cstdio>
#include <cstdlib>
void libzpaq::error(const char* msg) { fprintf(stderr, "zpaq error: %s\n", msg); exit(1); }
struct In: libzpaq::Reader { FILE* f; int get() { return getc(f); } };
struct Out: libzpaq::Writer { FILE* f; void put(int c) { putc(c, f); } };
int main(int argc, char** argv) {
  if (argc != 2) { fprintf(stderr, "usage: zpaq_stream METHOD|d <in >out\n"); return 2; }
  In in; in.f = stdin; Out out; out.f = stdout;
  if (argv[1][0] == 'd') libzpaq::decompress(&in, &out);
  else libzpaq::compress(&in, &out, argv[1], 0, 0, false);
  return fflush(stdout) != 0;
}
"""


def ref_dir() -> str:
    return os.path.join(fetch.cache_dir(), "reference")


def _download(name: str) -> str:
    s = SOURCES[name]
    path = os.path.join(ref_dir(), "dl", os.path.basename(s["url"]))
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with urllib.request.urlopen(s["url"]) as r, open(path + ".tmp", "wb") as f:
            shutil.copyfileobj(r, f)
        os.replace(path + ".tmp", path)
    if hashlib.sha256(open(path, "rb").read()).hexdigest() != s["sha256"]:
        raise SystemExit(f"{path}: sha256 differs from the pinned one")
    return path


def binary(name: str) -> str:
    """Path of the built tool, building it from the pinned source on first use."""
    exe = os.path.join(ref_dir(), "bin", f"{name}-{SOURCES[name]['version']}")
    if os.path.exists(exe):
        return exe
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    arc = _download(name)
    src = os.path.join(ref_dir(), "src", f"{name}-{SOURCES[name]['version']}")
    shutil.rmtree(src, ignore_errors=True)
    os.makedirs(src)
    if name == "paq8px":
        safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}  # Python 3.12, or 3.9.17+
        tarfile.open(arc).extractall(src, **safe)
        top = os.path.join(src, os.listdir(src)[0])
        build = os.path.join(src, "build-release")
        os.makedirs(build)
        subprocess.run(["cmake", "-DCMAKE_BUILD_TYPE=Release", "-DNATIVECPU=ON", top], cwd=build, check=True,
                       capture_output=True)
        subprocess.run(["make", "-j2"], cwd=build, check=True, capture_output=True)
        shutil.copy(os.path.join(build, "paq8px"), exe)
    else:
        zipfile.ZipFile(arc).extractall(src)
        with open(os.path.join(src, "zpaq_stream.cpp"), "w") as f:
            f.write(ZPAQ_STREAM)
        subprocess.run(["g++", "-O3", "-march=native", "-Dunix", "-pthread", "-I", src, "-o", exe,
                        os.path.join(src, "zpaq_stream.cpp"), os.path.join(src, "libzpaq.cpp")], check=True,
                       capture_output=True)
    return exe


def _command(tool: str, exe: str, src: str, out: str) -> list:
    if tool == "paq8px -8":
        return [exe, "-8", src, out]
    return ["sh", "-c", 'exec "$0" 5 <"$1" >"$2"', exe, src, out]


def _avail_mb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def _rss_mb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def cache_path() -> str:
    return os.path.join(fetch.cache_dir(), "reference.json")


def key(tool: str, sha256: str) -> str:
    return f"{tool}|{SOURCES[TOOLS[tool]]['version']}|{sha256}"


def fill(files: list, tools=tuple(TOOLS), jobs: int = 0, say=None) -> dict:
    """Compute missing cache entries: {key: [compressed bytes, CPU seconds]}."""
    path = cache_path()
    cache = json.load(open(path)) if os.path.exists(path) else {}
    todo = [(t, e) for t in tools for e in files if key(t, e["sha256"]) not in cache]
    if not todo:
        return cache
    exes = {t: binary(TOOLS[t]) for t in tools}
    jobs = jobs or os.cpu_count() or 1
    todo.sort(key=lambda te: te[1]["bytes"])  # large files first (popped from the end)
    running, done, t0, saved = {}, 0, time.time(), time.time()  # running: pid -> (tool, entry, dir, Popen)
    with tempfile.TemporaryDirectory(prefix="mixz-ref-") as tmp:
        while todo or running:
            while todo and len(running) < jobs:
                tool, e = todo[-1]
                # a process may still grow to its peak: start another only if all of them at their
                # peak would leave the reserve free (the first one always starts); free memory
                # already excludes what the running ones hold, so count only what they can still grow
                need = PEAK_MB[TOOLS[tool]] + sum(max(0, PEAK_MB[TOOLS[r[0]]] - _rss_mb(pid))
                                                  for pid, r in running.items())
                if running and _avail_mb() - need < RESERVE_MB:
                    break
                todo.pop()
                d = tempfile.mkdtemp(dir=tmp)
                src = os.path.join(d, "f")  # a 1-character name: paq8px stores the name in its header
                shutil.copyfile(e["abs"], src)
                p = subprocess.Popen(_command(tool, exes[tool], src, os.path.join(d, "o")),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                running[p.pid] = (tool, e, d, p)
            pid, status, ru = os.wait4(-1, 0)  # one child's exit and its own CPU time
            if pid not in running:
                continue
            tool, e, d, p = running.pop(pid)
            p.returncode = os.waitstatus_to_exitcode(status)
            out = os.path.join(d, "o")
            if p.returncode != 0 or not os.path.exists(out):
                raise SystemExit(f"{tool} failed on {e['abs']} (exit {p.returncode})")
            cache[key(tool, e["sha256"])] = [os.path.getsize(out), round(ru.ru_utime + ru.ru_stime, 3)]
            shutil.rmtree(d)
            done += 1
            if time.time() - saved > 60 or not (todo or running):
                with open(path + ".tmp", "w") as f:
                    json.dump(cache, f)
                os.replace(path + ".tmp", path)
                saved = time.time()
                if say:
                    say(f"reference: {done} done, {len(todo)} left, {len(running)} running, {time.time() - t0:.0f} s")
    return cache


def sizes(files: list, tools=tuple(TOOLS)) -> list:
    """Per file {tool: compressed bytes}, computing what the cache lacks."""
    cache = fill(files, tools, say=lambda m: print(m, file=sys.stderr, flush=True))
    return [{t: cache[key(t, e["sha256"])][0] for t in tools} for e in files]


def cpu_seconds(files: list, tools=tuple(TOOLS)) -> dict:
    path = cache_path()
    cache = json.load(open(path)) if os.path.exists(path) else {}
    return {t: sum(cache[key(t, e["sha256"])][1] for e in files) for t in tools}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tier", choices=fetch.TIERS, default="quick")
    ap.add_argument("--tool", action="append", choices=list(TOOLS), help="only this tool (repeatable)")
    ap.add_argument("--jobs", type=int, default=0, help="most processes at once (default: CPUs; memory limits it further)")
    args = ap.parse_args()
    files = fetch.ensure(args.tier)
    tools = tuple(args.tool or TOOLS)
    cache = fill(files, tools, args.jobs, say=lambda m: print(m, flush=True))
    for t in tools:
        n = sum(e["bytes"] for e in files)
        out = sum(cache[key(t, e["sha256"])][0] for e in files)
        cpu = sum(cache[key(t, e["sha256"])][1] for e in files)
        print(f"{t}: {out / n:.2%} of {n / 1e6:.2f} MB, {cpu:.0f} CPU s ({n / cpu / 1e3:.1f} KB/s)")


if __name__ == "__main__":
    main()
