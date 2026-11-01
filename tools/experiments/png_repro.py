import zlib, glob, random, collections, struct, os
random.seed(1)
fs = sorted(f for f in glob.glob('/usr/**/*.png', recursive=True) if os.path.isfile(f)); random.shuffle(fs)
def chunks(d):
    p = 8; out = []
    while p + 8 <= len(d):
        ln, = struct.unpack('>I', d[p:p+4]); t = d[p+4:p+8]
        out.append((t, d[p+8:p+8+ln])); p += 12 + ln
        if t == b'IEND': break
    return out, p
def try_repro(raw, z):
    cmf, flg = z[0], z[1]
    wbits = (cmf >> 4) + 8
    flevel = flg >> 6
    lv_order = {0:[1], 1:[2,3,4,5], 2:[6], 3:[7,8,9]}[flevel]
    lv_order += [l for l in range(1,10) if l not in lv_order]
    for strat in (0, 1, 2, 3):
        for lvl in lv_order:
            for ml in (8, 9, 7, 6, 5, 4, 3, 2, 1):
                c = zlib.compressobj(lvl, zlib.DEFLATED, wbits, ml, strat)
                # incremental early-abort
                out = b''; ok = True
                for i in range(0, len(raw), 1 << 16):
                    out += c.compress(raw[i:i+(1<<16)])
                    if out != z[:len(out)]: ok = False; break
                if not ok: continue
                out += c.flush()
                if out == z: return (lvl, ml, strat, wbits)
    return None
stats = collections.Counter(); bad = collections.Counter()
for f in fs[:250]:
    d = open(f,'rb').read()
    if d[:8] != b'\x89PNG\r\n\x1a\n': stats['notpng'] += 1; continue
    ch, end = chunks(d)
    ihdr = dict(ch).get(b'IHDR')
    w,h,depth,ctype,comp,filt,inter = struct.unpack('>IIBBBBB', ihdr)
    if inter: stats['interlaced'] += 1; continue
    z = b''.join(c for t,c in ch if t == b'IDAT')
    try: raw = zlib.decompress(z)
    except Exception: stats['badz'] += 1; continue
    r = try_repro(raw, z)
    stats['ok' if r else 'fail'] += 1
    if r: bad[r] += 1
print(stats); print(bad.most_common(10))
