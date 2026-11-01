#!/usr/bin/env python3
"""Cross-machine check against committed vectors.

Format 2 vectors of codec 3 (NAME.c3.mxz, and NAME.b8k.c3.mxz with 8 KiB blocks) were made on the
development machine (AMD Ryzen 3 1200, gcc 15.2). On any machine:
  1. every vector decompresses to the original (same sha256);
  2. compressing the original with the vector's codec gives byte-identical files (engine and container
     determinism). After an edit to the codec's source this is the proof that its output is unchanged.
The vectors of codecs 1 and 2 were removed with those codecs (pre-release); they are in git history.
Entries marked decode_only must only decompress (none at present).
Run: python3 tests/check_vectors.py
     python3 tests/check_vectors.py --write   (once per new codec: writes vectors that do not exist yet)"""
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mixz  # noqa: E402

V = os.path.join(ROOT, "tests", "vectors")
MANIFEST = os.path.join(V, "manifest.json")
BLOCK_VECTORS = {"mixz_py.txt": 8192}  # also kept as multi-block format 2 vectors
CODEC_SUFFIX = {3: "c3"}  # manifest key and file suffix of each codec's format 2 vectors


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read(name: str) -> bytes:
    with open(os.path.join(V, name), "rb") as f:
        return f.read()


def v2_entries(name: str):
    """(manifest key, file name, block size, codec) of the format 2 vectors of `name`."""
    for codec, suf in CODEC_SUFFIX.items():
        yield suf, f"{name}.{suf}.mxz", 0, codec
        if name in BLOCK_VECTORS:
            yield f"{suf}_blocks", f"{name}.b8k.{suf}.mxz", BLOCK_VECTORS[name], codec


def write(manifest: dict) -> None:
    for name, m in manifest.items():
        orig = read(name)
        for key, fname, bs, codec in v2_entries(name):
            path = os.path.join(V, fname)
            if key in m or os.path.exists(path):
                continue  # vectors are frozen once written
            blob = mixz.compress(orig, block_size=bs, codec=codec)
            with open(path, "wb") as f:
                f.write(blob)
            m[key] = {"file": fname, "block_size": bs, "codec": codec, "sha256": sha(blob), "info": mixz.describe(blob)}
            print(f"wrote {fname}")
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")


def check(manifest: dict) -> int:
    bad = 0
    for name, m in manifest.items():
        orig = read(name)
        res = {}
        for key, fname, bs, codec in v2_entries(name):
            if key not in m:
                res[f"{key} vector"] = False
                continue
            blob = read(fname)
            res[f"{key} decompress"] = sha(mixz.decompress(blob)) == m["sha256"]
            res[f"{key} recompress identical"] = sha(mixz.compress(orig, block_size=bs, codec=codec)) == m[key]["sha256"]
        for key, v in m.items():  # files the current encoder no longer writes: they must still decode
            if isinstance(v, dict) and v.get("decode_only"):
                res[f"{key} decompress"] = sha(mixz.decompress(read(v["file"]))) == m["sha256"]
        ok = all(res.values())
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name:20s} " + "  ".join(f"{k}={'yes' if v else 'NO'}" for k, v in res.items()))
    print("ALL VECTORS PASS" if not bad else f"{bad} FAILURES")
    return 1 if bad else 0


def main() -> int:
    mixz.USE_SNAPSHOTS = True
    with open(MANIFEST) as f:
        manifest = json.load(f)
    if {"--write", "--write-v2"} & set(sys.argv[1:]):
        write(manifest)
    return check(manifest)


if __name__ == "__main__":
    sys.exit(main())
