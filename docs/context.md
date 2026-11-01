# mixz: full project context

This file is the full context of the mixz project: where the idea came from, what was built, every measurement, the decisions taken, and the plan. The MVP was built on a cloud Linux VM; later work ran on an AMD Ryzen 3 1200 desktop. The public release ships without primers (`primers/README.md`); measurements in this file that name a primer were made with the development primers, which are not published. `CLAUDE.md` holds the short version. Commit IDs in this file (for example `4a54acc`) refer to the development repository, whose history is not published; the public repository starts with a single commit.

Certainty labels used here: **measured** = a number from a run, **tested** = covered by `tools/test_roundtrip.py`, **estimate** = reasoned but not run.

---

## 1. Origin

The original idea: for each file type, find the 10,000 most repeated 30-bit bit patterns and give each one a shorter unique code, cutting files into recognized blocks.

The idea was tested as specified (**measured**, files split by file into train and test halves, 10,000 codes of 14 bits plus a 1-bit hit/miss flag):

| Type | Block hit rate | Scheme as specified | Ideal entropy coding of the same scheme | gzip -9 | xz -6 | zstd -19 + dict |
|---|---|---|---|---|---|---|
| Python | 53% | 74.9% | 69.1% | 24.3% | 22.9% | 20.5% |
| ELF | 36% | 84.0% | 78.8% | 40.2% | 32.4% | 34.8% |
| PNG | 1.6% | 102.5% | 99.3% | 97.3% | 97.6% | 95.8% |
| .gz | 1.8% | 102.4% | 99.4% | 95.8% | 95.4% | 93.7% |
| random | 0% | 103.3% | 100% | 100% | 100% | 100% |

Why it lost:
- It needs an escape flag, so every miss grows the data.
- It can never go below 50% (30 bits become 15 at best).
- Bytes are 8 bits and 30 is not a multiple of 8, so the same text lands in 4 different bit alignments.
- A fixed table per file type cannot use repeats inside the file itself.
- Already-compressed data looks random.

What was kept: learning per file type what typical content looks like. That became the **primers** (now planned to become the **mind**).

---

## 2. What was built (the MVP)

A format-aware context-mixing (CM) compressor. Context mixing means several models each predict the next bit, and a small neural network (the mixer) combines their predictions. The design is in `README.md` section 5. In short:

**Engine (`engine/mixz.c` in the MVP, now `engine/cm.c`; C, integer-only, deterministic):**
- **Models:**
  - 8 hashed context models. Each context stores a bit-history state: bounded counts of 0s and 1s, kept in buckets of 16 bytes, one bucket per half-byte.
  - 8 run models: the last byte seen in each context and how often it repeated.
  - An order-0 model (no context).
  - A match model: it finds the longest earlier repeat and predicts that it continues. Minimum match length 6 (2 for images since codec 3, section 2.8).
- **Mixing and correction:**
  - 4 gated mixers in layer 1, selected by: the partial byte (c0), the match state, the previous byte (or the pixel to the left for images), and the number of contexts with history.
  - 1 final mixer in layer 2.
  - 2 APM/SSE correction stages.
  - Binary arithmetic coder with 32-bit range and 16-bit probabilities.
- **Profiles:** text, binary and image, with different context sets. Codec 3 added code and web, two variants of text (section 2.8).
- **Primed snapshots:** prime the model once, then clone it per file (since step 3: copy it into a per-thread worker, section 2.5). Output is identical to replaying the primer (**tested**).
- **Tuned parameters (measured sweeps on 1 MB text + 1 MB ELF):** `LR1=2, LR2=1, SHIFT1=12, SHIFT2=12, WINIT=8192, APMRATE=6`.
  - The run model helped: 1 MB text 145,269 → 143,041 bytes; 1 MB binary 356,175 → 344,262.
  - The 4th mixer helped slightly: 143,041 → 142,641; 344,262 → 342,330.
  - A 3rd SSE stage did not help and was removed.

**Wrapper (`mixz.py`):**
- Detects the format from content.
- Transforms, each kept only if the result rebuilds the original exactly (checked at compression time):
  - gzip: inflate, find the zlib parameters that reproduce the stream.
  - PNG: inflate IDAT, undo the row filters, re-create the stream bit-exactly.
  - x86 E8/E9 transform for executables.
- Picks a primer by testing each candidate as a zstd dictionary on a 64 KB sample.
- Falls back to storing the file when nothing helps.
- Container format 1: magic `MXZ`, version byte, CRC32 of the original, 16-bit primer tag, maxbits byte. Since plan step 1, format 2 is written (section 2.1). Format 1 is no longer read (section 2.4).

**Primers (`primers/*.bin`):**
- 512 KB per type: py, c, js, web, json, prose, man, elf.
- Built by a "cover" method: greedy selection of the 1 KB segments whose 8-byte substrings appear in the most training files.
- Built from the training half only. The split is by a hash of the normalized path.

### 2.1 File format 2 (plan step 1, 2026-10-24)

**`registry.json`** is append-only from the first public release (before it, a draft: section 2.4). Stored files refer to its IDs, so an entry is never changed, removed or reused. It holds:
- **Codecs:** an engine algorithm. Codec 1 was `engine/mixz.c` as it had been since the MVP (dropped, section 2.4). Its sha256 is registered, and a test fails if the engine file changes without a new codec ID.
- **Deflates:** an implementation that re-creates gzip and PNG streams. Deflate 1 is zlib 1.3.1.
- **Modules:** a mind module, with its file and sha256. Modules 1–8 are the 8 primers, numbered as `PRIMER_IDS`.
- **Versions:** everything the decoder needs besides the file's own header. That is a codec, an engine profile (the context set: text, binary or image), a list of modules and the mixer start weights (`null` means the codec's built-in values).
  - Versions 1–8: text, with no primer, then py, c, js, web, json, prose or man.
  - Versions 9–10: binary, with no primer or with elf.
  - Version 11: image.
- **Profiles:** the detected content type (text, binary, image, binary/x86, image/png; since step 6 also text/code and text/web, section 2.8). It is informational: decoding never depends on it.

**Layout of a format 2 file:**
- `"MXZ"`, then format byte 2, then a flags byte, then the original size as a varint, then the CRC32 of the original.
  - The flags byte holds the method, the transform, a meta bit, a block-table bit and one reserved bit that must be 0.
  - A varint is a variable-length integer: 7 bits per byte.
- A stored file follows with the original bytes.
- A modelled file follows with:
  1. version ID (varint), profile ID (varint), maxbits (1 byte);
  2. for the image engine profile: row length and bytes per pixel;
  3. if there is a transform: the model stream length;
  4. for gzip and PNG: the deflate ID, then the metadata length and metadata;
  5. if the block-table bit is set: the block count (at least 2), then for each block its model bytes, payload bytes and CRC32;
  6. the payload: the blocks' coded data, one after the other. Each block is coded from the primed model, so blocks can be decoded independently. Plan step 4 uses that across cores.

**What changed against format 1:**
- **Header size:** a one-block file's header is 2 bytes smaller (1 byte for gzip and PNG, which carry the deflate ID). The coded payload is byte-identical to format 1 (tested on all vectors).
- **Module check:** the 16-bit primer tag is replaced by checking the module's registered sha256 before decoding.
- **Pinned deflate:** gzip and PNG streams are re-created with zlib 1.3.1's compressor, compiled into the engine.
  - Where: `engine/pdeflate.c` and `engine/zlib-1.3.1/`. The files are unmodified; the tarball sha256 `9a93b2b7…df23` was checked against both zlib.net and GitHub.
  - Effect: results no longer depend on the system zlib. Format 1 files are also decoded with it.
  - Tested: its output equals the system zlib 1.3.1's at levels 1–9.
  - Level 0 is refused. Its stored-block sizes follow the caller's output buffer size, so they cannot be reproduced. mixz never searched level 0.
- **Size checks:** every size field is checked before any memory is allocated (tested on malformed headers; each is rejected in under 40 ms).
  - The bound: the coder caps a bit's probability at 65,504/65,536. So at most about 1,420 model bytes come from each payload byte, and the decoder rejects claims above 2,048 per byte.
  - Measured check: 100,000 zero bytes code to 74 payload bytes, which is 1,351:1.
  - Model tables grow with the stream length, so decoder memory stays within about 2,000× the `.mxz` size plus about 20 MB (estimate from the code).
  - Format 1 files get the same check.

**Measured effect** (quick tier, against the pre-format-2 code):
- **Coded payloads:** byte-identical on all 938 files.
- **Total output:** 1,524 bytes smaller (−0.058%), all from smaller headers. Per kind the change is −0.00% (elf) to −0.62% (web, which has many small files).
- **Speed:** the old and new code were run alternately on 1 core, timed in CPU time, best of 2 rounds. Compress went from 92.9 to 93.4 s (+0.6%) and decompress from 67.05 to 67.04 s (0.0%). Both are within noise.
  - A harness run at the same time showed −17%. That was machine state: the unchanged engine binary also measured 13% slower, with load 4–6 from other programs.
- **Pinned deflate:** equal to the system zlib 1.3.1 in 27,360 of 27,360 cases (levels 1–9, all memLevels and strategies, raw/zlib/gzip wrapping, whole and chunked input).
- **Determinism:** all format 1 and format 2 vectors are reproduced byte-identically by `-O0`, `-O3` and `-O3 -march=native` builds.

### 2.2 Codec 2 (plan step 2, 2026-10-24)

**What it is:**
- `engine/cm2.c` is codec 2 (since section 2.4: `engine/cm.c`, prefix `cm_`). New files are written with it (versions 12–22).
- `engine/mixz.c` stayed unchanged as codec 1 so that older files kept decoding, until codec 1 was dropped (section 2.4).
- Each codec exports the same functions under its own prefix (then `mz_`, `mz2_`). The registry entry names the source file and the prefix.
- Codec 2's parameters are fixed constants, not build options, so the source file alone defines the codec. Its sha256 is registered.

**The one change to what the model computes: a 16-bit mixer.**
- The 4 first-layer mixers use 16-bit weights, where 1.0 is 2^13, instead of 32-bit weights.
- Products are summed in pairs, and each pair sum is shifted down by 8. This matches the AVX2 multiply-add instruction (16 multiply-adds per instruction).
- The weight update rounds to nearest and saturates at the 16-bit limits (one rounding-multiply instruction per 16 weights). Codec 1 rounded down and clamped at ±8.0.
- A scalar path gives identical results. It is used when the build has no AVX2, for example `-O0` or plain `-O3`.
  - Tested: the `-O0`, `-O3` and `-O3 -march=native` builds give identical output on all vectors and on 9 probe streams.
  - Checked in the binary: the plain `-O3` build contains no AVX2 multiply-add instructions, so it really ran the scalar path.
- A weight row is now 64 bytes, exactly one cache line.
- Effect on size: −0.07% on the probe streams. Rounding to nearest helps slightly, and a weight scale of 2^14 or 2^15 measured the same or worse.

**Faster arrangement of the same arithmetic.** Each change was checked to give byte-identical output against the version before it (12 streams):
- The logistic function (squash) is a 4,095-entry table instead of an interpolation.
- The StateMaps (state → probability tables) live inside the model. Predict remembers which entry it used, and the update changes that entry with one addition to the packed word.
  - The adding is exact, because the probability field can never leave its range (proof in the code comment), so the clamps were removed too.
  - This change alone was 1.22–1.27× faster.
- Run-model state is read once per byte, not once per bit. A flag tracks whether the run byte still matches the bits seen so far.
- As soon as a bit is known, the memory the next prediction needs is requested in advance (prefetched): the next byte's or nibble's hash-table buckets, the run-table entries, the match-table slot, the APM rows and the mixer row.
- Building the APM tables now copies one row instead of computing 4.3 million values. That saves about 58 million instructions each time a model is created (counted with callgrind).
- Smaller items: a table for the "deterministic state" input, the image channel kept as a counter instead of a division per byte, and the shared APM input computed once.

**Engine speed** (1 core, CPU time, codec 1 and codec 2 run alternately in one session; harness speed inputs: 512 KiB Python, 512 KiB x86 code, 183 KiB image pixels; measured):

| Input | Compress, codec 1 → 2 | Decompress, codec 1 → 2 | Output size change |
|---|---|---|---|
| text | 2,471 → 1,567 ns/B (1.58×) | 2,446 → 1,588 ns/B (1.54×) | −0.06% |
| binary | 2,830 → 1,678 ns/B (1.69×) | 2,772 → 1,662 ns/B (1.67×) | −0.02% |
| image | 2,277 → 1,475 ns/B (1.54×) | 2,314 → 1,547 ns/B (1.50×) | −0.07% |

- Instructions per bit on text: about 2,020 for codec 1 and 1,430 for codec 2 (perf counters).
- The table above is from a quiet machine (load 1.3). The harness run below, under load 5–6 from other programs, measured 1.55–1.81×. Codec 1 slows down more under load.

**Harness, quick tier, against the step 1 baseline** (commit `4a54acc`, recorded the same day on a quiet machine; measured):
- **Size:** every kind is smaller, by 0.01% (JSON, man pages) to 0.07% (C, x86). The total is −0.06%. Verdict OK.
- **Round trip:** 1,877 of 1,877 decompressions OK (4 threads, 1 thread, CLI).
- **Determinism:** 1 against 4 threads, and `-O0` against `-O3 -march=native`: identical.
- **Engine A/B** (alternating, in the same run): text compress 2,619 → 1,565 ns/B and decompress 2,633 → 1,555 ns/B; binary 3,019 → 1,671 and 2,989 → 1,676; image 2,280 → 1,460 and 2,296 → 1,478.
- **Start-up** (CLI, 25 KB Python file, includes loading the primer into the model): compress 1.58 → 1.08 s and decompress 1.54 → 1.03 s, about −34%.
- **Pipeline, 1 core:** the step 1 and step 2 code were run alternately on the quick tier, 2 runs each, with snapshots on as in the harness. Times are CPU time; the two runs of each version agreed within 4%.

| | Step 1 (codec 1) | Step 2 (codec 2) | Change |
|---|---|---|---|
| Compress, total | 93.1 s (0.068 MB/s) | 72.3 s (0.088 MB/s) | 1.29× |
| Compress, user time | 77.4 s | 53.1 s | 1.46× |
| Compress, kernel time | 15.8 s | 19.2 s | +3.4 s |
| Decompress, total | 68.1 s (0.094 MB/s) | 46.0 s (0.138 MB/s) | 1.48× |
| Output | 2,621,950 B | 2,620,483 B | −0.056% |

  - Compression gains less than the engine because two of its costs are outside the engine: kernel time for the snapshot clone (section 3.5), and detection.
  - Why the kernel time rose is not known. Step 3 removes that cost.
- **Pipeline, 4 cores:** not measured fairly. Other programs kept the machine at load 5–6 during the run.

**The plan's premise was wrong, and the target was reached another way:**
- The plan assumed the mixer was 55% of the work. That figure came from cachegrind on the VM with an `-O2 -fno-inline` build, which gcc does not vectorize.
- In the real `-O3 -march=native` build, gcc had already vectorized codec 1's 32-bit mixer. On this machine it took about 16% of the time (perf).
- So the 16-bit mixer alone gave only 1.23–1.36×. The rest came from the StateMap and memory changes above.

**Tried and rejected** (probe streams: 9 kinds, about 192 KB each, coded with their primer; measured):

| Change | Output size | Why rejected |
|---|---|---|
| Remove the "deterministic state" inputs | +0.55% total, up to +2.5% for PNG | Over the 0.5% limit |
| Keep 4 of the 8 run models | +0.7% to +0.9% | Over the limit |
| Remove all run models | +2.0% | Over the limit |
| Stop updating a StateMap entry once its count reaches the limit | +0.74% | Over the limit: the slow adaptation matters |
| Cheaper context hashes (one mixing step instead of 2–6) | −0.08% to +0.06% | Only about 1% faster: not worth an output change |

- Table size does not matter for speed: 1 MB against 16 MB per context table gave 1,890 against 1,883 ns/B. The engine is limited by computation, not by memory.

### 2.3 Version identity: content hashes (2026-10-24)

**The problem:** a short version ID like 12 means something only through the registry that issued it. If a second party issues IDs (another registry, a fork), two files with "version 12" can need different models. The decoder would run the wrong model, and the CRC would reject the result only after a full decode.

**The design:**
- **A version's identity is a content hash.** It is the sha256 over the codec's source hash, the engine profile, the sha256 of each module in replay order, and the start weights (`mixz.version_hash`). Two machines compute the same hash exactly when they have the same content, without any coordination.
- **Short IDs are aliases, issued only by the official registry.** They cost 1 byte. A file can instead name its version by hash: ID 0, then the first 16 bytes of the hash, which costs 16 bytes more.
  - Readers before this change reject ID 0 as an unknown version (tested), so the form breaks no existing file.
  - The reader resolves the hash against its registry. If no registered version matches, the error names the hash it needs.
- **Modules are checked by sha256 before decoding,** so a missing or wrong module fails in milliseconds, not after a full decode.
- **The registry is a hash chain.** Each section's entries are hashed in ID order, and each step hashes the previous value with the entry's defining fields.
  - `registry.json` lists the head after each release (`heads`).
  - `tools/test_roundtrip.py` recomputes every listed head. Editing or removing an entry therefore fails the tests, so the append-only rule is enforced, not just written down.
  - The test also requires that the last listed head is the current registry, so adding an entry means recording a new head.
- **Why not a blockchain:** there is one publisher, so no consensus between parties who distrust each other is needed. The useful parts (everyone keeps a copy, nothing is removed, tampering is detectable) come from the hash chain alone. This is the same approach as Certificate Transparency and Go's checksum database.

**Not built yet:**
- A store that serves missing versions by hash. Any mirror could serve them without being trusted, because the reader checks the hash.
- Versions issued outside the official registry, which would use the hash form.
- Checking a registry against a head published outside the repository.

These belong with step 7 and later.

### 2.4 Codec 1 and format 1 dropped (2026-10-24)

**Decision (the author, 2026-10-24):** nothing has been released, so the first release starts with the current codec. Files of earlier codecs exist only on development machines and need not stay readable.

**What was done** (commit `af82acf`):
- `engine/mixz.c` (codec 1) was removed. Its transforms (E8/E9, PNG filters) are now `engine/transform.c`.
- `engine/cm2.c` is now `engine/cm.c`, with the function prefix `cm_`. Only its comment and prefix changed. All codec 2 vectors recompress byte-identically with `-O0`, `-O3` and `-O3 -march=native` builds (tested).
- `registry.json`: codec 1 and versions 1–11 were removed, and their IDs are listed in `retired`. Codec 2's `engine_sha256` was updated, and `heads` restarted with one head. The chain hashes entries in ID order, so gaps in the IDs are allowed.
- `mixz.py` reads format 2 only. A format 1 file and a retired version are rejected at the header, with an error that names the reason (tested).
- The format 1 and codec 1 test vectors were removed. They are still in git history (commit `0765eaa`).

**The rule until the first release:**
- Only the current codec is in the build.
- An **output-neutral** engine edit is made in place. The committed vectors must still recompress byte-identically, which proves the output did not change. Then the codec's `engine_sha256` and the head are updated.
- An **output-changing** edit gets a new codec ID and new version IDs, and new vectors are written. The old codec's entries are removed, and their IDs are added to `retired`.
- IDs are never reused. A stale development file therefore fails at its header, instead of after a full decode at the CRC check.
- From the release on, the append-only rule and the frozen codecs of section 2.1 apply.

**Weak points of this decision:**
- **Lost evidence.** The format 1 vectors were made on the VM. They were the only check that the engine gives identical output on two different machines (Intel and AMD, tested 2026-10-23). Codec 2 had been checked on one machine only. (Closed 2026-10-24: codec 2 passed on a second machine with two compilers; section 7.)
  - Its determinism rests on the same integer-only design. It was also checked across three builds, including a plain `-O3` build that uses the scalar mixer path instead of AVX2.
  - A run of `tests/check_vectors.py` on a second machine closes this. Any x86 or ARM machine would do, for example a short-lived cloud VM.
- **Version hashes change with every in-place edit**, because they include the source's sha256. Before the release this is acceptable.
  - After the release it would mean that a speed-only engine change needs a new codec, and the old codec's code stays.
  - A possible fix is to let a new codec declare that it decodes an older codec's files. That declaration would be proven by the old vectors.
  - This is not built; it is decided at the release.
- **ID numbers start at 2 (codec) and 12 (versions).** This is harmless, because IDs only need to be unique. Renumbering at the release is possible. It would cost the vectors, and stale files would then fail only at the CRC.

### 2.5 Model kept loaded (plan step 3, 2026-10-24)

**What was built** (no change to any output: all 938 quick-tier outputs are byte-identical to the baseline, and all vectors recompress identically with `-O0`, `-O3` and `-O3 -march=native` builds; tested):
- **Workers.** A worker is a model whose ~155 MB of buffers stay allocated. Each thread keeps one (`cm_worker_new`), and each file copies the primed snapshot into it.
  - Before, each file copied the snapshot into freshly allocated memory. The kernel had to zero and map that memory page by page, about 5.9 million page faults per quick-tier pass.
- **Write log.** While a worker codes a file, the engine records every entry of the large tables the file may change: context buckets, run-model entries, match-table slots and APM cells. That is about 41 entries per byte.
  - When the next file uses the same snapshot, only those entries and the appended match-buffer bytes are copied back, instead of the whole model.
  - The log holds 1 million entries, which covers files up to about 25 KB. A larger file falls back to the full copy, which is as fast at that size (measured: 1M to 8M entries gave the same time at 25 and 50 KB, and an 8M log was 12% slower at 100 KB).
  - `model_update` is compiled twice, with and without the log, so the plain path does not pay for it.
  - A build with `-DMZ_CHECK_RESTORE` compares every table with the snapshot after each restore and aborts on any difference. It ran clean on the worker tests and the vectors.
- **Service mode** (`mixz.py serve`). One process keeps the primed snapshots (`--models N`, default 8) and the workers loaded. `c`, `d` and `t` send each file's bytes to it over a Unix socket and fall back to working themselves when no server runs. The server runs the same `compress()`/`decompress()`, so the output is identical (tested).
  - **Security:** the socket is in a directory only the user can enter (`$XDG_RUNTIME_DIR`, or `/tmp/mixz-UID` with mode 700), and the server checks the peer's user ID (`SO_PEERCRED`).
  - **Stale servers:** each request carries a build identity, which is a hash of the path, size and modification time of `mixz.py`, the registry, the engine sources or library, and the primers. A server whose files differ refuses the request; the client prints a warning and works without it (tested).
- **Lighter client.** `subprocess`, the thread pool and `dataclasses` are no longer imported at start, so importing `mixz` went from 57 ms to 23 ms. The two dataclasses became a 20-line record helper.

**Measured** (1 core, quick tier 6.37 MB, CPU time, old and new alternated twice; the repeat agreed within 2%):

| | Before (`af82acf`) | Step 3 | Change |
|---|---|---|---|
| Compress, snapshots warm (a running server) | 45.5 s (31.1 user + 14.4 kernel) | 24.4 s (24.2 + 0.2) | 1.86× |
| Decompress, snapshots warm | 35.6 s (21.7 + 13.9) | 15.1 s (14.9 + 0.2) | 2.36× |
| Compress in one process, priming included | 52.0 s | 31.5 s | 1.65× |
| Page faults per compress pass | 5.9 million | 0.12–0.3 million | |
| Harness 1-core pipeline (fresh process, CPU time) | 0.115 MB/s | +22.8% compress, +28.9% decompress | |

**CLI start-up** (25 KB Python file, wall clock; measured):

| | Without server | With `mixz serve` running |
|---|---|---|
| Compress 25 KB | 0.99–1.04 s | 0.15 s |
| Decompress 25 KB | 0.97–0.98 s | 0.135–0.175 s |
| Compress / decompress 3 KB | 0.98 / 0.91 s | 0.092 / 0.088 s |

- **The plan's target (start-up 1.5 s → under 0.1 s) is met for small files and missed by about 50 ms at 25 KB.**
  - The fixed cost per call with a server is now about 85–105 ms. Python's own start is 22 ms, importing `mixz` 23–26 ms, and argparse, the socket module, the round trip and exit the rest.
  - The coding itself is about 38 ms for 25 KB.
  - Under about 50 ms would need a client that is not Python. This is not built: a long-running service pays none of this.
- **Engine alone (plain path, no snapshot):** −0.2% to +1.6% against `af82acf` (median and minimum of 8 alternating runs at load 5; machine code differs only by register allocation). This is within this machine's noise.

**Where a server's time goes now** (warm, 1 core, quick tier; measured): coding 15.1 s, detection and transforms 9.2 s, primer choice 0.7 s.
- Text kinds cost 1.8–2.8 µs per input byte, against 1.5 µs for the engine alone.
- Almost all of the 9.2 s is the gzip and PNG parameter search: man pages cost 10.8 µs/B and PNG 8.3 µs/B. This is outside step 3. It is a candidate for later (try the most common settings first).

**Batch memory against speed** (`--models`, measured): with 3 cached models (the default for `c`/`d`/`t`), a quick-tier batch primes 24 times (45.5 s CPU, 871 MB peak). With 8 it primes 9 times (33.4 s, 1,611 MB). The default stays at 3, and `--models` chooses.

**Not built:**
- Persisted snapshots: loading a primed model from disk instead of replaying the primer (about 0.8 s) in processes without a server.
- A non-Python client.
- Server management: start-on-demand, a systemd unit, an idle timeout.

**Weak points:**
- Memory. A server holds about 155 MB per cached model and per worker thread. With the defaults on 4 cores that is about 1.9 GB.
- The write log must cover every table write. A future engine edit that writes a table outside the logged sites would make a worker's next file differ from the replay path. The worker tests in `tools/test_roundtrip.py`, the vectors and the `MZ_CHECK_RESTORE` build catch this; run them after any engine edit.
- Service mode needs Unix sockets: Linux and macOS, not Windows.

### 2.6 Blocks across cores (plan step 4, 2026-10-24)

**What was built:**
- **The blocks of one file are coded at the same time,** one per thread, in both compression and decompression (`mixz.set_block_threads`, `--block-threads`; default one per core).
  - Every block starts from the primed model and shares one snapshot. Each thread has its own worker.
  - The output does not depend on the thread count (tested: 1 and 3 threads give the same bytes).
- **Equal blocks.** A stream is split into ceil(length / block size) blocks of nearly equal length. An image is split into whole rows.
  - Before, all blocks were the block size except a shorter last one. That last block had a different model size, so it needed a second primed model (about 0.8 s more).
  - This changes the output of `--block-size` only. The old 8 KiB vector is kept as a decode-only vector, and a new one was written.
- **The default stays one block** (the measurements below).

**Size cost of splitting** (large files: each kind's full-tier files concatenated in `sets.json` order, 3.1 MB each and 6.0 MB for elf, like an uncompressed archive; `tools/experiments/blocks.py`; measured):

| Block size | py | c | js | json | web | prose | elf | total |
|---|---|---|---|---|---|---|---|---|
| one block | 13.69% | 15.51% | 12.83% | 6.14% | 7.31% | 19.31% | 27.20% | 16.01% |
| 4 MiB | +0% | +0% | +0% | +0% | +0% | +0% | +0.70% | +0.3% |
| 2 MiB | +2.0% | +4.4% | +10.1% | +2.5% | +13.7% | +1.5% | +8.9% | +6.6% |
| 1 MiB | +3.3% | +7.0% | +11.2% | +7.4% | +19.2% | +3.8% | +11.4% | +9.1% |
| 512 KiB | +4.8% | +10.6% | +13.1% | +10.1% | +28.0% | +5.7% | +14.7% | +12.2% |

(Changes are relative to one block. With 4 MiB blocks only elf, at 6 MB, is split, into 2 blocks.)

- **This is far above the plan's assumption** of at most 4% at 4 blocks. That assumption came from one 2 MB Python file (+4%, section 3.3).
- **The cause:** each block starts from the primer and loses everything the model learned from the earlier parts of the file, above all the long repeats between files in an archive. Web and JS lose the most, because their files share the most text.
- **Real single large files** (logs, database dumps) may have less or more such long-range repetition than these concatenations. Not measured.

**Speed** (the same 24.9 MB, wall clock, measured at load 5–8 from other programs, so the 4-thread numbers are a lower bound):

| Blocks | Threads | Compress | Decompress |
|---|---|---|---|
| 2 MiB (2 per 3 MB file) | 1 | 0.590 MB/s | 0.594 MB/s |
| 2 MiB | 2 | 1.100 MB/s (1.86×) | 1.041 MB/s (1.75×) |
| 800 KiB (4 per 3 MB file) | 1 | 0.641 MB/s | 0.611 MB/s |
| 800 KiB | 2 | 1.117 MB/s (1.74×) | 1.054 MB/s (1.73×) |
| 800 KiB | 4 | 1.223 MB/s (1.91×) | 1.276 MB/s (2.09×) |

- CPU time stays at 38–41 s whatever the thread count, so splitting adds no work. The plan's target of at least 1.8× on 2 cores is met for compression and nearly met for decompression.
- Memory: about 155 MB per block thread (its worker), plus the primed models. Peak RSS was 1.5–2.7 GB with 4 threads and up to 10 cached models.

**Decision (accepted with the step 4 merge):** splitting stays opt-in (`--block-size`), because its size cost breaks the 0.5% rule by a factor of 4 to 25 on archive-like data.
- Many files at once already use all cores. Blocks only help one large file finish sooner (latency).
- Copies across blocks cannot recover that cost (section 2.7).
- **Not built:**
  - A large-file tier in the harness (`tools/experiments/blocks.py` measures this for now).
  - Splitting decided per file from its size.
  - Streaming (blocks need the whole file in memory now).

### 2.7 Step 5 dropped: copies cost more bits than CM spends on repeats (2026-10-24)

**Summary:** the plan's step 5 (turn repeats into copy instructions, model only the other bytes, at least 2× faster decompression "with little size cost") was measured and dropped by the author. Nothing of it is in the engine or the format. The experiment code (`tools/experiments/repcost.c` and `runmode.c`, commit `766979b`) is in the development repository's history, not in this repository.

**Why** (each quick-tier file coded alone from its primer; measured):
- CM spends 1–4 bits on a whole correctly predicted repeat of 16–31 bytes and 3–9 bits on one of 64–127 bytes. An explicit copy offset alone costs 10–20 bits, so copies make files larger.
- The cheapest copy form (no offset, no length; one adaptive bit per byte "equals the match model's byte" while a match of at least M bytes holds, the model skipped on yes) still loses, because a skipped byte teaches the model nothing and CM knows where repeats end:

| M | py | c | js | json | web | prose | elf |
|---|---|---|---|---|---|---|---|
| 8: size / decompression speed | +12.9% / 1.91× | +9.2% / 1.44× | +7.5% / 1.57× | +20.5% / 4.35× | +7.7% / 2.32× | +6.4% / 1.34× | +1.0% / 1.13× |
| 32 | +1.2% / 1.17× | +0.7% / 1.05× | +0.5% / 1.11× | +2.8% / 2.48× | +2.0% / 1.29× | +0.3% / 1.06× | +0.0% / 1.02× |

- Copies inside the CM stream also cannot point into a block decoded at the same time, so they do not make step 4's blocks cheaper. That would need a separate dedup layer before the model (not measured, not planned).

**Kept for step 8 (ratio):** 33–54% of all bits on text (16% on elf) go to the one byte where a predicted match breaks. Predicting those breaks better, for example with a second match candidate, may cut output (estimate).

### 2.8 Profiles and tuner, codec 3 (plan step 6, 2026-10-25)

**Summary:** two new text profiles and a shorter shortest match for images, in a new codec (codec 3).
- The profiles are **code** (Python, C, JavaScript) and **web** (HTML, CSS, XML, SVG).
- **Size, on the held-out test set (full tier, 30.6 MB):** py −1.33%, c −1.03%, js −1.71%, web −1.47%, png −0.97%, all files −0.65% (measured).
- **Speed:** the model is 0.7–1.7% slower (measured, CPU time, old and new engine run alternately).
- Codec 2 was dropped (pre-release rule). Accepted by the author and merged on 2026-10-25.

**Terms:**
- **Profile:** the type of content a file is detected as.
- **Engine profile:** which set of contexts the model uses for a profile.
- **Context:** the recent data the model looks at to predict the next bit, for example the last 3 bytes.
- **Slot:** one of the model's 8 hashed contexts.
- **Variant:** the codec with some constants or contexts changed, built only to measure it.
- **Sub-profile:** a profile below a parent profile in the profile tree; here text/code and text/web under text.

**Tuning data** (`tools/tuner/`):
- **Sources:** 63 pinned sources (`sources.json`). Each belongs to one split, so a project is never in both. None is a harness source.
- **Splits:**
  - **train:** where variants are compared and chosen;
  - **valid:** held-out data, where a chosen variant must code its kind at least 1% smaller than its parent;
  - **test:** the harness set, only reported.
- **Files:** `data.py --freeze` chose them the way the harness does (`sets.json`). Budgets per kind and split: 1 MiB for each text kind, 512 KiB for man, 4 MiB for elf, 3 MiB for png.
- **Excluded:** files whose content is in the harness test set, and files with more than 20% primer overlap.

**Search** (train split; measured):
- **Tool:** `tools/tuner/variants.py`. It builds the codec with changed `#define` values or context patches. It codes every file the way `mixz` does: the same stream, primer and primed snapshot. It reports the size change per kind.
- **17 parameter changes** (learning rates, mixer weights, limits): each moves the total by at most 0.33%. The best per kind is 0.1–0.4%, so none reaches 1%.
- **Shortest match** (`MINLEN`, 6 in codec 2), on png:
  - 5 gives −2.15%, 4 gives −3.15%, 3 gives −4.76%, 2 gives −5.85%;
  - 1 gives +0.87%;
  - elf gets worse below 5 (+0.67% at 3), so the setting has to be per profile.
- **A 9th context**, best per kind:
  - py −1.43% (indentation), c −1.31% (bracket nesting), js −1.45% (indentation or last punctuation);
  - json −0.90% (sparse), web −1.39% (HTML tag);
  - prose −0.33%, man −0.34%.
- **Replacing text contexts instead of adding one** keeps the number of contexts, so it costs no speed. 40 single replacements (8 slots × 5 contexts) and 17 combinations were measured. The best with 8 contexts:
  - py −1.64%, c −1.27%, js −1.89%, json −1.19%, web −1.73%;
  - no gain for prose and man.
- **The contexts:**
  - **indentation:** the line's leading spaces (a tab counts 4, capped at 31), whether the line is still inside its indentation, the line's first other byte, and the previous byte;
  - **bracket nesting:** the depth (capped at 15), the last 2 open brackets, and the last 2 bytes;
  - **last punctuation:** the last byte that is not a letter, digit or space, and the last 2 bytes;
  - **HTML tag:** a hash of the current tag name, whether the position is in the tag name or after it, and the last 2 bytes;
  - **sparse:** the 3rd and 4th bytes back.

**Speed of adding contexts** (CPU time, 1 core, best of 3 alternating runs on 2.1 MB of py and js; measured):

| Variant | CPU time against the text profile |
|---|---|
| 8 contexts, 3 replaced (the code profile) | +2.0% |
| 9 contexts | +10.1% |
| 11 contexts | +74% (above 9 contexts the mixer has no AVX2 path; with a 48-input AVX2 mixer about +30–35%, estimate) |

- 11 contexts gave py −2.64%, c −2.44%, js −3.51% on train.
- Not taken: the speed cost is far larger than the size gain. It could become a slower "max" preset later.

**Acceptance on the valid split** (candidates chosen on train; measured):

| Candidate | py | c | js | json | web | prose | man |
|---|---|---|---|---|---|---|---|
| code: indentation, last punctuation, bracket nesting in slots 0, 1, 6 | **−1.14%** | **−2.34%** | **−1.86%** | +0.10% | −1.47% | +0.50% | +0.19% |
| web: bracket nesting, HTML tag in slots 1, 7 | +1.27% | +0.96% | −0.18% | +0.02% | **−2.21%** | +0.40% | +0.10% |
| json: indentation, sparse in slots 0, 6 | −0.38% | −1.11% | −0.45% | +0.33% | +0.67% | +0.85% | +0.29% |

- **Image shortest match 2:** png −4.97%. With the same setting elf is +0.42%, so it applies to the image profile only.
- **Kept:** code (py, c, js), web, and shortest match 2 for images.
- **Rejected:** json (+0.33% on valid). Prose and man had no candidate that came close to 1%.

**What was built (codec 3):**
- **`engine/cm.c`:**
  - Engine profiles code (3) and web (4) are the text contexts with the slots above replaced.
  - The per-byte state those contexts read (indentation, brackets, tag, punctuation) is updated only for these two profiles.
  - The image profile's shortest match is 2 (`MINLEN_IMAGE`).
  - The engine now refuses unknown profile numbers.
- **Undefined behavior fixed:** the mixer's training step shifted a negative integer left, which is undefined in C. It was found with UBSan, a compiler check for undefined behavior. gcc gave the intended result, so the output did not change (the vectors pass unchanged).
- **Detector:** primer choice already picks the closest primer by zstd size, and that primer now also picks the sub-profile: py, c and js lead to code, web leads to web (`mixz.TEXT_SUBPROFILES`).
- **Registry:**
  - Codec 3, with profiles text/code and text/web under text, and 13 versions (23–35).
  - Codec 2 and versions 12–22 are retired, and the head restarted.
- **Vectors:**
  - Codec 3 vectors replace codec 2's, including its decode-only vector.
  - A generated HTML page was added, so all 5 engine profiles are covered.
  - They pass with gcc `-O3 -march=native`, with `-O0` (which uses the scalar mixer), and under UBSan.
  - The worker tests pass with `-DMZ_CHECK_RESTORE`.
- **Not checked yet:** the Pentium test machine (no AVX2), and clang (not installed on the desktop).

**Result on the harness test set** (quick tier, held out, against the step 4 baseline; measured):

| | py | c | js | json | web | prose | man | elf | png | total |
|---|---|---|---|---|---|---|---|---|---|---|
| codec 3 | 13.38% | 20.51% | 17.81% | 4.73% | 11.53% | 21.31% | 76.24% | 37.43% | 90.32% | 40.95% |
| change | −1.41% | −0.99% | −1.22% | −0.09% | −1.82% | +0.06% | +0.03% | 0.00% | −0.52% | −0.45% |

**Full tier** (3,815 files, 30.55 MB, held out; measured):

| | py | c | js | json | web | prose | man | elf | png | total |
|---|---|---|---|---|---|---|---|---|---|---|
| codec 3 | 14.51% | 19.80% | 15.45% | 7.48% | 13.12% | 21.02% | 70.73% | 30.88% | 87.37% | 30.93% |
| change | −1.33% | −1.03% | −1.71% | −0.28% | −1.47% | +0.17% | +0.10% | 0.00% | −0.97% | −0.65% |

- **Checks:** all 7,632 decompressions OK; 1 and 4 threads give identical output; `-O0` and `-O3` give identical output.
- **Engine speed A/B** (CPU time; spread 0.3–1.9%, so more reliable than the quick tier):
  - text +1.3% compress, +1.3% decompress;
  - binary +1.0% / +0.7%;
  - image +1.7% / +1.7%.
- **Pipeline speed:** the pipeline was faster than the baseline (+28% compress on 1 core). That comes from step 3, because the baseline (commit `af82acf`) predates steps 3 and 4. It is not an effect of codec 3. The baseline was recorded again after step 6 was accepted (section 3.5).
- **Memory:** free memory went from 5.77 GB to 3.48 GB at its lowest (measured).
- **Quick tier, above:**

- **json, prose and man change by at most 0.09%:** a few of their files pick the py, js or web primer and now get a sub-profile.
- **c gains less here than on valid** (−0.99% against −2.34%). So does **png** (−0.52% against −4.97%): the test PNGs are mostly photographs, where repeats are rare.
- **Engine speed A/B** (CPU time, same session; the run-to-run spread is 3–10%, so these are rough):
  - text +1.2% compress, +3.9% decompress;
  - binary +0.8% / +0.6%;
  - image +1.6% / +0.5%.
  - The cost comes from the per-byte state check and the per-profile shortest match. The code profile itself costs +2.0% against text (above).
- **Memory:** the quick tier lowered free memory from 7.55 GB to 5.27 GB at its lowest (measured).

**Problems on the way (fixed):**
- **The tuner leaked memory.** It never freed its engine worker (a full model, about 130 MB), so each pool process grew by one model per task. The desktop ran out of memory twice and VS Code crashed. Now:
  - workers are freed, and every task runs in a fresh process;
  - each worker has a 1.5 GB address-space limit;
  - the worker count fits the free memory, keeping a 4 GB reserve;
  - the run stops if free memory falls below 2 GB;
  - results are cached per task, so a stopped run resumes.
- **Variants with 10 or more contexts crashed** (42 crash reports). They overflowed two fixed sizes: the mixer's 32 inputs and mixer 3's 10-row selector table. The variant builder now sizes both and checks them when compiling. A crashing worker leaves no core dump, and the first crash stops the run.
- **A rented cloud machine was lost** with its results. The sweeps then ran on the desktop.

**Not built (left from step 6):**
- **An automatic search script:** the search above was a sequence of `variants.py` sweeps chosen by hand. A greedy search would make it repeatable: try each slot with each context, keep the best, repeat, then accept on valid.
- **Per-kind parameters** (learning rates and so on): the measured room is 0.1–0.4% per kind, below the 1% bar.
- **A 9-context "max" preset:** +10% CPU time for a further −0.3 to −0.6% (train).

### 2.9 Re-measured steps, per-file overhead, bundles, cores and blocks (2026-10-26)

**Why:** the request was every step's result against its target on one machine, the reason per-core speed is about 0.67 MB/s and not about 1.5 MB/s, a fix for the per-file overhead (target: within 20% of the model's speed), the reason 4 cores gave about 2×, and blocks measured on a file of at least 100 MB.

**Terms used here:**
- **Model speed:** a kind's files joined into one stream and coded as one file (`big` below). Per-file costs are then spread over 3 MB, so this is the model's own speed.
- **Warm:** the primed snapshots are already in memory, as in a running server. **Cold:** priming is included.
- **Restore:** putting a worker's model back to the primed state before the next file (section 2.5): from the write log for files up to about 25 KB, otherwise a full copy of the ~155 MB model.
- **Bundle:** several files in one container whose same-plan members are coded as one stream (below).

**Corrections to the request:**
- **Step 7 is not done** (it is in progress).
- **No step had a 1.5 MB/s target.** Step 2's target was "≥1.5× per core", and it was met. The 3–4× estimate for the whole plan (about 1.5–1.8 MB/s from the MVP's 0.46 MB/s) assumed step 5, which was dropped (section 2.7).
- **The pool/copy layer was never on:** step 5 was dropped and its code removed (`0a1402c`).
- **The harness has no log files.** A log set was added for these measurements (`tools/experiments/logs.json`): the 16 Loghub 2,000-line samples (4.46 MB; research use, cite Zhu et al., ISSRE 2023) and the NASA Kennedy Space Center HTTP log of July 1995 (205 MB). None shares a 64-byte string with a primer (measured).
- **The 0.16 MB/s and 2× figures were the whole harness set.** Its PNG and `.gz` files are slow per input byte (the model codes the decoded stream, several times larger, after a deflate parameter search). PNG is 46% of the full tier's 1-core time. The text kinds run at 0.43–0.68 MB/s per core.

**Per step** (`tools/experiments/steps.py`: each commit from its own worktree, same session, the Ryzen 3 1200 desktop; warm; MB of input per second; measured):

| Step | Commit | Python size | Python 1 core c / d | Python 4 threads c / d | JSON 1 core c / d | JSON 4 threads c / d | Logs 1 core c / d | Logs 4 threads c / d | Model speed py / json / logs |
|---|---|---|---|---|---|---|---|---|---|
| MVP | `0696daa` | 14.73% | 0.131 / 0.212 | 0.39 / 0.59 | 0.144 / 0.213 | 0.28 / 0.37 | 0.400 / 0.436 | 1.38 / 1.52 | 0.430 / 0.448 / 0.430 |
| 0 harness | `e7a232a` | 14.73% | 0.210 / 0.219 | 0.60 / 0.68 | 0.206 / 0.216 | 0.54 / 0.57 | 0.455 / 0.449 | 1.53 / 1.61 | 0.426 / 0.463 / 0.470 |
| 1 format 2 | `4a54acc` | 14.72% | 0.211 / 0.219 | 0.61 / 0.70 | 0.206 / 0.216 | 0.56 / 0.58 | 0.456 / 0.445 | 1.55 / 1.60 | 0.432 / 0.462 / 0.468 |
| 2 mixer | `af82acf` | 14.71% | 0.261 / 0.269 | 0.68 / 0.68 | 0.296 / 0.316 | 0.77 / 0.82 | 0.667 / 0.674 | 2.26 / 2.34 | 0.683 / 0.728 / 0.737 |
| 3 loaded model | `e161887` | 14.71% | 0.509 / 0.552 | 1.53 / 1.71 | 0.392 / 0.398 | 1.27 / 1.34 | 0.671 / 0.687 | 2.35 / 2.44 | 0.687 / 0.726 / 0.711 |
| 4 blocks | `c23c5d6` | 14.71% | 0.508 / 0.561 | 1.52 / 1.73 | 0.393 / 0.399 | 1.25 / 1.28 | 0.692 / 0.670 | 2.34 / 2.48 | 0.690 / 0.728 / 0.732 |
| 5 copy layer | dropped | — | — | — | — | — | — | — | — |
| 6 profiles | `ff28c4f` | 14.51% | 0.497 / 0.539 | 1.51 / 1.70 | 0.389 / 0.396 | 1.23 / 1.27 | 0.681 / 0.680 | 2.23 / 2.39 | 0.680 / 0.724 / 0.723 |
| this work | uncommitted | 14.51% | 0.481 / 0.545 | 1.47 / 1.66 | 0.620 / 0.640 | 1.79 / 1.87 | 0.679 / 0.674 | 2.15 / 2.34 | 0.675 / 0.721 / 0.722 |

JSON size: 7.53% (MVP) → 7.48% (step 6). Logs: 4.77% → 4.72%. 4-thread numbers are wall clock on a desktop with other programs running (±10%).

**Targets** (section 6):

| Step | Target | Result | Met |
|---|---|---|---|
| 0 | Same numbers on every run | Sizes identical on every run (tested); speed noise ±10% wall clock, ±5% CPU time, so decisions use same-session A/B | yes |
| 1 | Old (v1) and new files both decompress | Met at `4a54acc`; format 1 then dropped by decision (section 2.4) | yes |
| 2 | ≥1.5× per core, ≤0.5% larger | Model 1.57–1.60× (table), output 0.06% smaller | yes |
| 3 | Start-up 1.5 s → <0.1 s | 85–105 ms per call with a server; 25 KB files ~140 ms (section 2.5). Files alone 1.95× faster on Python | nearly |
| 4 | ≥1.8× on 2 cores, ≤4% larger at 4 blocks | 1.86× compress, 1.75× decompress on 2 cores; +6.6% at 2 MiB blocks on 3 MB files, +1.9% at 4 blocks on a 205 MB log | no (size) |
| 5 | ≥2× decompression | Dropped: copies cost more bits than the model (section 2.7) | — |
| 6 | Sub-profile kept only if ≥1% better | Code −1.1% to −2.3%, web −2.2% on valid; json rejected | yes |
| 7 | ≥25% smaller than zstd + dictionary per text profile | Not done. With today's (unlicensed) primers: py 26.8%, c 26.1%, js 26.2%, json 31.8%, web 27.2%, prose 24.2% smaller | in progress |

**Which steps underdelivered against their estimates** (measured):
- **Step 2:** the estimate was 1.5–2×; the result was the low end, because the `-O3 -march=native` build was already vectorized (section 2.2).
- **Step 5:** it was the step meant to take per-core speed from about 0.7 to about 1.5 MB/s, by not modelling repeated bytes. Measured: copies cost more bits than the model spends on repeats, so it was dropped.
- **Step 4:** speed met, size missed (blocks below).
- **The model has no cheap part left** (perf on the real build, 1 MB of Python, measured): 11,113 instructions per byte at 2.34 instructions per cycle and 3.34 GHz. Updating is 47% of the time and predicting 45%; within them the time is spread over the context lookups, the mixer, the state updates and the correction stages. One instance is limited by instructions, not memory: four at once take only 4% more cycles for the same work. Reaching 1.5 MB/s needs about 2.2× fewer instructions per byte, which means less model work per bit (fewer contexts or mixers, "fix C", which the author wants to avoid) or a different model.

**Per-file overhead** (1 core, warm; seconds of a 3.15 MB pass; measured):

| Kind | Files | Median size | Pass | Primer choice | Re-priming | Coding incl. restore | Model alone |
|---|---|---|---|---|---|---|---|
| Python | 217 | 6.5 KB | 6.33 s | 0.55 s | 0.00 | 5.64 s | 4.63 s |
| JSON | 192 | 3.4 KB | 8.08 s | 0.17 s | **3.10 s** | 7.83 s (incl. re-priming) | 4.35 s |
| Logs | 16 | 280 KB | 6.55 s | 0.11 s | 0.00 | 6.39 s | 6.17 s |

- **JSON re-primed in the warm pass.** Its files use 5 snapshot keys (json primer at 2 size classes, py primer at 2, js primer), and the in-process cache held 3 (`SNAP_CACHE`), so rare keys evicted the main one and it was primed again (~0.8 s each). A server keeps 8 and was not affected. **Fix: `SNAP_CACHE` 3 → 8** (no output change; ~155 MB per snapshot actually used). JSON files alone: 0.389 → 0.620 MB/s. The harness quick tier's 1-core pipeline: 0.115 MB/s at the af82acf baseline → 0.200 MB/s (+74%; step 3 alone gave +23%).
- **Python's remaining cost** is restore (~1.0 s: 184 files ≤25 KB restore from the log, the 33 larger ones copy 155 MB) and primer choice (~0.55 s, 8 zstd trials per file).
- **Copy-on-write snapshots do not help** (`tools/experiments/restore_pages.c`, code profile, py primer; measured): a 4 KB file writes into 47% of the context tables' 4 KB pages and a 24 KB file into 87%; the match table and run tables 87–100%. Copying pages on first write would copy most of the model, plus a page fault per page. The same 24 KB file changes only 0.9% of the context buckets, but its log records each one ~12 times.
- **A deduplicated log was tried and reverted:** one bit per table entry so each entry is logged once. It made every kind slower (Python 1 core 0.481 → 0.463 MB/s, 4 threads 1.466 → 1.284, logs 0.679 → 0.629; measured), because files no longer filled the log early and stayed on the slower logged path for their whole length.
- **Full copies compete for memory bandwidth:** one 155 MB copy takes 12.2 ms alone and 43–45 ms with four at once (measured).

**Bundles** (built; `mixz.compress_bundle`, `decompress_bundle`, `parse_bundle`; format 2, method 2; no engine output change):
- **What it does:** each member is planned as a single file would be. Members with no transform and the same engine profile and primer form a group, joined into one model stream: the model is primed and restored once per group, not once per file, and each member is predicted from the ones before it. Other members (PNG, gzip, x86 programs, stored data) are complete mixz files inside the bundle.
- **Reading one member** decodes its group only up to that member's end. The engine gained `cm_decompress_part` and `cm_decompress_primed_part`, which size the model for the whole stream and stop after n bytes. The old functions call them; outputs and vectors are unchanged (tested), and `engine_sha256` and `heads` were updated as the pre-release rule says.
- **Tested** (`tools/test_roundtrip.py`): round trip of 29 mixed members, every member alone, empty bundle, same output with 1 and 4 threads, a bundle inside a bundle rejected, 300 truncations and bit flips rejected or harmless. Header layout is in `mixz.py`.
- **Measured** (1 core, warm, one bundle per kind; files alone with the new cache):

| Kind | Model speed | Files alone | One bundle | Size, files alone → bundle | 4 bundles, 4 threads (c / d) |
|---|---|---|---|---|---|
| Python | 0.675 | 0.481 (71%) | 0.592 (88%) | 14.51% → 13.54% | 1.88 / 2.26 |
| JSON | 0.721 | 0.620 (86%) | 0.944 | 7.48% → 6.18% | 2.10 / 2.14 |
| Logs | 0.722 | 0.679 (94%) | 0.701 (97%) | 4.72% → 4.72% | 2.23 / 2.14 |
| Web | 0.710 | 0.451 (64%) | 0.814 | 13.12% → 7.72% | 1.67 / 2.15 |
| C | 0.663 | 0.434 (65%) | 0.666 (100%) | 19.80% → 15.59% | 1.49 / 1.86 |
| JavaScript | 0.683 | 0.584 (86%) | 0.853 | 15.45% → 12.65% | 1.81 / 2.21 |
| Prose | 0.656 | 0.429 (65%) | 0.714 | 21.02% → 19.73% | 1.53 / 2.01 |

- **Result:** bundles meet the target (within 20% of model speed) on every text kind; files alone meet it on JSON, logs and JavaScript only. Some bundles are faster than one stream: a bundle splits into groups by primer, each with a smaller match buffer (estimate).
- **The cost of a bundle:** reading one member decodes its group up to that member, on average half the bundle: about 2 s for a member in the middle of a 3 MB bundle (estimate from model speed), against ~20 ms for a file of its own. The bundle size trades output size and speed against that delay. Not decided: bundle size.
- **Not built:** a CLI command for bundles (no member names are stored; a caller keeps its own index), gzip members inside groups.

**Why 4 cores gave about 2×** (measured):
- **On the text kinds it is 2.9–3.3×,** not 2×. The whole-set figure is dominated by PNG, which scales 1.7×: a few large images (the largest is 18% of the PNG bytes) and the per-file deflate search.
- **The GIL costs 4–15%** on Python, JSON and logs (4 threads against 4 processes): Python 1.47 against 1.53 MB/s, JSON 1.79 against 2.05, logs 2.15 against 2.25. Web 23% (1,257 small files).
- **Clock speed:** with 4 cores busy the CPU runs at about 2.96 GHz instead of 3.34 (−11%).
- **Memory:** the engine alone needs 4% more cycles per byte with four instances; full restores are 3.6× slower with four at once (bandwidth). With threads, `cm_encode` takes 27–55% more CPU time per pass than on 1 core.
- **Engine alone:** four instances of the speed tool each run at 0.57–0.64 MB/s against 0.67 alone, about 3.6× in total.

**Blocks on large files** (`tools/experiments/blocks.py --file`; measured):

| File | One block | 2 blocks | 3–4 blocks | 6–7 blocks | 13 blocks | 49 blocks |
|---|---|---|---|---|---|---|
| NASA log, 205 MB | 3.16% | — | +1.92% (4 × 51 MB) | +3.19% (7 × 29 MB) | +5.27% | +12.49% |
| enwik8, 100 MB | 20.38% | +0.92% (2 × 50 MB) | +1.63% (3 × 33 MB) | +3.11% (6 × 17 MB) | — | — |

- Speed with 4 threads: NASA 4 blocks 2.66 MB/s compress, 2.60 decompress (about 3.8× one core); enwik8 3 blocks 1.95 / 1.91 MB/s.
- Each halving of the block size multiplies the cost by 1.6–1.9. Staying within 0.5% needs blocks of about 512 MiB (estimate, extrapolated).
- **Built:** with no `--block-size`, mixz chooses blocks from the stream length (`auto_block_size`): one block below 1 GiB, above it blocks of at least 512 MiB (and at most 64 blocks). Small files are never split. No file in the harness or the vectors changes.
- **The author's decision:** smaller blocks for large files cost more than 0.5%: blocks of 29–51 MB cost +0.9% to +3.2% in the table above, for 2.8–3.8× faster coding of one large file on 4 cores.

**Harness quick tier after this work** (measured): output identical to step 6 (total 40.95%); pipeline 1 core compress 0.200 MB/s (+74% against the af82acf baseline), decompress 0.284 MB/s (+104%); 4 cores 0.499 / 0.623 MB/s; engine A/B +0.7% to +2.7% (codec 3's cost, section 2.8); verdict OK.

**Found on the way:** mixz has no log primer, so every log file picks the Python primer and is coded with the code profile (measured on all 17 log files).

---

## 3. Measured results

### 3.1 Compression (held-out files, each compressed alone, every output round-tripped OK)

| Files | n | MB | gzip -9 | xz -9e | zstd -19 | zstd -19 +dict | brotli -q11 | mixz no primer | mixz |
|---|---|---|---|---|---|---|---|---|---|
| Python | 60 | 1.11 | 22.9% | 21.4% | 21.4% | 18.6% | 19.4% | 16.5% | 13.4% |
| C headers | 60 | 1.09 | 14.9% | 13.9% | 13.9% | 11.0% | 12.2% | 10.4% | 7.3% |
| JavaScript | 34 | 3.23 | 30.3% | 23.9% | 24.9% | 24.5% | 23.7% | 19.6% | 19.1% |
| JSON | 60 | 1.95 | 13.9% | 10.0% | 9.9% | 8.7% | 9.2% | 7.2% | 5.8% |
| HTML/CSS/XML/SVG | 60 | 0.24 | 28.8% | 28.5% | 27.7% | 17.7% | 23.9% | 23.1% | 11.8% |
| Prose | 60 | 0.22 | 25.6% | 26.4% | 24.7% | 16.6% | 20.4% | 20.5% | 11.9% |
| Man pages (.gz) | 60 | 0.46 | 100.3% | 100.8% | 100.1% | 100.1% | 100.1% | 71.9% | 55.1% |
| x86-64 ELF | 27 | 3.09 | 37.8% | 31.3% | 33.3% | 30.8% | 31.5% | 25.0% | 23.4% |
| PNG | 60 | 1.64 | 97.7% | 97.3% | 97.4% | 96.8% | 97.3% | 89.0% | 89.0% |
| All | | 13.1 | 38.5% | 34.6% | 35.3% | 33.5% | 33.9% | 28.7% | 26.5% |

Raw numbers: `results/bench.json` in the development repository (not published: it lists the test machine's system file paths). The corpus split depends on which files exist on the machine. Installing `pyflakes` mid-session changed the Python split, so Python numbers from different days are not directly comparable.

**Recompression coverage on this VM:**
- 79% of 300 system `.gz` files were reproducible with zlib (228 at level 9 with memLevel 8, and 9 at level 6).
- 31% of 250 PNGs (76) were reproducible.

### 3.2 Primers

**Method comparison** (40 held-out files, earlier corpus state):

| Primer | Python | ELF |
|---|---|---|
| none | 17.56% | 27.30% |
| concat 128 KB | 15.07% | 26.18% |
| concat 512 KB | 14.24% | 25.40% |
| cover 128 KB | 14.32% | 25.74% |
| cover 512 KB | 13.67% | 25.14% |

**Scaling** (40 held-out Python files, 573 KB, concat primers, later corpus state):

| Primer | Output size |
|---|---|
| none | 19.18% |
| 512 KB | 14.89% |
| 2 MB | 14.07% |
| 8 MB | 13.19% (maxbits 22) / 13.30% (maxbits 20) |

Each 4× more knowledge gave about 6% smaller output and has not levelled off. Priming 8 MB took about 20 s, so a large mind must stay loaded in memory.

### 3.3 Speed

Machine: Intel Xeon @ 2.1 GHz, 2 cores, L2 4 MB, AVX2 + AVX-512.

**mixz, one core:**
- About 2,059 ns per byte (2 MB Python, no primer, maxbits 20, `-O3 -march=native`), which is about 4,300 cycles per byte.
- The 0.4–0.5 MB/s overall figure is the same in both directions.

**Other tools (1 MB text):**

| Tool | Compress | Decompress |
|---|---|---|
| xz -9e | 2.8 MB/s | 87 MB/s |
| zstd -19 | 3.3 MB/s | 1,086 MB/s |
| brotli -q11 | – | 465 MB/s |

**Instructions per byte (cachegrind, 200 KB):**
- mixz: about 14,850.
- gzip -d: about 24.
- xz -d: about 54.

**Where mixz's instructions go** (`-O2 -fno-inline` build, so shares are approximate):

| Function | Share | What it is |
|---|---|---|
| train | 36.0% | mixer weight updates |
| dot | 19.0% | mixer prediction |
| predict | 12.1% | |
| sm_update | 9.0% | |
| squash | 6.4% | |
| model_update | 5.7% | |
| apm_init | 2.3% | |
| hmix | 1.7% | |
| sm_p | 1.7% | |
| stretch | 1.3% | |
| apm_pp | 1.0% | |
| bucket_find | 0.7% | |

**Table-size sweep (maxbits, 2 MB Python):**

| maxbits | ns per byte | Output size |
|---|---|---|
| 16 | 1,940 | 15.10% |
| 18 | 2,105 | 14.93% |
| 20 | 2,059 | 14.89% |
| 22 | 2,436 | 14.88% |

Memory is secondary: the cost is mostly arithmetic.

**Memory latency** (dependent random reads):

| Table size | Latency |
|---|---|
| 32 KB | 2.1 ns |
| 256 KB | 5.2 ns |
| 2 MB | 17.6 ns |
| 8 MB | 32.7 ns |
| 32 MB | 120 ns |
| 128 MB | 144 ns |

**Start-up:**
- A 28 KB file takes 1.73 s with a primer and 0.17 s without.
- Python start plus loading the engine takes 0.05 s.
- Peak RSS for one file is about 170 MB.
- Batch mode with snapshots compressed 60 Python files in 5.1 s on 2 cores. Replaying the primer per file would take about 45 s (estimate).

**Blocks on 2 cores (2 MB Python):**

| Setup | Speed | Output size |
|---|---|---|
| no primer, 1 block | 0.46 MB/s | 14.89% |
| no primer, 2 blocks | 0.75 MB/s (1.6×) | 15.34% |
| no primer, 4 blocks | 0.77 MB/s | 16.10% |
| py primer, 1 block | – | 14.07% |
| py primer, 4 blocks | 0.91 MB/s | 14.63% |

Primers halve the size cost of splitting: +4% with a primer against +8% without. (Step 4 measured much larger costs on archive-like files, for example +13.7% for web pages at 2 MiB blocks: section 2.6.)

**Repeat coverage** (greedy LZ parse, share of bytes inside repeats, each file alone; the last column adds the primer as a dictionary):

| Type | ≥8 B | ≥16 B | ≥32 B | ≥16 B with primer | Copy instructions per KB (with primer) |
|---|---|---|---|---|---|
| py | 77.8% | 54.3% | 30.7% | 58.8% | 20 |
| c | 94.9% | 90.5% | 76.3% | 91.9% | 18 |
| js | 68.9% | 45.5% | 26.1% | 47.2% | 15 |
| json | 87.7% | 72.1% | 55.4% | 78.5% | 17 |
| web | 63.0% | 51.4% | 38.3% | 75.2% | 14 |
| prose | 73.4% | 58.1% | 40.9% | 72.1% | 17 |
| elf | 57.9% | 31.2% | 14.8% | 34.7% | 11 |

**Estimate from coverage, falsified by step 5:** a hybrid that keeps the full model for non-repeated bytes was estimated to speed up decompression by 1.4–6.5×, assuming a copy costs about nothing in bits. CM spends fewer bits on these repeats than a copy does (section 2.7).

### 3.4 Why it is slow (summary)
1. **One prediction per bit, strictly sequential.** Bit n+1 depends on bit n and on what the model just learned.
2. **About 15,000 instructions per byte.** The mixer is about 55% of them.
3. **Symmetric.** The decoder must re-run the identical model, unlike LZ decoders, which only follow copy instructions. This causes the roughly 2,500× decompression gap against zstd and is inherent to CM.
4. **Primer replay.** About 1.5 s per command.
5. **Overhead outside the model on small files** (found 2026-10-23, section 3.5). Primer choice and primer replay took more time than the model itself.

### 3.5 Benchmark harness on the Ryzen desktop (2026-10-23)

Sections 3.1–3.4 were measured on the VM. This section is measured with the plan step 0 harness (`tools/harness/`) on the Ryzen desktop.

**Machine:**
- AMD Ryzen 3 1200: Zen 1, 4 cores, no SMT, AVX2, no AVX-512, at most 3.3 GHz with boost off, `schedutil` governor.
- 16 GB RAM, Arch Linux, gcc 15.2, Python 3.13.
- It is a desktop with other programs running. They hold a pinned core for 13–15% of the wall time (measured). For that reason the harness measures engine speed in CPU time, the time the process itself ran.

**Test set** (`tools/harness/sets.json`):
- Downloaded from 41 pinned sources with known licenses.
- Checked by content against the primers: a file is dropped if more than 20% of its bytes lie inside 64-byte strings of a primer.
- Quick tier: 938 files, 6.37 MB. Full tier: 3,815 files, 30.55 MB.

**Compression, full tier** (measured; identical before and after the overhead fixes below):

| Kind | Files | MB | gzip -9 | xz -9e | zstd -19 | zstd -19 + primer as dict | brotli -q11 | mixz | mixz against the best other |
|---|---|---|---|---|---|---|---|---|---|
| Python | 217 | 3.15 | 24.5% | 23.5% | 23.2% | 19.8% | 20.9% | 14.73% | −25.6% |
| C (sources and headers) | 905 | 3.15 | 33.0% | 32.2% | 31.1% | 26.8% | 28.7% | 20.08% | −25.1% |
| JavaScript | 87 | 3.15 | 25.1% | 21.5% | 22.1% | 21.0% | 20.7% | 15.74% | −24.0% |
| JSON | 192 | 3.15 | 15.3% | 11.6% | 11.8% | 11.0% | 10.2% | 7.53% | −26.2% |
| HTML/CSS/XML/SVG | 1,257 | 3.15 | 23.0% | 23.9% | 21.6% | 18.0% | 19.2% | 13.40% | −25.6% |
| Prose | 221 | 3.15 | 33.0% | 31.0% | 30.8% | 27.7% | 27.5% | 21.01% | −23.6% |
| Man pages (.gz) | 659 | 1.50 | 101.0% | 102.7% | 100.4% | 100.4% | 100.2% | 70.71% | −29.4% |
| x86-64 ELF | 10 | 5.98 | 45.9% | 38.3% | 40.9% | 40.7% | 39.0% | 30.91% | −19.3% |
| PNG | 267 | 4.19 | 97.7% | 97.9% | 97.6% | 97.6% | 97.5% | 88.32% | −9.4% |
| All | 3,815 | 30.55 | 43.2% | 40.8% | 40.8% | 39.1% | 39.0% | 31.18% | −20.1% |

- **How to read the last column:** "−20.1%" means mixz output is 20.1% smaller than the best other tool's output.
- **Comparison with the VM table:** on the VM, mixz was 21% smaller than the best other tool (26.5% against 33.5%). On clean, licensed data it is 20% smaller. The advantage holds.
- **Why per-kind numbers differ from the VM table:** the content differs. For example, the VM's C set was only headers (7.3%); here it is mostly C sources (20.1%).
- **Recompression on this set:**
  - Man pages: 8 of 104 quick-tier `.gz` files could not be rebuilt by zlib and were stored as they were.
  - PNG: the Kodak photos could not be rebuilt and were stored. The icons, screenshots and figures could: 54–95% each.

**Speed of the MVP as handed over** (baseline commit `31c35ff`, measured):
- **Engine alone,** one core, no primer, 512 KiB inputs; compress and decompress take the same time:
  - text: about 2,800 ns/byte wall-clock, about 2,450 ns/byte CPU time;
  - binary: about 3,200 wall-clock, 2,780 CPU time;
  - image: about 2,450 wall-clock, 2,200 CPU time.
- **Whole pipeline,** full tier, through `mixz.compress` with snapshots. This includes primer replay, primer choice and detection.
  - 1 core: compress 0.065 MB/s, decompress 0.096 MB/s.
  - 4 cores: compress 0.116 MB/s (1.8×), decompress 0.155 MB/s (1.6×).
- **Where the 1-core compress time went:**
  - Full tier: model coding 44%, primer choice 31%, primer replay 19%, detection and transforms 6%.
  - Quick tier, smaller files: primer replay 32%, primer choice 25%, model coding 34%.
- **Start-up,** CLI, 25 KB Python file with primer: compress 1.66 s, decompress 1.61 s. The VM measured 1.73 s.

**Overhead fixes** (commit `c48f8a8`, measured on the quick tier against the MVP baseline). Outputs were byte-identical on all 938 of 938 files.
- **Primer choice:** `choose_primer` built a new zstd compressor for each of its 8 trials per file. Each new compressor indexes the 512 KB primer again. Compressors are now reused per primer and per thread. Primer choice fell from 25% to 1% of 1-core compress time.
- **Snapshot priming:** it ran under one global lock, so threads that needed different primers waited for each other. It now runs outside the lock, with a per-snapshot wait.
- **Results:** the MVP's `mixz.py` and the fixed one were run alternately in one session, with the same engine library, compressing the quick tier. Each was run twice, and the two rounds agreed within 0.3%.

| Measure | MVP | Fixed | Change |
|---|---|---|---|
| Compress, 1 core (CPU time) | 99.6 s | 71.3 s | 1.40× (+40%) |
| Compress, 4 cores (wall clock) | 56.2 s | 29.5 s | 1.91× (+91%) |
| Compressed size (total) | 2,623,474 B | 2,623,474 B | identical |

- **Decompression:**
  - 4 cores: about +55%, from separate runs against the MVP baseline, so less certain.
  - 1 core: unchanged, since neither fix is on that path.
- **Separate runs against a stored baseline mislead by up to ±10% on this machine.** The MVP full-tier baseline was recorded while the machine was busy (load 4.27, swap 70% used). Against it, 1-core decompress later read +27%, although that code path did not change. This is why the harness uses CPU time and same-session A/B runs (see "Measurement noise" below).
- **Parallel efficiency after the fixes:** 4 cores compress the quick tier 2.42× faster than 1 core (wall clock). The 4-thread run uses 24% more CPU time in total than the 1-thread run. A likely cause is cores competing for memory and the shared cache (estimate).
- **What remains:** after the fixes, primer replay is about 43% of 1-core compress time on the quick tier (28% on the full tier), and model coding 44% (62%). Plan step 3 (model kept loaded) removes the replay, and steps 2 and 5 cut the coding.

**Current baseline** (commit `e7a232a`, after the fixes; the reference for step 1; full tier, measured):
- Engine alone, CPU time: text 2,280 ns/byte, binary 2,580, image 2,110.
- Pipeline:
  - 1 core: compress 0.112 MB/s, decompress 0.123 MB/s.
  - 4 cores: compress 0.232 MB/s, decompress 0.245 MB/s.
- Start-up (CLI, 25 KB file): 1.53 s compress, 1.47 s decompress.
- These speeds are wall clock from one run, so they are only as exact as the noise allows (see below).

**Measurement noise on this machine** (measured):
- **Engine, wall clock:** runs spread up to 15%. An A/B of two identical engines showed a false 11.6% difference.
- **Engine, CPU time:** runs spread under 2%, and identical engines agree within 1%. The harness therefore uses CPU time. When the engine changed, it also runs the old and new engines alternately in the same run.
- **Pipeline, wall clock:** varies by up to ±10% between runs.
- **Noise check** (a run with no code change against the new baseline, measured):
  - 1-core pipeline on CPU time: +1.3% / +0.8%.
  - Start-up on CPU time: −0.1% / +0.6%.
  - 4-core pipeline on wall clock: +9% / +7%.
- **CPU-time engine numbers drift too:** about ±5% between runs made at different times, because other programs compete for the memory system. This is why the harness compares engines only in A/B runs within one session.

**Quick-tier baseline for step 2** (commit `4a54acc`, step 1; recorded 2026-10-24 at load 0.9; measured):
- Engine alone, CPU time: text 2,345 ns/B, binary 2,634, image 2,157.
- Pipeline, 1 core, CPU time: compress 80.9 s, decompress 70.5 s for the 6.37 MB quick tier.
- Start-up: 1.58 s compress, 1.54 s decompress.
- The 1-core pipeline read about 10% slower than the old baseline of commit `e7a232a`, although the engine source is identical.
  - Cause, measured: almost all of it is kernel time. Coding 248 small files from a snapshot took 6.0–6.5 s of user time and 12.8–15.9 s of system time, and the system time grew across rounds of identical work.
  - Linking zlib into the same library cost about 5% of user time on this path, with an identical engine source (old and new library alternated, 3 rounds; likely code layout, estimate).

**Baselines for step 3** (commit `af82acf`: codec 2, codec 1 dropped; recorded 2026-10-24 at load 0.7–3.0; measured):

| | Quick tier (6.37 MB) | Full tier (30.55 MB) |
|---|---|---|
| Size, total | 41.14% (−0.06% against `4a54acc`) | 31.13% (−0.14% against `e7a232a`; every kind smaller) |
| Engine alone, CPU time (text, binary, image) | 1,465 / 1,553 / 1,409 ns/B | 1,464 / 1,578 / 1,412 ns/B |
| Pipeline, 1 core, compress / decompress | 0.115 / 0.140 MB/s | 0.148 / 0.164 MB/s |
| Pipeline, 4 cores (wall clock) | 0.253 / 0.291 MB/s | 0.289 / 0.303 MB/s |
| 1-core compress time: priming / coding / detection | 33% / 49% / 16% | 22% / 64% / 11% |
| Start-up, CLI, 25 KB file | 1.01 s / 0.95 s | 1.03 s / 1.00 s |

- The earlier step 2 A/B (0.088 MB/s compress, section 2.2) ran at load 5–6. These baselines ran on a quiet machine, so the two are not comparable.

**Where the pipeline's time goes on small files (found in step 2, measured):**
- For every file, the snapshot clone copies the whole primed model, about 155 MB at the default table size, into freshly allocated memory.
- The kernel time this costs depends on whether the C allocator reuses freed memory or hands it back to the kernel, which makes it vary:
  - Calling the engine directly on 248 small files: about 15 s of kernel time against about 6 s of user time.
  - Whole quick tier, 1 core: compression 15.8 s (codec 1) and 19.2 s (codec 2) of kernel time, against 53–77 s of user time. Decompression about 0–2 s.
- Kernel time is outside the engine, so it limits how much of codec 2's gain reaches compression (1.29× in total against 1.46× in user time). Reusing the clone's memory between files avoids it with no output change. Done in step 3 (section 2.5): kernel time fell from about 14 s to 0.2 s per quick-tier pass.


### 3.6 Public release without primers; paq8px and zpaq (2026-11-01)

**Why:** the first public release ships without the development primers, which contain third-party files (`primers/README.md`). Its numbers are measured without primers, against the strongest open context-mixing compressors as well as the common tools. A reviewer's comparison (`results/compare/reviewer-xeon-vm.json`) prompted the paq8px and zpaq columns.

**Reference tools** (`tools/harness/reference.py`; measured):
- paq8px -8 (v217, GPL-3.0) and zpaq -m5 (libzpaq 7.15, public domain), built from pinned, sha256-checked downloads with `-march=native`. Neither is part of the repository.
- paq8px: 10,040 CPU seconds for the full tier's 30.55 MB, about 3 KB/s, including its start-up for each of the 3,815 files. Up to 2.4 GB per process (its help text; 2.2 GB measured). The full tier took 2 h 6 min with two processes at a time.
- zpaq: 246 CPU seconds (124 KB/s), 102 MB peak. Its streaming format stores about 290 bytes of model description in every file: a 1-byte input gives 291 bytes (an empty input gives 0).
- `reference.py` starts another process only if all running ones at their peak would leave 4 GB free. It used to subtract each running process's whole peak from a free-memory figure that already excluded what those processes held, which allowed only one paq8px at a time; it now subtracts only what each can still grow.

**Full tier, each file alone** (3,815 files, 30.55 MB; `results/harness/<machine>/baseline-full.json`):

| Kind | mixz | brotli -q11 | zpaq -m5 | paq8px -8 | mixz against paq8px |
|---|---|---|---|---|---|
| py | 18.36% | 20.9% | 20.8% | 15.7% | +16.6% |
| c | 24.98% | 28.7% | 33.5% | 21.6% | +15.4% |
| js | 16.92% | 20.7% | 17.9% | 13.1% | +29.4% |
| json | 8.51% | 10.2% | 10.6% | 6.4% | +32.0% |
| web | 17.54% | 19.2% | 28.7% | 14.7% | +19.5% |
| prose | 24.24% | 27.5% | 26.5% | 21.5% | +12.5% |
| man (`.gz`) | 88.22% | 100.2% | 112.8% | 100.7% | −12.4% |
| elf | 31.06% | 39.0% | 32.8% | 23.7% | +31.1% |
| png | 87.37% | 97.5% | 98.5% | 95.3% | −8.3% |
| total | 33.80% | 39.0% | 39.7% | 32.3% | +4.8% |

- On the 7 kinds `mixz` does not recompress, together: `mixz` 21.47%, paq8px 17.49% (`mixz` 22.7% larger).
- `mixz` is smaller than zpaq on every kind (5.3–38.8%) and than the best common tool on every kind (8.4–18.9%).

**One stream per kind** (`tools/experiments/compare_reference.py`: the quick tier's py, js, json, web and prose, and the 16 Loghub samples; `results/compare/<machine>-no-primers.json`; measured):
- Overall, files alone against one stream: `mixz` 9.2% and 7.8% (bundle), zpaq 11.2% and 8.2%, paq8px 7.5% and 6.0%.
- A `mixz` bundle is smaller than zpaq on one stream on every kind (0.6–19.0%) and larger than paq8px on one stream on every kind: py +26.4%, js +29.1%, json +71.1%, web +36.5%, prose +16.1%, logs +38.1%; +30.4% overall.
- CPU time to compress all six sets as one stream (7.08 MB): `mixz` 10.1 s, zpaq 18.9 s, paq8px 1,024 s.

**Speed on one input** (`tools/experiments/speed_compare.py`: the quick tier's 38 Python files joined, 0.52 MB, one core, CPU time; `results/compare/<machine>-speed.json`; measured):
- `mixz` 0.69 MB/s compress, 0.67 MB/s decompress (1,484 ns/B), through its Python interface.
- paq8px -8 0.007 MB/s each way: 104 times `mixz`'s compress time and 101 times its decompress time. zpaq -m5 0.39 / 0.38 MB/s.
- zstd -19 decompresses at 951 MB/s (1.1 ns/B), 1,410 times faster than `mixz`. gzip -9 334 MB/s, xz -9e 129 MB/s, brotli -q11 419 MB/s.

**Baselines of the public copy** (no primers; recorded 2026-11-01 with no other heavy programs running: load average 0.7 before the quick-tier run, 2.2 at the start of the full-tier run, left from the quick run; measured):

| | Quick tier (6.37 MB) | Full tier (30.55 MB) |
|---|---|---|
| Size, total | 43.38% | 33.80% |
| Engine alone, CPU time, compress (text, binary, image) | 1,429 / 1,539 / 1,406 ns/B | 1,426 / 1,540 / 1,407 ns/B |
| Pipeline, 1 core, compress / decompress | 0.226 / 0.352 MB/s | 0.255 / 0.318 MB/s |
| Pipeline, 4 cores (wall clock) | 0.764 / 1.088 MB/s | 0.565 / 0.655 MB/s |
| 1-core compress time: coding / detection and transforms | 61% / 38% | 76% / 22% |
| Start-up, CLI, 25 KB file | 0.13 s / 0.12 s | 0.13 s / 0.13 s |

- Without primers there is no priming and no primer choice, so a server saves nothing: 0.12–0.13 s per command with and without it.
- The saved full baseline combines two runs of the same code on the same set with byte-identical outputs: speeds from the quiet run (20261101-173753) and the paq8px/zpaq columns from a later run (20261101-202629) whose speeds other programs disturbed (engine about 6% slower, 4-core pipeline 33% slower in wall-clock time).

**The reviewer's run with the development primers** (Intel Xeon VM, one process at a time, wall clock; `results/compare/reviewer-xeon-vm.json`; measured by the reviewer):
- Each file alone, `mixz` against paq8px -8: Python −7.9%, web −26.6%, prose −6.5%; JavaScript +5.9%, JSON +31.9%, logs +31.9%.
- paq8px -8 given `mixz`'s primer (its output for primer and files minus its output for the primer alone): 22.5% smaller than a `mixz` bundle on Python, 29.9% on JSON.
- The json, web and prose sets there are other subsets of the harness files than this section's, so only py, js and logs are directly comparable.

---

## 4. Correctness and safety

- **Round-trip suite** (`tools/test_roundtrip.py`, **tested**):
  - edge cases;
  - gzip header fields, trailing garbage, multi-member and corrupt input;
  - PNG 1-bit, palette, 16-bit, RGBA and interlaced images, bad CRC, trailing bytes;
  - fuzz input;
  - 3,000 adversarial E8/E9 buffers;
  - truncated and bit-flipped containers;
  - snapshot output identical to replay output.
- **Determinism (tested):** `-O0` and `-O3 -march=native` builds give byte-identical output on all 3 profiles.
  - Since 2026-10-23 this also holds across two different x86 machines (section 7).
  - Every harness run checks it again: output must be identical with 1 and 4 threads, `-O0` must match `-O3` on one file per kind, and repeated engine runs must match.
  - Every harness run also decompresses every output in a separate process.
- **Sanitizers:** ASan and UBSan were clean on all profiles.
- **Bug found and fixed:** the first E8/E9 transform only consumed 5 bytes when it transformed, so its inverse could take different paths on overlapping opcodes. 75 of 3,000 adversarial buffers failed. The benchmark CRC caught it on one ELF. The fix: every E8/E9 opcode consumes its 4 operand bytes.

---

## 5. Design decisions so far

**Technical:**
- **Version tags (agreed design):**
  - A mind version = a list of module IDs + the mixer weights trained for exactly that combination.
  - Released modules are immutable; new learning goes into new modules.
  - Each file stores a 4-byte version ID, and the decoder loads exactly that set.
  - A module is deleted only when no stored file references it.
  - Idle-time recompression moves old files to newer versions, so stored data shrinks over time.
  - **Correction, decided 2026-10-23: the version must also pin the engine algorithm.**
    - The problem: step 2 changes the mixer arithmetic. A file written before step 2 would decode wrongly with the new engine, even with the same modules and weights.
    - The fix: the version ID names a complete decoder recipe. That is a codec ID (engine algorithm), the engine settings (maxbits, tunables), the module list (each with its sha256) and the mixer weights.
    - Released codecs stay in the code, frozen. A changed engine gets a new codec ID. (Before the first release only the current codec is kept: section 2.4.)
    - The per-file 16-bit primer tag is replaced by the registry's module hashes.
  - Both corrections are implemented in format 2 (section 2.1).
  - **Correction, decided 2026-10-23: small IDs and no table for one-block files.**
    - Version ID and profile ID are varints: 1 byte for the first 128 IDs.
    - A file with one block carries no block table.
    - The reason: a fixed 4-byte ID plus a block table adds about 15 bytes per file. That is +2–5% output on small web/JSON files (estimate).
    - With both changes a v2 header is no larger than a v1 header: about 15 against 17 bytes (estimate).
- **Profiles and presets (agreed):**
  - Profile tree, e.g. general → text → Python → Python 3.15, and image → screenshot.
  - The detector picks the deepest matching profile.
  - Each profile has a preset: layers on/off, settings and mind modules.
  - An automated tuner works across train, validation and test splits.
  - A sub-profile survives only if it is ≥1% better than its parent on held-out data.
  - Expected tasks are like "find the best preset for Python 3.15 / screenshots".
- **Pooling (the author's idea), dropped 2026-10-24:** group all occurrences of a repeated piece and code them once (fix E, plan step 5). Measured: CM codes these repeats in fewer bits than a copy needs, and the speed gain within the 0.5% rule is 1.02–1.11× (section 2.7).
- **Speed fixes:**
  - A (keep the model loaded) — yes.
  - B (16-bit SIMD mixer) — the author first called it a side help. It was estimated at 1.5–2× with no ratio cost.
    - Done in step 2. The mixer alone gave 1.23–1.36×, because the real build was already vectorized. Together with output-neutral changes elsewhere in the per-bit work it gave 1.50–1.69× on a quiet machine, with output 0.06% smaller (measured, section 2.2).
  - C (fast mode with fewer models) — the author: avoid unless everything else is exhausted. Its estimated cost is 5–10% relative larger output, e.g. 13.4% → 14.1–14.7%, not 10 points.
  - D (blocks across cores) — yes; datacenter CPUs have many cores.
  - E (hybrid copy + model) — dropped after measuring (section 2.7).

**Primers:** the development primers contain Ubuntu system files verbatim, under many licenses, so they are not published. This release ships without primers; primers built only from public-domain data are plan step 7.

---

## 6. The optimization plan (agreed; steps 0–4 done by 2026-10-24, step 5 dropped, step 6 done 2026-10-25; re-measured 2026-10-26, section 2.9)

Speed first, then ratio. Every step is measured against the baseline on held-out data.

**Status:**
- **Step 0 is done** (2026-10-23): `tools/harness/`, with results in section 3.5.
  - A quick run takes about 5 minutes on this machine; the full tier takes about 25 minutes.
  - It also found and fixed two overhead problems outside the model, with no change to any output (section 3.5).
- **Step 1 is done** (2026-10-24): file format 2, with its measured effect in section 2.1.
- **Step 2 is done** (2026-10-24): codec 2, 1.50–1.69× faster per core, output 0.06% smaller (section 2.2). Content-hash version IDs were added at the same time, at the author's request (section 2.3).
- Codec 1 and format 1 were dropped (2026-10-24), because nothing is released yet (section 2.4).
- **Step 3 is done** (2026-10-24; model kept loaded: workers, write log, `mixz.py serve`; section 2.5).
- **Step 4 is done** (2026-10-24; blocks coded in parallel, equal blocks; section 2.6). Splitting stays opt-in, because the measured size cost is +6.6% at 2 MiB blocks.
- **Step 5 was dropped** (2026-10-24, by the author; section 2.7). Copies cost more bits than CM spends on repeats: 2× decompression on Python costs about +13% output.
- **Step 6 is done** (2026-10-25, accepted by the author; section 2.8). Codec 3 adds the code and web text profiles and a 2-byte shortest match for images: −0.65% on the full test set, −1.0% to −1.7% on code and web, with the model 0.7–1.7% slower.
- **Re-measured on 2026-10-26** (section 2.9): every step on one machine, per-file overhead, cores, blocks on 100–205 MB files. Built then, waiting for acceptance: bundles, the snapshot cache at 8, block size chosen from the file size.

| # | Step | What | Target | When |
|---|---|---|---|---|
| 0 | Benchmark harness | Fixed test set per profile; every change reports size change, speed change and round-trip result | Same numbers on every run | Days 1–2 |
| 1 | File format v2 | Mind-version tag, profile ID, block table | Old (v1) and new files both decompress | Days 2–4 |
| 2 | Cheaper mixer (B) | 16-bit weights and inputs, vector multiply-add, merged small steps | ≥1.5× per core, ≤0.5% larger output | Week 1–2 |
| 3 | Model kept loaded (A) | Service mode with primed model in memory | Start-up 1.5 s → <0.1 s | Week 2 |
| 4 | Blocks across cores (D) | Each block starts from the primed model | ≥1.8× on 2 cores, ≤4% larger at 4 blocks | Week 2–3 |
| 5 | ~~Pool + copy layer (E)~~ | Dropped: copies cost more bits than CM spends on repeats (section 2.7) | — | — |
| 6 | Profiles + tuner | Profile tree, detector, automatic preset search | Sub-profile kept only if ≥1% better than parent | Weeks 4–6 |
| 7 | Mind v1 | Immutable modules, larger knowledge, licensed training data | ≥25% smaller than zstd+dict per text profile | Weeks 5–7 |
| 8 | Later, ratio only | Neural predictor input, JPEG, non-zlib deflate (preflate-style) | Decided per item with the harness | After 7 |

**Rules:**
- Every output must round-trip exactly.
- A speed change costing >0.5% output size needs the maintainer's approval.
- Speed is measured on 1 core and on all cores, on the same machine each time.
- Step 1 lands before any step that changes output.

**Speed result of steps 2–4 (measured):** the model is 1.50–1.69× faster per core; with the model kept loaded, many small files compress 1.86× and decompress 2.36× faster on 1 core; one large file can be split across cores at a size cost. The earlier estimate of 3–4× per core assumed step 5.

**After the optimization phase:**
- Hardware: many-core CPUs now; GPUs for many parallel streams or a neural predictor; FPGA/ASIC only once the model design is frozen.

---

## 7. Known limitations (current code)
- Speed (section 3.3).
- Only zlib-reproducible gzip/PNG streams are recompressed.
- No JPEG, MP3, MP4 or ZIP support.
- No archive mode; one file uses one core.
- Service mode (`mixz.py serve`) uses Unix sockets, so it works on Linux and macOS, not on Windows. The CLI client is Python, so a call through the server still costs about 85–105 ms besides the coding (section 2.5).
- ~~The decoder trusts header sizes.~~ Fixed in step 1: sizes are checked before allocating (section 2.1).
- Corpus globs in `tools/corpora.py` are Linux paths. On macOS or Windows, `bench.py` and `build_primers.py` need new corpora (plan step 0 replaces them anyway).
- The engine build uses `cc` with `-march=native`, falling back to `-O3`/`-O2`. Windows has no automatic build.
- ~~gzip/PNG recompression depends on the system zlib.~~ Fixed in step 1: zlib 1.3.1's compressor is built into the engine (section 2.1). Before that, a machine with a different zlib could fail to decode such files (safely, through the CRC check).
  - Tested 2026-10-23: zlib 1.3.1 on Arch rebuilt the VM's `.gz` and `.png` vectors exactly.
  - Not tested: zlib-ng, which as far as known Fedora 40+ uses as its system zlib.
- Codec 2's fast mixer path needs AVX2 and is chosen when the engine is compiled. A build for a generic x86-64 target (plain `-O3`) uses the scalar path: the output is identical, but it is slower. Choosing the path at run time is not built yet, and a release binary for datacenters needs it.
- Cross-platform determinism:
  - Tested 2026-10-23 between x86 machines, for codec 1. The VM (Intel Xeon, AVX-512, Ubuntu) and the Ryzen desktop (AMD Zen 1, AVX2, Arch, gcc 15.2) gave byte-identical output: all vectors, with `-O0`, `-O3` and `-O3 -march=native` builds.
  - Tested 2026-10-24 for codec 2 on the Pentium test machine: Intel Pentium E5700 (2009, SSSE3 only, no AVX2), Ubuntu 26.04.
    - All codec 2 vectors recompress byte-identically, and the full round-trip suite passes (119/119), with gcc 15.2 `-O3 -march=native`, gcc `-O0` and clang 21.1 `-O3 -march=native`.
    - Without AVX2, gcc runs the plain mixer path, and clang turns that path into SSE2/SSSE3 vector code. That is a third code path, and it gave identical output.
  - x86 against arm64 is still untested. `tests/check_vectors.py` is the test.
  - The encoder's output also depends on the zstandard version, because primer choice ranks primers by zstd's compressed size. The check used zstandard 0.25.0 on both machines. Decoding does not depend on it.
- The `corpora.py` train/test split is by normalized path, which only matches the VM's file layout.
  - Measured on Arch: the test half overlapped the primers as much as the train half. For example, 10% against 15% of Python files had more than 5% of their bytes inside 64-byte strings of a primer.
  - Fixed 2026-10-23: `bench.py` and the harness drop files by content overlap (`tools/overlap.py`).
