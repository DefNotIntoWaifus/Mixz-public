#!/usr/bin/env python3
"""Round-trip and robustness tests for mixz. Run: python3 tools/test_roundtrip.py"""
import gzip
import hashlib
import os
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import mixz  # noqa: E402

mixz.USE_SNAPSHOTS = True

rng = random.Random(1234)
FAILS = []


def check(name, data, block_size=0):
    c = mixz.compress(data, block_size=block_size)
    d = mixz.decompress(c)
    if d != data:
        FAILS.append(name)
        print(f"FAIL {name}")
        return
    h = mixz.parse_header(c)
    kind = "stored" if h.method == mixz.M_STORED else f"{mixz.TRANSFORM_NAMES[h.transform]}/{mixz.PROFILE_NAMES[h.profile]}/{mixz.PRIMER_IDS[h.primer] or '-'}"
    if h.blocks:
        kind += f"  {len(h.blocks)} blocks"
    print(f"ok   {name:38s} {len(data):>9} -> {len(c):>8}  {kind}")
    return h


def report(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + (f": {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def rejected(blob, limit=1.0):
    """A damaged or malicious file must raise MixzError quickly (no large allocation, no long decode)."""
    t = time.perf_counter()
    try:
        mixz.decompress(blob)
        return False, "no error raised"
    except mixz.MixzError as e:
        dt = time.perf_counter() - t
        return dt < limit, f"{e} ({dt * 1000:.0f} ms)"


def v2_head(flags, size, version=0, profile=1, maxbits=20, extra=b""):
    version = version or mixz.version_id(mixz.P_TEXT, 0)  # 0: the current codec's text version
    return (b"MXZ" + bytes([2, flags]) + mixz.put_varint(size) + struct.pack("<I", 0) + mixz.put_varint(version)
            + mixz.put_varint(profile) + bytes([maxbits]) + extra)


def format_tests(text, elf_file):
    # multi-block files: every block is coded from the primed model and checked by its own CRC32
    check("blocks: text, 4 KiB", text, 4096)
    check("blocks: text, one byte over a block", text[:8193], 8192)
    h = check("blocks: x86 ELF", elf_file[:64] + text[:40000] + elf_file[64:20000], 8192)
    report("blocks: x86 ELF keeps the E8/E9 transform", h.transform == mixz.T_E8E9 and len(h.blocks) > 1)
    check("blocks: gzip", gz(text), 8192)
    check("blocks: zeros", bytes(100000), 30000)
    a = mixz.auto_block_size
    report("blocks: automatic size (none below 1 GiB, then >= 512 MiB, at most 64 blocks)",
           a(10 ** 6) == 0 and a((1 << 30) - 1) == 0 and a(3 << 30) == 512 << 20 and a(1 << 40) == (1 << 40) // 64
           and mixz.parse_header(mixz.compress(text)).blocks == [])
    # a file's blocks are coded at the same time on several threads: the output must not depend on how many
    big = (text * 4)[:150000]
    outs = []
    for n in (1, 3):
        mixz.set_block_threads(n)
        outs.append(mixz.compress(big, block_size=40000))
    back = mixz.decompress(outs[0])
    mixz.set_block_threads(0)
    report("blocks: the same output with 1 and 3 block threads, 4 equal blocks", outs[0] == outs[1] and back == big
           and [b[0] for b in mixz.parse_header(outs[0]).blocks] == [37500] * 4)
    # a version can be named by its hash
    for name, data in (("text", text), ("gzip", gz(text)), ("x86 ELF", elf_file[:64] + text[:20000] + elf_file[64:8000])):
        blob = mixz.compress(data, by_hash=True)
        h = mixz.parse_header(blob)
        report(f"version named by hash: {name} round trip", mixz.decompress(blob) == data and h.by_hash
               and h.codec == mixz.CODEC and len(blob) == len(mixz.compress(data)) + mixz.VERSION_HASH_BYTES)
    rgb = open(os.path.join(ROOT, "tests", "vectors", "synthetic_rgb.png"), "rb").read()
    h = check("blocks: png rows", rgb, 4096)
    report("blocks: png blocks hold whole rows", bool(h.blocks) and all(b[0] % h.stride == 0 for b in h.blocks[:-1]),
           f"stride {h.stride}, blocks {[b[0] for b in h.blocks]}")
    # damaged or malicious headers
    v2 = mixz.compress(text, block_size=8192)
    h = mixz.parse_header(v2)
    table = mixz.put_varint(len(h.blocks)) + b"".join(
        mixz.put_varint(r) + mixz.put_varint(c) + struct.pack("<I", crc) for r, c, crc in h.blocks)
    t0 = h.payload_at - len(table)
    assert v2[t0: h.payload_at] == table
    body = v2[h.payload_at:]

    def with_table(entries, n=None):
        n = len(entries) if n is None else n
        return v2[:t0] + mixz.put_varint(n) + b"".join(
            mixz.put_varint(r) + mixz.put_varint(c) + struct.pack("<I", crc) for r, c, crc in entries) + body
    (r0, c0, k0), (r1, c1, k1) = h.blocks[0], h.blocks[1]
    cases = [
        ("reserved flag bit set", v2[:4] + bytes([v2[4] | 0x80]) + v2[5:]),
        ("size over 1 TB", v2_head(1, 1 << 41) + bytes(10)),
        ("claims 1 GB of output from 10 bytes", v2_head(1, 10 ** 9) + bytes(10)),
        ("e8e9 stream length differs from size", v2_head(1 | (1 << 2), 1000, version=mixz.version_id(mixz.P_BIN, 0),
                                                         extra=mixz.put_varint(999)) + bytes(100)),
        ("unknown version", v2_head(1, 100, version=999) + bytes(50)),
        ("version of the dropped pre-release codec 1", v2_head(1, 100, version=1) + bytes(50)),
        ("unknown version hash", v2_head(1, 100)[:-3] + mixz.put_varint(0) + bytes(16) + v2_head(1, 100)[-2:] + bytes(50)),
        ("maxbits 30", v2_head(1, 100, maxbits=30) + bytes(50)),
        ("unknown deflate", v2_head(1 | (2 << 2) | mixz.F_META, 100, extra=mixz.put_varint(100) + mixz.put_varint(7)
                                    + mixz.put_varint(3) + b"abc") + bytes(50)),
        ("meta flag without gzip/png", v2_head(1 | mixz.F_META, 100, extra=mixz.put_varint(3) + b"abc") + bytes(50)),
        ("stored with transform bits", b"MXZ" + bytes([2, 1 << 2]) + mixz.put_varint(5) + struct.pack("<I", 0) + b"abcde"),
        ("stored, truncated", mixz.compress(os.urandom(100))[:-1]),
        ("block table: sizes do not add up", with_table([(r0 + 1, c0, k0)] + h.blocks[1:])),
        ("block table: bytes moved between blocks", with_table([(r0 + 1, c0, k0), (r1 - 1, c1, k1)] + h.blocks[2:])),
        ("block table: one block", with_table(h.blocks[:1], 1)),
        ("block table: a billion blocks", with_table(h.blocks, 10 ** 9)),
        ("format 1 (pre-release)", b"MXZ" + bytes([1, 1]) + mixz.put_varint(100) + struct.pack("<I", 0)
         + bytes([0, 0, 0, 0, 20]) + bytes(10)),
        ("unknown format 3", b"MXZ" + bytes([3]) + v2[4:]),
    ]
    for name, blob in cases:
        ok, detail = rejected(blob)
        report(f"rejected: {name}", ok, detail)
    for label, blob in (("", v2), (" (version named by hash)", mixz.compress(text, block_size=8192, by_hash=True))):
        end = mixz.parse_header(blob).payload_at
        slow = [n for n in range(end) if not rejected(blob[:n])[0]]
        report(f"rejected: truncated at each of the {end} header bytes{label}", not slow, f"not rejected: {slow[:10]}")
    # registry: append-only data the files depend on
    reg = mixz.registry()
    mods = all(mixz.PRIMER_IDS[k] == m["name"] and hashlib.sha256(open(os.path.join(ROOT, m["file"]), "rb").read()).hexdigest()
               == m["sha256"] for k, m in reg["modules"].items())
    report("registry: every mind module file matches its sha256 and primer name", mods)
    for c, e in reg["codecs"].items():
        eng = hashlib.sha256(open(os.path.join(ROOT, e["source"]), "rb").read()).hexdigest()
        report(f"registry: {e['source']} is registered codec {c} (an engine edit needs a new codec ID, or before the "
               "first release, unchanged vectors and an updated sha256)", e["engine_sha256"] == eng)
    refs = all(v["codec"] in reg["codecs"] and all(m in reg["modules"] for m in v["modules"]) for v in reg["versions"].values())
    report("registry: versions refer to registered codecs and modules", refs)
    # every (profile, primer) the plan can produce with the registered primers; a sub-profile also runs
    # without primers (use_primers off)
    shipped = [""] + [n for n in mixz.TEXT_PRIMERS + mixz.BIN_PRIMERS if mixz.PRIMER_IDS.index(n) in reg["modules"]]
    combos = [(mixz.TEXT_SUBPROFILES.get(n, mixz.P_TEXT), mixz.PRIMER_IDS.index(n))
              for n in [""] + mixz.TEXT_PRIMERS if n in shipped] + [
        (p, 0) for p in sorted(set(mixz.TEXT_SUBPROFILES.values()))] + [
        (mixz.P_BIN, mixz.PRIMER_IDS.index(n)) for n in [""] + mixz.BIN_PRIMERS if n in shipped] + [(mixz.P_IMAGE, 0)]
    try:
        missing = [c for c in combos if not mixz.version_id(*c)]
    except mixz.MixzError as e:
        missing = [str(e)]
    report("registry: the current codec has a version for every profile and primer", not missing, f"{missing}")
    report("registry: version hashes are distinct", len(reg["by_hash"]) == len(reg["versions"]))
    reused = {sec: sorted(set(ids) & set(reg[sec])) for sec, ids in reg["retired"].items()}
    report("registry: IDs of retired pre-release entries are not reused", not any(reused.values()), f"{reused}")
    heads = mixz.registry_heads()
    bad = [h["release"] for h in heads if mixz.registry_head(h["counts"]) != h["head"]]
    report(f"registry: all {len(heads)} recorded heads reproduce (no entry was changed or removed)", not bad, f"{bad}")
    report("registry: the last recorded head is the current registry (record a head when adding entries)",
           bool(heads) and heads[-1]["head"] == mixz.registry_head())
    # pinned deflate: same bytes as zlib 1.3.1 at every level mixz uses (only comparable on zlib 1.3.1)
    if zlib.ZLIB_RUNTIME_VERSION == "1.3.1":
        diff = 0
        for data in (text, text * 2 + os.urandom(3000), bytes(70000)):
            for level in range(1, 10):
                for mem, strategy, wbits in ((8, 0, -15), (9, 0, -15), (8, 1, 15), (5, 0, 12)):
                    a = zlib.compressobj(level, zlib.DEFLATED, wbits, mem, strategy)
                    b = mixz.PinnedDeflate(level, mem, strategy, wbits)
                    chunks = [data[i: i + 65536] for i in range(0, len(data), 65536)]
                    diff += (b"".join(map(a.compress, chunks)) + a.flush()) != (b"".join(map(b.compress, chunks)) + b.flush())
        report("pinned deflate == system zlib 1.3.1 (108 cases)", not diff, f"{diff} differ" if diff else "")
    else:
        print(f"skip pinned deflate comparison: system zlib is {zlib.ZLIB_RUNTIME_VERSION}, not 1.3.1")
    try:
        mixz.PinnedDeflate(0, 8, 0, -15)
        report("pinned deflate refuses level 0", False)
    except mixz.MixzError:
        report("pinned deflate refuses level 0", True)


def png(w, h, ctype, depth, interlace=0, level=6, strategy=0, filt=None, idat_split=8192, extra_chunks=()):
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    rowbytes = (w * ch * depth + 7) // 8
    rows = bytearray()
    for y in range(h):
        f = filt if filt is not None else rng.randrange(5)
        rows.append(f)
        base = (y * 7) & 255
        rows += bytes(((x * 3 + base + rng.randrange(4)) & 255) for x in range(rowbytes))
    c = zlib.compressobj(level, zlib.DEFLATED, 15, 8, strategy)
    z = c.compress(bytes(rows)) + c.flush()
    out = bytearray(b"\x89PNG\r\n\x1a\n")

    def chunk(t, b):
        out.extend(struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b)))
    chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, interlace))
    if ctype == 3:
        chunk(b"PLTE", bytes(rng.randrange(256) for _ in range(3 * 16)))
    for t, b in extra_chunks:
        chunk(t, b)
    for i in range(0, len(z), idat_split):
        chunk(b"IDAT", z[i: i + idat_split])
    chunk(b"IEND", b"")
    return bytes(out)


def gz(data, level=9, name=None, comment=None, extra=None, hcrc=False):
    flg = (4 if extra else 0) | (8 if name else 0) | (16 if comment else 0) | (2 if hcrc else 0)
    head = bytearray(b"\x1f\x8b\x08" + bytes([flg]) + b"\0\0\0\0" + b"\0\x03")
    if extra:
        head += struct.pack("<H", len(extra)) + extra
    if name:
        head += name + b"\0"
    if comment:
        head += comment + b"\0"
    if hcrc:
        head += struct.pack("<H", zlib.crc32(bytes(head)) & 0xFFFF)
    c = zlib.compressobj(level, zlib.DEFLATED, -15)
    return bytes(head) + c.compress(data) + c.flush() + struct.pack("<II", zlib.crc32(data), len(data))


def service_tests(text, elf_file):
    # snapshot workers: each thread's model is restored from the snapshot between files, by the
    # write log (small files after a file of the same snapshot) or by a full copy; output must equal
    # the replay path in any order, size and thread count
    lens = [0, 1, 300, 3000, 12000, 26000, 60000]
    src = text * 3
    files = [src[rng.randrange(len(src) - 60000):][:rng.choice(lens)] for _ in range(24)]
    files += [src[i * 997:][:200 + 523 * i] for i in range(12)]  # a run of small files, one primer
    files += [elf_file[:20000], elf_file, os.urandom(3000), bytes(700000)]
    rng.shuffle(files)
    mixz.USE_SNAPSHOTS = False
    ref = [mixz.compress(f) for f in files]
    mixz.USE_SNAPSHOTS = True
    for threads in (1, 3):
        with ThreadPoolExecutor(threads) as ex:
            got = list(ex.map(mixz.compress, files))
            back = list(ex.map(mixz.decompress, got))
        report(f"snapshot workers, {threads} thread(s): {len(files)} files equal the replay path and round-trip",
               got == ref and back == files, f"{sum(a != b for a, b in zip(got, ref))} differ")
    # service mode: the CLI sends c/d/t to 'mixz serve' and gets the same bytes as in-process
    tmp = tempfile.mkdtemp(prefix="mixz-test-")
    sock = os.path.join(tmp, "s.sock")
    env = dict(os.environ, MIXZ_SOCKET=sock)
    srv = subprocess.Popen([sys.executable, mixz.__file__, "serve", "-j", "2", "--models", "2"], env=env,
                           stderr=subprocess.PIPE)
    try:
        for _ in range(300):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        names = []
        for i, d in enumerate([text[:20000], gz(text[:30000]), elf_file[:30000], b"", os.urandom(500)]):
            names.append(os.path.join(tmp, f"f{i}"))
            with open(names[-1], "wb") as f:
                f.write(d)

        def cli(*a, env=env):
            return subprocess.run([sys.executable, mixz.__file__, *a], env=env, capture_output=True, text=True)
        r = cli("c", "-f", *names)
        outs = [open(n + ".mxz", "rb").read() for n in names]
        report("service: CLI compress through the server equals in-process compress", r.returncode == 0 and outs == [
            mixz.compress(open(n, "rb").read()) for n in names], r.stderr.strip())
        for n in names:
            os.rename(n, n + ".orig")
        r = cli("d", *[n + ".mxz" for n in names])
        report("service: CLI decompress through the server", r.returncode == 0 and all(
            open(n, "rb").read() == open(n + ".orig", "rb").read() for n in names), r.stderr.strip())
        bad = os.path.join(tmp, "bad.mxz")
        with open(bad, "wb") as f:
            f.write(outs[0][:len(outs[0]) // 2] + bytes([outs[0][len(outs[0]) // 2] ^ 1]) + outs[0][len(outs[0]) // 2 + 1:])
        r = cli("t", names[0] + ".mxz", bad)
        report("service: CLI test through the server reports the damaged file", r.returncode == 1
               and "OK" in r.stdout and "CRC" in r.stderr, (r.stdout + r.stderr).strip())
        lib2 = os.path.join(tmp, "copy.so")
        shutil.copy(os.environ.get("MIXZ_LIB") or mixz.LIB, lib2)  # the library in use
        r = cli("c", "-f", names[0] + ".orig", env=dict(env, MIXZ_LIB=lib2))
        report("service: a client with other files than the server works without it, with a warning",
               r.returncode == 0 and "restart" in r.stderr and open(names[0] + ".orig.mxz", "rb").read() == outs[0],
               r.stderr.strip())
        r = cli("c", "-f", "--no-server", names[0] + ".orig")
        report("service: --no-server", r.returncode == 0 and open(names[0] + ".orig.mxz", "rb").read() == outs[0])
        r = subprocess.run([sys.executable, mixz.__file__, "serve"], env=env, capture_output=True, text=True)
        report("service: a second server on the same socket refuses to start",
               r.returncode == 1 and "already running" in r.stderr, r.stderr.strip())
    finally:
        srv.send_signal(signal.SIGTERM)
        try:
            srv.wait(10)
        except subprocess.TimeoutExpired:
            srv.kill()
    report("service: the server stops on SIGTERM and removes its socket", srv.returncode == 0 and not os.path.exists(sock),
           f"exit {srv.returncode}")
    shutil.rmtree(tmp, ignore_errors=True)


def bundle_tests(text, elf_file):
    pieces = [text[i: i + 3000] for i in range(0, min(len(text), 60000), 3000)]
    items = pieces + [b"", b"x", b"0123456789abcdef", os.urandom(5000), text.replace(b"\n", b"\r\n")[:20000],
                      gz(text), png(32, 32, 2, 8), elf_file[:30000], b'{"a": [1, 2, 3], "b": "text"}\n' * 300]
    b = mixz.compress_bundle(items)
    _, groups, members = mixz.parse_bundle(b)
    singles = sum(len(mixz.compress(x)) for x in items)
    report("bundle: round trip", mixz.decompress_bundle(b) == items,
           f"{len(items)} members, {len(groups)} groups, {sum(m[2] < 0 for m in members)} own files, "
           f"{sum(map(len, items))} -> {len(b)} (one file each: {singles})")
    report("bundle: every member alone (prefix decode)", all(mixz.decompress_bundle(b, i) == x for i, x in enumerate(items)))
    report("bundle: decompress gives the members joined", mixz.decompress(b) == b"".join(items))
    report("bundle: describe", mixz.describe(b).startswith("bundle"), mixz.describe(b))
    mixz.set_block_threads(1)
    one = mixz.compress_bundle(items)
    mixz.set_block_threads(0)
    report("bundle: output independent of threads and repeatable", one == b == mixz.compress_bundle(items))
    e = mixz.compress_bundle([])
    report("bundle: empty", mixz.decompress_bundle(e) == [] and mixz.decompress(e) == b"")
    small = mixz.compress_bundle(pieces)
    report("bundle: same-kind members share one group", len(mixz.parse_bundle(small)[1]) == 1
           and len(small) < sum(len(mixz.compress(x)) for x in pieces), f"{len(small)} bytes")
    try:
        mixz.decompress_bundle(b, len(items))
        report("bundle: index out of range rejected", False)
    except mixz.MixzError:
        report("bundle: index out of range rejected", True)
    # a bundle inside a bundle, and damaged bundles: MixzError, never wrong data
    inner = mixz.compress_bundle([b"abc" * 10])
    nested = (bytes(mixz._head(mixz.M_BUNDLE, 0, 0, 30, zlib.crc32(b"abc" * 10))) + mixz.put_varint(0) + mixz.put_varint(1)
              + mixz.put_varint(30) + struct.pack("<I", zlib.crc32(b"abc" * 10)) + mixz.put_varint(0)
              + mixz.put_varint(len(inner)) + inner)
    ok, detail = rejected(nested)
    report("bundle: nested bundle rejected", ok, detail)
    bad = []
    for i in range(300):
        q = rng.randrange(len(small))
        damaged = small[:q] if i % 3 == 0 else small[:q] + bytes([small[q] ^ (1 << rng.randrange(8))]) + small[q + 1:]
        try:
            if mixz.decompress_bundle(damaged) != pieces:
                bad.append(q)
        except mixz.MixzError:
            pass
    report("bundle: 300 truncations and bit flips rejected or harmless", not bad, f"wrong data at {bad[:5]}" if bad else "")


def main():
    text = open(mixz.__file__, "rb").read()
    check("empty", b"")
    check("1 byte", b"x")
    check("15 bytes", b"abcdefghijklmno")
    check("16 bytes", b"abcdefghijklmnop")
    check("zeros 1MB", bytes(1 << 20))
    check("0xFF 300KB", b"\xff" * 300000)
    check("alternating", b"\x00\xff" * 100000)
    check("random 200KB", os.urandom(200000))
    check("source code", text)
    check("source CRLF", text.replace(b"\n", b"\r\n"))
    check("text + binary tail", text + os.urandom(5000))
    check("utf-8 text", ("ñandú café 日本語 " * 4000).encode())
    elf = b"\x7fELF\x02\x01\x01" + bytes(9) + b"\x02\x00\x3e\x00" + bytes(44)
    body = bytearray(os.urandom(50000))
    for i in range(0, len(body) - 5, 37):
        body[i] = 0xE8
        body[i + 4] = rng.choice([0, 0xFF])
    elf_file = elf + bytes(body) + b"\xe8\x01\x02\x03"
    check("x86 ELF with calls", elf_file)
    check("x86 ELF ending in E8", elf + b"\x90" * 100 + b"\xe8\x00")
    # gzip
    for lvl in (1, 6, 9):
        check(f"gzip level {lvl}", gz(text, lvl))
    check("gzip name+comment+extra+hcrc", gz(text, 9, b"file.py", b"hello", b"AB\x02\x00xy", True))
    check("gzip + trailing garbage", gz(text) + b"GARBAGE")
    check("gzip multi-member", gz(text[:5000]) + gz(text[5000:]))
    check("gzip via python module", gzip.compress(text, 9, mtime=0))
    check("gzip corrupt deflate", gz(text)[:200] + b"\x00" * 50 + gz(text)[250:])
    check("gzip truncated", gz(text)[:-20])
    # png
    check("png rgb8", png(64, 48, 2, 8))
    check("png rgba8", png(40, 40, 6, 8, level=9))
    check("png gray16", png(30, 20, 0, 16))
    check("png palette4", png(50, 30, 3, 4))
    check("png gray1", png(77, 33, 0, 1))
    check("png filtered strategy", png(64, 64, 2, 8, strategy=1))
    check("png interlaced flag", png(16, 16, 2, 8, interlace=1))
    check("png small IDAT split", png(64, 48, 2, 8, idat_split=100))
    check("png with text chunk", png(32, 32, 2, 8, extra_chunks=[(b"tEXt", b"Comment\0hi")]))
    p = bytearray(png(64, 48, 2, 8))
    p[40] ^= 0xFF
    check("png bad crc", bytes(p))
    check("png + trailing bytes", png(32, 32, 2, 8) + b"trailer")
    # fuzz
    for i in range(30):
        n = rng.choice([1, 17, 100, 1000, 5000, 20000])
        kind = rng.randrange(4)
        if kind == 0:
            d = os.urandom(n)
        elif kind == 1:
            d = bytes(rng.choice(b"ab\n ") for _ in range(n))
        elif kind == 2:
            d = text[rng.randrange(len(text) // 2):][:n]
        else:
            d = bytes((i * rng.randrange(1, 9)) & 255 for i in range(n))
        check(f"fuzz {i} kind{kind}", d)
    # x86 transform must be exactly invertible on adversarial input (dense, overlapping
    # E8/E9 opcodes and 00/FF bytes); this caught a real bug in an earlier version
    bad = 0
    for i in range(3000):
        n = rng.randrange(1, 200)
        buf = bytes(rng.choice([0xE8, 0xE9, 0x00, 0xFF, rng.randrange(256)]) for _ in range(n))
        if mixz.e8e9(mixz.e8e9(buf, False), True) != buf:
            bad += 1
    print(("ok  " if not bad else "FAIL") + f" e8e9 inverse on 3000 adversarial buffers ({bad} bad)")
    if bad:
        FAILS.append("e8e9 adversarial")
    # snapshot path must give byte-identical output to the replay path
    pid = next((i for i in range(1, len(mixz.PRIMER_IDS)) if mixz.primer_bytes(i)), 0)
    for codec in (mixz.CODECS if pid else ()):
        a = mixz.cm_encode(text, mixz.P_TEXT, pid, 0, 1, 20, codec)
        primer = mixz.primer_bytes(pid)
        import ctypes
        cap = len(text) * 2 + 4096
        out = ctypes.create_string_buffer(cap)
        r = mixz.codec_fn(codec, "compress")(text, len(text), primer, len(primer), mixz.P_TEXT, 0, 1, 20, out, cap)
        same = out.raw[:r] == a
        print(("ok  " if same else "FAIL") + f" codec {codec}: snapshot output == replay output")
        if not same:
            FAILS.append(f"snapshot codec {codec}")
    # corrupted containers must raise MixzError, never crash or return wrong data
    c = mixz.compress(text)
    for name, bad in (("truncated container", c[: len(c) // 2]), ("flipped payload byte", c[:-10] + bytes([c[-10] ^ 1]) + c[-9:]),
                      ("bad magic", b"XXX" + c[3:])):
        try:
            mixz.decompress(bad)
            FAILS.append(name)
            print(f"FAIL {name}: no error raised")
        except mixz.MixzError as e:
            print(f"ok   {name}: rejected ({e})")
    format_tests(text, elf_file)
    bundle_tests(text, elf_file)
    service_tests(text, elf_file)
    print()
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
