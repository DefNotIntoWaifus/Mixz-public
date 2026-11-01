/* Greedy LZ parse: how many bytes of a file fall inside repeats of >= minlen bytes.
   Repeats may point into an optional prefix (a primer), like an LZ dictionary. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define HB 20
#define DEPTH 32
void cover(const uint8_t *b, size_t n, size_t start, int minlen, uint64_t *out) {
  int32_t *head = malloc(sizeof(int32_t) << HB), *prev = malloc(sizeof(int32_t) * (n + 1));
  memset(head, 0xff, sizeof(int32_t) << HB);
  uint64_t covered = 0, tokens = 0, literals = 0;
  #define H(p) ((((uint32_t)b[p] | b[p+1] << 8 | b[p+2] << 16 | (uint32_t)b[p+3] << 24) * 2654435761u) >> (32 - HB))
  size_t i = 0;
  while (i + 4 <= n) {
    size_t best = 0;
    if (i >= start) {
      int32_t c = head[H(i)];
      for (int d = 0; d < DEPTH && c >= 0; d++, c = prev[c]) {
        size_t l = 0;
        while (i + l < n && b[c + l] == b[i + l] && l < 65535) l++;
        if (l > best) best = l;
      }
    }
    size_t step = (i >= start && best >= (size_t)minlen) ? best : 1;
    if (i >= start) { if (step > 1) { covered += step; tokens++; } else literals++; }
    for (size_t k = 0; k < step && i + 4 <= n; k++, i++) { uint32_t h = H(i); prev[i] = head[h]; head[h] = (int32_t)i; }
  }
  if (n > start) literals += (n - start) - covered - literals > 0 ? 0 : 0;
  out[0] = covered; out[1] = tokens; out[2] = literals;
  free(head); free(prev);
}
