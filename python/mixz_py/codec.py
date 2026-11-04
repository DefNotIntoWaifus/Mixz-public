"""The context-mixing codec: a Python port of engine/cm.c (codec 3).

The codec turns a stream of bytes into a much shorter stream and back. It works one bit at a time:

  1. Models predict the next bit. Each one gives a probability that the bit is 1:
     - 8 context models. Each looks at a different context (for example the last 3 bytes, or the
       current word), hashes it to a slot in a table, and finds a small "bit history" state there:
       roughly how many 0s and 1s followed this context before. A table learns which probability
       each state stands for.
     - 8 run models: for each context, the last byte that followed it and how often it repeated.
     - An order-0 model, which sees only the bits of the current byte so far.
     - A match model: it finds the longest earlier copy of the last few bytes and predicts that the
       copy continues.
  2. Mixers, small neural networks, combine the predictions. They work on "stretched"
     probabilities, st = ln(p / (1 - p)), and learn after every bit how much to trust each model.
     4 first-layer mixers each pick their weights by a different context; a final mixer combines
     the 4 results.
  3. Two APM tables (also called SSE) correct the final probability, given a small context.
  4. An arithmetic coder writes the bit. A bit predicted with probability p costs about -log2(p)
     bits of output, so good predictions give short output.
  5. Every model learns from the bit, and the next bit is predicted.

The decoder runs exactly the same model. It makes the same predictions, so the arithmetic decoder can
recover each bit from the coded stream.

Everything is integer arithmetic, as in the C code, so this file produces exactly the bytes the C
engine produces. Where C relies on 32-bit unsigned numbers wrapping around, this file masks with M32.
Probabilities are 12-bit (0..4095 means 0..1) inside the model and 16-bit at the coder; stretched
values are in 1/256 units, clamped to -2047..2047.

It is about 400 times slower than the C engine: about 1.4 KB per second on one core (measured).
"""
from array import array

M32 = 0xFFFFFFFF

# Fixed parameters. Changing any of them changes the output, so it would be a new codec.
LR1 = 2          # first-layer mixer learning rate
LR2 = 1          # final mixer learning rate
WINIT = 8192     # first-layer start weight (1.0 = 65536 before the 16-bit scaling)
SHIFT2 = 12      # final mixer update shift
SMLIM = 1023     # limit of a context model's adaptation count
APMRATE = 6      # APM learning rate (1/64 per bit)
WS = 13          # first-layer weights are 16-bit, 1.0 = 1 << WS
FW0, FW1, FW2 = 1, 1, 2  # weights of the mixer output and the two APM outputs in the final average
WMAX = 1 << 19   # final mixer weights are clamped to +-8.0 (16.16 fixed point)

NC = 8           # context models
NIN = 32         # mixer inputs: 3 per context model, order 0, 2 for the match model, bias; padded to 32
NM1 = 4          # first-layer mixers
MINLEN = 6       # match model: shortest match used (all profiles but image)
MINLEN_IMAGE = 2

# Engine profiles: which contexts the 8 context models use.
PROF_TEXT, PROF_BIN, PROF_IMAGE, PROF_CODE, PROF_WEB = 0, 1, 2, 3, 4


# ----------------------------------------------------------------------------------------------
# squash and stretch: the logistic function and its inverse
# ----------------------------------------------------------------------------------------------
SQ_T = [1, 2, 4, 6, 10, 17, 27, 45, 74, 120, 194, 311, 488, 747, 1102, 1546, 2048, 2550, 2994, 3349,
        3608, 3785, 3902, 3976, 4022, 4051, 4069, 4079, 4086, 4090, 4092, 4094, 4095]


def squash(d: int) -> int:
    """1 / (1 + e^-d), by interpolation in a 33-entry table: d in 1/256 units, result 1..4095."""
    if d > 2047:
        d = 2047
    if d < -2047:
        d = -2047
    w = d & 127
    i = (d >> 7) + 16
    return (SQ_T[i] * (128 - w) + SQ_T[i + 1] * w + 64) >> 7


# STRETCH[p] = the smallest d with squash(d) >= p: the inverse of squash, for p in 0..4095.
STRETCH = [0] * 4096
_pi = 0
for _x in range(-2047, 2048):
    _v = squash(_x)
    for _i in range(_pi, _v + 1):
        STRETCH[_i] = _x
    _pi = _v + 1
for _i in range(_pi, 4096):
    STRETCH[_i] = 2047
SQT = [squash(d) for d in range(-2047, 2048)]  # SQT[d + 2047] = squash(d)
DT = [131072 // (2 * i + 3) for i in range(1024)]  # 65536 / (i + 1.5): the step size after i updates


# ----------------------------------------------------------------------------------------------
# Bit-history states. A state stands for a pair (n0, n1): bounded counts of the 0s and 1s seen in a
# context. When a bit arrives, its count goes up and a large opposite count is cut down, so a state
# follows recent behaviour. The states are numbered by a breadth-first walk from (0, 0).
# ----------------------------------------------------------------------------------------------
MAXSTATES = 256
_MAXFOR = [40, 30, 24, 16, 10, 8, 7, 7]  # largest count allowed beside a small one


def _allowed(a: int, b: int) -> bool:
    mn, mx = min(a, b), max(a, b)
    return mn < 8 and mx <= _MAXFOR[mn]


def _discount(x: int) -> int:
    return (x >= 1) + (x >= 2) + (x >= 3) + (x >= 4) + (x >= 5) + (x >= 7) + (x >= 8)


def _after(a: int, b: int, y: int):
    """The counts after seeing bit y."""
    if y:
        b += 1
        if a > 2:
            a = _discount(a)
    else:
        a += 1
        if b > 2:
            b = _discount(b)
    while not _allowed(a, b):
        if a > b:
            a -= 1
        else:
            b -= 1
    return a, b


def _build_states():
    index = {(0, 0): 0}
    order = [(0, 0)]
    head = 0
    while head < len(order):
        a, b = order[head]
        head += 1
        for y in (0, 1):
            nxt = _after(a, b, y)
            if nxt not in index:
                index[nxt] = len(order)
                order.append(nxt)
    nex = [[0, 0] for _ in range(MAXSTATES)]
    n0, n1 = [0] * MAXSTATES, [0] * MAXSTATES
    for s, (a, b) in enumerate(order):
        n0[s], n1[s] = a, b
        nex[s] = [index[_after(a, b, 0)], index[_after(a, b, 1)]]
    # DETF: the state has seen only 0s or only 1s (a "deterministic" context); STN: n0 + n1
    detf = [int(s != 0 and (n0[s] == 0 or n1[s] == 0)) for s in range(MAXSTATES)]
    stn = [n0[s] + n1[s] for s in range(MAXSTATES)]
    return len(order), nex, n0, n1, detf, stn


NSTATES, NEX, ST_N0, ST_N1, DETF, STN = _build_states()


# ----------------------------------------------------------------------------------------------
# StateMap: a table entry that turns a context (or a state) into an adaptive probability. Each entry
# is 32 bits: a 22-bit probability and a 10-bit count of updates. The step size shrinks as the count
# grows (1/1.5, 1/2.5, ...), until the count reaches its limit.
# ----------------------------------------------------------------------------------------------
def sm_table(n: int) -> list:
    return [1 << 31] * n  # p = 0.5, count 0


def sm_table_states() -> list:
    """A StateMap over the 256 states, starting from each state's counts: p = (n1 + 0.5) / (n + 1)."""
    t = sm_table(256)
    for s in range(min(NSTATES, 256)):
        a, b = ST_N0[s], ST_N1[s]
        p22 = ((2 * b + 1) << 22) // (2 * (a + b) + 2)
        t[s] = min(p22, (1 << 22) - 1) << 10
    return t


def sm_update(t: list, i: int, y: int, limit: int) -> None:
    e = t[i]
    n = e & 1023
    p = e >> 10
    target = (1 << 22) - 1 if y else 0
    d = ((target - p) * DT[n]) >> 16  # p + d stays inside 0..2^22-1, so only the p field changes
    t[i] = (e + (d << 10) + (n < limit)) & M32


# ----------------------------------------------------------------------------------------------
# APM (adaptive probability map, also called SSE): for each small context, 33 points along the
# stretched probability axis, each holding a corrected 16-bit probability. The output interpolates
# between the two points around the input; the update moves the nearer one.
# ----------------------------------------------------------------------------------------------
_APM_ROW = [min(squash((j - 16) * 128) * 16, 65535) for j in range(33)]


def apm_table(n: int) -> array:
    return array("H", _APM_ROW) * n


# ----------------------------------------------------------------------------------------------
# Hashing (32-bit)
# ----------------------------------------------------------------------------------------------
def hmix(h: int) -> int:
    h ^= h >> 16
    h = (h * 0x7FEB352D) & M32
    h ^= h >> 15
    h = (h * 0x846CA68B) & M32
    h ^= h >> 16
    return h


def H2(a: int, b: int) -> int:
    return hmix((a * 0x9E3779B1 + hmix((b + 0x632BE5AB) & M32)) & M32)


def H3(a: int, b: int, c: int) -> int:
    return H2(H2(a, b), c)


def H4(a: int, b: int, c: int, d: int) -> int:
    return H2(H3(a, b, c), d)


def _ilog2(x: int) -> int:
    return max(x.bit_length() - 1, 0)


def model_sizes(total: int, maxbits: int):
    """Table sizes (as powers of 2) from the number of bytes the model will see. They depend only on
    (total, maxbits), so the encoder and the decoder agree."""
    t = _ilog2(total * 2 + 1) + 1  # context tables: about 2 buckets touched per byte per context
    maxbits = min(max(maxbits, 16), 24)
    t = min(max(t, 16), maxbits)
    b = min(max(_ilog2(total + 1) + 1, 16), 28)  # match model history buffer
    h = min(max(b - 2, 16), 24)  # match model hash table
    return t, b, h


_ZERO16 = bytes(16)


class Model:
    """The whole model. predict() sets self.pr, the 16-bit probability that the next bit is 1;
    update(y) learns from the actual bit y and predicts the next one."""

    def __init__(self, profile: int, stride: int, bpp: int, maxbits: int, total: int):
        if not PROF_TEXT <= profile <= PROF_WEB:
            raise ValueError("unknown engine profile")
        self.profile = profile
        self.minlen = MINLEN_IMAGE if profile == PROF_IMAGE else MINLEN
        self.stride = stride if stride > 0 else 1  # image: bytes per row
        self.bpp = bpp if bpp > 0 else 1  # image: bytes per pixel
        tb, bb, hb = model_sizes(total, maxbits)
        self.tbits = tb
        # Context tables: buckets of 16 bytes, [checksum, 15 states]. A bucket holds the states of one
        # half-byte: 15 nodes of a binary tree over its 4 bits (node 1, then node * 2 + bit).
        self.T = [bytearray(16 << tb) for _ in range(NC)]
        self.cp = [0] * NC  # current bucket of each context (offset into T[i])
        self.sp = [0] * NC  # this bit's slot (cp + node)
        self.ss = [0] * NC  # and its state
        self.h = [0] * NC  # context hashes for this byte
        self.nh = [0] * NC  # bucket hashes for the second half of the byte
        self.smt = [sm_table_states() for _ in range(NC)]  # state -> probability, per context
        self.sme = [0] * NC
        # Run model: per (context hash) the last byte seen and its repeat count, packed in 32 bits as
        # [16-bit checksum | byte | count]
        self.R = [array("I", bytes(4 << (tb - 1))) for _ in range(NC)]
        self.rp = [0] * NC
        self.rh = [0] * NC
        self.rexp = [-1] * NC  # the bit the run model expects, -1 if none
        self.rbyte = [0] * NC
        self.rq2 = [0] * NC  # 2 * min(count, 15)
        self.rlive = [0] * NC  # every bit of this byte so far matched the run byte
        self.rsmt = [sm_table(64) for _ in range(NC)]
        self.rsme = [0] * NC
        self.o0t = sm_table(256)  # order 0: the partial byte -> probability
        self.o0e = 0
        self.mmt = sm_table(64)  # match model: (length bucket, expected bit) -> probability
        self.mme = 0
        # match model
        self.buf = bytearray(1 << bb)  # every byte seen, as a ring buffer
        self.bufmask = (1 << bb) - 1
        self.pos = 0
        self.htbits = hb
        self.ht = array("I", bytes(4 << hb))  # hash of the last minlen bytes -> position after them
        self.mh = 0
        self.mlen = 0  # length of the current match, 0 if none
        self.mptr = 0  # position of the byte the match predicts
        self.mexp = -1
        self.mq = 0
        # mixers
        self.x = [0] * NIN
        nsel = [256, 32 * 8, 256, 10 * 8 * 2]  # rows of each first-layer mixer: one per selector value
        self.w1 = [[[WINIT >> (16 - WS)] * NIN for _ in range(n)] for n in nsel]
        self.sel = [0] * NM1
        self.d1 = [0] * NM1  # first-layer outputs, stretched
        self.p1 = [0] * NM1  # and squashed
        self.w2 = [[65536 // NM1] * (NM1 + 1) for _ in range(8 * 2)]  # final mixer rows
        self.sel2 = 0
        self.d2 = 0
        self.pm = 2048  # mixer output probability, 12-bit
        self.a1, self.a2 = apm_table(65536), apm_table(65536)
        self.a1i = self.a2i = 0  # APM entries to update
        self.pa1 = self.pa2 = 0
        # byte state
        self.c0 = 1  # the bits of the current byte so far, with a leading 1
        self.c4 = 0  # the last 4 bytes
        self.c8 = 0  # the 4 before them
        self.bits = 0  # bits of the current byte seen
        self.node = 1  # node in the current bucket
        self.word = self.word1 = 0  # hashes of the current and the previous word
        self.line_start = self.prev_line_start = 0
        self.img_pos = self.img_ch = 0  # image: bytes since the start, and that count modulo bpp
        # code and web profiles: indentation of the line, whether the line is still in it, the line's
        # first other byte; bracket depth and the last 4 open brackets; HTML tag name hash and where
        # we are in a tag (1 name, 2 after it); the last punctuation byte
        self.indent, self.inlead, self.lfirst = 0, 1, 0
        self.depth = self.bstack = self.tag = self.intag = self.lastp = 0
        self.ahi = self.ahash = 0  # per-byte parts of the two APM contexts
        self.pr = 32768
        self.compute_contexts()
        self.apm_contexts()
        self.run_hashes()
        for i in range(NC):
            self.cp[i] = self.bucket_find(i, self.h[i])
        self.run_lookup()
        self.predict()

    # ------------------------------------------------------------------------------------------
    # contexts
    # ------------------------------------------------------------------------------------------
    def hist(self, back: int) -> int:
        """The byte `back` positions ago (1 = the last byte), 0 before the start."""
        if back == 0 or back > self.pos:
            return 0
        return self.buf[(self.pos - back) & self.bufmask]

    def bucket_find(self, i: int, hh: int) -> int:
        """The bucket for hash hh in context table i: one of two neighbours whose checksum matches,
        else the one with the less used first state, cleared."""
        t = self.T[i]
        idx = hh >> (32 - self.tbits)
        chk = hh & 255
        b0 = idx << 4
        if t[b0] == chk:
            return b0
        b1 = (idx ^ 1) << 4
        if t[b1] == chk:
            return b1
        r = b0 if STN[t[b0 + 1]] <= STN[t[b1 + 1]] else b1
        t[r:r + 16] = _ZERO16
        t[r] = chk
        return r

    def run_hashes(self) -> None:
        for i in range(NC):
            self.rh[i] = hmix(self.h[i] ^ 0xA5A5A5A5)

    def run_lookup(self) -> None:
        for i in range(NC):
            hh = self.rh[i]
            R = self.R[i]
            r = hh >> (32 - (self.tbits - 1))
            chk = hh & 0xFFFF
            if (R[r] >> 16) != chk:
                R[r] = chk << 16  # a new context: byte 0, count 0
            self.rp[i] = r
            v = R[r]
            cnt = v & 255
            self.rbyte[i] = (v >> 8) & 255
            self.rq2[i] = min(cnt, 15) * 2
            self.rlive[i] = int(cnt != 0)

    def run_update(self, c: int) -> None:
        for i in range(NC):
            R, r = self.R[i], self.rp[i]
            v = R[r]
            cnt, b = v & 255, (v >> 8) & 255
            if cnt and b == c:
                if cnt < 255:
                    cnt += 1
            else:
                b, cnt = c, 1
            R[r] = (v & 0xFFFF0000) | (b << 8) | cnt

    def compute_contexts(self) -> None:
        """The 8 context hashes for the next byte, by engine profile."""
        c4, c8 = self.c4, self.c8
        c1 = c4 & 255
        h = self.h
        if self.profile == PROF_IMAGE:
            b, s = self.bpp, self.stride
            hist = self.hist
            W, N, NW = hist(b), hist(s), hist(s + b)  # the pixel to the left, above, above-left
            NE = hist(s - b if s > b else 0)  # above-right
            WW, NN = hist(2 * b), hist(2 * s)
            ch = self.img_ch  # which channel (R, G, B, ...)
            grad = min(max(W + N - NW, 0), 255)
            ew = min(max(2 * W - WW, 0), 255)
            en = min(max(2 * N - NN, 0), 255)
            h[0] = H3(1, W, ch)
            h[1] = H3(2, N, ch)
            h[2] = H4(3, W, N, ch)
            h[3] = H4(4, W, N, NW)
            h[4] = H3(5, grad, ch)
            h[5] = H4(6, N, NE, ch)
            h[6] = H4(7, c1, W, ch)
            h[7] = H4(8, ew, en, ch)
        elif self.profile == PROF_BIN:
            h[0] = H2(1, c1)
            h[1] = H2(2, c4 & 0xFFFF)
            h[2] = H2(3, c4 & 0xFFFFFF)
            h[3] = H2(4, c4)
            h[4] = H3(5, c4, c8 & 0xFFFF)
            h[5] = H2(6, c4 & 0xFFFF00)
            h[6] = H2(7, c4 & 0xFF00FF)
            h[7] = H4(8, c1, c4 >> 24, self.pos & 3)
        else:  # text, code, web
            col = self.pos - self.line_start
            above = 0  # the byte at the same column in the previous line
            if self.prev_line_start + col < self.line_start:
                above = self.hist(self.pos - (self.prev_line_start + col))
            h[0] = H2(1, c1)
            h[1] = H2(2, c4 & 0xFFFF)
            h[2] = H2(3, c4 & 0xFFFFFF)
            h[3] = H2(4, c4)
            h[4] = H3(5, c4, c8 & 0xFFFF)
            h[5] = H3(6, self.word, 0 if self.word else c1)
            h[6] = H3(7, self.word, self.word1)
            h[7] = H4(8, above, c1, min(col, 32))
            if self.profile == PROF_CODE:  # indentation, last punctuation, bracket nesting
                h[0] = H4(9, min(self.indent, 31) | (self.inlead << 8), self.lfirst, c1)
                h[1] = H3(12, self.lastp, c4 & 0xFFFF)
                h[6] = H4(10, min(self.depth, 15), self.bstack & 0xFFFF, c4 & 0xFFFF)
            elif self.profile == PROF_WEB:  # bracket nesting, HTML tag
                h[1] = H4(10, min(self.depth, 15), self.bstack & 0xFFFF, c4 & 0xFFFF)
                h[7] = H4(11, self.tag, self.intag, c4 & 0xFFFF)

    def apm_contexts(self) -> None:
        if self.profile == PROF_IMAGE:
            W, N = self.hist(self.bpp), self.hist(self.stride)
            self.ahi = W << 8
            self.ahash = H2(W, N) & 0xFF00
        else:
            self.ahi = (self.c4 & 255) << 8
            self.ahash = H2(self.c4 & 0xFFFF, 9) & 0xFF00

    # ------------------------------------------------------------------------------------------
    # prediction
    # ------------------------------------------------------------------------------------------
    def predict(self) -> None:
        x = self.x
        bits = self.bits
        k = known = 0
        for i in range(NC):
            slot = self.cp[i] + self.node
            self.sp[i] = slot
            s = self.T[i][slot]
            self.ss[i] = s
            known += s != 0
            self.sme[i] = s
            st = STRETCH[self.smt[i][s] >> 20]
            x[k] = st
            x[k + 1] = st if DETF[s] else 0  # the same input again when the context was deterministic
            # run model: live while every bit of this byte so far matched the run byte
            if self.rlive[i]:
                e = (self.rbyte[i] >> (7 - bits)) & 1
                self.rexp[i] = e
                j = self.rsme[i] = self.rq2[i] + e
                x[k + 2] = STRETCH[self.rsmt[i][j] >> 20]
            else:
                self.rexp[i] = -1
                x[k + 2] = 0
            k += 3
        self.o0e = self.c0
        x[24] = STRETCH[self.o0t[self.c0] >> 20]
        # match model: the predicted byte must agree with the bits of this byte so far
        self.mexp, self.mq = -1, 0
        if self.mlen > 0:
            pb = self.buf[self.mptr & self.bufmask] | 256
            if (pb >> (8 - bits)) == self.c0:
                self.mexp = (pb >> (7 - bits)) & 1
                ml = self.mlen
                self.mq = (ml if ml < 16 else 16 + ((ml - 16) >> 2) if ml < 32 else 20 + ((ml - 32) >> 3) if ml < 64
                           else 24 + ((ml - 64) >> 4) if ml < 128 else 28 + ((ml - 128) >> 6) if ml < 256 else 31)
            else:
                self.mlen = 0
        if self.mexp >= 0:
            self.mme = self.mq * 2 + self.mexp
            x[25] = STRETCH[self.mmt[self.mme] >> 20]
            conf = min(self.mlen, 32) * 64
            x[26] = conf if self.mexp else -conf
        else:
            x[25] = x[26] = 0
        x[27] = 256  # bias; x[28..31] stay 0
        self.nz = [i for i in range(28) if x[i]]  # the inputs that are not 0: only their weights learn

        # layer 1: four mixers, each picking its row of weights by a different context
        sel = self.sel
        sel[0] = self.c0
        sel[1] = (min(self.mq + 1, 31) if self.mexp >= 0 else 0) * 8 + bits
        sel[2] = self.hist(self.bpp) if self.profile == PROF_IMAGE else self.c4 & 255
        sel[3] = (known * 8 + bits) * 2 + (self.rexp[3] >= 0)
        for j in range(NM1):
            w = self.w1[j][sel[j]]
            # products are summed in pairs, each pair sum shifted down by 8 (as the AVX2 multiply-add
            # of the C code does it); inputs 28..31 are 0 and add nothing
            r = 0
            for i in range(0, 28, 2):
                r += (x[i] * w[i] + x[i + 1] * w[i + 1]) >> 8
            d = r >> (WS - 8)
            d = 2047 if d > 2047 else -2047 if d < -2047 else d
            self.d1[j] = d
            self.p1[j] = SQT[d + 2047]
        # layer 2
        d1 = self.d1
        self.sel2 = bits * 2 + (self.mexp >= 0)
        w = self.w2[self.sel2]
        s = ((d1[0] * w[0]) >> 8) + ((d1[1] * w[1]) >> 8) + ((d1[2] * w[2]) >> 8) + ((d1[3] * w[3]) >> 8) \
            + ((256 * w[4]) >> 8)
        s >>= 8
        self.d2 = 2047 if s > 2047 else -2047 if s < -2047 else s
        self.pm = SQT[self.d2 + 2047]

        # SSE: two APMs, one by the previous byte, one by a hash of the previous two bytes
        ca = self.c0 | self.ahi
        cb = (self.c0 ^ self.ahash) & 0xFFFF
        sp = STRETCH[self.pm] + 2048
        lo, wt = sp >> 7, sp & 127
        base = ca * 33 + lo
        self.a1i = base + (wt >> 6)
        self.pa1 = (self.a1[base] * (128 - wt) + self.a1[base + 1] * wt) >> 7
        base = cb * 33 + lo
        self.a2i = base + (wt >> 6)
        self.pa2 = (self.a2[base] * (128 - wt) + self.a2[base + 1] * wt) >> 7
        p = (self.pm * 16 * FW0 + self.pa1 * FW1 + self.pa2 * FW2) // (FW0 + FW1 + FW2)
        self.pr = 32 if p < 32 else 65535 - 32 if p > 65535 - 32 else p

    # ------------------------------------------------------------------------------------------
    # learning
    # ------------------------------------------------------------------------------------------
    def byte_update(self, c: int) -> None:
        """After a whole byte: history, words, lines, brackets and tags, the match model."""
        self.buf[self.pos & self.bufmask] = c
        self.pos += 1
        self.img_pos += 1
        self.img_ch += 1
        if self.img_ch == self.bpp:
            self.img_ch = 0
        self.c8 = ((self.c8 << 8) | (self.c4 >> 24)) & M32
        self.c4 = ((self.c4 << 8) | c) & M32
        lc = c + 32 if 65 <= c <= 90 else c  # lower case
        letter = 97 <= lc <= 122
        digit = 48 <= c <= 57
        if letter or c >= 128 or digit or c == 95:  # a word: letters (case-folded), digits, _
            self.word = ((self.word + lc + 1) * 0x2F0F3D2B) & M32
        elif self.word:
            self.word1 = self.word
            self.word = 0
        if c == 10:
            self.prev_line_start = self.line_start
            self.line_start = self.pos
        if self.profile >= PROF_CODE:
            if c == 10:
                self.indent, self.inlead, self.lfirst = 0, 1, 0
            elif self.inlead and c in (32, 9):
                self.indent += 4 if c == 9 else 1
            elif self.inlead:
                self.inlead, self.lfirst = 0, c
            if c in (40, 91, 123):  # ( [ {
                self.depth += 1
                self.bstack = ((self.bstack << 8) | c) & M32
            elif c in (41, 93, 125) and self.depth:  # ) ] }
                self.depth -= 1
                self.bstack >>= 8
            if c == 60:  # <
                self.intag, self.tag = 1, 0
            elif c == 62:  # >
                self.intag = 0
            elif self.intag == 1:
                if letter or digit or c in (47, 33, 58):  # / ! :
                    self.tag = ((self.tag + lc + 1) * 0x2F0F3D2B) & M32
                else:
                    self.intag = 2
            if not (letter or digit or c in (32, 10, 9) or c >= 128):
                self.lastp = c
        # match model: extend the current match, and hash the last minlen bytes
        if self.mlen > 0:
            if self.buf[self.mptr & self.bufmask] == c:
                self.mlen = min(self.mlen + 1, 65535)
                self.mptr += 1
            else:
                self.mlen = 0
        if self.pos >= self.minlen:
            hh = 0
            for i in range(1, self.minlen + 1):
                hh = ((hh + self.buf[(self.pos - i) & self.bufmask] + 1) * 0x2F0F3D2B) & M32
            self.mh = hmix(hh) >> (32 - self.htbits)

    def match_find(self) -> None:
        """Look for a new match when there is none, and record this position."""
        if self.pos >= self.minlen:
            if self.mlen == 0:
                cand = self.ht[self.mh]
                if cand > 0 and self.pos - cand < self.bufmask:
                    buf, mask, pos = self.buf, self.bufmask, self.pos
                    n = 0  # how many bytes before cand equal the bytes before pos
                    while n < 400 and n < cand and buf[(cand - n - 1) & mask] == buf[(pos - n - 1) & mask]:
                        n += 1
                    if n >= self.minlen:
                        self.mlen, self.mptr = n, cand
            self.ht[self.mh] = self.pos

    def update(self, y: int) -> None:
        """Learn from bit y, then predict the next bit."""
        c0n = (self.c0 << 1) | y
        if self.bits == 7:  # the byte is complete: new contexts for the next one
            self.run_update(c0n & 255)
            self.byte_update(c0n & 255)
            self.compute_contexts()
            self.apm_contexts()
            self.run_hashes()
        elif self.bits == 3:  # half the byte: the buckets for the second half
            for i in range(NC):
                self.nh[i] = hmix((self.h[i] + c0n * 0x9E3779B1) & M32)
        # APMs
        g = (y << 16) + (y << APMRATE) - y - y
        for t, i in ((self.a1, self.a1i), (self.a2, self.a2i)):
            v = t[i]
            v += (g - v) >> APMRATE
            t[i] = 0 if v < 0 else 65535 if v > 65535 else v
        # final mixer: w += x * err >> 12
        err = ((y << 12) - self.pm) * LR2
        w = self.w2[self.sel2]
        for i, xi in enumerate(self.d1 + [256]):
            v = w[i] + ((xi * err) >> SHIFT2)
            w[i] = WMAX if v > WMAX else -WMAX if v < -WMAX else v
        # first-layer mixers: w += round(x * err / 2^15), saturating at the 16-bit limits
        x, nz = self.x, self.nz
        for j in range(NM1):
            e = ((y << 12) - self.p1[j]) * LR1 * (1 << (WS - 13))
            w = self.w1[j][self.sel[j]]
            for i in nz:  # an input of 0 adds (0 + 16384) >> 15 = 0: its weight does not change
                v = w[i] + ((x[i] * e + 16384) >> 15)
                w[i] = 32767 if v > 32767 else -32768 if v < -32768 else v
        # context models: next state, and the probabilities that were used
        for i in range(NC):
            self.T[i][self.sp[i]] = NEX[self.ss[i]][y]
            sm_update(self.smt[i], self.sme[i], y, SMLIM)
            if self.rexp[i] >= 0:
                sm_update(self.rsmt[i], self.rsme[i], y, 1023)
            self.rlive[i] = int(self.rexp[i] == y)
        sm_update(self.o0t, self.o0e, y, 255)
        if self.mexp >= 0:
            sm_update(self.mmt, self.mme, y, 1023)

        self.c0 = c0n
        self.bits += 1
        self.node = self.node * 2 + y
        if self.bits == 8:
            self.match_find()
            self.c0, self.bits, self.node = 1, 0, 1
            for i in range(NC):
                self.cp[i] = self.bucket_find(i, self.h[i])
            self.run_lookup()
        elif self.bits == 4:
            self.node = 1
            for i in range(NC):
                self.cp[i] = self.bucket_find(i, self.nh[i])
        self.predict()

    def prime(self, primer: bytes) -> None:
        """Run the primer through the model without output: encoder and decoder both do this."""
        for c in primer:
            for b in range(7, -1, -1):
                self.update((c >> b) & 1)
        self.img_pos = self.img_ch = 0


# ----------------------------------------------------------------------------------------------
# Arithmetic coder. The interval [x1, x2] (32 bits) is split at xmid in proportion to the probability
# of a 1; the bit picks one side. When the top bytes of x1 and x2 agree, that byte is final and is
# written out.
# ----------------------------------------------------------------------------------------------
class Encoder:
    def __init__(self):
        self.x1, self.x2 = 0, M32
        self.out = bytearray()

    def bit(self, y: int, p16: int) -> None:
        xmid = self.x1 + (((self.x2 - self.x1) * p16) >> 16)
        if y:
            self.x2 = xmid
        else:
            self.x1 = xmid + 1
        while ((self.x1 ^ self.x2) & 0xFF000000) == 0:
            self.out.append(self.x2 >> 24)
            self.x1 = (self.x1 << 8) & M32
            self.x2 = ((self.x2 << 8) & M32) | 255

    def finish(self) -> bytes:
        for _ in range(4):
            self.out.append(self.x1 >> 24)
            self.x1 = (self.x1 << 8) & M32
        return bytes(self.out)


class Decoder:
    def __init__(self, data: bytes):
        self.x1, self.x2 = 0, M32
        self.data, self.pos = data, 0
        self.x = 0
        for _ in range(4):
            self.x = (self.x << 8) | self._next()

    def _next(self) -> int:
        if self.pos < len(self.data):
            self.pos += 1
            return self.data[self.pos - 1]
        return 0

    def bit(self, p16: int) -> int:
        xmid = self.x1 + (((self.x2 - self.x1) * p16) >> 16)
        y = int(self.x <= xmid)
        if y:
            self.x2 = xmid
        else:
            self.x1 = xmid + 1
        while ((self.x1 ^ self.x2) & 0xFF000000) == 0:
            self.x1 = (self.x1 << 8) & M32
            self.x2 = ((self.x2 << 8) & M32) | 255
            self.x = ((self.x << 8) & M32) | self._next()
        return y


# ----------------------------------------------------------------------------------------------
# The codec's interface (cm_compress and cm_decompress_part in the C code)
# ----------------------------------------------------------------------------------------------
def compress(data: bytes, primer: bytes, profile: int, stride: int, bpp: int, maxbits: int):
    """The coded stream, or None if it would be larger than the input plus 1/8 plus 4 KB (the C
    engine's output buffer)."""
    cap = len(data) + len(data) // 8 + 4096
    m = Model(profile, stride, bpp, maxbits, len(data) + len(primer))
    if primer:
        m.prime(primer)
    enc = Encoder()
    for c in data:
        for b in range(7, -1, -1):
            y = (c >> b) & 1
            enc.bit(y, m.pr)
            m.update(y)
        if len(enc.out) > cap:
            return None
    out = enc.finish()
    return None if len(out) > cap else out


def decompress(blob: bytes, n: int, primer: bytes, profile: int, stride: int, bpp: int, maxbits: int,
               total: int = 0) -> bytes:
    """The first n bytes of a coded stream of `total` bytes (a bundle member: the model is sized for
    the whole stream, as when it was coded)."""
    total = max(total, n)
    m = Model(profile, stride, bpp, maxbits, total + len(primer))
    if primer:
        m.prime(primer)
    dec = Decoder(blob)
    out = bytearray(n)
    for i in range(n):
        c = 0
        for _ in range(8):
            y = dec.bit(m.pr)
            m.update(y)
            c = (c << 1) | y
        out[i] = c
    return bytes(out)
