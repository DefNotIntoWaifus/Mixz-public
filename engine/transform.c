/*
 * mixz transforms: the E8/E9 x86 branch transform and PNG scanline filters, done before coding
 * (and undone after decoding) by mixz.py. They are part of the file format, like the codec:
 * changing what they compute makes existing files decode wrong (the CRC check rejects them).
 */
#include <stdint.h>
#include <stdlib.h>

typedef uint8_t U8;
typedef uint32_t U32;

#define EXPORT __attribute__((visibility("default")))

/* x86 call/jmp transform: rel32 -> absolute (low 24 bits), reversible.
 * Every E8/E9 opcode consumes its 4 operand bytes whether or not it is
 * transformed. So positions visited, and the bytes each decision reads
 * (b[i], b[i+4]), are never modified by any transform, and the inverse
 * makes exactly the same decisions as the forward pass. */
EXPORT void mz_e8e9(U8 *b, size_t n, int inverse) {
  size_t i = 0;
  while (i + 5 <= n) {
    if (b[i] == 0xE8 || b[i] == 0xE9) {
      if (b[i + 4] == 0x00 || b[i + 4] == 0xFF) {
        U32 v = (U32)b[i + 1] | ((U32)b[i + 2] << 8) | ((U32)b[i + 3] << 16);
        U32 off = (U32)(i + 5);
        v = inverse ? (v - off) : (v + off);
        v &= 0xFFFFFF;
        b[i + 1] = (U8)v; b[i + 2] = (U8)(v >> 8); b[i + 3] = (U8)(v >> 16);
      }
      i += 5;
    } else i++;
  }
}

/* PNG scanline filters. filtered rows = [type][rowbytes]; pixels = rowbytes per row */
static inline int paeth(int a, int b, int c) {
  int p = a + b - c;
  int pa = abs(p - a), pb = abs(p - b), pc = abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}
EXPORT int mz_png_unfilter(const U8 *in, size_t h, size_t rowbytes, int bpp, U8 *pix, U8 *types) {
  const U8 *src = in;
  for (size_t y = 0; y < h; y++) {
    int t = *src++;
    if (t > 4) return -1;
    types[y] = (U8)t;
    U8 *row = pix + y * rowbytes;
    const U8 *prev = y ? pix + (y - 1) * rowbytes : NULL;
    for (size_t x = 0; x < rowbytes; x++) {
      int a = x >= (size_t)bpp ? row[x - bpp] : 0;
      int b = prev ? prev[x] : 0;
      int c = (prev && x >= (size_t)bpp) ? prev[x - bpp] : 0;
      int v = src[x];
      switch (t) {
        case 1: v += a; break;
        case 2: v += b; break;
        case 3: v += (a + b) >> 1; break;
        case 4: v += paeth(a, b, c); break;
      }
      row[x] = (U8)v;
    }
    src += rowbytes;
  }
  return 0;
}
EXPORT int mz_png_refilter(const U8 *pix, const U8 *types, size_t h, size_t rowbytes, int bpp, U8 *out) {
  U8 *dst = out;
  for (size_t y = 0; y < h; y++) {
    int t = types[y];
    *dst++ = (U8)t;
    const U8 *row = pix + y * rowbytes;
    const U8 *prev = y ? pix + (y - 1) * rowbytes : NULL;
    for (size_t x = 0; x < rowbytes; x++) {
      int a = x >= (size_t)bpp ? row[x - bpp] : 0;
      int b = prev ? prev[x] : 0;
      int c = (prev && x >= (size_t)bpp) ? prev[x - bpp] : 0;
      int v = row[x];
      switch (t) {
        case 1: v -= a; break;
        case 2: v -= b; break;
        case 3: v -= (a + b) >> 1; break;
        case 4: v -= paeth(a, b, c); break;
      }
      dst[x] = (U8)v;
    }
    dst += rowbytes;
  }
  return 0;
}
