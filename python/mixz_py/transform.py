"""Reversible transforms done before coding: a Python port of engine/transform.c.

- E8/E9: x86 call (opcode E8) and jump (E9) instructions store their target as a distance from the
  instruction. Calls to the same function then look different at every place. The transform adds the
  position to the distance, so the same target gives the same bytes, which the model predicts well.
- PNG filters: each PNG row is stored as the difference to a prediction (the pixel to the left, the
  one above, ...). Undoing the filters gives the model the real pixels and the row above them.

Both are part of the file format: changing what they compute would make existing files decode wrong
(the CRC check would then reject them).
"""
import re

_OPCODE = re.compile(b"[\xe8\xe9]")


def e8e9(data: bytes, inverse: bool) -> bytes:
    """Every E8/E9 opcode consumes its 4 operand bytes whether or not it is transformed. The bytes each
    decision reads (the opcode and the operand's top byte) are never changed, so the inverse makes
    exactly the same decisions as the forward pass."""
    b = bytearray(data)
    n = len(b)
    i = 0
    while i + 5 <= n:
        m = _OPCODE.search(b, i, n - 4)  # the next E8/E9 byte that has 4 bytes after it
        if m is None:
            break
        i = m.start()
        if b[i + 4] in (0x00, 0xFF):  # the target is near: the operand's top byte is 0 or -1
            v = b[i + 1] | (b[i + 2] << 8) | (b[i + 3] << 16)
            off = i + 5  # the position after the instruction
            v = ((v - off) if inverse else (v + off)) & 0xFFFFFF
            b[i + 1], b[i + 2], b[i + 3] = v & 255, (v >> 8) & 255, v >> 16
        i += 5
    return bytes(b)


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def png_unfilter(raw: bytes, h: int, rowbytes: int, bpp: int):
    """Filtered rows ([filter type][rowbytes bytes] per row) -> (pixels, filter types), or None if a
    filter type is unknown. a = left, b = above, c = above-left; bytes are modulo 256."""
    pix = bytearray(h * rowbytes)
    types = bytearray(h)
    src = 0
    for y in range(h):
        t = raw[src]
        src += 1
        if t > 4:
            return None
        types[y] = t
        row = y * rowbytes
        prev = row - rowbytes  # only used when y > 0
        for x in range(rowbytes):
            a = pix[row + x - bpp] if x >= bpp else 0
            b = pix[prev + x] if y else 0
            v = raw[src + x]
            if t == 1:
                v += a
            elif t == 2:
                v += b
            elif t == 3:
                v += (a + b) >> 1
            elif t == 4:
                v += _paeth(a, b, pix[prev + x - bpp] if y and x >= bpp else 0)
            pix[row + x] = v & 255
        src += rowbytes
    return bytes(pix), bytes(types)


def png_refilter(pix: bytes, types: bytes, h: int, rowbytes: int, bpp: int) -> bytes:
    """The inverse of png_unfilter: pixels and filter types -> filtered rows."""
    out = bytearray(h * (rowbytes + 1))
    dst = 0
    for y in range(h):
        t = types[y]
        out[dst] = t
        dst += 1
        row = y * rowbytes
        prev = row - rowbytes
        for x in range(rowbytes):
            a = pix[row + x - bpp] if x >= bpp else 0
            b = pix[prev + x] if y else 0
            v = pix[row + x]
            if t == 1:
                v -= a
            elif t == 2:
                v -= b
            elif t == 3:
                v -= (a + b) >> 1
            elif t == 4:
                v -= _paeth(a, b, pix[prev + x - bpp] if y and x >= bpp else 0)
            out[dst + x] = v & 255
        dst += rowbytes
    return bytes(out)
