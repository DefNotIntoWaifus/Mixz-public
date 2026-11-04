"""Format detection and the format-aware steps, as in mixz.py: what to undo before coding, and how.

For each file, make_plan() decides:
  - PNG: decompress the image data, undo the row filters, code the pixels with the image engine
    profile. The deflate settings that rebuild the exact original bytes are found by trying them.
  - gzip: decompress, code the content, and keep the deflate settings that rebuild the exact bytes.
  - x86 programs (ELF or PE): the E8/E9 transform, then the binary profile.
  - text (97% or more printable bytes): the text profile, or code or web if a primer for those won.
  - other data: the binary profile, unless it looks already compressed (then the file is stored).
A transform is used only when its inverse gives back the original bytes; container.py checks that.
"""
import base64
import json
import os
import struct
import zlib
from dataclasses import dataclass

from . import transform
from .deflate import Deflate, deflate

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the repository
PRIMER_DIR = os.path.join(ROOT, "primers")

T_NONE, T_E8E9, T_GZIP, T_PNG = 0, 1, 2, 3
P_TEXT, P_BIN, P_IMAGE, P_CODE, P_WEB = 0, 1, 2, 3, 4
PROFILE_NAMES = {P_TEXT: "text", P_BIN: "binary", P_IMAGE: "image", P_CODE: "code", P_WEB: "web"}
TRANSFORM_NAMES = {T_NONE: "none", T_E8E9: "x86-e8e9", T_GZIP: "gzip-recompress", T_PNG: "png-recompress"}
# Primer IDs are part of the file format: append only, never reorder.
PRIMER_IDS = ["", "py", "c", "js", "web", "json", "prose", "man", "elf"]
TEXT_PRIMERS = ["py", "c", "js", "web", "json", "prose", "man"]
BIN_PRIMERS = ["elf"]
# a primer for these kinds also selects a text sub-profile (docs/context.md section 2.8)
TEXT_SUBPROFILES = {"py": P_CODE, "c": P_CODE, "js": P_CODE, "web": P_WEB}


class MixzError(Exception):
    pass


@dataclass
class Plan:
    transform: int
    profile: int
    stream: bytes  # what the codec codes
    stride: int = 0  # image: bytes per row
    bpp: int = 1  # image: bytes per pixel
    meta: bytes = b""  # gzip and PNG: what rebuilds the original around the stream
    primer: int = 0
    note: str = ""


# ----------------------------------------------------------------------------------------------
# varints: 7 bits per byte, the high bit set on every byte but the last
# ----------------------------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------------------------
# primers
# ----------------------------------------------------------------------------------------------
_primers: dict = {}
_zdicts: dict = {}


def primer_bytes(pid: int):
    """The primer's bytes, or None if its file is missing."""
    if pid == 0:
        return b""
    if pid not in _primers:
        try:
            with open(os.path.join(PRIMER_DIR, PRIMER_IDS[pid] + ".bin"), "rb") as f:
                _primers[pid] = f.read()
        except OSError:
            _primers[pid] = None
    return _primers[pid]


def available_primers(names) -> list:
    return [PRIMER_IDS.index(n) for n in names if primer_bytes(PRIMER_IDS.index(n))]


def sample(data: bytes, size: int = 65536) -> bytes:
    """The start, middle and end of the data, `size` bytes in all."""
    if len(data) <= size:
        return data
    third = size // 3
    mid = len(data) // 2
    return data[:third] + data[mid: mid + third] + data[-third:]


def choose_primer(data: bytes, candidates: list) -> int:
    """The primer that predicts this file best, by proxy: a fast compressor (zstd if installed, else
    zlib) using the primer as a dictionary on a sample of the file. A primer must win by 1%."""
    if not candidates:
        return 0
    s = sample(data)
    try:
        import zstandard

        def size(pid):
            if pid == 0:
                return len(zstandard.ZstdCompressor(level=6).compress(s))
            if pid not in _zdicts:
                _zdicts[pid] = zstandard.ZstdCompressionDict(primer_bytes(pid),
                                                             dict_type=zstandard.DICT_TYPE_RAWCONTENT)
            return len(zstandard.ZstdCompressor(level=6, dict_data=_zdicts[pid]).compress(s))
    except ImportError:
        def size(pid):
            c = zlib.compressobj(9, zlib.DEFLATED, 15, 9, 0, primer_bytes(pid)[-32768:]) if pid else zlib.compressobj(9)
            return len(c.compress(s) + c.flush())
    best_pid, best = 0, size(0)
    for pid in candidates:
        n = size(pid)
        if n < best * 0.99:
            best_pid, best = pid, n
    return best_pid


# ----------------------------------------------------------------------------------------------
# detection
# ----------------------------------------------------------------------------------------------
_CONTROL = list(range(0, 9)) + [11, 12] + list(range(14, 32)) + [127]


def text_score(s: bytes) -> float:
    """The share of bytes that are not control characters."""
    if not s:
        return 0.0
    return 1.0 - sum(s.count(bytes([b])) for b in _CONTROL) / len(s)


def x86_executable(data: bytes) -> bool:
    if data[:4] == b"\x7fELF" and len(data) > 20:
        endian = "<" if data[5] == 1 else ">"
        return struct.unpack(endian + "H", data[18:20])[0] in (3, 62)  # EM_386, EM_X86_64
    if data[:2] == b"MZ" and len(data) > 0x40:
        off = struct.unpack("<I", data[0x3C:0x40])[0]
        if data[off: off + 4] == b"PE\0\0":
            return struct.unpack("<H", data[off + 4: off + 6])[0] in (0x14C, 0x8664)
    return False


def looks_incompressible(data: bytes) -> bool:
    """Already compressed or random: a fast compressor saves less than 3% on a sample."""
    s = sample(data, 1 << 18)
    try:
        import zstandard
        c = len(zstandard.ZstdCompressor(level=1).compress(s))
    except ImportError:
        c = len(zlib.compress(s, 1))
    return c > 0.97 * len(s)


# ----------------------------------------------------------------------------------------------
# gzip and PNG: rebuild the exact deflate bytes
# ----------------------------------------------------------------------------------------------
def find_deflate_params(raw: bytes, comp: bytes, wbits: int, level_hint=None):
    """(level, memLevel, strategy) with which deflate turns `raw` into exactly `comp`, or None. Each
    candidate is fed 64 KB at a time, as the C version does, and dropped at its first wrong byte."""
    levels = list(level_hint or []) + [lv for lv in (9, 6, 1, 2, 3, 4, 5, 7, 8) if lv not in (level_hint or [])]
    for strategy in (0, 1):
        for mem in (8, 9, 7, 6, 5, 4, 3, 2, 1):
            for level in levels:
                c = Deflate(level, wbits, mem, strategy)
                out = bytearray()
                ok = True
                for i in range(0, len(raw), 1 << 16):
                    n = len(out)
                    out += c.compress(raw[i: i + (1 << 16)])
                    if out[n:] != comp[n: len(out)]:
                        ok = False
                        break
                if ok:
                    out += c.flush()
                    if out == comp:
                        return level, mem, strategy
            if mem == 9 and strategy == 0 and wbits < 0:
                break  # gzip: other memory levels essentially never occur
    return None


def gzip_forward(data: bytes):
    """(content, meta) of a single-member gzip file whose deflate data can be rebuilt, else None.
    meta: the deflate settings (3 bytes), the gzip header, the trailer if it is not the standard one,
    and any bytes after the end."""
    if len(data) < 18 or data[:3] != b"\x1f\x8b\x08":
        return None
    flg = data[3]
    p = 10
    try:
        if flg & 4:  # FEXTRA
            p += 2 + struct.unpack("<H", data[p: p + 2])[0]
        if flg & 8:  # FNAME
            p = data.index(b"\0", p) + 1
        if flg & 16:  # FCOMMENT
            p = data.index(b"\0", p) + 1
        if flg & 2:  # FHCRC
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
        return None  # several members: not handled
    params = find_deflate_params(raw, comp, -15)
    if params is None:
        return None
    trailer, rest = tail[:8], tail[8:]
    std = trailer == struct.pack("<II", zlib.crc32(raw), len(raw) & 0xFFFFFFFF)
    meta = bytes(params) + put_varint(p) + data[:p] + bytes([1 if std else 0])
    if not std:
        meta += put_varint(len(trailer)) + trailer
    return raw, meta + rest


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
    """(pixels, meta, bytes per row, bytes per pixel) of a non-interlaced PNG whose image data can be
    rebuilt, else None. meta (JSON, deflated) keeps the other chunks, the IDAT sizes, the deflate
    settings and each row's filter type."""
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
    hint = {0: [1], 1: [2, 3, 4, 5], 2: [6], 3: [7, 8, 9]}[z[1] >> 6]  # the zlib header's level field
    params = find_deflate_params(raw, z, wbits, hint)
    if params is None:
        return None
    r = transform.png_unfilter(raw, h, rowbytes, bpp)
    if r is None:
        return None
    pix, types = r
    meta = {
        "pre": [[c[0].decode("latin-1"), base64.b64encode(c[1]).decode()] for c in chunks[: idx[0]]],
        "post": [[c[0].decode("latin-1"), base64.b64encode(c[1]).decode()] for c in chunks[idx[-1] + 1:]],
        "idat": [len(chunks[i][1]) for i in idx],
        "zp": list(params) + [wbits],
        "h": h, "rowbytes": rowbytes, "bpp": bpp,
        "types": base64.b64encode(types).decode(),
        "tail": base64.b64encode(data[end:]).decode(),
    }
    return pix, deflate(json.dumps(meta, separators=(",", ":")).encode(), 9, 8, 0, 15), rowbytes, bpp


def png_inverse(pix: bytes, meta_blob: bytes) -> bytes:
    meta = json.loads(zlib.decompress(meta_blob))
    h, rowbytes, bpp = meta["h"], meta["rowbytes"], meta["bpp"]
    types = base64.b64decode(meta["types"])
    if len(types) != h or len(pix) != h * rowbytes or not 1 <= bpp <= 8 or any(t > 4 for t in types):
        raise MixzError("corrupt PNG metadata")
    raw = transform.png_refilter(pix, types, h, rowbytes, bpp)
    level, mem, strategy, wbits = meta["zp"]
    z = deflate(raw, level, mem, strategy, wbits)
    chunks = [(t.encode("latin-1"), base64.b64decode(b)) for t, b in meta["pre"]]
    p = 0
    for size in meta["idat"]:
        chunks.append((b"IDAT", z[p: p + size]))
        p += size
    chunks += [(t.encode("latin-1"), base64.b64decode(b)) for t, b in meta["post"]]
    return png_build(chunks) + base64.b64decode(meta["tail"])


def inverse_transform(t: int, stream: bytes, meta: bytes) -> bytes:
    if t == T_NONE:
        return stream
    if t == T_E8E9:
        return transform.e8e9(stream, inverse=True)
    if t == T_GZIP:
        return gzip_inverse(stream, meta)
    if t == T_PNG:
        return png_inverse(stream, meta)
    raise MixzError(f"unknown transform {t}")


# ----------------------------------------------------------------------------------------------
# the plan for a file
# ----------------------------------------------------------------------------------------------
def plan_for_plain(data: bytes, note: str = "") -> Plan:
    if x86_executable(data):
        stream = transform.e8e9(data, inverse=False)
        return Plan(T_E8E9, P_BIN, stream, primer=choose_primer(stream, available_primers(BIN_PRIMERS)),
                    note=note or "x86 executable")
    if text_score(sample(data)) > 0.97:
        primer = choose_primer(data, available_primers(TEXT_PRIMERS))
        prof = TEXT_SUBPROFILES.get(PRIMER_IDS[primer], P_TEXT)
        return Plan(T_NONE, prof, data, primer=primer, note=note or PROFILE_NAMES[prof])
    return Plan(T_NONE, P_BIN, data, primer=choose_primer(data, available_primers(BIN_PRIMERS)),
                note=note or "binary")


def make_plan(data: bytes):
    """The Plan for a file, or None when it should simply be stored."""
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
    # skip the slow model on data that is already compressed or random; small files and text always
    # get a real attempt
    if len(data) >= 4096 and text_score(sample(data)) <= 0.97 and looks_incompressible(data):
        return None
    return plan_for_plain(data)
