/*
 * mixz engine — context-mixing compressor core. This file is the current codec (its ID and
 * sha256 are in registry.json; codec 2 since plan step 2).
 *
 * Pipeline per bit:
 *   hashed context models (bit-history states -> adaptive probability)
 *   + run models + order-0 model + match model
 *   -> 2-layer gated logistic mixer (a small online neural network)
 *   -> 2 APM/SSE refinement stages
 *   -> binary arithmetic coder (16-bit probabilities)
 *
 * The first-layer mixer weights are 16-bit (1.0 = 1 << WS): products are summed in pairs with
 * each pair sum shifted down by 8, and the weight update rounds to nearest and saturates at the
 * 16-bit limits. On AVX2 this is one multiply-add and one rounding multiply per 16 inputs; the
 * scalar path gives identical results. The work is arranged for speed: squash by table,
 * StateMap tables inside the model with the entry kept from predict to update and updated with
 * one addition, run-model state read once per byte, and the next byte's and nibble's table
 * lookups, the APM rows and the mixer row prefetched as soon as the bit is known.
 *
 * The model can be "primed": a per-format primer is replayed through the model
 * before coding (encoder and decoder do the same), so statistics and the match
 * buffer start from learned per-format knowledge instead of zero.
 *
 * All arithmetic is integer-only so encoder and decoder are bit-exact across
 * compilers and CPUs. Exported functions have the prefix cm_.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#if defined(__AVX2__)
#include <immintrin.h>
#endif

typedef uint8_t U8;
typedef uint16_t U16;
typedef uint32_t U32;
typedef int32_t I32;
typedef uint64_t U64;
typedef int64_t I64;

#define EXPORT __attribute__((visibility("default")))
/* Fixed parameters: changing any of them changes the output, so it would be a new codec. */
#define LR1 2
#define LR2 1
#define WINIT 8192
#define SHIFT2 12
#define SMLIM 1023
#define APMRATE 6
#define WS 13 /* first-layer weights: 16-bit, 1.0 = 1 << WS */
#define FW0 1
#define FW1 1
#define FW2 2

/* ------------------------------------------------------------------ */
/* squash / stretch: logistic function and its inverse, 12-bit domain  */
/* ------------------------------------------------------------------ */
static const int SQ_T[33] = {1, 2, 4, 6, 10, 17, 27, 45, 74, 120, 194,
                             311, 488, 747, 1102, 1546, 2048, 2550, 2994, 3349,
                             3608, 3785, 3902, 3976, 4022, 4051, 4069, 4079,
                             4086, 4090, 4092, 4094, 4095};

static inline int squash(int d) { /* d: stretch units (1/256), returns p in 1..4095 */
  if (d > 2047) d = 2047;
  if (d < -2047) d = -2047;
  int w = d & 127;
  int i = (d >> 7) + 16;
  return (SQ_T[i] * (128 - w) + SQ_T[i + 1] * w + 64) >> 7;
}

static short STRETCH[4096];
static short SQT[4095]; /* squash(d) for d in -2047..2047 */
static int g_tables_ready = 0;

/* ------------------------------------------------------------------ */
/* Bit-history state machine                                           */
/* state = (n0, n1): bounded counts of 0s and 1s seen in a context.    */
/* On observing bit y the opposite count is discounted (non-stationary)*/
/* ------------------------------------------------------------------ */
#define MAXSTATES 256
static U8 NEX[MAXSTATES][2];
static U8 ST_N0[MAXSTATES], ST_N1[MAXSTATES];
static U8 DETF[MAXSTATES]; /* state seen only 0s or only 1s */
static U8 STN[MAXSTATES];  /* n0 + n1 */
static int NSTATES = 0;
static const int MAXFOR[8] = {40, 30, 24, 16, 10, 8, 7, 7};

static int st_allowed(int a, int b) {
  int mn = a < b ? a : b, mx = a < b ? b : a;
  if (mn >= 8) return 0;
  return mx <= MAXFOR[mn];
}
static int discount(int x) {
  return (x >= 1) + (x >= 2) + (x >= 3) + (x >= 4) + (x >= 5) + (x >= 7) + (x >= 8);
}
static int st_index[64][64];

static void build_states(void) {
  for (int i = 0; i < 64; i++)
    for (int j = 0; j < 64; j++) st_index[i][j] = -1;
  /* BFS from (0,0) so indices are deterministic */
  int qa[MAXSTATES], qb[MAXSTATES];
  int head = 0, tail = 0;
  st_index[0][0] = 0;
  qa[tail] = 0; qb[tail] = 0; tail++;
  while (head < tail) {
    int a = qa[head], b = qb[head]; head++;
    for (int y = 0; y < 2; y++) {
      int na = a, nb = b;
      if (y) { nb++; if (na > 2) na = discount(na); }
      else   { na++; if (nb > 2) nb = discount(nb); }
      while (!st_allowed(na, nb)) { if (na > nb) na--; else nb--; }
      if (st_index[na][nb] < 0) {
        st_index[na][nb] = tail;
        qa[tail] = na; qb[tail] = nb; tail++;
      }
    }
  }
  NSTATES = tail;
  for (int s = 0; s < NSTATES; s++) {
    int a = qa[s], b = qb[s];
    ST_N0[s] = (U8)a; ST_N1[s] = (U8)b;
    for (int y = 0; y < 2; y++) {
      int na = a, nb = b;
      if (y) { nb++; if (na > 2) na = discount(na); }
      else   { na++; if (nb > 2) nb = discount(nb); }
      while (!st_allowed(na, nb)) { if (na > nb) na--; else nb--; }
      NEX[s][y] = (U8)st_index[na][nb];
    }
  }
  for (int s = 0; s < MAXSTATES; s++) {
    DETF[s] = (U8)((s != 0) && (ST_N0[s] == 0 || ST_N1[s] == 0));
    STN[s] = (U8)(ST_N0[s] + ST_N1[s]);
  }
}

static int DT[1024]; /* adaptive learning-rate reciprocal table */

static void init_tables(void) {
  if (g_tables_ready) return;
  int pi = 0;
  for (int x = -2047; x <= 2047; x++) {
    int v = squash(x);
    for (int i = pi; i <= v; i++) STRETCH[i] = (short)x;
    pi = v + 1;
  }
  for (int i = pi; i < 4096; i++) STRETCH[i] = 2047;
  for (int d = -2047; d <= 2047; d++) SQT[d + 2047] = (short)squash(d);
  for (int i = 0; i < 1024; i++) DT[i] = (int)(131072 / (2 * i + 3)); /* 65536/(i+1.5) */
  build_states();
  g_tables_ready = 1;
}
static inline int stretch(int p) { return STRETCH[p]; }
static inline int squash_t(int d) { return SQT[d + 2047]; } /* d already in -2047..2047 */

/* ------------------------------------------------------------------ */
/* StateMap: context -> adaptive probability (22-bit p, 10-bit count)  */
/* ------------------------------------------------------------------ */
/* The tables live inside the Model; predict remembers the entry each map used, and
   the update after the bit changes that entry. */
static void sm_init(U32 *t, int n) {
  for (int i = 0; i < n; i++) t[i] = 1u << 31;
}
static void sm_init_states(U32 *t, int n) { /* prior from (n0,n1) */
  for (int i = 0; i < n; i++) {
    int s = i & 255;
    if (s >= NSTATES) continue;
    int a = ST_N0[s], b = ST_N1[s];
    U32 p22 = (U32)(((U64)(2 * b + 1) << 22) / (U64)(2 * (a + b) + 2));
    if (p22 > (1u << 22) - 1) p22 = (1u << 22) - 1;
    t[i] = p22 << 10;
  }
}
static inline int sm_p(const U32 *e) { return (int)(*e >> 20); }
static inline void sm_update(U32 *e, int y, int limit) {
  U32 t = *e;
  int n = (int)(t & 1023);
  I64 p = (I64)(t >> 10);
  I64 target = y ? ((1 << 22) - 1) : 0;
  /* p + d stays in 0..2^22-1 without clamping: DT[n] < 65536, so the step is less than
     |target - p|, and the floor of a negative step is at least -p. So adding d << 10 to the
     packed word changes only the p field, and n + 1 <= limit <= 1023 cannot carry into it. */
  I64 d = ((target - p) * DT[n]) >> 16;
  *e = t + ((U32)d << 10) + (U32)(n < limit);
}

/* ------------------------------------------------------------------ */
/* APM / SSE: refines a probability given a small context              */
/* ------------------------------------------------------------------ */
typedef struct { U16 *t; int idx; } APM;
static int apm_init(APM *a, int n) {
  a->t = (U16 *)malloc(sizeof(U16) * n * 33);
  if (!a->t) return -1;
  U16 row[33];
  for (int j = 0; j < 33; j++) {
    int v = squash((j - 16) * 128) * 16;
    row[j] = (U16)(v > 65535 ? 65535 : v);
  }
  for (int i = 0; i < n; i++) memcpy(a->t + (size_t)i * 33, row, sizeof(row));
  a->idx = 0;
  return 0;
}
static inline int apm_pp(APM *a, int s, int cx) { /* s = stretch(p) + 2048, 1..4095; returns 16-bit prob */
  int lo = s >> 7, w = s & 127;
  int base = cx * 33 + lo;
  a->idx = base + (w >> 6);
  return (a->t[base] * (128 - w) + a->t[base + 1] * w) >> 7;
}
static inline void apm_update(APM *a, int y, int rate) {
  int g = (y << 16) + (y << rate) - y - y;
  int v = a->t[a->idx];
  v += (g - v) >> rate;
  if (v < 0) v = 0;
  if (v > 65535) v = 65535;
  a->t[a->idx] = (U16)v;
}

/* ------------------------------------------------------------------ */
/* Hashing                                                             */
/* ------------------------------------------------------------------ */
static inline U32 hmix(U32 h) {
  h ^= h >> 16; h *= 0x7FEB352Du; h ^= h >> 15; h *= 0x846CA68Bu; h ^= h >> 16;
  return h;
}
static inline U32 H2(U32 a, U32 b) { return hmix(a * 0x9E3779B1u + hmix(b + 0x632BE5ABu)); }
static inline U32 H3(U32 a, U32 b, U32 c) { return H2(H2(a, b), c); }
static inline U32 H4(U32 a, U32 b, U32 c, U32 d) { return H2(H3(a, b, c), d); }

/* ------------------------------------------------------------------ */
/* Model                                                               */
/* ------------------------------------------------------------------ */
#define NC 8                 /* hashed context models */
#define NIN_USED (NC * 3 + 1 + 2 + 1) /* inputs: 3 per ctx, order0, match x2, bias */
#define NIN 32 /* padded for SIMD */
#define NM1 4                /* first-layer mixers */
#define MINLEN 6u            /* match model: shortest match, all profiles but image */
#define MINLEN_IMAGE 2u      /* image: 2 bytes (-5% on PNG pixels against 6, plan step 6) */

/* code and web are text with three and two context slots replaced (plan step 6) */
enum { PROF_TEXT = 0, PROF_BIN = 1, PROF_IMAGE = 2, PROF_CODE = 3, PROF_WEB = 4 };

typedef struct {
  int profile, stride, bpp;
  /* context tables: buckets of 16 bytes = [checksum, 15 nibble-tree states] */
  U8 *T[NC];
  U32 tbits;
  U8 *cp[NC];
  U8 *sp[NC]; int ss[NC]; /* this bit's slot and its state */
  U32 h[NC];
  U32 smt[NC][256]; U32 *sme[NC];
  /* run model: per (context, byte-history) the last byte seen and its repeat count */
  U32 *R[NC]; U32 *rp[NC]; int rexp[NC]; U32 rsmt[NC][64]; U32 *rsme[NC];
  U32 o0t[256], mmt[64]; U32 *o0e, *mme;
  /* match model */
  U8 *buf; U32 bufmask; U32 pos;
  U32 *ht; U32 htbits;
  U32 mlen, mptr; int mexp; int mq;
  /* mixer */
  short x[NIN];
  short *w1[NM1]; int sel[NM1]; int nsel[NM1]; int d1[NM1]; int p1[NM1];
  I32 *w2; int sel2; int d2; int pm;
  APM a1, a2; int pa1, pa2;
  /* byte state */
  U32 c0, c4, c8; int bits; int node;
  U32 word, word1;
  U32 line_start, prev_line_start;
  U32 img_pos, img_ch; /* image: bytes since the start, and that count modulo bpp */
  /* code and web profiles: the line's indentation (columns), whether the line is still in it, the
     line's first other byte; bracket depth and the last 4 open brackets; an HTML tag name hash and
     where we are in a tag (1 name, 2 after it); the last punctuation byte */
  U32 indent, inlead, lfirst, depth, bstack, tag, intag, lastp;
  U32 minlen;          /* match model: shortest match used */
  U32 nh[NC];          /* bucket hashes for the second nibble */
  U32 ahi, ahash;      /* per-byte parts of the two APM contexts */
  U32 mh;              /* match-model hash of the last minlen bytes */
  U32 rh[NC];          /* run-table hashes */
  int rbyte[NC], rq2[NC], rlive[NC]; /* run model for this byte: byte, 2*min(count,15), still matching */
  int pr; /* final 16-bit probability of bit==1 */
  /* write log of a worker (NULL otherwise, and once full): every entry of the large tables this
     file may change, as (kind << 29) | (context << 24) | index; restored from the snapshot */
  U32 *lp, *lend;
} Model;

enum { LOG_T, LOG_R, LOG_HT, LOG_A1, LOG_A2 };
#define INLINE inline __attribute__((always_inline))
/* lg: a compile-time constant; model_update is built with and without the log */
static INLINE void log_w(Model *m, int lg, U32 kind, U32 i, U32 idx) {
  if (lg && m->lp) {
    *m->lp++ = (kind << 29) | (i << 24) | idx;
    if (m->lp == m->lend) m->lp = NULL; /* full: the next load copies everything */
  }
}

static void model_free(Model *m) {
  for (int i = 0; i < NC; i++) { free(m->T[i]); free(m->R[i]); }
  free(m->buf); free(m->ht);
  for (int i = 0; i < NM1; i++) free(m->w1[i]);
  free(m->w2); free(m->a1.t); free(m->a2.t);
}

static U32 ilog2u(U64 x) { U32 r = 0; while (x > 1) { x >>= 1; r++; } return r; }

static inline U8 hist(Model *m, U32 back) { /* byte at pos-back (1 = last byte) */
  if (back == 0 || back > m->pos) return 0;
  return m->buf[(m->pos - back) & m->bufmask];
}

static U8 *bucket_find(Model *m, int i, U32 hh) {
  U32 idx = hh >> (32 - m->tbits);
  U8 chk = (U8)(hh & 255);
  U8 *b0 = m->T[i] + ((size_t)idx << 4);
  if (b0[0] == chk) return b0;
  U8 *b1 = m->T[i] + ((size_t)(idx ^ 1) << 4);
  if (b1[0] == chk) return b1;
  int p0 = STN[b0[1]], p1 = STN[b1[1]];
  U8 *r = (p0 <= p1) ? b0 : b1;
  memset(r, 0, 16);
  r[0] = chk;
  return r;
}

static inline void run_hashes(Model *m) {
  for (int i = 0; i < NC; i++) m->rh[i] = hmix(m->h[i] ^ 0xA5A5A5A5u);
}
static INLINE void run_lookup(Model *m, int lg) {
  for (int i = 0; i < NC; i++) {
    U32 hh = m->rh[i];
    U32 *r = m->R[i] + (hh >> (32 - (m->tbits - 1)));
    U32 chk = hh & 0xffff;
    if ((*r >> 16) != chk) *r = chk << 16; /* new: byte 0, count 0 */
    m->rp[i] = r;
    log_w(m, lg, LOG_R, (U32)i, (U32)(r - m->R[i]));
    U32 cnt = *r & 255;
    m->rbyte[i] = (int)((*r >> 8) & 255);
    m->rq2[i] = (cnt > 15 ? 15 : (int)cnt) * 2;
    m->rlive[i] = cnt != 0;
  }
}
static inline void run_update(Model *m, U8 c) {
  for (int i = 0; i < NC; i++) {
    U32 r = *m->rp[i];
    U32 cnt = r & 255, b = (r >> 8) & 255;
    if (cnt && b == c) { if (cnt < 255) cnt++; }
    else { b = c; cnt = 1; }
    *m->rp[i] = (r & 0xffff0000u) | (b << 8) | cnt;
  }
}

static void compute_contexts(Model *m) {
  U32 c4 = m->c4, c8 = m->c8;
  U32 c1 = c4 & 255;
  if (m->profile == PROF_IMAGE) {
    U32 b = (U32)m->bpp, s = (U32)m->stride;
    U32 W = hist(m, b), N = hist(m, s), NW = hist(m, s + b), NE = hist(m, s > b ? s - b : 0);
    U32 WW = hist(m, 2 * b), NN = hist(m, 2 * s);
    U32 ch = m->img_ch;
    int grad = (int)W + (int)N - (int)NW; if (grad < 0) grad = 0; if (grad > 255) grad = 255;
    int ew = 2 * (int)W - (int)WW; if (ew < 0) ew = 0; if (ew > 255) ew = 255;
    int en = 2 * (int)N - (int)NN; if (en < 0) en = 0; if (en > 255) en = 255;
    m->h[0] = H3(1, W, ch);
    m->h[1] = H3(2, N, ch);
    m->h[2] = H4(3, W, N, ch);
    m->h[3] = H4(4, W, N, NW);
    m->h[4] = H3(5, (U32)grad, ch);
    m->h[5] = H4(6, N, NE, ch);
    m->h[6] = H4(7, c1, W, ch);
    m->h[7] = H4(8, (U32)ew, (U32)en, ch);
  } else if (m->profile == PROF_BIN) {
    m->h[0] = H2(1, c1);
    m->h[1] = H2(2, c4 & 0xffff);
    m->h[2] = H2(3, c4 & 0xffffff);
    m->h[3] = H2(4, c4);
    m->h[4] = H3(5, c4, c8 & 0xffff);
    m->h[5] = H2(6, c4 & 0xffff00);
    m->h[6] = H2(7, c4 & 0xff00ff);
    m->h[7] = H4(8, c1, c4 >> 24, m->pos & 3);
  } else {
    U32 col = m->pos - m->line_start;
    U32 above = 0;
    if (m->prev_line_start + col < m->line_start) above = hist(m, m->pos - (m->prev_line_start + col));
    m->h[0] = H2(1, c1);
    m->h[1] = H2(2, c4 & 0xffff);
    m->h[2] = H2(3, c4 & 0xffffff);
    m->h[3] = H2(4, c4);
    m->h[4] = H3(5, c4, c8 & 0xffff);
    m->h[5] = H3(6, m->word, m->word ? 0 : c1);
    m->h[6] = H3(7, m->word, m->word1);
    m->h[7] = H4(8, above, c1, col < 32 ? col : 32);
    if (m->profile == PROF_CODE) { /* indentation, last punctuation, bracket nesting */
      m->h[0] = H4(9, (m->indent > 31 ? 31 : m->indent) | (m->inlead << 8), m->lfirst, c1);
      m->h[1] = H3(12, m->lastp, c4 & 0xffff);
      m->h[6] = H4(10, m->depth > 15 ? 15 : m->depth, m->bstack & 0xffff, c4 & 0xffff);
    } else if (m->profile == PROF_WEB) { /* bracket nesting, HTML tag */
      m->h[1] = H4(10, m->depth > 15 ? 15 : m->depth, m->bstack & 0xffff, c4 & 0xffff);
      m->h[7] = H4(11, m->tag, m->intag, c4 & 0xffff);
    }
  }
}

static void apm_contexts(Model *m) {
  if (m->profile == PROF_IMAGE) {
    U32 W = hist(m, (U32)m->bpp), N = hist(m, (U32)m->stride);
    m->ahi = W << 8;
    m->ahash = H2(W, N) & 0xff00;
  } else {
    m->ahi = (m->c4 & 255) << 8;
    m->ahash = H2(m->c4 & 0xffff, 9) & 0xff00;
  }
}

/* table sizes depend only on (total bytes modelled, maxbits): encoder and decoder agree */
static void model_sizes(U64 total, int maxbits, U32 *tb, U32 *bb, U32 *hb) {
  /* ~2 buckets touched per byte per context */
  U32 t = ilog2u(total * 2 + 1) + 1;
  if (maxbits < 16) maxbits = 16;
  if (maxbits > 24) maxbits = 24;
  if (t < 16) t = 16;
  if (t > (U32)maxbits) t = (U32)maxbits;
  U32 b = ilog2u(total + 1) + 1;
  if (b < 16) b = 16;
  if (b > 28) b = 28;
  U32 h = b - 2;
  if (h < 16) h = 16;
  if (h > 24) h = 24;
  *tb = t; *bb = b; *hb = h;
}

static int model_init(Model *m, int profile, int stride, int bpp, int maxbits, U64 total) {
  memset(m, 0, sizeof(*m));
  init_tables();
  if (profile < PROF_TEXT || profile > PROF_WEB) return -1;
  m->profile = profile;
  m->minlen = profile == PROF_IMAGE ? MINLEN_IMAGE : MINLEN;
  m->stride = stride > 0 ? stride : 1;
  m->bpp = bpp > 0 ? bpp : 1;
  U32 tb, bb, hb;
  model_sizes(total, maxbits, &tb, &bb, &hb);
  m->tbits = tb;
  for (int i = 0; i < NC; i++) {
    m->T[i] = (U8 *)calloc((size_t)1 << tb, 16);
    if (!m->T[i]) return -1;
    sm_init(m->smt[i], 256);
    sm_init_states(m->smt[i], 256);
    m->sme[i] = m->smt[i];
    m->R[i] = (U32 *)calloc((size_t)1 << (tb - 1), sizeof(U32));
    if (!m->R[i]) return -1;
    sm_init(m->rsmt[i], 64);
    m->rsme[i] = m->rsmt[i];
  }
  sm_init(m->o0t, 256); sm_init(m->mmt, 64);
  m->o0e = m->o0t; m->mme = m->mmt;
  m->buf = (U8 *)calloc((size_t)1 << bb, 1);
  m->bufmask = (1u << bb) - 1;
  m->htbits = hb;
  m->ht = (U32 *)calloc((size_t)1 << hb, sizeof(U32));
  if (!m->buf || !m->ht) return -1;
  m->nsel[0] = 256; m->nsel[1] = 32 * 8; m->nsel[2] = 256; m->nsel[3] = 10 * 8 * 2;
  for (int k = 0; k < NM1; k++) {
    m->w1[k] = (short *)aligned_alloc(64, sizeof(short) * NIN * m->nsel[k]); /* one row = one cache line */
    if (!m->w1[k]) return -1;
    for (int i = 0; i < NIN * m->nsel[k]; i++) m->w1[k][i] = (short)(WINIT >> (16 - WS));
  }
  m->w2 = (I32 *)malloc(sizeof(I32) * (NM1 + 1) * 8 * 2);
  if (!m->w2) return -1;
  for (int i = 0; i < (NM1 + 1) * 8 * 2; i++) m->w2[i] = 65536 / NM1;
  if (apm_init(&m->a1, 65536) || apm_init(&m->a2, 65536)) return -1;
  m->c0 = 1; m->bits = 0; m->node = 1;
  m->inlead = 1;
  compute_contexts(m);
  apm_contexts(m);
  run_hashes(m);
  for (int i = 0; i < NC; i++) m->cp[i] = bucket_find(m, i, m->h[i]);
  run_lookup(m, 0);
  m->pr = 32768;
  m->pm = 2048;
  return 0;
}

/* weights: 16.16 fixed point, clamped to +-8.0 so int32 products never overflow */
#define WMAX (1 << 19)
static inline int dot(const short *x, const I32 *w, int n) {
  I32 s = 0;
  for (int i = 0; i < n; i++) s += (x[i] * w[i]) >> 8;
  s >>= 8;
  if (s > 2047) s = 2047;
  if (s < -2047) s = -2047;
  return (int)s;
}
/* First layer: NM1 mixers of NIN 16-bit inputs and 16-bit weights. Products are summed in
   pairs and each pair sum is shifted down by 8 (as the AVX2 multiply-add does it), so the
   vector and scalar paths give identical results. */
static void mix1_dot(Model *m) {
  int r[NM1];
#if defined(__AVX2__)
  __m256i x0 = _mm256_loadu_si256((const __m256i *)m->x), x1 = _mm256_loadu_si256((const __m256i *)(m->x + 16));
  __m256i s[NM1];
  for (int j = 0; j < NM1; j++) {
    const short *w = m->w1[j] + (size_t)m->sel[j] * NIN;
    __m256i a = _mm256_srai_epi32(_mm256_madd_epi16(x0, _mm256_load_si256((const __m256i *)w)), 8);
    __m256i b = _mm256_srai_epi32(_mm256_madd_epi16(x1, _mm256_load_si256((const __m256i *)(w + 16))), 8);
    s[j] = _mm256_add_epi32(a, b);
  }
  __m256i h = _mm256_hadd_epi32(_mm256_hadd_epi32(s[0], s[1]), _mm256_hadd_epi32(s[2], s[3]));
  _mm_storeu_si128((__m128i *)r, _mm_add_epi32(_mm256_castsi256_si128(h), _mm256_extracti128_si256(h, 1)));
#else
  for (int j = 0; j < NM1; j++) {
    const short *w = m->w1[j] + (size_t)m->sel[j] * NIN;
    I32 s = 0;
    for (int i = 0; i < NIN; i += 2) s += (m->x[i] * w[i] + m->x[i + 1] * w[i + 1]) >> 8;
    r[j] = s;
  }
#endif
  for (int j = 0; j < NM1; j++) {
    int d = r[j] >> (WS - 8);
    if (d > 2047) d = 2047;
    if (d < -2047) d = -2047;
    m->d1[j] = d;
    m->p1[j] = squash_t(d);
  }
}
/* w += round(x * err / 2^(28 - WS)), saturating at the 16-bit limits */
static void mix1_train(Model *m, int y) {
  for (int j = 0; j < NM1; j++) {
    int e = ((y << 12) - m->p1[j]) * LR1 * (1 << (WS - 13)); /* |e| < 2^15 for WS <= 15; a multiply,
                                                                because shifting a negative int left is undefined in C */
    short *w = m->w1[j] + (size_t)m->sel[j] * NIN;
#if defined(__AVX2__)
    __m256i ev = _mm256_set1_epi16((short)e);
    __m256i w0 = _mm256_load_si256((const __m256i *)w), w1 = _mm256_load_si256((const __m256i *)(w + 16));
    w0 = _mm256_adds_epi16(w0, _mm256_mulhrs_epi16(_mm256_loadu_si256((const __m256i *)m->x), ev));
    w1 = _mm256_adds_epi16(w1, _mm256_mulhrs_epi16(_mm256_loadu_si256((const __m256i *)(m->x + 16)), ev));
    _mm256_store_si256((__m256i *)w, w0);
    _mm256_store_si256((__m256i *)(w + 16), w1);
#else
    for (int i = 0; i < NIN; i++) {
      int v = w[i] + ((m->x[i] * e + 16384) >> 15);
      w[i] = (short)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
    }
#endif
  }
}

static inline void train(const short *x, I32 *w, int n, int err, int shift) {
  for (int i = 0; i < n; i++) {
    I32 v = w[i] + ((x[i] * err) >> shift);
    v = v > WMAX ? WMAX : v;
    v = v < -WMAX ? -WMAX : v;
    w[i] = v;
  }
}

static inline int lenq(U32 len) {
  if (len < 16) return (int)len;
  if (len < 32) return 16 + (int)((len - 16) >> 2);
  if (len < 64) return 20 + (int)((len - 32) >> 3);
  if (len < 128) return 24 + (int)((len - 64) >> 4);
  return len < 256 ? 28 + (int)((len - 128) >> 6) : 31;
}

static void predict(Model *m) {
  int k = 0, known = 0;
  for (int i = 0; i < NC; i++) {
    U8 *slot = m->cp[i] + m->node;
    m->sp[i] = slot;
    int s = *slot;
    m->ss[i] = s;
    known += s != 0;
    m->sme[i] = m->smt[i] + s;
    int st = stretch(sm_p(m->sme[i]));
    m->x[k++] = (short)st;
    m->x[k++] = (short)(DETF[s] ? st : 0);
    /* run model: live while every bit of this byte so far matched the run byte */
    m->rexp[i] = -1;
    if (m->rlive[i]) {
      int e = (m->rbyte[i] >> (7 - m->bits)) & 1;
      m->rexp[i] = e;
      m->rsme[i] = m->rsmt[i] + m->rq2[i] + e;
      m->x[k++] = (short)stretch(sm_p(m->rsme[i]));
    } else m->x[k++] = 0;
  }
  m->o0e = m->o0t + m->c0;
  m->x[k++] = (short)stretch(sm_p(m->o0e));
  /* match model */
  m->mexp = -1; m->mq = 0;
  if (m->mlen > 0) {
    U32 pb = m->buf[m->mptr & m->bufmask] | 256u;
    if ((pb >> (8 - m->bits)) == m->c0) {
      m->mexp = (int)((pb >> (7 - m->bits)) & 1);
      m->mq = lenq(m->mlen);
    } else {
      m->mlen = 0;
    }
  }
  if (m->mexp >= 0) {
    m->mme = m->mmt + m->mq * 2 + m->mexp;
    int st = stretch(sm_p(m->mme));
    m->x[k++] = (short)st;
    int conf = (int)(m->mlen < 32 ? m->mlen : 32) * 64;
    m->x[k++] = (short)(m->mexp ? conf : -conf);
  } else {
    m->x[k++] = 0; m->x[k++] = 0;
  }
  m->x[k++] = 256; /* bias */
  while (k < NIN) m->x[k++] = 0;

  /* layer 1: three gated mixers with different selector contexts */
  U32 c1 = m->c4 & 255;
  m->sel[0] = (int)m->c0;
  m->sel[1] = (m->mexp >= 0 ? (m->mq + 1 > 31 ? 31 : m->mq + 1) : 0) * 8 + m->bits;
  if (m->profile == PROF_IMAGE) m->sel[2] = hist(m, (U32)m->bpp);
  else m->sel[2] = (int)c1;
  m->sel[3] = (known * 8 + m->bits) * 2 + (m->rexp[3] >= 0);
  mix1_dot(m);
  short x2[NM1 + 1];
  for (int j = 0; j < NM1; j++) x2[j] = (short)m->d1[j];
  x2[NM1] = 256;
  /* layer 2 */
  m->sel2 = m->bits * 2 + (m->mexp >= 0);
  m->d2 = dot(x2, m->w2 + (size_t)m->sel2 * (NM1 + 1), NM1 + 1);
  m->pm = squash_t(m->d2);

  /* SSE stages */
  int ca = (int)(m->c0 | m->ahi);
  int cb = (int)((m->c0 ^ m->ahash) & 0xffff);
  int sp = stretch(m->pm) + 2048;
  m->pa1 = apm_pp(&m->a1, sp, ca);
  m->pa2 = apm_pp(&m->a2, sp, cb);
  int p = (int)((U32)(m->pm * 16 * FW0 + m->pa1 * FW1 + m->pa2 * FW2) / (FW0 + FW1 + FW2)); /* all terms >= 0 */
  if (p < 32) p = 32;
  if (p > 65535 - 32) p = 65535 - 32;
  m->pr = p;
}

/* hash of the last n bytes (= hist(m, i) for i = 1..n); n is a constant at each call */
static INLINE U32 match_hash(const Model *m, U32 n) {
  U32 hh = 0;
  for (U32 i = 1; i <= n; i++) hh = (hh + m->buf[(m->pos - i) & m->bufmask] + 1u) * 0x2F0F3D2Bu;
  return hh;
}

/* first half of the byte update: history, words, lines, match extension; prefetches the
   match table slot that match_find reads after the bit updates */
static void byte_update(Model *m, U8 c) {
  m->buf[m->pos & m->bufmask] = c;
  m->pos++;
  m->img_pos++;
  if (++m->img_ch == (U32)m->bpp) m->img_ch = 0;
  m->c8 = (m->c8 << 8) | (m->c4 >> 24);
  m->c4 = (m->c4 << 8) | c;
  /* words (letters, case-folded) */
  U8 lc = (c >= 'A' && c <= 'Z') ? (U8)(c + 32) : c;
  if ((lc >= 'a' && lc <= 'z') || c >= 128 || (c >= '0' && c <= '9') || c == '_') {
    m->word = (m->word + lc + 1) * 0x2F0F3D2Bu;
  } else if (m->word) {
    m->word1 = m->word;
    m->word = 0;
  }
  if (c == '\n') { m->prev_line_start = m->line_start; m->line_start = m->pos; }
  if (m->profile >= PROF_CODE) {
    if (c == '\n') { m->indent = 0; m->inlead = 1; m->lfirst = 0; }
    else if (m->inlead && (c == ' ' || c == '\t')) m->indent += c == '\t' ? 4 : 1;
    else if (m->inlead) { m->inlead = 0; m->lfirst = c; }
    if (c == '(' || c == '[' || c == '{') { m->depth++; m->bstack = (m->bstack << 8) | c; }
    else if ((c == ')' || c == ']' || c == '}') && m->depth) { m->depth--; m->bstack >>= 8; }
    if (c == '<') { m->intag = 1; m->tag = 0; }
    else if (c == '>') m->intag = 0;
    else if (m->intag == 1) {
      if ((lc >= 'a' && lc <= 'z') || (c >= '0' && c <= '9') || c == '/' || c == '!' || c == ':')
        m->tag = (m->tag + lc + 1) * 0x2F0F3D2Bu;
      else m->intag = 2;
    }
    if (!((lc >= 'a' && lc <= 'z') || (c >= '0' && c <= '9') || c == ' ' || c == '\n' || c == '\t' || c >= 128))
      m->lastp = c;
  }
  /* match model */
  if (m->mlen > 0) {
    if (m->buf[m->mptr & m->bufmask] == c) {
      m->mlen++; m->mptr++;
      if (m->mlen > 65535) m->mlen = 65535;
    } else m->mlen = 0;
  }
  if (m->pos >= m->minlen) {
    U32 hh = m->minlen == MINLEN ? match_hash(m, MINLEN) : match_hash(m, MINLEN_IMAGE);
    m->mh = hmix(hh) >> (32 - m->htbits);
    __builtin_prefetch(m->ht + m->mh);
  }
}

/* second half: look for a new match and record this position */
static INLINE void match_find(Model *m, int lg) {
  if (m->pos >= m->minlen) {
    U32 hh = m->mh;
    if (m->mlen == 0) {
      U32 cand = m->ht[hh];
      if (cand > 0 && m->pos - cand < m->bufmask) {
        U32 l = 0;
        while (l < 400 && l < cand &&
               m->buf[(cand - l - 1) & m->bufmask] == m->buf[(m->pos - l - 1) & m->bufmask])
          l++;
        if (l >= m->minlen) { m->mlen = l; m->mptr = cand; }
      }
    }
    m->ht[hh] = m->pos;
    log_w(m, lg, LOG_HT, 0, hh);
  }
}

static void prefetch_contexts(Model *m) {
  for (int i = 0; i < NC; i++) {
    __builtin_prefetch(m->T[i] + ((size_t)(m->h[i] >> (32 - m->tbits)) << 4));
    __builtin_prefetch(m->R[i] + (m->rh[i] >> (32 - (m->tbits - 1))));
  }
}

static INLINE void model_update_t(Model *m, int y, int lg) {
  /* Work that needs only y comes first, so the memory loads it starts overlap the updates
     below. None of it reads or writes state those updates use. */
  U32 c0n = (m->c0 << 1) | (U32)y;
  if (m->bits == 7) {
    run_update(m, (U8)(c0n & 255));
    byte_update(m, (U8)(c0n & 255));
    compute_contexts(m);
    apm_contexts(m);
    run_hashes(m);
    prefetch_contexts(m);
  } else if (m->bits == 3) {
    for (int i = 0; i < NC; i++) {
      m->nh[i] = hmix(m->h[i] + c0n * 0x9E3779B1u);
      __builtin_prefetch(m->T[i] + ((size_t)(m->nh[i] >> (32 - m->tbits)) << 4));
    }
  }
  {
    U32 c = m->bits == 7 ? 1 : c0n;
    const U16 *r1 = m->a1.t + (size_t)(c | m->ahi) * 33, *r2 = m->a2.t + (size_t)((c ^ m->ahash) & 0xffff) * 33;
    __builtin_prefetch(r1); __builtin_prefetch(r1 + 32);
    __builtin_prefetch(r2); __builtin_prefetch(r2 + 32);
    __builtin_prefetch(m->w1[0] + (size_t)c * NIN);
  }
  /* SSE */
  log_w(m, lg, LOG_A1, 0, (U32)m->a1.idx);
  log_w(m, lg, LOG_A2, 0, (U32)m->a2.idx);
  apm_update(&m->a1, y, APMRATE);
  apm_update(&m->a2, y, APMRATE);
  /* mixers */
  int err2 = ((y << 12) - m->pm) * LR2;
  short x2[NM1 + 1];
  for (int j = 0; j < NM1; j++) x2[j] = (short)m->d1[j];
  x2[NM1] = 256;
  train(x2, m->w2 + (size_t)m->sel2 * (NM1 + 1), NM1 + 1, err2, SHIFT2);
  mix1_train(m, y);
  /* context states */
  for (int i = 0; i < NC; i++) {
    *m->sp[i] = NEX[m->ss[i]][y];
    sm_update(m->sme[i], y, SMLIM);
    if (m->rexp[i] >= 0) sm_update(m->rsme[i], y, 1023);
    m->rlive[i] = m->rexp[i] == y;
  }
  sm_update(m->o0e, y, 255);
  if (m->mexp >= 0) sm_update(m->mme, y, 1023);

  m->c0 = c0n;
  m->bits++;
  m->node = m->node * 2 + y;
  if (m->bits == 8) {
    match_find(m, lg);
    m->c0 = 1; m->bits = 0; m->node = 1;
    for (int i = 0; i < NC; i++) {
      m->cp[i] = bucket_find(m, i, m->h[i]);
      log_w(m, lg, LOG_T, (U32)i, (U32)((m->cp[i] - m->T[i]) >> 4));
    }
    run_lookup(m, lg);
  } else if (m->bits == 4) {
    m->node = 1;
    for (int i = 0; i < NC; i++) {
      m->cp[i] = bucket_find(m, i, m->nh[i]);
      log_w(m, lg, LOG_T, (U32)i, (U32)((m->cp[i] - m->T[i]) >> 4));
    }
  }
  predict(m);
}
static void model_update(Model *m, int y) { model_update_t(m, y, 0); }
static void model_update_log(Model *m, int y) { model_update_t(m, y, 1); }

static void model_start(Model *m) { predict(m); }

/* replay primer bytes through the model (no output) */
static void model_prime(Model *m, const U8 *primer, size_t plen) {
  for (size_t i = 0; i < plen; i++) {
    U8 c = primer[i];
    for (int b = 7; b >= 0; b--) model_update(m, (c >> b) & 1);
  }
  m->img_pos = 0;
  m->img_ch = 0;
}

/* ------------------------------------------------------------------ */
/* Arithmetic coder                                                    */
/* ------------------------------------------------------------------ */
typedef struct { U32 x1, x2, x; U8 *out; size_t outpos, outcap; const U8 *in; size_t inpos, inlen; int overflow; } Coder;

static inline void enc_bit(Coder *c, int y, int p16) {
  U32 xmid = c->x1 + (U32)(((U64)(c->x2 - c->x1) * (U32)p16) >> 16);
  if (y) c->x2 = xmid; else c->x1 = xmid + 1;
  while (((c->x1 ^ c->x2) & 0xff000000u) == 0) {
    if (c->outpos < c->outcap) c->out[c->outpos++] = (U8)(c->x2 >> 24);
    else c->overflow = 1;
    c->x1 <<= 8;
    c->x2 = (c->x2 << 8) | 255;
  }
}
static inline int dec_bit(Coder *c, int p16) {
  U32 xmid = c->x1 + (U32)(((U64)(c->x2 - c->x1) * (U32)p16) >> 16);
  int y = c->x <= xmid;
  if (y) c->x2 = xmid; else c->x1 = xmid + 1;
  while (((c->x1 ^ c->x2) & 0xff000000u) == 0) {
    c->x1 <<= 8;
    c->x2 = (c->x2 << 8) | 255;
    c->x = (c->x << 8) | (c->inpos < c->inlen ? c->in[c->inpos++] : 0);
  }
  return y;
}

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */
/* returns compressed size, or -1 on error, -2 if output buffer too small */
EXPORT long long cm_compress(const U8 *in, size_t n, const U8 *primer, size_t plen,
                             int profile, int stride, int bpp, int maxbits, U8 *out, size_t outcap) {
  Model *m = (Model *)malloc(sizeof(Model));
  if (!m) return -1;
  if (model_init(m, profile, stride, bpp, maxbits, (U64)n + plen)) { model_free(m); free(m); return -1; }
  model_start(m);
  if (plen) model_prime(m, primer, plen);
  Coder c = {0, 0xffffffffu, 0, out, 0, outcap, NULL, 0, 0, 0};
  for (size_t i = 0; i < n; i++) {
    U8 ch = in[i];
    for (int b = 7; b >= 0; b--) {
      int y = (ch >> b) & 1;
      enc_bit(&c, y, m->pr);
      model_update(m, y);
    }
    if (c.overflow) break;
  }
  for (int i = 0; i < 4; i++) {
    if (c.outpos < c.outcap) c.out[c.outpos++] = (U8)(c.x1 >> 24);
    else c.overflow = 1;
    c.x1 <<= 8;
  }
  model_free(m); free(m);
  return c.overflow ? -2 : (long long)c.outpos;
}

/* the first n bytes of a coded stream of total bytes (a bundle member): the model is sized for the
   whole stream, as when it was coded */
EXPORT int cm_decompress_part(const U8 *in, size_t inlen, const U8 *primer, size_t plen,
                              int profile, int stride, int bpp, int maxbits, U8 *out, size_t n, size_t total) {
  if (n > total) return -1;
  Model *m = (Model *)malloc(sizeof(Model));
  if (!m) return -1;
  if (model_init(m, profile, stride, bpp, maxbits, (U64)total + plen)) { model_free(m); free(m); return -1; }
  model_start(m);
  if (plen) model_prime(m, primer, plen);
  Coder c = {0, 0xffffffffu, 0, NULL, 0, 0, in, 0, inlen, 0};
  for (int i = 0; i < 4; i++) c.x = (c.x << 8) | (c.inpos < c.inlen ? c.in[c.inpos++] : 0);
  for (size_t i = 0; i < n; i++) {
    int ch = 0;
    for (int b = 0; b < 8; b++) {
      int y = dec_bit(&c, m->pr);
      model_update(m, y);
      ch = (ch << 1) | y;
    }
    out[i] = (U8)ch;
  }
  model_free(m); free(m);
  return 0;
}
EXPORT int cm_decompress(const U8 *in, size_t inlen, const U8 *primer, size_t plen,
                         int profile, int stride, int bpp, int maxbits, U8 *out, size_t n) {
  return cm_decompress_part(in, inlen, primer, plen, profile, stride, bpp, maxbits, out, n, n);
}

/* ------------------------------------------------------------------ */
/* Primed snapshots: prime once, copy per file (avoids replaying the   */
/* primer for every file in a batch). Output is identical to the       */
/* replay path because the primed state is identical.                  */
/* A worker is a model whose buffers are kept between files: each file */
/* copies the snapshot into it instead of allocating ~155 MB of fresh  */
/* memory, which the kernel would zero and map page by page.           */
/* ------------------------------------------------------------------ */
#define TSZ(m) (((size_t)1 << (m)->tbits) * 16)
#define RSZ(m) (((size_t)1 << ((m)->tbits - 1)) * 4)
#define BSZ(m) ((size_t)(m)->bufmask + 1)
#define HSZ(m) (sizeof(U32) * ((size_t)1 << (m)->htbits))
#define WSZ(m, k) (sizeof(short) * NIN * (m)->nsel[k])
#define W2SZ (sizeof(I32) * (NM1 + 1) * 8 * 2)
#define ASZ (sizeof(U16) * 65536 * 33)

/* allocate buffers of the sizes s uses (contents undefined); on failure the caller frees d */
static int model_alloc_like(Model *d, const Model *s) {
  memset(d, 0, sizeof(Model));
  d->tbits = s->tbits; d->bufmask = s->bufmask; d->htbits = s->htbits;
  memcpy(d->nsel, s->nsel, sizeof(d->nsel));
  for (int i = 0; i < NC; i++) {
    if (!(d->T[i] = (U8 *)malloc(TSZ(s))) || !(d->R[i] = (U32 *)malloc(RSZ(s)))) return -1;
  }
  if (!(d->buf = (U8 *)malloc(BSZ(s))) || !(d->ht = (U32 *)malloc(HSZ(s)))) return -1;
  for (int k = 0; k < NM1; k++) if (!(d->w1[k] = (short *)aligned_alloc(64, WSZ(s, k)))) return -1;
  if (!(d->w2 = (I32 *)malloc(W2SZ)) || !(d->a1.t = (U16 *)malloc(ASZ)) || !(d->a2.t = (U16 *)malloc(ASZ))) return -1;
  return 0;
}

static int model_same_sizes(const Model *a, const Model *b) {
  return a->tbits == b->tbits && a->bufmask == b->bufmask && a->htbits == b->htbits;
}

/* make d an exact copy of s; d's buffers have s's sizes (model_alloc_like). With a log, d was an
   exact copy of s before it coded one file, and the log holds every large-table entry that file
   may have changed: only those (and the match buffer bytes it appended) are copied back. */
static void model_copy(Model *d, const Model *s, const U32 *log, const U32 *log_end) {
  Model keep;
  memcpy(&keep, d, sizeof(Model));
  memcpy(d, s, sizeof(Model));
  for (int i = 0; i < NC; i++) {
    d->T[i] = keep.T[i];
    d->R[i] = keep.R[i];
    d->cp[i] = d->T[i] + (s->cp[i] - s->T[i]);
    d->sp[i] = d->T[i] + (s->sp[i] - s->T[i]);
    d->rp[i] = d->R[i] + (s->rp[i] - s->R[i]);
    d->sme[i] = d->smt[i] + (s->sme[i] - s->smt[i]);
    d->rsme[i] = d->rsmt[i] + (s->rsme[i] - s->rsmt[i]);
  }
  d->o0e = d->o0t + (s->o0e - s->o0t); d->mme = d->mmt + (s->mme - s->mmt);
  d->buf = keep.buf; d->ht = keep.ht; d->w2 = keep.w2; d->a1.t = keep.a1.t; d->a2.t = keep.a2.t;
  for (int k = 0; k < NM1; k++) memcpy(d->w1[k] = keep.w1[k], s->w1[k], WSZ(s, k));
  memcpy(d->w2, s->w2, W2SZ);
  if (!log) {
    for (int i = 0; i < NC; i++) { memcpy(d->T[i], s->T[i], TSZ(s)); memcpy(d->R[i], s->R[i], RSZ(s)); }
    memcpy(d->buf, s->buf, BSZ(s));
    memcpy(d->ht, s->ht, HSZ(s));
    memcpy(d->a1.t, s->a1.t, ASZ);
    memcpy(d->a2.t, s->a2.t, ASZ);
  } else {
    for (const U32 *e = log; e < log_end; e++) {
      U32 v = *e, i = (v >> 24) & 7, idx = v & 0xffffff;
      switch (v >> 29) {
        case LOG_T: memcpy(d->T[i] + ((size_t)idx << 4), s->T[i] + ((size_t)idx << 4), 16); break;
        case LOG_R: d->R[i][idx] = s->R[i][idx]; break;
        case LOG_HT: d->ht[idx] = s->ht[idx]; break;
        case LOG_A1: d->a1.t[idx] = s->a1.t[idx]; break;
        default: d->a2.t[idx] = s->a2.t[idx]; break;
      }
    }
    U32 n = keep.pos - s->pos; /* bytes the file appended to the match buffer */
    if (n > s->bufmask) memcpy(d->buf, s->buf, BSZ(s));
    else for (U32 p = s->pos; p != keep.pos; p++) d->buf[p & s->bufmask] = s->buf[p & s->bufmask];
  }
#ifdef MZ_CHECK_RESTORE
  for (int i = 0; i < NC; i++)
    if (memcmp(d->T[i], s->T[i], TSZ(s)) || memcmp(d->R[i], s->R[i], RSZ(s))) abort();
  if (memcmp(d->buf, s->buf, BSZ(s)) || memcmp(d->ht, s->ht, HSZ(s)) || memcmp(d->a1.t, s->a1.t, ASZ) ||
      memcmp(d->a2.t, s->a2.t, ASZ)) abort();
#endif
}

typedef struct { Model m; int maxbits; U64 serial; } Snapshot;
/* src: the snapshot the worker's model was last loaded from (its serial), 0 if none or if its log
   overflowed; the log then holds what the last file changed */
#define LOG_CAP (1u << 20) /* entries: ~41 per byte, so files up to ~25 KB restore from the log;
                              above that a full copy is as fast (measured) */
typedef struct { Model m; int ready; U64 src; U32 *log; } Worker;
static U64 snap_serial;

EXPORT void *cm_prime(const U8 *primer, size_t plen, int profile, int stride, int bpp, int maxbits,
                      unsigned long long total_hint) {
  Snapshot *s = (Snapshot *)malloc(sizeof(Snapshot));
  if (!s) return NULL;
  if (model_init(&s->m, profile, stride, bpp, maxbits, total_hint)) { model_free(&s->m); free(s); return NULL; }
  s->maxbits = maxbits;
  s->serial = __atomic_add_fetch(&snap_serial, 1, __ATOMIC_RELAXED);
  model_start(&s->m);
  model_prime(&s->m, primer, plen);
  return s;
}
EXPORT void cm_free_primed(void *p) { if (p) { model_free(&((Snapshot *)p)->m); free(p); } }

EXPORT void *cm_worker_new(void) { return calloc(1, sizeof(Worker)); }
EXPORT void cm_worker_free(void *p) { if (p) { model_free(&((Worker *)p)->m); free(((Worker *)p)->log); free(p); } }

/* the worker's model as a copy of the snapshot, reallocated only when the sizes differ; restored
   from the log when the last file used the same snapshot and fitted in the log */
static Model *worker_load(Worker *w, const Snapshot *sn) {
  const Model *s = &sn->m;
  if (w->ready && !model_same_sizes(&w->m, s)) { model_free(&w->m); w->ready = 0; }
  if (!w->ready) {
    if (model_alloc_like(&w->m, s)) { model_free(&w->m); memset(&w->m, 0, sizeof(Model)); return NULL; }
    w->ready = 1;
    w->src = 0;
  }
  if (!w->log && !(w->log = (U32 *)malloc(sizeof(U32) * LOG_CAP))) return NULL;
  int partial = w->src == sn->serial && w->m.lp != NULL;
  model_copy(&w->m, s, partial ? w->log : NULL, partial ? w->m.lp : NULL);
  w->src = sn->serial;
  /* log the entries the snapshot's state points at: the first bits write into them */
  Model *m = &w->m;
  m->lp = w->log; m->lend = w->log + LOG_CAP;
  for (int i = 0; i < NC; i++) {
    log_w(m, 1, LOG_T, (U32)i, (U32)((m->cp[i] - m->T[i]) >> 4));
    log_w(m, 1, LOG_R, (U32)i, (U32)(m->rp[i] - m->R[i]));
  }
  return m;
}

/* packed size class so the caller can pick a compatible snapshot */
EXPORT unsigned int cm_size_class(unsigned long long total, int maxbits) {
  U32 tb, bb, hb;
  model_sizes(total, maxbits, &tb, &bb, &hb);
  return tb | (bb << 8) | (hb << 16);
}

static int snap_fits(const Snapshot *s, U64 total) {
  U32 tb, bb, hb;
  model_sizes(total, s->maxbits, &tb, &bb, &hb);
  return tb == s->m.tbits && bb == ilog2u((U64)s->m.bufmask + 1) && hb == s->m.htbits;
}

/* worker: kept between calls by the caller (one per thread), or NULL for a temporary one */
EXPORT long long cm_compress_primed(void *snap, void *worker, const U8 *in, size_t n, size_t plen,
                                    U8 *out, size_t outcap) {
  Snapshot *s = (Snapshot *)snap;
  if (!snap_fits(s, (U64)n + plen)) return -3;
  Worker *w = worker ? (Worker *)worker : (Worker *)cm_worker_new();
  Model *m = w ? worker_load(w, s) : NULL;
  if (!m) { if (!worker) cm_worker_free(w); return -1; }
  Coder c = {0, 0xffffffffu, 0, out, 0, outcap, NULL, 0, 0, 0};
  for (size_t i = 0; i < n; i++) {
    U8 ch = in[i];
    if (m->lp) /* logged until the log is full, then the plain update */
      for (int b = 7; b >= 0; b--) { int y = (ch >> b) & 1; enc_bit(&c, y, m->pr); model_update_log(m, y); }
    else
      for (int b = 7; b >= 0; b--) { int y = (ch >> b) & 1; enc_bit(&c, y, m->pr); model_update(m, y); }
    if (c.overflow) break;
  }
  for (int i = 0; i < 4; i++) {
    if (c.outpos < c.outcap) c.out[c.outpos++] = (U8)(c.x1 >> 24);
    else c.overflow = 1;
    c.x1 <<= 8;
  }
  if (!worker) cm_worker_free(w);
  return c.overflow ? -2 : (long long)c.outpos;
}

EXPORT int cm_decompress_primed_part(void *snap, void *worker, const U8 *in, size_t inlen, size_t plen, U8 *out,
                                     size_t n, size_t total) {
  Snapshot *s = (Snapshot *)snap;
  if (n > total) return -1;
  if (!snap_fits(s, (U64)total + plen)) return -3;
  Worker *w = worker ? (Worker *)worker : (Worker *)cm_worker_new();
  Model *m = w ? worker_load(w, s) : NULL;
  if (!m) { if (!worker) cm_worker_free(w); return -1; }
  Coder c = {0, 0xffffffffu, 0, NULL, 0, 0, in, 0, inlen, 0};
  for (int i = 0; i < 4; i++) c.x = (c.x << 8) | (c.inpos < c.inlen ? c.in[c.inpos++] : 0);
  for (size_t i = 0; i < n; i++) {
    int ch = 0;
    if (m->lp)
      for (int b = 0; b < 8; b++) { int y = dec_bit(&c, m->pr); model_update_log(m, y); ch = (ch << 1) | y; }
    else
      for (int b = 0; b < 8; b++) { int y = dec_bit(&c, m->pr); model_update(m, y); ch = (ch << 1) | y; }
    out[i] = (U8)ch;
  }
  if (!worker) cm_worker_free(w);
  return 0;
}
EXPORT int cm_decompress_primed(void *snap, void *worker, const U8 *in, size_t inlen, size_t plen, U8 *out, size_t n) {
  return cm_decompress_primed_part(snap, worker, in, inlen, plen, out, n, n);
}
