/* How much of a primed model one file changes, and what restoring it costs.
   Copy-on-write snapshots (map the snapshot privately, copy a page when it is first written) only pay
   off if a file writes to few pages. This primes the model, codes FILE with a worker, and counts the
   distinct 4 KB pages and 64-byte lines its write log touched in each large table; then it times the
   two restores the engine has: from the log, and the full copy.
   Build: cc -O3 -march=native -o /tmp/rp restore_pages.c && /tmp/rp PRIMER FILE [PROFILE]
   The log holds 1M entries (~25 KB of input); a larger FILE is cut to fit. */
#include "../../engine/cm.c"
#include <stdio.h>
#include <time.h>

static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }

static U8 *slurp(const char *p, size_t *n) {
  FILE *f = fopen(p, "rb");
  if (!f) { perror(p); exit(1); }
  fseek(f, 0, SEEK_END); *n = (size_t)ftell(f); fseek(f, 0, SEEK_SET);
  U8 *b = (U8 *)malloc(*n ? *n : 1);
  if (fread(b, 1, *n, f) != *n) exit(1);
  fclose(f);
  return b;
}

static size_t distinct(const U32 *log, const U32 *end, U32 kind, int ctx, int shift, size_t nbits) {
  U8 *seen = (U8 *)calloc((nbits >> 3) + 1, 1);
  size_t c = 0;
  for (const U32 *e = log; e < end; e++) {
    U32 v = *e;
    if ((v >> 29) != kind || (ctx >= 0 && (int)((v >> 24) & 7) != ctx)) continue;
    size_t u = (v & 0xffffff) >> shift;
    if (!(seen[u >> 3] & (1 << (u & 7)))) { seen[u >> 3] |= (U8)(1 << (u & 7)); c++; }
  }
  free(seen);
  return c;
}

int main(int argc, char **argv) {
  if (argc < 3) { fprintf(stderr, "usage: %s PRIMER FILE [PROFILE]\n", argv[0]); return 1; }
  size_t pn, n;
  U8 *primer = slurp(argv[1], &pn), *in = slurp(argv[2], &n);
  int profile = argc > 3 ? atoi(argv[3]) : 0;
  Snapshot *s = (Snapshot *)cm_prime(primer, pn, profile, 0, 1, 20, pn + n);
  Worker *w = (Worker *)cm_worker_new();
  size_t cap = n + n / 8 + 4096;
  U8 *out = (U8 *)malloc(cap);
  for (;;) { /* shrink the input until its log does not overflow */
    cm_compress_primed(s, w, in, n, pn, out, cap); /* first call: full copy, then logged */
    cm_compress_primed(s, w, in, n, pn, out, cap); /* second: restored from the log of the first */
    if (w->m.lp) break;
    n = n * 3 / 4;
  }
  const Model *m = &w->m;
  const U32 *log = w->log, *end = m->lp;
  size_t T = (size_t)1 << m->tbits, R = T >> 1, H = (size_t)1 << m->htbits;
  printf("input %zu bytes, %zu log entries (%.1f per byte)\n", n, (size_t)(end - log), (double)(end - log) / n);
  size_t pages_t = 0, lines_t = 0, buckets_t = 0, pages_r = 0, lines_r = 0;
  for (int i = 0; i < NC; i++) {
    buckets_t += distinct(log, end, LOG_T, i, 0, T);
    lines_t += distinct(log, end, LOG_T, i, 2, T >> 2);     /* 4 buckets of 16 B per 64 B line */
    pages_t += distinct(log, end, LOG_T, i, 8, T >> 8);     /* 256 buckets per 4 KB page */
    lines_r += distinct(log, end, LOG_R, i, 4, R >> 4);
    pages_r += distinct(log, end, LOG_R, i, 10, R >> 10);
  }
  printf("context tables: %zu of %zu buckets (%.1f%%), %.1f%% of 64 B lines, %.1f%% of 4 KB pages\n",
         buckets_t, T * NC, 100.0 * buckets_t / (T * NC), 100.0 * lines_t / ((T >> 2) * NC), 100.0 * pages_t / ((T >> 8) * NC));
  printf("run tables: %.1f%% of 64 B lines, %.1f%% of 4 KB pages\n", 100.0 * lines_r / ((R >> 4) * NC), 100.0 * pages_r / ((R >> 10) * NC));
  printf("match table: %.1f%% of 4 KB pages; APM: %.1f%% / %.1f%% of 4 KB pages\n",
         100.0 * distinct(log, end, LOG_HT, -1, 10, H >> 10) / (H >> 10),
         100.0 * distinct(log, end, LOG_A1, -1, 11, 65536 * 33 >> 11) / ((65536 * 33 >> 11) + 1),
         100.0 * distinct(log, end, LOG_A2, -1, 11, 65536 * 33 >> 11) / ((65536 * 33 >> 11) + 1));
  /* restore timings: from the log (the file just coded), and the full copy */
  double t0 = now(), tlog = 0, tfull = 0;
  for (int r = 0; r < 5; r++) {
    cm_compress_primed(s, w, in, n, pn, out, cap); /* leaves a log */
    t0 = now(); worker_load(w, s); tlog += now() - t0;
    w->src = 0;
    t0 = now(); worker_load(w, s); tfull += now() - t0;
  }
  double tc = now();
  for (int r = 0; r < 5; r++) cm_compress_primed(s, w, in, n, pn, out, cap);
  tc = (now() - tc) / 5;
  printf("per file: coding (with log restore) %.1f ms; restore from log %.1f ms; full copy %.1f ms (model %.0f MB)\n",
         tc * 1e3, tlog / 5 * 1e3, tfull / 5 * 1e3,
         (double)(T * 16 * NC + R * 4 * NC + H * 4 + BSZ(m) + 2 * ASZ) / 1e6);
  return 0;
}
