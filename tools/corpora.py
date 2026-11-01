"""Corpus definitions and the train/test split shared by build_primers.py and bench.py.

Split rule: a file is in the training split if a hash of its *normalized* path is even.
Normalizing strips version numbers (python3.12 vs python3.13) and /usr/local vs /usr,
so copies of the same module in two Python versions always land in the same split and
the benchmark never tests on a file whose twin was used to build a primer.
"""
import glob
import gzip
import os
import re
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

CORPORA = {
    "py": ["/usr/lib/python3*/**/*.py", "/usr/local/lib/python3*/**/*.py"],
    "c": ["/usr/include/**/*.h", "/usr/include/**/*.c", "/usr/local/include/**/*.h"],
    "js": ["/usr/**/*.js", "/opt/**/*.js"],
    "web": ["/usr/**/*.html", "/usr/**/*.css", "/usr/**/*.xml", "/usr/**/*.svg"],
    "json": ["/usr/**/*.json", "/opt/**/*.json"],
    "prose": ["/usr/**/*.md", "/usr/**/*.rst", "/usr/**/*.txt", "/usr/share/doc/*/copyright"],
    "man": ["/usr/share/man/**/*.gz", "/usr/share/**/man/**/*.gz", "/usr/share/info/*.gz"],  # content = inflated troff/info source
    "elf": ["/usr/bin/*", "/usr/sbin/*", "/usr/lib/x86_64-linux-gnu/*.so*", "/usr/lib/x86_64-linux-gnu/**/*.so*"],
}


def norm(path: str) -> str:
    p = re.sub(r"python3\.\d+", "python3", path)
    p = re.sub(r"^/usr/local/", "/usr/", p)
    p = re.sub(r"\d+(\.\d+)+", "N", p)
    return p


def is_train(path: str) -> bool:
    return zlib.crc32(norm(path).encode()) % 2 == 0


def _is_elf_x86(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            h = f.read(20)
    except OSError:
        return False
    return h[:4] == b"\x7fELF" and h[18:20] in (b"\x3e\x00", b"\x03\x00")


def files(kind: str, min_size: int = 64):
    seen = set()
    out = []
    for pat in CORPORA[kind]:
        for f in glob.glob(pat, recursive=True):
            if f in seen or "/node_modules/.cache/" in f:
                continue
            seen.add(f)
            try:
                if os.path.islink(f) or not os.path.isfile(f) or os.path.getsize(f) < min_size:
                    continue
            except OSError:
                continue
            if kind == "elf" and not _is_elf_x86(f):
                continue
            out.append(f)
    out.sort()
    return out


def content(kind: str, path: str) -> bytes:
    """What the model sees for a file of this kind (after mixz's own transforms)."""
    with open(path, "rb") as f:
        data = f.read()
    if kind == "man":
        try:
            return gzip.decompress(data)
        except Exception:
            return b""
    if kind == "elf":
        import mixz
        return mixz.e8e9(data, inverse=False)
    return data


def split(kind: str):
    fs = files(kind)
    return [f for f in fs if is_train(f)], [f for f in fs if not is_train(f)]
