import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
HERE = os.path.dirname(os.path.abspath(__file__))
import ctypes, random
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, 'tools'))
import corpora, mixz
lib = ctypes.CDLL(os.path.join(HERE, 'liblzcov.so'))
lib.cover.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_int, ctypes.POINTER(ctypes.c_uint64)]
def run(kind, primer_name=None, minlen=16):
    train, test = corpora.split(kind)
    rng = random.Random(42); rng.shuffle(test)
    primer = mixz.primer_bytes(mixz.PRIMER_IDS.index(primer_name)) if primer_name else b''
    cov = tot = tok = 0
    for f in test[:60]:
        d = corpora.content(kind, f)[:2_000_000]
        if len(d) < 200: continue
        buf = primer + d; out = (ctypes.c_uint64 * 3)()
        lib.cover(buf, len(buf), len(primer), minlen, out)
        cov += out[0]; tok += out[1]; tot += len(d)
    return cov / tot, tok / tot * 1024
for kind in ['py', 'c', 'js', 'json', 'web', 'prose', 'elf']:
    row = []
    for ml in (8, 16, 32):
        c, t = run(kind, None, ml); row.append(f"≥{ml}B: {c:5.1%}")
    cp, tp = run(kind, kind, 16)
    print(f"{kind:6s} own-file repeats " + "  ".join(row) + f"   | with primer ≥16B: {cp:5.1%}  ({tp:.0f} copy instructions per KB)")
