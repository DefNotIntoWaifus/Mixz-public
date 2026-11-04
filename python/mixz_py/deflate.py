"""The deflate compressor of zlib 1.3.1, ported to Python (engine/zlib-1.3.1/deflate.c and trees.c).

Why mixz needs its own deflate: to store a .gz or PNG file smaller, mixz decompresses its deflate
data and codes the raw bytes. To give back the original file, it must compress them again to exactly
the same bytes. Different zlib versions (or zlib-ng) can produce different bytes from the same input,
so mixz pins one: zlib 1.3.1 ("deflate implementation 1" in registry.json). This file reproduces its
output byte for byte, for compression levels 1 to 9, every window size and memory level, and every
strategy. Level 0 is refused, as in the C engine: zlib sizes its stored blocks by the caller's
output buffer there, so it is not reproducible. The gzip wrapper (window bits above 15) is not
needed by mixz and is not supported; raw deflate (negative window bits) and the zlib wrapper are.

How deflate works, in short:
  - LZ77: at each position, find the longest earlier copy of the next bytes (up to 258 bytes, at most
    32 KB back) through hash chains of 3-byte strings. A copy is stored as (length, distance), other
    bytes as literals. Levels 4-9 are "lazy": a match is kept only if the next position does not
    start a longer one.
  - Huffman coding: the literals, lengths and distances of a block get prefix codes from their
    frequencies. Each block is written as stored, with the fixed codes, or with its own codes,
    whichever is smallest.

The functions keep zlib's names (fill_window, longest_match, deflate_slow, build_tree, ...) so the
two can be read side by side. The C code's output buffer is unlimited here, which is how mixz calls
zlib (engine/pdeflate.c), so the cases where zlib stops for lack of output space never occur.
"""
import zlib

MIN_MATCH, MAX_MATCH = 3, 258
MIN_LOOKAHEAD = MAX_MATCH + MIN_MATCH + 1
WIN_INIT = MAX_MATCH
TOO_FAR = 4096  # matches of length 3 farther than this are dropped
LENGTH_CODES, LITERALS = 29, 256
L_CODES = LITERALS + 1 + LENGTH_CODES
D_CODES, BL_CODES = 30, 19
HEAP_SIZE = 2 * L_CODES + 1
MAX_BITS, MAX_BL_BITS = 15, 7
END_BLOCK = 256
REP_3_6, REPZ_3_10, REPZ_11_138 = 16, 17, 18
STORED_BLOCK, STATIC_TREES, DYN_TREES = 0, 1, 2
Z_FILTERED, Z_HUFFMAN_ONLY, Z_RLE, Z_FIXED = 1, 2, 3, 4
Z_NO_FLUSH, Z_FINISH = 0, 4
INIT_STATE, BUSY_STATE, FINISH_STATE = 42, 113, 666
NEED_MORE, BLOCK_DONE, FINISH_STARTED, FINISH_DONE = 0, 1, 2, 3
M32 = 0xFFFFFFFF

# per level: good_length, max_lazy, nice_length, max_chain, function
CONFIG = [None, (4, 4, 8, 4, "fast"), (4, 5, 16, 8, "fast"), (4, 6, 32, 32, "fast"),
          (4, 4, 16, 16, "slow"), (8, 16, 32, 32, "slow"), (8, 16, 128, 128, "slow"),
          (8, 32, 128, 256, "slow"), (32, 128, 258, 1024, "slow"), (32, 258, 258, 4096, "slow")]

EXTRA_LBITS = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0]
EXTRA_DBITS = [0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13]
EXTRA_BLBITS = [0] * 16 + [2, 3, 7]
BL_ORDER = [16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15]


def bi_reverse(code: int, length: int) -> int:
    res = 0
    for _ in range(length):
        res = (res << 1) | (code & 1)
        code >>= 1
    return res


def gen_codes(code: list, length: list, max_code: int, bl_count: list) -> None:
    """Canonical prefix codes from the code lengths (bit-reversed, as deflate sends them LSB first)."""
    next_code = [0] * (MAX_BITS + 1)
    c = 0
    for bits in range(1, MAX_BITS + 1):
        c = (c + bl_count[bits - 1]) << 1
        next_code[bits] = c & 0xFFFF
    for n in range(max_code + 1):
        ln = length[n]
        if ln:
            code[n] = bi_reverse(next_code[ln], ln)
            next_code[ln] += 1


# The static tables (tr_static_init in trees.c)
BASE_LENGTH, LENGTH_CODE = [0] * LENGTH_CODES, [0] * 256
_length = 0
for _code in range(LENGTH_CODES - 1):
    BASE_LENGTH[_code] = _length
    for _ in range(1 << EXTRA_LBITS[_code]):
        LENGTH_CODE[_length] = _code
        _length += 1
LENGTH_CODE[255] = LENGTH_CODES - 1  # length 258 uses code 285, not 284 with 5 extra bits
BASE_DIST, DIST_CODE = [0] * D_CODES, [0] * 512
_dist = 0
for _code in range(16):
    BASE_DIST[_code] = _dist
    for _ in range(1 << EXTRA_DBITS[_code]):
        DIST_CODE[_dist] = _code
        _dist += 1
_dist >>= 7  # from here on, distances are divided by 128
for _code in range(16, D_CODES):
    BASE_DIST[_code] = _dist << 7
    for _ in range(1 << (EXTRA_DBITS[_code] - 7)):
        DIST_CODE[256 + _dist] = _code
        _dist += 1
STATIC_LTREE_LEN = [8] * 144 + [9] * 112 + [7] * 24 + [8] * 8
STATIC_LTREE_CODE = [0] * (L_CODES + 2)
_bl_count = [0] * (MAX_BITS + 1)
for _ln in STATIC_LTREE_LEN:
    _bl_count[_ln] += 1
gen_codes(STATIC_LTREE_CODE, STATIC_LTREE_LEN, L_CODES + 1, _bl_count)
STATIC_DTREE_LEN = [5] * D_CODES
STATIC_DTREE_CODE = [bi_reverse(n, 5) for n in range(D_CODES)]


def d_code(dist: int) -> int:
    """The distance code of a distance - 1."""
    return DIST_CODE[dist] if dist < 256 else DIST_CODE[256 + (dist >> 7)]


class TreeDesc:
    """A Huffman tree being built. As in zlib, each node has two 16-bit fields that hold two things
    at different times: fc is the frequency until the codes are made, then the code; dl is the
    parent node while the tree is built, then the code length."""

    def __init__(self, size: int, stree_len, extra: list, extra_base: int, elems: int, max_length: int):
        self.fc = [0] * size
        self.dl = [0] * size
        self.stree_len = stree_len  # the static tree's code lengths, or None
        self.extra, self.extra_base = extra, extra_base
        self.elems, self.max_length = elems, max_length
        self.max_code = 0


class Deflate:
    """zlib.compressobj(level, DEFLATED, wbits, memlevel, strategy) work-alike: compress() then flush()."""

    def __init__(self, level: int, wbits: int, memlevel: int, strategy: int):
        if not 1 <= level <= 9:
            raise ValueError("deflate: level must be 1..9")
        wrap = 1
        if wbits < 0:  # raw deflate, no zlib header and trailer
            wrap = 0
            if wbits < -15:
                raise ValueError("deflate: invalid parameters")
            wbits = -wbits
        elif wbits > 15:
            raise ValueError("deflate: the gzip wrapper is not supported")
        if (not 1 <= memlevel <= 9 or not 8 <= wbits <= 15 or not 0 <= strategy <= Z_FIXED
                or (wbits == 8 and wrap != 1)):
            raise ValueError("deflate: invalid parameters")
        if wbits == 8:
            wbits = 9  # zlib: "until 256-byte window bug fixed"
        self.wrap = wrap
        self.level, self.strategy = level, strategy
        self.w_bits = wbits
        self.w_size = 1 << wbits
        self.w_mask = self.w_size - 1
        self.max_dist = self.w_size - MIN_LOOKAHEAD
        self.hash_bits = memlevel + 7
        self.hash_size = 1 << self.hash_bits
        self.hash_mask = self.hash_size - 1
        self.hash_shift = (self.hash_bits + MIN_MATCH - 1) // MIN_MATCH
        self.window_size = 2 * self.w_size
        self.window = bytearray(self.window_size)
        self.prev = [0] * self.w_size  # previous position with the same hash, by position & w_mask
        self.head = [0] * self.hash_size  # most recent position of each hash
        self.high_water = 0
        self.lit_bufsize = 1 << (memlevel + 6)
        self.sym_end = self.lit_bufsize - 1  # a block ends after this many symbols
        self.syms = []  # (distance, literal or length - 3) of the current block
        self.status = INIT_STATE
        self.adler = 1
        self.last_flush = -2
        # lm_init
        good, lazy, nice, chain, func = CONFIG[level]
        self.max_lazy_match, self.good_match, self.nice_match, self.max_chain_length = lazy, good, nice, chain
        self.func = func
        self.strstart = 0
        self.block_start = 0
        self.lookahead = 0
        self.insert = 0
        self.match_length = self.prev_length = MIN_MATCH - 1
        self.match_available = 0
        self.match_start = self.prev_match = 0
        self.ins_h = 0
        # input and output
        self.next_in, self.in_pos, self.avail_in = b"", 0, 0
        self.out = bytearray()
        self.bi_buf = self.bi_valid = 0
        # trees (_tr_init)
        self.l_desc = TreeDesc(HEAP_SIZE, STATIC_LTREE_LEN, EXTRA_LBITS, LITERALS + 1, L_CODES, MAX_BITS)
        self.d_desc = TreeDesc(2 * D_CODES + 1, STATIC_DTREE_LEN, EXTRA_DBITS, 0, D_CODES, MAX_BITS)
        self.bl_desc = TreeDesc(2 * BL_CODES + 1, None, EXTRA_BLBITS, 0, BL_CODES, MAX_BL_BITS)
        self.heap = [0] * HEAP_SIZE
        self.depth = [0] * HEAP_SIZE
        self.bl_count = [0] * (MAX_BITS + 1)
        self.init_block()

    # ------------------------------------------------------------------------------------------
    # the zlib.compressobj interface
    # ------------------------------------------------------------------------------------------
    def compress(self, data: bytes) -> bytes:
        self.next_in, self.in_pos, self.avail_in = data, 0, len(data)
        self.deflate(Z_NO_FLUSH)
        return self._take()

    def flush(self) -> bytes:
        self.deflate(Z_FINISH)
        return self._take()

    def _take(self) -> bytes:
        out = bytes(self.out)
        self.out.clear()
        return out

    # ------------------------------------------------------------------------------------------
    # deflate()
    # ------------------------------------------------------------------------------------------
    def deflate(self, flush: int) -> None:
        if self.status == FINISH_STATE and flush != Z_FINISH:
            raise ValueError("deflate: stream already finished")
        old_flush, self.last_flush = self.last_flush, flush
        rank = lambda f: f * 2 - (9 if f > 4 else 0)  # noqa: E731
        if self.avail_in == 0 and rank(flush) <= rank(old_flush) and flush != Z_FINISH:
            return  # nothing to do (zlib returns Z_BUF_ERROR)
        if self.status == FINISH_STATE and self.avail_in != 0:
            raise ValueError("deflate: input after the end of the stream")
        if self.status == INIT_STATE and self.wrap == 0:
            self.status = BUSY_STATE
        if self.status == INIT_STATE:  # the zlib header
            header = (8 + ((self.w_bits - 8) << 4)) << 8
            if self.strategy >= Z_HUFFMAN_ONLY or self.level < 2:
                level_flags = 0
            elif self.level < 6:
                level_flags = 1
            elif self.level == 6:
                level_flags = 2
            else:
                level_flags = 3
            header |= level_flags << 6
            header += 31 - header % 31
            self.out += bytes([header >> 8, header & 255])
            self.adler = 1
            self.status = BUSY_STATE
        if self.avail_in != 0 or self.lookahead != 0 or (flush != Z_NO_FLUSH and self.status != FINISH_STATE):
            if self.strategy == Z_HUFFMAN_ONLY:
                bstate = self.deflate_huff(flush)
            elif self.strategy == Z_RLE:
                bstate = self.deflate_rle(flush)
            elif self.func == "fast":
                bstate = self.deflate_fast(flush)
            else:
                bstate = self.deflate_slow(flush)
            if bstate in (FINISH_STARTED, FINISH_DONE):
                self.status = FINISH_STATE
            if bstate in (NEED_MORE, FINISH_STARTED):
                return
        if flush != Z_FINISH or self.wrap <= 0:
            return
        a = self.adler  # the zlib trailer: adler32 of the input, most significant byte first
        self.out += bytes([(a >> 24) & 255, (a >> 16) & 255, (a >> 8) & 255, a & 255])
        self.wrap = -self.wrap

    # ------------------------------------------------------------------------------------------
    # the sliding window and the hash chains
    # ------------------------------------------------------------------------------------------
    def read_buf(self, at: int, size: int) -> int:
        n = min(self.avail_in, size)
        if n == 0:
            return 0
        chunk = self.next_in[self.in_pos: self.in_pos + n]
        self.window[at: at + n] = chunk
        self.in_pos += n
        self.avail_in -= n
        if self.wrap == 1:
            self.adler = zlib.adler32(chunk, self.adler)
        return n

    def slide_hash(self) -> None:
        ws = self.w_size
        self.head = [m - ws if m >= ws else 0 for m in self.head]
        self.prev = [m - ws if m >= ws else 0 for m in self.prev]

    def fill_window(self) -> None:
        """Read input into the window when the lookahead runs short; when the window is nearly full,
        move its upper half down (and every position in the hash chains with it)."""
        wsize = self.w_size
        win = self.window
        while True:
            more = self.window_size - self.lookahead - self.strstart
            if self.strstart >= wsize + self.max_dist:
                n = wsize - more
                win[0:n] = win[wsize:wsize + n]
                self.match_start = (self.match_start - wsize) & M32
                self.strstart -= wsize
                self.block_start -= wsize
                if self.insert > self.strstart:
                    self.insert = self.strstart
                self.slide_hash()
                more += wsize
            if self.avail_in == 0:
                break
            self.lookahead += self.read_buf(self.strstart + self.lookahead, more)
            # start the hash of the bytes not yet inserted, now that there is input
            if self.lookahead + self.insert >= MIN_MATCH:
                s = self.strstart - self.insert
                h = win[s]
                h = ((h << self.hash_shift) ^ win[s + 1]) & self.hash_mask
                while self.insert:
                    h = ((h << self.hash_shift) ^ win[s + MIN_MATCH - 1]) & self.hash_mask
                    self.prev[s & self.w_mask] = self.head[h]
                    self.head[h] = s
                    s += 1
                    self.insert -= 1
                    if self.lookahead + self.insert < MIN_MATCH:
                        break
                self.ins_h = h
            if not (self.lookahead < MIN_LOOKAHEAD and self.avail_in != 0):
                break
        # zero the WIN_INIT bytes after the data if they were never written: longest_match may read
        # up to MAX_MATCH bytes ahead, though matches never count beyond the lookahead
        if self.high_water < self.window_size:
            curr = self.strstart + self.lookahead
            if self.high_water < curr:
                init = min(self.window_size - curr, WIN_INIT)
                win[curr:curr + init] = bytes(init)
                self.high_water = curr + init
            elif self.high_water < curr + WIN_INIT:
                init = min(curr + WIN_INIT - self.high_water, self.window_size - self.high_water)
                win[self.high_water:self.high_water + init] = bytes(init)
                self.high_water += init

    def insert_string(self, s: int) -> int:
        """Add the 3 bytes at s to the hash chains; returns the previous head of the chain."""
        self.ins_h = ((self.ins_h << self.hash_shift) ^ self.window[s + MIN_MATCH - 1]) & self.hash_mask
        head = self.head[self.ins_h]
        self.prev[s & self.w_mask] = head
        self.head[self.ins_h] = s
        return head

    def longest_match(self, cur_match: int) -> int:
        """The longest match for the string at strstart along the hash chain from cur_match; sets
        match_start. Matches no longer than prev_length are ignored."""
        win = self.window
        chain_length = self.max_chain_length
        scan = self.strstart
        best_len = self.prev_length
        nice_match = self.nice_match
        limit = self.strstart - self.max_dist if self.strstart > self.max_dist else 0
        prev, wmask = self.prev, self.w_mask
        if self.prev_length >= self.good_match:
            chain_length >>= 2
        if nice_match > self.lookahead:
            nice_match = self.lookahead
        s0, s1 = win[scan], win[scan + 1]
        scan_end1, scan_end = win[scan + best_len - 1], win[scan + best_len]
        tail = win[scan + 3: scan + MAX_MATCH]
        while True:
            m = cur_match
            if win[m + best_len] == scan_end and win[m + best_len - 1] == scan_end1 and win[m] == s0 \
                    and win[m + 1] == s1:
                # Bytes 0 and 1 match; zlib does not compare byte 2, which the equal hash implies. The
                # length is 3 + the bytes that agree from offset 3 on, up to MAX_MATCH. (zlib reads up to
                # MAX_MATCH bytes ahead even past the input; the length is cut to the lookahead below.)
                other = win[m + 3: m + MAX_MATCH]
                if other == tail:
                    length = MAX_MATCH
                else:
                    diff = int.from_bytes(other, "little") ^ int.from_bytes(tail, "little")
                    length = 3 + ((diff & -diff).bit_length() - 1) // 8  # the first differing byte
                if length > best_len:
                    self.match_start = cur_match
                    best_len = length
                    if length >= nice_match:
                        break
                    scan_end1, scan_end = win[scan + best_len - 1], win[scan + best_len]
            cur_match = prev[cur_match & wmask]
            if cur_match <= limit:
                break
            chain_length -= 1
            if chain_length == 0:
                break
        return best_len if best_len <= self.lookahead else self.lookahead

    # ------------------------------------------------------------------------------------------
    # the match finders: deflate_fast (levels 1-3), deflate_slow (4-9), deflate_rle, deflate_huff
    # ------------------------------------------------------------------------------------------
    def tally(self, dist: int, lc: int) -> bool:
        """Record a literal (dist 0, lc the byte) or a match (lc = length - 3); True when the block
        is full."""
        self.syms.append((dist, lc))
        if dist == 0:
            self.l_desc.fc[lc] += 1
        else:
            self.l_desc.fc[LENGTH_CODE[lc] + LITERALS + 1] += 1
            self.d_desc.fc[d_code(dist - 1)] += 1
        return len(self.syms) == self.sym_end

    def flush_block(self, last: int) -> None:
        """FLUSH_BLOCK: write the block from block_start to strstart."""
        stored = self.window[self.block_start:self.strstart] if self.block_start >= 0 else None
        self.tr_flush_block(stored, self.strstart - self.block_start, last)
        self.block_start = self.strstart

    def deflate_fast(self, flush: int) -> int:
        while True:
            if self.lookahead < MIN_LOOKAHEAD:
                self.fill_window()
                if self.lookahead < MIN_LOOKAHEAD and flush == Z_NO_FLUSH:
                    return NEED_MORE
                if self.lookahead == 0:
                    break
            hash_head = 0
            if self.lookahead >= MIN_MATCH:
                hash_head = self.insert_string(self.strstart)
            if hash_head != 0 and self.strstart - hash_head <= self.max_dist:
                self.match_length = self.longest_match(hash_head)
            if self.match_length >= MIN_MATCH:
                bflush = self.tally(self.strstart - self.match_start, self.match_length - MIN_MATCH)
                self.lookahead -= self.match_length
                if self.match_length <= self.max_lazy_match and self.lookahead >= MIN_MATCH:
                    self.match_length -= 1  # the string at strstart is already in the table
                    while True:
                        self.strstart += 1
                        self.insert_string(self.strstart)
                        self.match_length -= 1
                        if self.match_length == 0:
                            break
                    self.strstart += 1
                else:
                    self.strstart += self.match_length
                    self.match_length = 0
                    w = self.window
                    self.ins_h = ((w[self.strstart] << self.hash_shift) ^ w[self.strstart + 1]) & self.hash_mask
            else:
                bflush = self.tally(0, self.window[self.strstart])
                self.lookahead -= 1
                self.strstart += 1
            if bflush:
                self.flush_block(0)
        self.insert = self.strstart if self.strstart < MIN_MATCH - 1 else MIN_MATCH - 1
        if flush == Z_FINISH:
            self.flush_block(1)
            return FINISH_DONE
        if self.syms:
            self.flush_block(0)
        return BLOCK_DONE

    def deflate_slow(self, flush: int) -> int:
        while True:
            if self.lookahead < MIN_LOOKAHEAD:
                self.fill_window()
                if self.lookahead < MIN_LOOKAHEAD and flush == Z_NO_FLUSH:
                    return NEED_MORE
                if self.lookahead == 0:
                    break
            hash_head = 0
            if self.lookahead >= MIN_MATCH:
                hash_head = self.insert_string(self.strstart)
            self.prev_length, self.prev_match = self.match_length, self.match_start
            self.match_length = MIN_MATCH - 1
            if hash_head != 0 and self.prev_length < self.max_lazy_match and self.strstart - hash_head <= self.max_dist:
                self.match_length = self.longest_match(hash_head)
                if self.match_length <= 5 and (self.strategy == Z_FILTERED or (
                        self.match_length == MIN_MATCH and ((self.strstart - self.match_start) & M32) > TOO_FAR)):
                    self.match_length = MIN_MATCH - 1
            # a match at the previous position that is not beaten here is written out
            if self.prev_length >= MIN_MATCH and self.match_length <= self.prev_length:
                max_insert = self.strstart + self.lookahead - MIN_MATCH
                bflush = self.tally(self.strstart - 1 - self.prev_match, self.prev_length - MIN_MATCH)
                self.lookahead -= self.prev_length - 1
                self.prev_length -= 2
                while True:
                    self.strstart += 1
                    if self.strstart <= max_insert:
                        self.insert_string(self.strstart)
                    self.prev_length -= 1
                    if self.prev_length == 0:
                        break
                self.match_available = 0
                self.match_length = MIN_MATCH - 1
                self.strstart += 1
                if bflush:
                    self.flush_block(0)
            elif self.match_available:
                # no better match: the previous byte goes out as a literal
                if self.tally(0, self.window[self.strstart - 1]):
                    self.flush_block(0)
                self.strstart += 1
                self.lookahead -= 1
            else:
                self.match_available = 1  # wait one step to compare with the next match
                self.strstart += 1
                self.lookahead -= 1
        if self.match_available:
            self.tally(0, self.window[self.strstart - 1])
            self.match_available = 0
        self.insert = self.strstart if self.strstart < MIN_MATCH - 1 else MIN_MATCH - 1
        if flush == Z_FINISH:
            self.flush_block(1)
            return FINISH_DONE
        if self.syms:
            self.flush_block(0)
        return BLOCK_DONE

    def deflate_rle(self, flush: int) -> int:
        """Z_RLE: only matches at distance 1 (runs of one byte)."""
        win = self.window
        while True:
            if self.lookahead <= MAX_MATCH:
                self.fill_window()
                if self.lookahead <= MAX_MATCH and flush == Z_NO_FLUSH:
                    return NEED_MORE
                if self.lookahead == 0:
                    break
            self.match_length = 0
            if self.lookahead >= MIN_MATCH and self.strstart > 0:
                scan = self.strstart - 1
                prev = win[scan]
                if win[scan + 1] == prev and win[scan + 2] == prev and win[scan + 3] == prev:
                    # zlib compares up to strstart + MAX_MATCH in steps of 8; the run is then cut to the
                    # lookahead
                    n = 3
                    while n < MAX_MATCH and win[scan + 1 + n] == prev:
                        n += 1
                    self.match_length = min(n, self.lookahead)
            if self.match_length >= MIN_MATCH:
                bflush = self.tally(1, self.match_length - MIN_MATCH)
                self.lookahead -= self.match_length
                self.strstart += self.match_length
                self.match_length = 0
            else:
                bflush = self.tally(0, win[self.strstart])
                self.lookahead -= 1
                self.strstart += 1
            if bflush:
                self.flush_block(0)
        self.insert = 0
        if flush == Z_FINISH:
            self.flush_block(1)
            return FINISH_DONE
        if self.syms:
            self.flush_block(0)
        return BLOCK_DONE

    def deflate_huff(self, flush: int) -> int:
        """Z_HUFFMAN_ONLY: literals only."""
        while True:
            if self.lookahead == 0:
                self.fill_window()
                if self.lookahead == 0:
                    if flush == Z_NO_FLUSH:
                        return NEED_MORE
                    break
            self.match_length = 0
            bflush = self.tally(0, self.window[self.strstart])
            self.lookahead -= 1
            self.strstart += 1
            if bflush:
                self.flush_block(0)
        self.insert = 0
        if flush == Z_FINISH:
            self.flush_block(1)
            return FINISH_DONE
        if self.syms:
            self.flush_block(0)
        return BLOCK_DONE

    # ------------------------------------------------------------------------------------------
    # bit output (deflate writes bits least significant first)
    # ------------------------------------------------------------------------------------------
    def send_bits(self, value: int, length: int) -> None:
        self.bi_buf |= value << self.bi_valid
        self.bi_valid += length
        while self.bi_valid >= 8:
            self.out.append(self.bi_buf & 255)
            self.bi_buf >>= 8
            self.bi_valid -= 8

    def bi_windup(self) -> None:
        """Pad to a byte boundary."""
        if self.bi_valid > 0:
            self.out.append(self.bi_buf & 255)
        self.bi_buf = self.bi_valid = 0

    # ------------------------------------------------------------------------------------------
    # Huffman trees (trees.c)
    # ------------------------------------------------------------------------------------------
    def init_block(self) -> None:
        for desc, n in ((self.l_desc, L_CODES), (self.d_desc, D_CODES), (self.bl_desc, BL_CODES)):
            desc.fc[:n] = [0] * n
        self.l_desc.fc[END_BLOCK] = 1
        self.opt_len = self.static_len = 0
        self.syms = []

    def _smaller(self, fc: list, n: int, m: int) -> bool:
        return fc[n] < fc[m] or (fc[n] == fc[m] and self.depth[n] <= self.depth[m])

    def pqdownheap(self, fc: list, k: int) -> None:
        heap = self.heap
        v = heap[k]
        j = k << 1
        while j <= self.heap_len:
            if j < self.heap_len and self._smaller(fc, heap[j + 1], heap[j]):
                j += 1
            if self._smaller(fc, v, heap[j]):
                break
            heap[k] = heap[j]
            k = j
            j <<= 1
        heap[k] = v

    def gen_bitlen(self, desc: TreeDesc) -> None:
        """Optimal code lengths from the tree, limited to max_length bits; updates opt_len and
        static_len."""
        fc, dl = desc.fc, desc.dl
        heap = self.heap
        bl_count = self.bl_count
        for bits in range(MAX_BITS + 1):
            bl_count[bits] = 0
        overflow = 0
        dl[heap[self.heap_max]] = 0  # the root
        for h in range(self.heap_max + 1, HEAP_SIZE):
            n = heap[h]
            bits = dl[dl[n]] + 1  # dl[n] is still n's parent here, and becomes its length
            if bits > desc.max_length:
                bits = desc.max_length
                overflow += 1
            dl[n] = bits
            if n > desc.max_code:
                continue  # not a leaf
            bl_count[bits] += 1
            xbits = desc.extra[n - desc.extra_base] if n >= desc.extra_base else 0
            f = fc[n]
            self.opt_len += f * (bits + xbits)
            if desc.stree_len:
                self.static_len += f * (desc.stree_len[n] + xbits)
        if overflow == 0:
            return
        # some codes are too long: move leaves until the lengths fit, then reassign them
        while True:
            bits = desc.max_length - 1
            while bl_count[bits] == 0:
                bits -= 1
            bl_count[bits] -= 1
            bl_count[bits + 1] += 2
            bl_count[desc.max_length] -= 1
            overflow -= 2
            if overflow <= 0:
                break
        h = HEAP_SIZE
        for bits in range(desc.max_length, 0, -1):
            n = bl_count[bits]
            while n != 0:
                h -= 1
                m = heap[h]
                if m > desc.max_code:
                    continue
                if dl[m] != bits:
                    self.opt_len += (bits - dl[m]) * fc[m]
                    dl[m] = bits
                n -= 1

    def build_tree(self, desc: TreeDesc) -> None:
        fc, dl = desc.fc, desc.dl
        heap, depth = self.heap, self.depth
        elems = desc.elems
        max_code = -1
        self.heap_len, self.heap_max = 0, HEAP_SIZE
        for n in range(elems):
            if fc[n] != 0:
                self.heap_len += 1
                heap[self.heap_len] = max_code = n
                depth[n] = 0
            else:
                dl[n] = 0
        # deflate needs at least two codes: add codes of frequency 1 if there are fewer
        while self.heap_len < 2:
            if max_code < 2:
                max_code += 1
                node = max_code
            else:
                node = 0
            self.heap_len += 1
            heap[self.heap_len] = node
            fc[node] = 1
            depth[node] = 0
            self.opt_len -= 1
            if desc.stree_len:
                self.static_len -= desc.stree_len[node]
        desc.max_code = max_code
        for n in range(self.heap_len // 2, 0, -1):
            self.pqdownheap(fc, n)
        node = elems  # the next internal node
        while True:
            n = heap[1]  # pqremove: the least frequent node
            heap[1] = heap[self.heap_len]
            self.heap_len -= 1
            self.pqdownheap(fc, 1)
            m = heap[1]  # the next least frequent
            self.heap_max -= 1
            heap[self.heap_max] = n
            self.heap_max -= 1
            heap[self.heap_max] = m
            fc[node] = fc[n] + fc[m]
            depth[node] = (depth[n] if depth[n] >= depth[m] else depth[m]) + 1
            dl[n] = dl[m] = node
            heap[1] = node
            node += 1
            self.pqdownheap(fc, 1)
            if self.heap_len < 2:
                break
        self.heap_max -= 1
        heap[self.heap_max] = heap[1]
        self.gen_bitlen(desc)
        gen_codes(fc, dl, max_code, self.bl_count)

    def scan_tree(self, desc: TreeDesc, max_code: int) -> None:
        """Count how often each code length (and run of lengths) occurs, for the bit length tree."""
        dl, bl = desc.dl, self.bl_desc.fc
        prevlen, nextlen, count = -1, dl[0], 0
        max_count, min_count = (138, 3) if nextlen == 0 else (7, 4)
        dl[max_code + 1] = 0xFFFF  # guard
        for n in range(max_code + 1):
            curlen, nextlen = nextlen, dl[n + 1]
            count += 1
            if count < max_count and curlen == nextlen:
                continue
            if count < min_count:
                bl[curlen] += count
            elif curlen != 0:
                if curlen != prevlen:
                    bl[curlen] += 1
                bl[REP_3_6] += 1
            elif count <= 10:
                bl[REPZ_3_10] += 1
            else:
                bl[REPZ_11_138] += 1
            count, prevlen = 0, curlen
            if nextlen == 0:
                max_count, min_count = 138, 3
            elif curlen == nextlen:
                max_count, min_count = 6, 3
            else:
                max_count, min_count = 7, 4

    def send_tree(self, desc: TreeDesc, max_code: int) -> None:
        """Send the code lengths of a tree, coded with the bit length tree."""
        dl = desc.dl
        bcode, blen = self.bl_desc.fc, self.bl_desc.dl
        send = lambda c: self.send_bits(bcode[c], blen[c])  # noqa: E731
        prevlen, nextlen, count = -1, dl[0], 0
        max_count, min_count = (138, 3) if nextlen == 0 else (7, 4)
        for n in range(max_code + 1):
            curlen, nextlen = nextlen, dl[n + 1]
            count += 1
            if count < max_count and curlen == nextlen:
                continue
            if count < min_count:
                for _ in range(count):
                    send(curlen)
            elif curlen != 0:
                if curlen != prevlen:
                    send(curlen)
                    count -= 1
                send(REP_3_6)
                self.send_bits(count - 3, 2)
            elif count <= 10:
                send(REPZ_3_10)
                self.send_bits(count - 3, 3)
            else:
                send(REPZ_11_138)
                self.send_bits(count - 11, 7)
            count, prevlen = 0, curlen
            if nextlen == 0:
                max_count, min_count = 138, 3
            elif curlen == nextlen:
                max_count, min_count = 6, 3
            else:
                max_count, min_count = 7, 4

    def build_bl_tree(self) -> int:
        self.scan_tree(self.l_desc, self.l_desc.max_code)
        self.scan_tree(self.d_desc, self.d_desc.max_code)
        self.build_tree(self.bl_desc)
        max_blindex = BL_CODES - 1
        while max_blindex >= 3 and self.bl_desc.dl[BL_ORDER[max_blindex]] == 0:
            max_blindex -= 1
        self.opt_len += 3 * (max_blindex + 1) + 5 + 5 + 4
        return max_blindex

    def send_all_trees(self, lcodes: int, dcodes: int, blcodes: int) -> None:
        self.send_bits(lcodes - 257, 5)
        self.send_bits(dcodes - 1, 5)
        self.send_bits(blcodes - 4, 4)
        for rank in range(blcodes):
            self.send_bits(self.bl_desc.dl[BL_ORDER[rank]], 3)
        self.send_tree(self.l_desc, lcodes - 1)
        self.send_tree(self.d_desc, dcodes - 1)

    def compress_block(self, lcode: list, llen: list, dcode: list, dlen: list) -> None:
        send = self.send_bits
        for dist, lc in self.syms:
            if dist == 0:
                send(lcode[lc], llen[lc])  # a literal byte
            else:
                code = LENGTH_CODE[lc]
                send(lcode[code + LITERALS + 1], llen[code + LITERALS + 1])
                if EXTRA_LBITS[code]:
                    send(lc - BASE_LENGTH[code], EXTRA_LBITS[code])
                dist -= 1
                code = d_code(dist)
                send(dcode[code], dlen[code])
                if EXTRA_DBITS[code]:
                    send(dist - BASE_DIST[code], EXTRA_DBITS[code])
        send(lcode[END_BLOCK], llen[END_BLOCK])

    def tr_stored_block(self, buf: bytes, stored_len: int, last: int) -> None:
        self.send_bits((STORED_BLOCK << 1) + last, 3)
        self.bi_windup()
        n = stored_len & 0xFFFF
        self.out += bytes([n & 255, n >> 8, ~n & 255, (~n >> 8) & 255])
        self.out += buf[:stored_len]

    def tr_flush_block(self, buf, stored_len: int, last: int) -> None:
        """Write the block stored, with the static codes, or with its own codes: the smallest."""
        self.build_tree(self.l_desc)
        self.build_tree(self.d_desc)
        max_blindex = self.build_bl_tree()
        opt_lenb = (self.opt_len + 3 + 7) >> 3
        static_lenb = (self.static_len + 3 + 7) >> 3
        if static_lenb <= opt_lenb or self.strategy == Z_FIXED:
            opt_lenb = static_lenb
        if stored_len + 4 <= opt_lenb and buf is not None:
            self.tr_stored_block(buf, stored_len, last)
        elif static_lenb == opt_lenb:
            self.send_bits((STATIC_TREES << 1) + last, 3)
            self.compress_block(STATIC_LTREE_CODE, STATIC_LTREE_LEN, STATIC_DTREE_CODE, STATIC_DTREE_LEN)
        else:
            self.send_bits((DYN_TREES << 1) + last, 3)
            self.send_all_trees(self.l_desc.max_code + 1, self.d_desc.max_code + 1, max_blindex + 1)
            self.compress_block(self.l_desc.fc, self.l_desc.dl, self.d_desc.fc, self.d_desc.dl)
        self.init_block()
        if last:
            self.bi_windup()


def deflate(raw: bytes, level: int, mem: int, strategy: int, wbits: int) -> bytes:
    c = Deflate(level, wbits, mem, strategy)
    return c.compress(raw) + c.flush()
