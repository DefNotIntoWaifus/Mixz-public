import zlib, glob, random, collections, struct
random.seed(1)
import os
fs = sorted(f for f in glob.glob('/usr/share/**/*.gz', recursive=True) if os.path.isfile(f)); random.shuffle(fs)
def parse_gzip(d):
    if d[:2] != b'\x1f\x8b' or d[2] != 8: return None
    flg = d[3]; p = 10
    if flg & 4: xlen = struct.unpack('<H', d[p:p+2])[0]; p += 2 + xlen
    if flg & 8: p = d.index(b'\0', p) + 1
    if flg & 16: p = d.index(b'\0', p) + 1
    if flg & 2: p += 2
    return p
def try_repro(raw, comp):
    for lvl in (9, 6, 1, 2, 3, 4, 5, 7, 8):
        for ml in (8, 9):
            c = zlib.compressobj(lvl, zlib.DEFLATED, -15, ml)
            out = c.compress(raw) + c.flush()
            if out == comp: return (lvl, ml)
    return None
stats = collections.Counter(); n=0
for f in fs[:300]:
    d = open(f,'rb').read(); p = parse_gzip(d)
    if p is None: stats['notgz']+=1; continue
    do = zlib.decompressobj(-15); raw = do.decompress(d[p:]); 
    clen = len(d) - p - len(do.unused_data)
    r = try_repro(raw, d[p:p+clen])
    stats[str(r)] += 1
print(stats)
