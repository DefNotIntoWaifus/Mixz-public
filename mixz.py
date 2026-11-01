#!/usr/bin/env python3
"""
mixz — format-aware context-mixing compressor (MVP).

What it does, per file:
  1. Detects the format from content (not the extension).
  2. Undoes format-specific encodings that hide redundancy, but only when the
     original bytes can be rebuilt bit-exactly:
       - gzip  -> inflate, remember deflate parameters
       - PNG   -> inflate IDAT + undo scanline filters
       - x86 executables -> E8/E9 call/jump address transform
  3. Picks a learned per-format primer (a sample of typical content for that
     format) by testing which one predicts the file best.
  4. Codes the bytes with the context-mixing engine (engine/cm.c), primed
     with that primer, so even small files start from format knowledge.
  5. Falls back to storing the file unchanged when nothing helps
     (worst-case overhead: a header of ~10 bytes).
Every container carries a CRC32 of the original file; decompression verifies it.
File format 2 is written and read (version IDs from registry.json: codec + mind modules). Files of
pre-release codecs and formats (codec 1, format 1) are rejected with a clear error.

Usage:
  mixz.py c FILE...          compress  -> FILE.mxz
  mixz.py d FILE.mxz...      decompress -> FILE
  mixz.py t FILE.mxz...      test integrity
  mixz.py info FILE.mxz...   show how a file was encoded
  mixz.py serve              keep primed models loaded; c/d/t then use this server (same output, no start-up)
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import struct
import sys
import threading
import time
import weakref
import zlib
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "engine", "cm.c")  # the codec
ZLIB_DIR = os.path.join(HERE, "engine", "zlib-1.3.1")
# the engine library: the codec, the transforms and the pinned deflate (pdeflate.c with zlib 1.3.1's compressor)
ENGINE_SOURCES = [SRC, os.path.join(HERE, "engine", "transform.c"), os.path.join(HERE, "engine", "pdeflate.c")] + [
    os.path.join(ZLIB_DIR, f) for f in ("deflate.c", "trees.c", "zutil.c", "adler32.c", "crc32.c")]
ENGINE_DEFINES = ["-DZ_PREFIX", "-I" + ZLIB_DIR]
LIB = os.path.join(HERE, "libmixz.so")  # MIXZ_LIB overrides it (the harness runs -O0 and -O3 builds)
PRIMER_DIR = os.path.join(HERE, "primers")
REGISTRY_PATH = os.path.join(HERE, "registry.json")

MAGIC = b"MXZ"
M_STORED, M_CM, M_BUNDLE = 0, 1, 2
T_NONE, T_E8E9, T_GZIP, T_PNG = 0, 1, 2, 3
P_TEXT, P_BIN, P_IMAGE, P_CODE, P_WEB = 0, 1, 2, 3, 4
PROFILE_NAMES = {P_TEXT: "text", P_BIN: "binary", P_IMAGE: "image", P_CODE: "code", P_WEB: "web"}
TRANSFORM_NAMES = {T_NONE: "none", T_E8E9: "x86-e8e9", T_GZIP: "gzip-recompress", T_PNG: "png-recompress"}
# Primer ids are part of the file format: append only, never reorder.
PRIMER_IDS = ["", "py", "c", "js", "web", "json", "prose", "man", "elf"]
TEXT_PRIMERS = ["py", "c", "js", "web", "json", "prose", "man"]
BIN_PRIMERS = ["elf"]
# text sub-profiles, chosen by the primer that primer choice picked: each was kept because it codes
# its kind >= 1% smaller than the text profile on held-out data (plan step 6, docs/context.md 2.8)
TEXT_SUBPROFILES = {"py": P_CODE, "c": P_CODE, "js": P_CODE, "web": P_WEB}
DEFAULT_MAXBITS = 20  # 2^20 buckets x 16 B per context table: ~128 MB peak model memory


class MixzError(Exception):
    pass


# --------------------------------------------------------------------------
# engine
# --------------------------------------------------------------------------
_lib = None


def build_engine(out: str, flags: list):
    """Compile the engine library; returns the compiler's subprocess.CompletedProcess."""
    import subprocess
    cc = os.environ.get("CC", "cc")
    return subprocess.run([cc, *flags, "-fPIC", "-shared", "-fvisibility=hidden", *ENGINE_DEFINES,
                           "-o", out, *ENGINE_SOURCES], capture_output=True)


def engine_files() -> list:
    """Every file the engine library is built from (sources and headers)."""
    return ENGINE_SOURCES + sorted(os.path.join(ZLIB_DIR, f) for f in os.listdir(ZLIB_DIR) if f.endswith(".h"))


def _build_engine() -> None:
    err = b""
    for flags in (["-O3", "-march=native"], ["-O3"], ["-O2"]):
        r = build_engine(LIB, flags)
        if r.returncode == 0:
            return
        err = r.stderr
    raise MixzError("could not build the engine (need a C compiler): " + err.decode(errors="replace"))


def engine():
    global _lib
    if _lib is None:
        path = os.environ.get("MIXZ_LIB")
        if not path:
            path = LIB
            if not os.path.exists(LIB) or os.path.getmtime(LIB) < max(map(os.path.getmtime, engine_files())):
                _build_engine()
        lib = ctypes.CDLL(path)
        cp, sz, i = ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int
        for c in {registry()["codecs"][k]["prefix"] for k in CODECS}:  # the model functions of each codec
            f = lambda name: getattr(lib, c + name)  # noqa: E731
            f("compress").restype = ctypes.c_longlong
            f("compress").argtypes = [cp, sz, cp, sz, i, i, i, i, cp, sz]
            f("decompress").restype = i
            f("decompress").argtypes = [cp, sz, cp, sz, i, i, i, i, cp, sz]
            f("prime").restype = ctypes.c_void_p
            f("prime").argtypes = [cp, sz, i, i, i, i, ctypes.c_ulonglong]
            f("free_primed").restype = None
            f("free_primed").argtypes = [ctypes.c_void_p]
            f("size_class").restype = ctypes.c_uint
            f("size_class").argtypes = [ctypes.c_ulonglong, i]
            f("worker_new").restype = ctypes.c_void_p
            f("worker_new").argtypes = []
            f("worker_free").restype = None
            f("worker_free").argtypes = [ctypes.c_void_p]
            f("compress_primed").restype = ctypes.c_longlong
            f("compress_primed").argtypes = [ctypes.c_void_p, ctypes.c_void_p, cp, sz, sz, cp, sz]
            f("decompress_primed").restype = i
            f("decompress_primed").argtypes = [ctypes.c_void_p, ctypes.c_void_p, cp, sz, sz, cp, sz]
            f("decompress_part").restype = i
            f("decompress_part").argtypes = [cp, sz, cp, sz, i, i, i, i, cp, sz, sz]
            f("decompress_primed_part").restype = i
            f("decompress_primed_part").argtypes = [ctypes.c_void_p, ctypes.c_void_p, cp, sz, sz, cp, sz, sz]
        lib.mz_e8e9.restype = None
        lib.mz_e8e9.argtypes = [cp, sz, i]
        lib.mz_png_unfilter.restype = i
        lib.mz_png_unfilter.argtypes = [cp, sz, sz, i, cp, cp]
        lib.mz_png_refilter.restype = i
        lib.mz_png_refilter.argtypes = [cp, cp, sz, sz, i, cp]
        lib.mz_zs_new.restype = ctypes.c_void_p
        lib.mz_zs_new.argtypes = [i, i, i, i]
        lib.mz_zs_run.restype = ctypes.c_longlong
        lib.mz_zs_run.argtypes = [ctypes.c_void_p, cp, sz, cp, sz, i]
        lib.mz_zs_free.restype = None
        lib.mz_zs_free.argtypes = [ctypes.c_void_p]
        _lib = lib
    return _lib


def codec_fn(codec: int, name: str):
    """The engine function `name` (compress, prime, ...) of a registered codec."""
    if codec not in CODECS:
        raise MixzError(f"codec {codec} is not in this build")
    return getattr(engine(), registry()["codecs"][codec]["prefix"] + name)


# Primed model snapshots: priming costs ~1.6 s per MB of primer, copying a primed model into a
# thread's worker costs ~20 ms, so a batch of files with the same primer primes once. Output is identical.
USE_SNAPSHOTS = False  # turned on for batches; a single file is cheaper without the extra copy
_snaps: "OrderedDict" = OrderedDict()
_snap_lock = threading.Lock()
_snap_busy: dict = {}  # key -> Event, set when the thread priming that key has finished
SNAP_CACHE = 8  # primed models kept, ~155 MB each at the default size; fewer evict and re-prime (~0.8 s)


class _Snap:
    def __init__(self, ptr, codec):
        self.ptr, self.codec, self.users = ptr, codec, 0


def _acquire_snapshot(codec, pid, profile, stride, bpp, maxbits, total):
    key = (codec, pid, profile, stride, bpp, maxbits, codec_fn(codec, "size_class")(total, maxbits))
    while True:
        with _snap_lock:
            snap = _snaps.get(key)
            if snap is not None:
                _snaps.move_to_end(key)
                snap.users += 1
                return snap
            busy = _snap_busy.get(key)
            if busy is None:
                busy = _snap_busy[key] = threading.Event()
                break
        busy.wait()  # another thread is priming this key
    # prime outside the lock: threads that need different primers do not wait for each other
    ptr = None
    try:
        primer = primer_bytes(pid)
        ptr = codec_fn(codec, "prime")(primer, len(primer), profile, stride, bpp, maxbits, total)
    finally:
        with _snap_lock:
            del _snap_busy[key]
            snap = None
            if ptr:
                snap = _Snap(ptr, codec)
                snap.users = 1
                _snaps[key] = snap
                for k in list(_snaps):  # evict idle snapshots beyond the cache size
                    if len(_snaps) <= SNAP_CACHE:
                        break
                    if _snaps[k].users == 0 and k != key:
                        old = _snaps.pop(k)
                        codec_fn(old.codec, "free_primed")(old.ptr)
        busy.set()
    return snap


def _release_snapshot(snap):
    with _snap_lock:
        snap.users -= 1


# One worker per thread: a model whose ~155 MB of buffers are reused from file to file, so each file
# copies the snapshot into memory that is already mapped instead of page-faulting in fresh memory.
_tls = threading.local()


class _Worker:
    def __init__(self, codec):
        self.codec, self.ptr = codec, codec_fn(codec, "worker_new")()
        if not self.ptr:
            raise MixzError("engine error (out of memory?)")
        weakref.finalize(self, codec_fn(codec, "worker_free"), self.ptr)  # freed when the thread ends


def _worker(codec) -> int:
    w = getattr(_tls, "worker", None)
    if w is None or w.codec != codec:
        w = _tls.worker = _Worker(codec)
    return w.ptr


def cm_encode(data: bytes, profile: int, pid: int, stride: int, bpp: int, maxbits: int, codec: int = 0,
              snap: bool = False):
    """snap: use a primed snapshot even when USE_SNAPSHOTS is off (the blocks of one file share one)."""
    codec = codec or CODEC
    cap = len(data) + len(data) // 8 + 4096
    out = ctypes.create_string_buffer(cap)
    primer = primer_bytes(pid) if pid else b""
    r = -3
    if primer and (USE_SNAPSHOTS or snap):
        snap = _acquire_snapshot(codec, pid, profile, stride, bpp, maxbits, len(data) + len(primer))
        if snap:
            try:
                r = codec_fn(codec, "compress_primed")(snap.ptr, _worker(codec), data, len(data), len(primer), out, cap)
            finally:
                _release_snapshot(snap)
    if r == -3:
        r = codec_fn(codec, "compress")(data, len(data), primer, len(primer), profile, stride, bpp, maxbits, out, cap)
    if r == -2:
        return None  # output would be larger than the input: not worth it
    if r < 0:
        raise MixzError("engine error (out of memory?)")
    return out.raw[:r]


def cm_decode(blob: bytes, n: int, profile: int, pid: int, stride: int, bpp: int, maxbits: int,
              codec: int = 0, snap: bool = False, total: int = 0) -> bytes:
    """total: the length of the whole coded stream when only its first n bytes are wanted."""
    codec = codec or CODEC
    total = max(total, n)
    out = ctypes.create_string_buffer(max(n, 1))
    primer = primer_bytes(pid) if pid else b""
    r = -3
    if primer and (USE_SNAPSHOTS or snap):
        snap = _acquire_snapshot(codec, pid, profile, stride, bpp, maxbits, total + len(primer))
        if snap:
            try:
                r = codec_fn(codec, "decompress_primed_part")(snap.ptr, _worker(codec), blob, len(blob), len(primer),
                                                              out, n, total)
            finally:
                _release_snapshot(snap)
    if r == -3:
        r = codec_fn(codec, "decompress_part")(blob, len(blob), primer, len(primer), profile, stride, bpp, maxbits,
                                               out, n, total)
    if r != 0:
        raise MixzError("engine error (out of memory?)")
    return out.raw[:n]


# The blocks of one file are coded at the same time, one per thread; each starts from the primed model,
# so the output does not depend on the number of threads.
BLOCK_THREADS = 0  # 0: one per core
_block_pool = None
_block_pool_lock = threading.Lock()


def _block_map(fn, items: list) -> list:
    if len(items) < 2:
        return [fn(x) for x in items]
    global _block_pool
    with _block_pool_lock:
        if _block_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _block_pool = ThreadPoolExecutor(max_workers=BLOCK_THREADS or os.cpu_count() or 1)
    return list(_block_pool.map(fn, items))


def set_block_threads(n: int) -> None:
    """Threads that code one file's blocks at the same time (0: one per core). Each keeps a worker model
    (~155 MB). The output does not depend on it."""
    global BLOCK_THREADS, _block_pool
    with _block_pool_lock:
        if _block_pool is not None:
            _block_pool.shutdown()
            _block_pool = None
        BLOCK_THREADS = max(0, n)


# Block size when the caller gives none (-1): one block below SPLIT_MIN, else blocks of at least BLOCK_MIN.
# Splitting loses what the model learned from the earlier blocks. Measured (2026-10-26): 4 blocks of 51 MB
# cost +1.9% on the 205 MB NASA HTTP log, 2 blocks of 50 MB +0.9% on enwik8, and each halving of the
# block size costs 1.6-1.9x more; 512 MiB blocks should stay within the 0.5% rule (estimate).
AUTO_BLOCKS = -1
SPLIT_MIN = 1 << 30
BLOCK_MIN = 512 << 20


def auto_block_size(n: int) -> int:
    return 0 if n < SPLIT_MIN else max(BLOCK_MIN, -(-n // 64))


def split_blocks(n: int, block_size: int, row: int = 1) -> list:
    """Lengths of the blocks of an n-byte stream: ceil(n / block_size) blocks of nearly equal size (whole
    rows of `row` bytes for images), so that every block has the same model size and shares one snapshot."""
    if row < 1 or n % row:
        row = 1
    rows = n // row
    k = min(-(-n // block_size), rows) if block_size > 0 and n > block_size else 1
    base, extra = divmod(rows, max(k, 1))
    return [(base + (i < extra)) * row for i in range(k)] if k > 1 else [n]


def e8e9(data: bytes, inverse: bool) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    engine().mz_e8e9(buf, len(data), 1 if inverse else 0)
    return buf.raw[: len(data)]


# --------------------------------------------------------------------------
# primers
# --------------------------------------------------------------------------
_primers: dict = {}
_zdicts: dict = {}
_ztls = threading.local()


def _zstd_trial(zstandard, pid: int):
    """A reusable level-6 compressor for one primer, per thread. A new compressor loads the
    512 KB primer into fresh tables on its first use, which costs more than the trial itself;
    reusing it keeps the tables and gives the same bytes."""
    cs = getattr(_ztls, "cs", None)
    if cs is None:
        cs = _ztls.cs = {}
    c = cs.get(pid)
    if c is None:
        if pid == 0:
            c = zstandard.ZstdCompressor(level=6)
        else:
            if pid not in _zdicts:
                _zdicts[pid] = zstandard.ZstdCompressionDict(
                    primer_bytes(pid), dict_type=zstandard.DICT_TYPE_RAWCONTENT)
            c = zstandard.ZstdCompressor(level=6, dict_data=_zdicts[pid])
        cs[pid] = c
    return c


def primer_bytes(pid: int) -> bytes:
    if pid == 0:
        return b""
    if pid not in _primers:
        path = os.path.join(PRIMER_DIR, PRIMER_IDS[pid] + ".bin")
        try:
            with open(path, "rb") as f:
                _primers[pid] = f.read()
        except OSError:
            _primers[pid] = None
    return _primers[pid]


def available_primers(names) -> list:
    out = []
    for name in names:
        pid = PRIMER_IDS.index(name)
        if primer_bytes(pid):
            out.append(pid)
    return out


def _sample(data: bytes, size: int = 65536) -> bytes:
    if len(data) <= size:
        return data
    third = size // 3
    mid = len(data) // 2
    return data[:third] + data[mid: mid + third] + data[-third:]


def choose_primer(data: bytes, candidates: list) -> int:
    """Pick the primer that best predicts this file. Proxy: a fast LZ compressor using the
    primer as a dictionary, on a sample of the file (milliseconds, vs ~1 s per CM trial).
    Uses zstd when installed (whole primer as dictionary), else zlib (last 32 KB of it)."""
    if not candidates:
        return 0
    sample = _sample(data)
    try:
        import zstandard

        def size(pid):
            return len(_zstd_trial(zstandard, pid).compress(sample))
    except ImportError:
        def size(pid):
            c = zlib.compressobj(9, zlib.DEFLATED, 15, 9, 0, primer_bytes(pid)[-32768:]) if pid else zlib.compressobj(9)
            return len(c.compress(sample) + c.flush())
    best_pid, best = 0, size(0)
    for pid in candidates:
        sz = size(pid)
        if sz < best * 0.99:
            best_pid, best = pid, sz
    return best_pid


# --------------------------------------------------------------------------
# format detection
# --------------------------------------------------------------------------
def text_score(sample: bytes) -> float:
    if not sample:
        return 0.0
    bad = sum(sample.count(bytes([b])) for b in list(range(0, 9)) + [11, 12] + list(range(14, 32)) + [127])
    return 1.0 - bad / len(sample)


def x86_executable(data: bytes) -> bool:
    if data[:4] == b"\x7fELF" and len(data) > 20:
        endian = "<" if data[5] == 1 else ">"
        machine = struct.unpack(endian + "H", data[18:20])[0]
        return machine in (3, 62)  # EM_386, EM_X86_64
    if data[:2] == b"MZ" and len(data) > 0x40:
        off = struct.unpack("<I", data[0x3C:0x40])[0]
        if data[off: off + 4] == b"PE\0\0":
            machine = struct.unpack("<H", data[off + 4: off + 6])[0]
            return machine in (0x14C, 0x8664)
    return False


def looks_incompressible(data: bytes) -> bool:
    sample = _sample(data, 1 << 18)
    try:
        import zstandard
        c = len(zstandard.ZstdCompressor(level=1).compress(sample))
    except ImportError:
        c = len(zlib.compress(sample, 1))
    return c > 0.97 * len(sample)


# --------------------------------------------------------------------------
# bit-exact deflate re-creation
# --------------------------------------------------------------------------
class PinnedDeflate:
    """zlib.compressobj work-alike on the zlib 1.3.1 compressor inside the engine (engine/pdeflate.c).
    Re-created gzip/PNG streams must not depend on the zlib of the machine (deflate implementation 1)."""

    def __init__(self, level: int, mem: int, strategy: int, wbits: int):
        self._h = None
        if not 1 <= level <= 9:  # level 0 sizes stored blocks by the caller's output buffer: not reproducible
            raise MixzError("deflate: level must be 1..9")
        self._lib = engine()
        self._h = self._lib.mz_zs_new(level, wbits, mem, strategy)
        if not self._h:
            raise MixzError("deflate: invalid parameters")

    def _run(self, data: bytes, finish: int) -> bytes:
        cap = len(data) + (len(data) >> 3) + (1 << 20)  # deflate output plus a pending block fits
        out = ctypes.create_string_buffer(cap)
        n = self._lib.mz_zs_run(self._h, data, len(data), out, cap, finish)
        if n < 0:
            raise MixzError("deflate failed")
        return ctypes.string_at(out, n)

    def compress(self, data: bytes) -> bytes:
        return self._run(data, 0)

    def flush(self) -> bytes:
        try:
            return self._run(b"", 1)
        finally:
            self.close()

    def close(self) -> None:
        if self._h:
            self._lib.mz_zs_free(self._h)
            self._h = None

    __del__ = close


def find_deflate_params(raw: bytes, comp: bytes, wbits: int, level_hint=None):
    """Find zlib (level, memLevel, strategy) that reproduce `comp` from `raw` exactly.
    Streams output incrementally and aborts a candidate at its first mismatching byte."""
    levels = list(level_hint or []) + [l for l in (9, 6, 1, 2, 3, 4, 5, 7, 8) if l not in (level_hint or [])]
    for strategy in (0, 1):
        for mem in (8, 9, 7, 6, 5, 4, 3, 2, 1):
            for level in levels:
                c = PinnedDeflate(level, mem, strategy, wbits)
                out = bytearray()
                ok = True
                for i in range(0, len(raw), 1 << 16):
                    out += c.compress(raw[i: i + (1 << 16)])
                    if out != comp[: len(out)]:
                        ok = False
                        break
                if not ok:
                    c.close()
                    continue
                out += c.flush()
                if out == comp:
                    return level, mem, strategy
            if mem == 9 and strategy == 0 and wbits < 0:
                break  # gzip: other memLevels essentially never occur
    return None


def deflate(raw: bytes, level: int, mem: int, strategy: int, wbits: int) -> bytes:
    c = PinnedDeflate(level, mem, strategy, wbits)
    return c.compress(raw) + c.flush()


# --------------------------------------------------------------------------
# varints
# --------------------------------------------------------------------------
def put_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def get_varint(buf: bytes, p: int):
    n = shift = 0
    while True:
        if p >= len(buf):
            raise MixzError("truncated header")
        b = buf[p]
        p += 1
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return n, p


def _record(cls):
    """A minimal @dataclass: __init__ (positional or keyword, defaults from the class; a list default
    is copied per instance), __repr__ and __eq__ over the annotated fields. The dataclasses module
    costs about 17 ms to import, which every CLI call through a server would pay."""
    names = list(cls.__annotations__)
    defaults = {n: getattr(cls, n) for n in names if hasattr(cls, n)}

    def __init__(self, *args, **kw):
        if len(args) > len(names):
            raise TypeError(f"{cls.__name__}: too many arguments")
        vals = dict(zip(names, args))
        for n in names[len(args):]:
            if n in kw:
                vals[n] = kw.pop(n)
            elif n in defaults:
                vals[n] = list(defaults[n]) if isinstance(defaults[n], list) else defaults[n]
            else:
                raise TypeError(f"{cls.__name__}: missing {n}")
        if kw:
            raise TypeError(f"{cls.__name__}: unexpected {sorted(kw)}")
        self.__dict__.update(vals)
    cls.__init__ = __init__
    cls.__repr__ = lambda self: f"{cls.__name__}(" + ", ".join(f"{n}={getattr(self, n)!r}" for n in names) + ")"
    cls.__eq__ = lambda self, o: type(o) is type(self) and all(getattr(self, n) == getattr(o, n) for n in names)
    return cls


# --------------------------------------------------------------------------
# transforms
# --------------------------------------------------------------------------
@_record
class Plan:
    transform: int
    profile: int
    stream: bytes
    stride: int = 0
    bpp: int = 1
    meta: bytes = b""
    primer: int = 0
    note: str = ""


def gzip_forward(data: bytes):
    if len(data) < 18 or data[:3] != b"\x1f\x8b\x08":
        return None
    flg = data[3]
    p = 10
    try:
        if flg & 4:
            p += 2 + struct.unpack("<H", data[p: p + 2])[0]
        if flg & 8:
            p = data.index(b"\0", p) + 1
        if flg & 16:
            p = data.index(b"\0", p) + 1
        if flg & 2:
            p += 2
        d = zlib.decompressobj(-15)
        raw = d.decompress(data[p:])
        if not d.eof:
            return None
    except (ValueError, zlib.error, struct.error):
        return None
    tail = d.unused_data
    comp = data[p: len(data) - len(tail)]
    if len(tail) - 8 > 65536:
        return None  # multi-member archives: not handled in the MVP
    params = find_deflate_params(raw, comp, -15)
    if params is None:
        return None
    trailer, rest = tail[:8], tail[8:]
    std = trailer == struct.pack("<II", zlib.crc32(raw), len(raw) & 0xFFFFFFFF)
    meta = bytes(params) + put_varint(p) + data[:p] + bytes([1 if std else 0])
    if not std:
        meta += put_varint(len(trailer)) + trailer
    meta += rest
    return raw, meta


def gzip_inverse(raw: bytes, meta: bytes) -> bytes:
    level, mem, strategy = meta[0], meta[1], meta[2]
    hlen, q = get_varint(meta, 3)
    header = meta[q: q + hlen]
    q += hlen
    std = meta[q]
    q += 1
    if std:
        trailer = struct.pack("<II", zlib.crc32(raw), len(raw) & 0xFFFFFFFF)
    else:
        tlen, q = get_varint(meta, q)
        trailer = meta[q: q + tlen]
        q += tlen
    return header + deflate(raw, level, mem, strategy, -15) + trailer + meta[q:]


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_chunks(data: bytes):
    p = 8
    out = []
    while p + 12 <= len(data):
        ln = struct.unpack(">I", data[p: p + 4])[0]
        typ = data[p + 4: p + 8]
        body = data[p + 8: p + 8 + ln]
        crc = data[p + 8 + ln: p + 12 + ln]
        if len(body) != ln or len(crc) != 4:
            return None, p
        out.append((typ, body, crc))
        p += 12 + ln
        if typ == b"IEND":
            break
    return out, p


def png_build(chunks) -> bytes:
    out = bytearray(PNG_SIG)
    for typ, body in chunks:
        out += struct.pack(">I", len(body)) + typ + body + struct.pack(">I", zlib.crc32(typ + body))
    return bytes(out)


def png_forward(data: bytes):
    if not data.startswith(PNG_SIG):
        return None
    chunks, end = png_chunks(data)
    if not chunks or chunks[0][0] != b"IHDR" or chunks[-1][0] != b"IEND":
        return None
    if any(zlib.crc32(t + b) != struct.unpack(">I", c)[0] for t, b, c in chunks):
        return None
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunks[0][1][:13])
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or interlace or w == 0 or h == 0:
        return None
    bits = channels * depth
    rowbytes = (w * bits + 7) // 8
    bpp = max(1, bits // 8)
    idx = [i for i, c in enumerate(chunks) if c[0] == b"IDAT"]
    if not idx or idx != list(range(idx[0], idx[-1] + 1)):
        return None
    z = b"".join(chunks[i][1] for i in idx)
    try:
        d = zlib.decompressobj()
        raw = d.decompress(z)
        if not d.eof or d.unused_data:
            return None
    except zlib.error:
        return None
    if len(raw) != h * (rowbytes + 1):
        return None
    wbits = (z[0] >> 4) + 8
    hint = {0: [1], 1: [2, 3, 4, 5], 2: [6], 3: [7, 8, 9]}[z[1] >> 6]
    params = find_deflate_params(raw, z, wbits, hint)
    if params is None:
        return None
    pix = ctypes.create_string_buffer(h * rowbytes)
    types = ctypes.create_string_buffer(h)
    if engine().mz_png_unfilter(raw, h, rowbytes, bpp, pix, types) != 0:
        return None
    meta = {
        "pre": [[c[0].decode("latin-1"), base64.b64encode(c[1]).decode()] for c in chunks[: idx[0]]],
        "post": [[c[0].decode("latin-1"), base64.b64encode(c[1]).decode()] for c in chunks[idx[-1] + 1:]],
        "idat": [len(chunks[i][1]) for i in idx],
        "zp": list(params) + [wbits],
        "h": h, "rowbytes": rowbytes, "bpp": bpp,
        "types": base64.b64encode(types.raw[:h]).decode(),
        "tail": base64.b64encode(data[end:]).decode(),
    }
    return pix.raw[: h * rowbytes], deflate(json.dumps(meta, separators=(",", ":")).encode(), 9, 8, 0, 15), rowbytes, bpp


def png_inverse(pix: bytes, meta_blob: bytes) -> bytes:
    meta = json.loads(zlib.decompress(meta_blob))
    h, rowbytes, bpp = meta["h"], meta["rowbytes"], meta["bpp"]
    types = base64.b64decode(meta["types"])
    if len(types) != h or len(pix) != h * rowbytes or not 1 <= bpp <= 8 or any(t > 4 for t in types):
        raise MixzError("corrupt PNG metadata")
    raw = ctypes.create_string_buffer(h * (rowbytes + 1))
    engine().mz_png_refilter(pix, types, h, rowbytes, bpp, raw)
    level, mem, strategy, wbits = meta["zp"]
    z = deflate(raw.raw, level, mem, strategy, wbits)
    chunks = [(t.encode("latin-1"), base64.b64decode(b)) for t, b in meta["pre"]]
    p = 0
    for size in meta["idat"]:
        chunks.append((b"IDAT", z[p: p + size]))
        p += size
    chunks += [(t.encode("latin-1"), base64.b64decode(b)) for t, b in meta["post"]]
    return png_build(chunks) + base64.b64decode(meta["tail"])


def inverse_transform(transform: int, stream: bytes, meta: bytes) -> bytes:
    if transform == T_NONE:
        return stream
    if transform == T_E8E9:
        return e8e9(stream, inverse=True)
    if transform == T_GZIP:
        return gzip_inverse(stream, meta)
    if transform == T_PNG:
        return png_inverse(stream, meta)
    raise MixzError(f"unknown transform {transform}")


def plan_for_plain(data: bytes, note: str = "") -> Plan:
    sample = _sample(data)
    if x86_executable(data):
        stream = e8e9(data, inverse=False)
        return Plan(T_E8E9, P_BIN, stream, primer=choose_primer(stream, available_primers(BIN_PRIMERS)),
                    note=note or "x86 executable")
    if text_score(sample) > 0.97:
        primer = choose_primer(data, available_primers(TEXT_PRIMERS))
        prof = TEXT_SUBPROFILES.get(PRIMER_IDS[primer], P_TEXT)
        return Plan(T_NONE, prof, data, primer=primer, note=note or PROFILE_NAMES[prof])
    return Plan(T_NONE, P_BIN, data, primer=choose_primer(data, available_primers(BIN_PRIMERS)),
                note=note or "binary")


def make_plan(data: bytes):
    """Return a Plan, or None when the data should simply be stored."""
    if data.startswith(PNG_SIG):
        r = png_forward(data)
        if r:
            pix, meta, rowbytes, bpp = r
            return Plan(T_PNG, P_IMAGE, pix, stride=rowbytes, bpp=bpp, meta=meta, note="png (re-created bit-exact)")
    if data[:3] == b"\x1f\x8b\x08":
        r = gzip_forward(data)
        if r:
            raw, meta = r
            inner = plan_for_plain(raw)
            if inner.transform == T_NONE:
                return Plan(T_GZIP, inner.profile, raw, meta=meta, primer=inner.primer,
                            note=f"gzip (re-created bit-exact) of {inner.note}")
    # skip the (slow) model on data that is already compressed or random; small files and
    # text always get a real attempt, since a primer can help where generic tests say no
    if len(data) >= 4096 and text_score(_sample(data)) <= 0.97 and looks_incompressible(data):
        return None
    return plan_for_plain(data)


# --------------------------------------------------------------------------
# registry (format v2): codecs, deflates, mind modules, versions, profiles; append only
# --------------------------------------------------------------------------
CODEC = 3  # the codec new files are written with (engine/cm.c)
CODECS = (3,)  # codecs this build can decode (codecs 1 and 2, pre-release, were dropped)
DEFLATE = 1  # the pinned zlib 1.3.1 compressor
ENGINE_PROFILES = {"text": P_TEXT, "binary": P_BIN, "image": P_IMAGE, "code": P_CODE, "web": P_WEB}
SECTIONS = ("codecs", "deflates", "modules", "versions", "profiles")
# the fields that define an entry (hashed into the registry chain); the rest is description
CHAIN_FIELDS = {"codecs": ("name", "engine_sha256"), "deflates": ("name",), "modules": ("name", "sha256"),
                "versions": ("codec", "engine_profile", "modules", "weights"), "profiles": ("name", "parent")}
VERSION_HASH_BYTES = 16  # a version named by hash in a header: ID 0, then this many bytes of its hash
_registry = None
_heads: list = []
_modules_ok: dict = {}


def registry() -> dict:
    global _registry, _heads
    if _registry is None:
        with open(REGISTRY_PATH) as f:
            r = json.load(f)
        _heads = r.get("heads", [])
        _registry = {k: {int(i): v for i, v in r[k].items()} for k in SECTIONS}
        _registry["retired"] = {k: set(v) for k, v in r.get("retired", {}).items() if k in SECTIONS}
        _registry["by_hash"] = {version_hash(vid, _registry): vid for vid in _registry["versions"]}
    return _registry


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def version_hash(vid: int, reg: dict = None) -> bytes:
    """A version's identity: sha256 over the content it stands for (the codec's source hash, the engine
    profile, each module's sha256 in replay order, the start weights). The same on every machine."""
    reg = reg or registry()
    v = reg["versions"][vid]
    return hashlib.sha256(b"mixz-version\0" + _canon({
        "codec": reg["codecs"][v["codec"]]["engine_sha256"], "engine_profile": v["engine_profile"],
        "modules": [reg["modules"][m]["sha256"] for m in v["modules"]], "weights": v["weights"]})).digest()


def registry_head(counts: dict = None) -> str:
    """Hash chain over the registry: each section's entries in ID order, each step hashing the previous
    value and the entry's ID and defining fields. With `counts`, the head of the registry as it was when
    each section had that many entries. Editing or removing an entry changes every later head."""
    reg = registry()
    parts = []
    for sec in SECTIONS:
        ids = sorted(reg[sec])
        n = len(ids) if counts is None else counts[sec]
        c = hashlib.sha256(b"mixz-registry/" + sec.encode()).digest()
        for i in ids[:n]:
            e = reg[sec][i]
            c = hashlib.sha256(c + _canon([i, {k: e.get(k) for k in CHAIN_FIELDS[sec]}])).digest()
        parts.append(f"{sec}:{n}:{c.hex()}")
    return hashlib.sha256(";".join(parts).encode()).hexdigest()


def registry_heads() -> list:
    """The heads recorded in registry.json, one per release of the registry ({"counts", "head"})."""
    registry()
    return _heads


def version_id(profile: int, pid: int, codec: int = CODEC) -> int:
    """The registered version for this engine profile and primer with the given codec."""
    for vid, v in registry()["versions"].items():
        if (v["codec"] == codec and ENGINE_PROFILES[v["engine_profile"]] == profile
                and v["modules"] == ([pid] if pid else []) and v["weights"] is None):
            return vid
    raise MixzError(f"no registered version for the {PROFILE_NAMES[profile]} profile with primer "
                    f"'{PRIMER_IDS[pid] or 'none'}' and codec {codec} (add one to registry.json)")


def version_by_hash(hp: bytes) -> int:
    """The registered version whose hash starts with `hp` (a header's VERSION_HASH_BYTES bytes)."""
    for h, vid in registry()["by_hash"].items():
        if h[:len(hp)] == hp:
            return vid
    raise MixzError(f"the file needs mind version {hp.hex()}, which this build's registry does not have "
                    "(get that version, or a newer mixz)")


def version_info(vid: int):
    """(codec, engine profile, primer id) of a registered version that this build can decode."""
    v = registry()["versions"].get(vid)
    if v is None and vid in registry()["retired"].get("versions", ()):
        raise MixzError(f"version {vid} belongs to a pre-release codec that this mixz does not read")
    if v is None:
        raise MixzError(f"unknown version {vid}: the file needs a newer mixz")
    if v["codec"] not in CODECS or v["weights"] is not None or len(v["modules"]) > 1:
        raise MixzError(f"version {vid} needs a codec or mind this build does not have")
    return v["codec"], ENGINE_PROFILES[v["engine_profile"]], (v["modules"][0] if v["modules"] else 0)


def check_module(pid: int) -> None:
    """A module must be byte-identical to the registered one before it is used for decoding."""
    if not pid or _modules_ok.get(pid):
        return
    m = registry()["modules"][pid]
    data = primer_bytes(pid)
    if not data:
        raise MixzError(f"missing mind module {m['file']}")
    if hashlib.sha256(data).hexdigest() != m["sha256"]:
        raise MixzError(f"{m['file']} differs from registered module {pid}; it was changed after release")
    _modules_ok[pid] = True


def profile_id(plan: "Plan") -> int:
    names = {p["name"]: i for i, p in registry()["profiles"].items()}
    if plan.transform == T_PNG:
        return names["image/png"]
    if plan.transform == T_E8E9:
        return names["binary/x86"]
    if plan.profile in (P_CODE, P_WEB):
        return names["text/" + PROFILE_NAMES[plan.profile]]
    return names[PROFILE_NAMES[plan.profile]]


# --------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------
# Format 2 (written):
#   "MXZ" | 2 | flags | size varint | crc32 (4, little-endian) | payload            (stored)
#   "MXZ" | 2 | flags | size varint | crc32 | version | profile varint | maxbits (1)
#     version: varint ID >= 1 from registry.json, or 0 followed by the first VERSION_HASH_BYTES bytes
#     of the version's hash (mixz.version_hash), for versions named by content
#     [stride varint, bpp varint]   when the version's engine profile is image
#     [model stream length varint]  when there is a transform
#     [deflate varint]              gzip and PNG: the implementation that re-creates the stream
#     [meta length varint, meta]    gzip and PNG
#     [blocks varint >= 2, then per block: model bytes varint, payload bytes varint, crc32 (4)]
#     payload: the blocks' coded data, one after the other; each block is coded from the primed model
#   flags: bits 0-1 method, bits 2-4 transform, bit 5 meta, bit 6 block table, bit 7 reserved (0)
# Format 1 (the MVP's, pre-release) is not read.
FORMAT = 2
F_META, F_BLOCKS, F_RESERVED = 1 << 5, 1 << 6, 1 << 7
MAX_SIZE = 1 << 40
# The coder caps a bit's probability at 65504/65536, so every bit costs at least 0.0007 bits:
# at most ~1,420 model bytes per payload byte. Larger claims are corrupt or malicious headers.
MAX_RATIO = 2048


def _check_ratio(stream_len: int, payload_len: int) -> None:
    if stream_len > MAX_RATIO * payload_len + 64:
        raise MixzError("corrupt or truncated file (the sizes in the header do not fit the data)")


def _head(method: int, transform: int, flags: int, size: int, crc: int) -> bytearray:
    return bytearray(MAGIC + bytes([FORMAT, method | (transform << 2) | flags]) + put_varint(size)
                     + struct.pack("<I", crc))


def _stored(data: bytes, crc: int) -> bytes:
    return bytes(_head(M_STORED, T_NONE, 0, len(data), crc)) + data


def compress(data: bytes, maxbits: int = DEFAULT_MAXBITS, use_primers: bool = True, verify: bool = False,
             block_size: int = AUTO_BLOCKS, codec: int = CODEC, by_hash: bool = False) -> bytes:
    """block_size: split the model stream into blocks of about this many bytes (0: one block; AUTO_BLOCKS:
    chosen from the stream length, auto_block_size).
    codec: the codec to write with. by_hash: name the version by its hash."""
    if codec not in CODECS:
        raise MixzError(f"codec {codec} is not in this build")
    out = _compress(data, maxbits, use_primers, block_size, codec, by_hash)
    if verify and decompress(out) != data:  # decompress also checks the CRC
        raise MixzError("verification failed: please report this file")
    return out


def _compress(data: bytes, maxbits: int, use_primers: bool, block_size: int = AUTO_BLOCKS, codec: int = CODEC,
              by_hash: bool = False) -> bytes:
    if not 16 <= maxbits <= 24:
        raise MixzError("maxbits must be between 16 and 24")
    crc = zlib.crc32(data)
    if len(data) < 16:
        return _stored(data, crc)
    plan = make_plan(data)
    if plan is None:
        return _stored(data, crc)
    if not use_primers:
        plan.primer = 0
    # safety: the transform must rebuild the original bytes exactly, else code the raw bytes
    if plan.transform != T_NONE and inverse_transform(plan.transform, plan.stream, plan.meta) != data:
        prof = P_TEXT if text_score(_sample(data)) > 0.97 else P_BIN
        plan = Plan(T_NONE, prof, data, primer=0, note="transform check failed; coded raw")
    stream = plan.stream
    if block_size < 0:
        block_size = auto_block_size(len(stream))
    # image blocks hold whole rows, so the pixel above stays inside the block
    blocks, q = [], 0
    for n in split_blocks(len(stream), block_size, plan.stride if plan.profile == P_IMAGE else 1):
        blocks.append(stream[q: q + n])
        q += n
    payloads = _block_map(lambda b: cm_encode(b, plan.profile, plan.primer, plan.stride, plan.bpp, maxbits, codec,
                                              snap=len(blocks) > 1), blocks)
    if any(p is None for p in payloads):
        return _stored(data, crc)
    flags = (F_META if plan.meta else 0) | (F_BLOCKS if len(blocks) > 1 else 0)
    head = _head(M_CM, plan.transform, flags, len(data), crc)
    vid = version_id(plan.profile, plan.primer, codec)
    head += (put_varint(0) + version_hash(vid)[:VERSION_HASH_BYTES]) if by_hash else put_varint(vid)
    head += put_varint(profile_id(plan)) + bytes([maxbits])
    if plan.profile == P_IMAGE:
        head += put_varint(plan.stride) + put_varint(plan.bpp)
    if plan.transform != T_NONE:
        head += put_varint(len(stream))
    if plan.transform in (T_GZIP, T_PNG):
        head += put_varint(DEFLATE)
    if plan.meta:
        head += put_varint(len(plan.meta)) + plan.meta
    if len(blocks) > 1:
        head += put_varint(len(blocks))
        for b, payload in zip(blocks, payloads):
            head += put_varint(len(b)) + put_varint(len(payload)) + struct.pack("<I", zlib.crc32(b))
    out = bytes(head) + b"".join(payloads)
    if len(out) >= len(data) + 9:
        return _stored(data, crc)
    return out


@_record
class Header:
    method: int
    size: int
    crc: int
    fmt: int = FORMAT
    profile: int = 0  # engine profile: text, binary or image
    transform: int = 0
    primer: int = 0  # primer (mind module) replayed before the data, 0 for none
    version: int = 0  # format 2: registered version
    by_hash: bool = False  # format 2: the header named the version by its hash
    codec: int = CODEC  # the codec the payload was coded with
    profile_id: int = 0  # format 2: detected content type (informational)
    deflate: int = 0  # format 2, gzip and PNG: deflate implementation
    maxbits: int = DEFAULT_MAXBITS
    stride: int = 0
    bpp: int = 1
    stream_len: int = 0
    meta: bytes = b""
    blocks: list = []  # [(model bytes, payload bytes, crc32)]; empty: one block (copied per header)
    payload_at: int = 0


def _u32(blob: bytes, p: int) -> int:
    if p + 4 > len(blob):
        raise MixzError("truncated header")
    return struct.unpack("<I", blob[p: p + 4])[0]


def parse_header(blob: bytes) -> Header:
    if blob[:3] != MAGIC:
        raise MixzError("not a mixz file")
    if len(blob) < 5:
        raise MixzError("truncated header")
    if blob[3] == 1:
        raise MixzError("format 1 file: written by a pre-release mixz, not readable by this one")
    if blob[3] != FORMAT:
        raise MixzError(f"unsupported mixz format {blob[3]}: the file needs a newer mixz")
    flags = blob[4]
    method, transform = flags & 3, (flags >> 2) & 7
    if flags & F_RESERVED or method not in (M_STORED, M_CM, M_BUNDLE) or transform not in TRANSFORM_NAMES:
        raise MixzError("corrupt header (flags)")
    size, p = get_varint(blob, 5)
    if size > MAX_SIZE:
        raise MixzError("corrupt header (size)")
    crc = _u32(blob, p)
    p += 4
    h = Header(method, size, crc, fmt=FORMAT, transform=transform)
    if method == M_BUNDLE:
        if flags != M_BUNDLE:
            raise MixzError("corrupt header (flags)")
        h.payload_at = p
        return h
    if method == M_STORED:
        if flags != M_STORED:
            raise MixzError("corrupt header (flags)")
        if len(blob) - p != size:
            raise MixzError("truncated file" if len(blob) - p < size else "corrupt file (bytes after the end)")
        h.payload_at = p
        return h
    h.version, p = get_varint(blob, p)
    if h.version == 0:
        if p + VERSION_HASH_BYTES > len(blob):
            raise MixzError("truncated header")
        h.version, h.by_hash = version_by_hash(blob[p: p + VERSION_HASH_BYTES]), True
        p += VERSION_HASH_BYTES
    h.profile_id, p = get_varint(blob, p)
    if p >= len(blob):
        raise MixzError("truncated header")
    h.maxbits = blob[p]
    p += 1
    if not 16 <= h.maxbits <= 24:
        raise MixzError("corrupt header (maxbits)")
    h.codec, h.profile, h.primer = version_info(h.version)
    if h.profile == P_IMAGE:
        h.stride, p = get_varint(blob, p)
        h.bpp, p = get_varint(blob, p)
        if not 1 <= h.bpp <= 8 or not 1 <= h.stride <= MAX_SIZE:
            raise MixzError("corrupt header (image geometry)")
    h.stream_len = size
    if transform != T_NONE:
        h.stream_len, p = get_varint(blob, p)
        if transform == T_E8E9 and h.stream_len != size:
            raise MixzError("corrupt header (size)")
    if transform in (T_GZIP, T_PNG):
        h.deflate, p = get_varint(blob, p)
        if h.deflate not in registry()["deflates"] or h.deflate != DEFLATE:
            raise MixzError(f"deflate implementation {h.deflate} is not in this build")
    if bool(flags & F_META) != (transform in (T_GZIP, T_PNG)):
        raise MixzError("corrupt header (flags)")
    if flags & F_META:
        mlen, p = get_varint(blob, p)
        if p + mlen > len(blob):
            raise MixzError("truncated header")
        h.meta = blob[p: p + mlen]
        p += mlen
    if flags & F_BLOCKS:
        n, p = get_varint(blob, p)
        if n < 2 or n > h.stream_len or 6 * n > len(blob) - p:  # an entry takes at least 6 bytes
            raise MixzError("corrupt header (block table)")
        for _ in range(n):
            raw_len, p = get_varint(blob, p)
            comp_len, p = get_varint(blob, p)
            h.blocks.append((raw_len, comp_len, _u32(blob, p)))
            p += 4
        if (sum(b[0] for b in h.blocks) != h.stream_len or sum(b[1] for b in h.blocks) != len(blob) - p
                or any(b[0] == 0 for b in h.blocks)):
            raise MixzError("corrupt header (block table does not match the data)")
        for raw_len, comp_len, _ in h.blocks:
            _check_ratio(raw_len, comp_len)
    _check_ratio(h.stream_len, len(blob) - p)
    h.payload_at = p
    return h


def decompress(blob: bytes) -> bytes:
    try:
        return _decompress(blob)
    except MixzError:
        raise
    except Exception as e:  # corrupt header/meta: report, never return wrong data
        raise MixzError(f"corrupt file ({e.__class__.__name__}: {e})") from None


def _decompress(blob: bytes) -> bytes:
    h = parse_header(blob)
    if h.method == M_BUNDLE:  # its members joined
        data = b"".join(_decompress_bundle(blob))
    elif h.method == M_STORED:
        data = blob[h.payload_at: h.payload_at + h.size]
    else:
        check_module(h.primer)
        payload = blob[h.payload_at:]
        if h.blocks:
            spans, q = [], 0
            for raw_len, comp_len, bcrc in h.blocks:
                spans.append((q, comp_len, raw_len, bcrc))
                q += comp_len

            def dec(span):
                q, comp_len, raw_len, bcrc = span
                part = cm_decode(payload[q: q + comp_len], raw_len, h.profile, h.primer, h.stride, h.bpp, h.maxbits,
                                 h.codec, snap=True)
                if zlib.crc32(part) != bcrc:
                    raise MixzError("CRC mismatch in a block: data is corrupt")
                return part
            stream = b"".join(_block_map(dec, spans))
        else:
            stream = cm_decode(payload, h.stream_len, h.profile, h.primer, h.stride, h.bpp, h.maxbits, h.codec)
        data = inverse_transform(h.transform, stream, h.meta)
    if len(data) != h.size or zlib.crc32(data) != h.crc:
        raise MixzError("CRC mismatch: data is corrupt")
    return data


# Bundle (format 2, method 2): several files in one container. Members with the same plan (no transform,
# the same engine profile and primer) are joined into one model stream, a group, so the model is primed
# and restored once per group instead of once per file, and each member is predicted from the ones
# before it. Any other member is a complete mixz file inside the bundle. Reading one member decodes its
# group up to that member's end.
#   "MXZ" | 2 | flags (method 2) | total size varint | crc32 of the members joined
#   groups varint, then per group: version, profile varint, maxbits (1), payload bytes varint
#     (version as in a single file: varint ID >= 1, or 0 and VERSION_HASH_BYTES of its hash)
#   members varint, then per member: size varint, crc32 (4), group varint (1-based; 0: a mixz file of
#     its own, then its length varint)
#   the groups' payloads, then the members' own files, in order


def compress_bundle(items: list, maxbits: int = DEFAULT_MAXBITS, codec: int = CODEC) -> bytes:
    """Compress several files (a list of bytes) into one bundle. decompress_bundle returns the list;
    decompress returns the members joined."""
    if codec not in CODECS:
        raise MixzError(f"codec {codec} is not in this build")
    if not 16 <= maxbits <= 24:
        raise MixzError("maxbits must be between 16 and 24")
    groups, key_of = {}, []  # (engine profile, primer) -> member indices
    for i, data in enumerate(items):
        plan = make_plan(data) if len(data) >= 16 else None
        key = (plan.profile, plan.primer) if plan and plan.transform == T_NONE and plan.profile != P_IMAGE else None
        if key:
            groups.setdefault(key, []).append(i)
        key_of.append(key)
    keys = list(groups)
    payloads = _block_map(lambda k: cm_encode(b"".join(items[i] for i in groups[k]), k[0], k[1], 0, 1, maxbits,
                                              codec, snap=len(keys) > 1), keys)
    gid, table, body = {}, bytearray(), []
    for k, payload in zip(keys, payloads):
        if payload is None:  # the model would make the group larger: its members become files of their own
            continue
        gid[k] = len(gid) + 1
        table += (put_varint(version_id(k[0], k[1], codec)) + put_varint(profile_id(Plan(T_NONE, k[0], b"")))
                  + bytes([maxbits]) + put_varint(len(payload)))
        body.append(payload)
    table = put_varint(len(gid)) + table + put_varint(len(items))
    crc = 0
    for i, data in enumerate(items):
        g = gid.get(key_of[i], 0)
        table += put_varint(len(data)) + struct.pack("<I", zlib.crc32(data)) + put_varint(g)
        if not g:
            own = _compress(data, maxbits, True, codec=codec)
            table += put_varint(len(own))
            body.append(own)
        crc = zlib.crc32(data, crc)
    return bytes(_head(M_BUNDLE, T_NONE, 0, sum(map(len, items)), crc) + table) + b"".join(body)


@_record
class BundleGroup:
    codec: int
    profile: int
    primer: int
    maxbits: int
    at: int  # payload position in the bundle
    length: int  # payload bytes
    stream_len: int = 0
    members: list = []


def parse_bundle(blob: bytes):
    """(header, groups, members): members are (size, crc32, group index or -1, own file position, own
    file length); each group lists its members in stream order."""
    h = parse_header(blob)
    if h.method != M_BUNDLE:
        raise MixzError("not a mixz bundle")
    p = h.payload_at
    ng, p = get_varint(blob, p)
    if 4 * ng > len(blob) - p:  # a group entry takes at least 4 bytes
        raise MixzError("corrupt header (bundle groups)")
    groups = []
    for _ in range(ng):
        vid, p = get_varint(blob, p)
        if vid == 0:
            if p + VERSION_HASH_BYTES > len(blob):
                raise MixzError("truncated header")
            vid = version_by_hash(blob[p: p + VERSION_HASH_BYTES])
            p += VERSION_HASH_BYTES
        codec, profile, primer = version_info(vid)
        if profile == P_IMAGE:
            raise MixzError("corrupt header (an image group in a bundle)")
        _, p = get_varint(blob, p)  # profile ID: informational
        if p >= len(blob) or not 16 <= blob[p] <= 24:
            raise MixzError("corrupt header (maxbits)")
        maxbits = blob[p]
        length, p = get_varint(blob, p + 1)
        groups.append(BundleGroup(codec, profile, primer, maxbits, 0, length))
    nm, p = get_varint(blob, p)
    if 6 * nm > len(blob) - p:  # a member entry takes at least 6 bytes
        raise MixzError("corrupt header (bundle members)")
    members = []
    for i in range(nm):
        size, p = get_varint(blob, p)
        crc = _u32(blob, p)
        g, p = get_varint(blob, p + 4)
        if size > MAX_SIZE or g > ng:
            raise MixzError("corrupt header (bundle members)")
        own = 0
        if g:
            groups[g - 1].members.append(i)
            groups[g - 1].stream_len += size
        else:
            own, p = get_varint(blob, p)
        members.append([size, crc, g - 1, 0, own])
    if sum(m[0] for m in members) != h.size:
        raise MixzError("corrupt header (bundle sizes)")
    for gr in groups:
        if not gr.members or not gr.stream_len:
            raise MixzError("corrupt header (an empty bundle group)")
        gr.at, p = p, p + gr.length
        _check_ratio(gr.stream_len, gr.length)
    for m in members:
        if m[2] < 0:
            m[3], p = p, p + m[4]
    if p != len(blob):
        raise MixzError("truncated file" if p > len(blob) else "corrupt file (bytes after the end)")
    return h, groups, [tuple(m) for m in members]


def decompress_bundle(blob: bytes, index: int = None):
    """The members of a bundle as a list, or with index only that member (its group is decoded up to
    the member's end). Every member is checked against its CRC."""
    try:
        return _decompress_bundle(blob, index)
    except MixzError:
        raise
    except Exception as e:  # corrupt header: report, never return wrong data
        raise MixzError(f"corrupt file ({e.__class__.__name__}: {e})") from None


def _decompress_bundle(blob: bytes, index: int = None):
    h, groups, members = parse_bundle(blob)
    if index is not None and not 0 <= index < len(members):
        raise MixzError(f"the bundle has no member {index}")
    want = range(len(members)) if index is None else [index]
    out = {}

    def own(i):
        f = blob[members[i][3]: members[i][3] + members[i][4]]
        if parse_header(f).method == M_BUNDLE:
            raise MixzError("corrupt file (a bundle inside a bundle)")
        return _decompress(f)

    def group(gi):
        gr = groups[gi]
        check_module(gr.primer)
        # one member: decode up to its end (the model is sized for the whole stream, as when coding)
        last = gr.members.index(index) if index is not None else len(gr.members) - 1
        n = sum(members[i][0] for i in gr.members[: last + 1])
        stream = cm_decode(blob[gr.at: gr.at + gr.length], n, gr.profile, gr.primer, 0, 1, gr.maxbits, gr.codec,
                           snap=len(groups) > 1, total=gr.stream_len)
        q, parts = 0, {}
        for i in gr.members[: last + 1]:
            parts[i] = stream[q: q + members[i][0]]
            q += members[i][0]
        return parts
    gis = sorted({members[i][2] for i in want if members[i][2] >= 0})
    for parts in _block_map(group, gis):
        out.update(parts)
    for i in want:
        if members[i][2] < 0:
            out[i] = own(i)
    for i in want:
        if len(out[i]) != members[i][0] or zlib.crc32(out[i]) != members[i][1]:
            raise MixzError("CRC mismatch in a bundle member: data is corrupt")
    return out[index] if index is not None else [out[i] for i in want]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def describe(blob: bytes) -> str:
    try:
        h = parse_header(blob)
    except (struct.error, IndexError) as e:
        raise MixzError(f"corrupt header ({e})") from None
    if h.method == M_STORED:
        return f"stored  size={h.size}  format={h.fmt}"
    if h.method == M_BUNDLE:
        _, groups, members = parse_bundle(blob)
        return (f"bundle  size={h.size} -> {len(blob)} ({len(blob) / max(h.size, 1):.1%})  format={h.fmt}  "
                f"members={len(members)}  groups={len(groups)} (" + ", ".join(
                    f"{PROFILE_NAMES[g.profile]}/{PRIMER_IDS[g.primer] or 'none'}: {len(g.members)}" for g in groups)
                + f")  own files={sum(m[2] < 0 for m in members)}")
    out = (f"cm  size={h.size} -> {len(blob)} ({len(blob) / max(h.size, 1):.1%})  format={h.fmt}  "
           f"engine={PROFILE_NAMES[h.profile]}  transform={TRANSFORM_NAMES[h.transform]}  "
           f"primer={PRIMER_IDS[h.primer] or 'none'}")
    prof = registry()["profiles"].get(h.profile_id, {}).get("name", f"#{h.profile_id}")
    return out + (f"  codec={h.codec}  version={h.version}{' (named by hash)' if h.by_hash else ''} "
                  f"[{version_hash(h.version)[:8].hex()}]  profile={prof}  blocks={max(1, len(h.blocks))}")


# --------------------------------------------------------------------------
# service mode: a long-running process keeps the primed snapshots and the per-thread workers loaded,
# so a CLI call costs the coding of its file instead of priming (~0.8 s per primer). The server runs
# the same compress()/decompress() as the CLI, so output is identical.
# --------------------------------------------------------------------------
def socket_path() -> str:
    """MIXZ_SOCKET, else a socket in a directory only this user can enter."""
    if os.environ.get("MIXZ_SOCKET"):
        return os.environ["MIXZ_SOCKET"]
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/mixz-{os.getuid()}"
    return os.path.join(base, "mixz.sock")


def build_identity() -> str:
    """What the output depends on, as files: code, registry, engine sources or library, primers.
    The server refuses clients whose files differ (a server left running after an update)."""
    paths = [os.path.abspath(__file__), REGISTRY_PATH, os.environ.get("MIXZ_LIB") or LIB] + engine_files() + sorted(
        os.path.join(PRIMER_DIR, f) for f in os.listdir(PRIMER_DIR))
    parts = []
    for p in paths:
        try:
            st = os.stat(p)
            parts.append(f"{p}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{p}:missing")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


MAX_REQUEST = 1 << 34  # 16 GiB; the server holds a request in memory


def _send(sock, header: dict, data: bytes = b"") -> None:
    h = json.dumps(dict(header, size=len(data))).encode()
    sock.sendall(struct.pack(">I", len(h)) + h)
    if data:
        sock.sendall(data)


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 1 << 20))
        if not chunk:
            raise MixzError("connection closed")
        buf += chunk
    return bytes(buf)


def _recv(sock):
    (hlen,) = struct.unpack(">I", _recv_exact(sock, 4))
    if hlen > 1 << 16:
        raise MixzError("bad message")
    header = json.loads(_recv_exact(sock, hlen))
    size = header.get("size", 0)
    if not isinstance(size, int) or not 0 <= size <= MAX_REQUEST:
        raise MixzError("bad message")
    return header, _recv_exact(sock, size)


def _serve_one(header: dict, data: bytes) -> bytes:
    op = header.get("op")
    if op == "c":
        return compress(data, maxbits=int(header["maxbits"]), use_primers=bool(header["use_primers"]),
                        verify=bool(header["verify"]), block_size=int(header["block_size"]))
    if op in ("d", "t"):
        out = decompress(data)
        return out if op == "d" else b""
    raise MixzError(f"unknown request {op!r}")


def serve(path: str, jobs: int, cache: int) -> int:
    import signal
    import socket
    import socketserver
    global USE_SNAPSHOTS, SNAP_CACHE
    USE_SNAPSHOTS, SNAP_CACHE = True, cache
    engine()
    ident = build_identity()
    d = os.path.dirname(path)
    if not os.environ.get("MIXZ_SOCKET"):
        os.makedirs(d, mode=0o700, exist_ok=True)
        st = os.stat(d)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise MixzError(f"{d} must belong to this user and be closed to others (mode 700)")
    if os.path.exists(path):
        try:
            with socket.socket(socket.AF_UNIX) as c:
                c.connect(path)
            raise MixzError(f"a server is already running on {path}")
        except (ConnectionRefusedError, FileNotFoundError):
            os.unlink(path)  # left by a server that did not exit cleanly
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=max(1, jobs))  # fixed threads: each keeps its worker model
    peercred = getattr(socket, "SO_PEERCRED", None)

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            sock = self.request
            try:
                if peercred is not None:
                    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, peercred, struct.calcsize("3i")))
                    if uid != os.getuid():
                        return
                header, data = _recv(sock)
                if header.get("identity") != ident:
                    _send(sock, {"ok": False, "stale": True,
                                 "error": "the server runs other mixz files than this client: restart 'mixz serve'"})
                    return
                try:
                    out = pool.submit(_serve_one, header, data).result()
                    _send(sock, {"ok": True}, out)
                except MixzError as e:
                    _send(sock, {"ok": False, "error": str(e)})
            except (OSError, MixzError, ValueError, KeyError):
                pass  # client went away or sent garbage: drop the connection

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    old_mask = os.umask(0o077)
    try:
        srv = Server(path, Handler)
    finally:
        os.umask(old_mask)
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=srv.shutdown).start())
    print(f"mixz server on {path} ({jobs} threads, up to {cache} primed models of ~155 MB)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        try:
            os.unlink(path)
        except OSError:
            pass
        pool.shutdown(wait=False, cancel_futures=True)
    return 0


class _Client:
    """Sends c/d/t requests to a running server; None from run() means: do it in this process."""

    def __init__(self, path: str):
        self.path, self.ident, self.dead = path, None, not os.path.exists(path)

    def run(self, header: dict, data: bytes):
        import socket
        if self.dead:
            return None
        if self.ident is None:
            self.ident = build_identity()
        try:
            with socket.socket(socket.AF_UNIX) as sock:
                sock.connect(self.path)
                _send(sock, dict(header, identity=self.ident), data)
                resp, out = _recv(sock)
        except (OSError, MixzError):
            self.dead = True  # no server (or it stopped): work locally from now on
            return None
        if resp.get("stale"):
            print(f"mixz: {resp['error']}; working without it", file=sys.stderr)
            self.dead = True
            return None
        if not resp.get("ok"):
            raise MixzError(resp.get("error", "server error"))
        return out


def parse_size(text: str) -> int:
    """'65536', '64K', '1M' -> bytes."""
    t = text.strip().upper()
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}.get(t[-1:], 1)
    return int(t[:-1] if mult > 1 else t) * mult


def _job(args, path, client):
    t = time.time()
    try:
        with open(path, "rb") as f:
            data = f.read()
        out = None
        if args.cmd == "c":
            header = {"op": "c", "maxbits": args.maxbits, "use_primers": not args.no_primer, "verify": args.verify,
                      "block_size": args.block_size}
            out = client.run(header, data) if client else None
            if out is None:
                out = compress(data, maxbits=args.maxbits, use_primers=not args.no_primer, verify=args.verify,
                               block_size=args.block_size)
            dst = args.output or path + ".mxz"
            msg = f"{path}: {len(data)} -> {len(out)} ({len(out) / max(len(data), 1):.1%})"
        elif args.cmd == "d":
            out = client.run({"op": "d"}, data) if client else None
            if out is None:
                out = decompress(data)
            dst = args.output or (path[:-4] if path.endswith(".mxz") else path + ".out")
            msg = f"{path}: -> {dst} ({len(out)} bytes)"
        elif args.cmd == "t":
            if (client.run({"op": "t"}, data) if client else None) is None:
                decompress(data)
            return f"{path}: OK", True
        else:
            return f"{path}: {describe(data)}", True
        if os.path.exists(dst) and not args.force:
            return f"{path}: {dst} exists (use -f to overwrite)", False
        with open(dst, "wb") as f:
            f.write(out)
        return f"{msg}  [{time.time() - t:.2f}s]", True
    except (OSError, MixzError) as e:
        return f"{path}: error: {e}", False


def main(argv=None) -> int:
    global USE_SNAPSHOTS, SNAP_CACHE
    ap = argparse.ArgumentParser(prog="mixz", description="format-aware context-mixing compressor")
    ap.add_argument("cmd", choices=["c", "d", "t", "info", "serve"],
                    help="c=compress d=decompress t=test info=describe serve=keep primed models loaded for c/d/t")
    ap.add_argument("files", nargs="*")
    ap.add_argument("-o", "--output", help="output path (single input only)")
    ap.add_argument("-f", "--force", action="store_true", help="overwrite existing outputs")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="files processed in parallel")
    ap.add_argument("--maxbits", type=int, default=DEFAULT_MAXBITS,
                    help="model size cap: 2^N buckets per context (16..24, default 20 = ~128 MB)")
    ap.add_argument("--no-primer", action="store_true", help="do not use per-format primers")
    ap.add_argument("--block-size", type=parse_size, default=AUTO_BLOCKS, metavar="N",
                    help="split each file into blocks of about N bytes (e.g. 64M), coded at the same time on "
                         "several cores; 0: one block; default: one block below 1 GiB, else blocks of 512 MiB or more")
    ap.add_argument("--block-threads", type=int, default=0, metavar="N",
                    help="threads for one file's blocks (default: one per core); the output does not depend on it")
    ap.add_argument("--verify", action="store_true", help="decompress after compressing and compare (2x slower)")
    ap.add_argument("--no-server", action="store_true", help="do not use a running 'mixz serve'")
    ap.add_argument("--socket", default=None, help="server socket (default: $MIXZ_SOCKET, else in $XDG_RUNTIME_DIR)")
    ap.add_argument("--models", type=int, default=None, metavar="N",
                    help=f"primed models kept in memory, ~155 MB each (default {SNAP_CACHE}; serve: 8)")
    args = ap.parse_intermixed_args(argv)  # options may sit between the command and the files (Python < 3.12)
    path = args.socket or socket_path()
    if args.cmd == "serve":
        if args.files:
            ap.error("serve takes no files")
        try:
            return serve(path, args.jobs, max(1, args.models or 8))
        except MixzError as e:
            print(f"mixz: {e}", file=sys.stderr)
            return 1
    if not args.files:
        ap.error("no input files")
    if args.output and len(args.files) > 1:
        ap.error("-o needs a single input file")
    client = None if args.no_server or args.cmd == "info" else _Client(path)
    if client is None or client.dead:
        engine()
    USE_SNAPSHOTS = len(args.files) > 1
    SNAP_CACHE = max(1, args.models or SNAP_CACHE)
    set_block_threads(args.block_threads)
    ok = True

    def emit(results):
        nonlocal ok
        for msg, good in results:
            print(msg, file=sys.stdout if good else sys.stderr)
            ok &= good
    if len(args.files) == 1 or args.jobs <= 1:  # no thread pool: its import costs ~7 ms per call
        emit(_job(args, p, client) for p in args.files)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            emit(ex.map(lambda p: _job(args, p, client), args.files))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
