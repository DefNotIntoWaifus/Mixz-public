/* Pinned deflate for re-creating gzip and PNG streams bit-exactly.
 *
 * The compressor of zlib 1.3.1 (engine/zlib-1.3.1, unmodified) is compiled into the engine with
 * Z_PREFIX, so its symbols cannot collide with the zlib already loaded into the process. .mxz files
 * then do not depend on the zlib of the machine that decodes them: other versions or
 * implementations (zlib-ng, for example) can produce different deflate bytes from the same input.
 *
 * The call pattern mirrors Python's zlib.compressobj: one deflate(Z_NO_FLUSH) pass per compress()
 * call, then deflate(Z_FINISH) until the end of the stream. Never change this file or the zlib
 * files: stored files rely on this exact output (deflate implementation 1 in registry.json). */
#include <stdlib.h>
#include "zlib.h"

#define EXPORT __attribute__((visibility("default")))

EXPORT void *mz_zs_new(int level, int wbits, int memlevel, int strategy) {
  z_stream *s = (z_stream *)calloc(1, sizeof(z_stream));
  if (!s) return NULL;
  if (deflateInit2(s, level, Z_DEFLATED, wbits, memlevel, strategy) != Z_OK) {
    free(s);
    return NULL;
  }
  return s;
}

/* Feed n bytes (finish = 0) or end the stream (finish = 1, n = 0). Writes what deflate produces to
 * out and returns its length; -1 on a zlib error, -2 if out was too small. */
EXPORT long long mz_zs_run(void *h, const unsigned char *in, size_t n, unsigned char *out, size_t cap,
                           int finish) {
  z_stream *s = (z_stream *)h;
  if (n > 0xffffffffu || cap > 0xffffffffu) return -1;
  s->next_in = (Bytef *)in;
  s->avail_in = (uInt)n;
  s->next_out = out;
  s->avail_out = (uInt)cap;
  for (;;) {
    int r = deflate(s, finish ? Z_FINISH : Z_NO_FLUSH);
    if (r == Z_STREAM_ERROR) return -1;
    if (finish ? r == Z_STREAM_END : s->avail_in == 0) break;
    if (s->avail_out == 0) return -2;
    if (r == Z_BUF_ERROR) return -1;
  }
  return (long long)(cap - s->avail_out);
}

EXPORT void mz_zs_free(void *h) {
  if (h) {
    deflateEnd((z_stream *)h);
    free(h);
  }
}
