#!/usr/bin/env python3
"""Download, verify and extract the harness test data, and build the frozen test set.

  fetch.py              make sure every file in sets.json is in the cache (run.py calls this)
  fetch.py --pin        download all sources and write their sha256 into sources.json
  fetch.py --calibrate  print the primer overlap of candidate files per kind
  fetch.py --freeze     choose the test set from the candidates and write sets.json

Cache: $MIXZ_CACHE or ~/.cache/mixz (downloads/, sets/<source id>/<member path>).
Downloaded archives are untrusted: members are read as data and written below the cache
directory only; names with absolute paths or '..' are skipped; nothing is executed.
"""
import argparse
import fnmatch
import hashlib
import io
import json
import os
import random
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, ROOT)
sys.path.insert(0, TOOLS)
import corpora  # noqa: E402
import overlap  # noqa: E402

SOURCES = os.path.join(HERE, "sources.json")
SETS = os.path.join(HERE, "sets.json")
KINDS = ["py", "c", "js", "json", "web", "prose", "man", "elf", "png"]
TIERS = ["quick", "full"]
KIB = 1024
# budgets in input bytes per kind; quick is part of full
BUDGET = {
    "quick": {"py": 512 * KIB, "c": 512 * KIB, "js": 512 * KIB, "json": 512 * KIB, "web": 512 * KIB,
              "prose": 512 * KIB, "man": 256 * KIB, "elf": 1536 * KIB, "png": 1536 * KIB},
    "full": {"py": 3072 * KIB, "c": 3072 * KIB, "js": 3072 * KIB, "json": 3072 * KIB, "web": 3072 * KIB,
             "prose": 3072 * KIB, "man": 1536 * KIB, "elf": 6144 * KIB, "png": 4096 * KIB},
}
MIN_FILE = 128
MAX_FILE = {"elf": 6 * 1024 * KIB, "png": 2 * 1024 * KIB}
MAX_FILE_TEXT = 1024 * KIB
UA = {"User-Agent": "mixz-bench-fetch/1"}


def cache_dir() -> str:
    return os.environ.get("MIXZ_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "mixz")


def load_sources() -> dict:
    with open(SOURCES) as f:
        return json.load(f)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(src: dict, pin: bool = False) -> str:
    d = os.path.join(cache_dir(), "downloads")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, src["id"] + "--" + os.path.basename(urllib.parse.urlparse(src["url"]).path))
    if not os.path.exists(path):
        print(f"  downloading {src['id']}: {src['url']}", file=sys.stderr, flush=True)
        tmp = path + ".part"
        with urllib.request.urlopen(urllib.request.Request(src["url"], headers=UA), timeout=300) as r, \
                open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(tmp, path)
    got = sha256_file(path)
    want = src.get("sha256")
    if want and got != want:
        raise SystemExit(f"{src['id']}: sha256 mismatch (got {got}, pinned {want}); delete {path} to re-download")
    if not want:
        if not pin:
            raise SystemExit(f"{src['id']}: no sha256 pinned in sources.json; run fetch.py --pin")
        src["sha256"] = got
    return path


def archive_type(src: dict) -> str:
    if "archive" in src:
        return src["archive"]
    p = urllib.parse.urlparse(src["url"]).path
    if p.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
        return "tar"
    if p.endswith(".zip"):
        return "zip"
    return "file"


def safe_name(name: str):
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    parts = name.split("/")
    if not name or name.startswith("/") or ".." in parts or any(p == "" for p in parts):
        return None
    return name


def _tar_members(tf, want):
    for m in tf:
        name = safe_name(m.name)
        if m.isfile() and name and want(name):
            yield name, tf.extractfile(m).read()


def _deb_members(path: str, want):
    with open(path, "rb") as f:
        b = f.read()
    if b[:8] != b"!<arch>\n":
        raise SystemExit(f"{path}: not a .deb (ar) archive")
    p = 8
    while p + 60 <= len(b):
        name = b[p: p + 16].decode("ascii", "replace").strip().rstrip("/")
        size = int(b[p + 48: p + 58].decode("ascii").strip())
        data = b[p + 60: p + 60 + size]
        p += 60 + size + (size & 1)
        if not name.startswith("data.tar"):
            continue
        if name.endswith(".zst"):
            import zstandard
            data = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)).read()
            mode = "r:"
        else:
            mode = {"xz": "r:xz", "gz": "r:gz", "bz2": "r:bz2"}.get(name.rsplit(".", 1)[-1], "r:")
        with tarfile.open(fileobj=io.BytesIO(data), mode=mode) as tf:
            yield from _tar_members(tf, want)


def members(src: dict, path: str, want):
    """Yield (name, bytes) for every regular file of the source whose name passes want()."""
    t = archive_type(src)
    if t == "deb":
        yield from _deb_members(path, want)
    elif t == "tar":
        with tarfile.open(path, "r:*") as tf:
            yield from _tar_members(tf, want)
    elif t == "zip":
        with zipfile.ZipFile(path) as zf:
            for zi in zf.infolist():
                name = safe_name(zi.filename)
                if not zi.is_dir() and name and want(name):
                    yield name, zf.read(zi)
    else:
        name = os.path.basename(urllib.parse.urlparse(src["url"]).path)
        if want(name):
            with open(path, "rb") as f:
                yield name, f.read()


def kinds_of(src: dict, name: str) -> list:
    if src.get("vm_split") and corpora.is_train("/" + name):
        return []  # its twin on the VM may be in a primer
    return [k for k, pats in src["kinds"].items() if any(fnmatch.fnmatchcase(name, p) for p in pats)]


def source_key(src: dict) -> str:
    """Changes whenever anything that decides which members are extracted, and as what, changes."""
    keep = {k: src.get(k) for k in ("url", "sha256", "archive", "kinds", "vm_split")}
    return sha256_bytes(json.dumps(keep, sort_keys=True).encode())


def extract(src: dict) -> dict:
    """Extract the source's matching members into the cache; return {name: {kinds, bytes, sha256}}."""
    out_dir = os.path.join(cache_dir(), "sets", src["id"])
    index_path = os.path.join(out_dir, ".index.json")
    if os.path.exists(index_path):
        with open(index_path) as f:
            index = json.load(f)
        if index.get("key") == source_key(src):
            return index["files"]
    path = download(src)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    files = {}
    for name, data in members(src, path, lambda n: bool(kinds_of(src, n))):
        dest = os.path.join(out_dir, *name.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        files[name] = {"kinds": kinds_of(src, name), "bytes": len(data), "sha256": sha256_bytes(data)}
    os.makedirs(out_dir, exist_ok=True)
    with open(index_path, "w") as f:
        json.dump({"key": source_key(src), "files": files}, f)
    return files


def file_path(source_id: str, name: str) -> str:
    return os.path.join(cache_dir(), "sets", source_id, *name.split("/"))


# --------------------------------------------------------------------------
# test-set selection
# --------------------------------------------------------------------------
def primer_overlap(kind: str, data: bytes) -> float:
    return overlap.primer_overlap(kind, data)


def candidates(sources: dict) -> dict:
    """{kind: [(source id, name, bytes, sha256)]} in a fixed order that balances bytes over sources
    (sources with the same 'group' count as one)."""
    per = {k: {} for k in KINDS}
    for src in sources["sources"]:
        group = src.get("group", src["id"])
        for name, info in sorted(extract(src).items()):
            for kind in info["kinds"]:
                if MIN_FILE <= info["bytes"] <= MAX_FILE.get(kind, MAX_FILE_TEXT):
                    per[kind].setdefault(group, []).append((src["id"], name, info["bytes"], info["sha256"]))
    out = {}
    for kind, by_src in per.items():
        queues = {}
        for sid, lst in by_src.items():
            lst = list(lst)
            random.Random(f"{kind}:{sid}").shuffle(lst)
            queues[sid] = lst
        order, placed = [], {sid: 0 for sid in queues}
        srcs = list(queues)  # sources.json order breaks ties
        while any(queues.values()):
            sid = min((s for s in srcs if queues[s]), key=lambda s: placed[s])
            item = queues[sid].pop(0)
            placed[sid] += item[2]
            order.append(item)
        out[kind] = order
    return out


def freeze(sources: dict) -> dict:
    cands = candidates(sources)
    sets = {"comment": "Frozen benchmark test set, generated by tools/harness/fetch.py --freeze. Do not edit by hand: "
                       "changing it makes results incomparable with earlier runs.",
            "overlap_k": overlap.K, "max_overlap": overlap.MAX_OVERLAP,
            "budgets": BUDGET, "kinds": {}}
    for kind in KINDS:
        limit = overlap.MAX_OVERLAP
        ov, taken = {}, set()  # overlap per content hash; content already chosen
        chosen, rejected = [], []
        for tier in TIERS:  # quick first, then full adds to it, so quick is part of full
            total = sum(f["bytes"] for f in chosen)
            for sid, name, n, sha in cands[kind]:
                if sha in taken or total + n > BUDGET[tier][kind]:
                    continue  # duplicate content, or does not fit the budget
                if sha not in ov:
                    with open(file_path(sid, name), "rb") as f:
                        ov[sha] = primer_overlap(kind, f.read())
                    if ov[sha] > limit:
                        rejected.append({"source": sid, "path": name, "bytes": n, "overlap": round(ov[sha], 4)})
                if ov[sha] > limit:
                    continue
                taken.add(sha)
                chosen.append({"source": sid, "path": name, "bytes": n, "sha256": sha,
                               "overlap": round(ov[sha], 4), "tier": tier})
                total += n
        sets["kinds"][kind] = {"files": chosen, "rejected_overlap": rejected}
        q = [f for f in chosen if f["tier"] == "quick"]
        print(f"{kind:5s} quick {len(q):4d} files {sum(f['bytes'] for f in q) / 1e6:6.2f} MB | "
              f"full {len(chosen):4d} files {sum(f['bytes'] for f in chosen) / 1e6:6.2f} MB | "
              f"rejected for overlap: {len(rejected)}", flush=True)
    return sets


def write_sets(sets: dict) -> None:
    """JSON with one line per file, so diffs of the test set are readable."""
    lines = ["{"] + [f" {json.dumps(k)}: {json.dumps(v)}," for k, v in sets.items() if k != "kinds"] + [' "kinds": {']
    for i, (kind, ks) in enumerate(sets["kinds"].items()):
        lines.append(f"  {json.dumps(kind)}: {{")
        for j, key in enumerate(("files", "rejected_overlap")):
            rows = [f"    {json.dumps(e)}" for e in ks[key]]
            lines.append(f'   "{key}": [' + ("\n" + ",\n".join(rows) + "\n   " if rows else "") + "]" + ("," if j == 0 else ""))
        lines.append("  }" + ("," if i < len(sets["kinds"]) - 1 else ""))
    lines += [" }", "}"]
    with open(SETS, "w") as f:
        f.write("\n".join(lines) + "\n")


def calibrate(sources: dict, per_kind: int = 300):
    import numpy as np
    for kind, order in candidates(sources).items():
        vals = {}
        for sid, name, n, sha in order[:per_kind]:
            with open(file_path(sid, name), "rb") as f:
                vals.setdefault(sid, []).append(primer_overlap(kind, f.read()))
        allv = np.array([v for vs in vals.values() for v in vs] or [0.0])
        print(f"{kind:5s} n={len(allv):4d} median={np.median(allv):6.1%} p90={np.percentile(allv, 90):6.1%} "
              f">5%={np.mean(allv > .05):5.1%} >20%={np.mean(allv > .2):5.1%} >50%={np.mean(allv > .5):5.1%}")
        for sid, vs in vals.items():
            v = np.array(vs)
            print(f"        {sid:18s} n={len(v):4d} median={np.median(v):6.1%} max={v.max():6.1%} >20%={np.mean(v > .2):5.1%}")


def ensure(tier: str = "full") -> list:
    """Make sure every file of the tier is in the cache; return its entries with 'kind' and 'abs'."""
    with open(SETS) as f:
        sets = json.load(f)
    srcs = {s["id"]: s for s in load_sources()["sources"]}
    want = TIERS[: TIERS.index(tier) + 1]
    out, need = [], set()
    for kind, ks in sets["kinds"].items():
        for e in ks["files"]:
            if e["tier"] in want:
                out.append(dict(e, kind=kind, abs=file_path(e["source"], e["path"])))
    for e in out:
        p = e["abs"]
        if not os.path.exists(p) or os.path.getsize(p) != e["bytes"]:
            need.add(e["source"])
    for sid in sorted(need):
        idx = os.path.join(cache_dir(), "sets", sid, ".index.json")
        if os.path.exists(idx):
            os.remove(idx)
        extract(srcs[sid])
    for e in out:
        with open(e["abs"], "rb") as f:
            if sha256_bytes(f.read()) != e["sha256"]:
                raise SystemExit(f"{e['source']}/{e['path']}: content differs from sets.json")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pin", action="store_true")
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--freeze", action="store_true")
    args = ap.parse_args()
    sources = load_sources()
    if args.pin:
        for src in sources["sources"]:
            download(src, pin=True)
        with open(SOURCES, "w") as f:
            json.dump(sources, f, indent=1)
            f.write("\n")
        print("sha256 pinned in sources.json")
    if args.calibrate:
        calibrate(sources)
    if args.freeze:
        write_sets(freeze(sources))
        print(f"wrote {SETS}")
    if not (args.pin or args.calibrate or args.freeze):
        files = ensure("full")
        print(f"{len(files)} files ready in {cache_dir()}")


if __name__ == "__main__":
    main()
