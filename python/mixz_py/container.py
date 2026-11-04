"""The .mxz file format (format 2) and the registry, as in mixz.py: compress, decompress and bundles.

A single file:
  "MXZ" | 2 | flags | size varint | crc32 (4, little-endian) | payload                      (stored)
  "MXZ" | 2 | flags | size varint | crc32 | version | profile varint | maxbits (1)
    version: varint ID >= 1 from registry.json, or 0 followed by the first 16 bytes of the
    version's hash (version_hash), for versions named by content
    [stride varint, bpp varint]   when the version's engine profile is image
    [model stream length varint]  when there is a transform
    [deflate varint]              gzip and PNG: the deflate implementation that rebuilds the stream
    [meta length varint, meta]    gzip and PNG
    [blocks varint >= 2, then per block: model bytes varint, payload bytes varint, crc32 (4)]
    payload: the blocks' coded data, one after the other; each block is coded from a fresh model
  flags: bits 0-1 method (0 stored, 1 model, 2 bundle), bits 2-4 transform, bit 5 meta,
  bit 6 block table, bit 7 reserved (0)

A version (registry.json) names everything the decoder needs: the codec and the primers it replays.
The registry and the primers are read from the repository this folder belongs to.
"""
import hashlib
import json
import os
import struct
import zlib
from dataclasses import dataclass, field

from . import codec
from .formats import (P_BIN, P_CODE, P_IMAGE, P_TEXT, P_WEB, PRIMER_IDS, PROFILE_NAMES, ROOT, T_E8E9, T_GZIP,
                      T_NONE, T_PNG, TRANSFORM_NAMES, MixzError, Plan, get_varint, inverse_transform, make_plan,
                      primer_bytes, put_varint, sample, text_score)

REGISTRY_PATH = os.path.join(ROOT, "registry.json")
MAGIC = b"MXZ"
FORMAT = 2
M_STORED, M_CM, M_BUNDLE = 0, 1, 2
F_META, F_BLOCKS, F_RESERVED = 1 << 5, 1 << 6, 1 << 7
CODEC = 3  # the codec in codec.py (engine/cm.c)
DEFLATE = 1  # deflate.py (zlib 1.3.1)
DEFAULT_MAXBITS = 20
ENGINE_PROFILES = {"text": P_TEXT, "binary": P_BIN, "image": P_IMAGE, "code": P_CODE, "web": P_WEB}
SECTIONS = ("codecs", "deflates", "modules", "versions", "profiles")
VERSION_HASH_BYTES = 16
MAX_SIZE = 1 << 40
# A bit costs at least 0.0007 bits (the coder caps probabilities at 65504/65536), so a payload byte
# holds at most ~1,420 model bytes. Larger claims are corrupt or malicious headers.
MAX_RATIO = 2048
# Block size when none is given: one block below 1 GiB, else blocks of at least 512 MiB.
AUTO_BLOCKS, SPLIT_MIN, BLOCK_MIN = -1, 1 << 30, 512 << 20


# ----------------------------------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------------------------------
_registry = None


def registry() -> dict:
    global _registry
    if _registry is None:
        with open(REGISTRY_PATH) as f:
            r = json.load(f)
        reg = {k: {int(i): v for i, v in r[k].items()} for k in SECTIONS}
        reg["retired"] = {k: set(v) for k, v in r.get("retired", {}).items() if k in SECTIONS}
        reg["by_hash"] = {version_hash(vid, reg): vid for vid in reg["versions"]}
        _registry = reg
    return _registry


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def version_hash(vid: int, reg: dict = None) -> bytes:
    """A version's identity: sha256 over what it stands for (the codec's source hash, the engine
    profile, each primer's sha256, the start weights). The same on every machine."""
    reg = reg or registry()
    v = reg["versions"][vid]
    return hashlib.sha256(b"mixz-version\0" + _canon({
        "codec": reg["codecs"][v["codec"]]["engine_sha256"], "engine_profile": v["engine_profile"],
        "modules": [reg["modules"][m]["sha256"] for m in v["modules"]], "weights": v["weights"]})).digest()


def version_id(profile: int, pid: int) -> int:
    for vid, v in registry()["versions"].items():
        if (v["codec"] == CODEC and ENGINE_PROFILES[v["engine_profile"]] == profile
                and v["modules"] == ([pid] if pid else []) and v["weights"] is None):
            return vid
    raise MixzError(f"no registered version for the {PROFILE_NAMES[profile]} profile with primer "
                    f"'{PRIMER_IDS[pid] or 'none'}' and codec {CODEC}")


def version_by_hash(hp: bytes) -> int:
    for h, vid in registry()["by_hash"].items():
        if h[:len(hp)] == hp:
            return vid
    raise MixzError(f"the file needs mind version {hp.hex()}, which this registry does not have")


def version_info(vid: int):
    """(engine profile, primer id) of a registered version this code can decode."""
    v = registry()["versions"].get(vid)
    if v is None and vid in registry()["retired"].get("versions", ()):
        raise MixzError(f"version {vid} belongs to a pre-release codec that this mixz does not read")
    if v is None:
        raise MixzError(f"unknown version {vid}: the file needs a newer mixz")
    if v["codec"] != CODEC or v["weights"] is not None or len(v["modules"]) > 1:
        raise MixzError(f"version {vid} needs a codec or mind this code does not have")
    return ENGINE_PROFILES[v["engine_profile"]], (v["modules"][0] if v["modules"] else 0)


def check_module(pid: int) -> None:
    """A primer must be byte-identical to the registered one before it is used for decoding."""
    if not pid:
        return
    m = registry()["modules"][pid]
    data = primer_bytes(pid)
    if not data:
        raise MixzError(f"missing mind module {m['file']}")
    if hashlib.sha256(data).hexdigest() != m["sha256"]:
        raise MixzError(f"{m['file']} differs from registered module {pid}")


def profile_id(plan: Plan) -> int:
    """The detected content type (informational, stored in the header)."""
    names = {p["name"]: i for i, p in registry()["profiles"].items()}
    if plan.transform == T_PNG:
        return names["image/png"]
    if plan.transform == T_E8E9:
        return names["binary/x86"]
    if plan.profile in (P_CODE, P_WEB):
        return names["text/" + PROFILE_NAMES[plan.profile]]
    return names[PROFILE_NAMES[plan.profile]]


# ----------------------------------------------------------------------------------------------
# coding with the codec
# ----------------------------------------------------------------------------------------------
def cm_encode(data: bytes, profile: int, pid: int, stride: int, bpp: int, maxbits: int):
    return codec.compress(data, primer_bytes(pid) if pid else b"", profile, stride, bpp, maxbits)


def cm_decode(blob: bytes, n: int, profile: int, pid: int, stride: int, bpp: int, maxbits: int,
              total: int = 0) -> bytes:
    return codec.decompress(blob, n, primer_bytes(pid) if pid else b"", profile, stride, bpp, maxbits, total)


def auto_block_size(n: int) -> int:
    return 0 if n < SPLIT_MIN else max(BLOCK_MIN, -(-n // 64))


def split_blocks(n: int, block_size: int, row: int = 1) -> list:
    """Lengths of the blocks of an n-byte stream: ceil(n / block_size) blocks of nearly equal size
    (whole rows of `row` bytes for images)."""
    if row < 1 or n % row:
        row = 1
    rows = n // row
    k = min(-(-n // block_size), rows) if block_size > 0 and n > block_size else 1
    base, extra = divmod(rows, max(k, 1))
    return [(base + (i < extra)) * row for i in range(k)] if k > 1 else [n]


# ----------------------------------------------------------------------------------------------
# single files
# ----------------------------------------------------------------------------------------------
def _head(method: int, t: int, flags: int, size: int, crc: int) -> bytearray:
    return bytearray(MAGIC + bytes([FORMAT, method | (t << 2) | flags]) + put_varint(size) + struct.pack("<I", crc))


def _stored(data: bytes, crc: int) -> bytes:
    return bytes(_head(M_STORED, T_NONE, 0, len(data), crc)) + data


def compress(data: bytes, maxbits: int = DEFAULT_MAXBITS, use_primers: bool = True, verify: bool = False,
             block_size: int = AUTO_BLOCKS, by_hash: bool = False) -> bytes:
    """data -> .mxz bytes. block_size: split the model stream into blocks of about this many bytes
    (0: one block). by_hash: name the version by its hash instead of its registry ID."""
    out = _compress(data, maxbits, use_primers, block_size, by_hash)
    if verify and decompress(out) != data:
        raise MixzError("verification failed: please report this file")
    return out


def _compress(data: bytes, maxbits: int, use_primers: bool, block_size: int = AUTO_BLOCKS,
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
    # the transform must rebuild the original bytes exactly, else the raw bytes are coded
    if plan.transform != T_NONE and inverse_transform(plan.transform, plan.stream, plan.meta) != data:
        prof = P_TEXT if text_score(sample(data)) > 0.97 else P_BIN
        plan = Plan(T_NONE, prof, data, primer=0, note="transform check failed; coded raw")
    stream = plan.stream
    if block_size < 0:
        block_size = auto_block_size(len(stream))
    blocks, q = [], 0
    for n in split_blocks(len(stream), block_size, plan.stride if plan.profile == P_IMAGE else 1):
        blocks.append(stream[q: q + n])
        q += n
    payloads = [cm_encode(b, plan.profile, plan.primer, plan.stride, plan.bpp, maxbits) for b in blocks]
    if any(p is None for p in payloads):
        return _stored(data, crc)
    flags = (F_META if plan.meta else 0) | (F_BLOCKS if len(blocks) > 1 else 0)
    head = _head(M_CM, plan.transform, flags, len(data), crc)
    vid = version_id(plan.profile, plan.primer)
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


@dataclass
class Header:
    method: int
    size: int
    crc: int
    fmt: int = FORMAT
    profile: int = 0
    transform: int = 0
    primer: int = 0
    version: int = 0
    by_hash: bool = False
    profile_id: int = 0
    deflate: int = 0
    maxbits: int = DEFAULT_MAXBITS
    stride: int = 0
    bpp: int = 1
    stream_len: int = 0
    meta: bytes = b""
    blocks: list = field(default_factory=list)  # [(model bytes, payload bytes, crc32)]; empty: one block
    payload_at: int = 0


def _u32(blob: bytes, p: int) -> int:
    if p + 4 > len(blob):
        raise MixzError("truncated header")
    return struct.unpack("<I", blob[p: p + 4])[0]


def _check_ratio(stream_len: int, payload_len: int) -> None:
    if stream_len > MAX_RATIO * payload_len + 64:
        raise MixzError("corrupt or truncated file (the sizes in the header do not fit the data)")


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
    method, t = flags & 3, (flags >> 2) & 7
    if flags & F_RESERVED or method not in (M_STORED, M_CM, M_BUNDLE) or t not in TRANSFORM_NAMES:
        raise MixzError("corrupt header (flags)")
    size, p = get_varint(blob, 5)
    if size > MAX_SIZE:
        raise MixzError("corrupt header (size)")
    crc = _u32(blob, p)
    p += 4
    h = Header(method, size, crc, transform=t)
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
    h.profile, h.primer = version_info(h.version)
    if h.profile == P_IMAGE:
        h.stride, p = get_varint(blob, p)
        h.bpp, p = get_varint(blob, p)
        if not 1 <= h.bpp <= 8 or not 1 <= h.stride <= MAX_SIZE:
            raise MixzError("corrupt header (image geometry)")
    h.stream_len = size
    if t != T_NONE:
        h.stream_len, p = get_varint(blob, p)
        if t == T_E8E9 and h.stream_len != size:
            raise MixzError("corrupt header (size)")
    if t in (T_GZIP, T_PNG):
        h.deflate, p = get_varint(blob, p)
        if h.deflate != DEFLATE or h.deflate not in registry()["deflates"]:
            raise MixzError(f"deflate implementation {h.deflate} is not in this code")
    if bool(flags & F_META) != (t in (T_GZIP, T_PNG)):
        raise MixzError("corrupt header (flags)")
    if flags & F_META:
        mlen, p = get_varint(blob, p)
        if p + mlen > len(blob):
            raise MixzError("truncated header")
        h.meta = blob[p: p + mlen]
        p += mlen
    if flags & F_BLOCKS:
        n, p = get_varint(blob, p)
        if n < 2 or n > h.stream_len or 6 * n > len(blob) - p:
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
    """.mxz bytes -> the original. Raises MixzError on any damage; never returns wrong data."""
    try:
        return _decompress(blob)
    except MixzError:
        raise
    except Exception as e:  # corrupt header or meta
        raise MixzError(f"corrupt file ({e.__class__.__name__}: {e})") from None


def _decompress(blob: bytes) -> bytes:
    h = parse_header(blob)
    if h.method == M_BUNDLE:
        data = b"".join(_decompress_bundle(blob))
    elif h.method == M_STORED:
        data = blob[h.payload_at: h.payload_at + h.size]
    else:
        check_module(h.primer)
        payload = blob[h.payload_at:]
        if h.blocks:
            parts, q = [], 0
            for raw_len, comp_len, bcrc in h.blocks:
                part = cm_decode(payload[q: q + comp_len], raw_len, h.profile, h.primer, h.stride, h.bpp, h.maxbits)
                if zlib.crc32(part) != bcrc:
                    raise MixzError("CRC mismatch in a block: data is corrupt")
                parts.append(part)
                q += comp_len
            stream = b"".join(parts)
        else:
            stream = cm_decode(payload, h.stream_len, h.profile, h.primer, h.stride, h.bpp, h.maxbits)
        data = inverse_transform(h.transform, stream, h.meta)
    if len(data) != h.size or zlib.crc32(data) != h.crc:
        raise MixzError("CRC mismatch: data is corrupt")
    return data


# ----------------------------------------------------------------------------------------------
# bundles: several files in one container. Members with the same plan (no transform, the same engine
# profile and primer) are joined into one model stream, a group, so each is predicted from the ones
# before it. Any other member is a complete mixz file inside the bundle.
#   "MXZ" | 2 | flags (method 2) | total size varint | crc32 of the members joined
#   groups varint, then per group: version, profile varint, maxbits (1), payload bytes varint
#   members varint, then per member: size varint, crc32 (4), group varint (1-based; 0: a mixz file
#     of its own, then its length varint)
#   the groups' payloads, then the members' own files, in order
# ----------------------------------------------------------------------------------------------
def compress_bundle(items: list, maxbits: int = DEFAULT_MAXBITS) -> bytes:
    if not 16 <= maxbits <= 24:
        raise MixzError("maxbits must be between 16 and 24")
    groups, key_of = {}, []
    for i, data in enumerate(items):
        plan = make_plan(data) if len(data) >= 16 else None
        key = (plan.profile, plan.primer) if plan and plan.transform == T_NONE and plan.profile != P_IMAGE else None
        if key:
            groups.setdefault(key, []).append(i)
        key_of.append(key)
    gid, table, body = {}, bytearray(), []
    for k in groups:
        payload = cm_encode(b"".join(items[i] for i in groups[k]), k[0], k[1], 0, 1, maxbits)
        if payload is None:  # the model would make the group larger: its members become files of their own
            continue
        gid[k] = len(gid) + 1
        table += (put_varint(version_id(k[0], k[1])) + put_varint(profile_id(Plan(T_NONE, k[0], b"")))
                  + bytes([maxbits]) + put_varint(len(payload)))
        body.append(payload)
    table = put_varint(len(gid)) + table + put_varint(len(items))
    crc = 0
    for i, data in enumerate(items):
        g = gid.get(key_of[i], 0)
        table += put_varint(len(data)) + struct.pack("<I", zlib.crc32(data)) + put_varint(g)
        if not g:
            own = _compress(data, maxbits, True)
            table += put_varint(len(own))
            body.append(own)
        crc = zlib.crc32(data, crc)
    return bytes(_head(M_BUNDLE, T_NONE, 0, sum(map(len, items)), crc) + table) + b"".join(body)


@dataclass
class BundleGroup:
    profile: int
    primer: int
    maxbits: int
    at: int  # payload position in the bundle
    length: int  # payload bytes
    stream_len: int = 0
    members: list = field(default_factory=list)


def parse_bundle(blob: bytes):
    """(header, groups, members): members are (size, crc32, group index or -1, own file position, own
    file length)."""
    h = parse_header(blob)
    if h.method != M_BUNDLE:
        raise MixzError("not a mixz bundle")
    p = h.payload_at
    ng, p = get_varint(blob, p)
    if 4 * ng > len(blob) - p:
        raise MixzError("corrupt header (bundle groups)")
    groups = []
    for _ in range(ng):
        vid, p = get_varint(blob, p)
        if vid == 0:
            if p + VERSION_HASH_BYTES > len(blob):
                raise MixzError("truncated header")
            vid = version_by_hash(blob[p: p + VERSION_HASH_BYTES])
            p += VERSION_HASH_BYTES
        profile, primer = version_info(vid)
        if profile == P_IMAGE:
            raise MixzError("corrupt header (an image group in a bundle)")
        _, p = get_varint(blob, p)  # profile ID: informational
        if p >= len(blob) or not 16 <= blob[p] <= 24:
            raise MixzError("corrupt header (maxbits)")
        maxbits = blob[p]
        length, p = get_varint(blob, p + 1)
        groups.append(BundleGroup(profile, primer, maxbits, 0, length))
    nm, p = get_varint(blob, p)
    if 6 * nm > len(blob) - p:
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
    except Exception as e:
        raise MixzError(f"corrupt file ({e.__class__.__name__}: {e})") from None


def _decompress_bundle(blob: bytes, index: int = None):
    h, groups, members = parse_bundle(blob)
    if index is not None and not 0 <= index < len(members):
        raise MixzError(f"the bundle has no member {index}")
    want = range(len(members)) if index is None else [index]
    out = {}
    for gi in sorted({members[i][2] for i in want if members[i][2] >= 0}):
        gr = groups[gi]
        check_module(gr.primer)
        last = gr.members.index(index) if index is not None else len(gr.members) - 1
        n = sum(members[i][0] for i in gr.members[: last + 1])
        stream = cm_decode(blob[gr.at: gr.at + gr.length], n, gr.profile, gr.primer, 0, 1, gr.maxbits,
                           total=gr.stream_len)
        q = 0
        for i in gr.members[: last + 1]:
            out[i] = stream[q: q + members[i][0]]
            q += members[i][0]
    for i in want:
        if members[i][2] < 0:
            f = blob[members[i][3]: members[i][3] + members[i][4]]
            if parse_header(f).method == M_BUNDLE:
                raise MixzError("corrupt file (a bundle inside a bundle)")
            out[i] = _decompress(f)
    for i in want:
        if len(out[i]) != members[i][0] or zlib.crc32(out[i]) != members[i][1]:
            raise MixzError("CRC mismatch in a bundle member: data is corrupt")
    return out[index] if index is not None else [out[i] for i in want]


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
    return out + (f"  codec={CODEC}  version={h.version}{' (named by hash)' if h.by_hash else ''} "
                  f"[{version_hash(h.version)[:8].hex()}]  profile={prof}  blocks={max(1, len(h.blocks))}")
