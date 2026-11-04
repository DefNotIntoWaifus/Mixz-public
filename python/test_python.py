#!/usr/bin/env python3
"""Checks that the Python version produces the same files as the C version.

  python3 python/test_python.py          the committed vectors (tests/vectors, made by the C version):
                                          each must decompress to the original, and compressing the
                                          original must give the identical file (about 8 minutes)
  python3 python/test_python.py --quick  the same without vectors over 40 KB or with primers (about 4 minutes)
  python3 python/test_python.py --c      also compares with the C engine of ../mixz.py on more inputs:
                                          the codec, deflate settings, files, bundles (needs a C compiler; 3 more minutes)
"""
import hashlib
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mixz_py  # noqa: E402
from mixz_py import codec, deflate  # noqa: E402

V = os.path.join(ROOT, "tests", "vectors")
failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {name}{'  ' + detail if detail else ''}", flush=True)
    if not ok:
        failures.append(name)


def vectors(quick: bool) -> None:
    print("vectors (made by the C version):")
    with open(os.path.join(V, "manifest.json")) as f:
        manifest = json.load(f)
    for name, entry in manifest.items():
        with open(os.path.join(V, name), "rb") as f:
            orig = f.read()
        for key, vec in entry.items():
            if not isinstance(vec, dict) or "file" not in vec:
                continue
            # --quick: no vector over 40 KB, and none that replays a primer (512 KB, ~6 minutes each way)
            if quick and (len(orig) > 40000 or "primer=none" not in vec.get("info", "primer=none")):
                continue
            with open(os.path.join(V, vec["file"]), "rb") as f:
                blob = f.read()
            t = time.time()
            back = mixz_py.decompress(blob)
            again = None if vec.get("decode_only") else mixz_py.compress(orig, block_size=vec["block_size"])
            ok = back == orig and (again is None or again == blob)
            check(vec["file"], ok, f"{len(orig)} -> {len(blob)} bytes, {time.time() - t:.0f} s")
            if hashlib.sha256(blob).hexdigest() != vec["sha256"]:
                check(vec["file"] + " sha256 in the manifest", False)


def damaged() -> None:
    print("damaged files are rejected:")
    blob = mixz_py.compress(b"hello hello hello hello, a small text file\n" * 20, use_primers=False)  # fast
    cases = {"cut short": blob[:-3], "bit flipped": blob[:20] + bytes([blob[20] ^ 1]) + blob[21:],
             "header cut short": blob[:8], "not mixz": b"PK\3\4" + blob[4:], "empty": b""}
    for name, bad in cases.items():
        try:
            mixz_py.decompress(bad)
            check(name, False, "decompressed without an error")
        except mixz_py.MixzError as e:
            check(name, True, str(e))


def against_c() -> None:
    sys.path.insert(0, ROOT)
    import mixz  # the C version
    print("against the C engine:")
    rnd = random.Random(7)
    text = open(os.path.join(ROOT, "README.md"), "rb").read() + open(os.path.join(ROOT, "mixz.py"), "rb").read()
    for prof, data, stride, bpp in [(0, text[:4000], 0, 1), (1, bytes(rnd.randrange(256) for _ in range(2000)), 0, 1),
                                    (2, bytes((x * 3 + y * 5) & 255 for y in range(30) for x in range(90)), 90, 3),
                                    (3, text[4000:8000], 0, 1), (4, text[8000:12000], 0, 1)]:
        ok = codec.compress(data, b"", prof, stride, bpp, 20) == mixz.cm_encode(data, prof, 0, stride, bpp, 20)
        check(f"codec, profile {prof}", ok)
    n = bad = 0
    for data in (text[:30000], bytes(rnd.randrange(256) for _ in range(5000)), b"\0" * 3000 + text[:3000]):
        for level in range(1, 10):
            for mem in (1, 5, 8, 9):
                for strategy in range(5):
                    for wbits in (-15, -9, 9, 15):
                        n += 1
                        bad += deflate.deflate(data, level, mem, strategy, wbits) != mixz.deflate(data, level, mem,
                                                                                                strategy, wbits)
    check(f"deflate, {n} cases (settings x inputs)", bad == 0, f"{bad} differ" if bad else "")
    import gzip
    import zlib
    samples = {"text": text[:6000], "gzip": gzip.compress(text[:8000], 6, mtime=0),
               "x86": b"\x7fELF\2\1\1" + bytes(11) + b"\x3e\0" + bytes(rnd.choice((0, 0xE8, 7, 0xFF)) for _ in range(3000)),
               "png": _png(rnd), "zlib-level1": zlib.compress(text[:5000], 1)}
    for name, data in samples.items():
        for bs in (0, 2000):
            ok = mixz_py.compress(data, block_size=bs) == mixz.compress(data, block_size=bs)
            check(f"file: {name}, block size {bs}", ok)
    items = [text[:3000], text[3000:5000], b"\1\2\3" * 10, gzip.compress(text[:4000], 9, mtime=0), b"tiny"]
    blob = mixz_py.compress_bundle(items)
    check("bundle", blob == mixz.compress_bundle(items) and mixz_py.decompress_bundle(blob) == items)
    check("bundle member", mixz_py.decompress_bundle(blob, index=1) == items[1])


def _png(rnd) -> bytes:
    import struct
    import zlib
    w, h = 40, 12
    rows = b"".join(bytes([y % 5]) + bytes((x * 3 + y * 7 + rnd.randrange(3)) & 255 for x in range(w * 3))
                    for y in range(h))
    chunk = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def main() -> int:
    t = time.time()
    vectors("--quick" in sys.argv)
    damaged()
    if "--c" in sys.argv:
        against_c()
    print(f"\n{'ALL PASSED' if not failures else str(len(failures)) + ' FAILED: ' + ', '.join(failures)}"
          f"  ({time.time() - t:.0f} s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
