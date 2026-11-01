/* Engine-only speed: compress and decompress FILE with no primer on one thread, REPEATS times.
   speed FILE PROFILE STRIDE BPP MAXBITS REPEATS   (profile 0=text 1=binary 2=image)
   Prints one JSON line per repeat, with wall-clock and CPU time (CPU time leaves out the time
   other programs held this core, which on a desktop is most of the noise).
   Exit status 1 if any round-trip differs. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
long long mz_compress(const unsigned char *, size_t, const unsigned char *, size_t, int, int, int, int,
                      unsigned char *, size_t);
int mz_decompress(const unsigned char *, size_t, const unsigned char *, size_t, int, int, int, int,
                  unsigned char *, size_t);
static double now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static double cpu(void) { struct timespec t; clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &t); return t.tv_sec + t.tv_nsec / 1e9; }
int main(int argc, char **argv) {
  if (argc != 7) { fprintf(stderr, "usage: speed FILE PROFILE STRIDE BPP MAXBITS REPEATS\n"); return 2; }
  FILE *f = fopen(argv[1], "rb");
  if (!f) { perror(argv[1]); return 2; }
  fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
  unsigned char *d = malloc(n + 1), *r = malloc(n + 1);
  size_t cap = (size_t)n * 2 + 1024;
  unsigned char *o = malloc(cap);
  if (!d || !r || !o || fread(d, 1, n, f) != (size_t)n) return 2;
  fclose(f);
  int prof = atoi(argv[2]), stride = atoi(argv[3]), bpp = atoi(argv[4]), mb = atoi(argv[5]), reps = atoi(argv[6]);
  int bad = 0;
  for (int k = 0; k < reps; k++) {
    double t0 = now(), u0 = cpu();
    long long cs = mz_compress(d, n, 0, 0, prof, stride, bpp, mb, o, cap);
    double t1 = now(), u1 = cpu();
    int rc = cs < 0 ? -1 : mz_decompress(o, cs, 0, 0, prof, stride, bpp, mb, r, n);
    double t2 = now(), u2 = cpu();
    int ok = rc == 0 && memcmp(d, r, n) == 0;
    bad |= !ok;
    unsigned long long h = 1469598103934665603ULL;
    for (long long i = 0; i < cs; i++) { h ^= o[i]; h *= 1099511628211ULL; }
    printf("{\"bytes\": %ld, \"out\": %lld, \"c_ns_per_byte\": %.1f, \"d_ns_per_byte\": %.1f, "
           "\"c_cpu_ns_per_byte\": %.1f, \"d_cpu_ns_per_byte\": %.1f, \"roundtrip_ok\": %s, \"fnv\": \"%016llx\"}\n",
           n, cs, (t1 - t0) * 1e9 / n, (t2 - t1) * 1e9 / n, (u1 - u0) * 1e9 / n, (u2 - u1) * 1e9 / n,
           ok ? "true" : "false", h);
    fflush(stdout);
  }
  return bad;
}
